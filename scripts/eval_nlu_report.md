# NLU 评测报告（2026-11-17 22:55）

- 用例：120 条（`eval_nlu_cases.json`，sha256 `821d6080e1a9bdcc`）
- 模型：qwen-flash, qwen3.8-flash, deepseek-v4.1-flash
- 代码：`9655b4e`（工作区含未提交的 NLU 修复：prompt 日期、城市笔误、订单状态、改口/否定/跨月日期）；Python 3.13.9
- 模式：`raw` 只解析模型 JSON；`pipeline` 走项目真实抽取链路（含规则补齐）
- 指标：意图准确率 / 字段准确率（micro）/ 全对率 / JSON 可解析率 / 延迟

| 配置 | 意图 | 字段 | 全对 | JSON | P50 | P95 | 失败 |
|---|---|---|---|---|---|---|---|
| rules | 89.2% | 87.4% | 72.5% | 100.0% | 0 ms | 0 ms | 0 |
| qwen-flash / raw | 98.3% | 92.8% | 89.2% | 100.0% | 656 ms | 890 ms | 0 |
| qwen-flash / pipeline | 99.2% | 96.6% | 95.8% | 100.0% | 665 ms | 956 ms | 0 |
| qwen3.8-flash / raw | 98.3% | 98.6% | 95.8% | 100.0% | 3731 ms | 9158 ms | 0 |
| qwen3.8-flash / pipeline | 99.2% | 99.5% | 98.3% | 100.0% | 4054 ms | 9592 ms | 0 |
| deepseek-v4.1-flash / raw | 100.0% | 99.5% | 99.2% | 100.0% | 1639 ms | 5385 ms | 0 |
| deepseek-v4.1-flash / pipeline | 100.0% | 100.0% | 100.0% | 100.0% | 1585 ms | 9228 ms | 0 |

## 难例子集（10 条）

| 配置 | 意图 | 字段 | 全对 | P50 | P95 |
|---|---|---|---|---|---|
| rules | 70.0% | 92.9% | 50.0% | 0 ms | 0 ms |
| qwen-flash / raw | 100.0% | 75.0% | 60.0% | 735 ms | 1080 ms |
| qwen-flash / pipeline | 100.0% | 100.0% | 100.0% | 779 ms | 956 ms |
| qwen3.8-flash / raw | 100.0% | 96.4% | 90.0% | 5655 ms | 22727 ms |
| qwen3.8-flash / pipeline | 100.0% | 100.0% | 100.0% | 6491 ms | 9624 ms |
| deepseek-v4.1-flash / raw | 100.0% | 100.0% | 100.0% | 2457 ms | 8415 ms |
| deepseek-v4.1-flash / pipeline | 100.0% | 100.0% | 100.0% | 2682 ms | 4059 ms |

## 失败明细（每配置最多 8 条）

### rules
- `book-weekend`：意图=book；缺/错字段 预测={'check_in': None, 'check_out': None} 期望={'check_in': '2026-11-21', 'check_out': '2026-11-22'}
- `book-sunday`：意图=book；缺/错字段 预测={'check_in': None, 'check_out': None} 期望={'check_in': '2026-11-22', 'check_out': '2026-11-23'}
- `book-next24`：意图=chat；缺/错字段 预测={} 期望={}
- `book-friday`：意图=book；缺/错字段 预测={'check_in': None, 'check_out': None} 期望={'check_in': '2026-11-20', 'check_out': '2026-11-21'}
- `book-two-dates`：意图=chat；缺/错字段 预测={} 期望={}
- `book-budget-range`：意图=book；缺/错字段 预测={'max_price': None} 期望={'max_price': 500}
- `book-chinese-price`：意图=book；缺/错字段 预测={'max_price': None} 期望={'max_price': 1000}
- `book-multi-constraint`：意图=book；缺/错字段 预测={'check_in': None, 'check_out': None} 期望={'check_in': '2026-11-21', 'check_out': '2026-11-23'}

### qwen-flash / raw
- `book-dayafter-nights`：意图=book；缺/错字段 预测={'keyword': None} 期望={'keyword': '海景'}
- `book-sunday`：意图=book；缺/错字段 预测={'check_in': '2026-11-20', 'check_out': '2026-11-21'} 期望={'check_in': '2026-11-22', 'check_out': '2026-11-23'}
- `book-nextweekend`：意图=book；缺/错字段 预测={'check_in': '2026-11-20', 'check_out': '2026-11-22'} 期望={'check_in': '2026-11-28', 'check_out': '2026-11-29'}
- `book-bare-pair`：意图=book；缺/错字段 预测={'check_in': '2026-12-15', 'check_out': '2026-12-18'} 期望={'check_in': '2026-12-12', 'check_out': '2026-12-15'}
- `book-pair-dash`：意图=book；缺/错字段 预测={'check_in': '2026-12-15', 'check_out': '2026-12-18'} 期望={'check_in': '2026-12-12', 'check_out': '2026-12-15'}
- `book-typo-shi`：意图=search；缺/错字段 预测={} 期望={}
- `book-under-200`：意图=search；缺/错字段 预测={} 期望={}
- `search-bay`：意图=search；缺/错字段 预测={'location': '海边'} 期望={'location': '三亚湾'}

### qwen-flash / pipeline
- `book-weekend`：意图=book；缺/错字段 预测={'check_in': '2026-11-20', 'check_out': '2026-11-21'} 期望={'check_in': '2026-11-21', 'check_out': '2026-11-22'}
- `book-sunday`：意图=book；缺/错字段 预测={'check_in': '2026-11-20', 'check_out': '2026-11-21'} 期望={'check_in': '2026-11-22', 'check_out': '2026-11-23'}
- `book-multi-constraint`：意图=book；缺/错字段 预测={'check_in': '2026-11-20', 'check_out': '2026-11-22'} 期望={'check_in': '2026-11-21', 'check_out': '2026-11-23'}
- `book-typo-shi`：意图=search；缺/错字段 预测={} 期望={}
- `search-bay`：意图=search；缺/错字段 预测={'location': '海边'} 期望={'location': '三亚湾'}

### qwen3.8-flash / raw
- `book-two-dates`：意图=search；缺/错字段 预测={} 期望={}
- `search-typo-city`：意图=search；缺/错字段 预测={'city': '大年市'} 期望={'city': '大连'}
- `search-keyword-hotspring`：意图=search；缺/错字段 预测={'keyword': None} 期望={'keyword': '温泉'}
- `order-history`：意图=order_query；缺/错字段 预测={'order_status': None} 期望={'order_status': 'ALL'}
- `edge-date-only`：意图=book；缺/错字段 预测={} 期望={}

### qwen3.8-flash / pipeline
- `book-checkout-first`：意图=search；缺/错字段 预测={} 期望={}
- `search-keyword-hotspring`：意图=search；缺/错字段 预测={'keyword': None} 期望={'keyword': '温泉'}

### deepseek-v4.1-flash / raw
- `search-keyword-hotspring`：意图=search；缺/错字段 预测={'keyword': None} 期望={'keyword': '温泉'}
