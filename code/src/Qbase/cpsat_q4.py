"""
问题4：OR-Tools CP-SAT 内层调度（与 othersolve_4 同构，数据来自展开工序与设备列表）。
用于在固定设备池（含增购）下最小化 Makespan，可跨车间优化工序排序与设备分配。
"""
from __future__ import annotations

import collections
import itertools
from typing import DefaultDict, Dict, List, Optional, Tuple

from ortools.sat.python import cp_model

from .parsers import Device, ExpandedOperation, distance_between
from .scheduler_common import ScheduleRecord
from .time_utils import calc_transport_time, calc_work_duration


def _norm_ws_node(workshop: str) -> str:
    return str(workshop).strip().upper().replace("车间", "")


def _travel_sec(
    dist_map: Dict[Tuple[str, str], float],
    speed: float,
    loc_a: str,
    loc_b: str,
) -> int:
    na = _norm_ws_node(loc_a) if not str(loc_a).startswith("班组") else str(loc_a).strip()
    nb = _norm_ws_node(loc_b) if not str(loc_b).startswith("班组") else str(loc_b).strip()
    if na == nb:
        return 0
    d_m = distance_between(dist_map, na, nb)
    return calc_transport_time(d_m, speed)


def _build_op_graph(
    expanded: List[ExpandedOperation],
) -> Tuple[List[str], Dict[str, Dict[str, object]]]:
    """车间内按 sequence_key、op_id 链式前驱。"""
    by_ws: DefaultDict[str, List[ExpandedOperation]] = collections.defaultdict(list)
    for op in expanded:
        by_ws[_norm_ws_node(op.workshop)].append(op)
    op_ids: List[str] = []
    op_map: Dict[str, Dict[str, object]] = {}
    for ws in sorted(by_ws.keys()):
        ops = sorted(by_ws[ws], key=lambda o: (o.sequence_key, o.op_id))
        prev: Optional[str] = None
        for op in ops:
            preds: List[str] = []
            if prev is not None:
                preds.append(prev)
            prev = op.op_id
            reqs = [(str(r["device_type"]), float(r["efficiency"])) for r in op.requirements]
            unit = None
            if op.requirements:
                unit = op.requirements[0].get("unit")
            op_map[op.op_id] = {
                "workshop": ws,
                "reqs": reqs,
                "quantity": float(op.quantity),
                "preds": preds,
                "unit": unit,
            }
            op_ids.append(op.op_id)
    return op_ids, op_map


def _compute_op_alloc(
    op_id: str,
    op_map: Dict[str, Dict[str, object]],
    global_count: Dict[str, int],
) -> Tuple[Optional[Dict[str, Tuple[int, int]]], Optional[int]]:
    """单工序：同类全上或枚举双设备 (n1,n2)，返回 {dtype: (n_used, dev_t)} 与阶段时长。"""
    spec = op_map[op_id]
    reqs: List[Tuple[str, float]] = spec["reqs"]  # type: ignore[assignment]
    qty = float(spec["quantity"])
    unit = spec.get("unit")

    if len(reqs) == 1:
        dtype, v = reqs[0]
        n_avail = global_count.get(dtype, 0)
        if n_avail <= 0:
            return None, None
        dur = calc_work_duration(qty, v * n_avail, unit)  # type: ignore[arg-type]
        return {dtype: (n_avail, dur)}, dur

    best_t = 10**18
    best_alloc: Optional[Dict[str, Tuple[int, int]]] = None
    ranges = []
    for dtype, v in reqs:
        n_avail = global_count.get(dtype, 0)
        if n_avail <= 0:
            return None, None
        ranges.append(range(1, n_avail + 1))
    for combo in itertools.product(*ranges):
        ts: Dict[str, int] = {}
        for (dtype, v), n in zip(reqs, combo):
            ts[dtype] = calc_work_duration(qty, v * n, unit)  # type: ignore[arg-type]
        dur = max(ts.values())
        if dur < best_t:
            best_t = dur
            best_alloc = {dtype: (n, ts[dtype]) for (dtype, _), n in zip(reqs, combo)}
    return best_alloc, int(best_t)


def solve_q4_scheduling_cpsat(
    expanded: List[ExpandedOperation],
    devices: List[Device],
    dist_map: Dict[Tuple[str, str], float],
    *,
    time_limit_sec: float = 20.0,
    num_workers: int = 4,
    horizon: int = 300_000,
) -> Tuple[Optional[List[ScheduleRecord]], Optional[int]]:
    """
    对给定设备列表（已含增购）建立 CP-SAT 析取模型并求最小 Makespan。
    成功返回 (ScheduleRecord 列表, makespan)；不可行或失败返回 (None, None)。
    """
    op_ids, op_map = _build_op_graph(expanded)
    if not op_ids:
        return [], 0

    team_count: Dict[Tuple[int, str], int] = collections.defaultdict(int)
    for d in devices:
        team_count[(d.team, d.device_type)] += 1
    global_count: Dict[str, int] = collections.defaultdict(int)
    for (_team, dtype), cnt in team_count.items():
        global_count[dtype] += int(cnt)
    dev_list: List[Tuple[int, str, int, str, str, float]] = []
    for d in sorted(devices, key=lambda x: (x.team, x.device_type, x.device_id)):
        home = d.initial_location or f"班组{d.team}"
        dev_list.append((d.team, d.device_type, len(dev_list), d.device_id, home, float(d.speed)))

    op_alloc: Dict[str, Dict[str, Tuple[int, int]]] = {}
    op_dur: Dict[str, int] = {}
    for oid in op_ids:
        alloc, dur = _compute_op_alloc(oid, op_map, dict(global_count))
        if alloc is None or dur is None:
            return None, None
        op_alloc[oid] = alloc
        op_dur[oid] = int(dur)

    model = cp_model.CpModel()
    H = int(horizon)

    op_start: Dict[str, cp_model.IntVar] = {}
    op_end: Dict[str, cp_model.IntVar] = {}
    for oid in op_ids:
        s = model.NewIntVar(0, H, f"s_{oid}")
        e = model.NewIntVar(0, H, f"e_{oid}")
        op_start[oid] = s
        op_end[oid] = e
        model.Add(e == s + op_dur[oid])

    for oid in op_ids:
        for pred in op_map[oid]["preds"]:  # type: ignore[index]
            model.Add(op_start[oid] >= op_end[str(pred)])

    assign: Dict[Tuple[str, int], cp_model.IntVar] = {}
    op_cand: Dict[str, Dict[str, List[int]]] = collections.defaultdict(lambda: collections.defaultdict(list))
    for dev_idx, (team, dtype, _k, _did, _home, _sp) in enumerate(dev_list):
        for oid in op_ids:
            if dtype in op_alloc[oid]:
                b = model.NewBoolVar(f"a_{oid}_{dev_idx}")
                assign[(oid, dev_idx)] = b
                op_cand[oid][dtype].append(dev_idx)

    for oid in op_ids:
        for dtype, (n_used, _dev_t) in op_alloc[oid].items():
            cands = op_cand[oid][dtype]
            if cands:
                # 设备不跨班转用：每台设备保留自己的班组和驻点；
                # 同一道工序允许两个班组的同型设备同时参与并联。
                model.Add(sum(assign[(oid, i)] for i in cands) == n_used)

    # 每台设备若参与某工序，开工时刻至少为「从驻点到该工序车间」的转运下界（补齐 pairwise 未覆盖的首次出车）。
    for oid in op_ids:
        ws = str(op_map[oid]["workshop"])
        s_var = op_start[oid]
        for dtype in op_cand[oid].keys():
            for dev_idx in op_cand[oid][dtype]:
                b = assign[(oid, dev_idx)]
                _tm, _dt, _k, _did, home, speed = dev_list[dev_idx]
                t0 = _travel_sec(dist_map, speed, home, ws)
                if t0 > 0:
                    model.Add(s_var >= t0).OnlyEnforceIf(b)

    dev_done: Dict[Tuple[str, int], cp_model.IntVar] = {}
    dev_opt_intervals: Dict[int, List[cp_model.IntervalVar]] = collections.defaultdict(list)
    dev_work_info: Dict[int, List[Tuple[str, str, cp_model.IntVar, cp_model.IntVar]]] = collections.defaultdict(list)

    for oid in op_ids:
        s_var = op_start[oid]
        for dtype in op_cand[oid].keys():
            for dev_idx in op_cand[oid][dtype]:
                b = assign[(oid, dev_idx)]
                dev_t = op_alloc[oid][dtype][1]
                done_var = model.NewIntVar(0, H, f"done_{oid}_{dev_idx}")
                model.Add(done_var == s_var + dev_t).OnlyEnforceIf(b)
                model.Add(done_var == 0).OnlyEnforceIf(b.Not())
                dev_done[(oid, dev_idx)] = done_var
                opt_itv = model.NewOptionalIntervalVar(s_var, dev_t, done_var, b, f"oitv_{oid}_{dev_idx}")
                dev_opt_intervals[dev_idx].append(opt_itv)
                dev_work_info[dev_idx].append((oid, dtype, done_var, b))

    for dev_idx, itvs in dev_opt_intervals.items():
        if len(itvs) > 1:
            model.AddNoOverlap(itvs)

    for dev_idx, info_list in dev_work_info.items():
        speed = dev_list[dev_idx][5]
        n = len(info_list)
        if n <= 1:
            continue
        for ia in range(n):
            for ib in range(ia + 1, n):
                oid_p, dtype_p, done_p, bp = info_list[ia]
                oid_q, dtype_q, done_q, bq = info_list[ib]
                ws_p = str(op_map[oid_p]["workshop"])
                ws_q = str(op_map[oid_q]["workshop"])
                travel_pq = _travel_sec(dist_map, speed, ws_p, ws_q)
                travel_qp = _travel_sec(dist_map, speed, ws_q, ws_p)
                if travel_pq == 0 and travel_qp == 0:
                    continue
                p_before_q = model.NewBoolVar(f"ord_{oid_p}_{oid_q}_{dev_idx}")
                if travel_pq > 0:
                    model.Add(op_start[oid_q] >= done_p + travel_pq).OnlyEnforceIf([bp, bq, p_before_q])
                if travel_qp > 0:
                    model.Add(op_start[oid_p] >= done_q + travel_qp).OnlyEnforceIf([bp, bq, p_before_q.Not()])

    makespan_var = model.NewIntVar(0, H, "makespan")
    model.AddMaxEquality(makespan_var, [op_end[oid] for oid in op_ids])
    model.Minimize(makespan_var)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit_sec)
    solver.parameters.num_workers = int(num_workers)
    solver.parameters.log_search_progress = False

    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None, None

    ms_val = int(solver.Value(makespan_var))
    op_by_id = {op.op_id: op for op in expanded}

    raw_rows: List[Dict[str, object]] = []
    for oid in op_ids:
        ws = str(op_map[oid]["workshop"])
        s0 = int(solver.Value(op_start[oid]))
        e0 = int(solver.Value(op_end[oid]))
        for dtype, (_n_used, dev_t) in op_alloc[oid].items():
            for dev_idx in op_cand[oid][dtype]:
                if int(solver.Value(assign[(oid, dev_idx)])) == 1:
                    team, _dt, _k, dev_id, _home, _sp = dev_list[dev_idx]
                    raw_rows.append(
                        {
                            "device_id": dev_id,
                            "team": team,
                            "device_type": dtype,
                            "workshop": ws,
                            "op_id": oid,
                            "start": s0,
                            "end": s0 + int(dev_t),
                            "dur": int(dev_t),
                            "op_end": e0,
                        }
                    )

    raw_rows.sort(key=lambda r: (int(r["start"]), str(r["op_id"]), int(r["team"]), str(r["device_id"])))

    dev_by_id = {d.device_id: d for d in devices}
    records: List[ScheduleRecord] = []
    for r in raw_rows:
        op = op_by_id.get(str(r["op_id"]))
        raw_id = op.raw_id if op else str(r["op_id"])
        rep_idx = op.repeat_index if op else 1
        ws_cell = op.workshop if op else str(r["workshop"])
        records.append(
            ScheduleRecord(
                seq=0,
                device_id=str(r["device_id"]),
                team=int(r["team"]),
                device_type=str(r["device_type"]),
                workshop=ws_cell,
                raw_id=raw_id,
                op_id=str(r["op_id"]),
                repeat_index=int(rep_idx),
                start_sec=int(r["start"]),
                end_sec=int(r["end"]),
                duration_sec=int(r["dur"]),
                transport_sec=0,
            )
        )

    by_dev: DefaultDict[str, List[ScheduleRecord]] = collections.defaultdict(list)
    for rec in records:
        by_dev[rec.device_id].append(rec)
    for did, lst in by_dev.items():
        lst.sort(key=lambda x: (x.start_sec, x.op_id))
        d = dev_by_id.get(did)
        prev_node = d.initial_location if d else f"班组{lst[0].team}"
        for rec in lst:
            node = rec.workshop.strip().upper()
            if prev_node == node:
                need = 0
            else:
                need = calc_transport_time(distance_between(dist_map, prev_node, node), d.speed) if d else 0
            rec.transport_sec = int(need)
            prev_node = node

    records.sort(key=lambda x: (x.start_sec, x.op_id, x.team, x.device_id))
    for i, rec in enumerate(records, 1):
        rec.seq = i

    return records, ms_val
