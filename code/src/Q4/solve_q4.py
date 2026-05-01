"""
问题4：预算约束下搜索设备购买方案，并完成 A-E 五个车间调度。
"""
from __future__ import annotations

import sys
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Dict, List, Tuple

# 将 src 加入路径以便导入 Qbase
_SRC = Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from Qbase.data_loader import load_device_table, load_distance_table, load_operation_table
from Qbase.exporter import export_purchase_plan, export_schedule
from Qbase.parsers import Device, expand_repeated_operations, parse_devices, parse_distances, parse_operations
from Qbase.scheduler_common import solve_problem3, solve_problem4
from Qbase.validators import validate_budget, validate_schedule


def _build_device_template(devices: List[Device]) -> Dict[Tuple[int, str], Device]:
    tmpl: Dict[Tuple[int, str], Device] = {}
    for d in devices:
        key = (d.team, d.device_type)
        if key not in tmpl:
            tmpl[key] = d
    return tmpl


def _calc_purchase_cost(
    purchase_counts: Dict[Tuple[int, str], int],
    templates: Dict[Tuple[int, str], Device],
) -> int:
    total = 0
    for key, cnt in purchase_counts.items():
        if cnt <= 0:
            continue
        total += int(round(templates[key].price)) * cnt
    return total


def _expand_devices_with_purchase(
    base_devices: List[Device],
    purchase_counts: Dict[Tuple[int, str], int],
    templates: Dict[Tuple[int, str], Device],
) -> List[Device]:
    out = [deepcopy(d) for d in base_devices]
    base_cnt = defaultdict(int)
    for d in base_devices:
        base_cnt[(d.team, d.device_type)] += 1
    for (team, dtype), cnt in purchase_counts.items():
        if cnt <= 0:
            continue
        tmpl = templates[(team, dtype)]
        for i in range(1, cnt + 1):
            idx = base_cnt[(team, dtype)] + i
            out.append(
                Device(
                    team=team,
                    device_id=f"{dtype}{team}-增购{idx}",
                    device_type=dtype,
                    speed=tmpl.speed,
                    price=tmpl.price,
                    initial_location=f"班组{team}",
                )
            )
    return out


def _utilization_by_type(records, devices: List[Device], makespan: int) -> Dict[str, float]:
    if makespan <= 0:
        return {}
    dev_count = defaultdict(int)
    for d in devices:
        dev_count[d.device_type] += 1
    work = defaultdict(int)
    for r in records:
        work[r.device_type] += int(r.duration_sec)
    util = {}
    for dtype, wt in work.items():
        denom = makespan * dev_count.get(dtype, 1)
        util[dtype] = wt / denom if denom > 0 else 0.0
    return util


def _search_purchase_plan(
    expanded,
    base_devices: List[Device],
    dist_map,
    budget_limit: int,
) -> Tuple[Dict[Tuple[int, str], int], List, int, int]:
    # 基准（不购买）用于识别瓶颈设备类型
    baseline_records, baseline_makespan = solve_problem3(expanded, base_devices, dist_map)
    util = _utilization_by_type(baseline_records, base_devices, baseline_makespan)
    bottleneck_types = sorted(util.keys(), key=lambda t: util[t], reverse=True)[:4]
    if not bottleneck_types:
        bottleneck_types = sorted({d.device_type for d in base_devices})

    templates = _build_device_template(base_devices)
    purchase_counts: Dict[Tuple[int, str], int] = defaultdict(int)
    best_records = baseline_records
    best_makespan = baseline_makespan
    best_cost = 0

    # 逐步贪心加购：每一步尝试给某个班组某类设备 +1，选择工期改善最大的动作
    for _step in range(12):
        step_best = None  # (makespan, cost, team, dtype, records)
        current_cost = _calc_purchase_cost(purchase_counts, templates)
        for dtype in bottleneck_types:
            for team in (1, 2):
                key = (team, dtype)
                if key not in templates:
                    continue
                unit_cost = int(round(templates[key].price))
                if current_cost + unit_cost > budget_limit:
                    continue
                trial = dict(purchase_counts)
                trial[key] = trial.get(key, 0) + 1
                devs = _expand_devices_with_purchase(base_devices, trial, templates)
                records, ms = solve_problem4(expanded, devs, dist_map)
                cost = _calc_purchase_cost(trial, templates)
                cand = (ms, cost, team, dtype, trial, records)
                if step_best is None or (cand[0], cand[1]) < (step_best[0], step_best[1]):
                    step_best = cand

        if step_best is None:
            break
        ms, cost, team, dtype, trial, records = step_best
        # 若本步无法改进工期，则停止迭代
        if ms >= best_makespan:
            break
        purchase_counts = defaultdict(int, trial)
        best_records = records
        best_makespan = ms
        best_cost = cost
        if best_cost >= budget_limit:
            break

    return dict(purchase_counts), best_records, best_makespan, best_cost


def _build_purchase_rows(
    purchase_counts: Dict[Tuple[int, str], int],
    devices: List[Device],
) -> List[dict]:
    preferred = ["自动化输送臂", "工业清洗机", "精密灌装机", "自动传感多功能机", "高速抛光机"]
    all_types = sorted({d.device_type for d in devices})
    ordered = preferred + [t for t in all_types if t not in preferred]
    rows = []
    for dtype in ordered:
        rows.append(
            {
                "设备名称": dtype,
                "班组1购买台数": int(purchase_counts.get((1, dtype), 0)),
                "班组2购买台数": int(purchase_counts.get((2, dtype), 0)),
            }
        )
    return rows


def update_dev_doc(dev_path: Path, content: str) -> None:
    dev_path.write_text(content, encoding="utf-8")


def main() -> None:
    budget_limit = 500000
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
    base_devices = [d for d in parse_devices(dev_df) if d.team in (1, 2)]
    dist_map = parse_distances(dist_df)

    purchase_counts, records, makespan, total_cost = _search_purchase_plan(
        expanded=expanded,
        base_devices=base_devices,
        dist_map=dist_map,
        budget_limit=budget_limit,
    )
    final_devices = _expand_devices_with_purchase(
        base_devices=base_devices,
        purchase_counts=purchase_counts,
        templates=_build_device_template(base_devices),
    )

    out_table4 = out_dir / "表4_问题4调度结果.xlsx"
    export_schedule(
        records=records,
        out_path=out_table4,
        makespan_sec=makespan,
        include_team=True,
        detail_sheet_name="表4_调度明细",
        summary_label="完成问题4任务的最短时长(s)",
    )

    purchase_rows = _build_purchase_rows(purchase_counts, base_devices)
    out_table5 = out_dir / "表5_问题4购买方案.xlsx"
    export_purchase_plan(
        rows=purchase_rows,
        out_path=out_table5,
        total_cost=total_cost,
    )

    report = validate_schedule(expanded, records, final_devices, dist_map, raw_repeat_map, makespan)
    budget_errs = validate_budget(total_cost=total_cost, budget_limit=budget_limit)
    budget_text = "通过\n" if not budget_errs else "\n".join(budget_errs) + "\n"
    report = report + "\n## 预算检查\n" + budget_text
    (out_dir / "validation_report.txt").write_text(report, encoding="utf-8")

    print(f"Makespan = {makespan} s")
    print(f"购买总费用 = {total_cost} 元")
    print(f"已导出: {out_table4}")
    print(f"已导出: {out_table5}")
    print(f"校验报告: {out_dir / 'validation_report.txt'}")


if __name__ == "__main__":
    main()
