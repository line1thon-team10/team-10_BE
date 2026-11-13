"""소지 기반 익명 세션 자격과 사용자 응답을 HTTP 캐시에 저장하지 않는다."""
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class ApiNoStoreMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return

        async def send_no_store(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(key, value) for key, value in message.get("headers", [])
                           if key.lower() != b"cache-control"]
                message = {**message, "headers": [*headers, (b"cache-control", b"no-store")]}
            await send(message)

        await self.app(scope, receive, send_no_store)
