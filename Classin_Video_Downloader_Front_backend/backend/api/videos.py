"""视频台账、失败重试和受控媒体播放。

安全模型（媒体端点没有登录 Bearer 也放行，规则单列在此）：
- 页面不直接暴露 file_path：先请求 ticket 端点换取 15 分钟、绑定单个
  视频 id 的 HttpOnly Cookie，媒体端点校验 Cookie 后才下发字节；
- Cookie 的 path 精确到 /api/videos/{id}/media——同一浏览器里打开
  另一个视频的 URL 也带不出上一条的凭证；
- 直链被转发出去最多 15 分钟内有效，URL 本身不含任何令牌（不会进
  Referer/代理日志）。

检索模型（Why 列表都从 Lesson 起步而不是 VideoAsset）：
- 用户心智是"哪个班 → 哪节课 → 几段视频"；以 Lesson 为主表左连
  VideoAsset，没有片段的课堂也会出现（video_count=0），不会出现
  "明明有课，视频库里却查不到"的反直觉。
"""
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
    """扫描保存目录，把已有 MP4 补录进台账（迁移旧下载的历史数据用）。

    Why 与下载任务互斥：扫描和引擎回调都会写 VideoAsset，SQLite 单写者
    下并行只会退化为锁等待；直接拒绝报错，用户明白"等任务跑完再扫"。
    """
    if db.scalars(select(DownloadTask).where(DownloadTask.status.in_(ACTIVE))).first():
        raise ConflictError("下载任务运行期间不能扫描文件，请任务结束后再试")
    saved = db.get(AppSetting, 1)
    # 扫描根固定取"默认下载根目录"：任务可以自定义目录，但迁移场景
    # 只会把旧文件整目录复制到默认位置，扫描范围收窄才不会误扫无关目录。
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
    """视频库一级列表：按班级汇总课堂数、片段数、状态和总大小。

    分组键（class_group）：有平台班级 ID 用 ID；手工录入没有 ID 的
    退化为 "name:班级名"。Why 不一概用名称：名称可被人工修正，
    有 ID 时用 ID 做键，改名不会把同一班拆成两行。
    """
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

    # 总数不能用 len(rows)：班级分组是聚合结果，这里把同一 stmt 收窄成
    # "只取分组键"的子查询再 count——条件一致，总数与翻页口径永远对齐。
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
    """班级展开层：逐课堂汇总片段（一级列表里点击某班级展开时调用）。

    两个查询分支对应一级列表的两种分组键：ID 班级过滤 class_id，
    "name:" 班级过滤"无 ID 且名称相等"的课堂——两级列表用同一套
    键规则，展开结果不会串到同名班级。
    """
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
    """视频片段分页（课堂展开层与全局搜索共用）。

    LEFT JOIN Lesson 取课堂/班级名：视频台账只存 lesson_id，名称在
    课堂表；outerjoin 保证课堂被删后残留的片段仍能列出（名称缺省为空串），
    不然一条脏数据会让整页查询丢行。
    """
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
    # count 复用同一 stmt（with_only_columns 换掉查询列、order_by(None) 去掉
    # 排序），保证"共 N 条"与实际翻页数据同条件；否则加了筛选后会出现
    # 总数对不上页数（翻到最后一页是空的）。
    total = db.scalar(stmt.with_only_columns(func.count(VideoAsset.id)).order_by(None)) or 0
    # 排序给 updated_at 加 id 二级键：批量写入的同一秒内次序不稳定，
    # 没有次级键翻页会出现重复行（用户看到同一条视频出现在两页）
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
    """签发单视频短时票据（HttpOnly Cookie，15 分钟）。

    Why 用 Cookie 而不是响应体里的裸 token：<video src> 原生播放器
    不会带 Authorization 头；token 拼进 URL 又会通过 Referer/代理日志
    泄漏。HttpOnly Cookie 对播放器透明，同时挡掉 XSS 读取。
    secure 标志跟随当前协议（含反代场景的 X-Forwarded-Proto）：
    HTTPS 部署自动开启，本地 http 调试不误伤。
    """
    asset = db.get(VideoAsset, video_id)
    # 只放行"确实有文件"的记录：状态没到 success/skipped 或文件被外部
    # 删除时，早早 404 比发一个必然会 404 的票据更省一次往返。
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
    """按 HTTP Range 流式下发视频（播放与下载共用一个端点）。

    鉴权只认先前签发的票据 Cookie（无 Bearer）：媒体的典型消费者是
    <video> 标签与浏览器下载器，它们都不会带自定义请求头。票据解码时
    校验 video_id 绑定——把 A 视频的票据拼到 B 的 URL 上会直接 401。
    Range 支持三种形态：bytes=a-b（闭区间）、bytes=a-（到结尾）、
    bytes=-N（末尾 N 字节）；逗号多段不支持（播放器/下载器都用单段，
    支持它要拼 multipart 响应，复杂度不成比例）。
    """
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
    # no-store：票据是短时的，媒体响应也不该被中间代理缓存后绕过鉴权。
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
            # 非法 Range 按 RFC 7233 回 416 + "bytes */总长"：
            # 播放器据此知道文件真实大小并重发正确 Range，直接 400 会让它卡死
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"})
        # 有效 Range → 206 + Content-Range + 重算的 Content-Length（缺后两者
        # 浏览器进度条按整个文件算，长度和实际下载字节数对不上）
        status_code = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        headers["Content-Length"] = str(end - start + 1)

    def chunks():
        # 1MB 分块生成器：大课堂视频几百 MB，一次性 read() 会把整个文件
        # 读进内存；流式 yield 让内存占用与文件大小无关，也支持断开即停。
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
    """为失败视频所属课堂建"重试任务"并直接启动。

    Why 整课重试而不是单片段重试：引擎的最小抓取单位是课堂（一次打开
    页面列出该课全部片段），做片段级重试要在引擎里开洞；配合
    skip_existing，重跑不会重复下载已完成的片段，代价只是多列一遍清单。
    目录沿用该视频上次任务（previous_task）的保存位置：用户当初把任务
    放到了别的盘，重试文件也该回原来的地方。
    """
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
