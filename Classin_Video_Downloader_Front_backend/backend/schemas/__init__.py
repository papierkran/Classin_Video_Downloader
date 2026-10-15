"""Pydantic v2 请求/响应模型（格式校验层）。

职责边界（Why 校验要分两层）：
- 这里只管"格式"：类型、长度、枚举、正则 → 由 FastAPI 自动 422。
- 业务规则（Cookie 是否为空、任务状态能否暂停）放 service 层抛 BizError。
格式校验下沉到框架、业务校验上收到领域，控制器才能保持薄。
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------- 通用 ----------


class PageOut(BaseModel):
    """分页信封：前端表格组件统一消费。

    Why 固定信封而非各端点自定义：表格组件（el-table + 分页器）
    一次开发到处复用，前提是每页响应形状完全一致；让个别端点返回
    {items, total} 另一种形状，前端就得写特判——特判多了组件就废了。
    """

    total: int
    page: int
    page_size: int
    items: list = Field(default_factory=list)


class MsgOut(BaseModel):
    """最简成功响应。Why 有 message 字段：前端 toast 直接展示它，
    而非前端硬编码"操作成功"（后端将来能下发更具体的文案）。"""
    message: str = "ok"


# ---------- 认证/用户 ----------


class LoginIn(BaseModel):
    """登录入参。Why min_length=1 而非 2：空账号的错误提示
    （"请输入账号"）属于前端交互，后端只拦真正非法的输入。"""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class UserOut(BaseModel):
    """用户出参。故意不含 password_hash——出参模型与实体分离是
    防泄露的最后防线：即使有人给模型加字段，也必须显式在此声明才出得去。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    role: str
    created_at: datetime
    last_login_at: datetime | None = None


class TokenOut(BaseModel):
    """登录响应：token + 用户信息打包返回，前端一次请求即可完成登录态初始化。"""
    token: str
    user: UserOut


class UserCreateIn(BaseModel):
    """建用户入参。username 用白名单正则（而非黑名单）：白名单天然
    排除空格/中文/emoji 等登录框里难处理的字符；密码 ≥6 是为了
    防止"给同事开个 1 位密码账号"这种协作场景下的弱口令。"""

    username: str = Field(pattern=r"^[a-zA-Z0-9_\-]{2,64}$")
    password: str = Field(min_length=6, max_length=128)


class PasswordChangeIn(BaseModel):
    """改密码入参。new_password 单独带 min_length=6：与 UserCreateIn
    保持同一强度下限——允许把密码改成 1 位等于没做密码策略
    （策略只在建号时执行一次，很快会被绕空）。"""
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=6, max_length=128)


# ---------- 课堂 ----------


class LessonIn(BaseModel):
    """创建/更新课堂。lesson_id 是平台侧主键，必须存在且格式像数字 ID。

    Why 用 pattern 卡格式：lesson_id 会拼进引擎查询与 CSV 匹配，
    放进空格/中文/引号这类值到时只会以"任务失败"的形式暴露，
    在入口拦下错误信息可读十倍（"参数校验失败：lesson_id"）。
    """

    class_id: str = Field(default="", max_length=32)
    lesson_id: str = Field(min_length=1, max_length=32, pattern=r"^[0-9a-fA-F\-]+$")
    lesson_name: str = Field(default="", max_length=255)
    class_name: str = Field(default="", max_length=255)
    start_time: str = Field(default="", max_length=64)
    creation_time: str = Field(default="", max_length=64)


class LessonUpdateIn(BaseModel):
    """更新课堂：lesson_id 不可改（改了等于换一条记录，破坏引用）。

    全字段可 None = 部分更新（PATCH 语义）。Why 不用 PUT 全量替换：
    前端编辑框往往只回传用户改动的字段，全量语义会把未回传字段
    清成默认值（名称被清空的经典 bug）。
    """

    lesson_name: str | None = Field(default=None, max_length=255)
    class_name: str | None = Field(default=None, max_length=255)
    start_time: str | None = Field(default=None, max_length=64)


class LessonOut(BaseModel):
    """课堂出参。from_attributes 让它直接吃 ORM 对象，service 不用
    手工搬运字段——字段增删只改这一处（但注意：它同时是 API 契约，
    删字段是 breaking change）。"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    class_id: str
    lesson_id: str
    lesson_name: str
    class_name: str
    start_time: str
    creation_time: str
    status: str
    message: str
    updated_at: datetime


class LessonBatchDeleteIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=1000)


class ImportResultOut(BaseModel):
    """导入结果：created/updated 分开展示，用户能确认"新增了哪批"。"""

    total: int
    created: int
    updated: int
    skipped: int
    message: str = "ok"


# ---------- 任务 ----------


class TaskCreateIn(BaseModel):
    """创建任务入参。

    lesson_ids=None 语义是"全部课堂"（而非"不选"）：前端"全部"按钮
    直接发 null，省去先拉全量 id 再回传的往返；空列表被 service 判为
    非法（没有课堂的任务没有意义）。
    """

    name: str = Field(min_length=1, max_length=128)
    # None/空 = 全部课堂；否则按 id 列表创建快照
    lesson_ids: list[int] | None = Field(default=None, max_length=5000)
    save_dir: str | None = Field(default=None, max_length=512)


class TaskItemOut(BaseModel):
    """任务明细出参。video_total/video_done 支撑"4/4 视频"列，
    percent 支撑行内进度条——两者都给前端，避免它自己做除法。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    lesson_id: str
    class_id: str
    lesson_name: str
    class_name: str
    start_time: str
    status: str
    message: str
    video_total: int
    video_done: int
    percent: int


class TaskOut(BaseModel):
    """任务出参（列表与启停响应共用）。

    为什么启停端点也返回它：前端点"启动"后可直接用响应覆盖行数据，
    省一次 GET；也让"乐观更新"有权威数据可回滚。
    """

    model_config = ConfigDict(from_attributes=True)  # 允许直接从 ORM 实例校验

    id: int
    name: str
    status: str
    save_dir: str
    lesson_count: int
    success_count: int
    fail_count: int
    current_lesson: str
    speed_mbps: float
    percent: int
    error: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class TaskDetailOut(TaskOut):
    """详情 = 列表字段 + items。继承而非平铺：两处字段一旦分叉，
    前端要写两套类型，改一处忘另一处是迟早的事。"""
    items: list[TaskItemOut] = Field(default_factory=list)


class TaskLogOut(BaseModel):
    """日志行。level 让前端着色（ERROR 红/INFO 灰），不用从文本猜级别。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    level: str
    message: str
    created_at: datetime


class TaskLogsPageOut(BaseModel):
    """日志页（增量）。last_id 是给前端的游标：下次轮询 after_id=last_id。
    空页回传原 after_id 而非 0——回 0 会让前端把游标重置回起点。"""
    items: list[TaskLogOut] = Field(default_factory=list)
    last_id: int = 0


class VideoOut(BaseModel):
    """视频片段出参（视频库表格行）。lesson/class 名称来自 JOIN 而非
    视频表本身：视频行只存 lesson_id，名称空串表示课堂已删（前端显示占位）。"""

    id: int
    lesson_id: str
    lesson_name: str = ""
    class_name: str = ""
    title: str
    record_date: str
    record_method: str
    status: str
    percent: int
    size_bytes: int
    error: str
    updated_at: datetime


class MediaTicketOut(BaseModel):
    """播放票据出参。url 是不含令牌的媒体地址（令牌在 HttpOnly Cookie 里），
    expires_in 让前端能提示"链接有效期"，谁都能看但看不了多久的语义。"""

    url: str
    expires_in: int


# ---------- 配置 ----------


class SettingsOut(BaseModel):
    """GET 响应：Cookie 只回脱敏值 + has_cookie 布尔，明文永不返回。"""

    cookie_masked: str = ""
    has_cookie: bool = False
    save_dir: str = ""
    driver_path: str = ""
    max_workers: int = 4
    speed_limit: int = 0
    skip_existing: bool = True
    headless: bool = False


class SettingsUpdateIn(BaseModel):
    """PUT 请求：全部可选 = 部分更新；cookie 传空串表示"不修改"。

    数值字段的上下限都是业务真实边界（Why 写死在 schema）：
    - max_workers ≤8：引擎自身也 clamp 到 8，超过只是浪费线程；
    - speed_limit ≤100000 KB/s（≈100MB/s）：再大没有意义，
      但能拦住误填 0 之外的量纲错误（如把 MB/s 填成 1000000）。
    """

    cookie: str | None = Field(default=None, max_length=8192)
    save_dir: str | None = Field(default=None, max_length=512)
    driver_path: str | None = Field(default=None, max_length=512)
    max_workers: int | None = Field(default=None, ge=1, le=8)
    speed_limit: int | None = Field(default=None, ge=0, le=100_000)
    skip_existing: bool | None = None
    headless: bool | None = None

    @field_validator("save_dir")
    @classmethod
    def _reject_unsafe_path(cls, v: str | None) -> str | None:
        # Why 拒绝控制字符：save_dir 会参与 os.makedirs，换行/空字符属异常输入
        if v is not None and any(ch in v for ch in ("\n", "\r", "\x00")):
            raise ValueError("路径包含非法字符")
        return v


class CookieVerifyIn(BaseModel):
    """验证入参。cookie=None 表示"验证已保存的那个"（复用库里配置）；
    传值则验证"未保存的草稿"——用户粘贴后先验证再保存的操作顺序。"""
    cookie: str | None = Field(default=None, max_length=8192)


class CookieVerifyOut(BaseModel):
    """验证出参。valid + message 分离：前端按 valid 选图标/颜色，
    message 展示原文（引擎返回的"Cookie已过期"等具体原因）。"""
    valid: bool
    message: str


# ---------- 课堂同步 ----------


class SyncLessonOut(BaseModel):
    """待下载新课列表行（同步新增、未纳入过任何任务的课堂）。

    id 是 lessons 表主键，前端勾选后直接作为建任务的 lesson_ids；
    started 由后端判定开课时间——未开课暂无回放，前端默认不勾选
    并打"未开课"标签，是否下载仍由用户决定。
    """

    id: int
    lesson_id: str
    lesson_name: str = ""
    class_name: str = ""
    start_time: str = ""
    started: bool = True


class SyncConfigOut(BaseModel):
    """同步配置出参（按钮式同步：只有凭据与回看天数两项）。
    cookie 只回脱敏值；using_download_cookie 告诉用户"同步实际在用哪份
    凭据"，避免"明明没配同步 Cookie 却能同步"的疑惑。"""

    cookie_masked: str = ""
    has_cookie: bool = False
    using_download_cookie: bool = False
    days: int = 7


class SyncConfigUpdateIn(BaseModel):
    """同步配置部分更新。cookie 传空串=不修改（与系统设置页同一约定：
    表单回车带上的空值不能误清凭据）。
    days ≤365 防误填把时间窗推到几年（拉回一堆历史课）。"""

    cookie: str | None = Field(default=None, max_length=8192)
    days: int | None = Field(default=None, ge=1, le=365)


class SyncRunOut(BaseModel):
    """单次同步的结果回执（按钮触发）。同步本身不建任务：
    随后由用户在"待下载新课"列表勾选，走常规任务流程。"""

    fetched: int = 0
    created: int = 0
    updated: int = 0
    message: str = ""


class SyncStatusOut(BaseModel):
    """同步状态卡数据：凭据来源 + 上次结果 + 待处理新课数（无调度概念）。"""

    cookie_source: Literal["sync", "download", "none"] = "none"
    last_sync_at: datetime | None = None
    last_sync_status: str = ""
    last_sync_message: str = ""
    pending_count: int = 0


# ---------- 仪表盘/引擎 ----------


class StatsOut(BaseModel):
    """首页统计信封。recent_tasks 内嵌 5 条任务（复用 TaskOut）：
    比让前端"先拿统计再拉任务列表"少一次请求与一次状态同步。"""
    lessons_total: int
    lessons_pending: int
    lessons_success: int
    lessons_failed: int
    tasks_total: int
    tasks_running: int
    videos_total: int = 0
    videos_downloaded: int = 0
    videos_failed: int = 0
    videos_downloading: int = 0
    recent_tasks: list[TaskOut] = Field(default_factory=list)


class EngineStatusOut(BaseModel):
    """环境自检出参。browser_path 给"检测不到浏览器"时的排障线索，
    driver_path 让用户确认用的是哪个驱动版本。"""
    browser: str = ""
    browser_path: str = ""
    driver_path: str = ""
    driver_ready: bool = False


# Why 保留这个别名：前端/测试若要断言状态取值，有一处权威来源，
# 不用去翻 models 注释（字符串状态散落是状态机实现的常见腐化路径）
TaskStatusLit = Literal["created", "running", "paused", "stopping", "success", "failed", "cancelled"]
