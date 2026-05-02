"""
设备甘特图：作业时间条上下双拼（上条=车间配色，下条=工序配色），运输段为斜线填充。
若存在「开工时间 < 转运时长」导致运输段起点为负，则自动扩展横轴左边界以完整显示运输段，
避免在 makespan 很长时裁剪成极短灰条或留白误解。
供问题 1–4 统一调用。

下条工序色：按车间内工序序号 1–6 固定色；展开重复（`op_id` 含 `#` 或 `repeat_index>1`）时下条左右均分，
左半为工序色、右半为轮次色（第 1/2/3 轮循环使用淡蓝 / 淡青 / 淡绿）。
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .scheduler_common import ScheduleRecord
from .time_utils import seconds_to_hhmmss


def _fmt_axis_sec(t: int) -> str:
    """横轴刻度标签：支持负秒（扩展左轴时）。"""
    if t >= 0:
        return seconds_to_hhmmss(t)
    return "-" + seconds_to_hhmmss(-t)


_WORKSHOP_COLORS = {
    "A": "#2563EB",
    "B": "#16A34A",
    "C": "#D97706",
    "D": "#DC2626",
    "E": "#7C3AED",
}
_TRANSPORT_FACE = "#CBD5E1"
_TRANSPORT_EDGE = "#64748B"
# 每台设备时间轴上首段「班组→车间」运输：加粗描边、略增高，避免在超长 makespan 下被作业条压住或细到不可辨
_TRANSPORT_FACE_FIRST = "#94A3B8"
_TRANSPORT_EDGE_FIRST = "#0F172A"

# 车间内工序序号 1–6 统一配色（淡红 / 橙 / 淡黄 / 深绿 / 淡青 / 深蓝）
_SEQ_FACE_COLORS: Dict[int, str] = {
    1: "#FCA5A5",
    2: "#FB923C",
    3: "#FEF08A",
    4: "#15803D",
    5: "#67E8F9",
    6: "#1E3A8A",
}
# 重复展开轮次（右半条）：第 1/2/3 轮淡蓝 / 淡青 / 淡绿，之后循环
_ROUND_FACE_COLORS: Dict[int, str] = {
    1: "#93C5FD",
    2: "#99F6E4",
    3: "#86EFAC",
}


def _norm_ws(workshop: str) -> str:
    return str(workshop).strip().upper().replace("车间", "")


def _pick_cjk_font() -> Optional[str]:
    import matplotlib.font_manager as fm

    for name in (
        "Microsoft YaHei",
        "SimHei",
        "Noto Sans CJK SC",
        "Source Han Sans SC",
        "PingFang SC",
    ):
        if name in {f.name for f in fm.fontManager.ttflist}:
            return name
    return None


def _parse_op_id_visual(op_id: str, record_repeat_index: int) -> Tuple[int, int, bool]:
    """
    从 op_id 与记录解析：车间内工序序号（车间字母后首段数字）、轮次、是否双拼下条。
    双拼：op_id 含「#轮次」或 repeat_index>1（展开重复条）。
    """
    oid = str(op_id).strip()
    repeat_idx = 1
    base = oid
    if "#" in oid:
        left, rt = oid.rsplit("#", 1)
        if rt.isdigit():
            base = left
            repeat_idx = max(1, int(rt))
    rep_from_rec = max(1, int(record_repeat_index or 1))
    repeat_idx = max(repeat_idx, rep_from_rec)
    split_strip = ("#" in oid) or rep_from_rec > 1
    m = re.match(r"^([A-Ea-e])", base)
    if not m:
        return 1, repeat_idx, split_strip
    rest = base[m.end() :]
    m2 = re.match(r"^(\d+)", rest)
    seq = int(m2.group(1)) if m2 else 1
    seq_idx = max(1, min(seq, 6))
    return seq_idx, repeat_idx, split_strip


def _seq_face_color(seq_idx: int) -> str:
    return _SEQ_FACE_COLORS.get(seq_idx, _SEQ_FACE_COLORS[6])


def _round_face_color(repeat_idx: int) -> str:
    k = ((repeat_idx - 1) % 3) + 1
    return _ROUND_FACE_COLORS[k]


def _compact_device_label(device_id: str) -> str:
    """密集甘特图用短设备名，避免 y 轴文字互相挤压。"""
    s = str(device_id)
    replacements = (
        ("自动传感多功能机", "传感"),
        ("自动化输送臂", "输送臂"),
        ("工业清洗机", "清洗"),
        ("精密灌装机", "灌装"),
        ("高速抛光机", "抛光"),
    )
    for old, new in replacements:
        s = s.replace(old, new)
    return s


def plot_device_gantt_dual_strip(
    records: Sequence[ScheduleRecord],
    makespan_sec: int,
    out_png: Path,
    *,
    title: str,
    title_suffix: str = "",
    filter_team: Optional[int] = None,
    show_team_on_op_strip: bool = False,
    transport_strip_op_ids: Optional[Set[str]] = None,
    dense_layout: bool = False,
) -> None:
    """
    按设备分行：每条作业在纵向上分为上下两半——上为车间色、下为工序色；
    运输段仍为开工前 [start-transport, start] 的斜线条；在作业条之后绘制（zorder 更高），首段运输加粗边并可在过窄时标注秒数。
    横轴左边界按全部运输段起点预扩展，以容纳 start<tr 的负起点。

    transport_strip_op_ids：若给定（如问题 1 单车间首道工序），仅对这些 op_id 绘制运输段；
    其余记录即使 transport_sec>0 也不画运输条（同车间后续工序不再出现「班组→车间」块）。
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    font = _pick_cjk_font()
    if font:
        plt.rcParams["font.sans-serif"] = [font, "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    recs = [r for r in records if filter_team is None or int(r.team) == int(filter_team)]
    if not recs:
        return

    by_dev: Dict[str, List[ScheduleRecord]] = {}
    for r in recs:
        by_dev.setdefault(r.device_id, []).append(r)
    for lst in by_dev.values():
        lst.sort(key=lambda x: (x.start_sec, x.seq))

    def _first_start(did: str) -> int:
        return min(x.start_sec for x in by_dev[did])

    dev_ids = sorted(by_dev.keys(), key=lambda d: (_first_start(d), d))
    y_of = {did: i for i, did in enumerate(dev_ids)}
    n_dev = len(dev_ids)
    if n_dev == 0:
        return

    # 运输段 [start-tr, start] 起点可能 <0；横轴若固定从 0 起会裁成短条。预求最左边界以扩展 xlim。
    x_left_bound = 0
    for r in recs:
        tr_raw = max(int(r.transport_sec), 0)
        if transport_strip_op_ids is not None and r.op_id not in transport_strip_op_ids:
            continue
        if tr_raw <= 0:
            continue
        x_left_bound = min(x_left_bound, int(r.start_sec) - tr_raw)

    fig_h = max(6.0, 0.38 * n_dev + 2.0)
    fig_w = 14.0
    if dense_layout:
        fig_h = max(7.0, 0.52 * n_dev + 2.8)
        fig_w = 18.0
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=120, facecolor="#F8FAFC")

    h_half = 0.24
    y_off = 0.125
    trans_h = 0.48

    for r in recs:
        yi = float(y_of[r.device_id])
        y_ws = yi - y_off
        y_op = yi + y_off
        ws = _norm_ws(r.workshop)
        ws_color = _WORKSHOP_COLORS.get(ws, "#64748B")
        seq_idx, rep_idx, split_op = _parse_op_id_visual(r.op_id, r.repeat_index)
        seq_color = _seq_face_color(seq_idx)
        round_color = _round_face_color(rep_idx)
        tr_raw = max(int(r.transport_sec), 0)
        if transport_strip_op_ids is not None and r.op_id not in transport_strip_op_ids:
            tr = 0
        else:
            tr = tr_raw
        w = max(r.end_sec - r.start_sec, 1)
        ax.barh(
            y_ws,
            w,
            left=r.start_sec,
            height=h_half,
            color=ws_color,
            edgecolor="white",
            linewidth=0.45,
            alpha=0.92,
            zorder=2,
        )
        if split_op and w >= 2:
            w2 = w / 2.0
            ax.barh(
                y_op,
                w2,
                left=r.start_sec,
                height=h_half,
                color=seq_color,
                edgecolor="white",
                linewidth=0.45,
                alpha=0.92,
                zorder=2,
            )
            ax.barh(
                y_op,
                w2,
                left=r.start_sec + w2,
                height=h_half,
                color=round_color,
                edgecolor="white",
                linewidth=0.45,
                alpha=0.92,
                zorder=2,
            )
        else:
            ax.barh(
                y_op,
                w,
                left=r.start_sec,
                height=h_half,
                color=seq_color,
                edgecolor="white",
                linewidth=0.45,
                alpha=0.92,
                zorder=2,
            )
        # 仅在下条标注文字（黑色）；上条仅靠颜色区分车间，避免白字与下条黑字叠压、窄条重叠。
        min_w_label = max(makespan_sec * (0.055 if dense_layout else 0.014), 1800 if dense_layout else 180)
        min_w_two_lines = max(makespan_sec * (0.085 if dense_layout else 0.022), 3200 if dense_layout else 420)
        if w >= min_w_label:
            cx = r.start_sec + w / 2
            op_txt = str(r.op_id)
            fs = 5.2 if dense_layout else 6.0
            if show_team_on_op_strip:
                if dense_layout:
                    op_txt = f"{r.op_id}/班{r.team}"
                elif w >= min_w_two_lines:
                    op_txt = f"{r.op_id}\n班{r.team}"
                else:
                    op_txt = f"{r.op_id}·班{r.team}"
                    fs = 5.5
            ax.text(
                cx,
                y_op,
                op_txt,
                ha="center",
                va="center",
                fontsize=fs,
                color="#0F172A",
                fontweight="bold",
                linespacing=0.95,
                clip_on=True,
                zorder=6,
            )

    # 第二遍：运输段盖在作业条之上，首段加粗；过长 makespan 下 tr 占比极小时加文字标注
    ms = max(int(makespan_sec), 1)
    thin_tr = max(120, ms // 500)
    for did in dev_ids:
        yi = float(y_of[did])
        lst = by_dev[did]
        seen_transport = False
        for r in lst:
            tr_raw = max(int(r.transport_sec), 0)
            if transport_strip_op_ids is not None and r.op_id not in transport_strip_op_ids:
                continue
            tr = tr_raw
            if tr <= 0:
                continue
            is_first = not seen_transport
            seen_transport = True
            h_tr = min(0.58, trans_h * 1.14) if is_first else trans_h
            face = _TRANSPORT_FACE_FIRST if is_first else _TRANSPORT_FACE
            edge = _TRANSPORT_EDGE_FIRST if is_first else _TRANSPORT_EDGE
            lw = 1.6 if is_first else 0.55
            ax.barh(
                yi,
                tr,
                left=r.start_sec - tr,
                height=h_tr,
                color=face,
                edgecolor=edge,
                linewidth=lw,
                hatch="///",
                alpha=0.96 if is_first else 0.95,
                zorder=4,
            )
            if is_first and tr <= thin_tr and not dense_layout:
                cx = r.start_sec - tr / 2.0
                ax.text(
                    cx,
                    yi,
                    f"转运{tr}s",
                    ha="center",
                    va="center",
                    fontsize=5.5,
                    color="#0F172A",
                    zorder=5,
                    bbox=dict(boxstyle="round,pad=0.12", facecolor="#F8FAFC", edgecolor="#64748B", linewidth=0.4, alpha=0.92),
                )

    ax.set_yticks(range(n_dev))
    if dense_layout:
        ax.set_yticklabels([_compact_device_label(x) for x in dev_ids], fontsize=6.8)
    else:
        ax.set_yticklabels(dev_ids, fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel("时间（秒）", fontsize=10, color="#334155")
    ax.set_ylabel("设备编号", fontsize=10, color="#334155")
    full_title = f"{title}{title_suffix}"
    ax.set_title(full_title, fontsize=12, fontweight="bold", color="#0F172A", pad=12)
    xmax = max(int(makespan_sec * 1.02), 1)
    xmin = int(math.floor(x_left_bound * 1.02)) if x_left_bound < 0 else 0
    ax.set_xlim(xmin, xmax)
    ax.axvline(makespan_sec, color="#DC2626", linestyle="--", linewidth=1.0, alpha=0.85, label="Makespan")
    ax.grid(axis="x", color="#CBD5E1", linestyle="-", linewidth=0.4, alpha=0.85)
    ax.set_facecolor("#F1F5F9")
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)

    tick_step = max(3600, int(makespan_sec) // 8)
    lo, hi = xmin, xmax
    t_tick = int(math.floor(lo / tick_step) * tick_step) if lo < 0 else 0
    xticks: List[int] = []
    guard = 0
    while t_tick <= hi + tick_step and guard < 120:
        xticks.append(t_tick)
        t_tick += tick_step
        guard += 1
    if not xticks:
        xticks = [xmin, 0, makespan_sec] if xmin < 0 else [0, makespan_sec]
    ax.set_xticks(xticks)
    ax.set_xticklabels([_fmt_axis_sec(int(t)) for t in xticks], rotation=25, ha="right", fontsize=7)

    handles = [
        Patch(facecolor=c, edgecolor="white", linewidth=0.5, label=f"上条 {k} 车间", alpha=0.92)
        for k, c in sorted(_WORKSHOP_COLORS.items())
    ]
    for i in range(1, 7):
        handles.append(
            Patch(
                facecolor=_SEQ_FACE_COLORS[i],
                edgecolor="white",
                linewidth=0.5,
                label=f"下条左 序{i}",
                alpha=0.92,
            )
        )
    for ri, lab in ((1, "下条右 第1轮"), (2, "第2轮"), (3, "第3轮")):
        handles.append(
            Patch(
                facecolor=_ROUND_FACE_COLORS[ri],
                edgecolor="white",
                linewidth=0.5,
                label=lab,
                alpha=0.92,
            )
        )
    handles.append(
        Patch(
            facecolor=_TRANSPORT_FACE,
            edgecolor=_TRANSPORT_EDGE,
            linewidth=0.5,
            hatch="///",
            label="运输段",
            alpha=0.95,
        )
    )
    handles.append(Line2D([0], [0], color="#DC2626", linestyle="--", linewidth=1.2, label="Makespan"))
    if dense_layout:
        ax.legend(
            handles=handles,
            loc="upper left",
            bbox_to_anchor=(1.005, 1.0),
            fontsize=5.2,
            framealpha=0.92,
            ncol=1,
            borderaxespad=0.0,
        )
    else:
        ax.legend(handles=handles, loc="upper right", fontsize=5.5, framealpha=0.92, ncol=3)

    if dense_layout:
        plt.tight_layout(rect=(0.0, 0.0, 0.86, 1.0))
    else:
        plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
