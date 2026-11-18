# NLU 评测基线（修复前，2026-11-17 21:51）

- 用例：120 条（`eval_nlu_cases.json`，sha256 `821d6080e1a9bdcc`）
- 代码：`9655b4e`（**prompt 日期示例、城市笔误、订单状态默认值修复之前**）
- 模型：qwen-flash, qwen3.8-flash, deepseek-v4.1-flash
- 模式：`raw`（只解析模型 JSON）/ `pipeline`（项目真实链路，含规则补齐）

| 配置 | 意图 | 字段 | 全对 | JSON | P50 | P95 |
|---|---|---|---|---|---|---|
| rules | 89.2% | 86.0% | 71.7% | 100% | 0 ms | 0 ms |
| qwen-flash / raw | 99.2% | 90.8% | 88.3% | 100% | 764 ms | 2608 ms |
| qwen-flash / pipeline | 98.3% | 95.7% | 93.3% | 100% | 759 ms | 1108 ms |
| qwen3.8-flash / raw | 100% | 98.6% | 98.3% | 100% | 4133 ms | 12853 ms |
| qwen3.8-flash / pipeline | 100% | 98.6% | 98.3% | 100% | 4121 ms | 10080 ms |
| deepseek-v4.1-flash / raw | 100% | 98.1% | 97.5% | 100% | 1525 ms | 5074 ms |
| deepseek-v4.1-flash / pipeline | 100% | 98.6% | 98.3% | 100% | 1477 ms | 3898 ms |

难例子集（10 条，修复前）：

| 配置 | 意图 | 字段 | 全对 |
|---|---|---|---|
| rules | 70.0% | 82.1% | 40.0% |
| qwen-flash / raw | 100% | 67.9% | 50.0% |
| qwen-flash / pipeline | 100% | 89.3% | 80.0% |
| qwen3.8-flash / raw | 100% | 92.9% | 90.0% |
| qwen3.8-flash / pipeline | 100% | 89.3% | 80.0% |
| deepseek-v4.1-flash / raw | 100% | 89.3% | 80.0% |
| deepseek-v4.1-flash / pipeline | 100% | 89.3% | 80.0% |

## 修复后复跑

修复内容（prompt 日期示例随今天滚动、城市笔误纠正、`order_status` 默认 ALL、裸数字日期对规则、跨月/倒挂日期修正、改口/否定后处理）之后，用同一条命令复跑：

```powershell
python scripts/eval_nlu.py
```

新结果写到 `eval_nlu_report.md` / `eval_nlu_results.json`；本文件保留的是修复前的对照基线。
