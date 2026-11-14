"""공통 응답 형식. 프론트(src/api/client.js)가 기대하는 모양에 맞춰요.

성공: { "success": true,  "data": ... }
실패: { "success": false, "error": { "code": "...", "message": "..." } }
"""
from types import UnionType
from typing import Any, Union, get_args, get_origin

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel


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
        # 처리되지 않은 500은 사용자 middleware 바깥에서 반환될 수 있다.
        headers={"Cache-Control": "no-store"},
        content={"success": False, "error": {"code": code, "message": message}},
    )


def _body_models(annotation):
    if get_origin(annotation) in (Union, UnionType):
        return [model for member in get_args(annotation) for model in _body_models(member)]
    return [annotation] if isinstance(annotation, type) and issubclass(annotation, BaseModel) else []


def _missing_preview(request: Request, error: RequestValidationError) -> bool:
    """Translate missing approval identity, without masking ordinary validation.

    Inspect the matched route's required fields instead of guessing from URLs.
    Material editing shares a route with approval, so an incomplete edit must
    retain its own validation errors rather than demand an approval identity.
    """
    errors = error.errors()
    if any(
        item['type'] != 'missing'
        and not (error.body is None and item['loc'][0] == 'body'
                 and item['type'] == 'model_attributes_type')
        for item in errors
    ):
        return False
    if any(tuple(item['loc']) == ('query', 'previewId') for item in errors):
        return True
    dependant = getattr(request.scope.get('route'), 'dependant', None)
    models = [
        model for field in getattr(dependant, 'body_params', ())
        for model in _body_models(field.field_info.annotation)
    ]
    approvals = [model for model in models if 'previewId' in model.model_fields
                 and model.model_fields['previewId'].is_required()]
    if not approvals:
        return False
    # An absent body and a JSON null body both contain no approval identity.
    if error.body is None:
        return any(item['loc'][0] == 'body' for item in errors)
    if not isinstance(error.body, dict) or 'previewId' in error.body:
        return False
    edit_fields = {name for model in models if model not in approvals for name in model.model_fields}
    if edit_fields.intersection(error.body):
        return False
    return any(item['loc'][0] == 'body' and item['loc'][-1] == 'previewId' for item in errors)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, e: ApiError):
        return _fail(e.code, e.message, e.status)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, e: RequestValidationError):
        if _missing_preview(request, e):
            return _fail("PREVIEW_REQUIRED", "적용할 미리보기 ID를 보내 주세요", 409)
        return _fail("INVALID_INPUT", "보낸 값의 형식이 맞지 않아요", 422)

    @app.exception_handler(HTTPException)
    async def _http(_: Request, e: HTTPException):
        return _fail(f"HTTP_{e.status_code}", str(e.detail), e.status_code)

    @app.exception_handler(Exception)
    async def _unknown(_: Request, e: Exception):
        return _fail("SERVER_ERROR", "서버에서 문제가 생겼어요", 500)
