# Q4 开发文档

## 1. 问题目标

- 在新增设备预算不超过 `500000` 元约束下，同时确定设备购买方案与作业调度策略。
- 使用班组1和班组2设备完成 A、B、C、D、E 五个车间全部整修任务，最小化总完工时长（Makespan）。
- 输出：
  - 表4：调度明细（序号、设备编号、起始时间、结束时间、持续工作时间(s)、工序编号、班组）
  - 表5：购买方案（设备名称、班组1购买台数、班组2购买台数、购买总费用）

依据：`src/Qbase/competitionB.md` 第九节“问题四逐句解析”。

## 2. 数据输入路径

| 文件 | 路径 |
|------|------|
| 工序流程表 | `code/data/工序流程表.xlsx` |
| 班组配置表 | `code/data/班组配置表.xlsx` |
| 车间距离表 | `code/data/车间距离表.xlsx` |

## 3. 代码文件说明

| 文件 | 作用 |
|------|------|
| `src/Q4/solve_q4.py` | 问题4入口：读取数据、搜索购买方案、执行调度、导出表4/表5、校验报告；绘制购买前/后甘特图、购买后车间柱状图、购买前后对比图，以及检索过程折线/爬山轨迹/候选 Makespan 分布等论文辅助图（matplotlib）；另调用 `Qbase.schedule_charts.plot_advanced_viz_bundle` 输出购买前/后各一套三维散点、雷达、热力图；默认 CP-SAT 主路径，支持 `--no-cpsat` |
| `src/Qbase/parsers.py` | 解析工序/设备/距离，展开重复工序 |
| `src/Qbase/scheduler_common.py` | `solve_problem4`：调用 `solve_problem3`（车间顺序全排列 + `solve_problem1` 并联枚举） |
| `src/Qbase/cpsat_q4.py` | 问题4 CP-SAT 内层：`solve_q4_scheduling_cpsat`；增购设备保留所属班组与驻点，不跨班转用；同一道工序允许班组1/2同型设备同时并联参与 |
| `src/Qbase/exporter.py` | `export_schedule` 与 `export_purchase_plan` |
| `src/Qbase/validators.py` | `validate_schedule` 与 `validate_budget` |
| `src/Qbase/time_utils.py` | 秒级时间计算、向上取整与格式化 |

## 4. 核心算法

### 4.1 调度求解（内层）

- **`solve_problem4`（启发式后备）**：与 **问题 3** 一致，调用 **`solve_problem3` → `solve_problem1`**（`enumerate_workshop_orders=True`）：对涉及车间的处理顺序做全排列，取最短贪心 Makespan；工序内同类并联与双设备 \((n_1,n_2)\) 枚举与 Q1–Q3 相同。
- **`Qbase.cpsat_q4.solve_q4_scheduling_cpsat`（默认主路径内层）**：在固定设备池下建立 OR-Tools **CP-SAT** 析取模型，跨车间联合优化工序开工顺序与设备分配，并补齐「设备从班组驻点到首次作业车间」的转运下界（`OnlyEnforceIf(assign)`）。购买设备按表5归属班组1或班组2，设备编号、班组、初始驻点均不跨班转用；但同一道工序允许两班组同型设备同时参与并联，工序阶段时长仍按总投入台数计算。
- **增购的意义**：扩大某类型总台数 \(N\)，使 `compute_op_alloc` 中并联上界变大，阶段 \(\lceil Q/(n\cdot v)\rceil\) 可缩短；CP-SAT 再在该资源池上求近最优排程。设备调度时按具体设备计算转运与互斥，购买给某班组的设备始终以该班组为所属班组输出。

### 4.2 购买方案搜索

- **默认（已安装 `ortools`）**：**CP-SAT 主路径** — 生成与 `othersolve_4` 类似的瓶颈导向购置候选（抛光机+传感机枚举、班组分配策略、剩余预算填其他类），去重后对每个方案先用 `solve_problem4` 贪心预筛，再对预筛前列方案用 CP-SAT 限时精化，最后 **爬山加购**；命令行 **`--no-cpsat`** 可关闭 CP-SAT，仅走下列启发式全搜索。
- **启发式后备（`--no-cpsat` 或无 ortools）**：
  - **有界完全枚举**：在「增购总台数 ≤ 上界」且总价不超过预算的前提下，DFS 遍历购买向量，叶节点调度评估。
  - **束搜索**、**多起点贪心**、**爬山加购**：同前版逻辑。

说明：CP-SAT 单方案时限越长，Makespan 往往越接近全局最优（与 Jupyter 版 60s/方案 可比）；默认在运行时间与质量间折中。若最优方案花满预算，表 5 总费用可等于 50 万。

## 5. 已处理约束

- [x] 工序顺序固定（按编号顺序，含重复展开后顺序）
- [x] 重复工序展开（含“重复3次”语义）
- [x] 双设备共同完成（两设备均完成后工序完成）
- [x] 工序内同类多台并联（与 Q1–Q3 同 `solve_problem1`）
- [x] 设备类型匹配
- [x] 设备互斥
- [x] 跨车间运输时间约束
- [x] 持续工作时间(s)不含运输时间
- [x] 预算约束（购买总费用不超过500000）
- [x] 增购设备班组归属约束（不跨班转用；同工序可双班组同时参与）

## 6. 输出路径

| 输出 | 路径 |
|------|------|
| 表4 | `src/Q4/outputs/表4_问题4调度结果.xlsx` |
| 表5 | `src/Q4/outputs/表5_问题4购买方案.xlsx` |
| 校验报告 | `src/Q4/outputs/validation_report.txt` |
| 甘特图（零增购） | `src/Q4/outputs/表4_问题4甘特图_购买前.png`（`Qbase.gantt_dual` 密集排版：上车间；下条工序 1–6、重复轮次与班组标注；设备名缩写、图例外置） |
| 甘特图（最优购买后） | `src/Q4/outputs/表4_问题4甘特图_购买后.png`（配色同前，使用 Q4 密集排版以减少文字重叠） |
| 车间柱状图（购买后） | `src/Q4/outputs/表4_问题4车间统计柱状图_购买后.png` |
| 购买前后对比 | `src/Q4/outputs/表4_问题4购买前后对比.png` |
| 最优购置方案检索图 | `src/Q4/outputs/表4_问题4最优购置方案检索图.png`（展示班组1/2购置台数、费用构成、预算占用、购买前后工期收益） |
| 外层搜索流程图 | `src/Q4/outputs/表4_问题4外层搜索流程图.png`（展示候选生成、贪心预筛、CP-SAT精化、爬山加购、最终抛光、输出校验的算法逻辑） |
| 检索过程折线图 | `src/Q4/outputs/表4_问题4检索过程折线图.png`（上：贪心预筛排序 Makespan 与前缀最小下界；下：CP-SAT 精化逐档“贪心值 vs 精化值”，标出触发改进档；**图例外置到坐标区右侧**并收紧 `tight_layout` 右边界，避免图例遮挡折线） |
| 爬山加购轨迹图 | `src/Q4/outputs/表4_问题4爬山加购轨迹图.png`（双轴：接受序列上的 Makespan 与累计购置费用） |
| 候选购置 Makespan 箱图 | `src/Q4/outputs/表4_问题4候选购置Makespan箱图.png`（贪心预筛候选集分布；样本过少时自动降级为散点） |
| 全局最优演进图 | `src/Q4/outputs/表4_问题4全局最优演进图.png`（`global_best_curve`：贪心 Top1 → CP-SAT 接受改进 → 爬山 → 抛光的 Makespan 阶梯折线） |
| 启发式阶段折线图 | `src/Q4/outputs/表4_问题4启发式阶段检索折线图.png`（仅 `--no-cpsat`：DFS/束搜索/多起点贪心/爬山阶段最优） |
| 三维 / 雷达 / 热力（购买后） | `表4_问题4购买后_三维调度散点.png`、`表4_问题4购买后_雷达综合评价.png`、`表4_问题4购买后_热力图设备时间占用.png` |
| 三维 / 雷达 / 热力（零增购基线） | 文件名前缀 `表4_问题4购买前_` 与上栏三后缀相同（与 `--no-gantt` 无关，求解结束默认写出） |

## 7. 运行方式

```bash
cd code
pip install -r requirements.txt
python src/Q4/solve_q4.py
# 跳过 PNG（仅导出表与校验）：
python src/Q4/solve_q4.py --no-gantt
# 禁用 CP-SAT，仅启发式购买搜索 + solve_problem4：
python src/Q4/solve_q4.py --no-cpsat
# 一键极参（推荐；与单项 --cpsat-* 不要混用，preset 会覆盖）：
python src/Q4/solve_q4.py --preset maximum --no-gantt
# 中等加强：
python src/Q4/solve_q4.py --preset strong --no-gantt
```

`--preset maximum` 对应极参（见 `solve_q4.PRESET_Q4["maximum"]`）：`cpsat_refine_sec=180`、`cpsat_refine_top=48`、`greedy_pre_rank=96`、`bottle_max_per_type=10`、`final_polish_sec=360`、`second_final_polish_sec=180`、`cpsat_workers=24`（与 Q3 CP-SAT 默认线程一致，且不超过 32）。

另可调：`--cpsat-refine-sec`、`--cpsat-refine-top`、`--greedy-pre-rank`、`--bottle-max-per-type`、`--final-polish-sec`、`--second-final-polish-sec`（0 关第二轮）、`--cpsat-workers`（`--preset balanced` 时逐项生效）。

## 8. 已知问题与建模要点

- **历史差异（已修）**：此前 `solve_problem4` 仅调用 `solve_problem1` 且车间顺序固定为字典序，**弱于** Q3 的「车间全排列」外层，会高估 Makespan；现已改为与 Q3 一致的 `solve_problem3` 封装。
- **CP-SAT 与贪心**：默认主路径用 CP-SAT 在候选购置下求近最优排程；`--no-cpsat` 路径仍为启发式购买搜索 + `solve_problem4`。
- **工序内并联**：CP-SAT 侧按全局台数 `compute_op_alloc` 固定每工序最优并联台数（与赛题并联规则一致）；贪心侧仍为 `solve_problem1` 内枚举。
- **班组口径**：当前口径为「设备不跨班转用，但工序可跨班组协同」。即表5买给班组1/2的设备只保留对应班组编号和驻点；同一道工序可以同时选择班组1和班组2的同型设备并联。
- **进一步压缩工期**：优先调命令行（见 §7）；或改 `solve_q4.py` 顶部常量；瓶颈候选已含班组 1/2 偏置各一种分配；末段「最终 CP-SAT 抛光」在已定购置上再加时限求解。
- **甘特图排版**：Q4 甘特图设备行多、增购设备名长，已在 `gantt_dual` 中启用 `dense_layout`：缩短 y 轴设备名、提高条内文字显示门槛、强制单行短标签、隐藏极窄运输秒数、将图例移至图外。
- **论文辅助图**：除 `最优购置方案检索图`（结果解释）与 `外层搜索流程图`（方法解释）外，另输出检索遥测图：`检索过程折线图`（图例外置避免压线）、`爬山加购轨迹图`、`候选购置Makespan箱图`、`全局最优演进图`（默认 CP-SAT 主路径）；`--no-cpsat` 时输出 `启发式阶段检索折线图`。

## 9. 下一步计划

- 增加邻域搜索（加购/减购/换购）以进一步探索近优方案；
- 在可控规模下增加 CP-SAT 精化模型做对比验证。

## 10. 最近一次修改记录

- 2026-05-01 18:45：新建 `Q4/solve_q4.py`，实现预算约束下的购买+调度联合求解流程。
- 2026-05-01 18:45：新增表5导出函数 `export_purchase_plan`，补充预算校验函数 `validate_budget`，新增 `solve_problem4` 接口。
- 2026-05-02：购买搜索改为有界枚举 + 束搜索 + 多起点贪心 + 爬山；去掉全量叶节点缓存以修复内存/耗时问题；枚举上界可调以平衡时间。
- 2026-05-02：文档对齐内层 `solve_problem4`→`solve_problem1` 的**工序内同类并联**语义；更新 §4.1、§5、§8 与 `ReferenceSolutionIdeas` §9、`SKILL`。
- 2026-05-02：`solve_q4.py` 代码整理——统一仅使用 `solve_problem4`（基线零采购与 `_evaluate_plan` 一致）；提取购买搜索常量；完善 `_evaluate_plan`、`_utilization_by_type` 注释。
- 2026-05-03：`_search_purchase_plan` 同步返回「零增购」基线调度与 Makespan；新增购买前/后设备甘特图、购买后各车间完工与双班组作业量柱状图、购买前后 Makespan 与各车间最后完工对比图；`validation_report` 增加购买前后工期摘要；支持 `--no-gantt`。
- 2026-05-03：控制台进度日志：统一前缀 `[问题4 HH:MM:SS +累计秒]`，覆盖读数、基线调度、DFS 叶节点节流、束搜索每层、多起点贪心变体、爬山接受步、导出与绘图；便于长耗时运行判断是否卡死。
- 2026-05-03：购买前/后甘特图改为 `Qbase.gantt_dual` 上下双拼（上车间、下工序+班组）。
- 2026-05-03：`gantt_dual` 下条工序/轮次配色与 Q1–Q3 统一。
- 2026-05-03：`solve_problem4` 对齐 Q3（车间全排列）；新增 `Qbase/cpsat_q4.py` 与默认 CP-SAT 购置主路径（瓶颈候选 + 贪心预筛 + CP-SAT 精化 + 爬山），`--no-cpsat` 回退原启发式搜索。
- 2026-05-03：瓶颈购置增加「班组 2 多 1 台」分配策略；默认略增大预筛与枚举上界；增加最终 CP-SAT 抛光与命令行调参（`--cpsat-refine-sec` 等）。
- 2026-05-03：`--preset balanced|strong|maximum` 一键调参；`maximum` 为极参 + 第二轮抛光；`--second-final-polish-sec`。
- 2026-05-03：按「增购设备不跨班转用、同工序允许两班组设备同时进行」重构 `cpsat_q4` 内层：移除单工序只能选一个班组的限制，恢复按同型设备总池并联选机；同一具体设备的转运析取仅在两任务均分配到该设备时触发。快速验证命令通过，购买后 Makespan = **36795 s**，预算与调度校验均通过。
- 2026-05-03：优化 Q4 甘特图文字排版：`plot_device_gantt_dual_strip` 新增 `dense_layout`，Q4 调用启用密集排版；重新生成 `表4_问题4甘特图_购买前.png`、`表4_问题4甘特图_购买后.png`，减少设备名、图例、条内工序文字的错位重叠。
- 2026-05-03：新增论文辅助图 `表4_问题4最优购置方案检索图.png` 与 `表4_问题4外层搜索流程图.png`，分别解释最优购置组合/预算收益和外层搜索—内层调度精化流程；快速参数重跑后图表生成成功，校验报告仍通过。
- 2026-05-03：在 CP-SAT 主路径记录检索遥测（贪心排序序列、CP-SAT 精化逐档结果、爬山接受序列），新增输出 `表4_问题4检索过程折线图.png`、`表4_问题4爬山加购轨迹图.png`、`表4_问题4候选购置Makespan箱图.png`；`--no-cpsat` 输出 `表4_问题4启发式阶段检索折线图.png`。
- 2026-05-03：修复 `表4_问题4检索过程折线图.png` 图例遮挡曲线（图例 `bbox_to_anchor` 外置 + `tight_layout(rect=…)` 右侧留白）；爬山轨迹图图例外置；补充 `表4_问题4全局最优演进图.png`。
- 2026-05-03：调用 `schedule_charts.plot_advanced_viz_bundle`，对零增购基线与购买后最优调度各导出三维散点、雷达综合评价、设备—时间热力图（与 `--no-gantt` 无关）。
- 2026-05-03：购买前/后三维散点同步采用工序瓶颈视角（阶段墙钟跨度、关键度、车间描边、Top 工序）。
