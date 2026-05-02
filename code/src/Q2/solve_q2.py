"""
问题2：仅使用班组1设备，完成 A-E 五个车间整修任务；默认 CP-SAT（与 othersolve 同思路），可选贪心回退。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# 将 src 加入路径以便导入 Qbase
_SRC = Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from Qbase.cpsat_q2 import (
    DEFAULT_CPSAT_NUM_WORKERS,
    DEFAULT_CPSAT_TIME_LIMIT_SEC,
    cpsat_available,
    solve_q2_cpsat,
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
from Qbase.scheduler_common import ScheduleRecord, solve_problem2
from Qbase.time_utils import seconds_to_hhmmss
from Qbase.validators import validate_schedule


# 车间颜色（与工序所在车间一致，便于区分）
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


def plot_device_gantt(
    records: List[ScheduleRecord],
    makespan_sec: int,
    out_png: Path,
    title_suffix: str = "",
) -> None:
    """设备甘特图（上下双拼：上=车间，下=工序），实现见 Qbase.gantt_dual。"""
    from Qbase.gantt_dual import plot_device_gantt_dual_strip

    plot_device_gantt_dual_strip(
        records,
        makespan_sec,
        out_png,
        title=f"问题2 设备甘特图  Makespan = {makespan_sec}s（{seconds_to_hhmmss(makespan_sec)}）",
        title_suffix=title_suffix,
    )


def plot_workshop_bars_q2(
    records: List[ScheduleRecord],
    expanded: List[ExpandedOperation],
    makespan_sec: int,
    out_png: Path,
) -> None:
    """
    上：各车间全部工序最后完工时刻（s）；
    下：班组1在各车间累计作业时间（duration_sec 之和，s）。
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    font = _pick_cjk_font()
    if font:
        plt.rcParams["font.sans-serif"] = [font, "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    workshops = sorted({_norm_ws(o.workshop) for o in expanded})
    finish_m: dict[str, int] = {w: 0 for w in workshops}
    t1_sum: dict[str, int] = {w: 0 for w in workshops}
    for r in records:
        wk = _norm_ws(r.workshop)
        if wk not in finish_m:
            continue
        finish_m[wk] = max(finish_m[wk], r.end_sec)
        if r.team == 1:
            t1_sum[wk] += r.duration_sec

    n = len(workshops)
    x = list(range(n))

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), dpi=120, facecolor="#F8FAFC", sharex=True)
    colors_fin = [_WORKSHOP_COLORS.get(w, "#64748B") for w in workshops]
    ax1.bar(x, [finish_m[w] for w in workshops], width=0.55, color=colors_fin, edgecolor="white", linewidth=0.6)
    ax1.set_ylabel("最后完工时刻（s）", fontsize=10, color="#334155")
    ax1.set_title(
        f"问题2 各车间完工与班组1作业量  全局 Makespan = {makespan_sec}s（{seconds_to_hhmmss(makespan_sec)}）",
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

    ax2.bar(x, [t1_sum[w] for w in workshops], width=0.55, label="班组1 累计作业(s)", color="#0EA5E9", edgecolor="white")
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


def _plot_q2_analysis_charts(
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
    """设备作业、工序完工、贪心与 CP 子链 Makespan 对比。"""
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
        out_dir / "表2_问题2各设备持续作业时间.png",
        title="问题2 各设备累计持续作业时间对比（班组1）",
        color_by_team=False,
    )
    plot_device_utilization_bars(
        records,
        makespan,
        out_dir / "表2_问题2各设备利用率.png",
        title="问题2 各设备利用率（作业累计 / Makespan）",
    )
    plot_operation_finish_timeline(
        records,
        expanded,
        out_dir / "表2_问题2各工序完工时刻.png",
        title="问题2 各展开工序完工时刻（五车间工艺顺序）",
    )
    if use_cpsat and chain_stats is not None:
        plot_solver_makespan_comparison(
            out_dir / "表2_问题2求解过程Makespan对比.png",
            "问题2 求解过程：贪心 baseline、CP-SAT 三子链、最终采用方案",
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
        "表2_问题2",
        "问题2（五车间·班组1）",
        total_device_count,
    )


def _log_q2(msg: str) -> None:
    """控制台进度（UTF-8 控制台下可读）。"""
    print(f"[问题2] {msg}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="问题2：五车间调度")
    parser.add_argument(
        "--solver",
        choices=("cpsat", "greedy", "auto"),
        default="auto",
        help="auto：有 ortools 则用 CP-SAT，否则贪心",
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
    parser.add_argument(
        "--no-gantt",
        action="store_true",
        help="不绘制甘特图与车间柱状图 PNG",
    )
    args = parser.parse_args()

    _log_q2(
        f"启动 — solver={args.solver}, time_limit={args.time_limit}s, workers={args.workers}, "
        f"no_gantt={args.no_gantt}"
    )

    code_root = Path(__file__).resolve().parents[2]
    data_dir = code_root / "data"
    out_dir = Path(__file__).resolve().parent / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)

    op_path = data_dir / "工序流程表.xlsx"
    dev_path = data_dir / "班组配置表.xlsx"
    dist_path = data_dir / "车间距离表.xlsx"

    _log_q2(f"1/6 读取 Excel：{op_path.name}、{dev_path.name}、{dist_path.name}")
    raw_df = load_operation_table(op_path)
    dev_df = load_device_table(dev_path)
    dist_df = load_distance_table(dist_path)

    _log_q2("2/6 解析工序与距离矩阵…")
    raw_ops = parse_operations(raw_df)
    raw_ae = [r for r in raw_ops if r.workshop in {"A", "B", "C", "D", "E"}]
    raw_repeat_map = {r.raw_id: r.repeat_count for r in raw_ae}
    expanded = expand_repeated_operations(raw_ae)
    devices = [d for d in parse_devices(dev_df) if d.team == 1]
    dist_map = parse_distances(dist_df)
    _log_q2(
        f"   原始 A–E 工序 {len(raw_ae)} 条 → 展开后 {len(expanded)} 条；班组1 设备 {len(devices)} 台"
    )

    use_cpsat = args.solver == "cpsat" or (args.solver == "auto" and cpsat_available())
    if args.solver == "cpsat" and not cpsat_available():
        raise SystemExit("未安装 ortools，请执行: pip install ortools")
    _log_q2(f"3/6 求解 — ortools 可用: {cpsat_available()}；将{'启用' if use_cpsat else '跳过'} CP-SAT")

    _log_q2("   贪心（车间顺序全排列 + solve_problem1）…")
    greedy_records, greedy_ms = solve_problem2(expanded, devices, dist_map)
    _log_q2(f"   贪心完成，Makespan = {greedy_ms} s")

    chain_stats: Optional[List[Tuple[str, int, str]]] = None
    if use_cpsat:
        _log_q2(
            f"   CP-SAT 三链求解（总时限约 {args.time_limit}s，线程 {args.workers}）…"
        )
        cp_records, cp_ms, st, chain_stats = solve_q2_cpsat(
            expanded,
            devices,
            dist_map,
            time_limit_sec=args.time_limit,
            num_workers=args.workers,
            raw_repeat_map=raw_repeat_map,
        )
        if not cp_records:
            _log_q2(f"   CP-SAT 未得可行解 ({st})，采用贪心结果。")
            records, makespan, mode = greedy_records, greedy_ms, "greedy_fallback"
        elif cp_ms < greedy_ms:
            _log_q2(f"   CP-SAT 完成，Makespan = {cp_ms} s（优于贪心，采用 CP-SAT）")
            records, makespan, mode = cp_records, cp_ms, f"cpsat_{st}"
        else:
            _log_q2(f"   CP-SAT Makespan = {cp_ms} s，贪心更优，采用贪心")
            records, makespan, mode = greedy_records, greedy_ms, f"greedy_best_vs_cpsat_{st}_{cp_ms}"
    else:
        records, makespan, mode = greedy_records, greedy_ms, "greedy"

    _log_q2(f"4/6 导出表2 — 模式={mode}，调度记录 {len(records)} 条")
    out_xlsx = out_dir / "表2_问题2调度结果.xlsx"
    try:
        export_schedule(
            records,
            out_xlsx,
            makespan_sec=makespan,
            include_team=False,
            detail_sheet_name="表2_调度明细",
            summary_label="完成问题2任务的最短时长(s)",
        )
    except PermissionError:
        fallback_xlsx = out_dir / "表2_问题2调度结果_新结果.xlsx"
        _log_q2(f"   目标 Excel 被占用，改写入备用文件：{fallback_xlsx.name}")
        export_schedule(
            records,
            fallback_xlsx,
            makespan_sec=makespan,
            include_team=False,
            detail_sheet_name="表2_调度明细",
            summary_label="完成问题2任务的最短时长(s)",
        )
        out_xlsx = fallback_xlsx

    _log_q2("5/6 约束校验并写入 validation_report.txt …")
    report = validate_schedule(expanded, records, devices, dist_map, raw_repeat_map, makespan)
    (out_dir / "validation_report.txt").write_text(report, encoding="utf-8")

    print(f"Makespan = {makespan} s  (求解模式: {mode})")
    print(f"已导出: {out_xlsx}")
    print(f"校验报告: {out_dir / 'validation_report.txt'}")

    gantt_path = out_dir / "表2_问题2甘特图.png"
    bar_path = out_dir / "表2_问题2车间统计柱状图.png"
    if not args.no_gantt:
        _log_q2("6/6 绘制甘特图与车间柱状图 …")
        try:
            plot_device_gantt(
                records,
                makespan,
                gantt_path,
                title_suffix=f"  [{mode}]",
            )
            plot_workshop_bars_q2(records, expanded, makespan, bar_path)
            _log_q2("   甘特图与车间柱状图已保存")
            print(f"甘特图: {gantt_path}")
            print(f"柱状图: {bar_path}")
        except ImportError:
            print("未安装 matplotlib，跳过甘特图与车间柱状图。请执行: pip install matplotlib")
        except OSError as e:
            print(f"甘特图/柱状图保存失败: {e}")
    else:
        _log_q2("6/6 跳过甘特图与车间柱状图 (--no-gantt)")

    try:
        _plot_q2_analysis_charts(
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
            "分析图: 表2_问题2各设备持续作业时间.png、表2_问题2各设备利用率.png、"
            "表2_问题2各工序完工时刻.png、表2_问题2_三维调度散点.png、表2_问题2_雷达综合评价.png、"
            "表2_问题2_热力图设备时间占用.png"
            + ("、表2_问题2求解过程Makespan对比.png" if use_cpsat and chain_stats is not None else "")
        )
    except ImportError:
        print("未安装 matplotlib，跳过分析图。请执行: pip install matplotlib")
    except OSError as e:
        print(f"分析图保存失败: {e}")

    _log_q2("— 完成 —")


if __name__ == "__main__":
    main()
