"""
问题 1：班组 1 独立完成 A 车间全部整修任务，贪心调度求解并导出表 1。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 将 src 加入路径以便导入 Qbase
_SRC = Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from Qbase.data_loader import load_device_table, load_distance_table, load_operation_table
from Qbase.exporter import export_schedule
from Qbase.parsers import (
    expand_repeated_operations,
    parse_devices,
    parse_distances,
    parse_operations,
)
from Qbase.scheduler_common import solve_problem1
from Qbase.time_utils import seconds_to_hhmmss
from Qbase.validators import validate_schedule


def _plot_q1_analysis_charts(
    records,
    expanded,
    makespan: int,
    out_dir: Path,
    total_device_count: int,
) -> None:
    """各设备作业量、利用率、工序完工时刻（展示贪心枚举并联后的结果结构）。"""
    from Qbase.schedule_charts import (
        plot_advanced_viz_bundle,
        plot_device_utilization_bars,
        plot_device_work_duration_bars,
        plot_operation_finish_timeline,
    )

    plot_device_work_duration_bars(
        records,
        makespan,
        out_dir / "表1_问题1各设备持续作业时间.png",
        title="问题1 各设备累计持续作业时间对比（A车间）",
        color_by_team=False,
    )
    plot_device_utilization_bars(
        records,
        makespan,
        out_dir / "表1_问题1各设备利用率.png",
        title="问题1 各设备利用率（作业累计 / Makespan）",
    )
    plot_operation_finish_timeline(
        records,
        expanded,
        out_dir / "表1_问题1各工序完工时刻.png",
        title="问题1 各展开工序完工时刻（按工艺顺序）",
    )
    plot_advanced_viz_bundle(
        records,
        expanded,
        makespan,
        out_dir,
        "表1_问题1",
        "问题1（A车间）",
        total_device_count,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="问题1：A车间调度")
    parser.add_argument("--no-gantt", action="store_true", help="不绘制设备甘特图 PNG")
    args = parser.parse_args()

    code_root = Path(__file__).resolve().parents[2]
    data_dir = code_root / "data"
    out_dir = Path(__file__).resolve().parent / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)

    op_path = data_dir / "工序流程表.xlsx"
    dev_path = data_dir / "班组配置表.xlsx"
    dist_path = data_dir / "车间距离表.xlsx"

    raw_df = load_operation_table(op_path)
    dev_df = load_device_table(dev_path)
    dist_df = load_distance_table(dist_path)

    raw_ops = parse_operations(raw_df)
    raw_a = [r for r in raw_ops if r.workshop == "A"]
    raw_repeat_map = {r.raw_id: r.repeat_count for r in raw_a}
    expanded = expand_repeated_operations(raw_a)
    devices = [d for d in parse_devices(dev_df) if d.team == 1]
    dist_map = parse_distances(dist_df)

    records, makespan = solve_problem1(expanded, devices, dist_map)

    out_xlsx = out_dir / "表1_问题1调度结果.xlsx"
    export_schedule(records, out_xlsx, makespan_sec=makespan, include_team=False)

    report = validate_schedule(expanded, records, devices, dist_map, raw_repeat_map, makespan)
    (out_dir / "validation_report.txt").write_text(report, encoding="utf-8")

    gantt_path = out_dir / "表1_问题1甘特图.png"
    if not args.no_gantt:
        try:
            from Qbase.gantt_dual import plot_device_gantt_dual_strip

            # 单车间：仅首道工序（最小 sequence_key）画「班组1→A」运输条，后续工序同车间不再画运输段
            min_sk = min(o.sequence_key for o in expanded)
            transport_ops = {o.op_id for o in expanded if o.sequence_key == min_sk}
            plot_device_gantt_dual_strip(
                records,
                makespan,
                gantt_path,
                title=f"问题1 设备甘特图（A车间）  Makespan = {makespan}s（{seconds_to_hhmmss(makespan)}）",
                transport_strip_op_ids=transport_ops,
            )
        except ImportError:
            print("未安装 matplotlib，跳过甘特图。请执行: pip install matplotlib")
        except OSError as e:
            print(f"甘特图保存失败: {e}")

    try:
        _plot_q1_analysis_charts(records, expanded, makespan, out_dir, len(devices))
        print(
            "分析图: 表1_问题1各设备持续作业时间.png、表1_问题1各设备利用率.png、表1_问题1各工序完工时刻.png、"
            "表1_问题1_三维调度散点.png、表1_问题1_雷达综合评价.png、表1_问题1_热力图设备时间占用.png"
        )
    except ImportError:
        print("未安装 matplotlib，跳过分析图。请执行: pip install matplotlib")
    except OSError as e:
        print(f"分析图保存失败: {e}")

    print(f"Makespan = {makespan} s")
    print(f"已导出: {out_xlsx}")
    print(f"校验报告: {out_dir / 'validation_report.txt'}")
    if not args.no_gantt:
        print(f"甘特图: {gantt_path}")


if __name__ == "__main__":
    main()
