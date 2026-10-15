"""课堂同步服务：按钮式触发，从 ClassIn 控制台 API 拉取课堂列表 → 幂等入库 → 提供待下载新课列表。

交互模型（设计决策：为什么不做定时自动下载）：
- 同步由同步管理页的"立即同步"按钮触发，点击即同步一次；不做后台
  调度——拉回的新课是否值得下载由用户判断（免费课/试听课/已手工下过的
  都在列表里），机器替用户"全下"只会浪费带宽与磁盘。
- 同步后新增课堂带 sync_pending 标记，汇总在 GET /api/sync/pending 的
  "待下载新课"列表；用户在同步页勾选后建任务（复用 TaskService 全流程，
  含任务日志），创建任务即清标（TaskService.create 统一处理），
  不会在任何路径重复下单。

背景与边界（Why 是 requests 而不是 Selenium）：
- 控制台（console.eeo.cn）课堂管理页底下的列表接口是纯 JSON
  （POST https://dynamic.eeo.cn/classroom/web/class/list，从教学微前端
  的打包产物中逆向确认），同步只需 HTTP 直连：比驱动浏览器快两个数量级，
  也不会和下载引擎抢同一个 Selenium 实例。
- 鉴权复用 ClassIn 同一 SSO Cookie（.eeo.cn 域）：同步 Cookie 为空时
  自动回退系统设置里的下载 Cookie；两份分开配置的场景是"下载换号但
  同步仍看原机构"，两个面各自独立报错、互不牵连。
- 同步是幂等 upsert：lesson_id 唯一约束 + "空值不覆盖"语义与 CSV 导入
  完全一致（见 lesson_service.import_rows），重复同步不会产生重复行，
  也不会把人工修正过的名称/时间刷丢。

并发与失败处理：
- 同一进程任意时刻只允许一个同步在跑（_sync_lock 非阻塞获取）：
  双击/多标签重复点"立即同步"时后者直接得到"正在进行中"，
  秒级操作没有排队的意义。
- 失败写入 last_sync_status/message（状态页可见），异常统一转
  ValidationFailure 供 API 层回可读文案。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

import requests
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.exceptions import BizError, ConflictError, NotFoundError, ValidationFailure
from ..db.models import AppSetting, Lesson
from ..schemas import (
    CookieVerifyOut,
    SyncConfigOut,
    SyncConfigUpdateIn,
    SyncLessonOut,
    SyncRunOut,
    SyncStatusOut,
)
from .setting_service import mask_cookie

logger = logging.getLogger("app.sync")

# ---- 控制台接口常量（来源：教学微前端 nxsaas-teaching 打包产物 + 实测对齐）----
CONSOLE_API_BASE = "https://dynamic.eeo.cn"
AS_SUB_PATH = "/coreapi/school/asSubList"  # 会话探活：errno==1 有效，101=未登录/过期
CLASS_LIST_PATH = "/classroom/web/class/list"  # 课堂列表（控制台列表页同款接口）
# 实测结论（304/304 与 CSV 导入数据对齐）：classId=课堂ID、courseId=班级ID、
# name=课堂名、courseName=班级名、startTime/createdAt=秒级时间戳。
# searchStatus 参数不传：从打包产物里抄来的 [0,1,10] 实测会把结果全部过滤为空
# （那是列表页某种 UI 筛选枚举，不是"全部状态"），不传即返回时间窗内全部课堂。
PAGE_SIZE = 100
MAX_PAGES = 20  # 分页上限=2000 课：同步是"回看窗口"不是全量导出，异常量说明参数被改错
# 控制台前端同款请求头：Origin/Referer 表明来源、UA 用常规桌面浏览器，
# 都是零成本的兼容垫片——控制台后端对非常规请求未必拦截，但没必要去试探。
_BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

# ---- 进程级同步闸门 ----
# 同一时刻只允许一轮同步：按钮式触发下这是防"双击/多标签重复点击"，
# 非阻塞获取（撞上直接 409），不做排队
_sync_lock = threading.Lock()


# ---------- 控制台 HTTP 客户端 ----------


def _headers(cookie: str) -> dict:
    """组装控制台请求头。Origin/Referer 声明调用方来源（控制台后端若做
    同源校验，缺这两个字段的请求会被拒）；各部分都是零成本兼容垫片。"""
    return {
        "Content-Type": "application/json;charset=UTF-8",
        "Cookie": cookie,
        "Origin": "https://console.eeo.cn",
        "Referer": "https://console.eeo.cn/",
        "User-Agent": _BROWSER_UA,
    }


def _post_json(cookie: str, path: str, payload: dict) -> dict:
    """POST 控制台 JSON 接口。三类异常（连接失败/非 JSON/结构异常）
    统一转 ValidationFailure：错误文案面向用户（"检查网络/Cookie"），
    具体异常类名作为线索保留，堆栈留在服务端日志。"""
    try:
        resp = requests.post(CONSOLE_API_BASE + path, json=payload, headers=_headers(cookie), timeout=20)
    except requests.RequestException as e:
        # 超时/拒绝连接/DNS 失败都在这里：同步是后台行为，绝不能抛出
        # 原始异常把调用栈暴露到 API 响应里
        raise ValidationFailure(f"连接 ClassIn 控制台失败（{e.__class__.__name__}），请检查服务器网络后重试") from e
    try:
        data = resp.json()
    except ValueError as e:
        # 登录态失效时有些网关会重定向到 HTML 登录页而不是返回 401 JSON，
        # 这个分支就是为它准备的：提示用户重新获取 Cookie
        raise ValidationFailure(f"控制台返回非 JSON 响应（HTTP {resp.status_code}），Cookie 可能已失效") from e
    if not isinstance(data, dict):
        raise ValidationFailure("控制台响应格式异常")
    return data


def _error_info(data: dict) -> tuple[int, str]:
    """提取 {error_info:{errno,error}}。errno 可能是数字或字符串
    （控制台前端自己也是用 == 宽松比较），这里统一转 int；
    缺字段按 -1 处理——-1 不等于成功码 1，会被当作失败进入错误分支。

    注意：errno==1 时 error 字段是"程序正常执行"这类文案，是成功提示
    而非错误，调用方按 errno 判定，不要看文案。
    """
    info = data.get("error_info")
    if not isinstance(info, dict):
        return -1, ""
    try:
        errno = int(info.get("errno", -1))
    except (TypeError, ValueError):
        errno = -1
    return errno, str(info.get("error") or info.get("errmsg") or "")


def verify_console_cookie(cookie: str) -> CookieVerifyOut:
    """用 asSubList 探活（errno==1 有效，101=过期）。

    Why 不用引擎的 check_cookie_validity：它探测的是下载页的 SPA HTML，
    静态页面恒返回 200 → 永远"有效"，对接口层的登录失效零感知；
    同步场景必须用真实接口判定（本次改造的踩坑结论）。
    """
    cookie = (cookie or "").strip()
    if not cookie:
        return CookieVerifyOut(valid=False, message="尚未配置 Cookie")
    try:
        data = _post_json(cookie, AS_SUB_PATH, {})
    except ValidationFailure as e:
        return CookieVerifyOut(valid=False, message=e.message)
    errno, text = _error_info(data)
    if errno == 1:
        return CookieVerifyOut(valid=True, message=f"Cookie 有效{_school_name(data)}")
    if errno == 101:
        return CookieVerifyOut(valid=False, message="Cookie 已过期或未登录（请重新登录 ClassIn 控制台后再复制一份）")
    return CookieVerifyOut(valid=False, message=f"控制台校验失败（errno={errno}）{text[:120]}")


def _school_name(data: dict) -> str:
    """尽量从 asSubList 响应里取出学校/机构名做回显，取不到就空串——
    探测接口的响应结构可能演进，兜底展示"有效"本身已足够。"""
    body = data.get("data")
    candidates = body
    if isinstance(body, dict):
        candidates = body.get("list") or body.get("subs") or body.get("schools")
    if isinstance(candidates, list):
        for item in candidates:
            if not isinstance(item, dict):
                continue
            for key in ("subName", "schoolName", "orgName", "name"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    return f"（{value.strip()}）"
    return ""


def fetch_lessons(cookie: str, days: int) -> list[dict]:
    """拉取课堂列表并归一化。时间窗 = [now-days 天, now+1 天]：

    - endTime 给 +1 天余量，避免"今天刚建的课"因边界秒被漏掉；
    - 分页按 total 提前收敛，拉够即停（课堂列表通常一页装下）。
    """
    now = int(time.time())
    base_payload = {
        # 时间窗单位是秒级时间戳；min(1, days) 兜底：配置层已限制 ≥1 天，
        # 这里再兜一次防止绕过配置直接调函数的调用方传 0 把窗口清零。
        "timeRange": {"startTime": now - max(1, days) * 86400, "endTime": now + 86400},
    }
    rows: list[dict] = []
    total = 0
    for page in range(1, MAX_PAGES + 1):
        data = _post_json(cookie, CLASS_LIST_PATH, {**base_payload, "page": page, "pageSize": PAGE_SIZE})
        errno, text = _error_info(data)
        if errno != 1:
            # 101 单独翻译：这是用户最常遇到的失败（Cookie 过期），
            # 文案要直接告诉下一步动作（重新登录控制台复制 Cookie）
            if errno == 101:
                raise ValidationFailure("Cookie 已过期或未登录，请重新登录 ClassIn 控制台后更新 Cookie")
            raise ValidationFailure(f"拉取课堂列表失败（errno={errno}）{text[:120]}")
        body = data.get("data")
        items = (body.get("list") if isinstance(body, dict) else None) or []
        if not isinstance(items, list):
            items = []  # 防御：响应结构演进时退化为"本页空"而不是崩溃
        if isinstance(body, dict) and body.get("total") is not None:
            try:
                total = int(body["total"])
            except (TypeError, ValueError):
                pass  # total 缺失/非法就退回上一页的值，分页仍靠 len(items) 收敛
        rows.extend(row for row in (_normalize_lesson(item) for item in items if isinstance(item, dict)) if row)
        # 收敛条件二选一：本页不足一整页（最后一页），或已达 total；
        # 并给 MAX_PAGES 兜底(循环 else)：接口 total 撒谎时不会无限翻页
        if len(items) < PAGE_SIZE or (total and page * PAGE_SIZE >= total):
            break
    else:
        logger.warning("同步分页达到上限 %d 页仍未取完，可能有课堂未拉取", MAX_PAGES)
    return rows


def _pick(item: dict, *keys: str) -> str:
    """按序取第一个非空字段值（数字 ID 统一转字符串）。

    Why 多候选键：控制台接口跨版本字段名有漂移（name/className、
    classId/activityId 并存），多猜几个别名只是几行代码；"少数派丢行"
    的代价是用户的课静默消失，不可接受。
    """
    for key in keys:
        value = item.get(key)
        if value is None or value == "" or value == 0:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _normalize_lesson(item: dict) -> dict | None:
    """控制台条目 → Lesson 字段字典（与 CSV 导入同构）。缺课堂 ID 的行丢弃
    （没有 ID 就无法下载，与导入口径一致）；class/lesson 的名称各留一个兜底。"""
    # 有的版本把班级信息嵌在 item["course"] 子对象里，先归一出来再统一取
    course = item.get("course") if isinstance(item.get("course"), dict) else {}
    # classId 优先（已实测 304/304 对齐导入数据）；activityId 是下一个候选，
    # 字段漂移时尽量不丢行——丢行的表现是"同步显示成功但课没进来"，最难排查
    lesson_id = _pick(item, "classId", "activityId", "lessonId", "id")
    if not lesson_id:
        return None
    return {
        "class_id": _pick(item, "courseId") or _pick(course, "courseId", "id"),
        "lesson_id": lesson_id,
        "lesson_name": _pick(item, "name", "className", "lessonName", "title"),
        "class_name": _pick(item, "courseName") or _pick(course, "courseName", "name", "title"),
        "start_time": _format_unix(item.get("startTime")),
        "creation_time": _format_unix(item.get("createdAt") or item.get("createTime") or item.get("creationTime")),
    }


def _format_unix(value) -> str:
    """秒级时间戳 → 'YYYY/M/D HH:MM'，与 CSV 导出的时间格式同构：
    展示一致、字符串前缀天然可排序（模型层刻意存字符串，见 models 注释）。"""
    try:
        ts = int(float(value))
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    dt = datetime.fromtimestamp(ts)
    return f"{dt.year}/{dt.month}/{dt.day} {dt:%H:%M}"


def _lesson_started(start_time: str) -> bool:
    """课堂是否已开课（自动下载的唯一时间判据）。

    解析失败按"已开课"处理：格式意外时宁可多下一次（失败只影响单课、
    任务里可见），也不能把课永久搁置在等待状态（静默漏下载更糟）。
    strptime 对 "2026/9/7 21:46" 这类不补零的月日同样可用。
    """
    try:
        return datetime.strptime(start_time, "%Y/%m/%d %H:%M") <= datetime.now()
    except (TypeError, ValueError):
        return True


# ---------- 同步领域服务 ----------


class SyncService:
    """同步配置/状态/执行的领域服务。事务边界与 commit 时机全在本类，
    API 层只做参数搬运与响应序列化。"""

    def __init__(self, db: Session):
        self.db = db

    def _require(self) -> AppSetting:
        row = self.db.get(AppSetting, 1)
        if not row:
            raise NotFoundError("配置尚未初始化，请重启服务")
        return row

    # ---------- 配置 ----------

    def get_config(self) -> SyncConfigOut:
        """现在只有两项配置：凭据（可回退下载 Cookie）与回看天数。"""
        s = self._require()
        # using_download_cookie 是给用户看的"实际在用哪份凭据"：
        # 没配同步 Cookie 却能同步，不说明来源会产生"回退了还是没配？"的疑惑
        sync_cookie = (s.sync_cookie or "").strip()
        return SyncConfigOut(
            cookie_masked=mask_cookie(sync_cookie),
            has_cookie=bool(sync_cookie),
            using_download_cookie=not sync_cookie and bool((s.cookie or "").strip()),
            days=s.sync_days or 7,
        )

    def update_config(self, payload: SyncConfigUpdateIn) -> SyncConfigOut:
        """部分更新。字段是否"动过"由 exclude_unset 区分；值显式为 null
        视同不改（与系统设置的 None=不修改语义保持一字不差）。"""
        s = self._require()
        data = payload.model_dump(exclude_unset=True)
        cookie = data.pop("cookie", None)
        if cookie and cookie.strip():
            # 空串/纯空白 = "没填"而不是"清空"：与系统设置页同一约定
            s.sync_cookie = cookie.strip()
        days = data.get("days")
        if days is not None:
            s.sync_days = int(days)
        self.db.commit()
        logger.info("同步配置已更新: %s", ", ".join(data.keys()) or ("cookie" if cookie else "无字段"))
        return self.get_config()

    # ---------- 状态 ----------

    def get_status(self) -> SyncStatusOut:
        s = self._require()
        source = "sync" if (s.sync_cookie or "").strip() else ("download" if (s.cookie or "").strip() else "none")
        return SyncStatusOut(
            cookie_source=source,
            last_sync_at=s.last_sync_at,
            last_sync_status=s.last_sync_status or "",
            last_sync_message=s.last_sync_message or "",
            pending_count=self._pending_count(),
        )

    def list_pending(self) -> list[SyncLessonOut]:
        """待下载新课列表（同步新增且从未纳入过任何任务）。

        按开课时间倒序：用户最可能勾选最近的课。started 由后端判定，
        前端据此默认不勾选未开课的课（暂无回放，下了必失败），
        但不禁用——是否下载最终由用户判断。
        """
        rows = self.db.scalars(
            select(Lesson).where(Lesson.sync_pending.is_(True)).order_by(Lesson.start_time.desc(), Lesson.id.desc())
        ).all()
        return [
            SyncLessonOut(
                id=lesson.id,
                lesson_id=lesson.lesson_id,
                lesson_name=lesson.lesson_name or "",
                class_name=lesson.class_name or "",
                start_time=lesson.start_time or "",
                started=_lesson_started(lesson.start_time),
            )
            for lesson in rows
        ]

    def _pending_count(self) -> int:
        return int(self.db.scalar(select(func.count()).select_from(Lesson).where(Lesson.sync_pending.is_(True))) or 0)

    def verify_cookie(self, cookie: str | None) -> CookieVerifyOut:
        """验证顺序：草稿 → 同步专用 → 下载回退。覆盖"粘贴后先验再存"
        与"不填时验证当前生效的那份"两种用户路径。"""
        s = self._require()
        target = (cookie or "").strip() or (s.sync_cookie or "").strip() or (s.cookie or "").strip()
        return verify_console_cookie(target)

    # ---------- 执行 ----------

    def run(self) -> SyncRunOut:
        """执行一次同步（按钮触发）。非阻塞闸门：撞上已有轮次直接 409 语义报错。

        Why finally 释放而不是 try/except：闸门必须释放，哪怕内部
        抛了未预期异常（否则 _sync_lock 永久占用，同步功能"死"掉）。
        """
        if not _sync_lock.acquire(blocking=False):
            raise ConflictError("同步正在进行中，请稍候再试")
        try:
            return self._run_locked()
        finally:
            _sync_lock.release()

    def _run_locked(self) -> SyncRunOut:
        s = self._require()
        # 凭据选择顺序固定为"同步专用 → 下载回退"：与配置页文案一致，
        # 避免"配了同步 Cookie 却因为下载 Cookie 有效而困惑"
        cookie = (s.sync_cookie or "").strip() or (s.cookie or "").strip()
        if not cookie:
            raise ValidationFailure("尚未配置 Cookie：请在【同步管理】填写控制台 Cookie，或在【系统设置】配置下载 Cookie")
        try:
            rows = fetch_lessons(cookie, max(1, s.sync_days or 7))
            created, updated = self._upsert(rows)
            message = f"同步完成：拉取 {len(rows)} 个课堂，新增 {created}，更新 {updated}"
            if created:
                message += f"；{created} 个新课已加入待下载列表，可在下方勾选后下载"
            s.last_sync_at = datetime.utcnow()
            s.last_sync_status = "success"
            s.last_sync_message = message
            self.db.commit()  # upsert 与状态一次提交：状态页与数据永远同刻一致
            logger.info("%s", message)
            return SyncRunOut(fetched=len(rows), created=created, updated=updated, message=message)
        except BizError as e:
            self._record_failure(s, e.message)
            raise
        except Exception as e:
            # 兜底：任何未预期异常也不能让"同步失败"静默——状态卡是用户
            # 判断"要不要重试"的唯一线索；转成业务错误让 API 层返回可读文案
            logger.exception("同步发生未预期异常")
            self._record_failure(s, f"同步异常：{e.__class__.__name__}")
            raise ValidationFailure(f"同步失败：{e.__class__.__name__}") from e

    def _record_failure(self, s: AppSetting, message: str) -> None:
        """失败落库（时间+文案）。按钮式触发下重试由用户决定，
        这里不做节流——记录本身就是给用户看的"上次尝试"证据。"""
        s.last_sync_at = datetime.utcnow()
        s.last_sync_status = "failed"
        s.last_sync_message = message[:1000]
        self.db.commit()

    def _upsert(self, rows: list[dict]) -> tuple[int, int]:
        """幂等入库，返回 (新增, 更新)。规则与 CSV 导入完全一致：
        lesson_id 已存在→刷新字段（空值不覆盖）；不存在→新建并标 sync_pending。
        行内去重先行：分页期间数据被改动可能翻出重复行，同轮内不应重放。"""
        created = updated = 0
        seen: set[str] = set()
        for row in rows:
            lesson_id = row["lesson_id"]
            if lesson_id in seen:
                continue  # 同轮内先到先得：分页错位导致的重复行只处理第一条
            seen.add(lesson_id)
            existing = self.db.scalars(select(Lesson).where(Lesson.lesson_id == lesson_id)).first()
            if existing:
                changed = False
                for field, value in row.items():
                    # "value and" 跳过空值：源数据留空≠清空该字段（与导入一致）
                    if field != "lesson_id" and value and getattr(existing, field) != value:
                        setattr(existing, field, value)
                        changed = True
                updated += int(changed)  # 无变化的行不计入 updated（幂等复跑时 created=updated=0）
            else:
                # 只有"新建"才打 sync_pending：待下载列表的语义是"补新"，
                # 老课重同步不该再冒出来让用户选（要重下走任务页/视频库重试）
                self.db.add(Lesson(**row, sync_pending=True))
                created += 1
        self.db.commit()  # 整批单事务：upsert 中途失败不会留下半批脏数据
        return created, updated
