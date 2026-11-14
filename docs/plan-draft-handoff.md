# 초기 계획과 승인 후 구조 후보의 공통 배분

공개 P-02 경로는 `GET /api/plan/draft`, `POST /api/plan/approve`다. 최초 계획과 기존 계획에 draft 과목 추가를 서로 다른 후보 종류로 저장하며 plan header/revision은 `ProposalService`만 변경한다.

M-05의 읽기 전용 입력은 `existing_allocation_input(connection, user_id, today=..., timezone_name=..., subject_ids=(), material_structure_overrides=..., affected_subject_ids=(subject_id,))`로 준비한다. 반환은 `(AllocationInput, supersede_ids, display_input)`이다. `subject_ids=()`는 과목 추가가 없는 자료 구조 편집에 사용한다.

`material_structure_overrides`는 `{material_id: {"units": [...], "concepts": [...]}}`다. 단원은 UUID id/material_id, sort_order, title, start_page/end_page, in_scope와 선택적인 difficulty/mastery_score를 가진 DB 형태이며 개념은 UUID id/unit_id, start_page/end_page, in_scope를 가진다. 신규/retire ID 정책은 자료 도메인이 결정한다. 입력 변환은 후보 행을 DB에 쓰지 않는다.

- 변경 과목의 기존 실제 학습 페이지는 material별 합집합으로 차감한다. 구조 교체로 단원 ID가 바뀌어도 이미 학습한 페이지를 새 학습으로 복제하지 않는다.
- 시작한 기존 태스크는 retired 단원 ID를 그대로 참조한다. 이 과거 단원은 새 의무가 없는 빈 scope로 solver에 제공한다.
- 타과목 태스크와 미래 보충/복습은 고정한다. 범위 편집으로 다른 과목을 임의 재배분하지 않는다.
- 과거 미완료 보충은 `PendingObligations`이며 API에서는 `409 OVERDUE_OBLIGATIONS`를 반환한다. 모든 범위 제외 등 `PlanInputError`는 M-05의 도메인 문맥에 맞춰 `409 PLAN_INFEASIBLE` 또는 소유권/입력 오류로 매핑한다.
- 승인에서 후보 구조를 먼저 저장하고 같은 트랜잭션의 `plan_draft._apply`에 `plan-placements-v1` changes를 전달한다. 이 helper는 새 task/range를 저장하고 `supersedeIds`를 carried_over로 표시하되 과거 원본/기록을 삭제하지 않는다. plan 자체는 만들거나 revision을 올리지 않는다.

Fraction snapshot은 `[분자,분모]`다. `placements`는 id, subjectId, kind, date, start, end, minutes, title, parts를 가지며 parts는 unitId/materialId/from/to/baseline/multiplier를 가진다. `activateSubjectIds=[]`이면 기존 과목 편집에서 draft 과목을 추가하지 않는다. 저장된 후보의 changes만 적용하며 사용자에게 전달된 표시용 숫자를 다시 승인 입력으로 받지 않는다.
