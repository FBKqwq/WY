# Q3 开发文档

## 1. 问题目标

- 使用**班组1和班组2**设备，完成 A、B、C、D、E 五个车间的全部整修任务。
- 建立调度模型，计算完成全部任务的**最短时长（Makespan，秒）**。
- 在表3中输出每台设备执行工序的：**序号、设备编号、起始时间、结束时间、持续工作时间(s)、工序编号、班组**。

依据：`src/Qbase/competitionB.md` 第八节「问题三逐句解析」及 B 题通用假设。

## 2. 数据输入

| 文件 | 路径 |
|------|------|
| 工序流程表 | `code/data/工序流程表.xlsx` |
| 班组配置表 | `code/data/班组配置表.xlsx` |
| 车间距离表 | `code/data/车间距离表.xlsx` |

## 3. 代码文件

| 文件 | 作用 |
|------|------|
| `src/Q3/solve_q3.py` | 问题3入口：读数据、筛 A-E、合并班组1+2设备；**默认 auto**：有 ortools 时跑 `solve_q3_cpsat`（与 Q2 同构三链 CP-SAT），否则贪心；`--solver greedy|cpsat|auto`、`--time-limit`、`--workers`；与贪心取较短 makespan；`plot_team_device_gantt` / `plot_workshop_team_bars`；`--no-charts` 仅跳过甘特图与车间柱状图；另默认导出 `schedule_charts` 分析图（设备作业按班组着色、利用率、工序完工、启用 CP 时的 Makespan 过程对比） |
| `src/Qbase/data_loader.py` | `load_operation_table` 等 Excel 加载 |
| `src/Qbase/parsers.py` | 工序/设备/距离解析，重复工序展开 |
| `src/Qbase/time_utils.py` | 作业时长、转运时间、时间格式、`detect_repeat_count` |
| `src/Qbase/scheduler_common.py` | `solve_problem3`：在 `solve_problem1` 上枚举**车间处理顺序全排列**（同 Q2），取最短 makespan 的贪心解 |
| `src/Qbase/cpsat_q2.py` | `solve_q3_cpsat`：设备池为班组1∪班组2，其余与 `solve_q2_cpsat` 三链一致；返回含三子链诊断元组（与 Q2 相同第 4 项）；虚拟首工序 dummy **仅挂入对应实体机槽位** `(dtype,k)`，避免跨槽位 pairwise 误查基地间距离 |
| `src/Qbase/schedule_charts.py` | 与 Q2 共用绘图函数；Q3 设备作业图按班组分色 |
| `src/Qbase/parsers.py` | `parse_distances`：对称距离矩阵（无 CP 专用补丁边） |
| `src/Qbase/validators.py` | `validate_schedule` 汇总校验 |
| `src/Qbase/exporter.py` | `export_schedule` 导出表3（`include_team=True`） |

## 4. 核心实现逻辑

- 解析并保留 `所属车间 in {A,B,C,D,E}` 的全部原始工序，按顺序与重复规则展开为 `ExpandedOperation`。
- 设备池为 **班组1 ∪ 班组2** 全部设备（`parse_devices` 后 `team in (1, 2)`），初始位置分别为 `班组1`、`班组2`；距离矩阵中两节点到各车间距离不同，**门限选机**时对每台机分别计算 `可用 + 转运`。
- 与 **Q1/Q2 相同**的 `solve_problem1` 内核：单设备类枚举 \(n\)、双设备类枚举 \((n_1,n_2)\)，`calc_work_duration(Q, n×单台效率, 单位)`；并联组在**同类型全池**上取门限最小的前 \(n\) 台，故同一道工序可混编两班组同型机（与 `ReferenceSolutionIdeas` §8.2a 及赛题未禁止混用的默认假设一致）。
- **贪心路径**：`solve_problem3` 枚举涉及车间的全排列作为外层车间块顺序，在每种顺序下调用 `solve_problem1`；仍为块内整链串行，弱于 CP-SAT 的跨车间并行。
- **CP-SAT 路径**（默认）：`solve_q3_cpsat` 以两班组并池设备建析取模型，与 `othersolve_3` 思路一致；工程上含虚拟首工序链、可变台数链及 legacy+repair，取校验通过的最短 makespan。
- 同车间连续作业转运时间为 0；跨节点按 `ceil(distance/speed)` 计入。

## 5. 重复工序处理

- 单个工序重复执行：展开为 `工序编号#1...#R`。
- 连续工序同重复次数场景：按“轮次×工序”交错展开，满足题目中重复执行约束。

## 6. 双设备工序处理

- 对每条设备需求分别产生设备任务，两类设备均完成该工序工程量；工序完成判定为该工序所有设备任务**结束时间的最大值**。
- 两类时长不同时，各台 `结束时间` 可不同；并联同类多台时各台 `持续时间` 为分摊后的单段作业时长。

## 7. 运输时间处理

- 距离由 `车间距离表.xlsx` 解析为对称矩阵。
- 同车间任务运输时间为0，跨节点运输时间按 `ceil(distance/speed)`。
- 输出表“持续工作时间(s)”仅记录设备作业时长，不包含运输时间。

## 8. 已实现约束

- [x] 工序顺序约束
- [x] 重复工序展开
- [x] 设备类型匹配
- [x] 双设备共同完成
- [x] 工序内同类多台并联（与 Q1、Q2 同 `solve_problem1` 路径）
- [x] 设备互斥
- [x] 跨车间运输时间
- [x] 问题3班组约束（班组1与班组2均可用，并池调度）
- [x] 表3字段约束（包含班组列）

## 9. 输出文件

| 输出 | 路径 |
|------|------|
| 表3 | `src/Q3/outputs/表3_问题3调度结果.xlsx`（含“表3_调度明细”“汇总”） |
| 校验报告 | `src/Q3/outputs/validation_report.txt` |
| 甘特图（班组1） | `src/Q3/outputs/表3_问题3甘特图_班组1.png`（`Qbase.gantt_dual`：上车间；下条工序 1–6 与重复轮次配色 + 运输段） |
| 甘特图（班组2） | `src/Q3/outputs/表3_问题3甘特图_班组2.png`（配色同班组1） |
| 车间统计柱状图 | `src/Q3/outputs/表3_问题3车间统计柱状图.png`（上：各车间最后完工时刻；下：两班组在该车间累计作业时间） |
| 各设备持续作业时间 | `src/Q3/outputs/表3_问题3各设备持续作业时间.png`（班组1/2 分色） |
| 各设备利用率 | `src/Q3/outputs/表3_问题3各设备利用率.png` |
| 各工序完工时刻 | `src/Q3/outputs/表3_问题3各工序完工时刻.png` |
| 求解过程 Makespan 对比 | `src/Q3/outputs/表3_问题3求解过程Makespan对比.png`（`--solver greedy` 时跳过） |
| 三维 / 雷达 / 热力 | `表3_问题3_三维调度散点.png`、`表3_问题3_雷达综合评价.png`、`表3_问题3_热力图设备时间占用.png` |

## 10. 运行方式

```bash
cd code
pip install -r requirements.txt
python src/Q3/solve_q3.py
# 不导出甘特图与车间柱状图（仍会导出 schedule_charts 分析图）：python src/Q3/solve_q3.py --no-charts
# 仅贪心：python src/Q3/solve_q3.py --solver greedy --no-charts
# CP-SAT 更长搜索：python src/Q3/solve_q3.py --time-limit 600
```

图表依赖 `matplotlib`（已在 `requirements.txt` 中）。导出表3前请勿用 Excel 独占打开 `表3_问题3调度结果.xlsx`，否则会写入失败。

运行时在控制台输出 `[问题3]` 前缀的分步进度（启动参数、读表、解析规模、贪心/CP-SAT、导出、校验、绘图），便于长耗时任务观察进程。

**默认 CP-SAT 资源**：与 Q2 共用 `Qbase.cpsat_q2` 的 `DEFAULT_CPSAT_TIME_LIMIT_SEC=300`、`DEFAULT_CPSAT_NUM_WORKERS=24`（针对 24 核级 CPU + 32G 内存）；可用 `--time-limit` / `--workers` 覆盖。

## 11. 验证结果

实跑后 `validation_report.txt` 中以下检查项应均通过：
重复工序展开、工序顺序、设备类型匹配、设备互斥、持续工作时间字段、运输时间抽查。

默认 CP-SAT 下 makespan 可与 `othersolve_3` 同量级（约 8.3×10⁴ s 级，随时限与数据略变）；贪心全排列为回退与对照。请以本机运行 `solve_q3.py` 后的终端输出与 `validation_report.txt` 为准。

## 12. 已知问题与建模要点

- **全局最优**：CP-SAT 在时限内可为 OPTIMAL/FEASIBLE，仍依赖 horizon 与建模近似；贪心为启发式。**不保证**超越附件真最优，但已显著优于固定车间字典序单链贪心。
- **工序内并联**：已通过 `solve_problem1` 与 Q1/Q2 对齐；两班组设备按**类型**并池枚举，勿在文档或二次开发中改回「每类单机」或「按班组割裂且无并联」叙述。
- **赛题若明确禁止**同工序混用两班组设备：须在调度层增加 `n_{j,k}^{(g)}` 约束或拆分求解，当前代码默认允许混用并联。

## 13. 下一步计划

- 需要更紧上界时可增大 `--time-limit` 或单独调 `solve_q3_cpsat` 的 horizon。
- 结合 `表3_问题3甘特图_班组*.png`、车间柱状图与「各设备持续作业时间」等分析图对照瓶颈设备与班组负载；`表3_问题3求解过程Makespan对比.png` 展示贪心与三 CP 子链的墙钟结果。

## 14. 修改记录

- 2026-05-01 18:35：新建 Q3 求解入口、输出导出与校验流程，新增 `solve_problem3` 接口。
- 2026-05-01 18:38：完成实跑验证，生成表3与校验报告并记录 makespan。
- 2026-05-02：对齐 `solve_problem1` 的同类并联与跨班并池语义；更新 §3–§4、§8、§11–§14；同步 `ReferenceSolutionIdeas` §8、`SKILL` 与 `solve_problem3` docstring。
- 2026-05-02：`solve_q3.py` 增加班组1/班组2设备甘特图（标注运输段）、各车间完工时刻与两班组累计作业柱状图；`--no-charts`；文档 §3、§9–§11、§13–§14 对齐。
- 2026-05-03：甘特图统一切换为 `Qbase.gantt_dual` 上下双拼（上车间、下工序）。
- 2026-05-03：`solve_problem3` 对齐 Q2 枚举车间全排列；`solve_q3_cpsat` + `solve_q3.py` 默认 CP-SAT。
- 2026-05-03：`cpsat_q2._q2_cp_model_ctx` 将每台设备的 dummy 区间只加入其实体槽位 `(dtype,k)`，从模型上去除「不同基地 dummy 出现在同一 kk 列表」导致的无效 pairwise 与对缺失「班组1—班组2」边的依赖；已移除 `parse_distances` 中的基地间虚拟补边。
- 2026-05-03：`solve_q3.py` 增加控制台分步进度日志（`[问题3]`，`flush=True`）。
- 2026-05-03：CP-SAT 默认 `--time-limit` / `--workers` 与 `cpsat_q2.DEFAULT_CPSAT_*` 对齐（300s、24 线程，参照 i9-13980HX + 32G）。
- 2026-05-03：`gantt_dual` 下条工序/轮次配色与 Q1–Q4 统一。
- 2026-05-03：`solve_q3_cpsat` 与 Q2 同步四元组返回；`solve_q3.py` 默认导出 `schedule_charts` 分析图（含按班组分色的设备作业图与 CP 子链 Makespan 对比）；`--no-charts` 不再跳过上述分析图。
- 2026-05-03：同 Q2 增补三维散点、雷达、热力图。
- 2026-05-03：三维散点图语义同 Q1 更新为工序瓶颈读图（`schedule_charts.plot_3d_schedule_scatter`）。
