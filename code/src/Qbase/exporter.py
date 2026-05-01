"""结果导出 Excel。"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .scheduler_common import ScheduleRecord
from .time_utils import seconds_to_hhmmss


def export_schedule(
    records: List[ScheduleRecord],
    out_path: str | Path,
    makespan_sec: int,
    include_team: bool = False,
    detail_sheet_name: str = "表1_调度明细",
    summary_label: str = "完成问题1任务的最短时长(s)",
) -> None:
    """导出调度表：序号、设备编号、起始时间、结束时间、持续工作时间(s)、工序编号（可选班组）。"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for r in records:
        row = {
            "序号": r.seq,
            "设备编号": r.device_id,
            "起始时间": seconds_to_hhmmss(r.start_sec),
            "结束时间": seconds_to_hhmmss(r.end_sec),
            "持续工作时间(s)": r.duration_sec,
            "工序编号": r.op_id,
        }
        if include_team:
            row["班组"] = f"班组{r.team}"
        rows.append(row)
    df = pd.DataFrame(rows)
    summary = pd.DataFrame([{summary_label: makespan_sec}])
    with pd.ExcelWriter(out_path, engine="openpyxl") as w:
        df.to_excel(w, sheet_name=detail_sheet_name, index=False)
        summary.to_excel(w, sheet_name="汇总", index=False)


def export_purchase_plan(
    rows: list[dict],
    out_path: str | Path,
    total_cost: int,
) -> None:
    """导出问题4表5购买方案。"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    summary = pd.DataFrame([{"购买设备总费用(元)": int(total_cost)}])
    with pd.ExcelWriter(out_path, engine="openpyxl") as w:
        df.to_excel(w, sheet_name="表5_购买方案", index=False)
        summary.to_excel(w, sheet_name="汇总", index=False)
