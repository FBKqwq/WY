"""
问题3：使用班组1和班组2设备，完成 A-E 五个车间整修任务；调度核与 Q1/Q2 一致（工序内同类多台并联），导出表3。
默认导出：班组1/班组2设备甘特图（`Qbase.gantt_dual`：上车间、下工序，含运输段）、各车间完工与两班组作业量柱状图。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 将 src 加入路径以便导入 Qbase
_SRC = Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from Qbase.cpsat_q2 import (
    DEFAULT_CPSAT_NUM_WORKERS,
    DEFAULT_CPSAT_TIME_LIMIT_SEC,
    cpsat_available,
    solve_q3_cpsat,
)
from Qbase.data_loader import load_device_table, load_distance_table, load_operation_table
from Qbase.exporter import export_schedule
from Qbase.parsers import (
    ExpandedOperation,
    expand_repeated_operations,
    parse_devices,
    parse_distances,
    parse_operations,
)
from Qbase.scheduler_common import ScheduleRecord, solve_problem3
from Qbase.time_utils import seconds_to_hhmmss
from Qbase.validators import validate_schedule

_WORKSHOP_COLORS = {
    "A": "#2563EB",
    "B": "#16A34A",
    "C": "#D97706",
    "D": "#DC2626",
    "E": "#7C3AED",
}

def _norm_ws(workshop: str) -> str:
    return str(workshop).strip().upper().replace("车间", "")


def _pick_cjk_font() -> str | None:
    import matplotlib.font_manager as fm

    for name in (
        "Microsoft YaHei",
        "SimHei",
        "Noto Sans CJK SC",
        "Source Han Sans SC",
        "PingFang SC",
    ):
        if name in {f.name for f in fm.fontManager.ttflist}:
            return name
    return None


def _log_q3(msg: str) -> None:
    """控制台进度。"""
    print(f"[问题3] {msg}", flush=True)


def plot_team_device_gantt(
    records: List[ScheduleRecord],
    team: int,
    makespan_sec: int,
    out_png: Path,
    title_suffix: str = "",
) -> None:
    """按班组筛选设备；甘特条上下双拼（上=车间，下=工序），见 Qbase.gantt_dual。"""
    from Qbase.gantt_dual import plot_device_gantt_dual_strip

    plot_device_gantt_dual_strip(
        records,
        makespan_sec,
        out_png,
        title=f"问题3 班组{team} 设备甘特图  Makespan = {makespan_sec}s（{seconds_to_hhmmss(makespan_sec)}）",
        title_suffix=title_suffix,
        filter_team=team,
        show_team_on_op_strip=False,
    )


def plot_workshop_team_bars(
    records: List[ScheduleRecord],
    expanded: List[ExpandedOperation],
    makespan_sec: int,
    out_png: Path,
) -> None:
    """
    上图：各车间全部工序最后完工时刻（s）。
    下图：各车间班组1/班组2 在该车间累计作业时间（duration_sec 之和，s）。
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    font = _pick_cjk_font()
    if font:
        plt.rcParams["font.sans-serif"] = [font, "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    workshops = sorted({_norm_ws(o.workshop) for o in expanded})
    finish_m: Dict[str, int] = {w: 0 for w in workshops}
    t1_sum: Dict[str, int] = {w: 0 for w in workshops}
    t2_sum: Dict[str, int] = {w: 0 for w in workshops}
    for r in records:
        wk = _norm_ws(r.workshop)
        if wk not in finish_m:
            continue
        finish_m[wk] = max(finish_m[wk], r.end_sec)
        if r.team == 1:
            t1_sum[wk] += r.duration_sec
        elif r.team == 2:
            t2_sum[wk] += r.duration_sec

    n = len(workshops)
    x = list(range(n))
    w_bar = 0.36

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), dpi=120, facecolor="#F8FAFC", sharex=True)

    colors_fin = [_WORKSHOP_COLORS.get(w, "#64748B") for w in workshops]
    ax1.bar(x, [finish_m[w] for w in workshops], width=0.55, color=colors_fin, edgecolor="white", linewidth=0.6)
    ax1.set_ylabel("最后完工时刻（s）", fontsize=10, color="#334155")
    ax1.set_title(
        f"问题3 各车间完工时刻与两班组作业量  全局 Makespan = {makespan_sec}s（{seconds_to_hhmmss(makespan_sec)}）",
        fontsize=12,
        fontweight="bold",
        color="#0F172A",
        pad=10,
    )
    ax1.axhline(makespan_sec, color="#DC2626", linestyle="--", linewidth=1.0, alpha=0.75, label="全局 Makespan")
    ax1.grid(axis="y", color="#CBD5E1", linestyle="-", linewidth=0.4, alpha=0.85)
    ax1.set_facecolor("#F1F5F9")
    for spine in ("top", "right"):
        ax1.spines[spine].set_visible(False)
    ax1.legend(loc="upper left", fontsize=8)

    x1 = [i - w_bar / 2 for i in x]
    x2 = [i + w_bar / 2 for i in x]
    ax2.bar(x1, [t1_sum[w] for w in workshops], width=w_bar, label="班组1 累计作业(s)", color="#0EA5E9", edgecolor="white")
    ax2.bar(x2, [t2_sum[w] for w in workshops], width=w_bar, label="班组2 累计作业(s)", color="#F97316", edgecolor="white")
    ax2.set_xticks(x)
    ax2.set_xticklabels([f"{w} 车间" for w in workshops], fontsize=10)
    ax2.set_ylabel("累计作业时间（s）", fontsize=10, color="#334155")
    ax2.set_xlabel("车间", fontsize=10, color="#334155")
    ax2.legend(loc="upper right", fontsize=9)
    ax2.grid(axis="y", color="#CBD5E1", linestyle="-", linewidth=0.4, alpha=0.85)
    ax2.set_facecolor("#F1F5F9")
    for spine in ("top", "right"):
        ax2.spines[spine].set_visible(False)

    plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def _plot_q3_analysis_charts(
    records: List[ScheduleRecord],
    expanded: List[ExpandedOperation],
    makespan: int,
    greedy_ms: int,
    mode: str,
    use_cpsat: bool,
    chain_stats: Optional[List[Tuple[str, int, str]]],
    out_dir: Path,
    total_device_count: int,
) -> None:
    """设备作业（按班组着色）、工序完工、CP 子链与贪心 Makespan 对比。"""
    from Qbase.schedule_charts import (
        plot_advanced_viz_bundle,
        plot_device_utilization_bars,
        plot_device_work_duration_bars,
        plot_operation_finish_timeline,
        plot_solver_makespan_comparison,
    )

    plot_device_work_duration_bars(
        records,
        makespan,
        out_dir / "表3_问题3各设备持续作业时间.png",
        title="问题3 各设备累计持续作业时间对比（班组1+2，按班组着色）",
        color_by_team=True,
    )
    plot_device_utilization_bars(
        records,
        makespan,
        out_dir / "表3_问题3各设备利用率.png",
        title="问题3 各设备利用率（作业累计 / Makespan）",
    )
    plot_operation_finish_timeline(
        records,
        expanded,
        out_dir / "表3_问题3各工序完工时刻.png",
        title="问题3 各展开工序完工时刻（五车间工艺顺序）",
    )
    if use_cpsat and chain_stats is not None:
        plot_solver_makespan_comparison(
            out_dir / "表3_问题3求解过程Makespan对比.png",
            "问题3 求解过程：贪心 baseline、CP-SAT 三子链、最终采用方案",
            greedy_ms,
            makespan,
            mode,
            chain_stats=chain_stats,
        )
    plot_advanced_viz_bundle(
        records,
        expanded,
        makespan,
        out_dir,
        "表3_问题3",
        "问题3（五车间·双班组）",
        total_device_count,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="问题3：两班组五车间调度")
    parser.add_argument(
        "--solver",
        choices=("cpsat", "greedy", "auto"),
        default="auto",
        help="auto：有 ortools 则用 CP-SAT，否则贪心（车间顺序全排列 + solve_problem1）",
    )
    parser.add_argument(
        "--time-limit",
        type=float,
        default=DEFAULT_CPSAT_TIME_LIMIT_SEC,
        help=f"CP-SAT 总时间预算（秒），默认 {DEFAULT_CPSAT_TIME_LIMIT_SEC:g}（三链分用）",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_CPSAT_NUM_WORKERS,
        help=f"CP-SAT 并行搜索线程数，默认 {DEFAULT_CPSAT_NUM_WORKERS}（适配 24 核级 CPU）",
    )
    parser.add_argument("--no-charts", action="store_true", help="不绘制甘特图与柱状图 PNG")
    args = parser.parse_args()

    _log_q3(
        f"启动 — solver={args.solver}, time_limit={args.time_limit}s, workers={args.workers}, "
        f"no_charts={args.no_charts}"
    )

    code_root = Path(__file__).resolve().parents[2]
    data_dir = code_root / "data"
    out_dir = Path(__file__).resolve().parent / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)

    op_path = data_dir / "工序流程表.xlsx"
    dev_path = data_dir / "班组配置表.xlsx"
    dist_path = data_dir / "车间距离表.xlsx"

    _log_q3(f"1/6 读取 Excel：{op_path.name}、{dev_path.name}、{dist_path.name}")
    raw_df = load_operation_table(op_path)
    dev_df = load_device_table(dev_path)
    dist_df = load_distance_table(dist_path)

    _log_q3("2/6 解析工序与距离矩阵…")
    raw_ops = parse_operations(raw_df)
    raw_ae = [r for r in raw_ops if r.workshop in {"A", "B", "C", "D", "E"}]
    raw_repeat_map = {r.raw_id: r.repeat_count for r in raw_ae}
    expanded = expand_repeated_operations(raw_ae)

    # 问题3使用班组1+班组2全部设备
    devices = [d for d in parse_devices(dev_df) if d.team in (1, 2)]
    dist_map = parse_distances(dist_df)
    _log_q3(
        f"   原始 A–E 工序 {len(raw_ae)} 条 → 展开后 {len(expanded)} 条；"
        f"班组1+2 设备共 {len(devices)} 台"
    )

    use_cpsat = args.solver == "cpsat" or (args.solver == "auto" and cpsat_available())
    if args.solver == "cpsat" and not cpsat_available():
        raise SystemExit("未安装 ortools，请执行: pip install ortools")
    _log_q3(f"3/6 求解 — ortools 可用: {cpsat_available()}；将{'启用' if use_cpsat else '跳过'} CP-SAT")

    _log_q3("   贪心（车间顺序全排列 + solve_problem1）…")
    greedy_records, greedy_ms = solve_problem3(expanded, devices, dist_map)
    _log_q3(f"   贪心完成，Makespan = {greedy_ms} s")

    chain_stats: Optional[List[Tuple[str, int, str]]] = None
    if use_cpsat:
        _log_q3(
            f"   CP-SAT 三链求解（总时限约 {args.time_limit}s，线程 {args.workers}）…"
        )
        cp_records, cp_ms, st, chain_stats = solve_q3_cpsat(
            expanded,
            devices,
            dist_map,
            time_limit_sec=args.time_limit,
            num_workers=args.workers,
            raw_repeat_map=raw_repeat_map,
        )
        if not cp_records:
            _log_q3(f"   CP-SAT 未得可行解 ({st})，采用贪心结果。")
            records, makespan, mode = greedy_records, greedy_ms, "greedy_fallback"
        elif cp_ms < greedy_ms:
            _log_q3(f"   CP-SAT 完成，Makespan = {cp_ms} s（优于贪心，采用 CP-SAT）")
            records, makespan, mode = cp_records, cp_ms, f"cpsat_{st}"
        else:
            _log_q3(f"   CP-SAT Makespan = {cp_ms} s，贪心更优，采用贪心")
            records, makespan, mode = greedy_records, greedy_ms, f"greedy_best_vs_cpsat_{st}_{cp_ms}"
    else:
        records, makespan, mode = greedy_records, greedy_ms, "greedy"

    _log_q3(f"4/6 导出表3 — 模式={mode}，调度记录 {len(records)} 条")
    out_xlsx = out_dir / "表3_问题3调度结果.xlsx"
    export_schedule(
        records,
        out_xlsx,
        makespan_sec=makespan,
        include_team=True,
        detail_sheet_name="表3_调度明细",
        summary_label="完成问题3任务的最短时长(s)",
    )

    _log_q3("5/6 约束校验并写入 validation_report.txt …")
    report = validate_schedule(expanded, records, devices, dist_map, raw_repeat_map, makespan)
    (out_dir / "validation_report.txt").write_text(report, encoding="utf-8")

    print(f"Makespan = {makespan} s  (求解模式: {mode})")
    print(f"已导出: {out_xlsx}")
    print(f"校验报告: {out_dir / 'validation_report.txt'}")

    if not args.no_charts:
        _log_q3("6/6 绘制班组甘特图与车间柱状图 …")
        try:
            g1 = out_dir / "表3_问题3甘特图_班组1.png"
            g2 = out_dir / "表3_问题3甘特图_班组2.png"
            bar_path = out_dir / "表3_问题3车间统计柱状图.png"
            plot_team_device_gantt(records, 1, makespan, g1)
            plot_team_device_gantt(records, 2, makespan, g2)
            plot_workshop_team_bars(records, expanded, makespan, bar_path)
            _log_q3("   甘特图与车间柱状图已保存")
            print(f"甘特图: {g1}")
            print(f"甘特图: {g2}")
            print(f"柱状图: {bar_path}")
        except ImportError:
            print("未安装 matplotlib，跳过甘特图与车间柱状图。请执行: pip install matplotlib")
        except OSError as e:
            print(f"甘特图/柱状图保存失败: {e}")
    else:
        _log_q3("6/6 跳过甘特图与车间柱状图 (--no-charts)")

    try:
        _plot_q3_analysis_charts(
            records,
            expanded,
            makespan,
            greedy_ms,
            mode,
            use_cpsat,
            chain_stats,
            out_dir,
            len(devices),
        )
        print(
            "分析图: 表3_问题3各设备持续作业时间.png、表3_问题3各设备利用率.png、"
            "表3_问题3各工序完工时刻.png、表3_问题3_三维调度散点.png、表3_问题3_雷达综合评价.png、"
            "表3_问题3_热力图设备时间占用.png"
            + ("、表3_问题3求解过程Makespan对比.png" if use_cpsat and chain_stats is not None else "")
        )
    except ImportError:
        print("未安装 matplotlib，跳过分析图。请执行: pip install matplotlib")
    except OSError as e:
        print(f"分析图保存失败: {e}")

    _log_q3("— 完成 —")


if __name__ == "__main__":
    main()
