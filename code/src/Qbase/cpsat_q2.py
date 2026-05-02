"""
问题2 / 问题3：OR-Tools CP-SAT 析取图调度（solve_q2_cpsat / solve_q3_cpsat；均返回四元组，第 4 项为三子链 makespan 与状态，供 `schedule_charts` 绘制求解过程对比图）。
并联台数 n 为决策变量；每台设备零时长虚拟首工序约束从班组出发的转运。
两阶段：先最小化 makespan，再固定 makespan 最小化真实工序开工之和并带解提示，以压缩时间轴、促进多车间尽早并行。

默认求解资源见模块常量 DEFAULT_CPSAT_NUM_WORKERS、DEFAULT_CPSAT_TIME_LIMIT_SEC（按 24 核级 CPU + 32G 内存调参，可被 CLI 覆盖）。
"""
from __future__ import annotations

import itertools
import math
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from ortools.sat.python import cp_model

from .parsers import Device, ExpandedOperation, distance_between
from .scheduler_common import ScheduleRecord
from .time_utils import calc_transport_time, calc_work_duration
from .validators import (
    validate_device_conflicts,
    validate_device_type_match,
    validate_duration_fields,
    validate_precedence,
    validate_repeat_counts,
    validate_transport,
)

# 参考：i9-13980HX 24 物理核 + 32G 内存匹配的默认 CP-SAT 资源。
# 线程数取物理核规模以喂满并行搜索；时限略放宽供三链（legacy/fixed/variable）分预算求解。
# 其他机器可用 solve_q2.py / solve_q3.py 的 --workers、--time-limit 覆盖。
DEFAULT_CPSAT_NUM_WORKERS = 24
DEFAULT_CPSAT_TIME_LIMIT_SEC = 300.0


def _workshop_node(ws: str) -> str:
    return str(ws).strip().upper().replace("车间", "")


def _build_predecessors(expanded: List[ExpandedOperation]) -> Dict[str, List[str]]:
    preds: Dict[str, List[str]] = {o.op_id: [] for o in expanded}
    by_ws: Dict[str, List[ExpandedOperation]] = defaultdict(list)
    for o in expanded:
        by_ws[o.workshop].append(o)
    for ops in by_ws.values():
        ops.sort(key=lambda x: (x.sequence_key, x.op_id))
        for i in range(1, len(ops)):
            preds[ops[i].op_id].append(ops[i - 1].op_id)
    return preds


def _legacy_pair_travel_sec(
    dist_map: Dict[Tuple[str, str], float],
    loc_a: str,
    loc_b: str,
) -> int:
    """与 othersolve.trans 一致：车间节点 A–E 间距离 / 2 向上取整；同车间为 0。"""
    na, nb = _workshop_node(loc_a), _workshop_node(loc_b)
    if na == nb:
        return 0
    d_m = float(distance_between(dist_map, na, nb))
    return int(math.ceil(d_m / 2.0))


def _q2_validation_errors(
    expanded: List[ExpandedOperation],
    records: List[ScheduleRecord],
    devices: List[Device],
    dist_map: Dict[Tuple[str, str], float],
    raw_repeat_map: Dict[str, int],
) -> List[str]:
    errs: List[str] = []
    errs.extend(validate_repeat_counts(raw_repeat_map, expanded))
    errs.extend(validate_precedence(expanded, records))
    errs.extend(validate_device_type_match(expanded, records))
    errs.extend(validate_device_conflicts(records))
    errs.extend(validate_duration_fields(records))
    errs.extend(validate_transport(records, devices, dist_map))
    return errs


def _repair_depot_timeline(
    expanded: List[ExpandedOperation],
    records: List[ScheduleRecord],
    team1: List[Device],
    dist_map: Dict[Tuple[str, str], float],
) -> List[ScheduleRecord]:
    """
    保持每台设备上的工序顺序与各条 duration 不变，仅整体平移各工序开工，
    使从班组初始位置起的运输时间与车间前驱关系同时满足。
    """
    preds = _build_predecessors(expanded)
    op_by_id = {o.op_id: o for o in expanded}
    all_oids = list({r.op_id for r in records})
    dev_map = {d.device_id: d for d in team1}

    cp_start: Dict[str, int] = {}
    op_span: Dict[str, int] = {}
    for oid in all_oids:
        rs = [r for r in records if r.op_id == oid]
        cp_start[oid] = min(x.start_sec for x in rs)
        op_span[oid] = max(x.end_sec for x in rs) - min(x.start_sec for x in rs)

    op_start_new: Dict[str, int] = {}
    op_end_new: Dict[str, int] = {}
    device_ready = {d.device_id: 0 for d in team1}
    device_loc = {d.device_id: _workshop_node(d.initial_location) for d in team1}
    done: set[str] = set()

    def pick_next() -> Optional[str]:
        cand = [
            oid
            for oid in all_oids
            if oid not in done and all(p in done for p in preds[oid])
        ]
        if not cand:
            return None
        return min(cand, key=lambda x: (cp_start[x], x))

    while len(done) < len(all_oids):
        oid = pick_next()
        if oid is None:
            raise RuntimeError("legacy repair: 无法按前驱拓扑展开工序")
        o = op_by_id[oid]
        ws_node = _workshop_node(o.workshop)
        lb = 0
        for p in preds[oid]:
            lb = max(lb, op_end_new[p])
        rs = [r for r in records if r.op_id == oid]
        for r in rs:
            d = dev_map[r.device_id]
            tr = calc_transport_time(
                distance_between(dist_map, device_loc[r.device_id], ws_node),
                d.speed,
            )
            lb = max(lb, device_ready[r.device_id] + tr)
        s_new = lb
        for r in rs:
            dur = r.duration_sec
            device_ready[r.device_id] = s_new + dur
            device_loc[r.device_id] = ws_node
        op_start_new[oid] = s_new
        op_end_new[oid] = s_new + op_span[oid]
        done.add(oid)

    out: List[ScheduleRecord] = []
    seq = 1
    for oid in sorted(all_oids, key=lambda x: (op_start_new[x], x)):
        o = op_by_id[oid]
        s_new = op_start_new[oid]
        delta = s_new - cp_start[oid]
        for r in sorted((rr for rr in records if rr.op_id == oid), key=lambda x: (x.device_type, x.device_id)):
            out.append(
                ScheduleRecord(
                    seq=seq,
                    device_id=r.device_id,
                    team=r.team,
                    device_type=r.device_type,
                    workshop=r.workshop,
                    raw_id=r.raw_id,
                    op_id=r.op_id,
                    repeat_index=r.repeat_index,
                    start_sec=r.start_sec + delta,
                    end_sec=r.end_sec + delta,
                    duration_sec=r.duration_sec,
                    transport_sec=0,
                )
            )
            seq += 1

    by_dev: Dict[str, List[ScheduleRecord]] = defaultdict(list)
    for r in out:
        by_dev[r.device_id].append(r)
    for did, lst in by_dev.items():
        lst.sort(key=lambda x: (x.start_sec, x.seq))
        d = dev_map[did]
        prev_node = d.initial_location
        prev_end = 0
        for r in lst:
            node = _workshop_node(r.workshop)
            if prev_node == node:
                need = 0
            else:
                need = calc_transport_time(distance_between(dist_map, prev_node, node), d.speed)
            r.transport_sec = need
            prev_node, prev_end = node, r.end_sec
    return out


def _isolated_best_alloc(
    op: ExpandedOperation,
    count_by_type: Dict[str, int],
) -> Tuple[Dict[str, Tuple[int, int]], int]:
    """
    与 othersolve 的 op_dev_durations 一致：单类用满台数；双类枚举 (n1,n2) 使 max(阶段时长) 最小。
    返回 alloc[device_type] = (台数, 该类持续秒数), op_dur。
    """
    reqs = op.requirements
    if len(reqs) == 1:
        r = reqs[0]
        dt = r["device_type"]
        n = max(1, count_by_type.get(dt, 1))
        dur = calc_work_duration(op.quantity, float(r["efficiency"]) * n, r.get("unit"))
        return {dt: (n, dur)}, dur
    if len(reqs) == 2:
        r1, r2 = reqs[0], reqs[1]
        d1, d2 = r1["device_type"], r2["device_type"]
        c1, c2 = max(1, count_by_type.get(d1, 1)), max(1, count_by_type.get(d2, 1))
        best_t, best = 10**18, {}
        for n1, n2 in itertools.product(range(1, c1 + 1), range(1, c2 + 1)):
            t1 = calc_work_duration(op.quantity, float(r1["efficiency"]) * n1, r1.get("unit"))
            t2 = calc_work_duration(op.quantity, float(r2["efficiency"]) * n2, r2.get("unit"))
            mx = max(t1, t2)
            if mx < best_t or (mx == best_t and n1 + n2 < sum(best[d][0] for d in best)):
                best_t = mx
                best = {d1: (n1, t1), d2: (n2, t2)}
        return best, best_t
    raise ValueError(f"工序 {op.op_id} 设备种类数不为 1 或 2")


def _device_key(devices: List[Device]) -> List[Tuple[str, int, Device]]:
    by_t: Dict[str, List[Device]] = defaultdict(list)
    for d in devices:
        by_t[d.device_type].append(d)
    for t in by_t:
        by_t[t].sort(key=lambda x: x.device_id)
    out: List[Tuple[str, int, Device]] = []
    for t in sorted(by_t.keys()):
        for k, dev in enumerate(by_t[t]):
            out.append((t, k, dev))
    return out


def _q2_cp_model_ctx(
    expanded: List[ExpandedOperation],
    team1: List[Device],
    dist_map: Dict[Tuple[str, str], float],
    horizon: int,
    fixed_alloc: Optional[Dict[str, Tuple[Dict[str, Tuple[int, int]], int]]] = None,
    legacy_othersolve: bool = False,
) -> Dict[str, Any]:
    """
    构建 CP 模型与提取解所需的上下文（不含目标函数）。
    fixed_alloc 非空时与 othersolve 一致：每道工序并联台数与阶段时长离線固定，CP 仅排顺序与分配机号。
    legacy_othersolve=True 时与 othersolve 析取模型同构：无虚拟首工序，车间间转运为 ceil(d/2)（与赛题 2m/s 一致）。
    """
    count_by_type: Dict[str, int] = defaultdict(int)
    for d in team1:
        count_by_type[d.device_type] += 1

    preds = _build_predecessors(expanded)
    real_op_ids = [o.op_id for o in expanded]
    op_by_id = {o.op_id: o for o in expanded}

    dev_keys = _device_key(team1)
    dummy_ids: List[str] = []
    dummy_for_key: Dict[Tuple[str, int], str] = {}
    if not legacy_othersolve:
        for dtype, k, _dev in dev_keys:
            did = f"__START__{dtype}__{k}"
            dummy_ids.append(did)
            dummy_for_key[(dtype, k)] = did

    op_ids = list(real_op_ids) if legacy_othersolve else real_op_ids + dummy_ids

    op_info: Dict[str, dict] = {}
    for o in expanded:
        op_info[o.op_id] = {
            "workshop": o.workshop,
            "preds": preds[o.op_id],
            "is_dummy": False,
            "raw_id": o.raw_id,
            "repeat_index": o.repeat_index,
            "reqs": o.requirements,
        }
    if not legacy_othersolve:
        for dtype, k, dev in dev_keys:
            did = dummy_for_key[(dtype, k)]
            op_info[did] = {
                "workshop": dev.initial_location,
                "preds": [],
                "is_dummy": True,
                "raw_id": did,
                "repeat_index": 0,
                "dtype": dtype,
                "k": k,
            }

    model = cp_model.CpModel()
    op_start: Dict[str, cp_model.IntVar] = {}
    op_end: Dict[str, cp_model.IntVar] = {}
    op_cp: Dict[str, Dict[str, Any]] = {}

    for oid in op_ids:
        s = model.NewIntVar(0, horizon, f"s_{oid}")
        e = model.NewIntVar(0, horizon, f"e_{oid}")
        op_start[oid] = s
        op_end[oid] = e

    if not legacy_othersolve:
        for did in dummy_ids:
            model.Add(op_start[did] == 0)
            model.Add(op_end[did] == 0)

    assign: Dict[str, Dict[str, Dict[int, cp_model.BoolVar]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    dev_end_var: Dict[str, Dict[str, cp_model.IntVar]] = defaultdict(dict)
    dev_intervals_per_device: Dict[Tuple[str, int], List[Tuple[str, cp_model.IntervalVar, cp_model.BoolVar]]] = (
        defaultdict(list)
    )

    for oid in real_op_ids:
        inf = op_info[oid]
        o = op_by_id[oid]
        reqs = inf["reqs"]
        s = op_start[oid]
        use_fixed = fixed_alloc is not None and oid in fixed_alloc

        if len(reqs) == 1:
            r = reqs[0]
            dtype = r["device_type"]
            n_max = max(1, count_by_type[dtype])
            if use_fixed:
                alloc_f, _op_dur_f = fixed_alloc[oid]
                n_need, dev_t = alloc_f[dtype]
                dev_t = int(dev_t)
                model.Add(op_end[oid] == s + dev_t)
                for kk in range(n_max):
                    assign[oid][dtype][kk] = model.NewBoolVar(f"asgn_{oid}_{dtype}_{kk}")
                model.Add(sum(assign[oid][dtype][kk] for kk in range(n_max)) == n_need)
                dev_done = model.NewIntVar(0, horizon, f"devdone_{oid}_{dtype}")
                model.Add(dev_done == s + dev_t)
                dev_end_var[oid][dtype] = dev_done
                for kk in range(n_max):
                    b = assign[oid][dtype][kk]
                    opt_itv = model.NewOptionalIntervalVar(s, dev_t, dev_done, b, f"ditv_{oid}_{dtype}_{kk}")
                    dev_intervals_per_device[(dtype, kk)].append((oid, opt_itv, b))
            else:
                dur_tab = [
                    calc_work_duration(o.quantity, float(r["efficiency"]) * (i + 1), r.get("unit"))
                    for i in range(n_max)
                ]
                n_var = model.NewIntVar(1, n_max, f"n_{oid}")
                dur_var = model.NewIntVar(0, horizon, f"dur_{oid}")
                model.AddElement(n_var - 1, dur_tab, dur_var)
                model.Add(op_end[oid] == s + dur_var)
                for kk in range(n_max):
                    assign[oid][dtype][kk] = model.NewBoolVar(f"asgn_{oid}_{dtype}_{kk}")
                model.Add(sum(assign[oid][dtype][kk] for kk in range(n_max)) == n_var)
                dev_done = model.NewIntVar(0, horizon, f"devdone_{oid}_{dtype}")
                model.Add(dev_done == s + dur_var)
                dev_end_var[oid][dtype] = dev_done
                for kk in range(n_max):
                    b = assign[oid][dtype][kk]
                    opt_itv = model.NewOptionalIntervalVar(s, dur_var, dev_done, b, f"ditv_{oid}_{dtype}_{kk}")
                    dev_intervals_per_device[(dtype, kk)].append((oid, opt_itv, b))
            op_cp[oid] = {"kind": "one", "dtype": dtype}

        elif len(reqs) == 2:
            r1, r2 = reqs[0], reqs[1]
            d1, d2 = r1["device_type"], r2["device_type"]
            c1, c2 = max(1, count_by_type[d1]), max(1, count_by_type[d2])
            if use_fixed:
                alloc_f, op_dur_f = fixed_alloc[oid]
                op_dur_f = int(op_dur_f)
                n1f, dur1f = alloc_f[d1]
                n2f, dur2f = alloc_f[d2]
                dur1f, dur2f = int(dur1f), int(dur2f)
                model.Add(op_end[oid] == s + op_dur_f)
                for kk in range(c1):
                    assign[oid][d1][kk] = model.NewBoolVar(f"asgn_{oid}_{d1}_{kk}")
                for kk in range(c2):
                    assign[oid][d2][kk] = model.NewBoolVar(f"asgn_{oid}_{d2}_{kk}")
                model.Add(sum(assign[oid][d1][kk] for kk in range(c1)) == n1f)
                model.Add(sum(assign[oid][d2][kk] for kk in range(c2)) == n2f)
                dd1 = model.NewIntVar(0, horizon, f"devdone_{oid}_{d1}")
                dd2 = model.NewIntVar(0, horizon, f"devdone_{oid}_{d2}")
                model.Add(dd1 == s + dur1f)
                model.Add(dd2 == s + dur2f)
                dev_end_var[oid][d1] = dd1
                dev_end_var[oid][d2] = dd2
                for kk in range(c1):
                    b = assign[oid][d1][kk]
                    opt_itv = model.NewOptionalIntervalVar(s, dur1f, dd1, b, f"ditv_{oid}_{d1}_{kk}")
                    dev_intervals_per_device[(d1, kk)].append((oid, opt_itv, b))
                for kk in range(c2):
                    b = assign[oid][d2][kk]
                    opt_itv = model.NewOptionalIntervalVar(s, dur2f, dd2, b, f"ditv_{oid}_{d2}_{kk}")
                    dev_intervals_per_device[(d2, kk)].append((oid, opt_itv, b))
            else:
                tab1 = [
                    calc_work_duration(o.quantity, float(r1["efficiency"]) * (i + 1), r1.get("unit"))
                    for i in range(c1)
                ]
                tab2 = [
                    calc_work_duration(o.quantity, float(r2["efficiency"]) * (i + 1), r2.get("unit"))
                    for i in range(c2)
                ]
                n1 = model.NewIntVar(1, c1, f"n1_{oid}")
                n2 = model.NewIntVar(1, c2, f"n2_{oid}")
                dur1 = model.NewIntVar(0, horizon, f"d1_{oid}")
                dur2 = model.NewIntVar(0, horizon, f"d2_{oid}")
                model.AddElement(n1 - 1, tab1, dur1)
                model.AddElement(n2 - 1, tab2, dur2)
                op_dur = model.NewIntVar(0, horizon, f"opdur_{oid}")
                model.AddMaxEquality(op_dur, [dur1, dur2])
                model.Add(op_end[oid] == s + op_dur)
                for kk in range(c1):
                    assign[oid][d1][kk] = model.NewBoolVar(f"asgn_{oid}_{d1}_{kk}")
                for kk in range(c2):
                    assign[oid][d2][kk] = model.NewBoolVar(f"asgn_{oid}_{d2}_{kk}")
                model.Add(sum(assign[oid][d1][kk] for kk in range(c1)) == n1)
                model.Add(sum(assign[oid][d2][kk] for kk in range(c2)) == n2)
                dd1 = model.NewIntVar(0, horizon, f"devdone_{oid}_{d1}")
                dd2 = model.NewIntVar(0, horizon, f"devdone_{oid}_{d2}")
                model.Add(dd1 == s + dur1)
                model.Add(dd2 == s + dur2)
                dev_end_var[oid][d1] = dd1
                dev_end_var[oid][d2] = dd2
                for kk in range(c1):
                    b = assign[oid][d1][kk]
                    opt_itv = model.NewOptionalIntervalVar(s, dur1, dd1, b, f"ditv_{oid}_{d1}_{kk}")
                    dev_intervals_per_device[(d1, kk)].append((oid, opt_itv, b))
                for kk in range(c2):
                    b = assign[oid][d2][kk]
                    opt_itv = model.NewOptionalIntervalVar(s, dur2, dd2, b, f"ditv_{oid}_{d2}_{kk}")
                    dev_intervals_per_device[(d2, kk)].append((oid, opt_itv, b))
            op_cp[oid] = {"kind": "two", "d1": d1, "d2": d2}
        else:
            raise ValueError(f"工序 {oid} 设备种类数不为 1 或 2")

    for oid in op_ids:
        for pred in op_info[oid]["preds"]:
            model.Add(op_start[oid] >= op_end[pred])

    if not legacy_othersolve:
        # 每台实体机 (dtype, k) 仅挂一条 dummy 可选区间到对应槽位；禁止把各台 dummy 扫进所有 kk，
        # 否则 pairwise 转运会在建模样阶段枚举到「班组1 dummy vs 班组2 dummy」等永不同机为真的点对并查距离。
        for did in dummy_ids:
            inf = op_info[did]
            dtype, k = inf["dtype"], inf["k"]
            s = op_start[did]
            dev_done = model.NewIntVar(0, horizon, f"devdone_{did}_{dtype}")
            model.Add(dev_done == 0)
            dev_end_var[did][dtype] = dev_done
            b = model.NewBoolVar(f"asgn_{did}_{dtype}_{k}")
            model.Add(b == 1)
            assign[did][dtype][k] = b
            opt_itv = model.NewOptionalIntervalVar(s, 0, dev_done, b, f"ditv_{did}_{dtype}_{k}")
            dev_intervals_per_device[(dtype, k)].append((did, opt_itv, b))

    for _key, items in dev_intervals_per_device.items():
        itvs = [itv for (_, itv, _) in items]
        if len(itvs) > 1:
            model.AddNoOverlap(itvs)

    dev_by_key: Dict[Tuple[str, int], Device] = {(t, k): d for t, k, d in dev_keys}

    for (dtype, kk), items in dev_intervals_per_device.items():
        dev = dev_by_key[(dtype, kk)]
        op_list = [oid for (oid, _, _) in items]
        for i, p in enumerate(op_list):
            for q in op_list[i + 1 :]:
                loc_p = op_info[p]["workshop"]
                loc_q = op_info[q]["workshop"]
                if legacy_othersolve:
                    travel_pq = _legacy_pair_travel_sec(dist_map, loc_p, loc_q)
                    travel_qp = _legacy_pair_travel_sec(dist_map, loc_q, loc_p)
                elif _workshop_node(loc_p) == _workshop_node(loc_q):
                    travel_pq = 0
                    travel_qp = 0
                else:
                    travel_pq = calc_transport_time(distance_between(dist_map, loc_p, loc_q), dev.speed)
                    travel_qp = calc_transport_time(distance_between(dist_map, loc_q, loc_p), dev.speed)

                if travel_pq == 0 and travel_qp == 0:
                    continue

                bp = assign[p][dtype][kk]
                bq = assign[q][dtype][kk]
                dev_done_p = dev_end_var[p][dtype]
                dev_done_q = dev_end_var[q][dtype]
                p_before_q = model.NewBoolVar(f"ord_{p}_{q}_{dtype}_{kk}")

                # 只有两道任务确实分配到同一台具体设备时，才触发二选一的转运顺序约束。
                # NoOverlap 已负责同机占用不重叠；这里补充序列相关转运时间。
                if travel_pq > 0:
                    model.Add(op_start[q] >= dev_done_p + travel_pq).OnlyEnforceIf([bp, bq, p_before_q])
                if travel_qp > 0:
                    model.Add(op_start[p] >= dev_done_q + travel_qp).OnlyEnforceIf([bp, bq, p_before_q.Not()])

    makespan = model.NewIntVar(0, horizon, "makespan")
    model.AddMaxEquality(makespan, [op_end[oid] for oid in real_op_ids])

    return {
        "model": model,
        "makespan": makespan,
        "op_start": op_start,
        "op_end": op_end,
        "assign": assign,
        "op_cp": op_cp,
        "op_info": op_info,
        "op_by_id": op_by_id,
        "real_op_ids": real_op_ids,
        "dummy_ids": dummy_ids,
        "count_by_type": dict(count_by_type),
        "dev_keys": dev_keys,
        "dev_by_key": dev_by_key,
        "team1": team1,
        "dist_map": dist_map,
        "horizon": horizon,
        "fixed_alloc": fixed_alloc,
        "expanded": expanded,
        "legacy_othersolve": legacy_othersolve,
    }


def _run_two_phase(
    ctx: Dict[str, Any],
    time_limit_sec: float,
    num_workers: int,
) -> Tuple[List[ScheduleRecord], int, str]:
    """阶段1最小化 makespan，阶段2固定 makespan 最小化开工和（带 hint）。"""
    horizon = ctx["horizon"]
    real_op_ids = ctx["real_op_ids"]

    t1 = max(15.0, float(time_limit_sec) * 0.55)
    t2 = max(10.0, float(time_limit_sec) - t1)

    m1 = ctx["model"]
    m1.Minimize(ctx["makespan"])
    solver1 = cp_model.CpSolver()
    solver1.parameters.max_time_in_seconds = t1
    solver1.parameters.num_workers = int(num_workers)
    solver1.parameters.log_search_progress = False
    st1 = solver1.Solve(m1)

    status_map = {
        cp_model.OPTIMAL: "OPTIMAL",
        cp_model.FEASIBLE: "FEASIBLE",
        cp_model.INFEASIBLE: "INFEASIBLE",
        cp_model.UNKNOWN: "UNKNOWN",
    }
    st1_msg = status_map.get(st1, str(st1))
    if st1 not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return [], 0, st1_msg

    ms_val = int(solver1.Value(ctx["makespan"]))

    fixed = ctx.get("fixed_alloc")
    legacy = bool(ctx.get("legacy_othersolve"))
    ctx2 = _q2_cp_model_ctx(
        ctx["expanded"],
        ctx["team1"],
        ctx["dist_map"],
        horizon,
        fixed_alloc=fixed,
        legacy_othersolve=legacy,
    )
    m2 = ctx2["model"]
    m2.Add(ctx2["makespan"] == ms_val)
    for oid in ctx2["real_op_ids"]:
        m2.AddHint(ctx2["op_start"][oid], int(solver1.Value(ctx["op_start"][oid])))
    sum_starts = m2.NewIntVar(0, len(real_op_ids) * horizon, "sum_real_op_starts")
    m2.Add(sum_starts == sum(ctx2["op_start"][oid] for oid in ctx2["real_op_ids"]))
    m2.Minimize(sum_starts)

    solver2 = cp_model.CpSolver()
    solver2.parameters.max_time_in_seconds = t2
    solver2.parameters.num_workers = int(num_workers)
    solver2.parameters.log_search_progress = False
    st2 = solver2.Solve(m2)
    st2_msg = status_map.get(st2, str(st2))

    if st2 in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        rec, ms = _q2_cp_extract(solver2, ctx2)
        assert ms == ms_val
        if legacy:
            rec = _repair_depot_timeline(ctx["expanded"], rec, ctx["team1"], ctx["dist_map"])
            ms_val = max(r.end_sec for r in rec) if rec else ms_val
        return rec, ms_val, f"{st1_msg}+compress_{st2_msg}"

    rec, ms = _q2_cp_extract(solver1, ctx)
    if legacy:
        rec = _repair_depot_timeline(ctx["expanded"], rec, ctx["team1"], ctx["dist_map"])
        ms = max(r.end_sec for r in rec) if rec else ms
    return rec, ms, f"{st1_msg}_compress_skip"


def _q2_cp_extract(solver: cp_model.CpSolver, ctx: Dict[str, Any]) -> Tuple[List[ScheduleRecord], int]:
    real_op_ids: List[str] = ctx["real_op_ids"]
    op_by_id = ctx["op_by_id"]
    op_cp = ctx["op_cp"]
    assign = ctx["assign"]
    count_by_type = ctx["count_by_type"]
    dev_keys = ctx["dev_keys"]
    team1 = ctx["team1"]
    dist_map = ctx["dist_map"]
    op_start = ctx["op_start"]

    ms_val = int(solver.Value(ctx["makespan"]))
    type_k_to_dev: Dict[Tuple[str, int], Device] = {(t, k): d for t, k, d in dev_keys}

    records: List[ScheduleRecord] = []
    seq = 1
    for oid in real_op_ids:
        o = op_by_id[oid]
        s = int(solver.Value(op_start[oid]))
        meta = op_cp[oid]
        if meta["kind"] == "one":
            dtype = meta["dtype"]
            n_tot = count_by_type[dtype]
            n_use = sum(int(solver.Value(assign[oid][dtype][kk])) for kk in range(n_tot))
            r = o.requirements[0]
            dev_t = calc_work_duration(o.quantity, float(r["efficiency"]) * n_use, r.get("unit"))
            for kk in range(n_tot):
                if int(solver.Value(assign[oid][dtype][kk])) != 1:
                    continue
                dev = type_k_to_dev[(dtype, kk)]
                dev_end = s + dev_t
                records.append(
                    ScheduleRecord(
                        seq=seq,
                        device_id=dev.device_id,
                        team=dev.team,
                        device_type=dtype,
                        workshop=o.workshop,
                        raw_id=o.raw_id,
                        op_id=oid,
                        repeat_index=o.repeat_index,
                        start_sec=s,
                        end_sec=dev_end,
                        duration_sec=dev_t,
                        transport_sec=0,
                    )
                )
                seq += 1
        else:
            d1, d2 = meta["d1"], meta["d2"]
            c1, c2 = count_by_type[d1], count_by_type[d2]
            n1u = sum(int(solver.Value(assign[oid][d1][kk])) for kk in range(c1))
            n2u = sum(int(solver.Value(assign[oid][d2][kk])) for kk in range(c2))
            r1, r2 = o.requirements[0], o.requirements[1]
            dur1 = calc_work_duration(o.quantity, float(r1["efficiency"]) * n1u, r1.get("unit"))
            dur2 = calc_work_duration(o.quantity, float(r2["efficiency"]) * n2u, r2.get("unit"))
            e1 = s + dur1
            e2 = s + dur2
            for kk in range(c1):
                if int(solver.Value(assign[oid][d1][kk])) != 1:
                    continue
                dev = type_k_to_dev[(d1, kk)]
                records.append(
                    ScheduleRecord(
                        seq=seq,
                        device_id=dev.device_id,
                        team=dev.team,
                        device_type=d1,
                        workshop=o.workshop,
                        raw_id=o.raw_id,
                        op_id=oid,
                        repeat_index=o.repeat_index,
                        start_sec=s,
                        end_sec=e1,
                        duration_sec=dur1,
                        transport_sec=0,
                    )
                )
                seq += 1
            for kk in range(c2):
                if int(solver.Value(assign[oid][d2][kk])) != 1:
                    continue
                dev = type_k_to_dev[(d2, kk)]
                records.append(
                    ScheduleRecord(
                        seq=seq,
                        device_id=dev.device_id,
                        team=dev.team,
                        device_type=d2,
                        workshop=o.workshop,
                        raw_id=o.raw_id,
                        op_id=oid,
                        repeat_index=o.repeat_index,
                        start_sec=s,
                        end_sec=e2,
                        duration_sec=dur2,
                        transport_sec=0,
                    )
                )
                seq += 1

    by_dev: Dict[str, List[ScheduleRecord]] = defaultdict(list)
    for r in records:
        by_dev[r.device_id].append(r)
    dev_map = {d.device_id: d for d in team1}
    for did, lst in by_dev.items():
        lst.sort(key=lambda x: (x.start_sec, x.seq))
        d = dev_map[did]
        prev_node = d.initial_location
        prev_end = 0
        for r in lst:
            node = _workshop_node(r.workshop)
            if prev_node == node:
                need = 0
            else:
                need = calc_transport_time(distance_between(dist_map, prev_node, node), d.speed)
            r.transport_sec = need
            prev_node, prev_end = node, r.end_sec

    return records, ms_val


def solve_q2_cpsat(
    expanded: List[ExpandedOperation],
    devices: List[Device],
    dist_map: Dict[Tuple[str, str], float],
    time_limit_sec: float = DEFAULT_CPSAT_TIME_LIMIT_SEC,
    num_workers: int = DEFAULT_CPSAT_NUM_WORKERS,
    horizon: int = 500_000,
    raw_repeat_map: Optional[Dict[str, int]] = None,
) -> Tuple[List[ScheduleRecord], int, str, List[Tuple[str, int, str]]]:
    """
    并行跑三条 CP-SAT 两阶段链，在**全部通过工程校验**的候选中取最短 makespan：

    - **legacy_repaired**：与 `othersolve_2.py` 析取图同构（无虚拟首工序、车间间 ceil(d/2) 转运），求解后对班组
      初始位置做时间轴前推，使与 `validate_transport` 一致。
    - **fixed_alloc**：带虚拟首工序的严格模型，离線固定并联台数。
    - **variable_n**：严格模型 + CP 内可变台数。

    每条链均为：先最小化 makespan，再固定 makespan 最小化真实工序开工和并带 hint。

    返回值第 4 项为各子链 (链标识, makespan, 状态串)，makespan 为 0 表示该链未得到有效调度。
    """
    team1 = [d for d in devices if d.team == 1]
    count_by_type: Dict[str, int] = defaultdict(int)
    for d in team1:
        count_by_type[d.device_type] += 1
    cbt = dict(count_by_type)

    fixed_map = {o.op_id: _isolated_best_alloc(o, cbt) for o in expanded}

    t_total = float(time_limit_sec)
    # 严格模型直接包含班组初始位置与按设备速度计算的转运，优先分配搜索预算；
    # legacy 链保留为与参考脚本同构的兜底对照。
    t_fix = max(30.0, t_total * 0.42)
    t_var = max(30.0, t_total * 0.38)
    t_leg = max(20.0, t_total - t_fix - t_var)

    ctx_leg = _q2_cp_model_ctx(
        expanded, team1, dist_map, horizon, fixed_alloc=fixed_map, legacy_othersolve=True
    )
    ctx_fix = _q2_cp_model_ctx(expanded, team1, dist_map, horizon, fixed_alloc=fixed_map)
    ctx_var = _q2_cp_model_ctx(expanded, team1, dist_map, horizon, fixed_alloc=None)

    rec_f, ms_f, st_f = _run_two_phase(ctx_fix, t_fix, num_workers)
    rec_v, ms_v, st_v = _run_two_phase(ctx_var, t_var, num_workers)
    rec_l, ms_l, st_l = _run_two_phase(ctx_leg, t_leg, num_workers)
    chain_stats: List[Tuple[str, int, str]] = [
        ("fixed_alloc", ms_f if rec_f else 0, st_f),
        ("variable_n", ms_v if rec_v else 0, st_v),
        ("legacy_repaired", ms_l if rec_l else 0, st_l),
    ]

    def _feasible(rec: List[ScheduleRecord], ms: int) -> bool:
        if not rec or ms <= 0:
            return False
        if raw_repeat_map is None:
            return True
        return not _q2_validation_errors(expanded, rec, devices, dist_map, raw_repeat_map)

    pool: List[Tuple[List[ScheduleRecord], int, str]] = []
    if rec_f and ms_f > 0:
        pool.append((rec_f, ms_f, f"fixed_alloc::{st_f}"))
    if rec_v and ms_v > 0:
        pool.append((rec_v, ms_v, f"variable_n::{st_v}"))
    if rec_l and ms_l > 0:
        pool.append((rec_l, ms_l, f"legacy_repaired::{st_l}"))

    if not pool:
        return [], 0, "INFEASIBLE", chain_stats

    candidates = [(r, m, t) for r, m, t in pool if _feasible(r, m)]
    if not candidates:
        candidates = list(pool)

    # makespan 最短；相同时优先严格模型，legacy 仅作对照兜底。
    def _prio(tag: str) -> int:
        if tag.startswith("fixed_alloc"):
            return 0
        if tag.startswith("variable_n"):
            return 1
        return 2

    best_rec, best_ms, best_tag = min(candidates, key=lambda x: (x[1], _prio(x[2]), len(x[0])))
    note = f"best_ms={best_ms} pick={best_tag}"
    if len(pool) > 1:
        note += f" pool_ms={[m for _, m, _ in pool]}"
    return best_rec, best_ms, note, chain_stats


def solve_q3_cpsat(
    expanded: List[ExpandedOperation],
    devices: List[Device],
    dist_map: Dict[Tuple[str, str], float],
    time_limit_sec: float = DEFAULT_CPSAT_TIME_LIMIT_SEC,
    num_workers: int = DEFAULT_CPSAT_NUM_WORKERS,
    horizon: int = 500_000,
    raw_repeat_map: Optional[Dict[str, int]] = None,
) -> Tuple[List[ScheduleRecord], int, str, List[Tuple[str, int, str]]]:
    """
    问题 3：班组 1 + 班组 2 设备并池，与 `solve_q2_cpsat` 相同的三链 CP-SAT（legacy / fixed_alloc / variable_n），
    仅设备池由「仅班组 1」换为「班组 1 ∪ 班组 2」，以支持跨班组混用同型机与跨车间并行，贴近 othersolve_3 思路。

    返回值第 4 项为各子链 (链标识, makespan, 状态串)，顺序与内部求解顺序一致：legacy → fixed → variable。
    """
    team_pool = [d for d in devices if d.team in (1, 2)]
    count_by_type: Dict[str, int] = defaultdict(int)
    for d in team_pool:
        count_by_type[d.device_type] += 1
    cbt = dict(count_by_type)

    fixed_map = {o.op_id: _isolated_best_alloc(o, cbt) for o in expanded}

    t_total = float(time_limit_sec)
    t_leg = max(20.0, t_total * 0.32)
    t_fix = max(25.0, t_total * 0.36)
    t_var = max(20.0, t_total - t_leg - t_fix)

    ctx_leg = _q2_cp_model_ctx(
        expanded, team_pool, dist_map, horizon, fixed_alloc=fixed_map, legacy_othersolve=True
    )
    ctx_fix = _q2_cp_model_ctx(expanded, team_pool, dist_map, horizon, fixed_alloc=fixed_map)
    ctx_var = _q2_cp_model_ctx(expanded, team_pool, dist_map, horizon, fixed_alloc=None)

    rec_l, ms_l, st_l = _run_two_phase(ctx_leg, t_leg, num_workers)
    rec_f, ms_f, st_f = _run_two_phase(ctx_fix, t_fix, num_workers)
    rec_v, ms_v, st_v = _run_two_phase(ctx_var, t_var, num_workers)
    chain_stats_q3: List[Tuple[str, int, str]] = [
        ("legacy_repaired", ms_l if rec_l else 0, st_l),
        ("fixed_alloc", ms_f if rec_f else 0, st_f),
        ("variable_n", ms_v if rec_v else 0, st_v),
    ]

    def _feasible(rec: List[ScheduleRecord], ms: int) -> bool:
        if not rec or ms <= 0:
            return False
        if raw_repeat_map is None:
            return True
        return not _q2_validation_errors(expanded, rec, team_pool, dist_map, raw_repeat_map)

    pool: List[Tuple[List[ScheduleRecord], int, str]] = []
    if rec_l and ms_l > 0:
        pool.append((rec_l, ms_l, f"legacy_repaired::{st_l}"))
    if rec_f and ms_f > 0:
        pool.append((rec_f, ms_f, f"fixed_alloc::{st_f}"))
    if rec_v and ms_v > 0:
        pool.append((rec_v, ms_v, f"variable_n::{st_v}"))

    if not pool:
        return [], 0, "INFEASIBLE", chain_stats_q3

    candidates = [(r, m, t) for r, m, t in pool if _feasible(r, m)]
    if not candidates:
        candidates = list(pool)

    def _prio(tag: str) -> int:
        if tag.startswith("legacy_repaired"):
            return 0
        if tag.startswith("fixed_alloc"):
            return 1
        return 2

    best_rec, best_ms, best_tag = min(candidates, key=lambda x: (x[1], _prio(x[2]), len(x[0])))
    note = f"best_ms={best_ms} pick={best_tag}"
    if len(pool) > 1:
        note += f" pool_ms={[m for _, m, _ in pool]}"
    return best_rec, best_ms, note, chain_stats_q3


def cpsat_available() -> bool:
    try:
        from ortools.sat.python import cp_model  # noqa: F401

        return True
    except ImportError:
        return False
