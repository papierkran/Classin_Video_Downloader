"""课堂同步路由：按钮式同步（配置读写 + 状态 + 待下载列表 + 立即同步 + Cookie 验证）。

全部端点要求 admin：同步凭据=Cookie（登录态），拉回来的课堂最终会进下载
队列——和系统设置同级的管理动作，普通成员只看任务/视频即可。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db.models import User
from ..schemas import CookieVerifyIn, CookieVerifyOut, SyncConfigOut, SyncConfigUpdateIn, SyncLessonOut, SyncRunOut, SyncStatusOut
from ..services.sync_service import SyncService
from .deps import db_session, require_admin

router = APIRouter(prefix="/api/sync", tags=["同步"])


@router.get("/config", response_model=SyncConfigOut)
def get_sync_config(_: User = Depends(require_admin), db: Session = Depends(db_session)):
    """读同步配置。Cookie 只回脱敏值 + "实际在用哪份"（using_download_cookie）。"""
    return SyncService(db).get_config()


@router.put("/config", response_model=SyncConfigOut)
def update_sync_config(payload: SyncConfigUpdateIn, _: User = Depends(require_admin), db: Session = Depends(db_session)):
    """部分更新；cookie 传空串=不修改（与系统设置页同一约定）。"""
    return SyncService(db).update_config(payload)


@router.get("/status", response_model=SyncStatusOut)
def get_sync_status(_: User = Depends(require_admin), db: Session = Depends(db_session)):
    """状态卡数据：凭据来源/上次结果/待下载新课数。"""
    return SyncService(db).get_status()


@router.get("/pending", response_model=list[SyncLessonOut])
def list_pending_lessons(_: User = Depends(require_admin), db: Session = Depends(db_session)):
    """待下载新课列表（同步新增、未纳入过任务），供同步页勾选后建任务。

    独立于"立即同步"返回体：进入页面即可看到历史遗留的待下载项，
    不必先点一次同步；下载建任务后 sync_pending 清标，列表自然收缩。
    """
    return SyncService(db).list_pending()


@router.post("/run", response_model=SyncRunOut)
def run_sync(_: User = Depends(require_admin), db: Session = Depends(db_session)):
    """立即同步（按钮触发，只拉取+入库，不建任何下载任务）。

    会真实访问控制台接口，同步实现由 FastAPI 放线程池；
    已有轮次在跑时返回 409（非阻塞闸门，见 sync_service.run）。
    """
    return SyncService(db).run()


@router.post("/verify-cookie", response_model=CookieVerifyOut)
def verify_sync_cookie(payload: CookieVerifyIn, _: User = Depends(require_admin), db: Session = Depends(db_session)):
    """验证控制台 Cookie（草稿 → 同步专用 → 下载回退，取第一个非空）。
    与系统设置的 verify-cookie 是两套判定：那边验下载页，这边验真接口。"""
    return SyncService(db).verify_cookie(payload.cookie)
