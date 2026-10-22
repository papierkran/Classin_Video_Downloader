"""控制台同步服务：把「班级管理」「课堂管理」两个页面的接口落库，并提供本地↔网站的下载对比。

接口来源（均在控制台登录态下实测确认，见 docs/技术方案.md）：
- 班级/课程：POST /course/web/list            （JSON，page/pageSize）
- 课堂：      POST /classroom/web/class/list   （JSON，page/pageSize，可不带 timeRange=全量）
- 回放片段：  POST /saasajax/school.ajax.php?action=getClassVodList
              （form：courseId/classId/page/perpage；**注意是 school.ajax.php
               而不是 webcast.ajax.php**——同 action 名在 webcast 上返回空体，
               这是实测踩坑结论，别再改回去）

设计要点：
- 与 sync_service 共用控制台 HTTP 事实来源：常量、请求头、errno 解析、
  时间戳格式化、进程级同步闸门都从那里复用，避免两处漂移。
- 同步是幂等 upsert：courses 以 course_id 唯一、lessons 以 lesson_id 唯一；
  空值不覆盖（网站这次没给≠清空本地）。
- 对比分两级：课堂级用本地 VideoAsset 聚合（列表页零额外请求）；片段级
  按需展开，实时拉 getClassVodList + 本地台账做匹配。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

import requests
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..core.exceptions import NotFoundError, ValidationFailure
from ..db.models import AppSetting, Course, Lesson, VideoAsset
from ..schemas import (
    ConsoleSyncRunOut,
    LessonConsoleOut,
    LessonSegmentsOut,
    SegmentOut,
    TaskCreateIn,
)
from .sync_service import (
    CONSOLE_API_BASE,
    MAX_PAGES,
    PAGE_SIZE,
    _sync_lock,
    _error_info,
    _format_unix,
    _headers as _json_headers,
)
from .task_service import TaskService

logger = logging.getLogger("app.course")

COURSE_LIST_PATH = "/course/web/list"
CLASS_LIST_PATH = "/classroom/web/class/list"
VOD_LIST_PATH = "/saasajax/school.ajax.php"

# 片段匹配容差：同一片段直链下载字节数一致，但个别源可能重封装，
# 允许 1% 误差；再小会漏配、再大会误配到另一段。
_SIZE_TOLERANCE = 0.01


# ---------- 控制台 HTTP ----------


def _headers(cookie: str, form: bool = False) -> dict:
    """form=True 时用表单 Content-Type（school.ajax.php 系列接口）。"""
    headers = _json_headers(cookie)
    if form:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    return headers


def _post(cookie: str, path: str, *, json_body: dict | None = None, form_body: str | None = None) -> dict:
    """POST 并在解析前剥离 UTF-8 BOM。

    Why 手动剥 BOM：classroom/course 两个接口无 BOM，但 school.ajax.php
    会在正文最前面塞一个 \\ufeff，requests 的 resp.json() 会因此抛
    "Expecting value: line 1 column 1"，用 utf-8-sig 解码后 json.loads 才稳。
    """
    try:
        resp = requests.post(
            CONSOLE_API_BASE + path,
            json=json_body,
            data=form_body,
            headers=_headers(cookie, form=form_body is not None),
            timeout=25,
        )
    except requests.RequestException as e:
        raise ValidationFailure(f"连接 ClassIn 控制台失败（{e.__class__.__name__}），请检查服务器网络后重试") from e
    try:
        data = json.loads(resp.content.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError) as e:
        raise ValidationFailure(f"控制台返回非 JSON 响应（HTTP {resp.status_code}），Cookie 可能已失效") from e
    if not isinstance(data, dict):
        raise ValidationFailure("控制台响应格式异常")
    return data


def _check(data: dict, what: str) -> dict:
    """校验 errno 并返回 data 段。101 翻译成"重新登录"的可执行文案。"""
    errno, text = _error_info(data)
    if errno == 1:
        body = data.get("data")
        return body if isinstance(body, dict) else {}
    if errno == 101:
        raise ValidationFailure("Cookie 已过期或未登录，请重新登录 ClassIn 控制台后更新 Cookie")
    raise ValidationFailure(f"拉取{what}失败（errno={errno}）{text[:120]}")


# ---------- 归一化 ----------


def _pick(item: dict, *keys: str) -> str:
    """按序取第一个非空字段（ID/名称字段名跨版本会漂移，多留别名）。"""
    for key in keys:
        value = item.get(key)
        if value is None or value == "" or value == 0:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _normalize_course(item: dict) -> dict | None:
    course_id = _pick(item, "courseId", "id")
    if not course_id:
        return None
    main = item.get("mainUserInfo") if isinstance(item.get("mainUserInfo"), dict) else {}
    cat = item.get("catInfo")
    cat_name = cat.get("name") if isinstance(cat, dict) else (cat if isinstance(cat, str) else "")
    return {
        "course_id": course_id,
        "course_name": _pick(item, "courseName", "name"),
        "teacher_name": _pick(main, "name") or _pick(item, "teacherName", "createrName"),
        "creator_name": _pick(item, "createrName", "creatorName"),
        "class_total": _as_int(item.get("totalClassNum")) if item.get("totalClassNum") is not None else 0,
        "complete_class_num": _as_int(item.get("completeClassNum")) if item.get("completeClassNum") is not None else 0,
        "student_num": _as_int(item.get("studentNum")) if item.get("studentNum") is not None else 0,
        "course_state": _as_int(item.get("courseState")),
        "first_class_begin_time": _format_unix(item.get("firstClassBeginTime")),
        "end_time": _format_unix(item.get("endTime")),
        "add_time": _format_unix(item.get("addTime")),
        "cat_name": str(cat_name or "").strip(),
    }


def _normalize_lesson(item: dict) -> dict | None:
    lesson_id = _pick(item, "classId", "lessonId", "id")
    if not lesson_id:
        return None
    teacher = item.get("teacher") if isinstance(item.get("teacher"), dict) else {}
    video = item.get("videoArray") if isinstance(item.get("videoArray"), dict) else {}
    unit = item.get("unit")
    if isinstance(unit, dict):
        unit = unit.get("name") or unit.get("title") or ""
    start = item.get("startTime")
    end = item.get("endTime")
    duration = 0
    if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end > start > 0:
        duration = int(end - start)
    return {
        "class_id": _pick(item, "courseId"),
        "lesson_id": lesson_id,
        "lesson_name": _pick(item, "name", "className", "lessonName"),
        "class_name": _pick(item, "courseName"),
        "start_time": _format_unix(start),
        # 开课时间的 epoch 秒（视频库日期范围筛选用），与控制台 startTime 同源
        "start_ts": int(start) if isinstance(start, (int, float)) and start > 0 else None,
        "creation_time": _format_unix(item.get("createdAt") or item.get("createTime")),
        "unit": str(unit or "").strip()[:255],
        "end_time": _format_unix(end),
        "duration": duration,
        "teacher_name": _pick(teacher, "name")[:128],
        "class_status": _as_int(item.get("classStatus")),
        "record_state": _as_int(video.get("recordState")),
        "live_state": _as_int(video.get("liveState")),
    }


# ---------- 拉取（分页） ----------


def _fetch_paged(cookie: str, path: str, base_payload: dict, items_key: str, normalizer, what: str, max_pages: int = MAX_PAGES) -> list[dict]:
    rows: list[dict] = []
    for page in range(1, max_pages + 1):
        body = _check(_post(cookie, path, json_body={**base_payload, "page": page, "pageSize": PAGE_SIZE}), what)
        raw = body.get(items_key)
        items = raw if isinstance(raw, list) else []
        rows.extend(r for r in (normalizer(i) for i in items if isinstance(i, dict)) if r)
        if len(items) < PAGE_SIZE:
            break
        # course 接口用 totalCourseNum，class 接口用 total；两者都存在则提前收敛
        total = body.get("total") or body.get("totalCourseNum")
        try:
            if total and page * PAGE_SIZE >= int(total):
                break
        except (TypeError, ValueError):
            pass
    else:
        logger.warning("同步 %s 达到分页上限 %d 页仍未取完", path, max_pages)
    return rows


def fetch_courses(cookie: str) -> list[dict]:
    """拉全部班级/课程。不带 courseState：列表页按"进行中/已结束"分 Tab，
    同步要的是全量（已结束班级也要能在页面回看）。"""
    return _fetch_paged(cookie, COURSE_LIST_PATH, {"withAuth": 0}, "courseList", _normalize_course, "班级列表")


def fetch_console_lessons(cookie: str) -> list[dict]:
    """拉全部课堂。不带 timeRange/searchStatus —— 实测带上 timeRange 需要
    合法时间戳、searchStatus 是 UI 枚举，都不传即返回全量。"""
    return _fetch_paged(cookie, CLASS_LIST_PATH, {}, "list", _normalize_lesson, "课堂列表")


VOD_PAGE_SIZE = 100  # 实测 perpage 上限就是 100，传 200 会被服务端拒（errno=104）


def fetch_class_vod(cookie: str, course_id: str, class_id: str) -> tuple[int, list[dict]]:
    """拉单课堂的回放片段清单，返回 (AllCount, FileList)。

    perpage 固定 100：接口对超过 100 的值直接回 errno=104（实测踩坑），
    需要更多时靠 page 翻页补齐。
    """
    files: list[dict] = []
    total = 0
    for page in range(1, MAX_PAGES + 1):
        form = f"courseId={course_id}&classId={class_id}&page={page}&perpage={VOD_PAGE_SIZE}"
        body = _check(_post(cookie, f"{VOD_LIST_PATH}?action=getClassVodList", form_body=form), "回放片段")
        vod = body.get("VodInfo") if isinstance(body.get("VodInfo"), dict) else {}
        batch = [f for f in (vod.get("FileList") or []) if isinstance(f, dict)]
        if vod.get("AllCount") is not None:
            total = _as_int(vod.get("AllCount"))
        files.extend(batch)
        if len(batch) < VOD_PAGE_SIZE or (total and len(files) >= total):
            break
    return (total if total else len(files)), files


# ---------- 领域服务 ----------


class CourseService:
    """课程/课堂同步 + 网站对比。cookie 选择与失败落库复用 AppSetting 字段。"""

    def __init__(self, db: Session):
        self.db = db

    def _require(self) -> AppSetting:
        row = self.db.get(AppSetting, 1)
        if not row:
            raise NotFoundError("配置尚未初始化，请重启服务")
        return row

    def _cookie(self, s: AppSetting) -> str:
        # Cookie 统一在【系统设置】管理：控制台同步与下载引擎是同一份
        # .eeo.cn 登录态，页面不再提供第二处填写入口（避免"改一处忘另一处"）
        return (s.cookie or "").strip()

    # ---------- 同步 ----------

    def run_sync(self) -> ConsoleSyncRunOut:
        if not _sync_lock.acquire(blocking=False):
            raise ValidationFailure("同步正在进行中，请稍候再试")
        try:
            return self._run_locked()
        finally:
            _sync_lock.release()

    def _run_locked(self) -> ConsoleSyncRunOut:
        s = self._require()
        cookie = self._cookie(s)
        if not cookie:
            raise ValidationFailure("尚未配置 Cookie：请到【系统设置】填写登录 Cookie（同步与下载共用同一份）")
        now = datetime.utcnow()
        try:
            courses = fetch_courses(cookie)
            lessons = fetch_console_lessons(cookie)
            c_created, c_updated = self._upsert_courses(courses, now)
            l_created, l_updated = self._upsert_lessons(lessons, now)
            message = (
                f"同步完成：班级拉取 {len(courses)}（新增 {c_created}/更新 {c_updated}），"
                f"课堂拉取 {len(lessons)}（新增 {l_created}/更新 {l_updated}）"
            )
            s.last_sync_at = now
            s.last_sync_status = "success"
            s.last_sync_message = message
            self.db.commit()
            logger.info("%s", message)
            return ConsoleSyncRunOut(
                courses_fetched=len(courses), courses_created=c_created, courses_updated=c_updated,
                lessons_fetched=len(lessons), lessons_created=l_created, lessons_updated=l_updated,
                message=message,
            )
        except Exception as e:
            # 失败也必须落库：状态卡是用户判断"要不要重试/换 Cookie"的唯一线索
            s.last_sync_at = now
            s.last_sync_status = "failed"
            s.last_sync_message = (e.message if isinstance(e, ValidationFailure) else f"同步异常：{e.__class__.__name__}")
            self.db.commit()
            raise

    def _upsert_courses(self, rows: list[dict], now: datetime) -> tuple[int, int]:
        created = updated = 0
        for row in rows:
            existing = self.db.scalars(select(Course).where(Course.course_id == row["course_id"])).first()
            if existing:
                changed = False
                for field, value in row.items():
                    if field == "course_id":
                        continue
                    if value not in ("", 0, -1, None) and getattr(existing, field) != value:
                        setattr(existing, field, value)
                        changed = True
                existing.synced_at = now
                updated += int(changed)
            else:
                self.db.add(Course(**row, synced_at=now))
                created += 1
        return created, updated

    def _upsert_lessons(self, rows: list[dict], now: datetime) -> tuple[int, int]:
        created = updated = 0
        for row in rows:
            existing = self.db.scalars(select(Lesson).where(Lesson.lesson_id == row["lesson_id"])).first()
            if existing:
                changed = False
                for field, value in row.items():
                    if field == "lesson_id":
                        continue
                    if value not in ("", 0, -1, None) and getattr(existing, field) != value:
                        setattr(existing, field, value)
                        changed = True
                existing.website_synced_at = now
                updated += int(changed)
            else:
                # 新建行不打 sync_pending：课程/课堂同步是"镜像 + 对比"用途，
                # 不该把上千条历史课堂全塞进"待下载新课"列表（那是 /api/sync 的语义）
                self.db.add(Lesson(**row, website_synced_at=now))
                created += 1
        return created, updated

    # ---------- 列表 ----------

    def list_courses(self, page: int, page_size: int, keyword: str = "", state: str = "") -> tuple[int, list[Course]]:
        stmt = select(Course)
        if keyword.strip():
            pattern = f"%{keyword.strip()}%"
            stmt = stmt.where(Course.course_name.like(pattern) | Course.course_id.like(pattern) | Course.teacher_name.like(pattern))
        if state != "":
            stmt = stmt.where(Course.course_state == _as_int(state))
        total = self.db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = list(self.db.scalars(stmt.order_by(Course.first_class_begin_time.desc(), Course.id.desc()).offset((page - 1) * page_size).limit(page_size)).all())
        return total, rows

    def list_lessons(self, page: int, page_size: int, keyword: str = "", download_state: str = "", class_id: str = "", order: str = "desc") -> tuple[int, list[LessonConsoleOut]]:
        """课堂列表 + 本地下载对比。

        download_state 依赖 VideoAsset 聚合，必须在 SQL 侧按整表过滤后再分页
        （否则 total 与翻页都只基于当页子集，筛选结果会失真）。

        Why 不过滤 website_synced_at：手工/CSV 导入的课堂也要留在这个页面
        （保留原有的编辑/删除/建任务能力），只是网站侧字段为空。
        """
        done = ("success", "skipped")
        agg = (
            select(
                VideoAsset.lesson_id.label("lid"),
                func.coalesce(func.sum(case((VideoAsset.status.in_(done), 1), else_=0)), 0).label("ok"),
                func.coalesce(func.sum(case((VideoAsset.status == "failed", 1), else_=0)), 0).label("fail"),
            )
            .group_by(VideoAsset.lesson_id)
            .subquery()
        )
        ok_c = func.coalesce(agg.c.ok, 0)
        fail_c = func.coalesce(agg.c.fail, 0)
        stmt = select(Lesson).outerjoin(agg, agg.c.lid == Lesson.lesson_id)
        if class_id:  # 班级展开：按班级/课程 ID 精确取该校全部课堂
            stmt = stmt.where(Lesson.class_id == class_id)
        if keyword.strip():
            pattern = f"%{keyword.strip()}%"
            stmt = stmt.where(Lesson.lesson_name.like(pattern) | Lesson.class_name.like(pattern) | Lesson.lesson_id.like(pattern))
        if download_state == "none":
            stmt = stmt.where(ok_c == 0, fail_c == 0)
        elif download_state == "done":
            stmt = stmt.where(ok_c > 0, fail_c == 0)
        elif download_state == "failed":
            stmt = stmt.where(ok_c == 0, fail_c > 0)
        elif download_state == "partial":
            stmt = stmt.where(ok_c > 0, fail_c > 0)
        total = self.db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        # 同一接口两处消费方排序诉求相反：课堂管理页要"最新在上"(desc)，
        # 班级同步的班级展开要"最老在上"(asc)。start_time 未补零，统一用
        # start_ts 排；is_(None) 升序把无时间的旧数据固定放到最后。
        if order == "asc":
            order_by = (Lesson.start_ts.is_(None), Lesson.start_ts.asc(), Lesson.start_time.asc(), Lesson.id.asc())
        else:
            order_by = (Lesson.start_ts.is_(None), Lesson.start_ts.desc(), Lesson.start_time.desc(), Lesson.id.desc())
        lessons = list(self.db.scalars(stmt.order_by(*order_by).offset((page - 1) * page_size).limit(page_size)).all())
        agg_map = self._local_agg([lesson.lesson_id for lesson in lessons])
        items = [self._to_console_out(lesson, *agg_map.get(lesson.lesson_id, (0, 0, 0, None))) for lesson in lessons]
        return total, items

    def _local_agg(self, lesson_ids: list[str]) -> dict[str, tuple[int, int, int, datetime | None]]:
        """一次聚合出本页每个课堂的 (本地总数, 已完成, 失败, 最近完成时间)。"""
        if not lesson_ids:
            return {}
        done = ("success", "skipped")
        stmt = (
            select(
                VideoAsset.lesson_id,
                func.count(VideoAsset.id),
                func.coalesce(func.sum(case((VideoAsset.status.in_(done), 1), else_=0)), 0),
                func.coalesce(func.sum(case((VideoAsset.status == "failed", 1), else_=0)), 0),
                func.max(case((VideoAsset.status.in_(done), VideoAsset.updated_at), else_=None)),
            )
            .where(VideoAsset.lesson_id.in_(lesson_ids))
            .group_by(VideoAsset.lesson_id)
        )
        out: dict[str, tuple[int, int, int, datetime | None]] = {}
        for lesson_id, total, ok, fail, done_at in self.db.execute(stmt).all():
            out[lesson_id] = (int(total or 0), int(ok or 0), int(fail or 0), done_at)
        return out

    @staticmethod
    def _to_console_out(lesson: Lesson, local_total: int, local_done: int, local_failed: int, downloaded_at: datetime | None) -> LessonConsoleOut:
        if local_done and local_failed:
            state = "partial"
        elif local_done:
            state = "done"
        elif local_failed:
            state = "failed"
        else:
            state = "none"
        return LessonConsoleOut(
            id=lesson.id, class_id=lesson.class_id or "", lesson_id=lesson.lesson_id,
            lesson_name=lesson.lesson_name or "", class_name=lesson.class_name or "",
            start_time=lesson.start_time or "", creation_time=lesson.creation_time or "",
            status=lesson.status or "pending", message=lesson.message or "", updated_at=lesson.updated_at,
            unit=lesson.unit or "", end_time=lesson.end_time or "", duration=lesson.duration or 0,
            teacher_name=lesson.teacher_name or "", class_status=lesson.class_status, record_state=lesson.record_state,
            live_state=lesson.live_state, website_synced_at=lesson.website_synced_at,
            local_total=local_total, local_done=local_done, local_failed=local_failed,
            downloaded_at=downloaded_at, download_state=state,
        )

    # ---------- 按班级下载 ----------

    def create_course_task(self, course_id: str, user_id: int | None = None):
        """为该班级下的全部课堂建任务并直接启动（「班级同步」页的一键下载）。

        Why 服务端一次成型而不是前端先拉全量 lesson_ids 再回传：
        班级课堂数可能超过单页上限，前端分页拼 id 既脆弱又多一轮往返；
        课堂已全在库（class_id=courseId），服务端直接查即可。
        """
        lessons = list(self.db.scalars(select(Lesson).where(Lesson.class_id == course_id)).all())
        if not lessons:
            raise ValidationFailure("该班级下还没有同步到课堂，请先点击「立即同步」")
        course = self.db.scalars(select(Course).where(Course.course_id == course_id)).first()
        name = f"下载班级：{course.course_name}" if course and course.course_name else f"下载班级 {course_id}"
        task = TaskService(self.db).create(
            TaskCreateIn(name=name, lesson_ids=[lesson.id for lesson in lessons]), user_id=user_id,
        )
        return TaskService(self.db).start(task.id)

    # ---------- 片段级对比 ----------

    def lesson_segments(self, lesson_id: int) -> LessonSegmentsOut:
        """按需拉网站片段 + 本地台账做匹配（展开课堂行时调用）。"""
        lesson = self.db.get(Lesson, lesson_id)
        if not lesson:
            raise NotFoundError("课堂不存在")
        s = self._require()
        cookie = self._cookie(s)
        if not cookie:
            raise ValidationFailure("尚未配置 Cookie，无法获取网站回放片段")

        assets = list(self.db.scalars(select(VideoAsset).where(VideoAsset.lesson_id == lesson.lesson_id)).all())
        try:
            website_total, files = fetch_class_vod(cookie, lesson.class_id or "", lesson.lesson_id)
            message = ""
        except ValidationFailure as e:
            # Cookie 过期/接口异常：不阻断列表，返回本地片段 + 可读提示
            website_total, files, message = 0, [], e.message

        # 本地匹配：以文件大小为主键（同一片段直链下载字节数一致），命中即消费；
        # 大小缺失（未完成/扫描失败）的行退化为不匹配，避免误配。
        pool = [a for a in assets if a.size_bytes]
        used: set[int] = set()
        segments: list[SegmentOut] = []
        for f in files:
            size = _as_int(f.get("Size")) if f.get("Size") is not None else 0
            match = None
            for a in pool:
                if a.id in used:
                    continue
                if size and abs(a.size_bytes - size) <= size * _SIZE_TOLERANCE:
                    match = a
                    break
            if match is not None:
                used.add(match.id)
            segments.append(self._to_segment(f, match))

        local_only = [self._to_segment(None, a) for a in assets if a.id not in used]
        return LessonSegmentsOut(
            lesson_id=lesson.lesson_id, course_id=lesson.class_id or "",
            website_total=website_total, local_total=len(assets),
            segments=segments, local_only=local_only, message=message,
        )

    @staticmethod
    def _to_segment(file: dict | None, asset: VideoAsset | None) -> SegmentOut:
        if file is None:
            # 本地独有片段（网站已删/CIDExt 不同）：只带本地侧信息
            assert asset is not None
            return SegmentOut(
                title=asset.title or "(本地片段)", size_bytes=asset.size_bytes or 0,
                downloaded=asset.status in ("success", "skipped"),
                local_status=asset.status, local_title=asset.title or "", local_id=asset.id,
            )
        ext = _pick(file, "CIDExt")
        name = _pick(file, "FileName") or ext or "(片段)"
        return SegmentOut(
            file_id=_pick(file, "FileId"), title=name,
            start_time=str(file.get("StartTime") or ""), end_time=str(file.get("EndTime") or ""),
            duration=_as_int(file.get("Duration")) if file.get("Duration") is not None else 0,
            size_bytes=_as_int(file.get("Size")) if file.get("Size") is not None else 0,
            status=_as_int(file.get("Status")) if file.get("Status") is not None else 0,
            source_type=_as_int(file.get("SourceType")) if file.get("SourceType") is not None else 0,
            ext=ext, downloaded=asset is not None,
            local_status=asset.status if asset else "",
            local_title=(asset.title if asset else "") or "",
            local_id=asset.id if asset else None,
            # Playset 里的直链只用于播放/下载，不落库、不出参（沿用视频库的票据模型）
        )
