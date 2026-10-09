"""各领域仓储实现（薄：查询条件而已，逻辑归 service）。"""
from __future__ import annotations

from sqlalchemy import select

from ..db.models import AppSetting, DownloadLog, DownloadTask, Lesson, TaskItem, User
from .base import BaseRepository


class UserRepo(BaseRepository[User]):
    model = User

    def get_by_username(self, username: str) -> User | None:
        """按用户名查（username 有唯一索引，查询走索引不扫表）。"""
        return self.db.scalars(select(User).where(User.username == username)).first()


class LessonRepo(BaseRepository[Lesson]):
    model = Lesson

    def get_by_lesson_id(self, lesson_id: str) -> Lesson | None:
        """lesson_id 是平台侧业务主键（唯一索引），导入 upsert 的查找依据。"""
        return self.db.scalars(select(Lesson).where(Lesson.lesson_id == lesson_id)).first()

    def filtered(self, status: str = "", keyword: str = ""):
        """列表查询条件（状态精确匹配 + 名称模糊匹配）。

        Why OR 拼在一个 where 而非分开查：模糊搜索场景下用户预期是
        "任一字段命中即返回"，拆开查询会让结果去重变复杂。
        """
        stmt = select(Lesson)
        if status:
            stmt = stmt.where(Lesson.status == status)
        if keyword:
            like = f"%{keyword}%"
            stmt = stmt.where(Lesson.lesson_name.like(like) | Lesson.class_name.like(like) | Lesson.lesson_id.like(like))
        # 默认按开课时间倒序：用户最关心最近的课
        return stmt.order_by(Lesson.start_time.desc(), Lesson.id.desc())

    def list_by_ids(self, ids: list[int]) -> list[Lesson]:
        """IN 查询一次取回（Why 不循环 get：N 次往返即使走索引也慢）。"""
        return list(self.db.scalars(select(Lesson).where(Lesson.id.in_(ids))).all())

    def list_all(self) -> list[Lesson]:
        return list(self.db.scalars(select(Lesson).order_by(Lesson.start_time.desc())).all())


class TaskRepo(BaseRepository[DownloadTask]):
    model = DownloadTask

    def filtered(self, status: str = ""):
        stmt = select(DownloadTask)
        if status:
            stmt = stmt.where(DownloadTask.status == status)
        return stmt.order_by(DownloadTask.id.desc())

    def latest_active(self) -> DownloadTask | None:
        """查找运行中的任务（全局并发闸门的查询依据）。

        保留此查询给"重启后核对"场景：内存 _runtime 为空但 DB 显示
        running 的脏数据，由 recover_stale_tasks 统一清理。
        """
        return self.db.scalars(
            select(DownloadTask).where(DownloadTask.status.in_(("running", "paused", "stopping"))).order_by(DownloadTask.id.desc())
        ).first()


class TaskItemRepo(BaseRepository[TaskItem]):
    model = TaskItem

    def list_by_task(self, task_id: int) -> list[TaskItem]:
        """按任务取明细（id 升序=创建顺序，与前端表格展示顺序一致）。"""
        return list(self.db.scalars(select(TaskItem).where(TaskItem.task_id == task_id).order_by(TaskItem.id)).all())

    def get_by_task_lesson(self, task_id: int, lesson_id: str) -> TaskItem | None:
        """组合定位（uq_task_lesson 唯一约束保证最多一行）。"""
        return self.db.scalars(select(TaskItem).where(TaskItem.task_id == task_id, TaskItem.lesson_id == lesson_id)).first()


class LogRepo(BaseRepository[DownloadLog]):
    model = DownloadLog

    def list_after(self, task_id: int, after_id: int, limit: int = 200) -> list[DownloadLog]:
        """增量拉取（Why after_id 而不是 offset：日志持续追加，
        offset 分页会在新增日志时产生重复/漏读）。

        id > after_id 走主键区间，配合 (task_id, id) 的查询顺序
        可以被 SQLite 用索引覆盖，日志上万行也快。
        """
        stmt = (
            select(DownloadLog)
            .where(DownloadLog.task_id == task_id, DownloadLog.id > after_id)
            .order_by(DownloadLog.id)
            .limit(limit)
        )
        return list(self.db.scalars(stmt).all())


class SettingRepo(BaseRepository[AppSetting]):
    model = AppSetting

    def get_singleton(self) -> AppSetting | None:
        """单行配置（id 恒为 1）。返回 None 说明 init_db 未跑过——
        service 层会转成可读错误而不是 NoneType 崩溃。"""
        return self.db.get(AppSetting, 1)
