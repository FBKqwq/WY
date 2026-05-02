"""调度求解（问题 1 为枚举+门限最优；问题 2/3 等复用同一入口时为可行解框架）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import permutations
from typing import Dict, List, Optional, Sequence, Tuple

from .parsers import Device, ExpandedOperation, distance_between
from .time_utils import calc_transport_time, calc_work_duration


@dataclass
class DeviceState:
    device: Device
    available: int = 0
    location: str = field(default_factory=str)

    def __post_init__(self):
        if not self.location:
            self.location = self.device.initial_location


@dataclass
class ScheduleRecord:
    seq: int
    device_id: str
    team: int
    device_type: str
    workshop: str
    raw_id: str
    op_id: str
    repeat_index: int
    start_sec: int
    end_sec: int
    duration_sec: int
    transport_sec: int


def _workshop_node(workshop: str) -> str:
    return workshop.strip().upper()


def _gates_to_workshop(
    states: List[DeviceState],
    node: str,
    dist_map: Dict[Tuple[str, str], float],
) -> List[Tuple[int, DeviceState, int]]:
    """每台设备到达 node 的最早时刻与转运秒数。"""
    out: List[Tuple[int, DeviceState, int]] = []
    for st in states:
        d_m = distance_between(dist_map, st.location, node)
        trans = calc_transport_time(d_m, st.device.speed)
        out.append((st.available + trans, st, trans))
    out.sort(key=lambda x: (x[0], x[1].device.device_id))
    return out


def solve_problem1(
    expanded_ops: List[ExpandedOperation],
    devices: List[Device],
    dist_map: Dict[Tuple[str, str], float],
    workshop_order: Optional[Sequence[str]] = None,
) -> Tuple[List[ScheduleRecord], int]:
    """
    问题 1：仅班组 1、仅指定车间工序（调用方已筛选）。
    返回 (设备任务记录, makespan 秒)。

    对单车间、工序顺序固定的典型 Q1（如仅 A 且设备类型不在后续工序复用），
    通过枚举并联台数并在固定台数下取门限最小的设备组，可得该链路上 Makespan 的全局最优解。

    workshop_order：多车间时外层按此顺序依次处理「各车间整条工序链」（再进入车间内顺序）；
    为 None 时等价于车间名字典序，与历史行为一致。
    """
    ws_in_ops = {op.workshop for op in expanded_ops}
    if workshop_order is None:
        workshops = sorted(ws_in_ops)
    else:
        order_list = list(workshop_order)
        if set(order_list) != ws_in_ops:
            raise ValueError(f"workshop_order 与工序涉及车间集合不一致: 给定={set(order_list)} 需要={ws_in_ops}")
        if len(order_list) != len(ws_in_ops):
            raise ValueError("workshop_order 长度须等于涉及车间数且不得重复")
        workshops = order_list

    workshop_ready = {w: 0 for w in workshops}

    dev_states = {d.device_id: DeviceState(device=d, location=d.initial_location) for d in devices}
    records: List[ScheduleRecord] = []
    seq = 1

    # 按车间内顺序依次调度
    for w in workshops:
        ops = [o for o in expanded_ops if o.workshop == w]
        ops.sort(key=lambda o: (o.sequence_key, o.op_id))
        for op in ops:
            node = _workshop_node(op.workshop)
            ready = workshop_ready[op.workshop]

            if len(op.requirements) == 1:
                req = op.requirements[0]
                dtype = req["device_type"]
                sts = [s for s in dev_states.values() if s.device.device_type == dtype]
                if not sts:
                    raise RuntimeError(f"工序 {op.op_id} 无可用设备类型 {dtype}")
                gated = _gates_to_workshop(sts, node, dist_map)
                best_one: Optional[Tuple[int, int, int, List[Tuple[DeviceState, int, int]]]] = None
                # (op_end, op_start, n, list of (st, trans, dur) 每台持续相同)
                for n in range(1, len(gated) + 1):
                    sel = gated[:n]
                    op_start = max(ready, sel[-1][0])
                    dur = calc_work_duration(
                        op.quantity, float(req["efficiency"]) * n, req.get("unit")
                    )
                    op_end = op_start + dur
                    cand = (op_end, op_start, n, [(s, tr, dur) for _, s, tr in sel])
                    if best_one is None or cand[0] < best_one[0] or (
                        cand[0] == best_one[0] and (cand[1], -cand[2]) < (best_one[1], -best_one[2])
                    ):
                        best_one = cand
                assert best_one is not None
                op_end, op_start, _, triples = best_one
                workshop_ready[op.workshop] = op_end
                for st, trans, dur in triples:
                    st.available, st.location = op_end, node
                    records.append(
                        ScheduleRecord(
                            seq=seq,
                            device_id=st.device.device_id,
                            team=st.device.team,
                            device_type=dtype,
                            workshop=op.workshop,
                            raw_id=op.raw_id,
                            op_id=op.op_id,
                            repeat_index=op.repeat_index,
                            start_sec=op_start,
                            end_sec=op_end,
                            duration_sec=dur,
                            transport_sec=trans,
                        )
                    )
                    seq += 1

            elif len(op.requirements) == 2:
                r1, r2 = op.requirements[0], op.requirements[1]
                d1t, d2t = r1["device_type"], r2["device_type"]
                sts1 = [s for s in dev_states.values() if s.device.device_type == d1t]
                sts2 = [s for s in dev_states.values() if s.device.device_type == d2t]
                if not sts1 or not sts2:
                    raise RuntimeError(f"工序 {op.op_id} 无法匹配双设备组合 {d1t}+{d2t}")
                g1 = _gates_to_workshop(sts1, node, dist_map)
                g2 = _gates_to_workshop(sts2, node, dist_map)
                best_pair: Optional[
                    Tuple[
                        int,
                        int,
                        int,
                        int,
                        List[Tuple[DeviceState, int]],
                        List[Tuple[DeviceState, int]],
                        int,
                        int,
                    ]
                ] = None
                # (op_end, op_start, n1, n2, list1 (st,trans), list2, dur1, dur2)
                for n1 in range(1, len(g1) + 1):
                    for n2 in range(1, len(g2) + 1):
                        dur1 = calc_work_duration(
                            op.quantity, float(r1["efficiency"]) * n1, r1.get("unit")
                        )
                        dur2 = calc_work_duration(
                            op.quantity, float(r2["efficiency"]) * n2, r2.get("unit")
                        )
                        sel1 = [(s, tr) for _, s, tr in g1[:n1]]
                        sel2 = [(s, tr) for _, s, tr in g2[:n2]]
                        t1 = max(ready, g1[n1 - 1][0])
                        t2 = max(ready, g2[n2 - 1][0])
                        op_start = max(t1, t2)
                        op_end = op_start + max(dur1, dur2)
                        cand = (op_end, op_start, n1, n2, sel1, sel2, dur1, dur2)
                        if best_pair is None or cand[0] < best_pair[0] or (
                            cand[0] == best_pair[0]
                            and (cand[1], cand[2] + cand[3]) < (best_pair[1], best_pair[2] + best_pair[3])
                        ):
                            best_pair = cand
                assert best_pair is not None
                op_end, op_start, n1, n2, sel1, sel2, dur1, dur2 = best_pair
                e1 = op_start + dur1
                e2 = op_start + dur2
                workshop_ready[op.workshop] = op_end
                for st, trans in sel1:
                    st.available, st.location = e1, node
                    records.append(
                        ScheduleRecord(
                            seq=seq,
                            device_id=st.device.device_id,
                            team=st.device.team,
                            device_type=d1t,
                            workshop=op.workshop,
                            raw_id=op.raw_id,
                            op_id=op.op_id,
                            repeat_index=op.repeat_index,
                            start_sec=op_start,
                            end_sec=e1,
                            duration_sec=dur1,
                            transport_sec=trans,
                        )
                    )
                    seq += 1
                for st, trans in sel2:
                    st.available, st.location = e2, node
                    records.append(
                        ScheduleRecord(
                            seq=seq,
                            device_id=st.device.device_id,
                            team=st.device.team,
                            device_type=d2t,
                            workshop=op.workshop,
                            raw_id=op.raw_id,
                            op_id=op.op_id,
                            repeat_index=op.repeat_index,
                            start_sec=op_start,
                            end_sec=e2,
                            duration_sec=dur2,
                            transport_sec=trans,
                        )
                    )
                    seq += 1

            else:
                raise ValueError(f"工序 {op.op_id} 设备种类数不为 1 或 2")

    makespan = max((r.end_sec for r in records), default=0)
    return records, makespan


def solve_problem2(
    expanded_ops: List[ExpandedOperation],
    devices: List[Device],
    dist_map: Dict[Tuple[str, str], float],
    enumerate_workshop_orders: bool = True,
) -> Tuple[List[ScheduleRecord], int]:
    """
    问题 2：仅班组 1，覆盖 A-E 五个车间（调用方可先过滤班组）。
    与问题 1 共用 `solve_problem1`：含工序内同类多台并联与双设备 (n_1, n_2) 台数枚举。

    enumerate_workshop_orders 为 True（默认）时，枚举「涉及车间」的全排列作为外层处理顺序，
    取 makespan 最短的贪心解，避免固定字典序车间块顺序造成的明显遗漏；仍为启发式（块内仍整车间串行）。
    为 False 时与历史一致，外层顺序为车间名字典序。
    """
    ws_tuple = tuple(sorted({op.workshop for op in expanded_ops}))
    if not enumerate_workshop_orders or len(ws_tuple) <= 1:
        return solve_problem1(expanded_ops, devices, dist_map, workshop_order=None)

    best_records: Optional[List[ScheduleRecord]] = None
    best_ms = 10**18
    for perm in permutations(ws_tuple):
        rec, ms = solve_problem1(expanded_ops, devices, dist_map, workshop_order=list(perm))
        if ms < best_ms:
            best_ms = ms
            best_records = rec
    assert best_records is not None
    return best_records, best_ms


def solve_problem3(
    expanded_ops: List[ExpandedOperation],
    devices: List[Device],
    dist_map: Dict[Tuple[str, str], float],
    enumerate_workshop_orders: bool = True,
) -> Tuple[List[ScheduleRecord], int]:
    """
    问题 3：使用班组 1 和班组 2 设备，覆盖 A-E 五个车间。
    与问题 1/2 共用 `solve_problem1`：调用方传入两班组并池后的 `devices` 列表后，
    对每道工序枚举同类并联台数 n 及双设备 (n_1, n_2)，门限选机可跨班组挑选同型机；
    表 3 通过 `include_team=True` 区分设备所属班组。

    enumerate_workshop_orders 为 True（默认）时，与问题 2 相同，枚举涉及车间的全排列作为
    外层车间块处理顺序，取 makespan 最短的贪心解；仍为启发式（块内整车间链串行，弱于 CP-SAT 跨车间并行）。
    """
    ws_tuple = tuple(sorted({op.workshop for op in expanded_ops}))
    if not enumerate_workshop_orders or len(ws_tuple) <= 1:
        return solve_problem1(expanded_ops, devices, dist_map, workshop_order=None)

    best_records: Optional[List[ScheduleRecord]] = None
    best_ms = 10**18
    for perm in permutations(ws_tuple):
        rec, ms = solve_problem1(expanded_ops, devices, dist_map, workshop_order=list(perm))
        if ms < best_ms:
            best_ms = ms
            best_records = rec
    assert best_records is not None
    return best_records, best_ms


def solve_problem4(
    expanded_ops: List[ExpandedOperation],
    devices: List[Device],
    dist_map: Dict[Tuple[str, str], float],
) -> Tuple[List[ScheduleRecord], int]:
    """
    问题 4：在给定设备集合（含增购设备）下进行调度。
    与问题 3 一致：枚举涉及车间的处理顺序全排列，在内层 `solve_problem1` 上取最短贪心 Makespan；
    含工序内同类并联与双设备 (n1,n2) 枚举。由调用方搜索增购方案并扩展 `devices`。
    """
    return solve_problem3(expanded_ops, devices, dist_map, enumerate_workshop_orders=True)
