"""时间工具单元测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ranking_facts.time_utils import format_utc, overlaps, parse_utc  # noqa: E402


class TimeUtilsTests(unittest.TestCase):
    def test_parse_and_format_roundtrip(self) -> None:
        raw = "2026-05-04T12:00:00Z"
        self.assertEqual(format_utc(parse_utc(raw)), raw)

    def test_lexicographic_order_matches_instant_order(self) -> None:
        earlier = "2026-05-04T09:59:59Z"
        later = "2026-05-04T10:00:00Z"
        self.assertLess(earlier, later)
        self.assertLess(parse_utc(earlier), parse_utc(later))

    def test_overlaps_half_open_intervals(self) -> None:
        self.assertTrue(overlaps(
            "2026-05-01T00:00:00Z", "2026-05-04T10:00:00Z",
            "2026-05-04T09:59:00Z", "2026-05-05T00:00:00Z",
        ))
        # 端点相接的半开区间不相交。
        self.assertFalse(overlaps(
            "2026-05-01T00:00:00Z", "2026-05-04T10:00:00Z",
            "2026-05-04T10:00:00Z", "2026-05-04T10:30:00Z",
        ))

    def test_parse_rejects_naive_timestamp(self) -> None:
        with self.assertRaises(ValueError):
            parse_utc("2026-05-04T12:00:00")


if __name__ == "__main__":
    unittest.main()
