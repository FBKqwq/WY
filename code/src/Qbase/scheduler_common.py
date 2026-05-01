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
                best: Optional[Tuple[int, int, int, str, DeviceState]] = None  # end, start, transport, did, st
                dtype = req["device_type"]
                dur = calc_work_duration(op.quantity, req["efficiency"], req.get("unit"))
                for st in dev_states.values():
                    if st.device.device_type != dtype:
                        continue
                    d_m = distance_between(dist_map, st.location, node)
                    trans = calc_transport_time(d_m, st.device.speed)
                    start = max(ready, st.available + trans)
                    end = start + dur
                    cand = (end, start, trans, st.device.device_id, st)
                    if best is None or cand < best:
                        best = cand
                if best is None:
                    raise RuntimeError(f"工序 {op.op_id} 无可用设备类型 {dtype}")
                end, start, trans, did, st = best
                st.available = end
                st.location = node
                workshop_ready[op.workshop] = end
                records.append(
                    ScheduleRecord(
                        seq=seq,
                        device_id=did,
                        team=st.device.team,
                        device_type=dtype,
                        workshop=op.workshop,
                        raw_id=op.raw_id,
                        op_id=op.op_id,
                        repeat_index=op.repeat_index,
                        start_sec=start,
                        end_sec=end,
                        duration_sec=dur,
                        transport_sec=trans,
                    )
                )
                seq += 1

            elif len(op.requirements) == 2:
                r1, r2 = op.requirements[0], op.requirements[1]
                d1t, d2t = r1["device_type"], r2["device_type"]
                dur1 = calc_work_duration(op.quantity, r1["efficiency"], r1.get("unit"))
                dur2 = calc_work_duration(op.quantity, r2["efficiency"], r2.get("unit"))
                best_pair = None
                sts1 = [s for s in dev_states.values() if s.device.device_type == d1t]
                sts2 = [s for s in dev_states.values() if s.device.device_type == d2t]
                for s_a in sts1:
                    for s_b in sts2:
                        if s_a.device.device_id == s_b.device.device_id:
                            continue
                        tr1 = calc_transport_time(distance_between(dist_map, s_a.location, node), s_a.device.speed)
                        tr2 = calc_transport_time(distance_between(dist_map, s_b.location, node), s_b.device.speed)
                        s1 = max(ready, s_a.available + tr1)
                        e1 = s1 + dur1
                        s2 = max(ready, s_b.available + tr2)
                        e2 = s2 + dur2
                        op_end = max(e1, e2)
                        cand = (op_end, s1, e1, s2, e2, tr1, tr2, s_a.device.device_id, s_b.device.device_id, s_a, s_b)
                        if best_pair is None or cand[0] < best_pair[0] or (
                            cand[0] == best_pair[0] and (cand[1], cand[3]) < (best_pair[1], best_pair[3])
                        ):
                            best_pair = cand
                if best_pair is None:
                    raise RuntimeError(f"工序 {op.op_id} 无法匹配双设备组合 {d1t}+{d2t}")
                op_end, s1, e1, s2, e2, tr1, tr2, id1, id2, st1, st2 = best_pair
                st1.available, st1.location = e1, node
                st2.available, st2.location = e2, node
                workshop_ready[op.workshop] = op_end
                records.append(
                    ScheduleRecord(
                        seq=seq,
                        device_id=id1,
                        team=st1.device.team,
                        device_type=d1t,
                        workshop=op.workshop,
                        raw_id=op.raw_id,
                        op_id=op.op_id,
                        repeat_index=op.repeat_index,
                        start_sec=s1,
                        end_sec=e1,
                        duration_sec=dur1,
                        transport_sec=tr1,
                    )
                )
                seq += 1
                records.append(
                    ScheduleRecord(
                        seq=seq,
                        device_id=id2,
                        team=st2.device.team,
                        device_type=d2t,
                        workshop=op.workshop,
                        raw_id=op.raw_id,
                        op_id=op.op_id,
                        repeat_index=op.repeat_index,
                        start_sec=s2,
                        end_sec=e2,
                        duration_sec=dur2,
                        transport_sec=tr2,
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
