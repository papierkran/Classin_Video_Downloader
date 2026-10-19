"""时间解析小工具。

Why 单独一个模块：`start_time` 的文本→时间戳解析同时被 db（旧数据回填）、
services（CSV 导入 / 控制台同步）使用；如果放在 services 里，db 层反向
import services 会形成环（services 又 import db.models）。放 core 无依赖。
"""
from __future__ import annotations

from datetime import datetime

# ClassIn 导出的开课时间形如 "2026/9/7 21:46"（月日不补零），
# 也兼容 ISO 风格与带秒的写法。
_FORMATS = (
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
)


def parse_start_ts(text: str | None) -> int | None:
    """把开课时间文本转成本地时区 epoch 秒；解析失败返回 None。

    与 sync_service._format_unix 互逆（同一 datetime.strptime/fromtimestamp
    本地时区口径），因此控制台同步写入的 start_ts 与 CSV 导入解析出的
    start_ts 可以直接比较大小——视频库的"开课时间范围"筛选依赖这一点。
    """
    if not text:
        return None
    value = str(text).strip()
    for fmt in _FORMATS:
        try:
            return int(datetime.strptime(value, fmt).timestamp())
        except ValueError:
            continue
    return None
