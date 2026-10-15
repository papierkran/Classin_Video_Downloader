"""统一异常体系 + FastAPI 全局异常处理器。

设计要点（Why 这样分层）：
- 业务异常 BizError：可预期、要展示给用户看的（如"已有任务运行中"），
  由 service 层抛出，控制器零 if/else 判断，符合"薄控制器"原则。
- 系统异常：不可预期的，统一在 handler 兜底为 500，对外只回
  {"code":500,"message":"服务器内部错误"}，避免把堆栈/路径泄露给公网用户。
- 所有错误响应统一为 {code, message, request_id}，前端一套拦截逻辑即可。
"""
from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger("app.error")


class BizError(Exception):
    """业务异常基类。http_status 默认 409：业务冲突是常见场景。"""

    def __init__(self, message: str, status_code: int = 409, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code or f"BIZ_{status_code}"


class ValidationFailure(BizError):
    """业务校验失败（参数格式合法但业务规则不满足，如 Cookie 为空）。"""

    def __init__(self, message: str):
        super().__init__(message, status_code=422, code="VALIDATION_FAILED")


class AuthError(BizError):
    """未登录/登录失效/账号密码错误。"""

    def __init__(self, message: str = "未登录或登录已过期"):
        super().__init__(message, status_code=401, code="UNAUTHORIZED")


class ForbiddenError(BizError):
    """已登录但无权限（当前只有 admin/member 两个角色）。"""

    def __init__(self, message: str = "没有权限执行该操作"):
        super().__init__(message, status_code=403, code="FORBIDDEN")


class NotFoundError(BizError):
    """目标资源不存在（查不存在的任务/课堂/视频统一用它）。"""

    def __init__(self, message: str = "资源不存在"):
        super().__init__(message, status_code=404, code="NOT_FOUND")


class ConflictError(BizError):
    """状态机冲突：如重复启动任务、对已结束任务执行暂停。"""

    def __init__(self, message: str):
        super().__init__(message, status_code=409, code="CONFLICT")


class RateLimitedError(BizError):
    """触发限流（目前仅登录失败次数超限）。"""

    def __init__(self, message: str = "操作过于频繁，请稍后再试"):
        super().__init__(message, status_code=429, code="RATE_LIMITED")


def _error_response(request: Request, status: int, code: str, message: str) -> JSONResponse:
    """构造统一错误体。request_id 便于用户报障时按 ID 在服务端日志定位。"""
    request_id = getattr(request.state, "request_id", None) or uuid.uuid4().hex[:12]
    return JSONResponse(status_code=status, content={"code": status, "code_str": code, "message": message, "request_id": request_id})


def register_exception_handlers(app: FastAPI) -> None:
    """把四类异常收敛到统一响应格式（Why 集中注册：新路由自动继承）。

    四个 handler 覆盖了 FastAPI 能产生的全部响应路径：
    - BizError：service 主动抛的业务错（可预期，文案面向用户）；
    - RequestValidationError：Pydantic 格式错（面向开发者但要翻译）；
    - Exception：兜底（面向用户但不含细节，只给 request_id）；
    - 404/405 由 Starlette 原生处理（返回 {"detail": ...}，与本格式
      不一致但它们不经过业务代码，前端对 404 只做跳转，无需统一）。
    """

    @app.exception_handler(BizError)
    async def _biz(request: Request, exc: BizError):
        return _error_response(request, exc.status_code, exc.code or "BIZ", exc.message)

    @app.exception_handler(RequestValidationError)
    async def _pydantic(request: Request, exc: RequestValidationError):
        # Why 手动拼消息：Pydantic 原始 errors 含 ctx/loc/executor 等内部结构，
        # 对普通用户不友好，这里收敛成第一条可读消息（loc 去掉 body 前缀，
        # 只留字段名，如 "lesson_id: 参数校验失败"）。
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", []) if x != "body")
        msg = first.get("message", "参数校验失败")
        detail = f"{loc}: {msg}" if loc else msg
        return _error_response(request, 422, "REQUEST_INVALID", detail)

    @app.exception_handler(Exception)
    async def _unknown(request: Request, exc: Exception):
        # Why 记完整堆栈到服务端、对外只给 request_id：公网部署防信息泄露
        # （堆栈会暴露绝对路径/库版本，是攻击者的地图）；request_id 让
        # 用户报错时能一句话定位——"报错弹窗里有串 ID，你查下日志"。
        request_id = getattr(request.state, "request_id", None) or uuid.uuid4().hex[:12]
        logger.exception("未处理异常 request_id=%s path=%s", request_id, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"code": 500, "code_str": "INTERNAL", "message": f"服务器内部错误（request_id: {request_id}）", "request_id": request_id},
        )
