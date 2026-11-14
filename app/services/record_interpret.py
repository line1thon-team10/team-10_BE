"""Read-only record candidates with source validation and a stale-input fence."""
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from uuid import UUID

from app.ai.gateway import AiGateway, GatewayRequest, InvalidOutput, MaterialRevision, ResultIdentity
from app.ai.record_interpret_adapter import MODEL_VERSION, PROMPT_VERSION, SCHEMA_VERSION, PROVIDER, VALIDATOR, explicit_ranges, validate_record_result
from app.core.clock import today_for
from app.core.responses import ApiError
from app.db import schema as t
from app.repositories.locking import UserBusyError, lock_user
from app.repositories.proposals import canonical_hash
from app.services.records import owned_task
from app.services.study import StudyData


def _capture(conn, user_id, task_id, text):
    try:
        owner = lock_user(conn,UUID(user_id))
    except UserBusyError:
        raise ApiError('REQUEST_IN_PROGRESS','다른 변경을 처리 중이에요. 다시 시도해 주세요',409) from None
    if owner is None:
        raise ApiError('UNKNOWN_SESSION','사용자를 찾지 못했어요',401)
    user = owner.to_mapping()
    data = StudyData(conn,user_id,subject_statuses=('draft','active','archived'))
    task = owned_task(data,task_id)
    materials = {row['id']:dict(row) for row in conn.execute(select(t.materials).where(
        t.materials.c.id.in_({part['material_id'] for part in task.ranges}))).mappings()}
    units = {row['id']:dict(row) for row in conn.execute(select(t.units).where(
        t.units.c.id.in_({part['unit_id'] for part in task.ranges}))).mappings()}
    ranges = []
    for part in task.ranges:
        material,unit = materials[part['material_id']],units[part['unit_id']]
        left,right = part['start_page'],part['end_page']
        if (left is None or right is None or not isinstance(material['page_count'],int)
                or not 1 <= left <= right <= material['page_count']):
            raise ApiError('PARSE_FAILED','실제 PDF 페이지가 있는 범위만 해석할 수 있어요',400)
        ranges.append({'id':part['id'],'materialId':material['id'],'materialName':material['original_name'],
            'unitTitle':unit['title'],'from':left,'to':right})
    context = {'text':text,'taskId':task_id,'title':task.row['title'],'ranges':ranges}
    basis = canonical_hash(jsonable_encoder({'task':task.row,'ranges':task.ranges,'record':task.record,
        'doneRanges':task.done_ranges,'materials':materials,'units':units,'plan':dict(data.plan) if data.plan else None,
        'today':today_for(user),'timezone':user['timezone']}))
    return context,basis,tuple(MaterialRevision(row['id'],str(row['sha256'])+':'+str(row['analysis_version']))
                               for row in materials.values())


class RecordInterpretService:
    def __init__(self, engine, *, gateway=None):
        self.engine = engine
        self.gateway = gateway or AiGateway(engine)

    def interpret(self, user_id, task_id, text):
        with self.engine.begin() as conn:
            context,basis,materials = _capture(conn,user_id,task_id,text)
        try:
            explicit_ranges(text,context['ranges'])
        except ValueError:
            raise ApiError('PARSE_FAILED','자료 이름과 완료한 페이지를 정확히 알려 주세요',400) from None
        identity = ResultIdentity.create(kind='nl_parse',provider='groq',model_version=MODEL_VERSION,
            prompt_version=PROMPT_VERSION,schema_version=SCHEMA_VERSION,input=context,
            validation_context=context,materials=materials)
        outcome = self.gateway.run(GatewayRequest(user_id,identity),validator=VALIDATOR,provider=PROVIDER)
        try:
            result = validate_record_result(outcome.data,context)
        except InvalidOutput:
            raise ApiError('AI_FAILED','검증된 페이지 후보를 준비하지 못했어요',503) from None
        if not result['isConfirmed']:
            raise ApiError('PARSE_FAILED','완료한 범위를 확정할 수 없어요. 다시 알려 주세요',400)
        with self.engine.begin() as conn:
            try:
                _,current,_ = _capture(conn,user_id,task_id,text)
            except ApiError as error:
                if error.code == 'UNKNOWN_SESSION':
                    raise
                raise ApiError('INPUT_STALE','해석하는 동안 태스크나 자료가 바뀌었어요. 다시 요청해 주세요',409) from None
            if current != basis:
                raise ApiError('INPUT_STALE','해석하는 동안 태스크나 자료가 바뀌었어요. 다시 요청해 주세요',409)
        completed = result['completedRanges']
        single = len(context['ranges']) == 1 and len(completed) == 1
        by_id = {row['id']:row for row in context['ranges']}
        label = ', '.join(f"{by_id[row['rangeId']]['materialName']} {by_id[row['rangeId']]['from']}~{row['toPage']}쪽"
                          for row in completed) if completed else '완료한 페이지 없음'
        return {'from':context['ranges'][0]['from'] if single else None,
            'to':completed[0]['toPage'] if single else None,'completedRanges':completed,'label':label}
