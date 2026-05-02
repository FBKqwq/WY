"""
问题4：预算约束下搜索设备购买方案，并完成 A-E 五个车间调度。
"""
from __future__ import annotations

import sys
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

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


def _frozen_counts(purchase_counts: Dict[Tuple[int, str], int]) -> Tuple[Tuple[Tuple[int, str], int], ...]:
    return tuple(sorted((k, int(v)) for k, v in purchase_counts.items() if v > 0))


def _evaluate_plan(
    expanded,
    base_devices: List[Device],
    purchase_counts: Dict[Tuple[int, str], int],
    templates: Dict[Tuple[int, str], Device],
    dist_map,
) -> Tuple[List, int, int]:
    """返回 (调度记录, makespan, 购买总费用)。"""
    devs = _expand_devices_with_purchase(base_devices, purchase_counts, templates)
    records, ms = solve_problem4(expanded, devs, dist_map)
    cost = _calc_purchase_cost(purchase_counts, templates)
    return records, ms, cost


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


def _greedy_multistart(
    expanded,
    base_devices: List[Device],
    dist_map,
    budget_limit: int,
    templates: Dict[Tuple[int, str], Device],
    bottleneck_types: List[str],
    max_steps: int,
) -> Tuple[Dict[Tuple[int, str], int], List, int, int]:
    """多起点贪心：瓶颈类型顺序轮换，每步在全部 (班组, 类型) 上加购 1 台并调度，直到预算或步数上限。"""
    all_types = sorted({d.device_type for d in base_devices})
    orderings: List[List[str]] = []
    # 高利用率类型优先 + 全类型兜底
    orderings.append(list(bottleneck_types) + [t for t in all_types if t not in bottleneck_types])
    orderings.append(list(all_types))
    for rot in range(min(5, len(bottleneck_types))):
        orderings.append(bottleneck_types[rot:] + bottleneck_types[:rot] + [t for t in all_types if t not in bottleneck_types])

    best_pc: Dict[Tuple[int, str], int] = {}
    best_records: List = []
    best_ms = 10**18
    best_cost = 0

    for type_order in orderings:
        purchase_counts: Dict[Tuple[int, str], int] = defaultdict(int)
        records, ms, _cost0 = _evaluate_plan(
            expanded, base_devices, dict(purchase_counts), templates, dist_map
        )
        cost = 0
        cur_records, cur_ms, cur_cost = records, ms, cost

        for _ in range(max_steps):
            step_best: Optional[Tuple[int, int, Tuple[int, str], Dict, List]] = None
            for dtype in type_order:
                for team in (1, 2):
                    key = (team, dtype)
                    if key not in templates:
                        continue
                    unit_cost = int(round(templates[key].price))
                    if cur_cost + unit_cost > budget_limit:
                        continue
                    trial = dict(purchase_counts)
                    trial[key] = trial.get(key, 0) + 1
                    rec, m, c = _evaluate_plan(expanded, base_devices, trial, templates, dist_map)
                    cand = (m, c, key, trial, rec)
                    if step_best is None or (cand[0], cand[1]) < (step_best[0], step_best[1]):
                        step_best = cand
            if step_best is None:
                break
            m, c, _key, trial, rec = step_best
            if m >= cur_ms:
                break
            purchase_counts = defaultdict(int, trial)
            cur_records, cur_ms, cur_cost = rec, m, c

        if (cur_ms, cur_cost) < (best_ms, best_cost):
            best_ms, best_cost = cur_ms, cur_cost
            best_records = cur_records
            best_pc = dict(purchase_counts)

    return dict(best_pc), best_records, best_ms, best_cost


def _dfs_evaluate_purchases(
    expanded,
    base_devices: List[Device],
    dist_map,
    budget_limit: int,
    templates: Dict[Tuple[int, str], Device],
    keys: List[Tuple[int, str]],
    unit_costs: List[int],
    idx: int,
    budget_left: int,
    counts: List[int],
    max_total_machines: int,
    current_total: int,
    best_holder: List,  # [best_pc dict, best_ms, best_cost, best_records]
) -> None:
    """DFS 叶节点直接调度评估，仅更新全局最优（不缓存全部叶节点，避免内存与时间爆炸）。"""
    if idx == len(keys):
        pc = {keys[i]: counts[i] for i in range(len(keys)) if counts[i] > 0}
        rec, ms, cost = _evaluate_plan(expanded, base_devices, pc, templates, dist_map)
        if best_holder[0] is None or (ms, cost) < (best_holder[1], best_holder[2]):
            best_holder[0] = dict(pc)
            best_holder[1] = ms
            best_holder[2] = cost
            best_holder[3] = rec
        return
    if current_total > max_total_machines:
        return
    price = unit_costs[idx]
    max_c = min(budget_left // price, max_total_machines - current_total) if price > 0 else 0
    for c in range(0, max_c + 1):
        counts[idx] = c
        spend = c * price
        _dfs_evaluate_purchases(
            expanded,
            base_devices,
            dist_map,
            budget_limit,
            templates,
            keys,
            unit_costs,
            idx + 1,
            budget_left - spend,
            counts,
            max_total_machines,
            current_total + c,
            best_holder,
        )
    counts[idx] = 0


def _beam_search_purchase(
    expanded,
    base_devices: List[Device],
    dist_map,
    budget_limit: int,
    templates: Dict[Tuple[int, str], Device],
    seed_states: Iterable[Tuple[Dict[Tuple[int, str], int], List, int, int]],
    beam_width: int,
    max_layers: int,
) -> Tuple[Dict[Tuple[int, str], int], List, int, int]:
    """
    束搜索：每层从当前束中尝试每种 (班组, 类型) 加购 1 台，按 (makespan, cost) 保留若干状态。
    排序时在同 makespan 下保留 cost 更小者（剩余预算更大），利于后续继续加购。
    """
    keys = sorted(templates.keys())
    beam: List[Tuple[Dict[Tuple[int, str], int], List, int, int]] = list(seed_states)
    if not beam:
        rec0, ms0, c0 = _evaluate_plan(expanded, base_devices, {}, templates, dist_map)
        beam = [({}, rec0, ms0, c0)]

    def _sort_key(item: Tuple[Dict, List, int, int]) -> Tuple[int, int]:
        _pc, _r, ms, cost = item
        return (ms, cost)

    best = min(beam, key=_sort_key)

    for _layer in range(max_layers):
        children: List[Tuple[Dict[Tuple[int, str], int], List, int, int]] = []
        for purchase_counts, _rec, ms, cost in beam:
            for key in keys:
                unit = int(round(templates[key].price))
                if cost + unit > budget_limit:
                    continue
                trial = defaultdict(int, purchase_counts)
                trial[key] += 1
                trial_d = dict(trial)
                rec, m, c = _evaluate_plan(expanded, base_devices, trial_d, templates, dist_map)
                children.append((trial_d, rec, m, c))

        if not children:
            break

        children.sort(key=_sort_key)
        seen: Set[Tuple[Tuple[Tuple[int, str], int], ...]] = set()
        new_beam: List[Tuple[Dict, List, int, int]] = []
        for item in children:
            fn = _frozen_counts(item[0])
            if fn in seen:
                continue
            seen.add(fn)
            new_beam.append(item)
            if len(new_beam) >= beam_width:
                break

        cand_best = min(new_beam, key=_sort_key)
        if _sort_key(cand_best) < _sort_key(best):
            best = cand_best
        beam = new_beam

    return best


def _hill_climb_add(
    expanded,
    base_devices: List[Device],
    dist_map,
    budget_limit: int,
    templates: Dict[Tuple[int, str], Device],
    purchase_counts: Dict[Tuple[int, str], int],
    records: List,
    makespan: int,
    cost: int,
) -> Tuple[Dict[Tuple[int, str], int], List, int, int]:
    """在预算内反复尝试任意位置 +1 台，直到单步无法严格缩短工期。"""
    keys = sorted(templates.keys())
    pc = defaultdict(int, purchase_counts)
    cur_r, cur_ms, cur_c = records, makespan, cost
    improved = True
    while improved:
        improved = False
        for key in keys:
            unit = int(round(templates[key].price))
            if cur_c + unit > budget_limit:
                continue
            trial = dict(pc)
            trial[key] = trial.get(key, 0) + 1
            rec, m, c = _evaluate_plan(expanded, base_devices, trial, templates, dist_map)
            if m < cur_ms or (m == cur_ms and c < cur_c):
                pc = defaultdict(int, trial)
                cur_r, cur_ms, cur_c = rec, m, c
                improved = True
                break
    return dict(pc), cur_r, cur_ms, cur_c


def _search_purchase_plan(
    expanded,
    base_devices: List[Device],
    dist_map,
    budget_limit: int,
) -> Tuple[Dict[Tuple[int, str], int], List, int, int]:
    baseline_records, baseline_makespan = solve_problem3(expanded, base_devices, dist_map)
    util = _utilization_by_type(baseline_records, base_devices, baseline_makespan)
    bottleneck_types = sorted(util.keys(), key=lambda t: util[t], reverse=True)
    if not bottleneck_types:
        bottleneck_types = sorted({d.device_type for d in base_devices})

    templates = _build_device_template(base_devices)
    keys = sorted(templates.keys())
    unit_costs = [int(round(templates[k].price)) for k in keys]

    # 1) 枚举：总增购台数不超过上界时遍历全部预算可行组合（叶上直接调度）
    # 增购总台数上界：与 50 万预算、最便宜单价组合后约 14 台；枚举 8 台内可覆盖主要组合且控制运行时间
    max_machines = 8
    best_holder: List = [None, 10**18, 10**18, baseline_records]
    _dfs_evaluate_purchases(
        expanded,
        base_devices,
        dist_map,
        budget_limit,
        templates,
        keys,
        unit_costs,
        0,
        budget_limit,
        [0] * len(keys),
        max_machines,
        0,
        best_holder,
    )
    best_pc = dict(best_holder[0]) if best_holder[0] is not None else {}
    best_ms = int(best_holder[1]) if best_holder[0] is not None else baseline_makespan
    best_cost = int(best_holder[2]) if best_holder[0] is not None else 0
    best_records = best_holder[3]

    # 2) 束搜索：从「不购买」与「枚举层最优」出发扩展（层内会生成大量近邻）
    seeds: List[Tuple[Dict[Tuple[int, str], int], List, int, int]] = [
        ({}, baseline_records, baseline_makespan, 0),
        (dict(best_pc), best_records, best_ms, best_cost),
    ]
    beam_pc, beam_rec, beam_ms, beam_cost = _beam_search_purchase(
        expanded,
        base_devices,
        dist_map,
        budget_limit,
        templates,
        seeds,
        beam_width=72,
        max_layers=28,
    )
    if (beam_ms, beam_cost) < (best_ms, best_cost):
        best_pc, best_records, best_ms, best_cost = beam_pc, beam_rec, beam_ms, beam_cost

    # 3) 多起点贪心（与束搜索互补）
    g_pc, g_rec, g_ms, g_cost = _greedy_multistart(
        expanded,
        base_devices,
        dist_map,
        budget_limit,
        templates,
        bottleneck_types[:6] if len(bottleneck_types) >= 6 else bottleneck_types,
        max_steps=18,
    )
    if (g_ms, g_cost) < (best_ms, best_cost):
        best_pc, best_records, best_ms, best_cost = g_pc, g_rec, g_ms, g_cost

    # 4) 爬山：在当前最优上继续尝试加购
    h_pc, h_rec, h_ms, h_cost = _hill_climb_add(
        expanded,
        base_devices,
        dist_map,
        budget_limit,
        templates,
        best_pc,
        best_records,
        best_ms,
        best_cost,
    )
    if (h_ms, h_cost) < (best_ms, best_cost):
        best_pc, best_records, best_ms, best_cost = h_pc, h_rec, h_ms, h_cost

    return dict(best_pc), best_records, best_ms, best_cost


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
