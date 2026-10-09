"""仪表盘统计路由：登录后首页一次性取齐所有汇总数字。

把统计做成单一端点（而非首页并行打 4-5 个请求）：SQLite 单库场景下
合并查询反而更快，前端也少几次状态同步竞态。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db.models import User
from ..schemas import StatsOut
from ..services.dashboard_service import DashboardService
from .deps import current_user, db_session

router = APIRouter(prefix="/api/dashboard", tags=["仪表盘"])


@router.get("/stats", response_model=StatsOut)
def stats(_: User = Depends(current_user), db: Session = Depends(db_session)):
    """返回课堂/任务汇总 + 最近 5 个任务（首页卡片与"最近任务"表共用）。"""
    return DashboardService(db).stats()
