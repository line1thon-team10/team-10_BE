# API enum과 DB 변환

API는 FE enum을 사용하며 DB enum을 그대로 노출하지 않는다.



| 무엇                       | 값                                                                          | 어디                                |
| -------------------------- | --------------------------------------------------------------------------- | ----------------------------------- |
| 블록(책) 상태              | `planned` · `doing` · `done` · `stable` · `shaky` · `risk`                  | today `recentDays` · books `blocks` |
| 단원 등급                  | `stable` · `shaky` · `risk` · null (퀴즈 전)                                | books · quiz `unitChanges`          |
| 태스크 상태                | `todo` · `partial` · `done` · `none`                                        | today `tasks[].status`              |
| 기록 result (요청)         | `done` · `partial` · `none`                                                 | `POST /api/records`                 |
| 퀴즈 문항 유형             | `choice` (OX 포함) · `short` · `essay`                                      | quiz `questions[].type`             |
| 확신도                     | `sure` · `unsure`                                                           | 퀴즈 제출 · 다시 풀기               |
| 오답 노트 result           | `sureWrong` · `unsureWrong` · `unsureRight` · `resolved`                    | review notes                        |
| 채점 방식                  | `ai` · `saved`                                                              | quiz result `essayGrading`          |
| 난이도                     | `low` · `mid` · `high`                                                      | units · custom-quiz                 |
| 자정 판정 judgment         | `carry` · `replan` · `none`                                                 | demo next-day                       |
| 기록 event type            | `box` · `toast`                                                             | records · replan 응답               |
| 기록 event reason          | box: `dayClose` · `unitDone` · `recover` / toast: `record` · `rate70`       | records · replan 응답               |
| 안내 notice type           | `carry` · `replanned`                                                       | today `notice`                      |
| 분배안 key                 | `A` · `B` · `C`                                                             | replan                              |
| 분배안 kind                | `overdue` · `pace`                                                          | replan                              |
| 집중 세션 status           | `active` · `paused` · `ended`                                               | study sessions                      |
| 자료 분석 status           | `running` · `done` · `failed`                                               | materials · jobs                    |
| S1 과목 status             | `pending` · `asking` · `confirmed`                                          | onboarding                          |
| 일정 kind                  | `class` · `job` · `meet` · `other`                                          | onboarding · calendar               |
| 일정 삭제 scope            | `once` · `future`                                                           | `DELETE /api/schedules/:id`         |
| 빈 시간 안 option          | `rest` · `pull` · `advance`                                                 | empty-time                          |
| 오늘 계획 action           | `more30` · `rest` · `postpone`                                              | plan/today                          |
| 오답 노트 묶음 group       | `date` · `unit` · `count`                                                   | review notes                        |
| 튜터 저장된 결과 kind      | `basic` · `easy`                                                            | tutor/backup                        |
| 데모 시드                  | `empty` · `day9` · `overdue`                                                | demo/seed                           |
| AI 상태 재현 target · mode | `plan` · `quiz` · `grading` · `tutor` · `replan` / `null` · `slow` · `fail` | demo/ai                             |


## 추가/변환

- custom quiz weight: even / weak.
- DB task planned/partial/completed/missed → API todo/partial/done/none. excluded/carried_over는 현재 task 목록에서 제외.
- DB study running/paused/ended/auto_stopped → API active/paused/ended/ended. auto_stopped는 autoStopped=true.
- DB quiz ox → API choice, options=[O,X] 순서는 저장된 보기 순서를 유지.
- DB confidence sure/unsure → AI 확실/애매, low/mid/high → 하/중/상.
- PDF ai_jobs pending/processing→running, completed→done, failed→failed.
- perfect는 회독 조건에 따른 블록 상태 추가안이며 FE 렌더러 대응이 필요하다. 단원 grade에는 포함하지 않는다.
