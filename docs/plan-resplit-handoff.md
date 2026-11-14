# 계획 재분할·재생성 인계

P-03의 POST/GET/DELETE `/api/plan/resplit`와 POST `/api/plan/resplit/apply`, U-06의 POST `/api/plan/rebuild`를 구현한다. 변경 요청과 한 번의 후속 질문은 소유 사용자별 `plan_change_requests`에 보존한다. requestId를 보낼 때 원래 요청 문장이 일치해야 하고, 답변만 보내거나 이미 제출한 답변을 다른 값으로 바꾸는 요청은 거절한다. 해석이나 배분 중 트랜잭션을 유지하지 않고 저장 직전에 자료·목표·일정·기록·집중 상태·보충 의무와 계획 revision을 다시 확인한다.

첫 승인 전에는 기존 초기 후보를 비교 기준으로 삼는다. resplit/apply는 저장된 배치로 새 initial_plan 후보를 만들고 같은 대상의 이전 초기 후보를 취소한다. subjects/plans/study_tasks는 변경하지 않는다. 이후 GET draft는 새 후보를 반환한다. POST plan/approve는 재분할 ID도 받으며, 서버가 연결된 초기 후보를 해석하여 최초 계획을 승인한다. 재분할 초안을 선택하지 않고 직접 approve하더라도 선택과 첫 승인을 같은 트랜잭션에서 처리한다.

승인 뒤에는 실제 학습 페이지를 차감하고 시작한 태스크·미기록 종료 세션·기존 보충 및 복습을 보존한다. 아직 배정하지 않은 보충 의무도 공용 입력 어댑터의 WorkRequirement로 포함하며 의무별 block 연결을 유지한다. 배분 변경은 이전 미완료 행을 carried_over로 남기고 새 태스크를 추가한다. 기록·점수·책 이력을 삭제하지 않는다. 후보 생성만으로 계획을 바꾸지 않으며 승인 성공은 revision을 한 번만 올린다. 같은 승인을 재전송하면 최초 응답을 반환한다.

GET 비교는 실제 배치의 분량을 날짜별 정수 before/after와 총시간·태스크 수로 반환한다. previewId 생략은 해당 사용자의 준비된 재분할 후보가 하나일 때만 허용한다. DELETE는 ID를 필수로 받으며 후보만 취소한다. 저장 오류는 후보·구조·계획 변경을 원자적으로 되돌린다.

자연어 해석은 `plan_redo` gateway와 ObservedGroq를 통과한다. 날짜/요일/과목/일일 분량은 원문과 후속 답변의 명시 근거로 검증한다. 불명확한 요청은 한 번 질문하며 두 번째 질문을 성공으로 바꾸지 않는다. AI가 직접 배치나 페이지 수를 정하지 않고 기존 allocation-v1 계산기가 계획을 만든다. `saved:true`는 정확한 문맥의 저장 해석만 사용하며 외부 호출 없이 실패를 반환할 수 있다. request를 생략한 rebuild는 현재 일정과 목표에 따른 결정적 재배분이므로 외부 AI를 호출하지 않는다.

로컬 E2E는 HTTP·트랜잭션·gateway ProcessExecutor·ObservedGroq를 통과하며 유료 Groq transport만 대체한다. 실제 공급자의 의미 품질은 별도 AI 품질 gate다. FE 변경, 개발 중 CI, push는 하지 않는다.
