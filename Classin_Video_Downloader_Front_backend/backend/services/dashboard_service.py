"""仪表盘统计服务 + 引擎环境自检。

stats() 面向首页（每次登录/刷新调用），所以刻意控制查询次数：
三次 GROUP BY 拿到课堂/任务/视频状态计数 + 一次 LIMIT 取最近任务，共 4 条 SQL。

Why 用 GROUP BY 而非逐状态 COUNT：4 个状态若各查一次就是 4 次
全表扫描；GROUP BY 一次扫描返回所有计数，状态种类将来增加也不
退化（查询数恒为 1）。
"""
from __future__ import annotations

import os
import shutil

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.config import BASE_DIR
from ..db.models import DownloadTask, Lesson, VideoAsset
from ..engine import driver_auto
from ..schemas import EngineStatusOut, StatsOut, TaskOut


class DashboardService:
    """首页聚合查询。无独立 repository：统计 SQL 是"读模型"
    （展示专用、不复用给写路径），塞进 LessonRepo 反而污染仓储职责。"""

    def __init__(self, db: Session):
        self.db = db

    def stats(self) -> StatsOut:
        """单次请求内聚合计数：分状态 GROUP BY 汇总，避免逐状态 COUNT。

        why_tasks_running 把 paused 并进 running：对用户而言
        "暂停中"仍是"这个任务还没完"，卡片真实反映待完成量
        （若分开显示，一个暂停任务会让两个卡片同时为 0，产生
        "明明有任务却没显示"的困惑）。
        """
        status_rows = self.db.execute(select(Lesson.status, func.count()).group_by(Lesson.status)).all()
        by_status = dict(status_rows)
        task_rows = self.db.execute(
            select(DownloadTask.status, func.count()).group_by(DownloadTask.status)  # type: ignore[arg-type]
        ).all()
        task_by_status = dict(task_rows)
        recent = self.db.scalars(select(DownloadTask).order_by(DownloadTask.id.desc()).limit(5)).all()
        video_rows = self.db.execute(select(VideoAsset.status, func.count()).group_by(VideoAsset.status)).all()
        video_by_status = dict(video_rows)
        return StatsOut(
            lessons_total=sum(by_status.values()),
            lessons_pending=by_status.get("pending", 0),
            lessons_success=by_status.get("success", 0),
            lessons_failed=by_status.get("failed", 0),
            tasks_total=sum(task_by_status.values()),
            tasks_running=task_by_status.get("running", 0) + task_by_status.get("paused", 0),
            videos_total=sum(video_by_status.values()),
            videos_downloaded=video_by_status.get("success", 0) + video_by_status.get("skipped", 0),
            videos_failed=video_by_status.get("failed", 0),
            videos_downloading=video_by_status.get("downloading", 0),
            recent_tasks=[TaskOut.model_validate(t) for t in recent],
        )


def engine_status() -> EngineStatusOut:
    """探测浏览器与驱动可用性（供设置页展示"环境是否就绪"）。

    探测策略是"零副作用"：只查路径与注册表，不启动浏览器——
    设置页可能频繁点"检测"，每次都拉起浏览器既慢又弹窗。
    Why 不校验驱动与浏览器版本匹配：版本号解析规则因渠道
    （稳定版/Beta/企业策略推送）而异，误报"不兼容"比漏报更伤
    （用户会去重装能用的环境）；真不匹配在启动任务时报错更准。
    """
    browser, browser_path = driver_auto.detect_browser_preference()
    # 驱动查找顺序：用户指定路径 > 项目根目录随附的 msedgedriver.exe
    candidates = [
        os.environ.get("CLASSIN_DRIVER_PATH", ""),
        str(BASE_DIR / "msedgedriver.exe"),
        str(BASE_DIR / "chromedriver.exe"),
    ]
    found = next((p for p in candidates if p and os.path.exists(p)), "")
    if not found:
        # 驱动不在项目内时尝试 PATH（webdriver_manager 缓存目录不在此层）
        for name in ("msedgedriver", "chromedriver"):
            w = shutil.which(name)
            if w:
                found = w
                break
    return EngineStatusOut(
        browser=browser or "",
        browser_path=browser_path or "",
        driver_path=found,
        # driver_ready 只代表"离线可用"；缺失时引擎会自动下载（见 driver_auto.ensure_driver）
        driver_ready=bool(browser and found),
    )
