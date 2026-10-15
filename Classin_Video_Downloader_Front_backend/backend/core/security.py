"""安全模块：密码哈希 + JWT 签发/校验 + 登录限速。

Why 不引入 passlib/bcrypt：仅有一个管理员场景，stdlib 的
PBKDF2-HMAC-SHA256（OpenSSL 内置）足够安全且零依赖，
避免公网上多一个需要维护的三方包。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time

import jwt

from .config import settings
from .exceptions import AuthError, RateLimitedError

# PBKDF2 迭代次数：2026 年 OWASP 建议 ≥600k for SHA256，
# 但本应用仅内部 2-3 人使用且登录低频，取 240k 在安全与登录耗时(~200ms)间平衡。
_PBKDF2_ITERATIONS = 240_000
_ALGO = "HS256"


def hash_password(password: str) -> str:
    """生成 `pbkdf2$iter$salt$hash` 格式密码哈希。

    Why 自己拼格式而非 passlib：格式里带上迭代次数（iter），
    将来要提高强度时新旧哈希可共存（verify 按存储的 iter 计算），
    不需要"全站改密"这种粗暴迁移。
    """
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERATIONS)
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """常数时间比较密码（Why hmac.compare_digest：防时序侧信道——
    普通 == 在逐字节比较时，首个不同字节的位置会泄露前缀长度，
    攻击者理论上可逐位试探；compare_digest 耗时与内容无关）。"""
    try:
        scheme, iterations, salt, expected = stored.split("$")
        if scheme != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        # 格式损坏视为不匹配，而不是抛异常（避免 500 泄露存储细节）
        return False


def create_token(user_id: int, username: str, role: str) -> str:
    """签发 JWT。payload 只放最小身份信息，业务数据仍实时查库。

    Why 不把权限/昵称全塞进 token：token 活 7 天，塞得越多，
    "改了角色但旧 token 还带着旧角色"的不一致窗口越大。
    """
    now = int(time.time())
    payload = {"sub": str(user_id), "username": username, "role": role, "iat": now, "exp": now + settings.token_ttl_seconds}
    return jwt.encode(payload, settings.secret_key, algorithm=_ALGO)


def decode_token(token: str) -> dict:
    """校验并解码 JWT，失败统一抛 401（前端收到即跳登录页）。

    Why 分异常类型给不同文案：过期("重新登录")和伪造("凭证无效")
    的用户动作不同，合并成一句反而让用户困惑该刷新还是该重登。
    """
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[_ALGO])
    except jwt.ExpiredSignatureError:
        raise AuthError("登录已过期，请重新登录")
    except jwt.InvalidTokenError:
        raise AuthError("无效的登录凭证")


def create_media_token(video_id: int) -> str:
    """签发短时、仅限单个视频的播放凭证，供原生 video 标签携带。

    与登录 token 同一签名密钥但用 purpose 声明隔离：登录 token 拿来当
    播放凭证会被 decode_media_token 拒绝（反之亦然），一组密钥两套用途
    而不互相提权。15 分钟足够看完短视频片段/拖动进度，又限制分享传播。
    """
    now = int(time.time())
    return jwt.encode({"purpose": "video", "video_id": video_id, "iat": now, "exp": now + 900}, settings.secret_key, algorithm=_ALGO)


def decode_media_token(token: str, video_id: int) -> dict:
    """校验媒体凭证：必须 purpose=video 且 video_id 与请求路径一致。

    Why 查 video_id 绑定：否则拿到任意一个视频的票据就能换 video_id
    访问全库视频（越权）。过期与无效分文案：过期提示"重新打开"，
    无效（伪造/串用）只说"无效"，不给出可利用的信息。
    """
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[_ALGO])
        if payload.get("purpose") != "video" or payload.get("video_id") != video_id:
            raise AuthError("无效的视频访问凭证")
        return payload
    except jwt.ExpiredSignatureError:
        raise AuthError("视频播放凭证已过期，请重新打开视频")
    except jwt.InvalidTokenError:
        raise AuthError("无效的视频访问凭证")


class LoginRateLimiter:
    """登录失败限速（进程内滑动窗口）。

    Why 要做：服务可能暴露公网，弱口令爆破是最现实的威胁；
    Why 进程内而非 Redis：单实例部署，引入 Redis 属过度设计。
    """

    def __init__(self, max_failures: int = 5, window_seconds: int = 300):
        self._max = max_failures
        self._window = window_seconds
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        """登录前置检查：窗口内失败次数达上限即 429（文案含剩余等待时间）。"""
        now = time.time()
        with self._lock:
            # 顺手淘汰过期记录：不淘汰的话失败次数永远累积，
            # 用户等够窗口也登不进来（假性永久锁定）
            hits = [t for t in self._failures.get(key, []) if now - t < self._window]
            if len(hits) >= self._max:
                raise RateLimitedError(f"登录失败次数过多，请 {self._window // 60} 分钟后再试")
            self._failures[key] = hits

    def record_failure(self, key: str) -> None:
        """登录失败后记账（key=用户名或 IP，由调用方决定粒度）。"""
        with self._lock:
            self._failures.setdefault(key, []).append(time.time())

    def reset(self, key: str) -> None:
        """登录成功后清账：成功即证明是合法用户，历史失败不应继续累计。"""
        with self._lock:
            self._failures.pop(key, None)


login_rate_limiter = LoginRateLimiter()
