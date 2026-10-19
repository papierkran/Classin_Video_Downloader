"""路由聚合：main.py 只需 include 一个 api_router。

Why 单聚合点：新增模块只需在此 include 一行，main.py 保持稳定；
同时这里也是"路由清单"的唯一权威位置，评审权限覆盖时看这一个文件即可。
"""
from fastapi import APIRouter

from . import auth, console, dashboard, lessons, settings, sync, tasks, videos

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(console.router)
api_router.include_router(dashboard.router)
api_router.include_router(lessons.router)
api_router.include_router(tasks.router)
api_router.include_router(settings.router)
api_router.include_router(sync.router)
api_router.include_router(videos.router)

__all__ = ["api_router"]
