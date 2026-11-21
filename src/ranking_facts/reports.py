"""面向政策与运营人员的中文文本报表。

每份报表末尾固定附带「数据血统」块，说明结论使用的政策版本、策略版本、
实际生效区间以及尚未核实的曝光记录。
"""

from __future__ import annotations

import sqlite3
import unicodedata
from collections.abc import Sequence

from . import queries
from .queries import Lineage


def _display_width(text: str) -> int:
    """按终端显示宽度计算（CJK 全角字符占两列）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1 for ch in text)


def render_table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    text_rows = [[("" if cell is None else str(cell)) for cell in row] for row in rows]
    widths = [
        max([_display_width(headers[i])] + [_display_width(row[i]) for row in text_rows],
        default=0,
    ) for i in range(len(headers))]

    def pad(cell: str, width: int) -> str:
        return cell + " " * (width - _display_width(cell))

    border = "-+-".join("-" * width for width in widths)
    lines = [" | ".join(pad(h, widths[i]) for i, h in enumerate(headers)), border]
    lines.extend(" | ".join(pad(row[i], widths[i]) for i in range(len(headers)))
                 for row in text_rows)
    return "\n".join(lines)


def lineage_footer(conn: sqlite3.Connection) -> str:
    info: Lineage = queries.lineage(conn)
    lines = [
        "数据血统（本报表结论的依据与边界）",
        f"  政策版本: {', '.join(info.policy_versions)}",
        f"  策略版本: {', '.join(info.strategy_versions)}",
        "  实际生效区间（真实投放，非计划量）:",
        *[f"    - {window}" for window in info.actual_windows],
        f"  尚未核实的曝光记录: {', '.join(info.unverified_exposures) or '无'}",
        f"  来源文件: {', '.join(info.sources)}",
    ]
    return "\n".join(lines)


def report_scopes(
    conn: sqlite3.Connection, as_of_time: str, known_at: str | None
) -> str:
    rows = queries.scopes_as_of(conn, as_of_time, known_at)
    title = f"政策允许范围（业务时点 {as_of_time}"
    if known_at:
        title += f"，系统获知截止 {known_at}"
    title += "）"
    table = render_table(
        ["范围ID", "内容", "政策版本", "业务有效区间", "处置状态", "系统登记时间"],
        [(r["scope_id"], r["content_id"], r["policy_version"],
          f"[{r['valid_from']}, {r['valid_to']})", r["scope_state"], r["recorded_at"])
         for r in rows],
    )
    return f"{title}\n{table}\n\n{lineage_footer(conn)}"


def report_impact(conn: sqlite3.Connection) -> str:
    rows = queries.impact_summary(conn)
    table = render_table(
        ["内容", "曝光条数", "总触达", "已核实触达", "待核实触达",
         "首/末曝光时间", "政策版本", "处置状态"],
        [(r["content_id"], r["exposure_rows"], r["impressions"],
          r["verified_impressions"], r["unverified_impressions"],
          f"{r['first_occurred']} ~ {r['last_occurred']}",
          ",".join(r["policy_versions"]), ",".join(r["scope_states"]))
         for r in rows],
    )
    return f"实际影响汇总（仅基于真实曝光事实）\n{table}\n\n{lineage_footer(conn)}"


def report_compliance(conn: sqlite3.Connection) -> str:
    gap_rows = queries.deployment_binding_gaps(conn)
    gap_table = render_table(
        ["投放", "策略/渠道", "实际投放窗口", "配置授权覆盖段", "未覆盖时段（合规缺口）"],
        [(g["deployment_id"], f"{g['strategy_version']}/{g['channel_id']}",
          f"[{g['actual_window'][0]}, {g['actual_window'][1]})",
          "; ".join(f"[{a}, {b})" for a, b in g["covered_segments"]) or "无",
          "; ".join(f"[{a}, {b})" for a, b in g["gap_segments"]) or "无缺口")
         for g in gap_rows],
    )
    exp_rows = queries.exposure_scope_compliance(conn)
    exp_table = render_table(
        ["曝光", "内容", "发生时间", "触达", "核实", "在政策范围内", "授权政策版本"],
        [(r["exposure_id"], r["content_id"], r["occurred_at"], r["impression_count"],
          "已核实" if r["is_verified"] else "待核实",
          "是" if r["in_scope"] else "否",
          ",".join(r["policy_versions"]) or "无授权")
         for r in exp_rows],
    )
    return (
        "合规比对（计划授权 vs 真实投放；政策范围 vs 真实曝光）\n"
        f"{gap_table}\n\n{exp_table}\n\n{lineage_footer(conn)}"
    )


def report_recovery(conn: sqlite3.Connection) -> str:
    rows = queries.recovery_status(conn)
    labels = {
        "no_telemetry": "窗口后无遥测数据（无法证实已恢复）",
        "verified_exposures_present": "存在已核实曝光（历史事实保留，需评估影响）",
        "exposures_unverified": "窗口内曝光尚未核实",
    }
    table = render_table(
        ["恢复动作", "投放", "结果", "开始", "结束", "窗口后核查"],
        [(r["recovery_id"], r["deployment_id"], r["result"], r["started_at"],
          r["finished_at"], labels[r["post_window_telemetry"]]) for r in rows],
    )
    return f"恢复状态与回滚核查\n{table}\n\n{lineage_footer(conn)}"


def report_tasks(conn: sqlite3.Connection, status: str | None = None) -> str:
    rows = queries.governance_tasks(conn, status)
    table = render_table(
        ["任务", "类型", "状态", "开启时间", "关闭时间", "关联对象", "证据与结论"],
        [(r["task_id"], r["task_type"], r["status"], r["opened_at"],
          r["resolved_at"], r["related_id"], r["evidence"]) for r in rows],
    )
    title = "治理任务（处置变更与恢复核查）"
    if status:
        title += f" — 仅 {status}"
    return f"{title}\n{table}\n\n{lineage_footer(conn)}"


def report_intervention_view(conn: sqlite3.Connection) -> str:
    blocks = ["干预三层视图（政策允许 / 配置计划 / 真实分发）"]
    for item in queries.intervention_view(conn):
        blocks.append(
            f"\n● 投放 {item['deployment_id']}  实际窗口 {item['actual_window'][0]} ~ "
            f"{item['actual_window'][1]}  真实结局: {item['deploy_state']}"
        )
        blocks.append("  [政策层] 授权政策版本: "
                      + (", ".join(item["policy_layer"]["policy_versions"]) or "无"))
        for scope in item["policy_layer"]["scopes"]:
            blocks.append(
                f"    - 范围 {scope['scope_id']} {scope['content_id']} "
                f"{scope['policy_version']} [{scope['valid_from']}, {scope['valid_to']}) "
                f"状态={scope['scope_state']} 登记于 {scope['recorded_at']}"
            )
        blocks.append(
            f"  [配置层] 策略 {item['config_layer']['strategy_version']} / "
            f"渠道 {item['config_layer']['channel_id']}"
        )
        for binding in item["config_layer"]["bindings"]:
            blocks.append(
                f"    - 绑定 {binding['binding_id']} 配置区间 "
                f"[{binding['valid_from']}, {binding['valid_to']}) "
                f"入库于 {binding['recorded_at']}"
            )
        delivery = item["delivery_layer"]
        blocks.append(
            f"  [真实层] 曝光 {delivery['exposure_rows']} 条，真实触达 "
            f"{delivery['impressions']}（其中待核实 {delivery['unverified_impressions']}）"
        )
        for exp in delivery["exposures"]:
            blocks.append(
                f"    - {exp['exposure_id']} {exp['content_id']} @ {exp['occurred_at']} "
                f"触达 {exp['impression_count']} 核实={bool(exp['is_verified'])}"
            )
    blocks.append("\n" + lineage_footer(conn))
    return "\n".join(blocks)
