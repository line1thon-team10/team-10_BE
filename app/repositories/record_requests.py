"""First successful record responses, within the caller's user-locked transaction."""
import hashlib
from uuid import UUID

from fastapi import Header
from sqlalchemy import select

from app.ai.gateway import canonical_json
from app.core.clock import now_utc
from app.core.responses import ApiError
from app.db import schema as t


def record_request_key(values: list[str] | None = Header(default=None, alias='Idempotency-Key')):
    if values is None:
        return None
    try:
        if len(values) != 1:
            raise ValueError
        return str(UUID(values[0]))
    except ValueError:
        raise ApiError('INVALID_INPUT', 'Idempotency-Key는 UUID 하나만 보내 주세요', 422) from None


class RecordRequestRepository:
    def __init__(self, conn, user_id, method, path, key, body):
        self.conn = conn
        self.identity = dict(user_id=user_id, method=method, path=path, idempotency_key=key)
        self.key = key
        self.request_hash = hashlib.sha256(canonical_json(body).encode()).hexdigest()

    def replay(self):
        if self.key is None:
            return None
        row = self.conn.execute(select(t.record_requests).filter_by(**self.identity)).mappings().first()
        if row is None:
            return None
        if row['request_hash'] != self.request_hash:
            raise ApiError('IDEMPOTENCY_CONFLICT', '같은 요청 키에 다른 기록을 보낼 수 없어요', 409)
        return row['response']

    def save(self, response):
        if self.key is not None:
            self.conn.execute(t.record_requests.insert().values(**self.identity,
                request_hash=self.request_hash, response=response, created_at=now_utc()))
        return response
