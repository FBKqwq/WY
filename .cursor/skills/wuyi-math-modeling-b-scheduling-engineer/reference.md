# 五一数学建模B题工程化代码实现专家 — 参考模板与数据结构

## 开发前检查摘要（每次写代码前输出）

```markdown
## 开发前检查摘要

当前开发问题：Qx

已重读文档：
- src/Qbase/xxx
- src/Qx/DEV.md

当前问题目标：
- ...

当前需要读取的数据：
- data/工序流程表.xlsx
- data/班组配置表.xlsx
- data/车间距离表.xlsx

当前关键约束：
- 工序顺序约束
- 设备类型匹配
- 重复工序展开
- 双设备共同完成
- 设备互斥
- 跨车间运输时间
- 当前问题特有约束

本次代码目标：
- ...
```

## 开发文档更新块（每次写代码后输出或写入 DEV.md）

```markdown
## 需要写入 src/Qx/DEV.md 的更新内容

# Qx 开发文档

## 最近更新时间
YYYY-MM-DD HH:MM

## 当前问题目标
...

## 本次实现内容
...

## 输入文件
...

## 输出文件
...

## 核心算法
...

## 已实现约束
- [x] 工序顺序约束
- [x] 设备类型匹配
- [x] 重复工序展开
- [x] 双设备共同完成
- [x] 设备互斥
- [x] 跨车间运输时间

## 未完成或待优化
...

## 运行方式
python src/Qx/solve_qx.py

## 输出检查
...

## 下一步计划
...
```

## DEV.md 长期结构（每次修改 Qx 代码后维护）

```markdown
# Qx 开发文档

## 1. 问题目标
说明当前问题要求完成什么。

## 2. 数据输入
列出当前使用的数据文件。

## 3. 代码文件
列出当前目录下的主要代码文件及作用。

## 4. 核心实现逻辑
说明当前采用的调度算法。

## 5. 重复工序处理
说明如何识别 repeat_count，如何展开。

## 6. 双设备工序处理
说明如何拆分设备任务。

## 7. 运输时间处理
说明如何计算跨车间运输时间。

## 8. 已实现约束
- [x] 工序顺序
- [x] 重复工序展开
- [x] 设备类型匹配
- [x] 双设备共同完成
- [x] 设备互斥
- [x] 跨车间运输时间

## 9. 输出文件
列出输出文件路径。

## 10. 运行方式
python src/Qx/solve_qx.py

## 11. 验证结果
记录最近一次验证结果。

## 12. 已知问题
记录还没解决的问题。

## 13. 下一步计划
记录后续要优化的内容。

## 14. 修改记录
- YYYY-MM-DD HH:MM：完成 xxx。
```

## Internal Data Structures

### Raw Operation

```json
{
    "raw_id": "A2",
    "workshop": "A",
    "order": 2,
    "quantity": 300,
    "repeat_count": 3,
    "requirements": [
        {
            "device_type": "工业清洗机",
            "efficiency": 20,
            "efficiency_unit": "m/h"
        }
    ]
}
```

### Expanded Operation

```json
{
    "op_id": "A2#1",
    "raw_id": "A2",
    "repeat_index": 1,
    "repeat_count": 3,
    "workshop": "A",
    "sequence_index": 2.1,
    "quantity": 300,
    "requirements": []
}
```

### Device

```json
{
    "team": 1,
    "device_id": "B1-001",
    "device_type": "工业清洗机",
    "speed": 1.5,
    "price": 80000,
    "initial_location": "班组1"
}
```

### Schedule Record

```json
{
    "seq": 1,
    "device_id": "B1-001",
    "team": 1,
    "device_type": "工业清洗机",
    "workshop": "A",
    "raw_id": "A2",
    "op_id": "A2#1",
    "repeat_index": 1,
    "start_sec": 0,
    "end_sec": 3600,
    "duration_sec": 3600,
    "start_time": "00:00:00",
    "end_time": "01:00:00",
    "transport_sec": 0
}
```
