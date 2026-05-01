"""调度结果可行性校验。"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Tuple

from .parsers import Device, ExpandedOperation, distance_between
from .scheduler_common import ScheduleRecord
from .time_utils import calc_transport_time, seconds_to_hhmmss


def validate_repeat_counts(raw_repeat_map: Dict[str, int], expanded: List[ExpandedOperation]) -> List[str]:
    errs: List[str] = []
    counts: Dict[str, int] = defaultdict(int)
    for e in expanded:
        counts[e.raw_id] += 1
    for rid, c in counts.items():
        exp = raw_repeat_map.get(rid, 1)
        if c != exp:
            errs.append(f"重复展开不一致: {rid} 期望 {exp} 次，实际 {c} 次")
    return errs


def validate_precedence(expanded: List[ExpandedOperation], records: List[ScheduleRecord]) -> List[str]:
    """同一车间内工序完成时间须递增。"""
    errs: List[str] = []
    op_end: Dict[str, int] = {}
    for r in records:
        cur = op_end.get(r.op_id, 0)
        op_end[r.op_id] = max(cur, r.end_sec)
    by_ws: Dict[str, List[ExpandedOperation]] = defaultdict(list)
    for o in expanded:
        by_ws[o.workshop].append(o)
    for ws, ops in by_ws.items():
        ops = sorted(ops, key=lambda x: (x.sequence_key, x.op_id))
        last_fin = -1
        for o in ops:
            fin = op_end.get(o.op_id)
            if fin is None:
                errs.append(f"工序 {o.op_id} 无调度记录")
                continue
            if fin < last_fin:
                errs.append(f"车间 {ws} 工序顺序违反: {o.op_id} 完成于 {fin} 早于前序完成 {last_fin}")
            last_fin = fin
    return errs


def validate_device_type_match(expanded: List[ExpandedOperation], records: List[ScheduleRecord]) -> List[str]:
    errs: List[str] = []
    req_by_op: Dict[str, List[str]] = {}
    for o in expanded:
        req_by_op[o.op_id] = [x["device_type"] for x in o.requirements]
    for r in records:
        types = req_by_op.get(r.op_id, [])
        if r.device_type not in types:
            errs.append(f"设备类型不匹配: {r.op_id} 需要 {types}，记录为 {r.device_type}")
    return errs


def validate_device_conflicts(records: List[ScheduleRecord]) -> List[str]:
    errs: List[str] = []
    by_dev: Dict[str, List[ScheduleRecord]] = defaultdict(list)
    for r in records:
        by_dev[r.device_id].append(r)
    for did, lst in by_dev.items():
        lst.sort(key=lambda x: x.start_sec)
        for i in range(1, len(lst)):
            if lst[i].start_sec < lst[i - 1].end_sec:
                errs.append(
                    f"设备互斥违反: {did} 任务 {lst[i].op_id} 开始 {lst[i].start_sec} < 前任务结束 {lst[i - 1].end_sec}"
                )
    return errs


def validate_transport(
    records: List[ScheduleRecord],
    devices: List[Device],
    dist_map: Dict[Tuple[str, str], float],
) -> List[str]:
    """检查同一设备相邻任务：开始时间 >= 前序结束 + 转运时间。"""
    errs: List[str] = []
    dev_by_id = {d.device_id: d for d in devices}
    by_dev: Dict[str, List[ScheduleRecord]] = defaultdict(list)
    for r in records:
        by_dev[r.device_id].append(r)
    for did, lst in by_dev.items():
        lst.sort(key=lambda x: (x.start_sec, x.seq))
        d = dev_by_id.get(did)
        if not d:
            continue
        prev_node = d.initial_location
        prev_end = 0
        for r in lst:
            node = r.workshop.strip().upper()
            if prev_node == node:
                need = 0
            else:
                need = calc_transport_time(distance_between(dist_map, prev_node, node), d.speed)
            earliest = prev_end + need
            if r.start_sec < earliest:
                errs.append(
                    f"运输时间违反: 设备 {did} 工序 {r.op_id} 开始 {r.start_sec} < 最早可开始 {earliest} "
                    f"(前序节点 {prev_node} 结束 {prev_end}, 需转运 {need}s)"
                )
            prev_node, prev_end = node, r.end_sec
    return errs


def validate_duration_fields(records: List[ScheduleRecord]) -> List[str]:
    errs: List[str] = []
    for r in records:
        if r.end_sec < r.start_sec:
            errs.append(f"时间倒序: {r.device_id} {r.op_id}")
        if r.duration_sec != r.end_sec - r.start_sec:
            errs.append(
                f"持续工作时间不一致: {r.op_id} {r.device_id} 记录 {r.duration_sec} 实际差 {r.end_sec - r.start_sec}"
            )
    return errs


def validate_schedule(
    expanded: List[ExpandedOperation],
    records: List[ScheduleRecord],
    devices: List[Device],
    dist_map: Dict[Tuple[str, str], float],
    raw_repeat_map: Dict[str, int],
    makespan: int,
) -> str:
    """汇总校验报告文本。"""
    sections = []
    checks = [
        ("重复工序展开", validate_repeat_counts(raw_repeat_map, expanded)),
        ("工序先后顺序", validate_precedence(expanded, records)),
        ("设备类型匹配", validate_device_type_match(expanded, records)),
        ("设备互斥", validate_device_conflicts(records)),
        ("持续工作时间字段", validate_duration_fields(records)),
        ("运输时间抽查", validate_transport(records, devices, dist_map)),
    ]
    for name, es in checks:
        sections.append(f"## {name}\n" + ("通过\n" if not es else "\n".join(es) + "\n"))
    sections.append(f"## Makespan\n{makespan} s = {seconds_to_hhmmss(makespan)}\n")
    return "\n".join(sections)


def validate_budget(*args, **kwargs) -> List[str]:
    return []
