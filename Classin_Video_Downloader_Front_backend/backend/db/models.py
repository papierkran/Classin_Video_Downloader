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
    # start_time 的 epoch 秒（本地时区，来源：控制台 startTime 或 CSV 导入解析）。
    # Why 冗余一列：start_time 文本月日不补零，"2026/9/10" 与 "2026/9/2"
    # 的字符串比较结果与时间先后不一致，无法在 SQL 里做区间过滤；
    # 存 epoch 让"开课时间范围"筛选能直接在数据库侧走索引区间。
    start_ts: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    creation_time: Mapped[str] = mapped_column(String(64), default="")
    # 课堂同步（sync_service）标记：True=由同步新增且尚未被任何任务纳入。
    # Why 单独一列而非比对 created_at：同步是幂等 upsert，"新增"只在
    # 当轮可知；落库标记让自动建任务可以在任意时刻挑出"还没下过的新课"，
    # 任务创建时统一清标（已纳入 = 不再自动下单），无需回查同步历史。
    sync_pending: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # pending/downloading/success/failed —— 与引擎 course_status_callback 对齐
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # 仪表盘按状态 GROUP BY → 索引
    message: Mapped[str] = mapped_column(String(255), default="")  # 最近一次结果说明（"完成：4/4"）
    # ---- 控制台「课堂管理」页同步（course_service）拉取的网站元数据 ----
    # 这些字段只由同步写、展示用；与 CSV 导入的字段互不覆盖（同步按 lesson_id upsert）。
    unit: Mapped[str] = mapped_column(String(255), default="")          # 单元主题
    end_time: Mapped[str] = mapped_column(String(64), default="")       # 结束时间（原文）
    duration: Mapped[int] = mapped_column(Integer, default=0)           # 课堂时长（秒）
    teacher_name: Mapped[str] = mapped_column(String(128), default="")  # 授课教师
    class_status: Mapped[int] = mapped_column(Integer, default=-1)      # 网站课堂状态枚举
    record_state: Mapped[int] = mapped_column(Integer, default=-1)      # 云端录课（0无/1有）
    live_state: Mapped[int] = mapped_column(Integer, default=-1)        # 直播/回放状态
    website_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # 上次从网站同步该行的时间
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Course(Base):
    """班级/课程（来自教学管理「班级管理」页接口 /course/web/list）。

    Why 单独一张表而不是复用 lessons：控制台把"班级/课程"（courseId，
    即本系统的 class_id）与"课堂"（classId，即 lesson_id）分成两个页面、
    两套接口、两种粒度；本系统也按页面拆成 courses / lessons 两张表，
    避免把"班级级汇总"和"课堂级明细"塞进同一行靠字段区分。
    """

    __tablename__ = "courses"
    __table_args__ = (UniqueConstraint("course_id", name="uq_courses_course_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    course_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    teacher_name: Mapped[str] = mapped_column(String(128), default="")   # 主讲教师
    creator_name: Mapped[str] = mapped_column(String(128), default="")   # 创建人
    class_total: Mapped[int] = mapped_column(Integer, default=0)         # 课堂总数（totalClassNum）
    complete_class_num: Mapped[int] = mapped_column(Integer, default=0)  # 已完成课堂数
    student_num: Mapped[int] = mapped_column(Integer, default=0)
    course_state: Mapped[int] = mapped_column(Integer, default=-1)       # 网站班级状态枚举
    first_class_begin_time: Mapped[str] = mapped_column(String(64), default="")  # 开课时间（首课）
    end_time: Mapped[str] = mapped_column(String(64), default="")        # 结束时间
    add_time: Mapped[str] = mapped_column(String(64), default="")        # 创建时间
    cat_name: Mapped[str] = mapped_column(String(128), default="")       # 分类（catInfo 尽力取值）
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)  # 上次同步时间


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
    # ---- 课堂同步（sync_service，按钮式触发，无调度字段）----
    # sync_cookie 空时回退到上面的 cookie：同一 SSO 体系下通常一份通用，
    # 但允许分开配置（同步只读、cookie 用于下载），换下载号时不误伤同步。
    sync_cookie: Mapped[str] = mapped_column(Text, default="")
    sync_days: Mapped[int] = mapped_column(Integer, default=7)  # 每次回看的天数窗口（课堂列表 API 的 timeRange）
    # 同步结果回显（失败也要落库：用户判断"要不要重试"的唯一线索）
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sync_status: Mapped[str] = mapped_column(String(16), default="")  # ""/success/failed
    last_sync_message: Mapped[str] = mapped_column(Text, default="")
    # 控制台账号密码：备用（控制台登录含图形/滑块验证码，当前不做自动登录），
    # 目标场景是"Cookie 失效时提醒用户 + 手工重新登录"，账号密码先存库备用
    classin_account: Mapped[str] = mapped_column(Text, default="")
    classin_password: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
