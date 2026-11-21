"""数仓端到端测试：基于 sources/ 合成夹具验证双时间语义与三层分离。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ranking_facts.db import connect  # noqa: E402
from ranking_facts.ingestion import build_warehouse  # noqa: E402
from ranking_facts import queries  # noqa: E402

SOURCES = ROOT / "sources"


class WarehouseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "warehouse.sqlite3"
        self.conn = connect(self.db_path)
        build_warehouse(self.conn, SOURCES)

    def tearDown(self) -> None:
        self.conn.close()
        self.temp.cleanup()

    # ---- 基础装载 -------------------------------------------------

    def test_raw_layer_matches_source_counts(self) -> None:
        for table, expected in [
            ("raw_policy_scope", 3),
            ("raw_binding", 3),
            ("raw_deployment", 3),
            ("raw_exposure", 3),
            ("raw_ingestion_log", 16),
        ]:
            with self.subTest(table=table):
                self.assertEqual(
                    self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                    expected,
                )

    def test_rebuild_is_idempotent(self) -> None:
        queries.lineage(self.conn)
        from ranking_facts.ingestion import rebuild_curated
        before = self.conn.execute(
            "SELECT scope_id, valid_from, scope_state FROM dim_policy_scope ORDER BY scope_id, valid_from"
        ).fetchall()
        rebuild_curated(self.conn)
        after = self.conn.execute(
            "SELECT scope_id, valid_from, scope_state FROM dim_policy_scope ORDER BY scope_id, valid_from"
        ).fetchall()
        self.assertEqual(before, after)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM governance_task").fetchone()[0], 5
        )

    # ---- 双时间：处置撤销不重写历史 -------------------------------

    def test_withdrawal_is_bitemporal_and_keeps_history(self) -> None:
        # 场景一：同一业务时点（撤销前的 11:30），对比撤销登记前后的系统认知。
        # 认知截止 11:00 时系统只持有 active 初始断言，结论为 active。
        rows = queries.scopes_as_of(
            self.conn, "2026-05-04T11:30:00Z", "2026-05-04T11:00:00Z"
        )
        s12 = [r for r in rows if r["scope_id"] == "S-12"]
        self.assertEqual(len(s12), 1)
        self.assertEqual(s12[0]["scope_state"], "active")

        # 同一业务时点、认知截止 13:00：withdrawn 断言区间从 12:00 起，
        # 不覆盖 11:30，历史结论仍为 active——事后撤销不重写历史。
        rows = queries.scopes_as_of(
            self.conn, "2026-05-04T11:30:00Z", "2026-05-04T13:00:00Z"
        )
        s12 = [r for r in rows if r["scope_id"] == "S-12"]
        self.assertEqual(s12[0]["scope_state"], "active")

        # 场景二：撤销后的业务时点 12:30，系统已获知撤销，结论变为 withdrawn。
        rows = queries.scopes_as_of(
            self.conn, "2026-05-04T12:30:00Z", "2026-05-04T13:00:00Z"
        )
        s12 = [r for r in rows if r["scope_id"] == "S-12"]
        self.assertEqual(s12[0]["scope_state"], "withdrawn")

        # 场景三：查询未来业务时点（12:30）但认知停留在 11:00：系统依据当时
        # 持有的完整区间 active 断言作答（撤销尚未登记），而不是用当前配置
        # 回填 withdrawn——这正是「不按当前配置重写历史认知」。
        rows = queries.scopes_as_of(
            self.conn, "2026-05-04T12:30:00Z", "2026-05-04T11:00:00Z"
        )
        s12 = [r for r in rows if r["scope_id"] == "S-12"]
        self.assertEqual(s12[0]["scope_state"], "active")

        # 同一未来时点、认知截止 13:00：新登记的 withdrawn 断言覆盖该时点且
        # recorded_at 更大，系统的结论更新为 withdrawn。
        rows = queries.scopes_as_of(
            self.conn, "2026-05-04T12:30:00Z", "2026-05-04T13:00:00Z"
        )
        s12 = [r for r in rows if r["scope_id"] == "S-12"]
        self.assertEqual(s12[0]["scope_state"], "withdrawn")

        # S-12 在策展层保留两条断言：完整区间的 active 初始断言，
        # 以及仅覆盖撤销后区间的 withdrawn 新断言。
        versions = self.conn.execute(
            "SELECT scope_state, valid_from FROM dim_policy_scope "
            "WHERE scope_id = 'S-12' ORDER BY recorded_at"
        ).fetchall()
        self.assertEqual([r["scope_state"] for r in versions], ["active", "withdrawn"])
        self.assertEqual(versions[0]["valid_from"], "2026-05-02T00:00:00Z")
        self.assertEqual(versions[1]["valid_from"], "2026-05-04T12:00:00Z")

    # ---- 双时间：迟到配置 -----------------------------------------

    def test_late_binding_absent_from_early_knowledge(self) -> None:
        # 05-02 中午：DEP-4 已在运行，但 B-2 尚未入库。
        running = queries.deployments_running(
            self.conn, "2026-05-02T12:00:00Z", "2026-05-02T12:00:00Z"
        )
        self.assertIn("DEP-4", [r["deployment_id"] for r in running])
        known_bindings = queries.bindings_as_of(
            self.conn, "2026-05-02T12:00:00Z", "2026-05-02T12:00:00Z"
        )
        self.assertNotIn("B-2", [r["binding_id"] for r in known_bindings])

        # 配置入库后才出现在认知中。
        known_bindings = queries.bindings_as_of(
            self.conn, "2026-05-03T12:00:00Z", "2026-05-03T12:00:00Z"
        )
        self.assertIn("B-2", [r["binding_id"] for r in known_bindings])

    def test_binding_gap_detects_unauthorized_delivery_window(self) -> None:
        gaps = {g["deployment_id"]: g for g in queries.deployment_binding_gaps(self.conn)}
        # B-2 的业务授权区间从 05-03T00:00 开始，配置 00:10 才入库；
        # 缺口按业务区间计算，因此截止于 00:00 而非入库时间。
        self.assertEqual(
            gaps["DEP-4"]["gap_segments"],
            [("2026-05-02T01:00:00Z", "2026-05-03T00:00:00Z")],
        )
        self.assertEqual(gaps["DEP-3"]["gap_segments"], [])
        self.assertEqual(gaps["DEP-5"]["gap_segments"], [])

    # ---- 不可变曝光与待核实状态 -----------------------------------

    def test_exposure_immutability_and_verification(self) -> None:
        # EXP-33 发生在失败回滚窗口内，仍然作为事实保留。
        exp33 = self.conn.execute(
            "SELECT * FROM fact_exposure WHERE exposure_id = 'EXP-33'"
        ).fetchone()
        self.assertEqual(exp33["impression_count"], 9)
        self.assertEqual(exp33["is_verified"], 1)

        # EXP-32 迟到且未核实；重建不会改变该状态。
        exp32 = self.conn.execute(
            "SELECT * FROM fact_exposure WHERE exposure_id = 'EXP-32'"
        ).fetchone()
        self.assertEqual(exp32["is_verified"], 0)
        self.assertEqual(exp32["recorded_at"], "2026-05-05T09:00:00Z")

        # 05-04 前系统只知道 EXP-31；之后 EXP-33、EXP-32 依次出现。
        early = queries.exposures_known(self.conn, "2026-05-04T00:00:00Z")
        self.assertEqual([r["exposure_id"] for r in early], ["EXP-31"])
        later = queries.exposures_known(self.conn, "2026-05-05T12:00:00Z")
        self.assertEqual(
            [r["exposure_id"] for r in later], ["EXP-31", "EXP-32", "EXP-33"]
        )

    def test_raw_exposure_rows_are_never_deleted_or_mutated(self) -> None:
        # 重复导入走 INSERT OR IGNORE，落地层行数与原值不变。
        build_warehouse(self.conn, SOURCES)
        row = self.conn.execute(
            "SELECT source_state, impression_count FROM raw_exposure WHERE exposure_id = 'EXP-32'"
        ).fetchone()
        self.assertEqual(row["source_state"], "late")
        self.assertEqual(row["impression_count"], 122)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM raw_exposure").fetchone()[0], 3
        )

    # ---- 合规、影响与恢复 -----------------------------------------

    def test_exposure_scope_compliance(self) -> None:
        rows = {r["exposure_id"]: r for r in queries.exposure_scope_compliance(self.conn)}
        self.assertTrue(rows["EXP-31"]["in_scope"])
        self.assertTrue(rows["EXP-32"]["in_scope"])
        self.assertTrue(rows["EXP-33"]["in_scope"])
        # EXP-31 发生于 05-02，当时只有 PV-2 的 S-10 已登记；S-11（PV-3）
        # 05-03 才登记，不能追溯授权此前的曝光。
        self.assertEqual(rows["EXP-31"]["policy_versions"], ["PV-2"])
        # EXP-32 发生于 S-10（PV-2）与 S-11（PV-3）业务区间的重叠处。
        self.assertEqual(rows["EXP-32"]["policy_versions"], ["PV-2", "PV-3"])
        self.assertFalse(rows["EXP-32"]["is_verified"])
        # EXP-33 发生时 S-12 尚未撤销（撤销 12:00 登记，曝光 10:15 发生）。
        self.assertEqual(rows["EXP-33"]["scope_states"], ["active"])
        # S-12 的撤销断言在 EXP-33 之后才为系统所知，必须显式披露而非追溯适用。
        self.assertEqual(
            rows["EXP-33"]["late_known_scopes"],
            [{"scope_id": "S-12", "recorded_at": "2026-05-04T12:00:00Z"}],
        )

    def test_impact_summary_uses_actual_exposures_not_plan(self) -> None:
        summary = {r["content_id"]: r for r in queries.impact_summary(self.conn)}
        post4 = summary["POST-4"]
        self.assertEqual(post4["impressions"], 652)          # 530 + 122
        self.assertEqual(post4["verified_impressions"], 530)
        self.assertEqual(post4["unverified_impressions"], 122)
        self.assertEqual(summary["POST-8"]["impressions"], 9)

    def test_recovery_status_failed_rollback(self) -> None:
        recovery = {r["deployment_id"]: r for r in queries.recovery_status(self.conn)}
        rec = recovery["DEP-5"]
        self.assertEqual(rec["result"], "failed")
        self.assertEqual(rec["post_window_telemetry"], "verified_exposures_present")
        self.assertEqual(
            [e["exposure_id"] for e in rec["exposures"]], ["EXP-33"]
        )

    def test_governance_tasks_cover_all_triggers(self) -> None:
        rows = queries.governance_tasks(self.conn)
        types = sorted({r["task_type"] for r in rows})
        self.assertEqual(
            types,
            [
                "config_late",
                "exposure_unverified",
                "partial_delivery_review",
                "recovery_check",
                "scope_revision_check",
            ],
        )
        open_tasks = {r["related_id"]: r for r in rows if r["status"] == "open"}
        self.assertIn("B-2", open_tasks)          # 迟到配置
        self.assertIn("EXP-32", open_tasks)       # 待核实曝光
        self.assertIn("DEP-4", open_tasks)        # partial 投放
        self.assertIn("DEP-5", open_tasks)        # 回滚失败
        resolved = [r for r in rows if r["status"] == "resolved"]
        self.assertEqual([r["related_id"] for r in resolved], ["S-12"])

    def test_lineage_reports_versions_windows_and_unverified(self) -> None:
        info = queries.lineage(self.conn)
        self.assertEqual(info.policy_versions, ("PV-2", "PV-3"))
        self.assertEqual(info.strategy_versions, ("RS-8", "RS-9"))
        self.assertTrue(any("DEP-5" in w and "rollback" in w for w in info.actual_windows))
        self.assertTrue(any("EXP-32" in e for e in info.unverified_exposures))


if __name__ == "__main__":
    unittest.main()
