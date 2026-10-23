# 聚合风险报告里的资产身份

投研值守人员在月末切换账簿时发现，风险系统只保留了若干组合的因子风险汇总，资产代码和因子载荷的对照表却在旧库下线时丢失。复核窗口只有一班，既要恢复每个资产对三个因子的载荷，也要在资金和敞口纪律内给出可执行的整数持仓。每完成一次组合构建，还要在复核流水里留下可重放的摘要。

程序入口是 `/app/factor_book.py`，研究台按下面的参数调用：

```text
python3 /app/factor_book.py --scenario <情景 JSON> --journal <复核流水 JSONL> --report <组合报告 JSON>
```

因子计算采用镜像自带的 Python 标准库，不引入额外运行时。`--inspect` 作为只读检查入口，结束时返回 0。

## 聚合风险材料

根对象读取八个字段：`assets`、`capital`、`case_id`、`factor_bounds`、`factors`、`observations`、`schema_version`、`unit`。

| 字段 | 读法 |
| --- | --- |
| `schema_version` | 整数 1 |
| `unit` | 字符串 `"currency"` |
| `case_id` | 包含账簿段和月份段的非空编号 |
| `capital` | 1 到 100 的整数 |
| `factors` | 三个互不相同的非空名称 |
| `factor_bounds` | 每个因子对应一对有限数，左端不大于右端 |

资产条目只含 `id`、`max_units`、`price`、`return`；清单长度 4 到 8。编号不可重复，`max_units` 取 1 到 4 的整数，价格为正有限数，收益为有限数。

复核输入条目只含 `factor_risk`、`observation`、`units`；清单长度 6 到 12。观察编号不可重复。单位向量的键与资产对齐且值为 0 到 4 的整数，因子风险的键与因子对齐且值为有限数。

资产载荷没有直接交付。每个因子的载荷都必须由全部聚合组合的资产单位向量联合最小二乘反演；只查看一个组合或把单位数当成载荷都不成立。观测矩阵秩不足、载荷非有限或任一资产的载荷全为零时应拒绝。

## 组合与复核流水

组合选择使用非负整数持仓，单资产不超过 `max_units`。总成本不得超过 `capital`，每个因子的组合敞口必须落在该因子的闭区间内。目标是在这些条件成立时最大化 `sum(units × return)`。最优目标并列时，按资产顺序比较持仓向量，取字典序最小的一组。

组合产物为 UTF-8 JSON，字段分布如下：

| 层级 | 字段 |
| --- | --- |
| 根层 | `allocations`、`case_id`、`checksum`、`exposure`、`factor_loadings`、`journal_entry`、`objective`、`schema_version`、`solver_nodes`、`spend` |
| 持仓 | `allocations` 覆盖全部资产，每项是对应整数持仓 |
| 载荷 | `factor_loadings` 外层覆盖资产，内层覆盖因子 |
| 敞口 | `exposure` 覆盖全部因子 |

复核流水条目固定携带 `case_id`、`input_sha256`、`report_sha256`、`status` 四个字段，状态写 `"completed"`。`report_sha256` 是剔除 `journal_entry` 后对报告紧凑字节计算的校验摘要。

同一情景和同一报告字节的复核不会新增流水；输入摘要变化时才追加。校验失败、载荷反演退化或不存在满足资金与敞口约束的组合时，不改变旧报告和流水，也不留下临时文件。

成功路径不打印日志，标准输出和标准错误均为空，退出码为 0。失败收口使用退出码 3；标准输出不写字，标准错误一行以 `FINANCE-CASE-REFUSED: ` 开头。报告按键字典序、无空格，并以单个换行结束。常规规模的一次组合构建，3 秒内应完成。
