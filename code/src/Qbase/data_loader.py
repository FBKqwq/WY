"""Excel 数据加载。"""
from __future__ import annotations

from pathlib import Path

import pandas as pd


def load_operation_table(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"未找到工序流程表: {p}")
    return pd.read_excel(p)


def load_device_table(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"未找到班组配置表: {p}")
    return pd.read_excel(p)


def load_distance_table(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"未找到车间距离表: {p}")
    return pd.read_excel(p)
