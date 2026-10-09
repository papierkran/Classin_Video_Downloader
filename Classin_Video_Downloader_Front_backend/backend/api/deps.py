"""API 依赖（FastAPI DI）：DB session、当前用户、权限、分页、客户端 IP。

Why 把它们集中在这里：依赖是路由的"横切关注点"，分散定义会导致
鉴权逻辑在多个文件间漂移；集中后只需审计这一个文件即可确认
"哪些路由需要什么权限"。
"""
from __future__ import annotations

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from ..core.exceptions import AuthError, ForbiddenError
from ..core.security import decode_token
from ..db.database import get_db
from ..db.models import User
from ..repositories import UserRepo


def db_session(db: Session = Depends(get_db)) -> Session:
    """注入 ORM session（session 生命周期=请求，见 db.database.get_db）。"""
    return db


def current_user(request: Request, db: Session = Depends(get_db), authorization: str | None = Header(default=None)) -> User:
    """Bearer JWT 认证（Why Header 而非 Cookie：SPA 无跨站场景，
    Header 不会被浏览器自动携带，天然免疫 CSRF——公网部署省一层防线）。

    每次请求都查库取用户而非只解 token：token 签发后用户可能被删/改角色，
    实时查库保证权限变更即时生效（代价是一次主键查询，可忽略）。
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthError()
    payload = decode_token(authorization.split(" ", 1)[1].strip())
    user = UserRepo(db).get(int(payload["sub"]))
    if not user:
        raise AuthError("账号已被删除")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    """管理操作（建用户/改配置）的权限闸门。

    默认拒绝：新路由只要不显式声明 require_admin 就只有普通登录权限，
    反过来"忘记加鉴权"的路由则会被 current_user 的 401 兜住。
    """
    if user.role != "admin":
        raise ForbiddenError("仅管理员可执行该操作")
    return user


def client_ip(request: Request) -> str:
    """限速 key 用的来源 IP（直连场景取 socket 地址即可；
    若将来挂 Nginx 反代，需改为读 X-Forwarded-For，已在文档注明）。"""
    return request.client.host if request.client else ""


def pagination(page: int = 1, page_size: int = 20) -> tuple[int, int]:
    """分页参数。page_size 上限 200：无上限的分页等于全表拉取，
    一个误传 page_size=999999 就能让大表查询拖垮服务。"""
    return max(1, page), min(max(1, page_size), 200)
