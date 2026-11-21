"""时间工具。

所有时间戳均为带时区的 ISO 8601 UTC（``...Z``）字符串。固定宽度的 UTC 文本
可直接按字典序比较，等价于按时刻比较，因此数仓中不再做额外的时区换算。
"""

from __future__ import annotations

from datetime import UTC, datetime

ISO_LEN = 20  # 例如 2026-05-04T12:00:00Z


def parse_utc(value: str) -> datetime:
    """解析 ``Z`` 结尾的 UTC 时间字符串。"""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError(f"时间戳缺少时区信息: {value!r}")
    return dt.astimezone(UTC)


def format_utc(dt: datetime) -> str:
    """格式化为 ``Z`` 结尾的 UTC 字符串。"""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc() -> str:
    return format_utc(datetime.now(UTC))


def overlaps(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    """两个半开区间 [start, end) 是否相交。"""
    return start_a < end_b and start_b < end_a
