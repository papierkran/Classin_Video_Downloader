"""控制台同步路由：班级/课程 + 课堂镜像入库，以及课堂级/片段级下载对比。

与 /api/sync 的分工：
- /api/sync 是"按钮式补新课"（同步新增→待下载列表→建任务），保留不动；
- /api/console 是"镜像控制台两个页面 + 对比本地已下载情况"，给
  「同步管理（班级）」与「课堂管理（课堂）」两个页面用。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..db.models import User
from ..schemas import ConsoleSyncRunOut, CourseOut, LessonSegmentsOut, PageOut, TaskOut
from ..services.course_service import CourseService
from .deps import current_user, db_session, require_admin

router = APIRouter(prefix="/api/console", tags=["控制台同步"])


@router.post("/sync", response_model=ConsoleSyncRunOut)
def run_console_sync(_: User = Depends(require_admin), db: Session = Depends(db_session)):
    """立即同步：拉取班级列表 + 课堂列表并幂等入库（真实访问控制台接口）。

    与 /api/sync/run 共用进程级同步闸门，撞上并发直接 422（见 service）。
    """
    return CourseService(db).run_sync()


@router.get("/courses", response_model=PageOut)
def list_courses(
    keyword: str = "",
    state: str = "",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    _: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    """班级/课程列表（courses 表，来自网站同步）。"""
    total, rows = CourseService(db).list_courses(page, page_size, keyword=keyword, state=state)
    return PageOut(total=total, page=page, page_size=page_size, items=[CourseOut.model_validate(r) for r in rows])


@router.get("/lessons", response_model=PageOut)
def list_console_lessons(
    keyword: str = "",
    download_state: str = "",
    class_id: str = "",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    _: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    """课堂列表 + 本地下载对比字段。class_id 供班级展开（取该班全部课堂）。"""
    total, items = CourseService(db).list_lessons(page, page_size, keyword=keyword, download_state=download_state, class_id=class_id)
    return PageOut(total=total, page=page, page_size=page_size, items=[i.model_dump() for i in items])


@router.post("/courses/{course_id}/download", response_model=TaskOut)
def download_course(course_id: str, user: User = Depends(require_admin), db: Session = Depends(db_session)):
    """一键下载某班级下的全部课堂（建任务并启动，复用任务状态机）。"""
    return TaskOut.model_validate(CourseService(db).create_course_task(course_id, user_id=user.id))


@router.get("/lessons/{lesson_id}/segments", response_model=LessonSegmentsOut)
def lesson_segments(lesson_id: int, _: User = Depends(current_user), db: Session = Depends(db_session)):
    """单课堂片段级对比：网站回放 FileList ↔ 本地 VideoAsset（按展开按需拉取）。"""
    return CourseService(db).lesson_segments(lesson_id)
