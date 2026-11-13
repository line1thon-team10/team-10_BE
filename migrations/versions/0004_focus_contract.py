"""Enforce one active session/open segment and snapshot focus presentation."""
import hashlib

from alembic import context, op
import sqlalchemy as sa

revision = "0004_focus_contract"
down_revision = "0003_demo_day_requests"
branch_labels = None
depends_on = None

# 이전 focus.py의 문구 순서. migration은 변경 가능한 앱 상수를 import하지 않는다.
QUOTES = ["벼락치기 말고, 피뢰침.", "지금은 이 범위만 보면 돼요.",
          "오늘 한 페이지가 시험 날의 한 문제예요.", "다 못 해도 괜찮아요. 한 만큼 쌓여요.",
          "밀려도 무너지지 않아요. 다시 맞추면 돼요.", "천천히 읽은 페이지가 오래 남아요.",
          "오늘의 블록 하나, 지금 쌓는 중이에요.", "어려운 곳에서 멈춘 것도 공부예요.",
          "한 번 더 본 개념은 덜 흔들려요.", "시험은 한 번, 준비는 매일 조금씩."]


def upgrade():
    conn = op.get_bind()
    # SQLite의 DDL rollback 차이도 고려해 어떤 DDL보다 먼저 검사한다.
    for query in (
        "SELECT user_id FROM study_sessions WHERE status IN ('running','paused') GROUP BY user_id HAVING count(*) > 1",
        "SELECT session_id FROM study_session_segments WHERE ended_at IS NULL GROUP BY session_id HAVING count(*) > 1",
    ):
        if not context.is_offline_mode() and conn.execute(sa.text(query)).first():
            raise RuntimeError("집중 중복 데이터가 있습니다. 삭제 없이 중단합니다. 백업 후 운영자가 확인해야 합니다.")
    op.add_column("study_sessions", sa.Column("expected_seconds", sa.Integer(), nullable=True))
    op.add_column("study_sessions", sa.Column("quote", sa.Text(), nullable=True))
    sessions = sa.table("study_sessions", sa.column("id", sa.Uuid(as_uuid=False)),
                        sa.column("task_id", sa.Uuid(as_uuid=False)), sa.column("expected_seconds", sa.Integer()),
                        sa.column("quote", sa.Text()))
    tasks = sa.table("study_tasks", sa.column("id", sa.Uuid(as_uuid=False)), sa.column("estimated_minutes", sa.Integer()))
    rows = [] if context.is_offline_mode() else conn.execute(
        sa.select(sessions.c.id, tasks.c.estimated_minutes).join(tasks, sessions.c.task_id == tasks.c.id)).all()
    for session_id, minutes in rows:
        quote = QUOTES[int(hashlib.md5(session_id.encode()).hexdigest(), 16) % len(QUOTES)]
        conn.execute(sessions.update().where(sessions.c.id == session_id)
                     .values(expected_seconds=max(1, int(minutes or 30)) * 60, quote=quote))
    op.create_index("uq_focus_active_user", "study_sessions", ["user_id"], unique=True,
                    postgresql_where=sa.text("status IN ('running', 'paused')"),
                    sqlite_where=sa.text("status IN ('running', 'paused')"))
    op.create_index("uq_focus_open_segment", "study_session_segments", ["session_id"], unique=True,
                    postgresql_where=sa.text("ended_at IS NULL"), sqlite_where=sa.text("ended_at IS NULL"))


def downgrade():
    op.drop_index("uq_focus_open_segment", table_name="study_session_segments")
    op.drop_index("uq_focus_active_user", table_name="study_sessions")
    op.drop_column("study_sessions", "quote")
    op.drop_column("study_sessions", "expected_seconds")
