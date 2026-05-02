"""贪心事件推进调度（问题 1/2 等可复用）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

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
) -> Tuple[List[ScheduleRecord], int]:
    """
    问题 1：仅班组 1、仅指定车间工序（调用方已筛选）。
    返回 (设备任务记录, makespan 秒)。
    """
    # 仅保留传入工序涉及车间（问题1通常为 A）
    workshops = sorted({op.workshop for op in expanded_ops})
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
) -> Tuple[List[ScheduleRecord], int]:
    """
    问题 2：仅班组 1，覆盖 A-E 五个车间（调用方可先过滤班组）。
    当前采用与问题1一致的贪心事件推进框架。
    """
    return solve_problem1(expanded_ops, devices, dist_map)


def solve_problem3(
    expanded_ops: List[ExpandedOperation],
    devices: List[Device],
    dist_map: Dict[Tuple[str, str], float],
) -> Tuple[List[ScheduleRecord], int]:
    """
    问题 3：使用班组 1 和班组 2 设备，覆盖 A-E 五个车间。
    当前采用与问题1一致的贪心事件推进框架。
    """
    return solve_problem1(expanded_ops, devices, dist_map)


def solve_problem4(
    expanded_ops: List[ExpandedOperation],
    devices: List[Device],
    dist_map: Dict[Tuple[str, str], float],
) -> Tuple[List[ScheduleRecord], int]:
    """
    问题 4：在给定设备集合（含增购设备）下进行调度。
    复用问题1的贪心事件推进框架，由调用方负责搜索增购方案。
    """
    return solve_problem1(expanded_ops, devices, dist_map)
