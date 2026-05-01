"""时间计算与格式化工具（秒级向上取整）。"""
from __future__ import annotations

import math
import re
from typing import Optional


def calc_work_duration(
    quantity: float,
    efficiency: float,
    unit: Optional[str] = None,
) -> int:
    """
    计算单台设备完成工程量的持续作业时间（秒），向上取整。
    efficiency 为 0 或非法时抛出 ValueError。
    """
    if efficiency is None or float(efficiency) <= 0:
        raise ValueError("设备效率必须为正数")
    q = float(quantity)
    v = float(efficiency)
    u = (unit or "").strip().lower().replace(" ", "")

    # 常见组合：m³/h、m3/h、立方米/小时
    if "m³/h" in u or "m3/h" in u or ("m³" in u and "/h" in u) or u in ("m³/h", "m3/h"):
        sec = q / v * 3600.0
        return int(math.ceil(sec))

    # m³/s
    if "m³/s" in u or "m3/s" in u or ("/s" in u and ("m³" in u or "m3" in u)):
        sec = q / v
        return int(math.ceil(sec))

    # 纯小时效率（无显式体积单位）
    if "/h" in u or u == "h" or u.endswith("h"):
        sec = q / v * 3600.0
        return int(math.ceil(sec))

    # 纯秒
    if u == "s" or "/s" in u:
        sec = q / v
        return int(math.ceil(sec))

    # 默认按 m³/h 处理（赛题附件常见）
    sec = q / v * 3600.0
    return int(math.ceil(sec))


def calc_transport_time(distance_m: float, speed_m_per_s: float) -> int:
    """跨节点转运时间（秒）：ceil(distance / speed)。"""
    if speed_m_per_s is None or float(speed_m_per_s) <= 0:
        raise ValueError("设备移动速度必须为正数")
    d = float(distance_m)
    if d <= 0:
        return 0
    return int(math.ceil(d / float(speed_m_per_s)))


def seconds_to_hhmmss(total_seconds: int) -> str:
    """将非负秒数转为 HH:MM:SS（可超过 24 小时）。"""
    if total_seconds < 0:
        raise ValueError("秒数不能为负")
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


_CN_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


def _cn_simple_to_int(text: str) -> Optional[int]:
    t = text.strip()
    if not t:
        return None
    if t == "十":
        return 10
    # 十一 ... 十九
    if len(t) == 2 and t[0] == "十" and t[1] in _CN_DIGITS:
        return 10 + _CN_DIGITS[t[1]]
    # 二十、二十一
    if "十" in t:
        parts = t.split("十", 1)
        if len(parts) == 2:
            a, b = parts[0], parts[1]
            tens = _CN_DIGITS.get(a, 1 if a == "" else None)
            if tens is None:
                return None
            tens *= 10
            ones = _CN_DIGITS[b] if b else 0
            return tens + ones
    if t in _CN_DIGITS:
        return _CN_DIGITS[t]
    return None


def detect_repeat_count(cell_value, row_text: str = "") -> int:
    """
    从单元格或整行文本识别重复执行次数，默认 1。
    支持：3、3次、重复3次、×3、x3、执行三次 等。
    """
    merged = f"{cell_value if cell_value is not None else ''} {row_text or ''}"
    merged_norm = str(merged)

    m = re.search(r"(?:重复|执行)?\s*(\d+)\s*次", merged_norm)
    if m:
        return max(1, int(m.group(1)))

    m = re.search(r"[×xX]\s*(\d+)", merged_norm)
    if m:
        return max(1, int(m.group(1)))

    for token in ("三次", "3次", "三遍", "三轮"):
        if token in merged_norm:
            return 3

    cn = re.search(r"(执行|重复)?\s*([一二两三四五六七八九十]+)\s*次", merged_norm)
    if cn:
        val = _cn_simple_to_int(cn.group(2))
        if val is not None:
            return max(1, val)

    try:
        if cell_value is None or (isinstance(cell_value, float) and math.isnan(cell_value)):
            return 1
        if isinstance(cell_value, (int, float)):
            iv = int(cell_value)
            return max(1, iv) if iv == cell_value or isinstance(cell_value, int) else max(1, iv)
    except (TypeError, ValueError):
        pass

    return 1
