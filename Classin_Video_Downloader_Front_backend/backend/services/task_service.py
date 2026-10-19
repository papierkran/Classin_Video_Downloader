"""下载任务服务：集中式状态机 + 后台 worker 线程 + 引擎回调桥接。

架构（Why 这样分层）：
  API(薄) ──> TaskService(状态迁移合法性) ──> worker线程(引擎阻塞调用)
                     │                              │
                     └──── 读写 download_tasks ─────┘（回调落库）

- 状态机只在本类迁移：created→running→(paused⇄running)→stopping→终态，
  避免 status 赋值散落各处后出现"已停止却显示运行中"这类不一致。
- 引擎 download_videos 是阻塞调用（内部含 Selenium + 线程池），
  必须放在独立线程，否则会卡死 uvicorn 事件循环。
- 全局同一时刻仅允许一个任务：Selenium 单浏览器实例 + SQLite 单写者，
  并发任务收益为负（争抢磁盘/驱动），用 _runtime 单例做闸门。
"""
from __future__ import annotations

import logging
import hashlib
import os
import re
import threading
import time
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..core.config import settings as app_settings
from ..core.exceptions import ConflictError, NotFoundError, ValidationFailure
from ..core.fsutil import db_path, display_name, fsencode_key, fs_path
from ..db.database import SessionLocal
from ..db.models import AppSetting, DownloadLog, DownloadTask, Lesson, TaskItem, VideoAsset
from ..engine import videodownloader
from ..repositories import LessonRepo, LogRepo, TaskItemRepo, TaskRepo
from ..schemas import TaskCreateIn, TaskDetailOut, TaskItemOut, TaskLogsPageOut

logger = logging.getLogger("app.task")

# 可启动（或重新启动）的状态集合；running/paused/stopping 一律拒绝
RESTARTABLE = ("created", "failed", "cancelled", "success")
ACTIVE = ("running", "paused", "stopping")  # 占用全局闸门的状态
# Why 把状态集提成常量而非散落字符串：start/delete/pause 三处都要判断，
# 集中一处改（如新增 queued 状态）不会漏改——漏改的表现是"新状态能被
# 并发启动"，属并发 bug，极难复现。

# ---- 模块级单例：worker 与请求线程共享的唯一运行时 ----
_runtime_lock = threading.Lock()  # 闸门：start/pause/resume/stop/delete 互斥
_runtime: "TaskRuntime | None" = None
# Why 模块级而非类属性：worker 是模块函数（线程目标），直接引用模块
# 变量最简单；放进类要绕"类还是实例"的归属问题，没有实际收益。


class TaskRuntime:
    """一次运行的任务的内存态（进程重启即丢，权威状态始终在 DB）。"""

    def __init__(self, task_id: int, items_map: dict[str, int], total_items: int):
        self.task_id = task_id
        self.pause_event = threading.Event()
        self.stop_event = threading.Event()
        self.driver_ref: dict = {}  # 引擎回传 WebDriver，异常时由 worker 兜底 quit
        self.items_map = items_map  # lesson_id -> task_item.id（回调免查库）
        self.total_items = total_items
        self.items_done = 0  # 已出终态的课堂数（算总进度用）
        self.lock = threading.Lock()  # 序列化回调对 DB 的写（回调来自线程池多线程）
        self.last_flush = 0.0  # 进度落库节流时间戳
        self.finished = False
        # 进程内存中的进度快照，节流写库
        self.percent = 0
        self.speed_mbps = 0.0
        self.current_lesson = ""
        self.item_percent = 0  # 当前课堂内部进度
        self.video_last_flush: dict[str, float] = {}


class TaskService:
    """任务领域服务：状态机迁移 + 查询 + 日志。

    为什么状态迁移方法都要拿 _runtime_lock：
    start 会在锁内创建 worker 线程，pause/stop 要看到同一份 _runtime；
    不加锁的话 "start 刚建好 runtime 还没赋值，pause 就进来了" 会误报
    "任务未在运行"。锁粒度是"一次状态判定+一次写库"（毫秒级），
    不会阻塞轮询读（读不拿这把锁）。
    """

    def __init__(self, db: Session):
        self.db = db
        self.repo = TaskRepo(db)
        self.item_repo = TaskItemRepo(db)
        self.log_repo = LogRepo(db)
        self.lesson_repo = LessonRepo(db)

    # ---------- 查询 ----------

    def list(self, page: int, page_size: int, status: str = "") -> tuple[int, list[DownloadTask]]:
        """分页列表。count 与取页共用同一 stmt，保证 total 与 items 口径一致
        （先后两次不同条件查询会出现"共 N 条但翻不到第 N 条"的错位）。"""
        stmt = self.repo.filtered(status=status)
        return self.repo.count(stmt), self.repo.page(stmt, page, page_size)

    def get(self, task_id: int) -> DownloadTask:
        """取任务，不存在抛 404（把 NotFoundError 收敛在 service，
        路由层无需逐个判空，也不用返回 200+null 这种歧义响应）。"""
        task = self.repo.get(task_id)
        if not task:
            raise NotFoundError("任务不存在")
        return task

    def get_detail(self, task_id: int) -> TaskDetailOut:
        """详情 = 任务本体 + 明细 items。两查合一而非 JOIN：items 行数
        少（≤5000）且需要独立序列化，JOIN 反而要做行去重。"""
        task = self.get(task_id)
        detail = TaskDetailOut.model_validate(task)
        detail.items = [TaskItemOut.model_validate(i) for i in self.item_repo.list_by_task(task_id)]
        return detail

    def get_logs(self, task_id: int, after_id: int, limit: int) -> TaskLogsPageOut:
        """增量日志页。last_id 回传给前端做下次轮询的游标——
        前端只需存一个整数，无需感知日志总量。"""
        self.get(task_id)  # 不存在直接 404
        rows = self.log_repo.list_after(task_id, after_id, limit)
        from ..schemas import TaskLogOut

        items = [TaskLogOut.model_validate(r) for r in rows]
        return TaskLogsPageOut(items=items, last_id=items[-1].id if items else after_id)

    def append_log(self, task_id: int, message: str, level: str = "INFO") -> None:
        """service 层事件写日志（启动/停止等用户操作也要留痕）。

        Why 单独提供：引擎 log_callback 只覆盖"下载过程"，用户点了暂停
        这类交互事件引擎不知道；统一写同一张表，日志时间线才完整。
        """
        self.db.add(DownloadLog(task_id=task_id, level=level, message=message[:2000]))
        self.db.commit()

    # ---------- 创建 ----------

    def create(self, payload: TaskCreateIn, user_id: int | None = None) -> DownloadTask:
        """创建任务（不启动）。手动建任务与自动同步共用这一个入口。

        目录回退顺序：调用方指定 → 系统设置默认目录 → 配置默认值。
        快照式写入 TaskItem：任务执行期间课堂库再变化也不影响已建任务。
        """
        lessons = self._resolve_lessons(payload.lesson_ids)
        save_dir = (payload.save_dir or "").strip()
        if not save_dir:
            row = self.db.get(AppSetting, 1)
            save_dir = (row.save_dir if row else "") or app_settings.default_save_dir

        task = self.repo.add(
            DownloadTask(
                name=payload.name,
                save_dir=save_dir,
                lesson_count=len(lessons),
                created_by=user_id,
            )
        )
        # 快照课堂到 items（Why 快照：创建后用户改/删课堂不影响本任务）
        for lesson in lessons:
            self.db.add(
                TaskItem(
                    task_id=task.id,
                    lesson_id=lesson.lesson_id,
                    class_id=lesson.class_id,
                    lesson_name=lesson.lesson_name,
                    class_name=lesson.class_name,
                    start_time=lesson.start_time,
                )
            )
        # 纳入任务即清"同步新增"标记：自动下载的语义是"只补没下过的新课"，
        # 不清标记每轮同步都会把同一批课重复下单（手动建任务同样受益——
        # 用户手动下过的课，同步不必再替它操心）。
        if lessons:
            self.db.execute(
                update(Lesson)
                .where(Lesson.id.in_([lesson.id for lesson in lessons]), Lesson.sync_pending.is_(True))
                .values(sync_pending=False)
            )
        self.db.commit()
        logger.info("创建任务 #%d '%s' (%d 个课堂)", task.id, task.name, len(lessons))
        return task

    def _resolve_lessons(self, lesson_ids: list[int] | None) -> list[Lesson]:
        if lesson_ids:
            lessons = self.lesson_repo.list_by_ids(lesson_ids)
            if not lessons:
                raise ValidationFailure("所选课堂不存在或已被删除")
            return lessons
        lessons = self.lesson_repo.list_all()
        if not lessons:
            raise ValidationFailure("还没有课堂数据，请先导入 CSV/XLSX")
        return lessons

    # ---------- 状态机操作（全部持全局闸门锁） ----------

    def start(self, task_id: int) -> DownloadTask:
        """启动（或重跑）任务。校验顺序刻意是"状态 → 闸门 → 数据 → 环境"：
        越便宜的判定越先做，避免每次点击都白查一遍配置表；
        全部在锁内完成，保证"检查通过"到"线程启动"之间不会被并发插入。
        """
        global _runtime
        with _runtime_lock:
            task = self.get(task_id)
            # 双重状态判定：ACTIVE 拦"运行中重复点启动"，RESTARTABLE 拦未知态
            if task.status in ACTIVE:
                raise ConflictError(f"任务当前状态为'{_zh(task.status)}'，无法再次启动")
            if task.status not in RESTARTABLE:
                raise ConflictError(f"状态'{_zh(task.status)}'的任务不能启动")
            # 全局闸门：DB 状态可能因崩溃残留为 running，所以两处都要查
            # （DB 查活跃任务 + 内存 _runtime），任一命中都拒绝
            if _runtime is not None and not _runtime.finished:
                running = self.repo.get(_runtime.task_id)
                name = running.name if running else str(_runtime.task_id)
                raise ConflictError(f"已有任务正在运行（#{_runtime.task_id} {name}），请先停止")

            items = self.item_repo.list_by_task(task_id)
            if not items:
                raise ValidationFailure("任务中没有课堂，无法启动")

            # 业务前置校验：Cookie 是引擎注入登录态的唯一凭据，空则必失败。
            # Why 在启动前拦而不是让引擎跑起来再报错：引擎启动浏览器要
            # 十几秒，fail-fast 能让用户立刻去设置页补配置。
            row = self.db.get(AppSetting, 1)
            if not row or not row.cookie.strip():
                raise ValidationFailure("尚未配置 Cookie，请到【系统设置】填写并验证后再启动")
            save_dir = task.save_dir
            try:
                import os

                os.makedirs(save_dir, exist_ok=True)
            except OSError as e:
                raise ValidationFailure(f"保存目录不可用: {e}") from e

            # 重跑前重置 items（skip_existing 会跳过已有文件，天然支持断点重跑）。
            # Why 重置：上一轮的 failed 状态若残留，_recount 会把旧失败计入
            # 本轮聚合数，进度与成败统计就失去意义。逐字段清零而非重建行：
            # items.id 是运行时 items_map 的引用目标，换行会让映射失效。
            for item in items:
                item.status = "pending"
                item.message = ""
                item.percent = 0
                item.video_total = 0
                item.video_done = 0
            task.status = "running"
            task.error = ""
            task.percent = 0
            task.speed_mbps = 0.0
            task.current_lesson = ""
            task.success_count = 0
            task.fail_count = 0
            task.started_at = datetime.utcnow()
            task.finished_at = None
            self.append_log(task_id, "任务启动")
            self.db.commit()

            runtime = TaskRuntime(
                task_id=task_id,
                items_map={i.lesson_id: i.id for i in items},
                total_items=len(items),
            )
            _runtime = runtime
            threading.Thread(target=_worker, args=(task_id, runtime), daemon=True, name=f"dl-task-{task_id}").start()
            logger.info("任务 #%d 已启动（%d 个课堂）", task_id, len(items))
            return task

    def pause(self, task_id: int) -> DownloadTask:
        with _runtime_lock:
            task = self.get(task_id)
            self._require_runtime(task_id, "暂停")
            # 显式校验当前状态：只允许 running→paused（幂等的"重复暂停"
            # 会让状态机失去意义，且二次 set 事件虽无害但掩盖了客户端时序 bug）
            if task.status != "running":
                raise ConflictError(f"任务当前状态为'{_zh(task.status)}'，无法暂停")
            task.status = "paused"
            _runtime.pause_event.set()
            self.append_log(task_id, "任务已暂停（已传输的文件会完成后再停下）")
            self.db.commit()
            return task

    def resume(self, task_id: int) -> DownloadTask:
        with _runtime_lock:
            task = self.get(task_id)
            self._require_runtime(task_id, "恢复")
            if task.status != "paused":
                raise ConflictError(f"任务当前状态为'{_zh(task.status)}'，无法恢复")
            task.status = "running"
            _runtime.pause_event.clear()
            self.append_log(task_id, "任务已恢复")
            self.db.commit()
            return task

    def stop(self, task_id: int) -> DownloadTask:
        with _runtime_lock:
            task = self.get(task_id)
            if task.status == "stopping":
                raise ConflictError("任务正在停止中，请稍候")
            self._require_runtime(task_id, "停止")
            # 先清暂停：否则引擎卡在 pause 自旋里看不到 stop 信号
            _runtime.pause_event.clear()
            _runtime.stop_event.set()
            task.status = "stopping"
            self.append_log(task_id, "收到停止指令，正在中断…")
            self.db.commit()
            return task

    def delete(self, task_id: int) -> None:
        with _runtime_lock:
            task = self.get(task_id)
            if task.status in ACTIVE:
                raise ConflictError("任务运行中，请先停止再删除")
            self.repo.delete(task)  # items/logs 级联删除
            self.repo.commit()
            logger.info("删除任务 #%d", task_id)

    @staticmethod
    def _require_runtime(task_id: int, action: str) -> None:
        if _runtime is None or _runtime.task_id != task_id or _runtime.finished:
            raise ConflictError(f"任务未在运行，无法{action}")


def _zh(status: str) -> str:
    """状态→中文展示。集中一处：错误文案要给用户看，散在各 raise 点
    改动时必然漏（漏了就直接暴露英文状态串，观感像半成品）。"""
    return {"created": "待启动", "running": "运行中", "paused": "已暂停", "stopping": "停止中", "success": "已完成", "failed": "已失败", "cancelled": "已取消"}.get(status, status)


# ---------- worker 线程 ----------


def _worker(task_id: int, runtime: TaskRuntime) -> None:
    """后台下载线程：持有独立 DB session（请求线程的 session 不能跨线程用）。

    职责链：组装 courses_override → 同步阻塞调用引擎 → finally 收尾。
    为什么把整段放 try/finally：引擎可能抛任何异常（浏览器崩溃、网络），
    若不在 finally 里 _finalize，任务会永远卡在 running 假状态。
    """
    global _runtime
    db = SessionLocal()
    error_msg = ""
    try:
        task = db.get(DownloadTask, task_id)
        row = db.get(AppSetting, 1)
        items = list(db.scalars(select(TaskItem).where(TaskItem.task_id == task_id).order_by(TaskItem.id)).all())
        # 引擎吃 dict 列表（courses_override），字段名与 CSV 导出对齐；
        # 快照而非回查 Lesson：任务运行中课堂被编辑也不影响本轮下载
        courses = [
            {
                "lesson_id": i.lesson_id,
                "course_id": i.class_id,  # 引擎侧字段名是 course_id，语义=班级ID
                "lesson_name": i.lesson_name,
                "class_name": i.class_name,
                "start_time": i.start_time,
            }
            for i in items
        ]

        videodownloader.download_videos(
            save_dir=task.save_dir,
            csv_file=None,  # 走 courses_override，不再读文件（课堂来自 DB）
            cookie_str=row.cookie if row else "",
            driver_path=(row.driver_path if row else "") or None,
            headless=bool(row.headless) if row else False,
            # 四个回调分别对应引擎的四个观察点：日志/进度/单课完成/状态变迁
            # （Observer 模式：引擎不感知 DB，只负责喊，翻译工作全在这层）
            log_callback=lambda msg: _on_log(db, runtime, msg),
            pause_event=runtime.pause_event,
            stop_event=runtime.stop_event,
            progress_callback=lambda total, idx, course, video: _on_progress(db, runtime, total, idx, course, video),
            lesson_done_callback=lambda info: _on_lesson_done(db, runtime, info),
            driver_ref=runtime.driver_ref,
            course_status_callback=lambda idx, course, st, msg: _on_course_status(db, runtime, course, st, msg),
            courses_override=courses,
            max_workers=(row.max_workers if row else 4),
            resume=True,  # 断点续传始终开启：重跑场景的主力
            speed_limit=(row.speed_limit if row else 0) * 1024,  # 库存 KB/s，引擎吃 B/s
            skip_existing=bool(row.skip_existing) if row else True,
            video_status_callback=lambda event: _on_video_status(db, runtime, event),
        )
    except Exception as e:  # 引擎内部异常（浏览器启动失败等）
        # Why 不向上抛：线程里没人接异常，抛了只会在日志留堆栈，
        # 任务状态却没人收尾；吞掉并转成 error_msg 交给 _finalize 落库
        logger.exception("任务 #%d worker 异常", task_id)
        error_msg = str(e)[:1000] or e.__class__.__name__
    finally:
        # 顺序有意：先写终态（_finalize）再清 _runtime——反了的话，
        # 并发 start 会看到"runtime 没了但 DB 还是 running"的中间态
        _finalize(db, runtime, error_msg)
        db.close()
        with _runtime_lock:
            runtime.finished = True
            if _runtime is runtime:
                _runtime = None


def _on_log(db: Session, runtime: TaskRuntime, msg: str) -> None:
    """引擎日志 → DB。try 全包：回调里抛异常会被引擎吞掉，
    但可能污染共享 session，所以自己兜底 rollback。"""
    try:
        with runtime.lock:
            db.add(DownloadLog(task_id=runtime.task_id, message=str(msg)[:2000]))
            db.commit()
    except Exception:
        # rollback 后再吞：日志写失败不影响下载主流程，
        # 但 session 已脏，不 rollback 会把后续回调一起拖垮
        logger.exception("写任务日志失败")
        db.rollback()


def _on_course_status(db: Session, runtime: TaskRuntime, course: dict, status: str, message: str) -> None:
    """引擎课程状态回调 → 同步更新 Lesson + TaskItem（仪表盘实时依赖它）。"""
    try:
        lesson_id = (course or {}).get("lesson_id", "")
        with runtime.lock:
            # items_done 是总进度的分子：min 封顶防止引擎对同一课重发
            # 终态事件（重试路径）把进度算超 100%
            if status in ("success", "failed"):
                runtime.items_done = min(runtime.items_done + 1, runtime.total_items)
                runtime.speed_mbps = 0.0
            # 回写课堂表：课堂列表的状态列与仪表盘统计都读这里
            lesson = db.scalars(select(Lesson).where(Lesson.lesson_id == lesson_id)).first()
            if lesson:
                lesson.status = status
                lesson.message = message[:255]
            item_id = runtime.items_map.get(lesson_id)
            item = db.get(TaskItem, item_id) if item_id else None
            if item:
                item.status = status
                item.message = message[:255]
                if status in ("success", "failed"):
                    # 终态行进度定格为 100：失败行的"完成度"由状态列表达，
                    # 挂着中间百分比反而像还在跑（前端按 status 着色）
                    item.percent = 100
            _recount(db, runtime)
            db.commit()
    except Exception:
        logger.exception("更新课堂状态失败")
        db.rollback()


def _on_lesson_done(db: Session, runtime: TaskRuntime, info: dict) -> None:
    """单课完成回调 → 更新视频计数与任务聚合数。

    与 _on_course_status 的分工：那个管"状态"（pending→success），
    这个管"数量"（4/4 个视频）；引擎会先发状态再发这个，故
    这里的 recount 是最终校准（幂等重算不怕重复触发）。
    """
    try:
        lesson_id = info.get("lesson_id", "")
        total = int(info.get("total_videos") or 0)
        downloaded = int(info.get("downloaded") or 0)
        with runtime.lock:
            # 走 items_map 拿行 id 而不是按 lesson_id 查库：回调高频，
            # 映射表是启动时建好的内存索引（见 TaskRuntime.items_map）
            item_id = runtime.items_map.get(lesson_id)
            item = db.get(TaskItem, item_id) if item_id else None
            if item:
                item.video_total = total
                item.video_done = downloaded
                # total=0（无视频）直接记 100：状态是 failed 时进度条满格
                # 反而误导，但 0/0 显示 0% 又像"没跑"，取 100 表示"已处理完"
                item.percent = int(downloaded * 100 / total) if total else 100
            _recount(db, runtime)
            db.commit()
    except Exception:
        logger.exception("更新课堂计数失败")
        db.rollback()


def _video_source_key(lesson_id: str, video: dict) -> str:
    """片段的稳定标识（lesson + 录制日期/方式/标题的哈希）。

    Why 用"业务身份"而不是文件路径：同一片段重试时会换文件名后缀或
    目录，按路径建键会在台账里长出重复行；哈希后定长，可直接做唯一键。
    \x1f 是单元分隔符：名称里绝不会出现，拼接不会产生歧义。
    """
    identity = "\x1f".join(str(video.get(key) or "").strip() for key in ("record_date", "record_method", "segment_title"))
    return hashlib.sha256(f"{lesson_id}\x1f{identity}".encode("utf-8")).hexdigest()


def _on_video_status(db: Session, runtime: TaskRuntime, event: dict) -> None:
    """引擎视频事件写入台账；高频进度按视频 1 秒节流，终态即时落库。"""
    try:
        course = event.get("course") or {}
        video = event.get("video") or {}
        lesson_id = str(course.get("lesson_id") or "")
        if not lesson_id:
            return
        source_key = _video_source_key(lesson_id, video)
        status = str(event.get("status") or "pending")
        now = time.monotonic()
        with runtime.lock:
            if status == "downloading" and now - runtime.video_last_flush.get(source_key, 0) < 1.0:
                return
            runtime.video_last_flush[source_key] = now
            asset = db.scalars(select(VideoAsset).where(
                VideoAsset.lesson_id == lesson_id, VideoAsset.source_key == source_key,
            )).first()
            path_asset = db.scalars(select(VideoAsset).where(
                VideoAsset.lesson_id == lesson_id, VideoAsset.file_path == db_path(str(event.get("path") or "")),
            )).first()
            # 迁移旧文件按路径建过行（source_key=legacy:…），重新下载同一片段
            # 会命中两条：保留按业务身份建的行，删掉按路径建的那条，防止重复
            if asset is not None and path_asset is not None and asset.id != path_asset.id:
                db.delete(path_asset)
            elif asset is None:
                asset = path_asset
            if asset is None:
                asset = VideoAsset(lesson_id=lesson_id, source_key=source_key)
                db.add(asset)
            asset.source_key = source_key
            asset.task_id = runtime.task_id
            # 字段长度都按模型列宽在这里截断：引擎给的标题/错误可能超长，
            # 让 SQLite 静默截断会与 Pydantic 出参校验打架，显式裁切更可控
            asset.title = str(video.get("segment_title") or "")[:255]
            asset.record_date = str(video.get("record_date") or "")[:64]
            asset.record_method = str(video.get("record_method") or "")[:64]
            asset.file_path = db_path(str(event.get("path") or ""))[:1024]
            asset.status = status
            asset.percent = max(0, min(100, int(event.get("percent") or 0)))
            asset.error = str(event.get("error") or "")[:1000]
            real_path = fs_path(asset.file_path)
            try:
                asset.size_bytes = os.path.getsize(real_path) if real_path and os.path.isfile(real_path) else 0
            except OSError:
                asset.size_bytes = 0
            db.commit()
    except Exception:
        logger.exception("更新视频台账失败")
        db.rollback()


def scan_existing_video_files(db: Session, root: str) -> dict[str, int]:
    """把服务器已有目录中的视频文件匹配到课堂，供迁移旧下载结果使用。

    匹配策略（精确到宽松，宁可漏配不可错配）：
    1. 目录名末尾的课堂 ID + 一级目录末尾的班级 ID 同时命中（新命名）；
    2. 完整目录名+班级后缀与"按课堂字段重建的期望目录名"一致（新旧都认）；
    3. 旧目录名唯一命中唯一课堂时兜底（班级目录未带 ID 的历史结构）。
    三级都无唯一结果的文件计入 unmatched，由用户人工处理——
    错配的代价是视频挂到别的课堂下，比漏配难发现得多。
    """
    base = os.path.realpath(root)
    if not os.path.isdir(base):
        raise ValidationFailure("视频保存目录不存在，请先配置目录并将旧视频复制到服务器")

    def safe(value: str) -> str:
        """文件名清洗（与引擎命名规则同一套非法字符表）。"""
        return re.sub(r'[\\/*?:"<>|]', "_", (value or "").strip()).rstrip(". ") or "_"

    def start_folder(value: str) -> str:
        """把开始时间转为目录名格式：'2026/9/7 21:46' → '2026-09-07_21-46'。

        日期段补零（9→09）是关键：旧版引擎用补零格式建目录，这里不补
        的话期望目录名和磁盘上的实际目录对不上，整批历史文件失配。
        """
        text = (value or "").replace("/", "-").replace(" ", "_").replace(":", "-")
        pieces = text.split("_")
        if len(pieces) >= 2:
            date_parts = pieces[0].split("-")
            if len(date_parts) == 3:
                pieces[0] = f"{date_parts[0]}-{date_parts[1].zfill(2)}-{date_parts[2].zfill(2)}"
            return "_".join(pieces)
        return safe(value)

    lessons = list(db.scalars(select(Lesson)).all())
    # 两张查找表（值都是 {id: Lesson} 而非单个 Lesson：同名/同目录的
    # 候选有多个时，宁可判为"歧义不匹配"也不能任选其一——这就是用
    # 字典收多重候选而不是直接覆盖的原因）：
    # - expected：(目录名, 班级后缀) → 候选课堂（策略 1/2 精确匹配用）
    # - by_leaf：目录名 → 候选课堂（策略 3 宽松兜底用）
    expected: dict[tuple[str, str], dict[int, Lesson]] = {}
    by_leaf: dict[str, dict[int, Lesson]] = {}
    for lesson in lessons:
        legacy_leaf = f"{start_folder(lesson.start_time)}_{safe(lesson.lesson_name or lesson.lesson_id)}" if lesson.start_time else safe(lesson.lesson_name or lesson.lesson_id)
        id_leaf = f"{legacy_leaf}_{safe(lesson.lesson_id)}"
        legacy_class_suffix = "_" + safe(lesson.class_name or lesson.class_id or "未知班级")
        id_class_suffix = legacy_class_suffix + ("_" + safe(lesson.class_id) if lesson.class_id else "")
        # 同时认新命名（班级/课堂目录追加平台 ID）和旧命名，避免历史文件扫描失配。
        for leaf, class_suffix in {(legacy_leaf, legacy_class_suffix), (id_leaf, id_class_suffix)}:
            expected.setdefault((leaf.casefold(), class_suffix.casefold()), {})[lesson.id] = lesson
        by_leaf.setdefault(legacy_leaf.casefold(), {})[lesson.id] = lesson
        by_leaf.setdefault(id_leaf.casefold(), {})[lesson.id] = lesson

    lesson_by_id = {safe(lesson.lesson_id).casefold(): lesson for lesson in lessons if lesson.lesson_id}
    indexed = existing = unmatched = 0
    for current, _, files in os.walk(base):
        folder = os.path.basename(current).casefold()
        class_folder = os.path.basename(os.path.dirname(current)).casefold()
        for filename in files:
            if not filename.lower().endswith(".mp4"):
                continue
            full_path = os.path.realpath(os.path.join(current, filename))
            try:
                # commonpath 防目录穿越：目录里可能有指向外部位置的软链，
                # 只有解析后仍位于扫描根内的文件才录入台账
                if os.path.normcase(os.path.commonpath([base, full_path])) != os.path.normcase(base):
                    continue
                size = os.path.getsize(full_path)
            except (OSError, ValueError):
                continue  # 文件在遍历间隙被删/无权限：跳过，扫描整体不失败
            # ID 是目录名末尾的稳定键；优先按课堂 ID + 班级 ID 匹配，
            # 课堂名称/日期可能被人工修正，不能因此让旧文件变成“无法匹配”。
            folder_lesson_id = folder.rsplit("_", 1)[-1]
            folder_class_id = class_folder.rsplit("_", 1)[-1]
            candidate = lesson_by_id.get(folder_lesson_id)
            id_matches = [candidate] if (
                candidate and candidate.class_id
                and safe(candidate.class_id).casefold() == folder_class_id
            ) else []
            matches_by_id = {lesson.id: lesson for lesson in id_matches}
            # 兼容旧目录和 ID 不可用的记录，才回退到完整目录名匹配。
            for (leaf, suffix), group in expected.items():
                if not matches_by_id and leaf == folder and class_folder.endswith(suffix):
                    matches_by_id.update(group)
            matches = list(matches_by_id.values())
            if not matches and len(by_leaf.get(folder, {})) == 1:
                # 旧目录中的课程文件夹名若唯一，班级文件夹沿用过旧命名也能安全匹配。
                matches = list(by_leaf[folder].values())
            if len(matches) != 1:
                unmatched += 1
                continue
            match = matches[0]
            # 路径可能含非 UTF-8 代理字节：fsencode 成字节再哈希，避免编码异常
            path_key = hashlib.sha256(fsencode_key("legacy", safe(match.lesson_id), full_path)).hexdigest()
            stored_path = db_path(full_path)
            asset = db.scalars(select(VideoAsset).where(
                VideoAsset.lesson_id == match.lesson_id, VideoAsset.file_path == stored_path,
            )).first()
            if asset is None:
                asset = VideoAsset(lesson_id=match.lesson_id, source_key=path_key)
                db.add(asset)
                indexed += 1
            else:
                existing += 1
            asset.title = display_name(os.path.splitext(filename)[0])[:255]
            asset.file_path = stored_path
            asset.status = "skipped" if size else "failed"
            asset.percent = 100 if size else 0
            asset.size_bytes = size
            asset.error = "" if size else "文件为空"
    db.commit()
    return {"indexed": indexed, "existing": existing, "unmatched": unmatched}


def _on_progress(db: Session, runtime: TaskRuntime, total_courses: int, course_index: int, course: dict | None, video) -> None:
    """进度回调（引擎每 ~0.5s 触发）→ 节流落库，防 SQLite 写放大。

    两层更新策略：
    - 内存快照（runtime.percent/speed）：每次必更新，给轮询读用；
    - DB 落库：0.5s 节流——4 个下载线程并发时回调频率 ×4，
      不节流就是每秒 8 次写事务，WAL 也扛不住这种无谓开销。
    Why 只在 running 时写：paused/stopping 的状态归请求线程管，
      进度回调若覆盖 status 会把"已暂停"改回"运行中"（竞态脏写）。
    """
    try:
        with runtime.lock:
            # video 是 dict 才是真正的下载进度（引擎还有一种传课程下标的调用，
            # 语义是"第 N 课开始"，不含百分比，这里忽略避免算错进度）
            if isinstance(video, dict) and total_courses:
                v_percent = int(video.get("percent") or 0)
                runtime.speed_mbps = float(video.get("speed_mbps") or 0.0)
                runtime.item_percent = v_percent
                v_total = int(video.get("total_videos") or 1) or 1
                v_index = int(video.get("video_index") or 1)
                # items_map 把 lesson_id 直接映射到行 id：回调高频，
                # 每次查库定位 item 会把 SQLite 读写比推高一个量级
                item_id = runtime.items_map.get((course or {}).get("lesson_id", ""))
                if item_id:
                    item = db.get(TaskItem, item_id)
                    if item:
                        item.video_total = v_total
                        item.video_done = v_index
                        # 单课进度 = 已完成视频数 + 当前视频进度的加权
                        item.percent = int(((v_index - 1) + v_percent / 100) * 100 / v_total)
                if course:
                    runtime.current_lesson = course.get("lesson_name") or course.get("class_name") or ""
                # 总进度封顶 99：终态翻转前不允许显示 100%（否则用户以为
                # 结束了去点停止，实际上还在收尾写库）
                runtime.percent = min(99, int((runtime.items_done + (v_percent / 100)) * 100 / max(runtime.total_items, 1)))

            now = time.monotonic()
            if now - runtime.last_flush < 0.5:  # 0.5s 节流：4 线程并发时也只 ~2 写/秒
                return
            runtime.last_flush = now
            task = db.get(DownloadTask, runtime.task_id)
            if task and task.status == "running":  # paused/stopping 由请求线程掌握状态，不覆盖
                task.percent = runtime.percent
                task.speed_mbps = runtime.speed_mbps
                task.current_lesson = runtime.current_lesson
                db.commit()
    except Exception:
        logger.exception("更新进度失败")
        db.rollback()


def _recount(db: Session, runtime: TaskRuntime) -> None:
    """按 items 实况重算任务成功/失败数（幂等，避免增量计数漂移）。"""
    # Why 先 flush：本 session 刚改的 item.status 还没落库，autoflush=False 下
    # COUNT 查询看不到未提交变更，会算出过期的聚合数（差 1）。
    db.flush()
    success = db.scalar(select(func.count()).where(TaskItem.task_id == runtime.task_id, TaskItem.status == "success")) or 0
    failed = db.scalar(select(func.count()).where(TaskItem.task_id == runtime.task_id, TaskItem.status == "failed")) or 0
    task = db.get(DownloadTask, runtime.task_id)
    if task:
        task.success_count = success
        task.fail_count = failed


def _finalize(db: Session, runtime: TaskRuntime, error_msg: str) -> None:
    """worker 收尾：唯一有权写终态的地方（正常/异常/取消三合一）。

    三态判定优先级：stop 信号 > 硬异常 > 成败统计。
    Why stop 最优先：用户点了停止，即使引擎恰好正常跑完，
    "我停了它"才是用户认知的事实，显示"已完成"会造成误解。
    """
    try:
        with runtime.lock:
            task = db.get(DownloadTask, runtime.task_id)
            if not task:
                return
            if runtime.stop_event.is_set():
                status, error = "cancelled", ""
            elif error_msg:
                status, error = "failed", error_msg
            else:
                # 判定规则：至少一课成功即视为任务成功（单课"无视频"不算任务失败），
                # 全军覆没才算 failed；明细在 items/fail_count 中可查。
                status = "success" if (task.success_count or 0) > 0 else "failed"
                error = "" if status == "success" else "所有课堂均未能完成下载，详见任务日志"
            task.status = status
            task.error = error
            task.finished_at = datetime.utcnow()
            task.speed_mbps = 0.0
            task.current_lesson = ""
            # 收尾时把所有还挂在 downloading 的视频台账判失败：
            # 引擎退出后 callback 不会再来了，不在这里清，"下载中"状态
            # 会永远留在视频库里（用户刷新页面看到永不动的进度条）
            incomplete = list(db.scalars(select(VideoAsset).where(
                VideoAsset.task_id == runtime.task_id, VideoAsset.status == "downloading",
            )).all())
            for asset in incomplete:
                asset.status = "failed"
                asset.error = (error or "任务结束时下载未完成")[:1000]
                real_path = fs_path(asset.file_path)
                try:
                    asset.size_bytes = os.path.getsize(real_path) if real_path and os.path.isfile(real_path) else 0
                except OSError:
                    asset.size_bytes = 0
            # 只有成功才置满：失败/取消保留实际进度供排查（进度条停在
            # 中断点比"满格但失败"更能提示用户是中途出的事）
            if status == "success":
                task.percent = 100
            # 终态日志与终态字段同一事务提交：日志时间线里"任务结束"
            # 永远出现在状态翻转的同一刻，排查时不会出现顺序错乱
            db.add(
                DownloadLog(
                    task_id=runtime.task_id,
                    level="INFO" if status == "success" else "ERROR",
                    message=f"任务结束：{_zh(status)}（成功 {task.success_count} / 失败 {task.fail_count}）"
                    + (f"；错误: {error}" if error else ""),
                )
            )
            db.commit()
            logger.info("任务 #%d 结束: %s", runtime.task_id, status)
    except Exception:
        logger.exception("任务收尾写库失败")
        db.rollback()
    finally:
        # 引擎正常路径已 quit；此处兜底异常路径的浏览器泄漏（driver_ref 仍持有）。
        # Why 必须兜底：浏览器进程不退会一直占内存/端口，长跑服务几周就耗尽
        driver = runtime.driver_ref.pop("driver", None)
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def recover_stale_tasks() -> None:
    """进程启动时把上次崩溃残留的 active 状态置为 failed（崩溃恢复）。

    Why 必须做：状态机的权威在 DB，但 worker 是内存线程——断电/强杀后
    DB 里的 running 永远不会翻转，用户会看到"幽灵任务"且无法再启动任何
    任务（被 _runtime 闸门误判）。启动时统一清账是最简单的自愈。
    """
    with SessionLocal() as db:
        stale = list(db.scalars(select(DownloadTask).where(DownloadTask.status.in_(ACTIVE))).all())
        for task in stale:
            # 逐任务清零 + 单次统一 commit：数量少（最多 1 个活跃任务），
            # 简单直白比事务优化更重要
            task.status = "failed"
            task.error = "服务重启导致任务中断，请重新启动"
            task.finished_at = datetime.utcnow()
            for asset in db.scalars(select(VideoAsset).where(
                VideoAsset.task_id == task.id, VideoAsset.status == "downloading",
            )).all():
                asset.status = "failed"
                asset.error = "服务重启导致该视频下载中断"
            db.add(DownloadLog(task_id=task.id, level="ERROR", message="服务重启，任务中断"))
        if stale:
            db.commit()
            logger.warning("已恢复 %d 个中断任务", len(stale))
