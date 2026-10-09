"""SQLAlchemy engine/session 管理 + 首次启动初始化（建表、种子数据）。

分层 Why：
- engine 是"连接"级单例（进程一份）；session 是"请求"级（每请求一份）。
  混用两者是 SQLAlchemy 最常见的泄漏源——engine 持连接池，session 持事务，
  事务不关就会一直占着写锁（SQLite 下表现是"整个应用偶发卡住"）。
- init_db 幂等：开发时反复重启、部署时容器重建都安全，不需要迁移工具
  （单机小库 schema 演进靠"删库重建"或手动 ALTER 足够，引 Alembic 属过度设计）。
"""
from __future__ import annotations

import logging
from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ..core.config import settings
from ..core.security import hash_password

logger = logging.getLogger("app.db")

# check_same_thread=False 的原因见 config.database_url 注释；
# pool_pre_ping 防止 SQLite 文件被外部工具改动后连接失效。
engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False, "timeout": 30},
    pool_pre_ping=True,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _record):
    """启用 WAL + busy_timeout + 外键。

    Why 每条都必要：
    - WAL：后台下载线程高频写日志/进度，API 线程同时读，默认 journal 模式
      下极易 "database is locked"；WAL 允许读写并发。
    - busy_timeout=30s：写锁被占时不立刻报错而是排队，扛住任务收尾时的
      突发写（状态+计数+日志一起提交）。
    - foreign_keys=ON：SQLite 默认不 enforced 外键，不开的话
      删任务不会级联删 items/logs，会留下孤儿数据。
    """
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


# expire_on_commit=False：commit 后对象属性保持可读——请求线程返回
# Pydantic 校验发生在 commit 之后，若被 expire 会触发隐式 IO 再查一次
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：每请求一个 session，请求结束即关闭（防连接泄漏）。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """建表 + 种子数据（幂等，重复启动安全）。

    顺序不可换：先建表（seed 依赖表存在），再种管理员（登录依赖它），
    最后种配置（任务启动依赖 save_dir 等字段有值）。
    """
    from . import models  # noqa: F401  确保模型注册到 metadata

    models.Base.metadata.create_all(bind=engine)
    _seed_admin()
    _seed_default_settings()


def _seed_admin() -> None:
    """首启创建默认管理员。Why 查重再插：init_db 每次启动都会跑，
    无查重的话第二次启动就撞 users.username 唯一约束炸掉整个启动流程。"""
    from . import models

    with SessionLocal() as db:
        if db.query(models.User).filter(models.User.username == settings.admin_username).first():
            return
        db.add(
            models.User(
                username=settings.admin_username,
                password_hash=hash_password(settings.admin_password),
                role="admin",
            )
        )
        db.commit()
        logger.info("已创建默认管理员账号 '%s'（请尽快修改默认密码）", settings.admin_username)


def _seed_default_settings() -> None:
    """初始化单行配置。save_dir 指到 data/videos：项目目录内、免管理员权限，
    用户开箱即用（不给默认值的话第一次任务启动会拿到空路径）。"""
    from . import models

    with SessionLocal() as db:
        if db.get(models.AppSetting, 1):
            return
        db.add(models.AppSetting(id=1, save_dir=settings.default_save_dir))
        db.commit()
        logger.info("已初始化默认配置 save_dir=%s", settings.default_save_dir)
