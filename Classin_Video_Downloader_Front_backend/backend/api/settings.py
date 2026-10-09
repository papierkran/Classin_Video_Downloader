"""系统设置路由（配置读写 + Cookie 验证 + 引擎环境自检）。

全部端点要求 admin 角色（require_admin）：Cookie 是登录态凭据，
save_dir/driver_path 是本机路径，普通成员可看任务但不应能改这些。
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..core.exceptions import ValidationFailure
from ..db.models import User
from ..schemas import CookieVerifyIn, CookieVerifyOut, EngineStatusOut, SettingsOut, SettingsUpdateIn
from ..services.dashboard_service import engine_status
from ..services.setting_service import SettingService
from .deps import db_session, require_admin

router = APIRouter(prefix="/api/settings", tags=["设置"])


@router.get("/directories")
def list_directories(path: str | None = Query(default=None, max_length=4096), _: User = Depends(require_admin)):
    """返回服务器目录树的一层，仅管理员可用；不返回文件列表。"""
    if os.name == "nt":
        if not path:
            drives = [f"{chr(letter)}:\\" for letter in range(ord("A"), ord("Z") + 1)
                      if os.path.exists(f"{chr(letter)}:\\")]
            return {"current_path": "", "parent_path": None,
                    "directories": [{"name": drive, "path": drive} for drive in drives]}
    elif not path:
        path = os.path.abspath(os.sep)

    current = Path(os.path.abspath(os.path.expanduser(path or os.curdir)))
    if not current.exists() or not current.is_dir():
        raise ValidationFailure("目录不存在或不可访问")

    directories = []
    try:
        with os.scandir(current) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        directories.append({"name": entry.name, "path": os.path.abspath(entry.path)})
                except OSError:
                    continue
    except PermissionError:
        directories = []
    directories.sort(key=lambda item: item["name"].casefold())
    parent = str(current.parent) if current.parent != current else None
    return {"current_path": str(current), "parent_path": parent, "directories": directories}


@router.get("", response_model=SettingsOut)
def get_settings(_: User = Depends(require_admin), db: Session = Depends(db_session)):
    """读配置。Cookie 只回脱敏值（cookie_masked），明文永不进响应体——
    响应可能进浏览器缓存/代理日志，明文回显等于把凭据二次扩散。"""
    return SettingService(db).get()


@router.put("", response_model=SettingsOut)
def update_settings(payload: SettingsUpdateIn, _: User = Depends(require_admin), db: Session = Depends(db_session)):
    """部分更新（字段全可选，缺省=不改）。cookie 只有传非空值才覆盖。"""
    return SettingService(db).update(payload)


@router.post("/verify-cookie", response_model=CookieVerifyOut)
def verify_cookie(payload: CookieVerifyIn, _: User = Depends(require_admin), db: Session = Depends(db_session)):
    """在线探测 Cookie（会访问 console.eeo.cn，同步实现由 FastAPI 放线程池）。

    Why 单独一个端点：验证是网络 I/O，可能等 10s+，和 PUT 配置合并会让
    "保存"按钮莫名卡住；拆开后保存永远是本地快操作。
    """
    return SettingService(db).verify_cookie(payload.cookie)


@router.get("/engine", response_model=EngineStatusOut)
def get_engine_status(_: User = Depends(require_admin)):
    """浏览器/驱动环境自检：设置页"检测环境"按钮调用，纯本地探测（无网络）。"""
    return engine_status()
