"""配置服务：脱敏读取、部分更新、Cookie 在线验证。

三条设计红线：
1. Cookie 明文只出现在"写入"和"传给引擎"两条路径，读取一律脱敏；
2. 所有写操作先过业务校验（目录可建、格式合法）再落库，避免脏配置
   在下一次任务启动时才爆炸（那时排查成本高得多）；
3. 验证 Cookie 是网络操作，独立端点不与保存耦合（见 api/settings.py）。
"""
from __future__ import annotations

import logging
import os

from sqlalchemy.orm import Session

from ..core.exceptions import NotFoundError, ValidationFailure
from ..db.models import AppSetting
from ..engine import videodownloader
from ..repositories import SettingRepo
from ..schemas import CookieVerifyOut, SettingsOut, SettingsUpdateIn

logger = logging.getLogger("app.settings")


def mask_cookie(cookie: str) -> str:
    """Cookie 脱敏：只留前后各 12 字符，足以让用户辨认"是哪一份"。

    Why 不是全掩：用户常存多份 Cookie（换账号），全掩后无法区分；
    Why 也不是只掩中间：Cookie 的区分信息往往在开头的会话键名上。
    """
    if not cookie:
        return ""
    if len(cookie) <= 24:
        return "*" * len(cookie)
    return f"{cookie[:12]}...{cookie[-12:]}"


class SettingService:
    """配置领域服务。所有读写都经 _require()：配置行不存在时给
    可读错误而非 NoneType 崩溃（部署时若 init_db 失败，这里是最先
    被用户碰到的地方，错误信息要能指向"重启/重建数据目录"）。"""

    def __init__(self, db: Session):
        self.repo = SettingRepo(db)

    def _require(self) -> AppSetting:
        setting = self.repo.get_singleton()
        if not setting:
            raise NotFoundError("配置尚未初始化，请重启服务")
        return setting

    def get(self) -> SettingsOut:
        """脱敏读取。明文 cookie 只存在于 DB 与"传给引擎"的内存路径，
        API 出口是第三个也是唯一可控的出口——在这里挡住即全链路挡住。"""
        s = self._require()
        return SettingsOut(
            cookie_masked=mask_cookie(s.cookie),
            has_cookie=bool(s.cookie),
            save_dir=s.save_dir,
            driver_path=s.driver_path,
            max_workers=s.max_workers,
            speed_limit=s.speed_limit,
            skip_existing=s.skip_existing,
            headless=s.headless,
        )

    def update(self, payload: SettingsUpdateIn) -> SettingsOut:
        """部分更新。None=不修改（model_dump exclude_unset 已区分），
        cookie 额外用"空串也不改"双保险——HTML 表单回车可能把空值
        带上来，若空串覆盖，用户编辑别的字段会顺手清掉 Cookie。

        省略清除功能是刻意的：误清空 Cookie 让下次任务直接失败，
        代价大于"想换账号"这个低频诉求（重粘一份新的即可）。
        """
        s = self._require()
        data = payload.model_dump(exclude_unset=True)
        cookie = data.pop("cookie", None)
        if cookie:
            s.cookie = cookie.strip()

        save_dir = data.get("save_dir")
        if save_dir is not None:
            # Why 这里只做规范化+建目录，不校验可写性：跨平台只读盘
            # 检测成本高；真正写入失败会在任务日志中体现（fail fast 于引擎侧）。
            save_dir = os.path.normpath(os.path.abspath(save_dir))
            try:
                os.makedirs(save_dir, exist_ok=True)
            except OSError as e:
                raise ValidationFailure(f"保存目录不可用: {e}") from e
            s.save_dir = save_dir

        for key, value in data.items():
            setattr(s, key, value)
        self.repo.commit()
        logger.info("配置已更新: %s", ", ".join(data.keys()) or ("cookie" if cookie else "无字段"))
        return self.get()

    def verify_cookie(self, cookie: str | None) -> CookieVerifyOut:
        """在线探测 Cookie（网络调用，同步端点在线程池执行）。

        传值=验证草稿（未保存），不传=验证已存配置——覆盖
        "粘贴→验证→保存"和"怀疑过期→直接验证"两种用户路径。
        """
        target = (cookie or "").strip() or self._require().cookie
        if not target:
            return CookieVerifyOut(valid=False, message="尚未配置 Cookie")
        valid, message = videodownloader.check_cookie_validity(target)
        return CookieVerifyOut(valid=valid, message=message)

    @staticmethod
    def raw_cookie(db: Session) -> str:
        """供任务启动时读取明文 Cookie（仅 service 间调用，不进 API 响应）。"""
        row = db.get(AppSetting, 1)
        return row.cookie if row else ""
