"""应用配置模块（Why 单独抽一层？）。

配置是"读一次就不该再变"的环境信息，散落在业务代码里会导致：
1) 测试/部署要改多处；2) 密钥路径等敏感信息难以统一脱敏。
这里用 frozen dataclass 收敛所有配置，并支持环境变量覆盖，
使本地开发（127.0.0.1）与公网部署（0.0.0.0 + 反代）零改码切换。
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

# 项目根目录：backend/core/config.py 上溯两级 = 项目根
# Why：所有相对路径（data/、frontend/）都锚定根目录，避免依赖启动 cwd。
BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("CLASSIN_DATA_DIR", BASE_DIR / "data"))
FRONTEND_DIR = BASE_DIR / "frontend"


def _load_or_create_secret() -> str:
    """加载/生成 JWT 签名密钥并落盘。

    Why 落盘而不是启动时随机生成：重启后随机密钥会使所有在线用户的
    token 立即失效，2-3 人公网访问时表现为"莫名其妙掉线"。
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    key_file = DATA_DIR / "secret.key"
    if key_file.exists():
        key = key_file.read_text(encoding="utf-8").strip()
        if key:
            return key
    key = secrets.token_hex(32)
    key_file.write_text(key, encoding="utf-8")
    try:  # 密钥文件收紧权限（Windows 下可能不支持，忽略即可）
        os.chmod(key_file, 0o600)
    except OSError:
        pass
    return key


@dataclass(frozen=True)
class Settings:
    """运行时配置（环境变量优先，缺省回退到本地友好默认值）。"""

    # 服务监听：默认只绑本机，公网部署必须显式 CLASSIN_HOST=0.0.0.0。
    # Why 默认关：公网可直接访问的后台必须"显式开启"而非"默认开放"。
    host: str = field(default_factory=lambda: os.environ.get("CLASSIN_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.environ.get("CLASSIN_PORT", "8000")))

    db_path: Path = field(default_factory=lambda: DATA_DIR / "app.db")
    secret_key: str = field(default_factory=_load_or_create_secret)
    token_ttl_seconds: int = field(
        default_factory=lambda: int(os.environ.get("CLASSIN_TOKEN_TTL", str(7 * 24 * 3600)))
    )

    # 首次启动自动创建的管理员（部署后应立即改密）
    admin_username: str = field(default_factory=lambda: os.environ.get("CLASSIN_ADMIN_USER", "admin"))
    admin_password: str = field(default_factory=lambda: os.environ.get("CLASSIN_ADMIN_PASSWORD", "admin123"))

    # 引擎默认产物目录：放在 data/videos 下，跨平台安全、无需管理员权限
    default_save_dir: str = field(default_factory=lambda: str(DATA_DIR / "videos"))

    @property
    def database_url(self) -> str:
        # Why SQLite 需要 check_same_thread=False：FastAPI 线程池与后台
        # 下载线程会跨线程用同一 engine，真正的隔离交给 session-per-request。
        return f"sqlite:///{self.db_path.as_posix()}"


settings = Settings()
