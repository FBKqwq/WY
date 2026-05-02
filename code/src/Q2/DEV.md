# Q2 开发文档

## 1. 问题目标

- 仅使用**班组1**设备，完成 A、B、C、D、E 五个车间的全部整修任务。
- 建立调度模型，计算完成全部任务的**最短时长（Makespan，秒）**。
- 在表2中输出每台设备执行工序的：**序号、设备编号、起始时间、结束时间、持续工作时间(s)、工序编号**。

依据：`src/Qbase/competitionB.md` 第七节「问题二逐句解析」及 B 题通用假设。

## 2. 数据输入

| 文件 | 路径 |
|------|------|
| 工序流程表 | `code/data/工序流程表.xlsx` |
| 班组配置表 | `code/data/班组配置表.xlsx` |
| 车间距离表 | `code/data/车间距离表.xlsx` |

## 3. 代码文件

| 文件 | 作用 |
|------|------|
| `src/Q2/solve_q2.py` | 问题2入口：读数据、筛 A-E 与班组1；默认 `auto` 在已安装 ortools 时跑 CP-SAT 并与贪心取较短 makespan；`--solver` / `--time-limit` / `--workers`；默认导出甘特图（含运输段）与车间柱状图，`--no-gantt` 仅跳过二者；另默认导出设备作业/利用率/工序完工及（启用 CP 时）三子链 Makespan 对比图（`Qbase.schedule_charts`）；若目标表2 Excel 被占用，则写入 `_新结果.xlsx` 备用文件并继续校验 |
| `src/Qbase/data_loader.py` | `load_operation_table` 等 Excel 加载 |
| `src/Qbase/parsers.py` | 工序/设备/距离解析，重复工序展开 |
| `src/Qbase/time_utils.py` | 作业时长、转运时间、时间格式、`detect_repeat_count` |
| `src/Qbase/scheduler_common.py` | `solve_problem2`：默认枚举涉及车间的**全排列**作为 `solve_problem1` 的外层车间块顺序并取最短 makespan；`solve_problem1` 支持参数 `workshop_order` |
| `src/Qbase/cpsat_q2.py` | `solve_q2_cpsat` / `solve_q3_cpsat`：**三条** CP-SAT 两阶段链分预算串行跑，在通过 `validators` 全套检查的候选中取最短 makespan；返回第 4 元组为各子链 `(链标识, makespan, 状态串)` 供过程对比图；默认 `DEFAULT_CPSAT_NUM_WORKERS=24`、`DEFAULT_CPSAT_TIME_LIMIT_SEC=300`；Q2 优先 strict fixed/variable 链，legacy 链作为对照兜底；同一具体设备上的转运析取仅在两任务均分配到该设备时触发 |
| `src/Qbase/schedule_charts.py` | 设备作业横向条、利用率、工序完工时刻、贪心+CP 子链+最终采用的 Makespan 柱状对比 |
| `src/Qbase/validators.py` | `validate_schedule` 汇总校验 |
| `src/Qbase/exporter.py` | `export_schedule` 导出表2 |

## 4. 核心实现逻辑

- 解析并保留 `所属车间 in {A,B,C,D,E}` 的全部原始工序，按顺序与重复规则展开为 `ExpandedOperation`。
- 设备池仅保留**班组1**设备，初始位置为 `班组1`。
- **贪心基线**：与 Q1 相同的 `solve_problem1` 内核：单工序枚举 \(n\)、双工序枚举 \((n_1,n_2)\)，门限选机。问题 2 下 `solve_problem2` **默认**枚举「涉及车间」的 **\(5! = 120\)** 种外层块顺序（`itertools.permutations`），每次调用 `solve_problem1(..., workshop_order=perm)`，取 makespan 最小者，避免原先固定字典序「A→B→…」造成的块顺序遗漏；单车间时与 Q1 等价无额外开销。全局仍为启发式（块内仍是「整车间工序链串行」后再换下一车间块）。
- **CP-SAT（推荐）**：`cpsat_q2.solve_q2_cpsat` 在 A–E 全工序与班组1 设备池上联合优化工序开始、设备分配与顺序；设备互斥为 `AddNoOverlap` 可选区间。内部串行跑 **三条** 两阶段链（阶段1 最小化 makespan，阶段2 固定 makespan 后最小化真实工序开工和并带 hint），Q2 总时间预算优先给 strict 链：`fixed` 约 **42%**（下限 30s）、`variable` 约 **38%**（下限 30s）、`legacy` 为**剩余**（下限 20s），见 `cpsat_q2.solve_q2_cpsat`：
  - **legacy_repaired**：与 `othersolve.py` 析取模型同构——**无**每台设备零时长虚拟首工序；车间之间转运弧与参考脚本一致，为 **\(\lceil d/2\rceil\)** 秒（与附件 2 m/s、整数米距一致）；CP 解出后调用 `_repair_depot_timeline`，在保持设备链顺序与工序时长不变的前提下整体前推开工，使与 `validate_transport`（班组初始位置 + `ceil(distance/speed)`）一致。
  - **fixed_alloc**：每台设备有虚拟首工序（位于班组初始节点），车间间弧用设备速度与距离矩阵；每道工序并联台数与阶段时长由 `_isolated_best_alloc` 离线固定（与 `othersolve` 的孤立最优台数假设一致），CP 只排机号与顺序。
  - **variable_n**：同样带虚拟首工序与按速度的转运弧，但在 CP 内用 `AddElement` 决策并联台数 \(n\)（及双设备两侧台数），可行域更宽。
  - **转运析取修正**：同一具体设备槽位上的两道候选任务，只有在二者均被分配到该设备时，才由顺序布尔量触发 `start_next >= done_prev + travel`；不再对未共同分配的候选任务施加无条件先后关系，避免同型多机被误串行。
  - **取优规则**：若传入 `raw_repeat_map`（`solve_q2.py` 已传入），则仅在 **六项校验均无报错** 的候选中取最短 makespan；相同时优先 `fixed_alloc`，其次 `variable_n`，再 `legacy_repaired`；若过滤后为空则退回未过滤池（避免无输出）。最后与贪心比较，导出 **较短 makespan** 对应调度。
- 同车间连续作业转运时间为 0；工程校验与 strict 模型弧上跨节点为 `ceil(distance/speed)`（当前数据下班组1 设备速度均为 2 m/s 时与 \(\lceil d/2\rceil\) 一致）。

## 5. 重复工序处理

- 单个工序重复执行：展开为 `工序编号#1...#R`。
- 连续工序同重复次数场景：按“轮次×工序”交错展开，满足题目中重复执行约束。

## 6. 双设备工序处理

- 对每条设备需求分别产生设备任务，且两类设备都需完成同一工程量。
- 工序完成判定时间为该工序所有设备任务结束时间的最大值。

## 7. 运输时间处理

- 距离由 `车间距离表.xlsx` 解析为对称矩阵。
- 同车间任务运输时间为0，跨节点运输时间按 `ceil(distance/speed)`。
- 输出表“持续工作时间(s)”仅记录设备作业时长，不包含运输时间。

## 8. 已实现约束

- [x] 工序顺序约束
- [x] 重复工序展开
- [x] 设备类型匹配
- [x] 双设备共同完成
- [x] 工序内同类多台并联（与 Q1 同实现路径）
- [x] 设备互斥
- [x] 跨车间运输时间
- [x] 问题2班组约束（仅班组1）

## 9. 输出文件

| 输出 | 路径 |
|------|------|
| 表2 | `src/Q2/outputs/表2_问题2调度结果.xlsx`（含“表2_调度明细”“汇总”） |
| 校验报告 | `src/Q2/outputs/validation_report.txt` |
| 设备甘特图 | `src/Q2/outputs/表2_问题2甘特图.png`（`Qbase.gantt_dual`：上条车间色；下条工序 1–6 固定色，重复条左右「工序｜轮次」；运输段斜线；需 `matplotlib`） |
| 车间统计柱状图 | `src/Q2/outputs/表2_问题2车间统计柱状图.png`（上：各车间最后完工时刻；下：班组1在该车间累计作业时间） |
| 各设备持续作业时间 | `src/Q2/outputs/表2_问题2各设备持续作业时间.png` |
| 各设备利用率 | `src/Q2/outputs/表2_问题2各设备利用率.png` |
| 各工序完工时刻 | `src/Q2/outputs/表2_问题2各工序完工时刻.png` |
| 求解过程 Makespan 对比 | `src/Q2/outputs/表2_问题2求解过程Makespan对比.png`（`--solver greedy` 或未跑 CP 时不生成） |
| 三维 / 雷达 / 热力 | `表2_问题2_三维调度散点.png`、`表2_问题2_雷达综合评价.png`、`表2_问题2_热力图设备时间占用.png` |

## 10. 运行方式

```bash
cd code
pip install -r requirements.txt
python src/Q2/solve_q2.py
# 仅贪心：python src/Q2/solve_q2.py --solver greedy
# 延长 CP 搜索：python src/Q2/solve_q2.py --time-limit 300
# 指定并行线程：python src/Q2/solve_q2.py --workers 8
# 不画甘特图与车间柱状图（仍会导出 schedule_charts 分析图）：python src/Q2/solve_q2.py --no-gantt
```

运行时在控制台输出 `[问题2]` 前缀的分步进度（启动参数、读表、解析规模、贪心/CP-SAT、导出、校验、绘图），便于长耗时任务观察进程。

**默认 CP-SAT 资源**（与 `Qbase.cpsat_q2` 中 `DEFAULT_CPSAT_*` 一致，按 i9-13980HX 级 24 物理核 + 32G 内存调参）：`--time-limit` 默认 **300** s（三链分预算总墙钟约等于该值），`--workers` 默认 **24**。核数较少或笔记本省电时可自行改小。

## 11. 验证结果

最近一次实跑示例（`--solver cpsat`，CP-SAT 时间上限 300s，`--workers 24`，`--no-gantt`，与贪心仍取优）：

- Makespan = **144880 s**（**40:14:40**），CP 状态串为 `fixed_alloc::OPTIMAL+compress_OPTIMAL`；同次三条池 makespan 为 `[144880, 144880, 144880]`，在通过全套校验的候选中取最短。该结果已低于外部参考的 **159574 s**。
- `src/Q2/outputs/validation_report.txt` 中以下检查项均通过：
  重复工序展开、工序顺序、设备类型匹配、设备互斥、持续工作时间字段、运输时间抽查。
- 本次运行时原目标 Excel 被系统占用，程序自动导出到 `src/Q2/outputs/表2_问题2调度结果_新结果.xlsx`，并继续生成校验报告。

## 12. 已知问题与建模要点

- **全局最优**：每条 CP 链在给定子时间预算内可对**该链对应模型**给出 OPTIMAL/FEASIBLE；三链合并后取校验通过的最短 makespan，不保证跨模型统一全局最优。当前 Q2 实跑三链均为 **144880 s**，且 strict fixed 链已报 `OPTIMAL+compress_OPTIMAL`。
- **legacy 前推修复**：按「车间前驱已全部处理」的拓扑候选中选取当前最小 CP 开工工序迭代推进；在工序图主要为车间链、且设备顺序与 CP 时间轴一致时可保持设备链顺序；若将来改数据导致异常，应检查校验报告或改为按设备时间线全局重排。
- **贪心**：多车间顺序仍为全排列启发式；与 CP 取较短导出。
- **工序内并联**：贪心路径与 Q1 对齐；CP 的 fixed 链与 `_isolated_best_alloc` 对齐；若修改调度主循环，须保留「枚举台数 + 逐台导出」，避免回退为单机模型。

## 13. 下一步计划

- 视需要增大 `--time-limit` 或调整 `horizon` 以继续压缩 makespan；三链子预算比例如需调优可在 `cpsat_q2.solve_q2_cpsat` 内修改。
- 结合 `表2_问题2甘特图.png`、`表2_问题2车间统计柱状图.png` 与 `表2_问题2各设备持续作业时间.png` 等分析瓶颈设备、运输空档与各车间完工节奏；`表2_问题2求解过程Makespan对比.png` 对照贪心与三条 CP 子链的墙钟结果。
- 若需要覆盖正式表2文件，请先关闭正在占用 `表2_问题2调度结果.xlsx` 的 Excel/WPS 窗口后重新运行。

## 14. 修改记录

- 2026-05-01 18:00：新建 Q2 求解入口、输出导出与校验流程。
- 2026-05-01 18:10：完成实跑验证，生成表2与校验报告并记录 makespan。
- 2026-05-02：文档对齐 `solve_problem1` 的同类并联语义；更新 §4、§11–§12 与 Makespan；与 `ReferenceSolutionIdeas` §7、`SKILL` 中问题2表述一致。
- 2026-05-02：新增 `Qbase/cpsat_q2.py`（CP-SAT + 可变并联台数 + 虚拟首工序）；`solve_q2.py` 默认与贪心取较短 makespan；`requirements.txt` 增加 `ortools`。
- 2026-05-02：`solve_problem1` 增加 `workshop_order`；`solve_problem2` 默认枚举车间全排列取最优贪心，排除固定车间块顺序带来的遗漏。
- 2026-05-02：`cpsat_q2` 增加与 `othersolve` 同构的 **legacy** 链及 `_repair_depot_timeline`；`solve_q2_cpsat` 三链（legacy / fixed / variable）+ `raw_repeat_map` 校验取优；`solve_q2.py` 支持 `--workers`、`--no-gantt` 与默认甘特图导出；文档 §3–§4、§9–§13 对齐。
- 2026-05-02：`solve_q2.py` 甘特图补充运输段展示；新增 `plot_workshop_bars_q2` 导出车间统计柱状图；`--no-gantt` 同时跳过两图；更新 §3、§9–§10、§13–§14。
- 2026-05-03：设备甘特图改为 `Qbase.gantt_dual` 上下双拼（上车间、下工序配色）。
- 2026-05-03：`solve_q2.py` 增加控制台分步进度日志（`[问题2]`，`flush=True`）。
- 2026-05-03：`cpsat_q2` 增加 `DEFAULT_CPSAT_NUM_WORKERS=24`、`DEFAULT_CPSAT_TIME_LIMIT_SEC=300`；`solve_q2.py` / `solve_q3.py` 与 `solve_q2_cpsat` / `solve_q3_cpsat` 默认 CP 参数与之对齐（参照 i9-13980HX + 32G）。
- 2026-05-03：`gantt_dual` 甘特下条统一工序 1–6 与重复轮次配色（与 Q1/Q3/Q4 共用）。
- 2026-05-03：`gantt_dual` 按运输段起点预扩展横轴左边界并支持负刻度标签，首单 depot 转运条按真实 `[start-tr,start]` 宽度绘制（与工业清洗机等一致，避免灌装机/输送臂首条被裁成细线或留白）。
- 2026-05-03：重构 Q2 CP-SAT 设备转运析取约束，仅在两任务共同分配到同一具体设备时触发顺序与转运；Q2 搜索预算与同值取优改为优先 strict fixed/variable 链；实跑得到 **144880 s**，六项校验通过；`solve_q2.py` 增加目标 Excel 被占用时的备用导出文件。
- 2026-05-03：`solve_q2_cpsat` 增加第 4 返回值（三子链 makespan 与状态）；新增 `Qbase.schedule_charts` 并默认导出设备作业/利用率/工序完工及 CP 路径下的求解过程对比图；`--no-gantt` 不再跳过上述分析图。
- 2026-05-03：`schedule_charts.plot_advanced_viz_bundle` 增补三维散点、雷达、设备—时间热力图。
- 2026-05-03：`plot_3d_schedule_scatter` 改为工序瓶颈视角（阶段墙钟跨度、关键度色条、车间描边、Top 工序），与 Q1/Q3/Q4 共用。
