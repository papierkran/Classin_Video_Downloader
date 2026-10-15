"""FastAPI 应用入口（前后端一体：API + 静态前端同进程托管）。

启动：python run.py 或 uvicorn backend.main:app --host 127.0.0.1 --port 8000

装配顺序即依赖顺序（Why 显式按序写而非散落各处）：
  配置 → 日志 → 建表/种子 → 恢复脏任务 → 异常兜底 → 路由 → 静态资源。
每一步都依赖前一步的产物（日志要 config 的路径、种子要表存在、
路由要异常处理器已注册），打散到装饰器里会让"启动失败查哪里"变难。
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import api_router
from .core.config import FRONTEND_DIR, settings
from .core.exceptions import register_exception_handlers
from .core.logging_conf import setup_logging
from .db.database import init_db
from .services.task_service import recover_stale_tasks

logger = logging.getLogger("app.main")


@asynccontextmanager
async def lifespan(_: FastAPI):
    """启动钩子：日志→建表→种子→崩溃任务恢复（顺序不可换）。

    Why 恢复脏任务放在最后：它要读 download_tasks 表（依赖建表），
    且清理动作要写日志（依赖日志已配置）。颠倒任一顺序都会在
    "服务重启后残留 running 任务"这个场景下出错。
    """
    setup_logging()
    init_db()
    recover_stale_tasks()
    logger.info("服务就绪 http://%s:%d", settings.host, settings.port)
    yield
    # 无更多 shutdown 钩子：下载线程是 daemon——进程退出时浏览器可能残留，
    # 但下次启动 recover_stale_tasks 会把状态清干净（优雅停机复杂度
    # 换不来单机场景的实际收益）


app = FastAPI(
    title="ClassIn 视频下载管理系统",
    version="1.0.0",
    lifespan=lifespan,
    # docs 保留在 /api/docs（非根路径）：自用排障方便，又不会被
    # 静态首页链到；公网可再用反代规则封掉（README 部署章节）
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)

# 必须在 include_router 之前注册：FastAPI 按注册顺序匹配 handler，
# 后注册的 ExceptionMiddleware 包裹先注册的路由才有兜底效果
register_exception_handlers(app)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """为每个请求挂 request_id（错误响应里回传，方便按 ID 查日志）。

    为什么用中间层而不是在 handler 里写：中间件对所有路由（含 404）
    生效，且业务代码零侵入——排障手段应该长在框架层，不该每个端点抄一遍。
    """
    request.state.request_id = uuid.uuid4().hex[:12]
    start = time.perf_counter()
    response = await call_next(request)
    elapsed = (time.perf_counter() - start) * 1000
    # Why 只记 /api 路径：静态资源访问量大且对排障无价值，避免日志噪音
    if request.url.path.startswith("/api"):
        logger.info("%s %s -> %d (%.0fms) rid=%s", request.method, request.url.path, response.status_code, elapsed, request.state.request_id)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


app.include_router(api_router)


@app.get("/api/health", tags=["健康检查"])
def health():
    """公网部署时给反向代理/监控用的存活探针（无鉴权：它存在的意义
    就是"在没登录/配置没就绪时也能探"，加鉴权反而失去作用）。"""
    return {"status": "ok"}


# 前端静态托管：/ 指向 frontend/index.html，其余资源按路径下发。
# Why 放在最后：路由按注册顺序匹配，API 优先于静态兜底。
# 只 mount /static 而非 /：mount('/') 会吞掉所有未匹配路径（含打错的
# /api/x），返回 index.html 而不是 404，把 API 错误伪装成前端路由。
_index = FRONTEND_DIR / "index.html"
if _index.exists():
    _static = StaticFiles(directory=str(FRONTEND_DIR))
    app.mount("/static", _static, name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(_index)
else:
    logger.warning("未找到前端入口 %s（仅提供 API）", _index)
