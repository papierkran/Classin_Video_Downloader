"""认证与用户管理路由。

安全设计（公网部署视角）：
- 登录失败限速在 service 层（用户名+IP 维度），路由不重复实现。
- 所有端点除 login 外都依赖 current_user/require_admin，
  忘记加依赖 = 立即 401，"默认拒绝"比"默认放行"安全。
- 登录成功才签发 JWT；token 里只带身份，不带业务数据，
  封禁/改密后虽有最长 TTL 的残留窗口，但业务读取仍会实时校验用户存在。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..db.models import User
from ..schemas import LoginIn, MsgOut, PasswordChangeIn, TokenOut, UserCreateIn, UserOut
from ..services.auth_service import AuthService
from .deps import current_user, db_session, require_admin

router = APIRouter(prefix="/api/auth", tags=["认证"])


@router.post("/login", response_model=TokenOut)
def login(payload: LoginIn, request: Request, db: Session = Depends(db_session)):
    """登录：失败 5 次/5 分钟触发限速（见 security.LoginRateLimiter）。

    Why 用 IP+用户名做 key：单 IP 撞多个账号、单账号被多 IP 撆，
    两个维度合起来才覆盖常见爆破模式。
    """
    svc = AuthService(db, client_ip=request.client.host if request.client else "")
    token, user = svc.login(payload.username, payload.password)
    return TokenOut(token=token, user=UserOut.model_validate(user))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)):
    """返回当前登录用户：前端启动时用它校验 token 是否仍有效。"""
    return UserOut.model_validate(user)


@router.put("/password", response_model=MsgOut)
def change_password(payload: PasswordChangeIn, user: User = Depends(current_user), db: Session = Depends(db_session)):
    """修改自己的密码（需验旧密码；改密不强制下线，靠 TTL 自然过期）。"""
    AuthService(db).change_password(user, payload.old_password, payload.new_password)
    return MsgOut(message="密码已修改")


@router.post("/users", response_model=UserOut, status_code=201)
def create_user(payload: UserCreateIn, _: User = Depends(require_admin), db: Session = Depends(db_session)):
    """创建新用户（仅 admin）：2-3 人协作场景给同事各开一个账号，
    而不是共享 admin（共享账号使限速与审计全部失效）。"""
    return UserOut.model_validate(AuthService(db).create_user(payload.username, payload.password))
