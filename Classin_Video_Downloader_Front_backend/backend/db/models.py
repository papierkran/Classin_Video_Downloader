"""数据模型（ER 图见 docs/技术方案.md）。

实体关系概览：
  User 1──< DownloadTask 1──< TaskItem >──1 Lesson
  DownloadTask 1──< DownloadLog
  AppSetting（单行配置）

设计决策（每条都是权衡后的取舍，改表前先读）：
- 任务创建时把"选中的课堂"快照成 TaskItem：下载过程中用户增删课堂
  不影响进行中的任务（可追溯性，避免任务列表被外部修改污染）。
- 课堂状态冗余在 Lesson.status：仪表盘统计不必 JOIN 任务表（读多写少，
  冗余写由唯一的 course_status_callback 维护，一致性可控）。
- 字符串时间（start_time）而非 DateTime：ClassIn 导出的时间格式
  "2026/4/5 18:00" 不是标准 ISO，强行解析会丢原文；展示排序用字符串
  前缀（年月日）天然可用，无需为排序牺牲数据保真。
- 状态用 String(16) 而非 Enum 类型：SQLite 的 Enum 存储是 CHECK 约束，
  加新状态要改表；String + 代码层状态机（task_service）更灵活，
  非法值由状态机拒绝而非数据库（错误信息更可读）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """声明式基类。所有模型继承它 → create_all 一次建全表。"""


class User(Base):
    """登录用户。2-3 人公网访问 → 多用户 + 角色（目前仅 admin 可管配置）。

    Why 不存明文密码也不存"密码提示"：唯一的凭据是 password_hash，
    登录失败文案统一"用户名或密码错误"（不区分哪个错，防枚举）。
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    # role 预留扩展：当前全员 admin，将来加 "viewer"（只读）无需迁移数据
    role: Mapped[str] = mapped_column(String(16), default="admin", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Lesson(Base):
    """一节课（来自 ClassIn 导出的 CSV/XLSX 或手工录入）。

    class_id=班级ID, lesson_id=课堂ID（平台侧唯一键，用于 upsert 去重）。
    lesson_id 唯一约束是导入幂等的根基：没有它，"重复导入同一批"
    会产生重复行，任务范围随之翻倍。
    """

    __tablename__ = "lessons"
    __table_args__ = (UniqueConstraint("lesson_id", name="uq_lessons_lesson_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    class_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    lesson_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    lesson_name: Mapped[str] = mapped_column(String(255), default="")
    class_name: Mapped[str] = mapped_column(String(255), default="", index=True)  # 搜索/分组常用 → 索引
    start_time: Mapped[str] = mapped_column(String(64), default="")  # 原文保存，见模块 docstring
    creation_time: Mapped[str] = mapped_column(String(64), default="")
    # pending/downloading/success/failed —— 与引擎 course_status_callback 对齐
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # 仪表盘按状态 GROUP BY → 索引
    message: Mapped[str] = mapped_column(String(255), default="")  # 最近一次结果说明（"完成：4/4"）
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DownloadTask(Base):
    """一次下载任务（状态机：created→running→paused→stopping→success/failed/cancelled）。

    状态迁移只允许在 TaskService 中进行（集中式状态机，防止散落的
    status 赋值产生非法状态）。
    """

    __tablename__ = "download_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="created", index=True)
    save_dir: Mapped[str] = mapped_column(String(512), nullable=False)
    lesson_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    # 当前正在处理的课堂名 + 实时速度（MB/s），供前端进度条展示
    current_lesson: Mapped[str] = mapped_column(String(255), default="")
    speed_mbps: Mapped[float] = mapped_column(default=0.0)
    percent: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    items: Mapped[list["TaskItem"]] = relationship(back_populates="task", cascade="all, delete-orphan")
    logs: Mapped[list["DownloadLog"]] = relationship(back_populates="task", cascade="all, delete-orphan")


class TaskItem(Base):
    """任务中的单个课堂（任务↔课堂快照，含实时进度）。"""

    __tablename__ = "task_items"
    __table_args__ = (UniqueConstraint("task_id", "lesson_id", name="uq_task_lesson"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("download_tasks.id", ondelete="CASCADE"), index=True)
    lesson_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # 冗余课堂快照：即使原 Lesson 被删除，历史任务仍可完整回显
    class_id: Mapped[str] = mapped_column(String(32), default="")
    lesson_name: Mapped[str] = mapped_column(String(255), default="")
    class_name: Mapped[str] = mapped_column(String(255), default="")
    start_time: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # pending/downloading/success/failed/skipped（与引擎回调一致）
    message: Mapped[str] = mapped_column(String(255), default="")
    video_total: Mapped[int] = mapped_column(Integer, default=0)
    video_done: Mapped[int] = mapped_column(Integer, default=0)
    percent: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    task: Mapped[DownloadTask] = relationship(back_populates="items")


class DownloadLog(Base):
    """任务运行日志（引擎 log_callback 落库；增量拉取 after_id 避免全量传输）。"""

    __tablename__ = "download_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("download_tasks.id", ondelete="CASCADE"), index=True)
    level: Mapped[str] = mapped_column(String(8), default="INFO")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    task: Mapped[DownloadTask] = relationship(back_populates="logs")


class VideoAsset(Base):
    """单个课堂视频片段的持久化下载台账。"""

    __tablename__ = "video_assets"
    __table_args__ = (UniqueConstraint("lesson_id", "source_key", name="uq_video_lesson_source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lesson_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("download_tasks.id", ondelete="SET NULL"), nullable=True, index=True)
    source_key: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), default="")
    record_date: Mapped[str] = mapped_column(String(64), default="")
    record_method: Mapped[str] = mapped_column(String(64), default="")
    file_path: Mapped[str] = mapped_column(String(1024), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    percent: Mapped[int] = mapped_column(Integer, default=0)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(String(1000), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AppSetting(Base):
    """单行应用配置（id 恒为 1）。

    Why 单行而不是 key-value：配置字段固定且都有类型校验，
    key-value 会把类型/校验逻辑散到读写两侧，反而更难维护。
    Cookie 是敏感凭据：API 层只回显脱敏值，明文只存库。
    """

    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cookie: Mapped[str] = mapped_column(Text, default="")
    save_dir: Mapped[str] = mapped_column(String(512), default="")
    driver_path: Mapped[str] = mapped_column(String(512), default="")
    max_workers: Mapped[int] = mapped_column(Integer, default=4)
    speed_limit: Mapped[int] = mapped_column(Integer, default=0)  # KB/s，0=不限速
    skip_existing: Mapped[bool] = mapped_column(Boolean, default=True)
    headless: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
