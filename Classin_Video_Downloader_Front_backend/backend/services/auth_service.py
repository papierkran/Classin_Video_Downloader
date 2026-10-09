"""认证服务：登录（限速）、建用户、改密。

职责边界（SRP）：只处理"身份"，不碰业务数据；路由层负责收集
用户名密码，本层负责校验与签发，测试时可直接 new AuthService 而不起 HTTP。

登录失败 5 次会把限速 key（用户名+客户端 IP）锁 5 分钟——
公网部署下这是最廉价有效的防爆破手段（不依赖 Redis，单进程足够）。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from ..core.exceptions import AuthError, ConflictError, NotFoundError, ValidationFailure
from ..core.security import create_token, hash_password, login_rate_limiter, verify_password
from ..db.models import User
from ..repositories import UserRepo

logger = logging.getLogger("app.auth")


class AuthService:
    """认证领域服务。client_ip 由路由从请求头注入（依赖链：
    Request → get_current_user/deps → AuthService），这样本类不依赖
    FastAPI 类型，单元测试直接 new AuthService(db, "1.2.3.4") 即可。"""

    def __init__(self, db: Session, client_ip: str = ""):
        self.db = db
        self.repo = UserRepo(db)
        self.client_ip = client_ip

    def login(self, username: str, password: str) -> tuple[str, User]:
        """返回 (token, user)。

        顺序刻意是：先限速检查 → 再查库 → 最后重置限速。
        限速在最前意味着即使数据库慢/宕，爆破也被挡住（fail-closed）。
        """
        rate_key = f"{username}@{self.client_ip}"
        login_rate_limiter.check(rate_key)

        user = self.repo.get_by_username(username)
        # Why 即使用户不存在也做一次假校验：防用户名枚举（时间侧信道）
        stored = user.password_hash if user else hash_password("dummy-password-not-match")
        if not user or not verify_password(password, stored):
            login_rate_limiter.record_failure(rate_key)
            logger.warning("登录失败: %s from %s", username, self.client_ip)
            raise AuthError("用户名或密码错误")

        login_rate_limiter.reset(rate_key)
        user.last_login_at = datetime.utcnow()
        self.db.commit()
        logger.info("登录成功: %s from %s", username, self.client_ip)
        return create_token(user.id, user.username, user.role), user

    def create_user(self, username: str, password: str) -> User:
        """创建用户（重名 409）。角色固定 admin：当前场景全员平权，
        预留 role 字段是为了将来区分"只读成员"而不用改表结构。"""
        if self.repo.get_by_username(username):
            raise ConflictError(f"用户名 '{username}' 已存在")
        user = self.repo.add(User(username=username, password_hash=hash_password(password), role="admin"))
        self.repo.commit()
        return user

    def change_password(self, user: User, old_password: str, new_password: str) -> None:
        """改密三重校验：旧密码对、新旧不同、（长度由 schema 兜底）。

        Why 禁止新旧相同：用户"改了个寂寞"后会以为密码已更换，
        安全预期与实际不符。
        """
        if not verify_password(old_password, user.password_hash):
            raise ValidationFailure("原密码不正确")
        if old_password == new_password:
            raise ValidationFailure("新密码不能与原密码相同")
        user.password_hash = hash_password(new_password)
        self.db.commit()

    def get_user(self, user_id: int) -> User:
        """按 id 查用户（供测试/将来"用户管理"页用）。"""
        user = self.repo.get(user_id)
        if not user:
            raise NotFoundError("用户不存在")
        return user
