"""
调度结果辅助图表：设备作业量、利用率、工序完工、求解过程对比等。
供 Q1–Q4 入口脚本调用；依赖 matplotlib、numpy。
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from Qbase.parsers import ExpandedOperation
from Qbase.scheduler_common import ScheduleRecord
from Qbase.time_utils import seconds_to_hhmmss


def _apply_matplotlib_cjk() -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.font_manager as fm
    import matplotlib.pyplot as plt

    for name in (
        "Microsoft YaHei",
        "SimHei",
        "Noto Sans CJK SC",
        "Source Han Sans SC",
        "PingFang SC",
    ):
        if name in {f.name for f in fm.fontManager.ttflist}:
            plt.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            break
    plt.rcParams["axes.unicode_minus"] = False


def plot_device_work_duration_bars(
    records: List[ScheduleRecord],
    makespan_sec: int,
    out_png: Path,
    title: str,
    color_by_team: bool = False,
) -> None:
    """各设备累计持续作业时间（duration_sec 之和）横向对比。"""
    _apply_matplotlib_cjk()
    import matplotlib.pyplot as plt

    acc: Dict[str, int] = defaultdict(int)
    team_of: Dict[str, int] = {}
    for r in records:
        acc[r.device_id] += r.duration_sec
        team_of[r.device_id] = r.team
    if not acc:
        return
    ids = sorted(acc.keys(), key=lambda d: (-acc[d], d))
    vals = [acc[d] for d in ids]
    fig_h = max(4.0, 0.35 * len(ids) + 1.5)
    fig, ax = plt.subplots(figsize=(10, fig_h), dpi=120, facecolor="#F8FAFC")
    if color_by_team:
        colors = ["#0EA5E9" if team_of.get(d, 1) == 1 else "#F97316" for d in ids]
    else:
        colors = ["#3B82F6"] * len(ids)
    y = list(range(len(ids)))
    ax.barh(y, vals, color=colors, edgecolor="white", height=0.65)
    ax.set_yticks(y)
    ax.set_yticklabels(ids, fontsize=9)
    ax.set_xlabel("累计持续作业时间（s）", fontsize=10, color="#334155")
    ax.set_title(
        f"{title}\n全局 Makespan = {makespan_sec}s（{seconds_to_hhmmss(makespan_sec)}）",
        fontsize=11,
        fontweight="bold",
        color="#0F172A",
    )
    ax.axvline(makespan_sec, color="#DC2626", linestyle="--", linewidth=1.0, alpha=0.65, label="Makespan（参考）")
    ax.grid(axis="x", color="#CBD5E1", linestyle="-", linewidth=0.4, alpha=0.85)
    ax.set_facecolor("#F1F5F9")
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    if color_by_team:
        from matplotlib.patches import Patch

        ax.legend(
            handles=[
                Patch(facecolor="#0EA5E9", label="班组1"),
                Patch(facecolor="#F97316", label="班组2"),
            ],
            loc="lower right",
            fontsize=8,
        )
    else:
        ax.legend(loc="lower right", fontsize=8)
    plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def plot_device_utilization_bars(
    records: List[ScheduleRecord],
    makespan_sec: int,
    out_png: Path,
    title: str,
) -> None:
    """各设备利用率 = 累计作业时间 / Makespan（单设备上限 1）。"""
    if makespan_sec <= 0:
        return
    _apply_matplotlib_cjk()
    import matplotlib.pyplot as plt

    acc: Dict[str, int] = defaultdict(int)
    for r in records:
        acc[r.device_id] += r.duration_sec
    ids = sorted(acc.keys(), key=lambda d: (-acc[d] / makespan_sec, d))
    util = [acc[d] / makespan_sec for d in ids]
    fig_h = max(4.0, 0.35 * len(ids) + 1.5)
    fig, ax = plt.subplots(figsize=(9, fig_h), dpi=120, facecolor="#F8FAFC")
    y = list(range(len(ids)))
    ax.barh(y, util, color="#10B981", edgecolor="white", height=0.65)
    ax.set_yticks(y)
    ax.set_yticklabels(ids, fontsize=9)
    ax.set_xlabel("利用率（作业累计 / Makespan）", fontsize=10, color="#334155")
    ax.set_xlim(0, min(1.05, max(util) * 1.08) if util else 1.0)
    ax.set_title(title, fontsize=11, fontweight="bold", color="#0F172A")
    ax.axvline(1.0, color="#64748B", linestyle=":", linewidth=1.0, alpha=0.8)
    ax.grid(axis="x", color="#CBD5E1", linestyle="-", linewidth=0.4, alpha=0.85)
    ax.set_facecolor("#F1F5F9")
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def plot_operation_finish_timeline(
    records: List[ScheduleRecord],
    expanded: List[ExpandedOperation],
    out_png: Path,
    title: str,
) -> None:
    """按工序展开顺序展示各 op 的完工时刻（算法结果总览）。"""
    _apply_matplotlib_cjk()
    import matplotlib.pyplot as plt

    op_finish: Dict[str, int] = {}
    for r in records:
        op_finish[r.op_id] = max(op_finish.get(r.op_id, 0), r.end_sec)
    ordered_ops: List[str] = []
    for o in sorted(expanded, key=lambda x: (x.sequence_key, x.op_id)):
        if o.op_id not in ordered_ops:
            ordered_ops.append(o.op_id)
    if not ordered_ops:
        return
    ends = [op_finish.get(oid, 0) for oid in ordered_ops]
    x = list(range(len(ordered_ops)))
    fig, ax = plt.subplots(figsize=(max(10, len(ordered_ops) * 0.35), 5), dpi=120, facecolor="#F8FAFC")
    ax.bar(x, ends, color="#6366F1", edgecolor="white", width=0.72)
    ax.set_xticks(x)
    ax.set_xticklabels(ordered_ops, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("工序完工时刻（s）", fontsize=10, color="#334155")
    ax.set_title(title, fontsize=11, fontweight="bold", color="#0F172A")
    ax.grid(axis="y", color="#CBD5E1", linestyle="-", linewidth=0.4, alpha=0.85)
    ax.set_facecolor("#F1F5F9")
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


_CHAIN_LABEL_CN = {
    "fixed_alloc": "CP 固定并联台数链",
    "variable_n": "CP 可变并联链",
    "legacy_repaired": "CP Legacy 对照链",
}


def plot_solver_makespan_comparison(
    out_png: Path,
    title: str,
    greedy_ms: int,
    final_ms: int,
    final_mode: str,
    chain_stats: Optional[Sequence[Tuple[str, int, str]]] = None,
) -> None:
    """
    展示求解过程：贪心 baseline、各 CP 子链 Makespan、最终采用值。
    chain_stats: (链简称, makespan, 求解状态摘要)，makespan 为 0 表示该链未得到有效解。
    """
    _apply_matplotlib_cjk()
    import matplotlib.pyplot as plt

    names: List[str] = []
    vals: List[int] = []
    colors: List[str] = []

    names.append("贪心 baseline")
    vals.append(greedy_ms)
    colors.append("#94A3B8")

    if chain_stats:
        for tag, ms, _st in chain_stats:
            label = _CHAIN_LABEL_CN.get(tag, tag)
            names.append(label)
            vals.append(ms if ms > 0 else 0)
            colors.append("#A78BFA" if ms > 0 else "#E2E8F0")

    names.append(f"最终采用\n({final_mode})")
    vals.append(final_ms)
    colors.append("#0EA5E9")

    fig, ax = plt.subplots(figsize=(11, 5), dpi=120, facecolor="#F8FAFC")
    x = list(range(len(names)))
    bars = ax.bar(x, vals, color=colors, edgecolor="white", linewidth=0.6, width=0.62)
    ax.set_xticks(x)
    ax.set_xticklabels(names, fontsize=9)
    ax.set_ylabel("Makespan（s）", fontsize=10, color="#334155")
    ax.set_title(title, fontsize=12, fontweight="bold", color="#0F172A", pad=10)
    ax.grid(axis="y", color="#CBD5E1", linestyle="-", linewidth=0.4, alpha=0.85)
    ax.set_facecolor("#F1F5F9")
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for i, b in enumerate(bars):
        h = b.get_height()
        if h > 0:
            ax.text(b.get_x() + b.get_width() / 2, h, f"{int(h)}", ha="center", va="bottom", fontsize=8, color="#0F172A")
    plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def _norm_ws(workshop: str) -> str:
    return str(workshop).strip().upper().replace("车间", "")


_WS_COLOR_3D = {
    "A": "#2563EB",
    "B": "#16A34A",
    "C": "#D97706",
    "D": "#DC2626",
    "E": "#7C3AED",
}


def _device_axis_tick_short(device_id: str) -> str:
    """
    横轴刻度用短编号（如 1-2、1-1），从完整设备编号串中提取末段「数字-数字」；
    无匹配时退回原串截断，避免轴标签过长。
    """
    s = str(device_id).strip()
    found = re.findall(r"\d+\s*-\s*\d+", s)
    if found:
        return found[-1].replace(" ", "")
    m = re.search(r"(\d+-\d+)\s*$", s)
    if m:
        return m.group(1)
    return s if len(s) <= 10 else s[:10] + "…"


def _dedupe_axis_short_labels(shorts: List[str]) -> List[str]:
    """短编号重复时在括号内加序号，保证刻度可区分。"""
    seen: Dict[str, int] = {}
    out: List[str] = []
    for t in shorts:
        seen[t] = seen.get(t, 0) + 1
        if seen[t] == 1:
            out.append(t)
        else:
            out.append(f"{t}({seen[t]})")
    return out


def _peak_annotation_device_name(rec: ScheduleRecord) -> str:
    """三维图最高点旁注：设备类型 + 完整设备编号（与调度表一致）。"""
    return f"{rec.device_type} {rec.device_id}".strip()


def _operation_wallclock_bounds(records: List[ScheduleRecord]) -> Tuple[Dict[str, int], Dict[str, int]]:
    """每道工序在所有设备行上的最早开始、最晚结束（阶段墙钟）。"""
    op_lo: Dict[str, int] = {}
    op_hi: Dict[str, int] = {}
    for r in records:
        oid = r.op_id
        if oid not in op_lo:
            op_lo[oid] = r.start_sec
        else:
            op_lo[oid] = min(op_lo[oid], r.start_sec)
        if oid not in op_hi:
            op_hi[oid] = r.end_sec
        else:
            op_hi[oid] = max(op_hi[oid], r.end_sec)
    return op_lo, op_hi


def _triangulate_xy_for_trisurf(
    xs: Sequence[float],
    ys: Sequence[float],
) -> Tuple[np.ndarray, np.ndarray, Any]:
    """
    对 (x,y) 做 Delaunay 三角剖分供 plot_trisurf；对重合的 (设备索引, 时间) 微扰 y，
    避免剖分退化。
    """
    from matplotlib.tri import Triangulation

    xs_arr = np.asarray(xs, dtype=float)
    ys_arr = np.asarray(ys, dtype=float)
    seen: Dict[Tuple[int, int], int] = {}
    ys_adj = ys_arr.copy()
    for i in range(len(xs_arr)):
        xi = int(round(xs_arr[i]))
        yi_key = int(round(ys_arr[i] * 3600000.0))
        k = (xi, yi_key)
        n = seen.get(k, 0)
        if n > 0:
            ys_adj[i] = ys_arr[i] + 2e-5 * n
        seen[k] = n + 1
    tri = Triangulation(xs_arr, ys_adj)
    return xs_arr, ys_adj, tri


def plot_3d_schedule_scatter(
    records: List[ScheduleRecord],
    makespan_sec: int,
    out_png: Path,
    title: str,
) -> None:
    """
    三维调度图（工序瓶颈：点串三角网成面 + 散点）：
    - 横轴刻度：仅显示短编号「数字-数字」（如 1-2），由完整 device_id 解析；重复短号时加 (2) 区分；
    - 全局 Z 最高点旁标注**设备名称**（设备类型 + 完整设备编号）；
    - 纵轴：任务时间中点（小时）；竖轴：工序阶段墙钟跨度（秒）；
    - 曲面：`plot_trisurf` 三角网；散点描边=车间。
    """
    if not records or makespan_sec <= 0:
        return
    _apply_matplotlib_cjk()
    import matplotlib.pyplot as plt
    from matplotlib.cm import ScalarMappable
    from matplotlib.colors import Normalize
    from mpl_toolkits.mplot3d import Axes3D  # noqa: F401

    op_lo, op_hi = _operation_wallclock_bounds(records)
    span_by_op = {oid: max(0, op_hi[oid] - op_lo[oid]) for oid in op_lo}

    dev_ids = sorted({r.device_id for r in records}, key=lambda x: (len(str(x)), str(x)))
    d_index = {d: i for i, d in enumerate(dev_ids)}
    n_dev = len(dev_ids)
    xs = [float(d_index[r.device_id]) for r in records]
    ys = [(r.start_sec + r.end_sec) / 2.0 / 3600.0 for r in records]
    zs = [float(span_by_op.get(r.op_id, 0.0)) for r in records]

    durs = [float(r.duration_sec) for r in records]
    max_dur = max(durs) if durs else 1.0
    sizes = [18.0 + 95.0 * (d / max_dur) for d in durs]
    edge_cs = [_WS_COLOR_3D.get(_norm_ws(r.workshop), "#64748B") for r in records]

    ranked = sorted(span_by_op.items(), key=lambda kv: -kv[1])[:6]
    span_note = "、".join(f"{oid}({int(s)}s)" for oid, s in ranked if s > 0) or "—"

    z_max = max(zs) if zs else 1.0
    z_max = max(z_max, 1.0)
    norm_z = Normalize(vmin=0.0, vmax=z_max)
    y_max_h = max(float(makespan_sec) / 3600.0 * 1.02, max(ys) * 1.02 if ys else 0.0, 1e-3)

    fig = plt.figure(figsize=(13.0, 7.4), dpi=120, facecolor="#F8FAFC")
    ax = fig.add_subplot(111, projection="3d")

    if len(xs) >= 3:
        try:
            xs_arr, ys_adj, tri = _triangulate_xy_for_trisurf(xs, ys)
            zs_arr = np.asarray(zs, dtype=float)
            ax.plot_trisurf(
                xs_arr,
                ys_adj,
                zs_arr,
                triangles=tri.triangles,
                cmap="YlOrRd",
                norm=norm_z,
                alpha=0.48,
                linewidth=0.25,
                edgecolor="#94A3B8",
                antialiased=True,
                shade=True,
            )
        except (RuntimeError, ValueError):
            pass

    ax.scatter(
        xs,
        ys,
        zs,
        c=zs,
        cmap="YlOrRd",
        norm=norm_z,
        s=sizes,
        alpha=0.88,
        edgecolors=edge_cs,
        linewidths=0.55,
        depthshade=True,
    )

    z_arr = np.asarray(zs, dtype=float)
    imax = int(np.argmax(z_arr))
    r_peak = records[imax]
    peak_name = _peak_annotation_device_name(r_peak)
    dz = max(z_max * 0.04, 1.0)
    ax.text(
        float(xs[imax]),
        float(ys[imax]),
        float(zs[imax]) + dz,
        f"最高点\n{peak_name}",
        fontsize=7,
        color="#0F172A",
        ha="center",
        va="bottom",
        zorder=10,
        bbox=dict(boxstyle="round,pad=0.28", facecolor="#FFFBEB", edgecolor="#DC2626", linewidth=0.9, alpha=0.95),
    )

    sm = ScalarMappable(cmap="YlOrRd", norm=norm_z)
    sm.set_array(np.linspace(0.0, z_max, 256))
    cbar = fig.colorbar(sm, ax=ax, shrink=0.55, pad=0.11, aspect=22)
    cbar.set_label("工序阶段墙钟跨度（s），三角网由任务点连接成面", fontsize=8)

    ax.set_xlabel("设备短编号（字典序；刻度形如 1-2）", fontsize=9, labelpad=8)
    ax.set_ylabel("任务时间中点（h）", fontsize=9, labelpad=8)
    ax.set_zlabel("工序阶段墙钟跨度（s）", fontsize=9, labelpad=8)
    ax.set_title(
        f"{title}\nMakespan={makespan_sec}s（{seconds_to_hhmmss(makespan_sec)}）\n"
        f"三角网串点成面；墙钟跨度 Top：{span_note}",
        fontsize=9,
        pad=10,
    )
    ax.set_xlim(-0.5, max(n_dev - 0.5, 0.5))
    ax.set_ylim(0.0, y_max_h)
    ax.set_zlim(0.0, z_max * 1.05)
    ax.set_xticks(list(range(len(dev_ids))))
    xtick_fs = 6 if len(dev_ids) > 14 else 7
    tick_short = _dedupe_axis_short_labels([_device_axis_tick_short(d) for d in dev_ids])
    ax.set_xticklabels(tick_short, fontsize=xtick_fs, rotation=38, ha="right")
    ax.view_init(elev=24, azim=-58)

    from matplotlib.lines import Line2D

    leg_handles = [
        Line2D(
            [0],
            [0],
            marker="o",
            color="w",
            markerfacecolor="#F1F5F9",
            markeredgecolor=c,
            markersize=8,
            label=f"{wk}车间",
        )
        for wk, c in sorted(_WS_COLOR_3D.items())
    ]
    ax.legend(
        handles=leg_handles,
        title="散点描边=车间",
        loc="upper left",
        bbox_to_anchor=(0.02, 0.98),
        fontsize=7,
        title_fontsize=8,
        framealpha=0.92,
    )

    fig.subplots_adjust(left=0.02, right=0.87, top=0.88, bottom=0.06)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def plot_schedule_radar(
    records: List[ScheduleRecord],
    expanded: List[ExpandedOperation],
    makespan_sec: int,
    out_png: Path,
    title: str,
    total_device_count: int,
) -> None:
    """
    雷达图：多指标归一化到 [0,1] 的综合形态（利用率、车间均衡、设备参与、并联强度、运输占比等）。
    """
    if not records or makespan_sec <= 0 or total_device_count <= 0:
        return
    _apply_matplotlib_cjk()
    import matplotlib.pyplot as plt

    acc: Dict[str, int] = defaultdict(int)
    for r in records:
        acc[r.device_id] += r.duration_sec
    total_work = sum(acc.values())
    util_mean = min(1.0, total_work / float(makespan_sec * total_device_count))
    util_peak = min(1.0, max((acc[d] / makespan_sec for d in acc), default=0.0))

    workshops = sorted({_norm_ws(o.workshop) for o in expanded})
    finish_m: Dict[str, int] = {w: 0 for w in workshops}
    for r in records:
        wk = _norm_ws(r.workshop)
        if wk in finish_m:
            finish_m[wk] = max(finish_m[wk], r.end_sec)
    fins = [finish_m[w] for w in workshops if finish_m[w] > 0]
    if len(fins) >= 2:
        mu = float(np.mean(fins))
        balance = 1.0 - min(1.0, float(np.std(fins)) / max(mu, 1.0))
    else:
        balance = 1.0

    used = len({r.device_id for r in records})
    participation = used / float(total_device_count)

    n_ops = len({r.op_id for r in records})
    parallel = (len(records) / float(max(n_ops, 1))) / 6.0
    parallel = min(1.0, parallel)

    tot_trans = sum(int(r.transport_sec) for r in records)
    trans_ratio = tot_trans / float(max(makespan_sec * max(used, 1), 1))
    transport_score = 1.0 - min(1.0, trans_ratio * 3.0)

    labels = (
        "利用率均值",
        "峰值利用率",
        "车间完工均衡",
        "设备参与率",
        "并联强度",
        "运输紧凑度",
    )
    values = (
        util_mean,
        util_peak,
        max(0.0, min(1.0, balance)),
        min(1.0, participation),
        min(1.0, parallel),
        max(0.0, min(1.0, transport_score)),
    )
    angles = np.linspace(0, 2 * np.pi, len(labels), endpoint=False)
    vals = np.array(values, dtype=float)
    angles_c = np.concatenate([angles, angles[:1]])
    vals_c = np.concatenate([vals, vals[:1]])

    fig, ax = plt.subplots(figsize=(7.5, 7.5), subplot_kw=dict(projection="polar"), dpi=120, facecolor="#F8FAFC")
    ax.plot(angles_c, vals_c, color="#0EA5E9", linewidth=2.0)
    ax.fill(angles_c, vals_c, color="#38BDF8", alpha=0.28)
    ax.set_xticks(angles)
    ax.set_xticklabels(labels, fontsize=9)
    ax.set_ylim(0, 1.05)
    ax.set_yticks([0.2, 0.4, 0.6, 0.8, 1.0])
    ax.set_title(title + "\n（各轴 0–1 归一，形态供方案间对比）", fontsize=10, fontweight="bold", pad=16)
    ax.grid(color="#CBD5E1", linestyle="-", linewidth=0.5, alpha=0.9)
    fig.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def plot_device_time_heatmap(
    records: List[ScheduleRecord],
    makespan_sec: int,
    out_png: Path,
    title: str,
    n_bins: int = 56,
) -> None:
    """热力图：纵轴设备、横轴时间桶，颜色表示该桶内作业时间占桶长的比例。"""
    if not records or makespan_sec <= 0:
        return
    _apply_matplotlib_cjk()
    import matplotlib.pyplot as plt

    dev_ids = sorted({r.device_id for r in records}, key=lambda x: (len(str(x)), str(x)))
    n_dev = len(dev_ids)
    if n_dev == 0:
        return
    idx = {d: i for i, d in enumerate(dev_ids)}
    n_bins = int(max(12, min(n_bins, max(24, makespan_sec // 400))))
    edges = np.linspace(0, float(makespan_sec), n_bins + 1)
    mat = np.zeros((n_dev, n_bins), dtype=float)
    for r in records:
        i = idx[r.device_id]
        s, e = float(r.start_sec), float(r.end_sec)
        for j in range(n_bins):
            lo, hi = edges[j], edges[j + 1]
            if e <= lo or s >= hi:
                continue
            ov = max(0.0, min(e, hi) - max(s, lo))
            mat[i, j] += ov / max(hi - lo, 1e-9)
    mat = np.clip(mat, 0.0, 1.0)

    fig_h = max(5.0, min(22.0, 0.28 * n_dev + 2.2))
    fig, ax = plt.subplots(figsize=(12, fig_h), dpi=120, facecolor="#F8FAFC")
    im = ax.imshow(mat, aspect="auto", cmap="YlOrRd", vmin=0.0, vmax=1.0, interpolation="nearest")
    ax.set_xlabel(f"时间轴（共 {n_bins} 桶，0 → Makespan）", fontsize=10, color="#334155")
    ax.set_ylabel("设备（字典序）", fontsize=10, color="#334155")
    ax.set_title(title, fontsize=11, fontweight="bold", color="#0F172A")
    step = max(1, n_dev // 35)
    ax.set_yticks(list(range(0, n_dev, step)))
    ax.set_yticklabels([dev_ids[i] for i in range(0, n_dev, step)], fontsize=7)
    ax.set_xticks(np.linspace(0, n_bins - 1, min(9, n_bins)).astype(int))
    cbar = fig.colorbar(im, ax=ax, fraction=0.025, pad=0.02)
    cbar.set_label("桶内作业占比", fontsize=9)
    fig.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def plot_advanced_viz_bundle(
    records: List[ScheduleRecord],
    expanded: List[ExpandedOperation],
    makespan_sec: int,
    out_dir: Path,
    file_prefix: str,
    title_base: str,
    total_device_count: int,
) -> None:
    """一次写出三维散点、雷达、热力图（文件名前缀如 表1_问题1）。"""
    if makespan_sec <= 0:
        return
    plot_3d_schedule_scatter(
        records,
        makespan_sec,
        out_dir / f"{file_prefix}_三维调度散点.png",
        title=f"{title_base} 三维调度散点",
    )
    plot_schedule_radar(
        records,
        expanded,
        makespan_sec,
        out_dir / f"{file_prefix}_雷达综合评价.png",
        title=f"{title_base} 雷达综合评价",
        total_device_count=total_device_count,
    )
    plot_device_time_heatmap(
        records,
        makespan_sec,
        out_dir / f"{file_prefix}_热力图设备时间占用.png",
        title=f"{title_base} 设备—时间热力图",
    )
