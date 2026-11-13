"""공통 응답 형식. 프론트(src/api/client.js)가 기대하는 모양에 맞춰요.

성공: { "success": true,  "data": ... }
실패: { "success": false, "error": { "code": "...", "message": "..." } }
"""
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


def ok(data: Any = None) -> dict:
    return {"success": True, "data": data}


class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code = code
        self.message = message
        self.status = status


def _fail(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"success": False, "error": {"code": code, "message": message}},
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, e: ApiError):
        return _fail(e.code, e.message, e.status)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, e: RequestValidationError):
        return _fail("INVALID_INPUT", "보낸 값의 형식이 맞지 않아요", 422)

    @app.exception_handler(HTTPException)
    async def _http(_: Request, e: HTTPException):
        return _fail(f"HTTP_{e.status_code}", str(e.detail), e.status_code)

    @app.exception_handler(Exception)
    async def _unknown(_: Request, e: Exception):
        return _fail("SERVER_ERROR", "서버에서 문제가 생겼어요", 500)
