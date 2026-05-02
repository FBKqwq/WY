"""解析工序、设备与距离矩阵。"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .time_utils import detect_repeat_count


def _norm(s: str) -> str:
    return str(s).strip().replace(" ", "")


def _find_col(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    cols = { _norm(c): c for c in df.columns }
    for cand in candidates:
        key = _norm(cand)
        if key in cols:
            return cols[key]
        for k, orig in cols.items():
            if key in k or k in key:
                return orig
    return None


@dataclass
class RawOperation:
    raw_id: str
    workshop: str
    order: int
    quantity: float
    repeat_count: int
    requirements: List[Dict[str, Any]]  # device_type, efficiency, unit
    source_row: int = 0


@dataclass
class ExpandedOperation:
    op_id: str
    raw_id: str
    repeat_index: int
    repeat_count: int
    workshop: str
    sequence_key: float
    quantity: float
    requirements: List[Dict[str, Any]]


@dataclass
class Device:
    team: int
    device_id: str
    device_type: str
    speed: float
    price: float
    initial_location: str = "班组1"


def parse_operations(df: pd.DataFrame) -> List[RawOperation]:
    """将工序流程表解析为 RawOperation 列表。支持明细列与官方紧凑列两种格式。"""
    # 官方紧凑格式：任务/工序/设备作业效率/工程量/备注
    c_task = _find_col(df, ["任务"])
    c_proc = _find_col(df, ["工序"])
    c_eff = _find_col(df, ["设备作业效率", "设备作业效率信息"])
    c_note = _find_col(df, ["备注"])
    if c_task and c_proc and c_eff:
        rows: List[RawOperation] = []
        current_ws = ""
        for idx, row in df.iterrows():
            task = row.get(c_task)
            if task is not None and not pd.isna(task):
                t = str(task).strip()
                m = re.search(r"([ABCDE])\s*车间", t)
                if m:
                    current_ws = m.group(1)
            if not current_ws:
                raise ValueError("官方工序表中未解析到车间信息（任务列）")

            proc_text = str(row[c_proc]).strip()
            m_proc = re.match(r"^\s*([A-E]\d+)", proc_text)
            if not m_proc:
                continue
            rid = m_proc.group(1)
            order = int(re.sub(r"\D", "", rid))

            qty_text = str(row.get(_find_col(df, ["工程量", "数量"]) or "工程量", "")).strip()
            m_qty = re.search(r"([0-9]+(?:\.[0-9]+)?)", qty_text)
            if not m_qty:
                raise ValueError(f"工序 {rid} 无法解析工程量: {qty_text}")
            qty = float(m_qty.group(1))

            eff_text = str(row[c_eff]).strip()
            parts = [p.strip() for p in re.split(r"[和+＋]", eff_text) if p.strip()]
            reqs: List[Dict[str, Any]] = []
            for p in parts:
                m_req = re.search(r"([\u4e00-\u9fa5A-Za-z0-9]+?)\s*([0-9]+(?:\.[0-9]+)?)\s*m[³3]/h", p)
                if not m_req:
                    continue
                reqs.append(
                    {
                        "device_type": m_req.group(1).strip(),
                        "efficiency": float(m_req.group(2)),
                        "unit": "m³/h",
                    }
                )
            if not reqs:
                raise ValueError(f"工序 {rid} 未解析到设备效率信息: {eff_text}")

            note = ""
            if c_note and not pd.isna(row.get(c_note)):
                note = str(row.get(c_note)).strip()
            rep = detect_repeat_count(None, note)
            rows.append(
                RawOperation(
                    raw_id=rid,
                    workshop=current_ws,
                    order=order,
                    quantity=qty,
                    repeat_count=max(1, int(rep)),
                    requirements=reqs,
                    source_row=int(idx) + 2,
                )
            )

        # 处理「C3-C5」区间 +「重复N遍/次」类备注（避免误匹配「完成1遍」中的 1）
        if c_note:
            for _, row in df.iterrows():
                if pd.isna(row.get(c_note)):
                    continue
                note = str(row[c_note])
                m_pair = re.search(r"([A-E]\d+)\s*-\s*([A-E]\d+)", note)
                if not m_pair:
                    continue
                left, right = m_pair.group(1), m_pair.group(2)
                lo, hi = int(re.sub(r"\D", "", left)), int(re.sub(r"\D", "", right))
                ws = left[0]
                m_rep_bian = re.search(r"重复\s*(\d+)\s*[遍次]", note)
                if m_rep_bian:
                    rep = max(1, int(m_rep_bian.group(1)))
                else:
                    m_rng = re.search(
                        r"([A-E]\d+)\s*-\s*([A-E]\d+).*?(重复|再进行)\s*([0-9]+|[一二两三四五六七八九十]+)\s*(遍|次)",
                        note,
                    )
                    if not m_rng:
                        continue
                    rep = detect_repeat_count(m_rng.group(4), note)
                for r in rows:
                    if r.workshop == ws and lo <= r.order <= hi:
                        r.repeat_count = max(r.repeat_count, rep)

        rows.sort(key=lambda r: (r.workshop, r.order, r.raw_id))
        return rows

    # 明细格式：工序编号/所属车间/设备类型1...
    c_id = _find_col(df, ["工序编号", "工序号", "编号"])
    c_ws = _find_col(df, ["所属车间", "车间"])
    c_order = _find_col(df, ["工序顺序", "顺序", "序号"])
    c_qty = _find_col(df, ["工程量", "数量"])
    c_t1 = _find_col(df, ["设备类型1", "设备1", "设备类型一"])
    c_e1 = _find_col(df, ["设备效率1", "效率1", "设备1效率"])
    c_t2 = _find_col(df, ["设备类型2", "设备2", "设备类型二"])
    c_e2 = _find_col(df, ["设备效率2", "效率2", "设备2效率"])
    c_rep = _find_col(df, ["重复执行次数", "重复次数", "执行次数"])
    c_unit = _find_col(df, ["单位", "工程量单位", "效率单位"])

    if not c_id or not c_ws or not c_qty:
        raise ValueError("工序流程表缺少必要列（工序编号/所属车间/工程量）")

    rows: List[RawOperation] = []
    for idx, row in df.iterrows():
        rid = str(row[c_id]).strip()
        ws_raw = str(row[c_ws]).strip().upper().replace(" ", "").replace("车间", "")
        if ws_raw in ("A", "B", "C", "D", "E"):
            ws = ws_raw
        else:
            m = re.search(r"[ABCDE]", ws_raw)
            if not m:
                raise ValueError(f"无法解析车间: {row[c_ws]}")
            ws = m.group(0)

        order = int(row[c_order]) if c_order and not pd.isna(row.get(c_order)) else int(re.sub(r"\D", "", rid) or 0)
        qty = float(row[c_qty])
        unit_cell = row[c_unit] if c_unit else None
        unit_str = str(unit_cell).strip() if unit_cell is not None and not (isinstance(unit_cell, float) and math.isnan(unit_cell)) else ""

        rep = 1
        if c_rep and not pd.isna(row.get(c_rep)):
            rep = detect_repeat_count(row[c_rep], "")
        else:
            rep = detect_repeat_count(None, " ".join(str(row.get(c, "")) for c in df.columns))

        reqs: List[Dict[str, Any]] = []
        t1 = row.get(c_t1) if c_t1 else None
        e1 = row.get(c_e1) if c_e1 else None
        if t1 is not None and not (isinstance(t1, float) and math.isnan(t1)) and str(t1).strip() and str(t1).strip() != "nan":
            reqs.append({"device_type": str(t1).strip(), "efficiency": float(e1), "unit": unit_str or "m³/h"})
        t2 = row.get(c_t2) if c_t2 else None
        e2 = row.get(c_e2) if c_e2 else None
        if t2 is not None and not (isinstance(t2, float) and math.isnan(t2)) and str(t2).strip() and str(t2).strip() != "nan":
            reqs.append({"device_type": str(t2).strip(), "efficiency": float(e2), "unit": unit_str or "m³/h"})

        if not reqs:
            raise ValueError(f"工序 {rid} 未解析到设备需求")

        rows.append(
            RawOperation(
                raw_id=rid,
                workshop=ws,
                order=order,
                quantity=qty,
                repeat_count=max(1, int(rep)),
                requirements=reqs,
                source_row=int(idx) + 2,
            )
        )

    rows.sort(key=lambda r: (r.workshop, r.order, r.raw_id))
    return rows


def _expand_block(block: List[RawOperation]) -> List[ExpandedOperation]:
    """对同一车间内连续子块展开：多块同次重复且工序顺序连续时按轮次交错展开。"""
    if not block:
        return []
    r = block[0].repeat_count
    if r <= 1:
        return [
            ExpandedOperation(
                op_id=raw.raw_id,
                raw_id=raw.raw_id,
                repeat_index=1,
                repeat_count=1,
                workshop=raw.workshop,
                sequence_key=float(raw.order),
                quantity=raw.quantity,
                requirements=list(raw.requirements),
            )
            for raw in block
        ]
    out: List[ExpandedOperation] = []
    if len(block) == 1:
        raw = block[0]
        for k in range(1, r + 1):
            out.append(
                ExpandedOperation(
                    op_id=f"{raw.raw_id}#{k}",
                    raw_id=raw.raw_id,
                    repeat_index=k,
                    repeat_count=r,
                    workshop=raw.workshop,
                    sequence_key=float(raw.order) + k * 1e-3,
                    quantity=raw.quantity,
                    requirements=list(raw.requirements),
                )
            )
        return out
    # 多工序同重复次数：外层轮次，内层工序顺序（如 C3-C4-C5 各 3 次）
    base_order = float(block[0].order)
    for k in range(1, r + 1):
        for idx, raw in enumerate(block):
            out.append(
                ExpandedOperation(
                    op_id=f"{raw.raw_id}#{k}",
                    raw_id=raw.raw_id,
                    repeat_index=k,
                    repeat_count=r,
                    workshop=raw.workshop,
                    sequence_key=base_order + (k - 1) * 10 + idx * 0.01,
                    quantity=raw.quantity,
                    requirements=list(raw.requirements),
                )
            )
    return out


def expand_repeated_operations(raw_operations: List[RawOperation]) -> List[ExpandedOperation]:
    """
    将重复工序展开为独立子工序。
    同一车间内：若连续多行 repeat_count 相同且均大于 1，且工序顺序字段为连续整数，
    则按「轮次 × 工序」交错展开；否则逐行独立展开。
    """
    from itertools import groupby

    def workshop_key(ro: RawOperation):
        return ro.workshop

    result: List[ExpandedOperation] = []
    for _, ws_group in groupby(sorted(raw_operations, key=lambda x: (x.workshop, x.order, x.raw_id)), key=workshop_key):
        lst = list(ws_group)
        i = 0
        while i < len(lst):
            raw = lst[i]
            if raw.repeat_count <= 1:
                result.extend(_expand_block([raw]))
                i += 1
                continue
            r = raw.repeat_count
            block = [raw]
            j = i + 1
            while j < len(lst):
                nxt = lst[j]
                if nxt.repeat_count != r or nxt.repeat_count <= 1:
                    break
                prev = block[-1]
                if int(nxt.order) != int(prev.order) + 1:
                    break
                block.append(nxt)
                j += 1
            result.extend(_expand_block(block))
            i = j
    result.sort(key=lambda e: (e.workshop, e.sequence_key, e.op_id))
    return result


def parse_devices(df: pd.DataFrame) -> List[Device]:
    # 官方汇总格式：每行一个设备类型，给出班组1/班组2设备编号清单
    c_name = _find_col(df, ["设备名称", "设备类型"])
    c_ids1 = _find_col(df, ["班组1设备编号"])
    c_ids2 = _find_col(df, ["班组2设备编号"])
    c_n1 = _find_col(df, ["班组1"])
    c_n2 = _find_col(df, ["班组2"])
    c_speed = _find_col(df, ["移动速度", "速度"])
    c_price = _find_col(df, ["设备单价", "单价", "价格"])
    if c_name and (c_ids1 or c_n1) and (c_ids2 or c_n2) and c_speed:
        devices: List[Device] = []

        def _split_ids(text: str) -> List[str]:
            if not text or text == "nan":
                return []
            clean = text.replace("。", ";").replace("；", ";").replace("\n", ";")
            return [x.strip() for x in clean.split(";") if x.strip()]

        for _, row in df.iterrows():
            dtype = str(row[c_name]).strip()
            speed = float(row[c_speed])
            price = float(row[c_price]) if c_price and not pd.isna(row.get(c_price)) else 0.0

            ids1 = _split_ids(str(row.get(c_ids1, ""))) if c_ids1 else []
            ids2 = _split_ids(str(row.get(c_ids2, ""))) if c_ids2 else []
            n1 = int(float(row[c_n1])) if c_n1 and not pd.isna(row.get(c_n1)) else len(ids1)
            n2 = int(float(row[c_n2])) if c_n2 and not pd.isna(row.get(c_n2)) else len(ids2)

            if not ids1:
                ids1 = [f"{dtype}1-{i}" for i in range(1, n1 + 1)]
            if not ids2:
                ids2 = [f"{dtype}2-{i}" for i in range(1, n2 + 1)]

            for did in ids1[:n1]:
                devices.append(Device(team=1, device_id=did, device_type=dtype, speed=speed, price=price, initial_location="班组1"))
            for did in ids2[:n2]:
                devices.append(Device(team=2, device_id=did, device_type=dtype, speed=speed, price=price, initial_location="班组2"))
        return devices

    c_team = _find_col(df, ["班组", "所属班组"])
    c_type = _find_col(df, ["设备类型", "类型"])
    c_id = _find_col(df, ["设备编号", "编号"])
    c_speed = _find_col(df, ["移动速度", "速度"])
    c_price = _find_col(df, ["设备单价", "单价", "价格"])

    if not all([c_team, c_type, c_id, c_speed]):
        raise ValueError("班组配置表缺少必要列")

    devices: List[Device] = []
    for _, row in df.iterrows():
        team_raw = row[c_team]
        team = int(float(str(team_raw).replace("班组", "").strip()))
        dtype = str(row[c_type]).strip()
        did = str(row[c_id]).strip()
        speed = float(row[c_speed])
        price = float(row[c_price]) if c_price and not pd.isna(row.get(c_price)) else 0.0
        init_loc = f"班组{team}"
        devices.append(Device(team=team, device_id=did, device_type=dtype, speed=speed, price=price, initial_location=init_loc))
    return devices


def parse_distances(df: pd.DataFrame) -> Dict[Tuple[str, str], float]:
    """
    解析距离为对称矩阵。节点名：班组1、班组2、A、B、C、D、E。
    支持：三列(起点,终点,距离)、或方阵。
    """
    dist: Dict[Tuple[str, str], float] = {}

    def add(u: str, v: str, d: float):
        u, v = _norm(u), _norm(v)
        dist[(u, v)] = float(d)
        dist[(v, u)] = float(d)

    # 三列格式
    c_from = _find_col(df, ["起点", "起始", "from", "From"])
    c_to = _find_col(df, ["终点", "目标", "to", "To"])
    c_d = _find_col(df, ["距离(m)", "距离", "间距", "米", "距离（m）"])

    if c_from and c_to and c_d:
        for _, row in df.iterrows():
            if pd.isna(row.get(c_from)) or pd.isna(row.get(c_to)):
                continue
            d_raw = str(row[c_d]).strip()
            m_num = re.search(r"([0-9]+(?:\.[0-9]+)?)", d_raw)
            if not m_num:
                continue
            add(str(row[c_from]).strip(), str(row[c_to]).strip(), float(m_num.group(1)))
        nodes = sorted({n for (a, b) in dist.keys() for n in (a, b)})
        for n in nodes:
            add(n, n, 0.0)
        return dist

    # 方阵：首列为节点名
    first = df.columns[0]
    labels = [str(x).strip() for x in df[first].tolist()]
    for i, u in enumerate(labels):
        for j, v in enumerate(df.columns[1:]):
            val = df.iloc[i, j + 1]
            if pd.isna(val):
                continue
            add(u, str(v).strip(), float(val))

    for n in ["班组1", "班组2", "A", "B", "C", "D", "E"]:
        add(n, n, 0.0)

    return dist


def distance_between(dist: Dict[Tuple[str, str], float], u: str, v: str) -> float:
    u, v = _norm(u), _norm(v)
    if u == v:
        return 0.0
    key = (u, v)
    if key in dist:
        return float(dist[key])
    # 宽松匹配：班组1 / 班组一
    alias = {"班组一": "班组1", "班组二": "班组2"}
    u2, v2 = alias.get(u, u), alias.get(v, v)
    key2 = (u2, v2)
    if key2 in dist:
        return float(dist[key2])
    raise KeyError(f"距离矩阵缺少 {u} -> {v}")
