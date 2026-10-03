# 酒店预订智能体（Hotel Booking Agent）

一个用 Python 实现的**教学型酒店预订对话 Agent**，在终端里用自然语言完成「搜酒店 → 选房型 → 验价锁房 → 创建真实订单 → 拿支付链接」全流程。

- 真实房价/房型来自**道旅 RollingGo**（B2B 酒店供应链，个人可免费申请 Key）
- 下单走道旅 **OAuth 授权 + REST 直连**，返回通用收银台支付链接（程序不代付）
- LLM 支持通义千问 DashScope（无 Key 时自动降级为内置规则引擎，零配置也能跑）
- 同一套工具层同时服务 **ReAct + Workflow 混合版**与 **Supervisor-Worker 多 Agent 版**两个入口

---

## 目录

- [功能特性](#功能特性)
- [整体架构](#整体架构)
- [目录结构](#目录结构)
- [环境准备](#环境准备)
- [配置项说明（.env）](#配置项说明env)
- [快速开始](#快速开始)
- [对话下单全流程（真实链路）](#对话下单全流程真实链路)
- [真实下单原理与安全设计](#真实下单原理与安全设计)
- [数据降级矩阵](#数据降级矩阵)
- [模块说明](#模块说明)
- [核心数据模型](#核心数据模型)
- [常见问题](#常见问题)
- [免责声明](#免责声明)

---

## 功能特性

- **自然语言预订**：「帮我订下周六成都的酒店，预算 300 以内，住两晚」→ LLM 自动提取城市/日期/预算/星级（含口语日期换算：明天、这周末、下周末）
- **真实酒店搜索**：道旅 `searchHotels` 返回全国城市实时可订酒店、展示价、星级、设施标签；支持按星级、每晚预算服务端筛选
- **实时房型报价**：道旅 `getHotelDetail` 返回同房型下含早/退改等上百条报价，程序按房型聚合、每房型保留最便宜一档，展示餐食、床型、面积、退改政策、是否需申请
- **真实验价 + 下单**：道旅 OAuth REST 接口 `/hotelpriceconfirm`（锁价拿 `referenceNo`）→ `/hotelbook`（创建订单拿 `orderNo` + `paymentUrl`）→ `/hotelorders`（查单）
- **资金安全设计**：验价与下单两步分离、强制二次确认、常用入住人须用户确认后才使用、**程序只拿到支付链接，绝不在程序内完成支付**
- **三级数据源回退**：道旅（有价格）→ 高德 POI（仅名称地址）→ 内置模拟酒店库，任何 Key 缺失都不阻断演示
- **RAG 酒店评价**：切块 → Embedding（DashScope `text-embedding-v2` 或 TF-IDF 模拟）→ 向量召回 → 重排 → 生成回答
- **两套教学架构**：单 Agent 的 ReAct+Workflow 混合状态机（`main.py`）、Supervisor-Worker 四专员多 Agent 协作（`main_multi.py`）

## 整体架构

```
                         用户自然语言（终端）
                                │
                 ┌──────────────▼──────────────┐
                 │   LLMClient（llm.py）        │
                 │  通义千问 / 规则引擎双模式    │
                 │  意图识别 + 参数提取(JSON)    │
                 └──────────────┬──────────────┘
                                │ BookingParams
        ┌───────────────────────┼────────────────────────┐
        ▼                       ▼                        ▼
  agent.py               multi_agent.py               rag.py
  ReAct+Workflow         Supervisor + 4 Worker      评价 RAG 检索
  状态机阶段流转          消息队列+共享状态
        │                       │
        └───────────┬───────────┘
                    ▼
              tools.py（统一工具层 / 内存 ORDER_DB）
                    │
        ┌───────────┼──────────────────┐
        ▼           ▼                  ▼
 rollinggo_mcp.py  amap_poi.py     HOTEL_DB（内置模拟数据）
 道旅 MCP 搜索/详情  高德 POI 回退
 rollinggo_book.py
 道旅 OAuth REST：验价 / 下单 / 查单
```

**主版状态机阶段**（`AgentState.stage`）：
`intent`（意图理解）→ `clarify`（追问缺失的城市/日期）→ `search`（搜索）→ `recommend`（选店）→ `booking`（选房型+入住人，自动验价）→ `booking_confirm`（展示锁定价/退改/邮箱，等待【确认下单】）→ 下单返回支付链接

## 目录结构

```
.
├── README.md                  # 本文档
├── .gitignore                 # 忽略 .env / __pycache__
└── hotel_booking_agent/       # 全部源码与配置（运行时请进入此目录）
    ├── .env.example           # 配置模板（复制为 .env 后填 Key）
    ├── main.py                # 入口①：ReAct + Workflow 混合版（真实下单链路在这）
    ├── main_multi.py          # 入口②：Supervisor-Worker 多 Agent 协作版
    ├── agent.py               # 主版对话 Agent：状态机、选店选房、验价确认编排
    ├── multi_agent.py         # 多 Agent：Supervisor/Search/Booking/Service/Review
    ├── llm.py                 # LLM 封装：DashScope 真实模式 + 规则引擎模拟模式
    ├── models.py              # dataclass：Hotel / RoomRatePlan / BookingParams / Order / AgentState
    ├── tools.py               # 工具层：三级回退搜索、详情、计价、下单、内存订单库
    ├── rollinggo_mcp.py       # 道旅 MCP（Streamable HTTP）客户端：搜索 + 房型报价
    ├── rollinggo_book.py      # 道旅 OAuth REST：登录/验价/下单/查单（也支持命令行）
    ├── amap_poi.py            # 高德 Web 服务 POI 文本搜索适配器
    └── rag.py                 # 酒店评价 RAG（Embedding 召回 + 重排）
```

## 环境准备

- **Python 3.10+**（开发环境为 Windows 11 + Python 3.13）
- 依赖包：

```powershell
pip install requests python-dotenv
pip install dashscope      # 可选：不装则 LLM 与 RAG 自动用规则/TF-IDF 模拟模式
```

> 无需 Node.js：道旅 OAuth 登录已在 `rollinggo_book.py` 中用 Python 原生实现（授权码 + PKCE，起本地回调服务），不依赖官方 `@rollinggo/hotel` CLI。

## 配置项说明（.env）

进入 `hotel_booking_agent/` 目录，复制模板：

```powershell
Copy-Item .env.example .env   # PowerShell
```

| 配置项 | 必填 | 使用模块 | 作用 |
|---|---|---|---|
| `DASHSCOPE_API_KEY` | 否 | llm.py / rag.py | 通义千问 Key；留空 → 意图识别用规则引擎、Embedding 用 TF-IDF |
| `AMAP_API_KEY` | 选填 | amap_poi.py | 高德「**Web 服务**」类型 Key；道旅不可用时的 POI 回退（无价格） |
| `ROLLINGGO_API_KEY` | 真实搜索必填 | rollinggo_mcp.py | 道旅开发者 Key（`mcp_` 开头），用于酒店搜索与实时房价 |
| `ROLLINGGO_MCP_URL` | 可选 | 两个 rollinggo 模块 | 默认 `https://mcp.rollinggo.cn/mcp`；国际站改 `https://mcp.rollinggo.ai/mcp` |
| `ROLLINGGO_OAUTH_SERVER` | 可选 | rollinggo_book.py | OAuth 中转，默认 `https://rollinggo.store` |
| `ROLLINGGO_OAUTH_AUTHORIZE` | 可选 | rollinggo_book.py | 授权端点，默认 `https://api.rollinggo.cn/oauth2/authorize` |
| `ROLLINGGO_CLIENT_ID` | 可选 | rollinggo_book.py | 默认 `rollinggoskill`（国内版固定值） |
| `ROLLINGGO_ACCESS_TOKEN` | 可选 | rollinggo_book.py | 直接注入 access token，优先于本地 token 文件 |
| `ROLLINGGO_TOKEN_FILE` | 可选 | rollinggo_book.py | token 存储路径，默认 `~/.hotel-cli/token.json` |

Key 申请地址：

- 道旅：`https://travelportal-partner-center.dida.com/register?lang=zh`（注册后拿 `mcp_` 开头 Key；下单权限通过 OAuth 登录授权，见下节）
- DashScope：`https://dashscope.console.aliyun.com/`
- 高德：`https://lbs.amap.com/`（注意必须创建 **Web 服务** 类型 Key）

`.env` 已被 `.gitignore` 忽略，不会提交；仓库里只提供 `.env.example`。

## 快速开始

```powershell
# Windows PowerShell（中文环境建议开启 UTF-8）
$env:PYTHONUTF8 = "1"
cd hotel_booking_agent

# 首次使用真实下单：浏览器授权一次（token 存到 ~/.hotel-cli/token.json，约 14 天有效）
python rollinggo_book.py login
python rollinggo_book.py          # 不带参数 = 查看登录状态

# 启动主版（支持真实下单全链路）
python main.py

# 或启动多 Agent 教学版
python main_multi.py
```

只配置 `ROLLINGGO_API_KEY` 而不登录 OAuth：搜索/房价/验价前的浏览全部可用，走到真实下单时程序会提示登录命令，并自动回退为本地模拟下单，不阻断体验。

## 对话下单全流程（真实链路）

```
你 > 帮我订 10 月 20 日成都的酒店，住一晚，预算 300 以内
       → 道旅实时搜索，返回酒店列表（序号、名称、星级、每晚价、设施）

你 > 1
       → 拉取该酒店实时房型报价（含早/床型/面积/退改，按价格排序，限显 8 种）

你 > 1，入住人陈晨
       → 自动调用验价接口锁房：展示【最终总价】（含税费，可能与列表价不同）、
         退改截止时间、账号常用入住人/邮箱（须确认后才使用）
       → 进入下单二次确认阶段

你 > 确认下单
       → 调用创建订单接口：返回道旅订单号 + 收银台支付链接
       → 订单保存为 source=rollinggo；不支付不会扣款，超时未付订单自动取消

你 > 取消          # 验价后任何时候都可放弃，referenceNo 随即作废
```

也可以在多轮对话中补充：「换一家」「第 3 种房型」「使用常用入住人 1」「换个邮箱 xxx@xx.com」。

命令行直接操作道旅账号（不经过对话）：

```powershell
python rollinggo_book.py login     # 浏览器 OAuth 授权
python rollinggo_book.py           # 查看 token 状态
```

## 真实下单原理与安全设计

鉴权分两层，这是道旅平台的设计，不是程序限制：

| 能力 | 鉴权方式 | 端点/协议 |
|---|---|---|
| 搜索酒店、房型报价 | API Key（`mcp_`，Bearer 头） | MCP Streamable HTTP：`searchHotels` / `getHotelDetail` |
| 验价、下单、查单 | OAuth 2.0 授权码 + PKCE | REST：`/hotelpriceconfirm` / `/hotelbook` / `/hotelorders` |

下单数据流（字段严格来自上一步响应，不自行构造）：

```
getHotelDetail 产出 ratePlanId
      │
      ▼
POST /hotelpriceconfirm {hotelID, ratePlanID, dateParam, occupancyDetails}
      │  成功码 code=2000
      ▼
priceDetailsInfo.referenceNo（锁价参考号，短时效）
      │  + guestProfile.recentGuests（仅展示，须用户确认）
      ▼
POST /hotelbook {referenceNo, contact{firstName,lastName,email}, guestList}
      │
      ▼
orderNo + paymentUrl（通用收银台，用户自行打开支付）
```

安全约定：

1. **验价与下单分离**：选房只触发验价，必须用户明确回复「确认下单」才会创建订单。
2. **不代付**：程序拿到 `paymentUrl` 即结束，不保存支付信息、不自动跳转扣款。
3. **常用入住人保护**：验价响应中的 `recentGuests/contactDefault` 只做展示，用户回复序号确认后才带入订单。
4. **中文姓名自动拆分**：按「姓 + 名」拆为 `lastName/firstName`，可在确认前纠正。
5. **申请房提示**：`isOnRequest=true` 的房型需供应商确认，并非即时确认，界面会明示。
6. **轮询有界**：需要异步确认的接口 2 秒轮询一次、最多 150 次（5 分钟），失败给出明确提示，不谎称成功。
7. 未登录 OAuth 时自动降级为内存模拟订单（`ORD...` 号），并提示如何登录。

## 数据降级矩阵

| 配置情况 | 酒店数据 | 价格/房型 | 下单 |
|---|---|---|---|
| `ROLLINGGO_API_KEY` 有效 | 全国真实酒店 | 实时报价 + 退改 | OAuth 登录后真实下单，否则模拟下单 |
| 仅 `AMAP_API_KEY` 有效 | 高德真实 POI（名称/地址） | 无（价格筛选不生效） | 模拟下单 |
| 均未配置/接口异常 | 内置 4 城市模拟库 | 模拟数据 | 模拟下单 |
| 无 `DASHSCOPE_API_KEY` | 不影响数据源 | — | 意图识别走规则引擎，RAG 走 TF-IDF |

## 模块说明

| 文件 | 关键内容 |
|---|---|
| `models.py` | 全部 dataclass；`BookingParams.is_complete()/missing_fields()` 驱动参数追问；`Order.source` 区分 mock/rollinggo |
| `llm.py` | `LLMClient.extract_intent_and_params()`：意图 book/search/order_query/cancel/chat + 结构化参数；含口语日期换算 prompt |
| `tools.py` | `search_hotels()` 三级回退；`get_hotel_detail(live=True)` 实时富化；`create_order()` 模拟单；`create_real_order()` 道旅真单；运行时酒店缓存 `_RUNTIME_HOTEL_CACHE`；内存 `ORDER_DB` |
| `rollinggo_mcp.py` | `RollingGoMCPClient`：MCP 握手、JSON/SSE 双解析；搜索参数构造（`hotelTags.maxPricePerNight` 等）；报价按房型聚合取最低价 |
| `rollinggo_book.py` | OAuth PKCE 登录与本地回调服务；token 存取（与官方 CLI 共享 `~/.hotel-cli/token.json`）；`price_confirm() / create_booking() / list_orders() / get_order_detail()` |
| `amap_poi.py` | 高德文本搜索 v3（`restapi.amap.com/v3/place/text`），住宿大类 100000，星级词映射 |
| `agent.py` | 主状态机：选店、序号/房型名/床型关键词匹配报价、验价确认卡编排、下单与取消 |
| `multi_agent.py` | `AgentMessage` 消息队列 + `AgentState` 共享黑板；Supervisor 分发 Search/Booking/Service/Review 四个 Worker |
| `rag.py` | 模拟评价语料切块、Embedding 双模式、召回+重排+生成 |
| `main.py` / `main_multi.py` | 两个终端入口，仅做输入输出循环 |

## 核心数据模型

```python
RoomRatePlan   # room_name, price_per_night, meal, cancelable, cancel_policy,
               # bed_type, max_occupancy, room_size, on_request, rate_plan_id
Hotel          # hotel_id(道旅前缀 RLG_), name, star, price_per_night,
               # facilities, room_types, rate_plans[], booking_url
Order          # order_id, ..., status(pending/paid/cancelled),
               # source(mock/rollinggo), payment_url, contact_email
BookingParams  # city / check_in / check_out（必填）, min_star,
               # max_price, facilities, keyword（选填）
```

## 常见问题

**Q：搜索能调通，下单报「未登录」？**
A：`mcp_` API Key 只有搜索权限，下单必须先 `python rollinggo_book.py login` 完成 OAuth 授权。

**Q：验价总价和列表价不一样？**
A：列表价是缓存展示均价，验价返回的是含税费/服务费的实时锁定价，以下单确认卡上的验价为准。

**Q：token 过期怎么办？**
A：重新执行 `python rollinggo_book.py login`；token 文件在 `~/.hotel-cli/token.json`，在仓库目录之外，不会被提交。

**Q：Windows 终端中文乱码？**
A：先执行 `$env:PYTHONUTF8 = "1"` 再运行程序。

**Q：会真的扣钱吗？**
A：只有你本人打开 `paymentUrl` 在收银台完成支付才会扣款；只验价或创建订单后不支付，不会产生费用。

## 免责声明

本项目为教学演示项目：酒店数据与报价归道旅及对应供应商所有，价格与房态以下单收银台实时信息为准；请在你本人的道旅账号下使用真实接口，自行承担预订与支付后果。请勿将 `.env`、OAuth token 文件或任何真实密钥提交到公开仓库。
