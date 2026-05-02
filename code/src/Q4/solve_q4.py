"""
问题4：预算约束下搜索设备购买方案，并完成 A-E 五个车间调度；
零增购（购买前）基线与 `solve_q3.py` 默认一致：`solve_problem3` 贪心 + 可用时 `solve_q3_cpsat` 择更优。
候选购置方案的内层精化在 OR-Tools 可用时使用 `cpsat_q4.solve_q4_scheduling_cpsat`；`--no-cpsat` 时购置搜索仍用 `solve_problem4`。
"""
from __future__ import annotations

import os
import sys
import time
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

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
from Qbase.exporter import export_purchase_plan, export_schedule
from Qbase.parsers import Device, ExpandedOperation, expand_repeated_operations, parse_devices, parse_distances, parse_operations
from Qbase.scheduler_common import ScheduleRecord, solve_problem3, solve_problem4
from Qbase.time_utils import seconds_to_hhmmss
from Qbase.validators import validate_budget, validate_schedule

try:
    from Qbase.cpsat_q4 import solve_q4_scheduling_cpsat

    _CPSAT_Q4_AVAILABLE = True
except Exception:  # noqa: BLE001 — 无 ortools 或导入失败时走启发式
    solve_q4_scheduling_cpsat = None  # type: ignore[assignment,misc]
    _CPSAT_Q4_AVAILABLE = False

# 购买搜索超参：在运行时间与解质量之间折中；增大枚举上界/束宽通常更慢但可能更优。
ENUM_MAX_TOTAL_PURCHASED_MACHINES = 14
BEAM_WIDTH = 72
BEAM_MAX_LAYERS = 28
GREEDY_MULTISTART_MAX_STEPS = 18
GREEDY_BOTTLENECK_TYPES_CAP = 6

# CP-SAT 精化：对贪心预筛后的前若干方案做全局调度（与 othersolve_4 同构内层）。
# 追求更优：用 `--preset strong|maximum` 或调大下列常量 / 命令行；`maximum` 为长时间运行推荐极参（见 PRESET_Q4）。
CPSAT_REFINE_TIME_SEC = 70.0
CPSAT_REFINE_TOP = 32
GREEDY_PRE_RANK_KEEP = 36
BOTTLENECK_ENUM_MAX_PER_TYPE = 7
# 在已定最优购置向量上再跑 CP-SAT 抛光；`--preset maximum` 会加长并加第二轮。
CPSAT_FINAL_POLISH_SEC = 90.0
CPSAT_SECOND_POLISH_SEC = 0.0

# 预设：balanced=与上方常量一致；strong=中等加强；maximum=多核长时间极参（总 CP 时间可达数小时级，按需选用）。
PRESET_Q4: Dict[str, Dict[str, Any]] = {
    "balanced": {},
    "strong": {
        "cpsat_refine_sec": 95.0,
        "cpsat_refine_top": 38,
        "greedy_pre_rank": 48,
        "bottle_max_per_type": 8,
        "final_polish_sec": 150.0,
        "second_final_polish_sec": 0.0,
        "cpsat_workers": DEFAULT_CPSAT_NUM_WORKERS,
    },
    # 极参：粗估 CP 主循环约 top×sec（如 48×180≈2.4h）+ 两轮抛光，适合过夜/工作站。
    "maximum": {
        "cpsat_refine_sec": 180.0,
        "cpsat_refine_top": 48,
        "greedy_pre_rank": 96,
        "bottle_max_per_type": 10,
        "final_polish_sec": 360.0,
        "second_final_polish_sec": 180.0,
        "cpsat_workers": DEFAULT_CPSAT_NUM_WORKERS,
    },
}

_Q4_T0: List[float] = [0.0]


def _apply_q4_preset_args(ns: Any) -> None:
    """将 --preset 覆盖到与各调参 flag 同名的 argparse 属性上。"""
    name = str(getattr(ns, "preset", "balanced") or "balanced")
    ov = PRESET_Q4.get(name) or {}
    int_keys = {"cpsat_refine_top", "greedy_pre_rank", "bottle_max_per_type", "cpsat_workers"}
    for k, v in ov.items():
        if k in int_keys:
            setattr(ns, k, int(v))
        else:
            setattr(ns, k, float(v))


def _q4_reset_timer() -> None:
    _Q4_T0[0] = time.perf_counter()


def _q4_log(msg: str) -> None:
    dt = time.perf_counter() - _Q4_T0[0]
    wall = time.strftime("%H:%M:%S", time.localtime())
    print(f"[问题4 {wall} +{dt:9.1f}s] {msg}", flush=True)


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


def _zero_purchase_baseline_same_as_q3(
    expanded: List[ExpandedOperation],
    base_devices: List[Device],
    dist_map,
    raw_repeat_map: Dict[str, int],
) -> Tuple[List[ScheduleRecord], int]:
    """
    零增购（未购置）全局调度与 `solve_q3.py` 默认一致：
    先 `solve_problem3` 贪心，再在 ortools 可用时调用 `solve_q3_cpsat`，若 CP-SAT 更优则采用（同 Q3 --solver auto）。
    """
    greedy_records, greedy_ms = solve_problem3(expanded, base_devices, dist_map)
    if not cpsat_available():
        return greedy_records, greedy_ms
    cp_records, cp_ms, st, _ = solve_q3_cpsat(
        expanded,
        base_devices,
        dist_map,
        time_limit_sec=float(DEFAULT_CPSAT_TIME_LIMIT_SEC),
        num_workers=int(DEFAULT_CPSAT_NUM_WORKERS),
        raw_repeat_map=raw_repeat_map,
    )
    if cp_records and cp_ms > 0 and cp_ms < greedy_ms:
        _q4_log(f"零增购基线：CP-SAT 优于贪心（{greedy_ms}s → {cp_ms}s，{st}），采用 CP-SAT")
        return cp_records, cp_ms
    if cp_records and cp_ms > 0:
        _q4_log(f"零增购基线：贪心更优或持平（贪心 {greedy_ms}s，CP-SAT {cp_ms}s），采用贪心")
    else:
        _q4_log(f"零增购基线：CP-SAT 无可行解（{st}），采用贪心 {greedy_ms}s")
    return greedy_records, greedy_ms


def _evaluate_plan(
    expanded,
    base_devices: List[Device],
    purchase_counts: Dict[Tuple[int, str], int],
    templates: Dict[Tuple[int, str], Device],
    dist_map,
) -> Tuple[List, int, int]:
    """
    返回 (调度记录, makespan, 购买总费用)。
    内层调用 solve_problem4（与问题3一致：车间顺序全排列 + solve_problem1 并联枚举）。
    """
    devs = _expand_devices_with_purchase(base_devices, purchase_counts, templates)
    records, ms = solve_problem4(expanded, devs, dist_map)
    cost = _calc_purchase_cost(purchase_counts, templates)
    return records, ms, cost


def _utilization_by_type(records, devices: List[Device], makespan: int) -> Dict[str, float]:
    """按设备类型聚合「作业秒」/(makespan×该类型台数)，用于粗粒度瓶颈排序（与并联记录兼容）。"""
    if makespan <= 0:
        return {}
    dev_count = defaultdict(int)
    for d in devices:
        dev_count[d.device_type] += 1
    work = defaultdict(int)
    for r in records:
        work[r.device_type] += int(r.duration_sec)
    util: Dict[str, float] = {}
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
    _q4_log("多起点贪心：开始（每变体需多次内层调度，可能较慢）")
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

    for oi, type_order in enumerate(orderings):
        _q4_log(f"多起点贪心 变体 {oi + 1}/{len(orderings)}：初始调度中…")
        purchase_counts: Dict[Tuple[int, str], int] = defaultdict(int)
        records, ms, _cost0 = _evaluate_plan(
            expanded, base_devices, dict(purchase_counts), templates, dist_map
        )
        cost = 0
        cur_records, cur_ms, cur_cost = records, ms, cost
        _q4_log(
            f"多起点贪心 变体 {oi + 1}：初始 Makespan={cur_ms}s，开始逐步加购（每变体至多 {max_steps} 轮）"
        )

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

        _q4_log(f"多起点贪心 变体 {oi + 1} 结束：Makespan={cur_ms}s，购买费用={cur_cost} 元")
        if (cur_ms, cur_cost) < (best_ms, best_cost):
            best_ms, best_cost = cur_ms, cur_cost
            best_records = cur_records
            best_pc = dict(purchase_counts)

    _q4_log(f"多起点贪心 全部完成：最优 Makespan={best_ms}s，费用={best_cost} 元")
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
    prog: Optional[List[Any]] = None,  # [叶计数, 上次进度日志 perf_counter]
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
        if prog is not None:
            prog[0] += 1
            n_leaf = prog[0]
            now = time.perf_counter()
            if n_leaf == 1 or n_leaf % 350 == 0 or now - prog[1] >= 15.0:
                _q4_log(
                    f"有界枚举 DFS：已评估叶节点 {n_leaf} 个；当前最优 makespan={best_holder[1]}s，费用={best_holder[2]} 元"
                )
                prog[1] = now
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
            prog,
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

    for layer in range(max_layers):
        _q4_log(
            f"束搜索 第 {layer + 1}/{max_layers} 层：当前束大小={len(beam)}，"
            f"全局当前最优 makespan={best[2]}s 费用={best[3]} 元；正在扩展子状态…"
        )
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
        _q4_log(
            f"束搜索 第 {layer + 1} 层结束：本层生成子状态 {len(children)} 个，保留束 {len(new_beam)}；"
            f"束内最优 makespan={cand_best[2]}s"
        )
        beam = new_beam

    _q4_log(f"束搜索结束：输出最优 makespan={best[2]}s，费用={best[3]} 元")
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
    hill_history: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Dict[Tuple[int, str], int], List, int, int]:
    """在预算内反复尝试任意位置 +1 台，直到单步无法严格缩短工期。"""
    keys = sorted(templates.keys())
    pc = defaultdict(int, purchase_counts)
    cur_r, cur_ms, cur_c = records, makespan, cost
    _q4_log(f"爬山加购：起点 makespan={cur_ms}s，费用={cur_c} 元")
    if hill_history is not None:
        hill_history.append(
            {"step": 0, "makespan": int(cur_ms), "cost": int(cur_c), "add_key": None, "accepted": False}
        )
    improved = True
    accept_n = 0
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
                accept_n += 1
                _q4_log(f"爬山加购 第 {accept_n} 次接受：加购键={key} → makespan={cur_ms}s，费用={cur_c} 元")
                if hill_history is not None:
                    hill_history.append(
                        {
                            "step": int(accept_n),
                            "makespan": int(cur_ms),
                            "cost": int(cur_c),
                            "add_key": (int(key[0]), str(key[1])),
                            "accepted": True,
                        }
                    )
                break
    _q4_log(f"爬山加购结束：最终 makespan={cur_ms}s，费用={cur_c} 元（共接受 {accept_n} 次加购）")
    return dict(pc), cur_r, cur_ms, cur_c


def _unit_price_from_templates(templates: Dict[Tuple[int, str], Device], dtype: str) -> int:
    if (1, dtype) in templates:
        return int(round(templates[(1, dtype)].price))
    return int(round(templates[(2, dtype)].price))


def _generate_bottleneck_purchase_candidates(
    templates: Dict[Tuple[int, str], Device],
    budget_limit: int,
    max_per_type: int = BOTTLENECK_ENUM_MAX_PER_TYPE,
) -> List[Dict[Tuple[int, str], int]]:
    """
    生成与 othersolve_4 类似的瓶颈导向购置向量（抛光机+传感机枚举 + 班组分配策略 + 剩余预算填其他类）。
    """
    dtypes = sorted({k[1] for k in templates.keys()})
    bottle_a = "高速抛光机"
    bottle_b = "自动传感多功能机"
    if bottle_a not in dtypes or bottle_b not in dtypes:
        return []
    price_a = _unit_price_from_templates(templates, bottle_a)
    price_b = _unit_price_from_templates(templates, bottle_b)
    other_types = sorted(
        [d for d in dtypes if d not in (bottle_a, bottle_b)],
        key=lambda d: -_unit_price_from_templates(templates, d),
    )
    plans: List[Dict[Tuple[int, str], int]] = []
    seen: Set[Tuple[Tuple[Tuple[int, str], int], ...]] = set()

    def _add_plan(pc: Dict[Tuple[int, str], int]) -> None:
        cost = _calc_purchase_cost(pc, templates)
        if cost > budget_limit:
            return
        key = _frozen_counts(pc)
        if key in seen:
            return
        seen.add(key)
        plans.append(dict(pc))

    pao_max = min(max_per_type, budget_limit // price_a) if price_a > 0 else 0
    chuan_max = min(max_per_type, budget_limit // price_b) if price_b > 0 else 0

    for n_pao in range(0, pao_max + 1):
        for n_chuan in range(0, chuan_max + 1):
            cost_bottle = n_pao * price_a + n_chuan * price_b
            if cost_bottle > budget_limit:
                continue
            remain = budget_limit - cost_bottle
            alloc_strategies = [
                {
                    bottle_a: (n_pao // 2 + n_pao % 2, n_pao // 2),
                    bottle_b: (n_chuan // 2 + n_chuan % 2, n_chuan // 2),
                },
                {
                    bottle_a: (n_pao // 2, n_pao // 2 + n_pao % 2),
                    bottle_b: (n_chuan // 2, n_chuan // 2 + n_chuan % 2),
                },
                {bottle_a: (n_pao, 0), bottle_b: (n_chuan, 0)},
                {bottle_a: (0, n_pao), bottle_b: (0, n_chuan)},
            ]
            for bottle_alloc in alloc_strategies:
                pc: Dict[Tuple[int, str], int] = {}
                pc[(1, bottle_a)] = bottle_alloc[bottle_a][0]
                pc[(2, bottle_a)] = bottle_alloc[bottle_a][1]
                pc[(1, bottle_b)] = bottle_alloc[bottle_b][0]
                pc[(2, bottle_b)] = bottle_alloc[bottle_b][1]
                rem = remain
                for d in other_types:
                    pr = _unit_price_from_templates(templates, d)
                    n_buy = min(max_per_type, rem // pr) if pr > 0 else 0
                    if n_buy > 0:
                        n1 = n_buy // 2 + n_buy % 2
                        n2 = n_buy // 2
                        pc[(1, d)] = pc.get((1, d), 0) + n1
                        pc[(2, d)] = pc.get((2, d), 0) + n2
                        rem -= n_buy * pr
                _add_plan(pc)

    for dtype in (bottle_a, bottle_b):
        pr = _unit_price_from_templates(templates, dtype)
        cap = min(max_per_type, budget_limit // pr) if pr > 0 else 0
        for n_total in range(1, cap + 1):
            for t1, t2 in ((n_total, 0), (0, n_total), (n_total // 2 + n_total % 2, n_total // 2)):
                pc = {(1, dtype): t1, (2, dtype): t2}
                _add_plan(pc)

    return plans


def _search_purchase_plan_cpsat_primary(
    expanded,
    base_devices: List[Device],
    dist_map,
    budget_limit: int,
    raw_repeat_map: Dict[str, int],
    *,
    cpsat_refine_sec: float,
    cpsat_refine_top: int,
    greedy_pre_rank_keep: int,
    bottleneck_max_per_type: int,
    final_polish_sec: float,
    second_final_polish_sec: float,
    cpsat_workers: int,
) -> Tuple[Dict[Tuple[int, str], int], List, int, int, List, int, Dict[str, Any]]:
    """瓶颈候选 + 贪心预筛 + CP-SAT 精化 + 爬山加购 + 可选最终 CP-SAT 抛光。"""
    assert solve_q4_scheduling_cpsat is not None
    templates = _build_device_template(base_devices)
    _q4_log("CP-SAT 主路径：计算零增购基线（与问题3默认：贪心 + solve_q3_cpsat 择优）…")
    baseline_records, baseline_makespan = _zero_purchase_baseline_same_as_q3(
        expanded, base_devices, dist_map, raw_repeat_map
    )

    candidates: List[Dict[Tuple[int, str], int]] = [{}]
    candidates.extend(
        _generate_bottleneck_purchase_candidates(
            templates, budget_limit, max_per_type=bottleneck_max_per_type
        )
    )
    uniq: List[Dict[Tuple[int, str], int]] = []
    seen_fn: Set[Tuple[Tuple[Tuple[int, str], int], ...]] = set()
    for pc in candidates:
        fn = _frozen_counts(pc)
        if fn in seen_fn:
            continue
        if _calc_purchase_cost(pc, templates) > budget_limit:
            continue
        seen_fn.add(fn)
        uniq.append(dict(pc))

    _q4_log(f"CP-SAT 主路径：去重后候选购置方案 {len(uniq)} 个；贪心（车间全排列）预筛…")
    ranked: List[Tuple[int, int, Dict[Tuple[int, str], int], List]] = []
    for pc in uniq:
        rec, ms, cost = _evaluate_plan(expanded, base_devices, pc, templates, dist_map)
        ranked.append((ms, cost, pc, rec))
    ranked.sort(key=lambda x: (x[0], x[1]))
    greedy_rank_makespans = [int(x[0]) for x in ranked]
    telemetry: Dict[str, Any] = {
        "mode": "cpsat_primary",
        "greedy_rank_makespans": greedy_rank_makespans,
        "greedy_rank_costs": [int(x[1]) for x in ranked],
        "cpsat_refine_curve": [],
        "global_best_curve": [],
        "hill_climb": [],
        "polish": [],
    }
    if not ranked:
        return {}, baseline_records, baseline_makespan, 0, baseline_records, baseline_makespan, telemetry

    best_ms, best_cost, best_pc, best_records = ranked[0][0], ranked[0][1], dict(ranked[0][2]), ranked[0][3]
    telemetry["global_best_curve"].append(
        {"stage": "greedy_top1", "makespan": int(best_ms), "cost": int(best_cost), "note": "贪心排序后取第一的贪心评估值"}
    )
    pre_keep = min(greedy_pre_rank_keep, len(ranked))
    refine_list = ranked[: min(cpsat_refine_top, pre_keep)]
    nw = min(max(1, int(cpsat_workers)), 32)

    _q4_log(
        f"CP-SAT 主路径：贪心预筛保留 {pre_keep} 个，对其前 {len(refine_list)} 个做 CP-SAT 精化"
        f"（每方案至多 {cpsat_refine_sec:.0f}s，workers={nw}）…"
    )
    for i, (ms0, c0, pc, _rg) in enumerate(refine_list):
        devs = _expand_devices_with_purchase(base_devices, pc, templates)
        rec_c, ms_c = solve_q4_scheduling_cpsat(
            expanded,
            devs,
            dist_map,
            time_limit_sec=float(cpsat_refine_sec),
            num_workers=nw,
        )
        ms_c_int = int(ms_c) if ms_c is not None else None
        improved_here = bool(
            rec_c is not None
            and ms_c_int is not None
            and (ms_c_int < best_ms or (ms_c_int == best_ms and c0 < best_cost))
        )
        telemetry["cpsat_refine_curve"].append(
            {
                "i": int(i),
                "greedy_ms": int(ms0),
                "greedy_cost": int(c0),
                "cpsat_ms": ms_c_int,
                "improved": improved_here,
            }
        )
        if rec_c is not None and ms_c is not None and (ms_c < best_ms or (ms_c == best_ms and c0 < best_cost)):
            best_ms, best_cost, best_pc, best_records = ms_c, c0, dict(pc), rec_c
            _q4_log(f"CP-SAT 精化 第 {i + 1}/{len(refine_list)} 档改进 → makespan={best_ms}s，费用={best_cost} 元")
            telemetry["global_best_curve"].append(
                {
                    "stage": f"cpsat_refine_{i + 1}",
                    "makespan": int(best_ms),
                    "cost": int(best_cost),
                    "note": "CP-SAT 精化接受更优解",
                }
            )

    hill_hist: List[Dict[str, Any]] = []
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
        hill_history=hill_hist,
    )
    telemetry["hill_climb"] = hill_hist
    if (h_ms, h_cost) < (best_ms, best_cost):
        best_pc, best_records, best_ms, best_cost = h_pc, h_rec, h_ms, h_cost
        _q4_log(f"爬山加购（CP-SAT 路径后）改进 → makespan={best_ms}s，费用={best_cost} 元")
        telemetry["global_best_curve"].append(
            {"stage": "hill_climb", "makespan": int(best_ms), "cost": int(best_cost), "note": "爬山加购后全局最优（相对精化后起点）"}
        )

    if final_polish_sec > 0:
        _q4_log(
            f"最终 CP-SAT 抛光：当前最优购置费用={best_cost} 元，加长时限 {final_polish_sec:.0f}s（workers={nw}）…"
        )
        devs_f = _expand_devices_with_purchase(base_devices, best_pc, templates)
        rec_f, ms_f = solve_q4_scheduling_cpsat(
            expanded,
            devs_f,
            dist_map,
            time_limit_sec=float(final_polish_sec),
            num_workers=nw,
        )
        if rec_f is not None and ms_f is not None and ms_f < best_ms:
            prev = int(best_ms)
            best_records, best_ms = rec_f, int(ms_f)
            _q4_log(f"抛光改进 makespan → {best_ms}s（{seconds_to_hhmmss(best_ms)}）")
            telemetry["polish"].append({"round": 1, "before_ms": prev, "after_ms": int(best_ms)})
            telemetry["global_best_curve"].append(
                {"stage": "final_polish_1", "makespan": int(best_ms), "cost": int(best_cost), "note": "固定购置向量下的加长CP-SAT抛光"}
            )

    if second_final_polish_sec > 0:
        _q4_log(f"第二轮 CP-SAT 抛光：时限 {second_final_polish_sec:.0f}s（workers={nw}）…")
        devs2 = _expand_devices_with_purchase(base_devices, best_pc, templates)
        rec2, ms2 = solve_q4_scheduling_cpsat(
            expanded,
            devs2,
            dist_map,
            time_limit_sec=float(second_final_polish_sec),
            num_workers=nw,
        )
        if rec2 is not None and ms2 is not None and ms2 < best_ms:
            prev = int(best_ms)
            best_records, best_ms = rec2, int(ms2)
            _q4_log(f"第二轮抛光改进 makespan → {best_ms}s（{seconds_to_hhmmss(best_ms)}）")
            telemetry["polish"].append({"round": 2, "before_ms": prev, "after_ms": int(best_ms)})
            telemetry["global_best_curve"].append(
                {"stage": "final_polish_2", "makespan": int(best_ms), "cost": int(best_cost), "note": "第二轮抛光（若启用）"}
            )

    _q4_log(
        f"CP-SAT 主路径结束：最终 makespan={best_ms}s（{seconds_to_hhmmss(best_ms)}），"
        f"购买费用={best_cost} 元；基线={baseline_makespan}s"
    )
    return dict(best_pc), best_records, best_ms, best_cost, baseline_records, baseline_makespan, telemetry


def _search_purchase_plan_legacy(
    expanded,
    base_devices: List[Device],
    dist_map,
    budget_limit: int,
    raw_repeat_map: Dict[str, int],
) -> Tuple[Dict[Tuple[int, str], int], List, int, int, List, int, Dict[str, Any]]:
    _q4_log("计算零增购基线（与问题3默认：贪心 + solve_q3_cpsat 择优）…")
    baseline_records, baseline_makespan = _zero_purchase_baseline_same_as_q3(
        expanded, base_devices, dist_map, raw_repeat_map
    )
    _q4_log(
        f"基线完成：Makespan={baseline_makespan}s（{seconds_to_hhmmss(baseline_makespan)}），"
        "开始瓶颈统计与购买搜索"
    )
    util = _utilization_by_type(baseline_records, base_devices, baseline_makespan)
    bottleneck_types = sorted(util.keys(), key=lambda t: util[t], reverse=True)
    if not bottleneck_types:
        bottleneck_types = sorted({d.device_type for d in base_devices})

    templates = _build_device_template(base_devices)
    keys = sorted(templates.keys())
    unit_costs = [int(round(templates[k].price)) for k in keys]
    telemetry: Dict[str, Any] = {"mode": "legacy", "stage_points": []}
    telemetry["stage_points"].append(
        {
            "stage": "baseline",
            "makespan": int(baseline_makespan),
            "cost": 0,
            "note": "零增购基线（与问题3默认口径一致）",
        }
    )

    # 1) 枚举：总增购台数不超过上界时 DFS 遍历预算可行组合（叶节点 _evaluate_plan）
    max_machines = ENUM_MAX_TOTAL_PURCHASED_MACHINES
    _q4_log(
        f"有界枚举 DFS：购买键数={len(keys)}，增购总台数上界={max_machines}，预算={budget_limit}；"
        "每评估一叶节点需一次完整调度，耗时长属正常"
    )
    best_holder: List = [None, 10**18, 10**18, baseline_records]
    dfs_prog: List[Any] = [0, 0.0]
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
        dfs_prog,
    )
    _q4_log(
        f"有界枚举 DFS 结束：共评估叶节点 {dfs_prog[0]} 个；枚举层最优 makespan={best_holder[1]}s，费用={best_holder[2]} 元"
    )
    best_pc = dict(best_holder[0]) if best_holder[0] is not None else {}
    best_ms = int(best_holder[1]) if best_holder[0] is not None else baseline_makespan
    best_cost = int(best_holder[2]) if best_holder[0] is not None else 0
    best_records = best_holder[3]
    telemetry["stage_points"].append(
        {
            "stage": "dfs_enum",
            "makespan": int(best_ms),
            "cost": int(best_cost),
            "note": f"有界枚举 DFS 结束（叶节点 {int(dfs_prog[0])} 个）",
        }
    )

    # 2) 束搜索：从「不购买」与「枚举层最优」出发扩展（层内会生成大量近邻）
    _q4_log(
        f"束搜索：层数上限={BEAM_MAX_LAYERS}，束宽={BEAM_WIDTH}；"
        "每层对束内每状态尝试各 (班组,类型)+1 台并各调度一次"
    )
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
        beam_width=BEAM_WIDTH,
        max_layers=BEAM_MAX_LAYERS,
    )
    if (beam_ms, beam_cost) < (best_ms, best_cost):
        best_pc, best_records, best_ms, best_cost = beam_pc, beam_rec, beam_ms, beam_cost
        _q4_log(f"束搜索改进了全局最优 → makespan={best_ms}s，费用={best_cost} 元")
    telemetry["stage_points"].append(
        {"stage": "beam_search", "makespan": int(best_ms), "cost": int(best_cost), "note": "束搜索阶段结束后的当前全局最优"}
    )

    # 3) 多起点贪心（与束搜索互补）
    g_pc, g_rec, g_ms, g_cost = _greedy_multistart(
        expanded,
        base_devices,
        dist_map,
        budget_limit,
        templates,
        bottleneck_types[:GREEDY_BOTTLENECK_TYPES_CAP]
        if len(bottleneck_types) >= GREEDY_BOTTLENECK_TYPES_CAP
        else bottleneck_types,
        max_steps=GREEDY_MULTISTART_MAX_STEPS,
    )
    if (g_ms, g_cost) < (best_ms, best_cost):
        best_pc, best_records, best_ms, best_cost = g_pc, g_rec, g_ms, g_cost
        _q4_log(f"多起点贪心改进了全局最优 → makespan={best_ms}s，费用={best_cost} 元")
    telemetry["stage_points"].append(
        {"stage": "greedy_multistart", "makespan": int(best_ms), "cost": int(best_cost), "note": "多起点贪心阶段结束后的当前全局最优"}
    )

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
        _q4_log(f"爬山加购改进了全局最优 → makespan={best_ms}s，费用={best_cost} 元")
    telemetry["stage_points"].append(
        {"stage": "hill_climb", "makespan": int(best_ms), "cost": int(best_cost), "note": "爬山加购阶段结束后的当前全局最优"}
    )

    _q4_log(
        f"购买搜索全部阶段结束：最终 makespan={best_ms}s（{seconds_to_hhmmss(best_ms)}），"
        f"购买费用={best_cost} 元；基线={baseline_makespan}s"
    )
    return dict(best_pc), best_records, best_ms, best_cost, baseline_records, baseline_makespan, telemetry


def _search_purchase_plan(
    expanded,
    base_devices: List[Device],
    dist_map,
    budget_limit: int,
    raw_repeat_map: Dict[str, int],
    *,
    use_cpsat: bool = True,
    cpsat_refine_sec: float = CPSAT_REFINE_TIME_SEC,
    cpsat_refine_top: int = CPSAT_REFINE_TOP,
    greedy_pre_rank_keep: int = GREEDY_PRE_RANK_KEEP,
    bottleneck_max_per_type: int = BOTTLENECK_ENUM_MAX_PER_TYPE,
    final_polish_sec: float = CPSAT_FINAL_POLISH_SEC,
    second_final_polish_sec: float = CPSAT_SECOND_POLISH_SEC,
    cpsat_workers: int = DEFAULT_CPSAT_NUM_WORKERS,
) -> Tuple[Dict[Tuple[int, str], int], List, int, int, List, int, Dict[str, Any]]:
    if use_cpsat and _CPSAT_Q4_AVAILABLE:
        _q4_log("已启用 CP-SAT 主路径（安装 ortools 且导入成功）；可用 --no-cpsat 回退纯启发式搜索")
        return _search_purchase_plan_cpsat_primary(
            expanded,
            base_devices,
            dist_map,
            budget_limit,
            raw_repeat_map,
            cpsat_refine_sec=float(cpsat_refine_sec),
            cpsat_refine_top=int(cpsat_refine_top),
            greedy_pre_rank_keep=int(greedy_pre_rank_keep),
            bottleneck_max_per_type=int(bottleneck_max_per_type),
            final_polish_sec=float(final_polish_sec),
            second_final_polish_sec=float(second_final_polish_sec),
            cpsat_workers=int(cpsat_workers),
        )
    if use_cpsat and not _CPSAT_Q4_AVAILABLE:
        _q4_log("CP-SAT 不可用，改用启发式 DFS/束搜索/贪心/爬山（与 --no-cpsat 相同）")
    return _search_purchase_plan_legacy(expanded, base_devices, dist_map, budget_limit, raw_repeat_map)


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


_WORKSHOP_COLORS = {
    "A": "#2563EB",
    "B": "#16A34A",
    "C": "#D97706",
    "D": "#DC2626",
    "E": "#7C3AED",
}
def _norm_ws(workshop: str) -> str:
    return str(workshop).strip().upper().replace("车间", "")


def _pick_cjk_font() -> Optional[str]:
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


def plot_device_gantt_q4(
    records: List[ScheduleRecord],
    makespan_sec: int,
    out_png: Path,
    *,
    title_tag: str,
    extra_suffix: str = "",
) -> None:
    """设备甘特图上下双拼（上=车间，下=工序+班组），实现见 Qbase.gantt_dual。"""
    from Qbase.gantt_dual import plot_device_gantt_dual_strip

    plot_device_gantt_dual_strip(
        records,
        makespan_sec,
        out_png,
        title=f"问题4 设备甘特图（{title_tag}）  Makespan = {makespan_sec}s（{seconds_to_hhmmss(makespan_sec)}）",
        title_suffix=extra_suffix,
        filter_team=None,
        show_team_on_op_strip=True,
        dense_layout=True,
    )


def _workshop_finish_map(records: List[ScheduleRecord], workshops: List[str]) -> Dict[str, int]:
    finish_m = {w: 0 for w in workshops}
    for r in records:
        wk = _norm_ws(r.workshop)
        if wk in finish_m:
            finish_m[wk] = max(finish_m[wk], r.end_sec)
    return finish_m


def plot_workshop_bars_after_q4(
    records: List[ScheduleRecord],
    expanded: List[ExpandedOperation],
    makespan_sec: int,
    out_png: Path,
    *,
    title_note: str = "购买后（最优方案）",
) -> None:
    """上：各车间最后完工时刻；下：班组1/班组2在各车间累计作业时间（分组柱）。"""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    font = _pick_cjk_font()
    if font:
        plt.rcParams["font.sans-serif"] = [font, "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    workshops = sorted({_norm_ws(o.workshop) for o in expanded})
    finish_m = _workshop_finish_map(records, workshops)
    t1_sum: Dict[str, int] = {w: 0 for w in workshops}
    t2_sum: Dict[str, int] = {w: 0 for w in workshops}
    for r in records:
        wk = _norm_ws(r.workshop)
        if wk not in t1_sum:
            continue
        if r.team == 1:
            t1_sum[wk] += int(r.duration_sec)
        elif r.team == 2:
            t2_sum[wk] += int(r.duration_sec)

    n = len(workshops)
    x = list(range(n))
    w_bar = 0.36

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), dpi=120, facecolor="#F8FAFC", sharex=True)
    colors_fin = [_WORKSHOP_COLORS.get(w, "#64748B") for w in workshops]
    ax1.bar(x, [finish_m[w] for w in workshops], width=0.55, color=colors_fin, edgecolor="white", linewidth=0.6)
    ax1.set_ylabel("最后完工时刻（s）", fontsize=10, color="#334155")
    ax1.set_title(
        f"问题4 各车间完工与双班组作业量（{title_note}）  Makespan = {makespan_sec}s（{seconds_to_hhmmss(makespan_sec)}）",
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
    ax2.bar(x2, [t2_sum[w] for w in workshops], width=w_bar, label="班组2 累计作业(s)", color="#A855F7", edgecolor="white")
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


def plot_q4_before_after_comparison(
    expanded: List[ExpandedOperation],
    baseline_records: List[ScheduleRecord],
    baseline_ms: int,
    after_records: List[ScheduleRecord],
    after_ms: int,
    purchase_cost: int,
    out_png: Path,
) -> None:
    """购买前后：全局 Makespan 与各车间最后完工时刻对比。"""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    font = _pick_cjk_font()
    if font:
        plt.rcParams["font.sans-serif"] = [font, "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    workshops = sorted({_norm_ws(o.workshop) for o in expanded})
    fin_b = _workshop_finish_map(baseline_records, workshops)
    fin_a = _workshop_finish_map(after_records, workshops)

    fig, (ax0, ax1) = plt.subplots(2, 1, figsize=(10, 8.5), dpi=120, facecolor="#F8FAFC")
    labels_ms = ["购买前（零增购）", "购买后（最优方案）"]
    vals_ms = [baseline_ms, after_ms]
    colors_ms = ["#94A3B8", "#059669"]
    bars0 = ax0.bar(range(2), vals_ms, width=0.5, color=colors_ms, edgecolor="white", linewidth=0.8)
    ax0.set_xticks(range(2))
    ax0.set_xticklabels(labels_ms, fontsize=10)
    ax0.set_ylabel("全局 Makespan（s）", fontsize=10, color="#334155")
    delta = baseline_ms - after_ms
    pct = (delta / baseline_ms * 100.0) if baseline_ms > 0 else 0.0
    ax0.set_title(
        f"问题4 购买前后对比  购买费用 = {purchase_cost} 元  工期缩短 {delta}s（{pct:.2f}%）",
        fontsize=12,
        fontweight="bold",
        color="#0F172A",
        pad=10,
    )
    for i, b in enumerate(bars0):
        h = b.get_height()
        ax0.text(b.get_x() + b.get_width() / 2, h + max(baseline_ms, after_ms) * 0.01, f"{int(h)}", ha="center", va="bottom", fontsize=10, fontweight="bold")
    ax0.grid(axis="y", color="#CBD5E1", linestyle="-", linewidth=0.4, alpha=0.85)
    ax0.set_facecolor("#F1F5F9")
    for spine in ("top", "right"):
        ax0.spines[spine].set_visible(False)

    n = len(workshops)
    idx = list(range(n))
    w = 0.35
    x_left = [i - w / 2 for i in idx]
    x_right = [i + w / 2 for i in idx]
    ax1.bar(x_left, [fin_b[w] for w in workshops], width=w, label="购买前", color="#94A3B8", edgecolor="white")
    ax1.bar(x_right, [fin_a[w] for w in workshops], width=w, label="购买后", color="#059669", edgecolor="white")
    ax1.set_xticks(idx)
    ax1.set_xticklabels([f"{w} 车间" for w in workshops], fontsize=10)
    ax1.set_ylabel("各车间最后完工时刻（s）", fontsize=10, color="#334155")
    ax1.set_xlabel("车间", fontsize=10, color="#334155")
    ax1.legend(loc="upper right", fontsize=9)
    ax1.grid(axis="y", color="#CBD5E1", linestyle="-", linewidth=0.4, alpha=0.85)
    ax1.set_facecolor("#F1F5F9")
    for spine in ("top", "right"):
        ax1.spines[spine].set_visible(False)

    plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def plot_q4_purchase_search_summary(
    purchase_counts: Dict[Tuple[int, str], int],
    base_devices: List[Device],
    baseline_ms: int,
    after_ms: int,
    total_cost: int,
    budget_limit: int,
    out_png: Path,
) -> None:
    """论文辅助图：最优购置方案、费用构成与工期收益。"""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    font = _pick_cjk_font()
    if font:
        plt.rcParams["font.sans-serif"] = [font, "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    preferred = ["自动化输送臂", "工业清洗机", "精密灌装机", "自动传感多功能机", "高速抛光机"]
    templates = _build_device_template(base_devices)
    dtypes = [d for d in preferred if (1, d) in templates or (2, d) in templates]
    short = {
        "自动化输送臂": "输送臂",
        "工业清洗机": "清洗机",
        "精密灌装机": "灌装机",
        "自动传感多功能机": "传感机",
        "高速抛光机": "抛光机",
    }
    x = list(range(len(dtypes)))
    team1 = [int(purchase_counts.get((1, d), 0)) for d in dtypes]
    team2 = [int(purchase_counts.get((2, d), 0)) for d in dtypes]
    costs = []
    for d, c1, c2 in zip(dtypes, team1, team2):
        tmpl = templates.get((1, d)) or templates.get((2, d))
        unit = int(round(tmpl.price)) if tmpl else 0
        costs.append((c1 + c2) * unit)

    fig = plt.figure(figsize=(13, 9), dpi=120, facecolor="#F8FAFC")
    gs = fig.add_gridspec(2, 2, height_ratios=[1.05, 0.95], width_ratios=[1.15, 0.85], hspace=0.32, wspace=0.28)
    ax_cnt = fig.add_subplot(gs[0, :])
    ax_cost = fig.add_subplot(gs[1, 0])
    ax_ms = fig.add_subplot(gs[1, 1])

    w = 0.34
    ax_cnt.bar([i - w / 2 for i in x], team1, width=w, label="班组1购买台数", color="#0EA5E9", edgecolor="white")
    ax_cnt.bar([i + w / 2 for i in x], team2, width=w, label="班组2购买台数", color="#A855F7", edgecolor="white")
    ax_cnt.set_xticks(x)
    ax_cnt.set_xticklabels([short.get(d, d) for d in dtypes], fontsize=10)
    ax_cnt.set_ylabel("购买台数", fontsize=10, color="#334155")
    ax_cnt.set_title(
        "问题4 最优购置方案检索图：购买组合、费用与工期收益",
        fontsize=13,
        fontweight="bold",
        color="#0F172A",
        pad=10,
    )
    ymax = max(team1 + team2 + [1])
    ax_cnt.set_ylim(0, ymax + 1.2)
    for i, (c1, c2) in enumerate(zip(team1, team2)):
        if c1 > 0:
            ax_cnt.text(i - w / 2, c1 + 0.06, str(c1), ha="center", va="bottom", fontsize=9, fontweight="bold")
        if c2 > 0:
            ax_cnt.text(i + w / 2, c2 + 0.06, str(c2), ha="center", va="bottom", fontsize=9, fontweight="bold")
    ax_cnt.legend(loc="upper right", fontsize=9)
    ax_cnt.grid(axis="y", color="#CBD5E1", linewidth=0.4, alpha=0.85)
    ax_cnt.set_facecolor("#F1F5F9")

    cost_colors = ["#94A3B8" if v == 0 else "#F97316" for v in costs]
    ax_cost.barh([short.get(d, d) for d in dtypes], costs, color=cost_colors, edgecolor="white", height=0.55)
    ax_cost.axvline(budget_limit, color="#DC2626", linestyle="--", linewidth=1.0, label="预算上限")
    ax_cost.set_xlabel("费用（元）", fontsize=10, color="#334155")
    ax_cost.set_title(f"费用构成：{total_cost} / {budget_limit} 元", fontsize=11, fontweight="bold", color="#0F172A")
    ax_cost.grid(axis="x", color="#CBD5E1", linewidth=0.4, alpha=0.85)
    ax_cost.legend(loc="lower right", fontsize=8)
    ax_cost.set_facecolor("#F1F5F9")

    ms_vals = [baseline_ms, after_ms]
    ms_labels = ["购买前", "购买后"]
    bars = ax_ms.bar(ms_labels, ms_vals, width=0.5, color=["#94A3B8", "#059669"], edgecolor="white")
    delta = baseline_ms - after_ms
    pct = delta / baseline_ms * 100.0 if baseline_ms > 0 else 0.0
    ax_ms.set_ylabel("Makespan（s）", fontsize=10, color="#334155")
    ax_ms.set_title(f"工期缩短 {delta}s（{pct:.2f}%）", fontsize=11, fontweight="bold", color="#0F172A")
    for b, v in zip(bars, ms_vals):
        ax_ms.text(b.get_x() + b.get_width() / 2, v + max(ms_vals) * 0.015, f"{v}\n{seconds_to_hhmmss(v)}", ha="center", va="bottom", fontsize=9, fontweight="bold")
    ax_ms.grid(axis="y", color="#CBD5E1", linewidth=0.4, alpha=0.85)
    ax_ms.set_facecolor("#F1F5F9")

    for ax in (ax_cnt, ax_cost, ax_ms):
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)

    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def plot_q4_outer_search_flow(
    baseline_ms: int,
    after_ms: int,
    total_cost: int,
    budget_limit: int,
    args: Any,
    out_png: Path,
) -> None:
    """论文辅助图：外层购买搜索与内层调度精化流程。"""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

    font = _pick_cjk_font()
    if font:
        plt.rcParams["font.sans-serif"] = [font, "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    fig, ax = plt.subplots(figsize=(14, 7.5), dpi=120, facecolor="#F8FAFC")
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)

    steps = [
        ("数据读取与展开", "读取三张Excel\n展开重复工序\n设备按班组建池"),
        ("零增购基线", f"Q3口径调度\nMakespan={baseline_ms}s\n{seconds_to_hhmmss(baseline_ms)}"),
        ("候选购置生成", f"瓶颈设备枚举\n预算≤{budget_limit}元\nbottle_max={args.bottle_max_per_type}"),
        ("贪心预筛", f"solve_problem4快速评估\n保留前{args.greedy_pre_rank}个候选"),
        ("CP-SAT精化", f"前{args.cpsat_refine_top}个候选\n每案{args.cpsat_refine_sec:g}s\nworkers={args.cpsat_workers}"),
        ("爬山加购", "在当前最优上\n尝试单台加购\n只接受严格改进"),
        ("最终抛光", f"固定最优购置\nCP-SAT {args.final_polish_sec:g}s\n二轮{args.second_final_polish_sec:g}s"),
        ("输出与校验", f"表4/表5/图表\n费用={total_cost}元\nMakespan={after_ms}s"),
    ]

    xs = [0.08, 0.31, 0.54, 0.77, 0.08, 0.31, 0.54, 0.77]
    ys = [0.68, 0.68, 0.68, 0.68, 0.28, 0.28, 0.28, 0.28]
    box_w, box_h = 0.18, 0.19
    colors = ["#DBEAFE", "#E0F2FE", "#FEF3C7", "#FFEDD5", "#DCFCE7", "#EDE9FE", "#FCE7F3", "#D1FAE5"]

    centers = []
    for idx, ((title, body), x, y, color) in enumerate(zip(steps, xs, ys, colors), 1):
        box = FancyBboxPatch(
            (x - box_w / 2, y - box_h / 2),
            box_w,
            box_h,
            boxstyle="round,pad=0.018,rounding_size=0.018",
            facecolor=color,
            edgecolor="#334155",
            linewidth=0.8,
        )
        ax.add_patch(box)
        ax.text(x, y + 0.052, f"{idx}. {title}", ha="center", va="center", fontsize=10.5, fontweight="bold", color="#0F172A")
        ax.text(x, y - 0.025, body, ha="center", va="center", fontsize=8.6, color="#334155", linespacing=1.2)
        centers.append((x, y))

    arrow_pairs = [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 7)]
    for a, b in arrow_pairs:
        x1, y1 = centers[a]
        x2, y2 = centers[b]
        start = (x1 + box_w / 2, y1) if y1 == y2 and x2 > x1 else (x1, y1 - box_h / 2)
        end = (x2 - box_w / 2, y2) if y1 == y2 and x2 > x1 else (x2, y2 + box_h / 2)
        ax.add_patch(
            FancyArrowPatch(
                start,
                end,
                arrowstyle="-|>",
                mutation_scale=13,
                linewidth=1.1,
                color="#475569",
                connectionstyle="arc3,rad=0.0",
            )
        )

    delta = baseline_ms - after_ms
    pct = delta / baseline_ms * 100.0 if baseline_ms > 0 else 0.0
    ax.text(
        0.5,
        0.92,
        f"问题4 外层检索图：购买方案搜索 + 内层CP-SAT调度精化（工期缩短 {delta}s，{pct:.2f}%）",
        ha="center",
        va="center",
        fontsize=14,
        fontweight="bold",
        color="#0F172A",
    )
    ax.text(
        0.5,
        0.06,
        "逻辑说明：外层只改变增购向量；每个候选方案都重新构造设备池，并用含同类并联、双设备同步、设备互斥与转运约束的内层调度求 Makespan。",
        ha="center",
        va="center",
        fontsize=9.5,
        color="#334155",
    )

    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def plot_q4_greedy_rank_and_refine_process(telemetry: Dict[str, Any], out_png: Path) -> None:
    """论文辅助图：贪心预筛排序轨迹 + CP-SAT 精化逐档评估（折线）。"""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    font = _pick_cjk_font()
    if font:
        plt.rcParams["font.sans-serif"] = [font, "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    ranks = telemetry.get("greedy_rank_makespans") or []
    refine = telemetry.get("cpsat_refine_curve") or []

    # 图例外置到坐标区右侧，避免遮挡右上「爬坡」线段；tight_layout 右侧留白。
    _legend_kw = {
        "loc": "upper left",
        "bbox_to_anchor": (1.01, 1.0),
        "fontsize": 8,
        "framealpha": 0.96,
        "borderaxespad": 0.35,
        "fancybox": True,
    }

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12.5, 8.6), dpi=120, facecolor="#F8FAFC", sharex=False)
    if ranks:
        xs = list(range(1, len(ranks) + 1))
        ax1.plot(xs, ranks, color="#64748B", linewidth=1.4, marker="o", markersize=3.2, label="贪心预筛 Makespan（按排序位次）")
        best_so_far: List[int] = []
        cur = 10**18
        for v in ranks:
            cur = min(cur, int(v))
            best_so_far.append(cur)
        ax1.plot(xs, best_so_far, color="#EA580C", linewidth=2.0, label="前缀最小（检索下界曲线）")
        ax1.set_xlabel("贪心排序位次（1=当前贪心最优候选）", fontsize=10, color="#334155")
        ax1.set_ylabel("Makespan（s）", fontsize=10, color="#334155")
        ax1.grid(axis="y", color="#CBD5E1", linewidth=0.4, alpha=0.85)
        ax1.set_facecolor("#F1F5F9")
        ax1.margins(x=0.02, y=0.06)
        ax1.legend(**_legend_kw)
    else:
        ax1.text(0.5, 0.5, "无贪心预筛样本", ha="center", va="center", fontsize=12, color="#64748B")
        ax1.set_axis_off()

    ax1.set_title("问题4 检索过程（折线）：贪心预筛排序与前缀最优下界", fontsize=12, fontweight="bold", color="#0F172A", pad=10)
    for spine in ("top", "right"):
        ax1.spines[spine].set_visible(False)

    if refine:
        idx = [int(p["i"]) + 1 for p in refine]
        gms = [int(p["greedy_ms"]) for p in refine]
        cms = [p["cpsat_ms"] for p in refine]
        cms_y = [float(v) if v is not None else float("nan") for v in cms]
        ax2.plot(idx, gms, color="#94A3B8", linewidth=1.2, marker="o", markersize=3.0, label="该档贪心 Makespan")
        ax2.plot(idx, cms_y, color="#2563EB", linewidth=1.4, marker="s", markersize=3.2, label="该档 CP-SAT Makespan（时限内）")
        imp_x = [int(p["i"]) + 1 for p in refine if p.get("improved")]
        imp_y = [float(p["cpsat_ms"]) for p in refine if p.get("improved") and p.get("cpsat_ms") is not None]
        if imp_x:
            ax2.scatter(imp_x, imp_y, color="#DC2626", s=38, zorder=5, label="该档触发全局改进")
        ax2.set_xlabel("CP-SAT 精化档序号（按贪心排序截取前 K）", fontsize=10, color="#334155")
        ax2.set_ylabel("Makespan（s）", fontsize=10, color="#334155")
        ax2.grid(axis="y", color="#CBD5E1", linewidth=0.4, alpha=0.85)
        ax2.set_facecolor("#F1F5F9")
        ax2.margins(x=0.04, y=0.08)
        ax2.legend(**_legend_kw)
    else:
        ax2.text(0.5, 0.5, "无 CP-SAT 精化记录", ha="center", va="center", fontsize=12, color="#64748B")
        ax2.set_axis_off()

    ax2.set_title("CP-SAT 精化逐档评估：贪心值 vs 精化值", fontsize=12, fontweight="bold", color="#0F172A", pad=10)
    for spine in ("top", "right"):
        ax2.spines[spine].set_visible(False)

    note = f"贪心候选样本数={len(ranks)}；CP-SAT 精化档数={len(refine)}"
    fig.suptitle("问题4 外层检索遥测（折线）", fontsize=13, fontweight="bold", color="#0F172A", y=0.995)
    fig.text(0.5, 0.012, note, ha="center", va="center", fontsize=9, color="#475569")

    # rect 右侧收紧，为图例留出画布空间（避免 savefig 裁切后图例压线）
    plt.tight_layout(rect=[0.03, 0.04, 0.78, 0.96])
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def plot_q4_hill_climb_trajectory(telemetry: Dict[str, Any], out_png: Path) -> None:
    """论文辅助图：爬山加购接受序列（工期与费用双轴折线）。"""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    font = _pick_cjk_font()
    if font:
        plt.rcParams["font.sans-serif"] = [font, "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    hist = telemetry.get("hill_climb") or []
    fig, ax1 = plt.subplots(figsize=(11.5, 6.2), dpi=120, facecolor="#F8FAFC")
    if not hist:
        ax1.text(0.5, 0.5, "无爬山记录（可能未触发接受步）", ha="center", va="center", fontsize=12, color="#64748B")
        ax1.set_axis_off()
    else:
        steps = [int(h["step"]) for h in hist]
        ms = [int(h["makespan"]) for h in hist]
        cs = [int(h["cost"]) for h in hist]
        ax1.step(steps, ms, where="post", color="#2563EB", linewidth=2.0, label="Makespan（s）")
        ax1.scatter(steps, ms, color="#1D4ED8", s=26, zorder=4)
        ax1.set_xlabel("爬山步（0=起点；每接受一次 +1）", fontsize=10, color="#334155")
        ax1.set_ylabel("Makespan（s）", fontsize=10, color="#1E3A8A")
        ax1.grid(axis="y", color="#CBD5E1", linewidth=0.4, alpha=0.85)
        ax1.set_facecolor("#F1F5F9")

        ax2 = ax1.twinx()
        ax2.step(steps, cs, where="post", color="#059669", linewidth=1.8, linestyle="--", label="累计购置费用（元）")
        ax2.scatter(steps, cs, color="#047857", s=22, zorder=4)
        ax2.set_ylabel("费用（元）", fontsize=10, color="#065F46")

        lines = ax1.get_lines() + ax2.get_lines()
        labels = [ln.get_label() for ln in lines]
        ax1.legend(
            lines,
            labels,
            loc="upper left",
            bbox_to_anchor=(1.01, 1.0),
            fontsize=8,
            framealpha=0.96,
            borderaxespad=0.35,
            fancybox=True,
        )

    ax1.set_title("问题4 爬坡（爬山加购）轨迹：工期下降与费用上升", fontsize=12, fontweight="bold", color="#0F172A", pad=10)
    for spine in ("top",):
        ax1.spines[spine].set_visible(False)

    plt.tight_layout(rect=[0.04, 0.06, 0.82, 0.96])
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def plot_q4_candidate_makespan_distribution(telemetry: Dict[str, Any], out_png: Path) -> None:
    """论文辅助图：候选购置在贪心预筛口径下的 Makespan 分布（箱图/散点降级）。"""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    font = _pick_cjk_font()
    if font:
        plt.rcParams["font.sans-serif"] = [font, "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    vals = [int(v) for v in (telemetry.get("greedy_rank_makespans") or [])]
    fig, ax = plt.subplots(figsize=(10.5, 6.0), dpi=120, facecolor="#F8FAFC")
    ax.set_facecolor("#F1F5F9")
    if len(vals) >= 5:
        ax.boxplot(vals, vert=True, showmeans=True, meanline=True, labels=["贪心预筛候选集"])
        ax.set_ylabel("Makespan（s）", fontsize=10, color="#334155")
        ax.set_title("问题4 候选购置 Makespan 分布（箱图）", fontsize=12, fontweight="bold", color="#0F172A", pad=10)
    elif vals:
        ax.scatter([1] * len(vals), vals, color="#2563EB", s=34, alpha=0.85, edgecolors="white", linewidths=0.6)
        ax.set_xticks([1])
        ax.set_xticklabels(["贪心预筛候选集"])
        ax.set_ylabel("Makespan（s）", fontsize=10, color="#334155")
        ax.set_title(f"问题4 候选购置 Makespan 分布（样本={len(vals)}，箱图降级为散点）", fontsize=12, fontweight="bold", color="#0F172A", pad=10)
    else:
        ax.text(0.5, 0.5, "无分布样本", ha="center", va="center", fontsize=12, color="#64748B")
        ax.set_axis_off()

    ax.grid(axis="y", color="#CBD5E1", linewidth=0.4, alpha=0.85)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)

    plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def plot_q4_global_best_evolution(telemetry: Dict[str, Any], out_png: Path) -> None:
    """论文辅助图：外层检索过程中「全局最优 Makespan」的阶梯式演进（贪心Top1→CP改进→爬山→抛光）。"""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    font = _pick_cjk_font()
    if font:
        plt.rcParams["font.sans-serif"] = [font, "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    curve = telemetry.get("global_best_curve") or []
    fig, ax = plt.subplots(figsize=(11.5, 5.8), dpi=120, facecolor="#F8FAFC")
    ax.set_facecolor("#F1F5F9")

    def _short_stage(st: str) -> str:
        if st.startswith("cpsat_refine_"):
            return "CP#" + st.rsplit("_", 1)[-1]
        return {
            "greedy_top1": "贪心Top1",
            "hill_climb": "爬山",
            "final_polish_1": "抛光①",
            "final_polish_2": "抛光②",
        }.get(st, st[:14])

    if not curve:
        ax.text(0.5, 0.5, "无全局最优演进记录", ha="center", va="center", fontsize=12, color="#64748B")
        ax.set_axis_off()
    else:
        xs = list(range(len(curve)))
        ms = [int(p["makespan"]) for p in curve]
        ax.step(xs, ms, where="post", color="#7C3AED", linewidth=2.2, label="全局最优 Makespan")
        ax.scatter(xs, ms, color="#5B21B6", s=40, zorder=4, edgecolors="white", linewidths=0.6)
        ax.set_xticks(xs)
        ax.set_xticklabels([_short_stage(str(p.get("stage", ""))) for p in curve], rotation=28, ha="right", fontsize=8.5)
        ax.set_ylabel("Makespan（s）", fontsize=10, color="#334155")
        ax.set_xlabel("检索阶段（仅记录发生接受/改进的节点）", fontsize=10, color="#334155")
        ax.grid(axis="y", color="#CBD5E1", linewidth=0.4, alpha=0.85)
        ax.margins(x=0.02, y=0.12)
        if len(ms):
            last_x, last_y = xs[-1], ms[-1]
            ax.annotate(
                f"{last_y}s\n{seconds_to_hhmmss(last_y)}",
                xy=(last_x, last_y),
                xytext=(10, 12),
                textcoords="offset points",
                fontsize=9,
                fontweight="bold",
                color="#4C1D95",
                bbox=dict(boxstyle="round,pad=0.25", facecolor="#F5F3FF", edgecolor="#7C3AED", alpha=0.95),
            )
        ax.legend(loc="upper right", fontsize=9)

    ax.set_title("问题4 全局最优 Makespan 演进（阶梯折线）", fontsize=12, fontweight="bold", color="#0F172A", pad=10)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)

    plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def plot_q4_legacy_stage_curve(telemetry: Dict[str, Any], out_png: Path) -> None:
    """论文辅助图：启发式（legacy）路径的阶段最优折线（样本稀疏，不做箱图）。"""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    font = _pick_cjk_font()
    if font:
        plt.rcParams["font.sans-serif"] = [font, "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    pts = telemetry.get("stage_points") or []
    fig, ax = plt.subplots(figsize=(11.5, 6.0), dpi=120, facecolor="#F8FAFC")
    if len(pts) < 2:
        ax.text(0.5, 0.5, "无阶段记录", ha="center", va="center", fontsize=12, color="#64748B")
        ax.set_axis_off()
    else:
        xs = list(range(len(pts)))
        ms = [int(p["makespan"]) for p in pts]
        cs = [int(p["cost"]) for p in pts]
        labels = [str(p.get("stage", "")) for p in pts]
        ax.plot(xs, ms, color="#2563EB", linewidth=2.0, marker="o", label="Makespan（s）")
        ax.set_xticks(xs)
        ax.set_xticklabels(labels, rotation=18, ha="right", fontsize=9)
        ax.set_ylabel("Makespan（s）", fontsize=10, color="#334155")
        ax.grid(axis="y", color="#CBD5E1", linewidth=0.4, alpha=0.85)
        ax.set_facecolor("#F1F5F9")

        ax2 = ax.twinx()
        ax2.plot(xs, cs, color="#059669", linewidth=1.8, linestyle="--", marker="s", label="费用（元）")
        ax2.set_ylabel("费用（元）", fontsize=10, color="#065F46")
        lines = ax.get_lines() + ax2.get_lines()
        labs = [ln.get_label() for ln in lines]
        ax.legend(lines, labs, loc="upper right", fontsize=9)

    ax.set_title("问题4 启发式购买搜索：阶段最优（--no-cpsat）", fontsize=12, fontweight="bold", color="#0F172A", pad=10)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)

    plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def update_dev_doc(dev_path: Path, content: str) -> None:
    dev_path.write_text(content, encoding="utf-8")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="问题4：预算内购买与调度",
        epilog="极参一键：python src/Q4/solve_q4.py --preset maximum --no-gantt  （与单项 --cpsat-* 不要混用，后者会被 preset 覆盖）",
    )
    parser.add_argument(
        "--preset",
        choices=tuple(PRESET_Q4.keys()),
        default="balanced",
        help="balanced=默认常量；strong=加强；maximum=极参（长时，见 PRESET_Q4）",
    )
    parser.add_argument(
        "--no-gantt",
        action="store_true",
        help="不绘制甘特图与柱状图 PNG",
    )
    parser.add_argument(
        "--no-cpsat",
        action="store_true",
        help="禁用 CP-SAT 主路径，仅使用启发式购买搜索 + solve_problem4 贪心调度",
    )
    parser.add_argument(
        "--cpsat-refine-sec",
        type=float,
        default=CPSAT_REFINE_TIME_SEC,
        help=f"每套候选购置方案的 CP-SAT 精化时限（秒），默认 {CPSAT_REFINE_TIME_SEC:g}；越大越可能更优但更慢",
    )
    parser.add_argument(
        "--cpsat-refine-top",
        type=int,
        default=CPSAT_REFINE_TOP,
        help=f"贪心预筛后对前 K 套方案做 CP-SAT，默认 {CPSAT_REFINE_TOP}",
    )
    parser.add_argument(
        "--greedy-pre-rank",
        type=int,
        default=GREEDY_PRE_RANK_KEEP,
        help=f"贪心预筛至少评多少套方案再排序取前 K，默认 {GREEDY_PRE_RANK_KEEP}",
    )
    parser.add_argument(
        "--bottle-max-per-type",
        type=int,
        default=BOTTLENECK_ENUM_MAX_PER_TYPE,
        help=f"瓶颈枚举每类最多台数上界，默认 {BOTTLENECK_ENUM_MAX_PER_TYPE}",
    )
    parser.add_argument(
        "--final-polish-sec",
        type=float,
        default=CPSAT_FINAL_POLISH_SEC,
        help="已定最优购置后对最终方案再跑 CP-SAT 的加长时限（秒）；设为 0 关闭抛光",
    )
    parser.add_argument(
        "--second-final-polish-sec",
        type=float,
        default=CPSAT_SECOND_POLISH_SEC,
        help="第一轮抛光后再跑第二轮 CP-SAT 的时限（秒）；0 关闭。maximum 预设为 140",
    )
    parser.add_argument(
        "--cpsat-workers",
        type=int,
        default=DEFAULT_CPSAT_NUM_WORKERS,
        help=f"问题4内层 CP-SAT 线程数，默认 {DEFAULT_CPSAT_NUM_WORKERS}",
    )
    args = parser.parse_args()
    _apply_q4_preset_args(args)

    _q4_reset_timer()
    _q4_log("启动：读取 Excel 与解析数据…")
    if getattr(args, "preset", "balanced") != "balanced":
        _q4_log(
            f"已应用 --preset {args.preset}：refine={args.cpsat_refine_sec}s×{args.cpsat_refine_top}，"
            f"greedy_rank={args.greedy_pre_rank}，bottle_max={args.bottle_max_per_type}，"
            f"polish1={args.final_polish_sec}s，polish2={args.second_final_polish_sec}s，workers={args.cpsat_workers}"
        )

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
    _q4_log(
        f"数据就绪：展开工序 {len(expanded)} 条，班组1+2 设备 {len(base_devices)} 台；"
        f"开始购买方案搜索（预算 {budget_limit} 元）"
    )

    purchase_counts, records, makespan, total_cost, baseline_records, baseline_makespan, search_telemetry = _search_purchase_plan(
        expanded=expanded,
        base_devices=base_devices,
        dist_map=dist_map,
        budget_limit=budget_limit,
        raw_repeat_map=raw_repeat_map,
        use_cpsat=not args.no_cpsat,
        cpsat_refine_sec=args.cpsat_refine_sec,
        cpsat_refine_top=args.cpsat_refine_top,
        greedy_pre_rank_keep=args.greedy_pre_rank,
        bottleneck_max_per_type=args.bottle_max_per_type,
        final_polish_sec=args.final_polish_sec,
        second_final_polish_sec=args.second_final_polish_sec,
        cpsat_workers=args.cpsat_workers,
    )
    _q4_log("搜索结束：正在合并增购设备列表并导出表4/表5…")
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
    _q4_log("表4、表5 已写出；正在校验可行性…")

    report = validate_schedule(expanded, records, final_devices, dist_map, raw_repeat_map, makespan)
    budget_errs = validate_budget(total_cost=total_cost, budget_limit=budget_limit)
    budget_text = "通过\n" if not budget_errs else "\n".join(budget_errs) + "\n"
    report = report + "\n## 预算检查\n" + budget_text
    report = (
        report
        + f"\n## 购买前后工期（内层调度口径一致）\n"
        f"购买前 Makespan = {baseline_makespan} s（{seconds_to_hhmmss(baseline_makespan)}）\n"
        f"购买后 Makespan = {makespan} s（{seconds_to_hhmmss(makespan)}）\n"
        f"缩短 {baseline_makespan - makespan} s\n"
    )
    (out_dir / "validation_report.txt").write_text(report, encoding="utf-8")
    _q4_log("校验报告已写入。")

    gantt_before = out_dir / "表4_问题4甘特图_购买前.png"
    gantt_after = out_dir / "表4_问题4甘特图_购买后.png"
    bars_after = out_dir / "表4_问题4车间统计柱状图_购买后.png"
    compare_png = out_dir / "表4_问题4购买前后对比.png"
    purchase_search_png = out_dir / "表4_问题4最优购置方案检索图.png"
    outer_search_png = out_dir / "表4_问题4外层搜索流程图.png"
    process_line_png = out_dir / "表4_问题4检索过程折线图.png"
    hill_png = out_dir / "表4_问题4爬山加购轨迹图.png"
    box_png = out_dir / "表4_问题4候选购置Makespan箱图.png"
    global_best_png = out_dir / "表4_问题4全局最优演进图.png"
    legacy_stage_png = out_dir / "表4_问题4启发式阶段检索折线图.png"
    if not args.no_gantt:
        _q4_log("正在绘制甘特图与对比柱状图（matplotlib）…")
        try:
            plot_device_gantt_q4(
                baseline_records,
                baseline_makespan,
                gantt_before,
                title_tag="购买前（零增购）",
            )
            plot_device_gantt_q4(
                records,
                makespan,
                gantt_after,
                title_tag="购买后（最优方案）",
            )
            plot_workshop_bars_after_q4(records, expanded, makespan, bars_after)
            plot_q4_before_after_comparison(
                expanded,
                baseline_records,
                baseline_makespan,
                records,
                makespan,
                total_cost,
                compare_png,
            )
            plot_q4_purchase_search_summary(
                purchase_counts,
                base_devices,
                baseline_makespan,
                makespan,
                total_cost,
                budget_limit,
                purchase_search_png,
            )
            plot_q4_outer_search_flow(
                baseline_makespan,
                makespan,
                total_cost,
                budget_limit,
                args,
                outer_search_png,
            )
            mode = str(search_telemetry.get("mode", ""))
            if mode == "cpsat_primary":
                plot_q4_greedy_rank_and_refine_process(search_telemetry, process_line_png)
                plot_q4_hill_climb_trajectory(search_telemetry, hill_png)
                plot_q4_candidate_makespan_distribution(search_telemetry, box_png)
                plot_q4_global_best_evolution(search_telemetry, global_best_png)
            elif mode == "legacy":
                plot_q4_legacy_stage_curve(search_telemetry, legacy_stage_png)
        except ImportError:
            print("未安装 matplotlib，跳过图表。请执行: pip install matplotlib")
        except OSError as e:
            print(f"图表保存失败: {e}")
        else:
            _q4_log("全部图表已保存。")

    try:
        from Qbase.schedule_charts import plot_advanced_viz_bundle

        plot_advanced_viz_bundle(
            records,
            expanded,
            makespan,
            out_dir,
            "表4_问题4购买后",
            "问题4（购买后最优）",
            len(final_devices),
        )
        plot_advanced_viz_bundle(
            baseline_records,
            expanded,
            baseline_makespan,
            out_dir,
            "表4_问题4购买前",
            "问题4（零增购基线）",
            len(base_devices),
        )
        _q4_log("三维/雷达/热力分析图（购买前、购买后各一套）已保存。")
    except ImportError:
        print("未安装 matplotlib，跳过三维/雷达/热力图。请执行: pip install matplotlib")
    except OSError as e:
        print(f"三维/雷达/热力图保存失败: {e}")

    print(f"Makespan = {makespan} s")
    print(f"购买前 Makespan（零增购）= {baseline_makespan} s")
    print(f"购买总费用 = {total_cost} 元")
    print(f"已导出: {out_table4}")
    print(f"已导出: {out_table5}")
    print(f"校验报告: {out_dir / 'validation_report.txt'}")
    if not args.no_gantt:
        print(f"甘特图（购买前）: {gantt_before}")
        print(f"甘特图（购买后）: {gantt_after}")
        print(f"车间柱状图（购买后）: {bars_after}")
        print(f"购买前后对比图: {compare_png}")
        print(f"最优购置方案检索图: {purchase_search_png}")
        print(f"外层搜索流程图: {outer_search_png}")
        if str(search_telemetry.get("mode", "")) == "cpsat_primary":
            print(f"检索过程折线图: {process_line_png}")
            print(f"爬山加购轨迹图: {hill_png}")
            print(f"候选购置Makespan箱图: {box_png}")
            print(f"全局最优演进图: {global_best_png}")
        elif str(search_telemetry.get("mode", "")) == "legacy":
            print(f"启发式阶段检索折线图: {legacy_stage_png}")
    print(
        "分析图（三维/雷达/热力）: 表4_问题4购买后_三维调度散点.png、表4_问题4购买后_雷达综合评价.png、"
        "表4_问题4购买后_热力图设备时间占用.png；购买前对应三文件名前缀「表4_问题4购买前_」。"
    )


if __name__ == "__main__":
    main()
