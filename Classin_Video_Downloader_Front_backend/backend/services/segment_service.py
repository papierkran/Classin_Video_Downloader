"""单片段重试：不经过 Selenium/任务队列，直接按控制台直链补下单个失败片段。

Why 独立于引擎任务：
- 引擎的最小抓取单位是"课堂"（getClassVodList 一次列整课），整课重试会
  把已成功的片段再列一遍、且必须排队等前一个任务结束；
- 单片段只需要一个 FileList 直链 + requests 断点续传，没必要启动浏览器。

并发：允许不同片段并行重试，用信号量限制同时下载数，避免把带宽打满。
"""
from __future__ import annotations

import logging
import os
import threading
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.exceptions import ConflictError, NotFoundError, ValidationFailure
from ..core.fsutil import fs_path
from ..db.database import SessionLocal
from ..db.models import AppSetting, Lesson, VideoAsset
from ..engine.videodownloader import download_file, parse_cookie_string
from .course_service import fetch_class_vod
from .task_service import _file_to_video, _video_source_key

logger = logging.getLogger("app.segment")

_inflight: set[int] = set()
_inflight_lock = threading.Lock()
_slots = threading.Semaphore(3)  # 同时最多 3 个单片段重试


def _match_site_file(files: list[dict], asset: VideoAsset, lesson_id: str) -> tuple[dict, int] | None:
    """找到台账行对应的网站 FileList 项：优先业务身份(source_key)，退化为大小匹配。"""
    for idx, item in enumerate(files, start=1):
        video = _file_to_video(item, idx)
        if video and _video_source_key(lesson_id, video) == asset.source_key:
            return video, _size_of(item)
    # 兼容历史行：source_key 可能来自旧版页面字段；按大小（±1%）唯一命中兜底
    if asset.size_bytes:
        candidates = []
        for idx, item in enumerate(files, start=1):
            video = _file_to_video(item, idx)
            if not video:
                continue
            size = _size_of(item)
            if abs(size - asset.size_bytes) <= max(1, asset.size_bytes // 100):
                candidates.append((video, size))
        if len(candidates) == 1:
            return candidates[0]
    return None


def _size_of(item: dict) -> int:
    try:
        return int(item.get("Size") or 0)
    except (TypeError, ValueError):
        return 0


def start_segment_retry(db: Session, video_id: int) -> dict:
    """校验并把单个片段放进后台线程下载，立即返回。"""
    asset = db.get(VideoAsset, video_id)
    if not asset:
        raise NotFoundError("视频片段不存在")
    if asset.status == "downloading":
        raise ConflictError("该片段正在下载中")
    lesson = db.scalar(select(Lesson).where(Lesson.lesson_id == asset.lesson_id))
    if not lesson:
        raise NotFoundError("片段所属课堂不存在")
    if not lesson.class_id:
        raise ValidationFailure("该课堂缺少班级 ID，无法从网站获取片段")
    row = db.get(AppSetting, 1)
    cookie = (row.cookie or "").strip() if row else ""
    if not cookie:
        raise ValidationFailure("尚未配置 Cookie，请到【系统设置】填写并验证")
    save_path = fs_path(asset.file_path)
    if not save_path:
        raise ValidationFailure("该片段没有可用的本地路径，请改用整课重试")

    _total, files = fetch_class_vod(cookie, lesson.class_id, lesson.lesson_id)
    matched = _match_site_file(files, asset, lesson.lesson_id)
    if not matched:
        raise ValidationFailure("未在网站找到对应片段（可能已删除或转码中），请改用整课重试")
    video, expected_size = matched

    with _inflight_lock:
        if video_id in _inflight:
            raise ConflictError("该片段正在重试中")
        _inflight.add(video_id)
    try:
        cookies = parse_cookie_string(cookie)
        threading.Thread(
            target=_download_segment,
            args=(video_id, video["download_url"], save_path, expected_size, cookies),
            daemon=True,
            name=f"seg-retry-{video_id}",
        ).start()
    except Exception:
        with _inflight_lock:
            _inflight.discard(video_id)
        raise
    return {"video_id": video_id, "title": asset.title or video["segment_title"], "status": "started"}


def _download_segment(video_id: int, url: str, save_path: str, expected_size: int, cookies: list[dict]) -> None:
    """后台线程：下载单个片段并回写台账状态/进度。"""
    db = SessionLocal()
    last_flush = [0.0]

    def flush(percent: int) -> None:
        now = time.monotonic()
        if now - last_flush[0] < 1.0 and percent < 100:
            return
        last_flush[0] = now
        try:
            row = db.get(VideoAsset, video_id)
            if row:
                row.percent = max(0, min(100, int(percent)))
                db.commit()
        except Exception:
            db.rollback()

    try:
        asset = db.get(VideoAsset, video_id)
        if not asset:
            return
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        # 文件其实已完整（上次误判失败）：直接置成功，不再重复下载
        if os.path.isfile(save_path) and expected_size > 0:
            existing = os.path.getsize(save_path)
            if abs(existing - expected_size) <= max(1, expected_size // 100):
                asset.status = "success"
                asset.percent = 100
                asset.size_bytes = existing
                asset.error = ""
                db.commit()
                return
        asset.status = "downloading"
        asset.percent = 0
        asset.error = ""
        db.commit()

        def progress_callback(percent, speed_mbps, info):
            flush(int(percent or 0))

        with _slots:
            ok = download_file(url, save_path, cookies=cookies, progress_callback=progress_callback, resume=True, timeout=60)

        asset = db.get(VideoAsset, video_id)
        if not asset:
            return
        if ok:
            asset.status = "success"
            asset.percent = 100
            asset.error = ""
            try:
                asset.size_bytes = os.path.getsize(save_path)
            except OSError:
                pass
        else:
            asset.status = "failed"
            asset.error = "单片段重试失败"
        db.commit()
    except Exception:
        logger.exception("单片段重试异常 #%s", video_id)
        try:
            asset = db.get(VideoAsset, video_id)
            if asset:
                asset.status = "failed"
                asset.error = "单片段重试异常"
                db.commit()
        except Exception:
            db.rollback()
    finally:
        with _inflight_lock:
            _inflight.discard(video_id)
        db.close()
