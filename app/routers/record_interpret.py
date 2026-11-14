from fastapi import APIRouter, Depends
from sqlalchemy import Connection
from app.core.auth import current_user
from app.core.responses import ok
from app.db.session import get_conn
from app.schemas.record_interpret import InterpretRecord
from app.services.record_interpret import RecordInterpretService

router = APIRouter(prefix='/api/records',tags=['기록 해석'])


@router.post('/interpret')
def interpret(command: InterpretRecord,user: dict=Depends(current_user),conn: Connection=Depends(get_conn)):
    engine = conn.engine
    conn.close()
    return ok(RecordInterpretService(engine).interpret(user['id'],command.taskId,command.text))
