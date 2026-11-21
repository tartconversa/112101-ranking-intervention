"""数据导入与策展层重建。

导入分两步：

1. ``load_csv_sources`` 把四个业务 CSV 与事件日志原样写入 ``raw_*`` 落地表，
   按主键去重，可重复执行；落地层永不更新、永不删除。
2. ``rebuild_curated`` 从落地层重建双时间策展层。重建是幂等的全量推导：
   系统记录时间（recorded_at）只来自 ``raw_ingestion_log``，业务时间只来自
   业务 CSV，两类时间在整个数仓中保持独立。
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from .db import create_schema, reset_curated

SOURCE_FILES = {
    "raw_policy_scope": "policy_scopes.csv",
    "raw_binding": "policy_strategy_bindings.csv",
    "raw_deployment": "deployments.csv",
    "raw_exposure": "exposures.csv",
    "raw_ingestion_log": "ingestion_log.csv",
}

# 处置状态变更事件 → dim_policy_scope.scope_state 取值
STATE_EVENT_TO_STATE = {
    "withdrawn": "withdrawn",
    "narrowed": "narrowed",
    "expanded": "active",
}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def load_csv_sources(conn: sqlite3.Connection, sources_dir: str | Path) -> None:
    """把 sources/ 下的 CSV 幂等导入原始落地层。"""
    create_schema(conn)
    directory = Path(sources_dir)
    for table, filename in SOURCE_FILES.items():
        rows = _read_csv(directory / filename)
        if not rows:
            continue
        columns = list(rows[0].keys())
        placeholders = ", ".join("?" for _ in columns)
        sql = (
            f"INSERT OR IGNORE INTO {table} ({', '.join(columns)}) "
            f"VALUES ({placeholders})"
        )
        conn.executemany(sql, [[row[col] for col in columns] for row in rows])
    conn.commit()


def _logs_by_record(conn: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    rows = conn.execute(
        "SELECT * FROM raw_ingestion_log ORDER BY recorded_at, log_id"
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["record_id"], []).append(row)
    return grouped


def _first_recorded_at(logs: list[sqlite3.Row]) -> str:
    return min(log["recorded_at"] for log in logs)


def _build_policy_scopes(conn: sqlite3.Connection, logs: dict[str, list[sqlite3.Row]]) -> None:
    for scope in conn.execute("SELECT * FROM raw_policy_scope").fetchall():
        record_logs = logs.get(scope["record_id"], [])
        if not record_logs:
            raise ValueError(f"政策范围 {scope['scope_id']} 缺少 ingestion_log 记录时间")
        first_log = min(record_logs, key=lambda log: log["recorded_at"])
        state_events = sorted(
            (log for log in record_logs
             if log["event_kind"] in STATE_EVENT_TO_STATE),
            key=lambda log: log["recorded_at"],
        )
        # 源 CSV 的 scope_state 是当前快照（如已撤销的范围终态为 withdrawn），
        # 不能直接当作登记时的初始状态。按事件日志的变更序列回推登记时点状态：
        # 没有变更事件则与快照一致；否则初始状态是首个事件之前的状态。
        if state_events:
            initial_state = "active"
        else:
            initial_state = scope["scope_state"]
        # 初始断言：登记时系统认知的完整业务区间，原样保留，永不修改。
        conn.execute(
            "INSERT INTO dim_policy_scope VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                scope["scope_id"], scope["content_id"], scope["policy_version"],
                scope["effective_from"], scope["effective_to"], initial_state,
                first_log["recorded_at"], scope["record_id"],
            ),
        )
        # 处置变更：追加一条业务区间从「事件时点」起的新断言，不修改任何旧行。
        #   - 变更前的业务时点只匹配初始行 → 历史结论不被改写；
        #   - 变更后的业务时点同时匹配两行，取 recorded_at 最大者 → 结论更新；
        #   - 若查询的认知截止早于事件登记，新断言不可见 → 系统当时的认知保持旧态。
        for log in state_events:
            conn.execute(
                "INSERT INTO dim_policy_scope VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    scope["scope_id"], scope["content_id"], scope["policy_version"],
                    log["recorded_at"], scope["effective_to"],
                    STATE_EVENT_TO_STATE[log["event_kind"]],
                    log["recorded_at"], scope["record_id"],
                ),
            )


def _build_bindings(conn: sqlite3.Connection, logs: dict[str, list[sqlite3.Row]]) -> None:
    for binding in conn.execute("SELECT * FROM raw_binding").fetchall():
        recorded_at = _first_recorded_at(logs[binding["record_id"]])
        conn.execute(
            "INSERT INTO dim_binding VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                binding["binding_id"], binding["policy_version"],
                binding["strategy_version"], binding["channel_id"],
                binding["valid_from"], binding["valid_to"],
                recorded_at, binding["record_id"],
            ),
        )


def _build_deployments(conn: sqlite3.Connection, logs: dict[str, list[sqlite3.Row]]) -> None:
    for dep in conn.execute("SELECT * FROM raw_deployment").fetchall():
        recorded_at = _first_recorded_at(logs[dep["record_id"]])
        conn.execute(
            "INSERT INTO fact_deployment VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                dep["deployment_id"], dep["strategy_version"], dep["channel_id"],
                dep["started_at"], dep["ended_at"], dep["deploy_state"],
                recorded_at, dep["record_id"],
            ),
        )


def _build_exposures(conn: sqlite3.Connection, logs: dict[str, list[sqlite3.Row]]) -> None:
    for exp in conn.execute("SELECT * FROM raw_exposure").fetchall():
        record_logs = logs.get(exp["record_id"], [])
        if not record_logs:
            raise ValueError(f"曝光 {exp['exposure_id']} 缺少 ingestion_log 记录时间")
        is_verified = int(any(log["event_kind"] == "confirmed" for log in record_logs))
        conn.execute(
            "INSERT INTO fact_exposure VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                exp["exposure_id"], exp["content_id"], exp["deployment_id"],
                exp["occurred_at"], exp["impression_count"], exp["source_state"],
                is_verified, _first_recorded_at(record_logs), exp["record_id"],
            ),
        )


def _build_recovery_actions(conn: sqlite3.Connection, logs: dict[str, list[sqlite3.Row]]) -> None:
    for dep in conn.execute("SELECT * FROM raw_deployment").fetchall():
        record_logs = logs.get(dep["record_id"], [])
        starts = [log for log in record_logs if log["event_kind"] == "rollback_started"]
        if not starts:
            continue
        finish_logs = [
            log for log in record_logs
            if log["event_kind"] in ("rollback_failed", "rollback_succeeded")
        ]
        started_at = starts[0]["recorded_at"]
        if finish_logs:
            finish = finish_logs[0]
            finished_at = finish["recorded_at"]
            result = "failed" if finish["event_kind"] == "rollback_failed" else "succeeded"
        else:
            finished_at, result = None, "in_progress"
        conn.execute(
            "INSERT INTO fact_recovery_action VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"REC-{dep['deployment_id']}", dep["deployment_id"], "rollback",
                started_at, finished_at, result,
                starts[0]["recorded_at"], dep["record_id"],
            ),
        )


def _build_governance_tasks(conn: sqlite3.Connection) -> None:
    tasks: list[tuple[str, str, str, str, str | None, str, str]] = []

    # 1) 配置迟到：投放启动的那一刻，系统中还没有任何同策略/渠道的授权配置。
    #    只以启动时点为准，避免把后续补录的正常绑定（如回滚窗口的 B-3）误判为迟到。
    late_sql = """
        SELECT d.deployment_id, d.started_at,
               MIN(b.recorded_at) AS first_binding_recorded
        FROM fact_deployment d
        LEFT JOIN dim_binding b
          ON b.strategy_version = d.strategy_version
         AND b.channel_id = d.channel_id
         AND b.recorded_at <= d.started_at
        GROUP BY d.deployment_id
        HAVING first_binding_recorded IS NULL
        ORDER BY d.deployment_id
    """
    for row in conn.execute(late_sql).fetchall():
        first_binding = conn.execute(
            "SELECT binding_id, recorded_at FROM dim_binding "
            "WHERE strategy_version = (SELECT strategy_version FROM fact_deployment "
            "                          WHERE deployment_id = ?) "
            "  AND channel_id = (SELECT channel_id FROM fact_deployment "
            "                    WHERE deployment_id = ?) "
            "ORDER BY recorded_at LIMIT 1",
            (row["deployment_id"], row["deployment_id"]),
        ).fetchone()
        tasks.append((
            "config_late", "open",
            first_binding["recorded_at"] if first_binding else row["started_at"], None,
            first_binding["binding_id"] if first_binding else row["deployment_id"],
            (f"投放 {row['deployment_id']} 于 {row['started_at']} 启动时，系统中没有任何"
             f"对应授权配置；首个配置 {first_binding['binding_id']} 直到 "
             f"{first_binding['recorded_at']} 才入库，启动时段属于未授权投放"),
        ))

    # 2) 待核实曝光：迟到或未经对账确认的真实曝光记录。
    for row in conn.execute(
        "SELECT * FROM fact_exposure WHERE is_verified = 0 ORDER BY exposure_id"
    ).fetchall():
        tasks.append((
            "exposure_unverified", "open", row["recorded_at"], None, row["exposure_id"],
            (f"曝光 {row['exposure_id']}（{row['impression_count']} 次）"
             f"发生于 {row['occurred_at']}，源状态 {row['source_state']}，尚未核实"),
        ))

    # 3) partial 投放：计划投放与实际触达存在缺口，需运营复核。
    for row in conn.execute(
        "SELECT * FROM fact_deployment WHERE deploy_state = 'partial' ORDER BY deployment_id"
    ).fetchall():
        tasks.append((
            "partial_delivery_review", "open", row["recorded_at"], None,
            row["deployment_id"],
            (f"投放 {row['deployment_id']} 实际状态为 partial，"
             f"窗口 {row['started_at']} ~ {row['ended_at']}，需复核计划与实际触达差异"),
        ))

    # 4) 回滚失败：恢复动作未成功，保持开放核查。
    for row in conn.execute(
        "SELECT * FROM fact_recovery_action ORDER BY recovery_id"
    ).fetchall():
        status = "open" if row["result"] != "succeeded" else "resolved"
        tasks.append((
            "recovery_check", status, row["recorded_at"],
            row["finished_at"] if status == "resolved" else None,
            row["deployment_id"],
            (f"投放 {row['deployment_id']} 回滚结果 {row['result']}；"
             + ("恢复未完成，需人工核查回滚窗口内曝光"
                if status == "open" else "回滚成功，恢复完成")),
        ))

    # 5) 处置变更触发范围修订核查：撤销/收窄断言之后是否仍有该内容的真实曝光。
    #    只认事件日志登记的变更（valid_from = recorded_at），排除源记录的初始状态。
    scope_sql = """
        SELECT s.scope_id, s.content_id, s.valid_from AS changed_at,
               s.scope_state, s.recorded_at
        FROM dim_policy_scope s
        JOIN (
            SELECT scope_id, MIN(recorded_at) AS first_recorded
            FROM dim_policy_scope GROUP BY scope_id
        ) first
          ON first.scope_id = s.scope_id
        WHERE s.scope_state IN ('withdrawn', 'narrowed')
          AND s.recorded_at > first.first_recorded
        ORDER BY s.scope_id
    """
    for row in conn.execute(scope_sql).fetchall():
        later = conn.execute(
            "SELECT COUNT(*) AS n FROM fact_exposure "
            "WHERE content_id = ? AND occurred_at >= ?",
            (row["content_id"], row["changed_at"]),
        ).fetchone()["n"]
        if later == 0:
            status, resolved_at = "resolved", row["recorded_at"]
            evidence = (
                f"范围 {row['scope_id']} 于 {row['changed_at']} {row['scope_state']}，"
                f"截至重建时点未发现 {row['content_id']} 变更后的曝光，范围修订核查通过"
            )
        else:
            status, resolved_at = "open", None
            evidence = (
                f"范围 {row['scope_id']} 于 {row['changed_at']} {row['scope_state']}，"
                f"但仍有 {later} 条 {row['content_id']} 曝光发生在变更之后，需恢复核查"
            )
        tasks.append((
            "scope_revision_check", status, row["recorded_at"], resolved_at,
            row["scope_id"], evidence,
        ))

    for index, (task_type, status, opened_at, resolved_at, related_id, evidence) in enumerate(
        sorted(tasks), start=1
    ):
        conn.execute(
            "INSERT INTO governance_task VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"T-{index:02d}", task_type, status, opened_at, resolved_at, related_id, evidence),
        )


def rebuild_curated(conn: sqlite3.Connection) -> None:
    """从原始落地层幂等重建全部策展表与治理任务。"""
    reset_curated(conn)
    logs = _logs_by_record(conn)
    _build_policy_scopes(conn, logs)
    _build_bindings(conn, logs)
    _build_deployments(conn, logs)
    _build_exposures(conn, logs)
    _build_recovery_actions(conn, logs)
    _build_governance_tasks(conn)
    conn.commit()


def build_warehouse(conn: sqlite3.Connection, sources_dir: str | Path) -> None:
    """完整建库流程：导入原始 CSV，再重建策展层。"""
    load_csv_sources(conn, sources_dir)
    rebuild_curated(conn)
