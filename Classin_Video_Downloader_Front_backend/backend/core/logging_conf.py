"""日志配置。

Why 单独模块：uvicorn/fastapi/app 三处 logger 需要同一套格式，
否则排查问题时时间戳格式不一致、无法按 request_id 串联。
输出到 data/app.log 便于公网部署后回溯（用户只看到 request_id）。
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import DATA_DIR


def setup_logging(level: int = logging.INFO) -> None:
    """装全局 handler（lifespan 里调用一次）。幂等靠 handlers.clear()。"""
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    # 控制台：开发时看；文件：部署后回溯。Why 轮转 5MB×3：SQLite 之外
    # 日志是最容易吃满磁盘的东西，公网长跑必须限制（不轮转的单机服务
    # 半年后 app.log 占满 C 盘是常见事故）。
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(DATA_DIR / "app.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    # Why 文件 handler 用 utf-8 显式声明：Windows 默认 gbk，中文日志
    # 在别的机器上按 gbk 读会乱码，跨环境排障时这是隐形障碍

    root = logging.getLogger()
    root.setLevel(level)
    # Why 先清空再加：uvicorn 可能已挂 handler，重复添加会导致日志翻倍
    # （开发期热重启时尤其明显——同一条消息出现 3 遍）。
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)

    # 降噪：selenium/webdriver_manager 的 DEBUG 对用户无意义
    # （还会把 Cookie 等敏感串打进日志——第三方库的 DEBUG 不受我们控制）
    logging.getLogger("selenium").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("WDM").setLevel(logging.WARNING)
