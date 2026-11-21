"""历史时点查询与治理报表的读取层。

所有 ``*_as_of`` 函数都接受两个独立时点：

* ``as_of_time``：业务时间点（"干预当时发生了什么"），依据各事实的业务区间过滤；
* ``known_at``：系统记录时间点（"当时系统知道什么"），依据 ``recorded_at`` 过滤。

两者结合即为双时间查询：迟到登记、事后撤销不会改写历史时点上已存在的结论。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .time_utils import overlaps


# ---------------------------------------------------------------------
# 双时间基础查询
# ---------------------------------------------------------------------


def scopes_as_of(
    conn: sqlite3.Connection, as_of_time: str, known_at: str | None = None
) -> list[sqlite3.Row]:
    """某业务时点上对系统已知的政策允许范围。

    双时间语义：先按业务区间与认知时点过滤断言，再在每个 scope_id 中取
    recorded_at 最大的一条，得到系统在该认知时点上对该范围的最新结论。
    """
    sql = """
        SELECT s.* FROM dim_policy_scope s
        JOIN (
            SELECT scope_id, MAX(recorded_at) AS latest_recorded
            FROM dim_policy_scope
            WHERE valid_from <= ? AND valid_to > ? AND recorded_at <= ?
            GROUP BY scope_id
        ) latest
          ON latest.scope_id = s.scope_id
         AND latest.latest_recorded = s.recorded_at
        WHERE s.valid_from <= ? AND s.valid_to > ? AND s.recorded_at <= ?
        ORDER BY s.scope_id
    """
    cutoff = known_at or "9999-12-31T23:59:59Z"
    params = (as_of_time, as_of_time, cutoff, as_of_time, as_of_time, cutoff)
    return conn.execute(sql, params).fetchall()


def bindings_as_of(
    conn: sqlite3.Connection, as_of_time: str, known_at: str | None = None
) -> list[sqlite3.Row]:
    """某业务时点上对系统已知的策略授权配置（配置计划层）。"""
    sql = (
        "SELECT * FROM dim_binding "
        "WHERE valid_from <= ? AND valid_to > ? AND recorded_at <= ? "
        "ORDER BY binding_id"
    )
    return conn.execute(
        sql, (as_of_time, as_of_time, known_at or "9999-12-31T23:59:59Z")
    ).fetchall()


def deployments_running(
    conn: sqlite3.Connection, as_of_time: str, known_at: str | None = None
) -> list[sqlite3.Row]:
    """某业务时点上实际在运行的投放（真实投放层）。"""
    sql = (
        "SELECT * FROM fact_deployment "
        "WHERE started_at <= ? AND ended_at > ? AND recorded_at <= ? "
        "ORDER BY deployment_id"
    )
    return conn.execute(
        sql, (as_of_time, as_of_time, known_at or "9999-12-31T23:59:59Z")
    ).fetchall()


def exposures_known(
    conn: sqlite3.Connection, known_at: str, verified_only: bool = False
) -> list[sqlite3.Row]:
    """截至某系统时点已入库的真实曝光；可限定只看已核实记录。"""
    sql = "SELECT * FROM fact_exposure WHERE recorded_at <= ?"
    if verified_only:
        sql += " AND is_verified = 1"
    return conn.execute(sql + " ORDER BY exposure_id", (known_at,)).fetchall()


# ---------------------------------------------------------------------
# 区间工具与合规比对
# ---------------------------------------------------------------------


def subtract_intervals(
    window: tuple[str, str], covered: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    """返回半开区间 ``window`` 未被 ``covered`` 任一段覆盖的子区间。"""
    start, end = window
    cuts = {start, end}
    for seg_start, seg_end in covered:
        if seg_start > start and seg_start < end:
            cuts.add(seg_start)
        if seg_end > start and seg_end < end:
            cuts.add(seg_end)
    points = sorted(cuts)
    gaps: list[tuple[str, str]] = []
    for seg_start, seg_end in zip(points, points[1:]):
        if not any(
            overlaps(seg_start, seg_end, c_start, c_end) for c_start, c_end in covered
        ):
            gaps.append((seg_start, seg_end))
    return gaps


def _binding_coverage(
    conn: sqlite3.Connection, deployment: sqlite3.Row
) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT * FROM dim_binding "
        "WHERE strategy_version = ? AND channel_id = ? "
        "ORDER BY valid_from",
        (deployment["strategy_version"], deployment["channel_id"]),
    ).fetchall()
    return [(row["valid_from"], row["valid_to"]) for row in rows]


def deployment_binding_gaps(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """真实投放窗口与配置授权区间的比对：计划层未覆盖的实际投放时段。"""
    results: list[dict[str, object]] = []
    for dep in conn.execute("SELECT * FROM fact_deployment ORDER BY deployment_id"):
        window = (dep["started_at"], dep["ended_at"])
        covered = _binding_coverage(conn, dep)
        gaps = subtract_intervals(window, covered)
        results.append({
            "deployment_id": dep["deployment_id"],
            "strategy_version": dep["strategy_version"],
            "channel_id": dep["channel_id"],
            "actual_window": window,
            "covered_segments": covered,
            "gap_segments": gaps,
            "deploy_state": dep["deploy_state"],
        })
    return results


def exposure_scope_compliance(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """逐条核对真实曝光是否落在当时的政策允许范围内，并标注授权政策版本。

    「当时」按曝光发生时刻系统的认知判定：只接受 recorded_at <= 曝光时间的断言，
    且每个 scope_id 取最新一条；事后才登记的范围/撤销不能用于追溯改写结论，
    但会在 late_known_scopes 中单独披露。
    """
    results: list[dict[str, object]] = []
    for exp in conn.execute("SELECT * FROM fact_exposure ORDER BY exposure_id"):
        # 业务区间覆盖曝光时点的断言，用于判定曝光当时的合规状态。
        scopes = conn.execute(
            "SELECT * FROM dim_policy_scope "
            "WHERE content_id = ? AND valid_from <= ? AND valid_to > ? "
            "ORDER BY recorded_at, scope_id",
            (exp["content_id"], exp["occurred_at"], exp["occurred_at"]),
        ).fetchall()
        # 登记晚于曝光的断言（含业务区间不覆盖该时点的撤销/收窄行）：
        # 这些是系统在曝光之后才获知的事实，只能披露、不能追溯适用。
        late_scopes = conn.execute(
            "SELECT scope_id, MIN(recorded_at) AS first_recorded "
            "FROM dim_policy_scope WHERE content_id = ? AND recorded_at > ? "
            "GROUP BY scope_id ORDER BY scope_id",
            (exp["content_id"], exp["occurred_at"]),
        ).fetchall()
        knowable = [s for s in scopes if s["recorded_at"] <= exp["occurred_at"]]
        # 同一 scope_id 在曝光时点可能有多条断言，取系统当时已知的最新结论。
        latest_by_scope: dict[str, sqlite3.Row] = {}
        for scope in knowable:
            current = latest_by_scope.get(scope["scope_id"])
            if current is None or scope["recorded_at"] > current["recorded_at"]:
                latest_by_scope[scope["scope_id"]] = scope
        active = list(latest_by_scope.values())
        results.append({
            "exposure_id": exp["exposure_id"],
            "content_id": exp["content_id"],
            "occurred_at": exp["occurred_at"],
            "impression_count": exp["impression_count"],
            "is_verified": bool(exp["is_verified"]),
            "in_scope": bool(active),
            "policy_versions": sorted({s["policy_version"] for s in active}),
            "scope_states": sorted({s["scope_state"] for s in active}),
            # 每个范围只披露一次：该范围在曝光之后才出现新断言，
            # 这些迟到认知不能用于追溯改写曝光当时的合规结论。
            "late_known_scopes": [
                {"scope_id": row["scope_id"], "recorded_at": row["first_recorded"]}
                for row in late_scopes
            ],
        })
    return results


# ---------------------------------------------------------------------
# 影响、恢复与干预视图
# ---------------------------------------------------------------------


def impact_summary(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """按内容汇总真实影响：曝光事实（而非计划投放量）是唯一度量来源。"""
    results: list[dict[str, object]] = []
    rows = conn.execute(
        """
        SELECT e.content_id,
               COUNT(*)                         AS exposure_rows,
               SUM(e.impression_count)          AS impressions,
               SUM(CASE WHEN e.is_verified = 1
                        THEN e.impression_count ELSE 0 END) AS verified_impressions,
               MIN(e.occurred_at)               AS first_occurred,
               MAX(e.occurred_at)               AS last_occurred
        FROM fact_exposure e
        GROUP BY e.content_id
        ORDER BY e.content_id
        """
    ).fetchall()
    for row in rows:
        scopes = conn.execute(
            "SELECT DISTINCT policy_version, scope_state FROM dim_policy_scope "
            "WHERE content_id = ? ORDER BY policy_version",
            (row["content_id"],),
        ).fetchall()
        results.append({
            "content_id": row["content_id"],
            "exposure_rows": row["exposure_rows"],
            "impressions": row["impressions"],
            "verified_impressions": row["verified_impressions"],
            "unverified_impressions": row["impressions"] - row["verified_impressions"],
            "first_occurred": row["first_occurred"],
            "last_occurred": row["last_occurred"],
            "policy_versions": sorted({s["policy_version"] for s in scopes}),
            "scope_states": sorted({s["scope_state"] for s in scopes}),
        })
    return results


def recovery_status(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """恢复动作及其窗口后曝光核查：区分已确认干净、发现曝光与无法核实。"""
    results: list[dict[str, object]] = []
    for rec in conn.execute("SELECT * FROM fact_recovery_action ORDER BY recovery_id"):
        dep = conn.execute(
            "SELECT * FROM fact_deployment WHERE deployment_id = ?",
            (rec["deployment_id"],),
        ).fetchone()
        after = conn.execute(
            "SELECT * FROM fact_exposure WHERE deployment_id = ? ORDER BY exposure_id",
            (rec["deployment_id"],),
        ).fetchall()
        verified_after = [e for e in after if e["is_verified"]]
        if not after:
            post_window = "no_telemetry"
        elif verified_after:
            post_window = "verified_exposures_present"
        else:
            post_window = "exposures_unverified"
        results.append({
            "recovery_id": rec["recovery_id"],
            "deployment_id": rec["deployment_id"],
            "result": rec["result"],
            "started_at": rec["started_at"],
            "finished_at": rec["finished_at"],
            "actual_window": (dep["started_at"], dep["ended_at"]),
            "post_window_telemetry": post_window,
            "exposures": [dict(e) for e in after],
        })
    return results


def governance_tasks(conn: sqlite3.Connection, status: str | None = None) -> list[sqlite3.Row]:
    """治理任务清单（处置变更、迟到配置、待核实曝光与回滚核查）。"""
    sql = "SELECT * FROM governance_task"
    params: tuple[object, ...] = ()
    if status:
        sql += " WHERE status = ?"
        params = (status,)
    return conn.execute(sql + " ORDER BY task_id", params).fetchall()


def intervention_view(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """三层事实并排视图：政策允许范围 / 配置计划 / 真实投放与曝光。"""
    view: list[dict[str, object]] = []
    for dep in conn.execute("SELECT * FROM fact_deployment ORDER BY deployment_id"):
        bindings = conn.execute(
            "SELECT * FROM dim_binding WHERE strategy_version = ? AND channel_id = ?",
            (dep["strategy_version"], dep["channel_id"]),
        ).fetchall()
        exposures = conn.execute(
            "SELECT * FROM fact_exposure WHERE deployment_id = ? ORDER BY exposure_id",
            (dep["deployment_id"],),
        ).fetchall()
        content_ids = sorted({e["content_id"] for e in exposures})
        scope_rows: list[sqlite3.Row] = []
        for content_id in content_ids:
            scope_rows.extend(conn.execute(
                "SELECT * FROM dim_policy_scope WHERE content_id = ? ORDER BY valid_from",
                (content_id,),
            ).fetchall())
        view.append({
            "deployment_id": dep["deployment_id"],
            "actual_window": (dep["started_at"], dep["ended_at"]),
            "deploy_state": dep["deploy_state"],
            "policy_layer": {
                "policy_versions": sorted({b["policy_version"] for b in bindings}),
                "scopes": [dict(s) for s in scope_rows],
            },
            "config_layer": {
                "strategy_version": dep["strategy_version"],
                "channel_id": dep["channel_id"],
                "bindings": [dict(b) for b in bindings],
            },
            "delivery_layer": {
                "exposure_rows": len(exposures),
                "impressions": sum(e["impression_count"] for e in exposures),
                "unverified_impressions": sum(
                    e["impression_count"] for e in exposures if not e["is_verified"]
                ),
                "exposures": [dict(e) for e in exposures],
            },
        })
    return view


# ---------------------------------------------------------------------
# 数据血统
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class Lineage:
    policy_versions: tuple[str, ...]
    strategy_versions: tuple[str, ...]
    actual_windows: tuple[str, ...]
    unverified_exposures: tuple[str, ...]
    sources: tuple[str, ...] = (
        "policy_scopes.csv",
        "policy_strategy_bindings.csv",
        "deployments.csv",
        "exposures.csv",
        "ingestion_log.csv",
    )


def lineage(conn: sqlite3.Connection) -> Lineage:
    """汇总当前报表结论所依赖的版本、实际区间与待核实记录。"""
    policy_versions = tuple(r[0] for r in conn.execute(
        "SELECT DISTINCT policy_version FROM dim_policy_scope ORDER BY policy_version"
    ))
    strategy_versions = tuple(r[0] for r in conn.execute(
        "SELECT DISTINCT strategy_version FROM dim_binding ORDER BY strategy_version"
    ))
    windows = tuple(
        f"{r['deployment_id']}: [{r['started_at']}, {r['ended_at']}) / {r['deploy_state']}"
        for r in conn.execute(
            "SELECT * FROM fact_deployment ORDER BY deployment_id"
        )
    )
    unverified = tuple(
        f"{r['exposure_id']}({r['impression_count']}, {r['source_state']})"
        for r in conn.execute(
            "SELECT * FROM fact_exposure WHERE is_verified = 0 ORDER BY exposure_id"
        )
    )
    return Lineage(policy_versions, strategy_versions, windows, unverified)
