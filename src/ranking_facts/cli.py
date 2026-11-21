"""命令行入口：建库、历史时点查询与治理报表。

用法示例：
    python -m ranking_facts.cli rebuild --db data/warehouse.sqlite3
    python -m ranking_facts.cli scopes --as-of 2026-05-03T12:00:00Z
    python -m ranking_facts.cli report --name impact
"""

from __future__ import annotations

import argparse
from pathlib import Path

from . import reports
from .db import connect
from .ingestion import build_warehouse, load_csv_sources, rebuild_curated
from .queries import (
    bindings_as_of,
    deployments_running,
    exposures_known,
    scopes_as_of,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "data" / "warehouse.sqlite3"
DEFAULT_SOURCES = ROOT / "sources"


def _add_db_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help=f"SQLite 数仓路径（默认 {DEFAULT_DB}）")
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES,
                        help=f"合成 CSV 目录（默认 {DEFAULT_SOURCES}）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ranking_facts",
        description="推荐治理干预双时间数仓：政策范围 / 配置计划 / 真实分发三层分离",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="仅把 sources/ CSV 导入原始落地层")
    _add_db_arg(p_import)

    p_rebuild = sub.add_parser("rebuild", help="导入 CSV 并（重新）构建策展层")
    _add_db_arg(p_rebuild)

    p_refresh = sub.add_parser("refresh-curated", help="不重复导入，仅从落地层重建策展层")
    _add_db_arg(p_refresh)

    p_scopes = sub.add_parser("scopes", help="按历史时点查询政策允许范围")
    _add_db_arg(p_scopes)
    p_scopes.add_argument("--as-of", required=True, help="业务时间点（ISO 8601 UTC）")
    p_scopes.add_argument("--known-at", help="系统获知截止时间；缺省表示当前全量认知")

    p_bindings = sub.add_parser("bindings", help="按历史时点查询策略授权配置（计划层）")
    _add_db_arg(p_bindings)
    p_bindings.add_argument("--as-of", required=True)
    p_bindings.add_argument("--known-at")

    p_running = sub.add_parser("running", help="按历史时点查询实际在运行的投放（真实层）")
    _add_db_arg(p_running)
    p_running.add_argument("--as-of", required=True)
    p_running.add_argument("--known-at")

    p_exposures = sub.add_parser("exposures", help="按系统获知截止时间查询真实曝光")
    _add_db_arg(p_exposures)
    p_exposures.add_argument("--known-at", required=True)
    p_exposures.add_argument("--verified-only", action="store_true")

    p_report = sub.add_parser("report", help="输出治理报表")
    _add_db_arg(p_report)
    p_report.add_argument(
        "--name", required=True,
        choices=["impact", "compliance", "recovery", "tasks", "layers", "scopes"],
    )
    p_report.add_argument("--as-of", help="name=scopes 时的业务时间点")
    p_report.add_argument("--known-at", help="系统获知截止时间")
    p_report.add_argument("--status", help="name=tasks 时按状态过滤")

    p_demo = sub.add_parser("demo", help="端到端演示：建库并依次输出关键报表与时点对比")
    _add_db_arg(p_demo)
    return parser


def _print_rows(rows: list) -> None:
    if not rows:
        print("（无记录）")
        return
    headers = rows[0].keys()
    print(reports.render_table(
        list(headers),
        [[row[key] for key in headers] for row in rows],
    ))


def _run_demo(conn) -> None:
    print("=" * 78)
    print("演示 1｜三层视图：政策允许范围 / 配置计划 / 真实分发不可互相代替")
    print("=" * 78)
    print(reports.report_intervention_view(conn))

    print("\n" + "=" * 78)
    print("演示 2｜双时间时点对比：S-12 于 2026-05-04T12:00Z 撤销，撤销事件 12:00 才登记")
    print("=" * 78)
    check_time = "2026-05-04T12:30:00Z"
    print("\n[业务时点 12:30，系统认知截止 11:00] 撤销尚未登记，系统仍认为范围 active：")
    _print_rows(scopes_as_of(conn, check_time, "2026-05-04T11:00:00Z"))
    print("\n[同一业务时点 12:30，系统认知截止 13:00] 撤销已登记，结论变为 withdrawn：")
    _print_rows(scopes_as_of(conn, check_time, "2026-05-04T13:00:00Z"))
    print("\n[业务时点 11:00（撤销前），系统认知截止 13:00] 历史事实不被重写，仍为 active：")
    _print_rows(scopes_as_of(conn, "2026-05-04T11:00:00Z", "2026-05-04T13:00:00Z"))

    print("\n" + "=" * 78)
    print("演示 3｜迟到配置：DEP-4 于 05-02T01:00Z 启动，B-2 直到 05-03T00:10Z 才入库")
    print("=" * 78)
    early = "2026-05-02T12:00:00Z"
    print(f"\n[系统认知截止 {early}] 实际在运行的投放：")
    _print_rows(deployments_running(conn, early, early))
    print(f"\n[同一时点] 系统已知的策略授权配置（DEP-4 无授权记录）：")
    _print_rows(bindings_as_of(conn, early, early))

    print("\n" + "=" * 78)
    print("演示 4｜不可变曝光：EXP-32 迟到待核实，EXP-33 保留在失败的回滚窗口内")
    print("=" * 78)
    before_late = "2026-05-04T00:00:00Z"
    print(f"\n[系统认知截止 {before_late}] 已入库曝光：")
    _print_rows(exposures_known(conn, before_late))
    after_late = "2026-05-05T12:00:00Z"
    print(f"\n[系统认知截止 {after_late}] 已入库曝光（EXP-32 出现但仍待核实）：")
    _print_rows(exposures_known(conn, after_late))

    print("\n" + "=" * 78)
    print("演示 5｜实际影响、合规比对与恢复核查")
    print("=" * 78)
    print()
    print(reports.report_impact(conn))
    print()
    print(reports.report_compliance(conn))
    print()
    print(reports.report_recovery(conn))
    print()
    print(reports.report_tasks(conn))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    conn = connect(args.db)
    try:
        if args.command == "import":
            load_csv_sources(conn, args.sources)
            print(f"原始落地层已导入：{args.db}")
            return 0
        if args.command == "rebuild":
            build_warehouse(conn, args.sources)
            print(f"数仓构建完成（原始层 + 双时间策展层）：{args.db}")
            return 0
        if args.command == "refresh-curated":
            rebuild_curated(conn)
            print(f"策展层已从落地层重建：{args.db}")
            return 0
        if args.command == "scopes":
            print(reports.report_scopes(conn, args.as_of, args.known_at))
            return 0
        if args.command == "bindings":
            _print_rows(bindings_as_of(conn, args.as_of, args.known_at))
            return 0
        if args.command == "running":
            _print_rows(deployments_running(conn, args.as_of, args.known_at))
            return 0
        if args.command == "exposures":
            _print_rows(exposures_known(conn, args.known_at, args.verified_only))
            return 0
        if args.command == "report":
            if args.name == "impact":
                print(reports.report_impact(conn))
            elif args.name == "compliance":
                print(reports.report_compliance(conn))
            elif args.name == "recovery":
                print(reports.report_recovery(conn))
            elif args.name == "tasks":
                print(reports.report_tasks(conn, args.status))
            elif args.name == "layers":
                print(reports.report_intervention_view(conn))
            elif args.name == "scopes":
                if not args.as_of:
                    raise SystemExit("report --name scopes 需要 --as-of 参数")
                print(reports.report_scopes(conn, args.as_of, args.known_at))
            return 0
        if args.command == "demo":
            _run_demo(conn)
            return 0
    finally:
        conn.close()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
