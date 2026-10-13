"""视频台账、失败重试和受控媒体播放。"""
from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import case, func, literal, or_, select
from sqlalchemy.orm import Session

from ..core.exceptions import AuthError, ConflictError, NotFoundError
from ..core.config import settings
from ..core.security import create_media_token, decode_media_token
from ..db.models import AppSetting, DownloadTask, Lesson, User, VideoAsset
from ..schemas import MediaTicketOut, PageOut, TaskCreateIn, TaskOut, VideoOut
from ..services.task_service import ACTIVE, TaskService, scan_existing_video_files
from .deps import current_user, db_session

router = APIRouter(prefix="/api/videos", tags=["视频库"])


@router.post("/scan")
def scan_video_files(_: User = Depends(current_user), db: Session = Depends(db_session)):
    if db.scalars(select(DownloadTask).where(DownloadTask.status.in_(ACTIVE))).first():
        raise ConflictError("下载任务运行期间不能扫描文件，请任务结束后再试")
    saved = db.get(AppSetting, 1)
    root = saved.save_dir if saved and saved.save_dir else settings.default_save_dir
    result = scan_existing_video_files(db, root)
    return {**result, "message": "扫描完成"}


@router.get("/classes")
def list_video_classes(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    keyword: str = "",
    _: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    """视频库一级列表：按班级汇总课堂数、片段数、状态和总大小。"""
    class_group = case(
        (Lesson.class_id.is_not(None) & (Lesson.class_id != ""), Lesson.class_id),
        else_=(literal("name:") + func.coalesce(Lesson.class_name, "未命名班级")),
    )
    stmt = select(
        func.max(Lesson.class_id).label("class_id"),
        func.max(Lesson.class_name).label("class_name"),
        func.count(func.distinct(Lesson.id)).label("lesson_count"),
        func.count(VideoAsset.id).label("video_count"),
        func.coalesce(func.sum(case((VideoAsset.status.in_(("success", "skipped")), 1), else_=0)), 0).label("ready_count"),
        func.coalesce(func.sum(case((VideoAsset.status == "failed", 1), else_=0)), 0).label("failed_count"),
        func.coalesce(func.sum(VideoAsset.size_bytes), 0).label("size_bytes"),
    ).outerjoin(VideoAsset, VideoAsset.lesson_id == Lesson.lesson_id).group_by(class_group)
    if keyword.strip():
        pattern = f"%{keyword.strip()}%"
        stmt = stmt.where(or_(Lesson.class_name.ilike(pattern), Lesson.class_id.ilike(pattern), Lesson.lesson_name.ilike(pattern), Lesson.lesson_id.ilike(pattern)))

    grouped = stmt.with_only_columns(class_group).group_by(class_group).subquery()
    total = db.scalar(select(func.count()).select_from(grouped)) or 0
    rows = db.execute(stmt.order_by(func.max(Lesson.class_name), func.max(Lesson.class_id)).offset((page - 1) * page_size).limit(page_size)).all()
    items = []
    for row in rows:
        class_id, class_name, lesson_count, video_count, ready_count, failed_count, size_bytes = row
        class_id = class_id or ""
        class_name = class_name or "未命名班级"
        items.append({
            "class_key": class_id or f"name:{class_name}", "class_id": class_id, "class_name": class_name,
            "lesson_count": int(lesson_count or 0), "video_count": int(video_count or 0),
            "ready_count": int(ready_count or 0), "failed_count": int(failed_count or 0),
            "size_bytes": int(size_bytes or 0),
        })
    return {"total": total, "page": page, "page_size": page_size, "items": items}


@router.get("/classes/{class_key}/lessons")
def list_class_video_lessons(
    class_key: str,
    _: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    """视频库班级展开层：逐课堂汇总已收录的视频片段。"""
    if class_key.startswith("name:"):
        class_name = class_key[5:]
        class_filter = or_(Lesson.class_id.is_(None), Lesson.class_id == "")
        stmt = select(Lesson, func.count(VideoAsset.id),
                      func.coalesce(func.sum(case((VideoAsset.status.in_(("success", "skipped")), 1), else_=0)), 0),
                      func.coalesce(func.sum(case((VideoAsset.status == "failed", 1), else_=0)), 0),
                      func.coalesce(func.sum(VideoAsset.size_bytes), 0)) \
            .outerjoin(VideoAsset, VideoAsset.lesson_id == Lesson.lesson_id).where(class_filter, Lesson.class_name == class_name)
    else:
        stmt = select(Lesson, func.count(VideoAsset.id),
                      func.coalesce(func.sum(case((VideoAsset.status.in_(("success", "skipped")), 1), else_=0)), 0),
                      func.coalesce(func.sum(case((VideoAsset.status == "failed", 1), else_=0)), 0),
                      func.coalesce(func.sum(VideoAsset.size_bytes), 0)) \
            .outerjoin(VideoAsset, VideoAsset.lesson_id == Lesson.lesson_id).where(Lesson.class_id == class_key)
    rows = db.execute(stmt.group_by(Lesson.id).order_by(Lesson.start_time.desc(), Lesson.lesson_name)).all()
    return [{
        "id": lesson.id, "lesson_id": lesson.lesson_id, "lesson_name": lesson.lesson_name or "未命名课堂",
        "start_time": lesson.start_time or "", "video_count": int(video_count or 0),
        "ready_count": int(ready_count or 0), "failed_count": int(failed_count or 0),
        "size_bytes": int(size_bytes or 0),
    } for lesson, video_count, ready_count, failed_count, size_bytes in rows]


@router.get("", response_model=PageOut)
def list_videos(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    status: str = "",
    keyword: str = "",
    lesson_id: str = "",
    _: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    stmt = select(VideoAsset, Lesson.lesson_name, Lesson.class_name).outerjoin(
        Lesson, Lesson.lesson_id == VideoAsset.lesson_id
    )
    if lesson_id:
        stmt = stmt.where(VideoAsset.lesson_id == lesson_id)
    if status:
        stmt = stmt.where(VideoAsset.status == status)
    if keyword:
        pattern = f"%{keyword.strip()}%"
        stmt = stmt.where(or_(
            VideoAsset.title.ilike(pattern), VideoAsset.lesson_id.ilike(pattern),
            Lesson.lesson_name.ilike(pattern), Lesson.class_name.ilike(pattern),
        ))
    total = db.scalar(stmt.with_only_columns(func.count(VideoAsset.id)).order_by(None)) or 0
    rows = db.execute(stmt.order_by(VideoAsset.updated_at.desc(), VideoAsset.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    items = [
        VideoOut(
            id=asset.id, lesson_id=asset.lesson_id, lesson_name=lesson_name or "", class_name=class_name or "",
            title=asset.title, record_date=asset.record_date, record_method=asset.record_method,
            status=asset.status, percent=asset.percent, size_bytes=asset.size_bytes,
            error=asset.error, updated_at=asset.updated_at,
        )
        for asset, lesson_name, class_name in rows
    ]
    return PageOut(total=total, page=page, page_size=page_size, items=items)


@router.post("/{video_id}/ticket", response_model=MediaTicketOut)
def create_play_ticket(video_id: int, request: Request, response: Response, _: User = Depends(current_user), db: Session = Depends(db_session)):
    asset = db.get(VideoAsset, video_id)
    if not asset or asset.status not in ("success", "skipped") or not asset.file_path or not os.path.isfile(asset.file_path):
        raise NotFoundError("视频文件不存在或尚未下载完成")
    token = create_media_token(video_id)
    response.set_cookie(
        key=f"video_ticket_{video_id}", value=token, max_age=900, httponly=True,
        secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https",
        samesite="strict", path=f"/api/videos/{video_id}/media",
    )
    return MediaTicketOut(url=f"/api/videos/{video_id}/media", expires_in=900)


@router.get("/{video_id}/media")
def stream_video(video_id: int, request: Request, download: bool = False, db: Session = Depends(db_session)):
    ticket = request.cookies.get(f"video_ticket_{video_id}")
    if not ticket:
        raise AuthError("视频访问凭证缺失，请重新打开视频")
    decode_media_token(ticket, video_id)
    asset = db.get(VideoAsset, video_id)
    if not asset or asset.status not in ("success", "skipped") or not asset.file_path:
        raise NotFoundError("视频文件不存在或尚未下载完成")
    path = Path(asset.file_path).resolve()
    if not path.is_file():
        raise NotFoundError("视频文件已从服务器移除")
    size = path.stat().st_size
    if size <= 0:
        raise NotFoundError("视频文件为空")

    range_header = request.headers.get("range")
    start, end, status_code = 0, size - 1, 200
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(size), "Cache-Control": "private, no-store"}
    if range_header:
        try:
            unit, value = range_header.split("=", 1)
            if unit != "bytes" or "," in value:
                raise ValueError
            first, last = value.split("-", 1)
            if first:
                start = int(first)
                end = min(int(last), size - 1) if last else size - 1
            else:
                suffix = int(last)
                start = max(0, size - suffix)
            if start < 0 or start >= size or end < start:
                raise ValueError
        except (ValueError, TypeError):
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"})
        status_code = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        headers["Content-Length"] = str(end - start + 1)

    def chunks():
        with path.open("rb") as stream:
            stream.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    media_type = mimetypes.guess_type(path.name)[0] or "video/mp4"
    disposition = "attachment" if download else "inline"
    headers["Content-Disposition"] = f"{disposition}; filename*=UTF-8''{quote(path.name)}"
    return StreamingResponse(chunks(), status_code=status_code, media_type=media_type, headers=headers)


@router.post("/{video_id}/retry", response_model=TaskOut)
def retry_video(video_id: int, _: User = Depends(current_user), db: Session = Depends(db_session)):
    asset = db.get(VideoAsset, video_id)
    if not asset:
        raise NotFoundError("视频记录不存在")
    lesson = db.scalars(select(Lesson).where(Lesson.lesson_id == asset.lesson_id)).first()
    if not lesson:
        raise NotFoundError("对应课堂已从课堂库删除，无法重试")
    if db.scalars(select(DownloadTask).where(DownloadTask.status.in_(ACTIVE))).first():
        raise ConflictError("已有下载任务运行，请结束后再重试")
    previous_task = db.get(DownloadTask, asset.task_id) if asset.task_id else None
    saved = db.get(AppSetting, 1)
    if not saved or not saved.cookie.strip():
        raise ConflictError("尚未配置 ClassIn Cookie，请先到系统设置完成配置")
    task = TaskService(db).create(
        TaskCreateIn(
            name=f"重试：{lesson.lesson_name or lesson.lesson_id}",
            lesson_ids=[lesson.id],
            save_dir=previous_task.save_dir if previous_task else (saved.save_dir if saved else None),
        ),
    )
    return TaskOut.model_validate(TaskService(db).start(task.id))
