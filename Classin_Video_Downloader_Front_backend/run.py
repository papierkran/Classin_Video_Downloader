"""启动入口：python run.py

Why 不直接 uvicorn 命令行：命令行需要 cd 到项目根才能找到 backend 包，
这个脚本自动把 cwd/sys.path 修正到项目根，任何目录下双击/命令都能起。
"""
import sys
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.config import settings  # noqa: E402  路径就绪后再导入

if __name__ == "__main__":
    uvicorn.run(
        "backend.main:app",
        host=settings.host,
        port=settings.port,
        # reload 开发热更新会起两个进程，后台任务线程状态会分裂，
        # 所以默认关闭；需要热更新时：CLASSIN_RELOAD=1 python run.py
        reload=bool(__import__("os").environ.get("CLASSIN_RELOAD")),
    )
