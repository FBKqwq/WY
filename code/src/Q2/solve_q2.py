"""
问题2：仅使用班组1设备，完成 A-E 五个车间整修任务，贪心调度求解并导出表2。
"""
from __future__ import annotations

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
from Qbase.scheduler_common import solve_problem2
from Qbase.validators import validate_schedule


def main() -> None:
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
    raw_ae = [r for r in raw_ops if r.workshop in {"A", "B", "C", "D", "E"}]
    raw_repeat_map = {r.raw_id: r.repeat_count for r in raw_ae}
    expanded = expand_repeated_operations(raw_ae)
    devices = [d for d in parse_devices(dev_df) if d.team == 1]
    dist_map = parse_distances(dist_df)

    records, makespan = solve_problem2(expanded, devices, dist_map)

    out_xlsx = out_dir / "表2_问题2调度结果.xlsx"
    export_schedule(
        records,
        out_xlsx,
        makespan_sec=makespan,
        include_team=False,
        detail_sheet_name="表2_调度明细",
        summary_label="完成问题2任务的最短时长(s)",
    )

    report = validate_schedule(expanded, records, devices, dist_map, raw_repeat_map, makespan)
    (out_dir / "validation_report.txt").write_text(report, encoding="utf-8")

    print(f"Makespan = {makespan} s")
    print(f"已导出: {out_xlsx}")
    print(f"校验报告: {out_dir / 'validation_report.txt'}")


if __name__ == "__main__":
    main()
