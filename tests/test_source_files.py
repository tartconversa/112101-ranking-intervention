from __future__ import annotations

import csv
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SOURCES = [
    'deployments.csv',
    'exposures.csv',
    'policy_scopes.csv',
    'policy_strategy_bindings.csv',
    'ingestion_log.csv',
]


class SourceFileTests(unittest.TestCase):
    def test_source_files_have_headers_and_records(self) -> None:
        for filename in EXPECTED_SOURCES:
            with self.subTest(filename=filename):
                with (ROOT / "sources" / filename).open(encoding="utf-8", newline="") as stream:
                    rows = list(csv.DictReader(stream))
                self.assertTrue(rows)
                self.assertTrue(rows[0])

    def test_record_identifiers_are_unique_within_each_source(self) -> None:
        for filename in EXPECTED_SOURCES:
            with self.subTest(filename=filename):
                with (ROOT / "sources" / filename).open(encoding="utf-8", newline="") as stream:
                    reader = csv.DictReader(stream)
                    rows = list(reader)
                # ingestion_log 是只追加事件流，同一业务记录会对应多个事件，
                # 因此其唯一键是 log_id；其余源以 record_id 为唯一键。
                key = "log_id" if filename == "ingestion_log.csv" else "record_id"
                values = [row[key] for row in rows]
                self.assertTrue(all(values))
                self.assertEqual(len(values), len(set(values)))

    def test_ingestion_log_references_existing_source_records(self) -> None:
        with (ROOT / "sources" / "ingestion_log.csv").open(
            encoding="utf-8", newline=""
        ) as stream:
            logs = list(csv.DictReader(stream))
        for filename in EXPECTED_SOURCES:
            if filename == "ingestion_log.csv":
                continue
            with (ROOT / "sources" / filename).open(
                encoding="utf-8", newline=""
            ) as stream:
                ids = {row["record_id"] for row in csv.DictReader(stream)}
            for log in logs:
                if log["source_file"] == filename.removesuffix(".csv"):
                    with self.subTest(log=log["log_id"]):
                        self.assertIn(log["record_id"], ids)


if __name__ == "__main__":
    unittest.main()
