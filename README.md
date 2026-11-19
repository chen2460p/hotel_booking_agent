# 酒店预订智能体（Hotel Booking Agent）

一个用 Python 实现的**酒店预订对话 Agent**，在终端里用自然语言完成「搜酒店 → 选房型 → 验价锁房 → 创建真实订单 → 拿支付链接」全流程。

- 真实房价/房型来自**道旅 RollingGo**（B2B 酒店供应链，个人可免费申请 Key）
- 下单走道旅 **OAuth 授权 + REST 直连**，返回通用收银台支付链接（程序不代付）
- LLM 支持通义千问 DashScope（无 Key 时自动降级为内置规则引擎，零配置也能跑）
- 同一套工具层同时服务 **ReAct + Workflow 混合版**与 **Supervisor-Worker 多 Agent 版**两个入口

> 用 AI 编码工具（Claude Code / Cursor / Trae / DSH 等）改这个仓库前，先读根目录 [AGENTS.md](AGENTS.md)：运行目录、测试红线、真实交易接口禁令、分层约定都在那里。

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
- [测试与评测](#测试与评测)
- [常见问题](#常见问题)
- [免责声明](#免责声明)

---

## 功能特性

- **自然语言预订**：「帮我订下周六成都的酒店，预算 300 以内，住两晚」→ LLM 自动提取城市/日期/预算/星级（含口语日期换算：明天、后天、这周末、下周末、"住三天"晚数推断）；多轮对话里先答入住再补离店不会互相覆盖
- **真实酒店搜索**：道旅 `searchHotels` 返回全国城市实时可订酒店、展示价、星级、设施标签；支持按星级、每晚预算服务端筛选；**带 POI（故宫/中街/春熙路/高铁站/大学/中小学/医院）搜索时每条酒店附带直线距离，并按"近→远"排序**；POI 搜索有距离结果时不再补城市搜索，避免市中心/下辖县酒店混入
- **地名笔误确认**：如「南宁是第二中学」这种「市/是」笔误，或「大年市西安路」这种城市名笔误（→大连市），先提示「我猜你想说的是『大连市西安路』附近，对吗？」，用户确认后才搜索，避免按错地名查出一堆无关酒店
- **实时房型报价**：道旅 `getHotelDetail` 返回同房型下含早/退改等上百条报价，程序按房型聚合、每房型保留最多 4 档代表性报价（最低价 / 含早 / 可取消 / 申请确认），**同房型多档对比**并标出差价；每个房型配**多张实拍图**（道旅 `roomInfo.images` 单图 + 携程官方相册按房型名模糊匹配补齐，每房型最多 8 张；携程抓取仅限本地演示，勿用于生产/商用）
- **真实验价 + 下单**：道旅 OAuth REST 接口 `/hotelpriceconfirm`（锁价拿 `referenceNo`）→ `/hotelbook`（创建订单拿 `orderNo` + `paymentUrl`）→ `/hotelorders`（查单）
- **历史订单查询**：对话里说「查我的历史订单 / 查待支付的订单 / 查已取消的订单」即可拉取道旅账号全部真实订单（跨会话保留），支持按状态筛选；未登录时降级展示本次会话的本地订单并引导登录
- **Web 多会话与聊天记录保留**：聊天记录按会话缓存在浏览器 `localStorage`，从「查看历史订单」返回或刷新页面后自动恢复（含已展开的酒店、滚动位置）；顶栏可切换历史会话或「＋ 新建会话」。服务端上下文 30 分钟无活动会过期，前端会提示「历史仍在、上下文已重置」
- **酒店列表结构化下发（防幻觉、防错位）**：酒店信息只保留在服务端会话（`search_results` + `search_revision`），Web 端通过 `GET /api/session/{id}/hotels` 取结构化 items 渲染卡片；选店走 `POST /api/session/{id}/select`（`hotel_id` + `revision`），不经过 LLM、也不按序号猜。旧列表/会话过期由后端拒绝（409/404）并把卡片置灰，避免用户点到上一轮的第 N 家
- **房型列表结构化下发**：选中酒店后，`ChatResponse.rooms_view` 直接给出房型 × 报价档（`room_no` / `plan_no` / `plan_key`（即 `rate_plan_id`）/ 每晚价 / 总价 / 餐食 / 退改 / 图片），编号与文本渲染同源（都走 `plan_groups`）；选房走 `POST /api/session/{id}/room`（`room_no` + `plan_no` + `rooms_revision`），**不再解析回复文本里的「2-1」**，旧房型列表返回 409
- **订单时间可追溯**：下单时间（道旅 `orderDate` / 本地 `created_at`）与支付时间（`paid_at`）分开存储、互不覆盖；列表按下单时间倒序，已支付订单在明细行单独展示支付时间。道旅接口没有支付时间字段时显示 `-`，不拿创建时间冒充
- **真实订单取消**：说「取消订单 订单号」先走 pre-cancel 查询罚金/退款并拿 10 分钟凭证，用户明确回复「确认取消」后才提交 confirm-cancel；不可在线取消时原样展示道旅客服指引。Web 订单页同样支持两步取消
- **资金安全设计**：验价与下单两步分离、强制二次确认、常用入住人须用户确认后才使用、**程序只拿到支付链接，绝不在程序内完成支付**
- **三级数据源回退**：道旅（有价格）→ 高德 POI（仅名称地址）→ 内置模拟酒店库，任何 Key 缺失都不阻断运行
- **RAG 酒店评价**：切块 → Embedding（DashScope `text-embedding-v2` 或 TF-IDF 模拟）→ 向量召回 → 重排 → 生成回答
- **两套架构**：单 Agent 的 ReAct+Workflow 混合状态机（`main.py`）、Supervisor-Worker 四专员多 Agent 协作（`main_multi.py`）

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
├── AGENTS.md                     # 给 AI 编码助手的项目约定（动手前先读）
├── README.md                     # 本文档
├── .gitignore                    # 忽略 .env / *.db / __pycache__ / 测试临时目录
├── .dockerignore                 # Docker 构建上下文排除
├── Dockerfile                    # 镜像构建；单 worker，勿加 --workers
├── docker-compose.yml            # 单机部署编排（显式 name: hotel-booking）
├── DOCKER.md                     # 部署与运维说明
├── .gitea/workflows/deploy.yml   # push main 自动部署；宿主端口 9122
├── scripts/                      # 评测与部署自检（从仓库根执行）
│   ├── eval_nlu.py               # 120 条 NLU 评测（3 模型 × raw/pipeline）
│   ├── eval_nlu_cases.json       # 评测用例集
│   ├── eval_nlu_report.md        # 最新评测报告（资产，随代码提交）
│   ├── eval_nlu_baseline.md      # 修复前基线
│   ├── eval_nlu_results.json     # 逐条明细
│   └── preflight.py              # 部署前 30 秒自检
├── tests/                        # 全部测试（从仓库根执行）
│   ├── run_all.py                # 离线单测总入口，自动发现 test_*_unit.py
│   ├── test_api_smoke.py         # 接口冒烟，需先起服务
│   └── test_*_unit.py            # 离线单测（NLU/选店/房型/取消/订单时间/支付守卫…）
└── hotel_booking_agent/          # 业务源码与配置（运行时进入此目录）
    ├── .env.example              # 配置模板（复制为 .env 后填 Key）
    ├── requirements-web.txt      # Web 依赖 + 通用依赖
    ├── 启动前端.bat              # Windows 一键起 Web（chcp 65001 + python web.py）
    ├── main.py                   # 入口①：ReAct + Workflow 混合版（真实下单链路在这）
    ├── main_multi.py             # 入口②：Supervisor-Worker 多 Agent 协作版
    ├── web.py                    # 入口③：FastAPI（托管前端产物 + JSON/SSE 接口 + 订单页）
    ├── templates/orders.html     # 订单页（Jinja2 服务端渲染）
    ├── frontend/                 # Web 聊天页源码（React + TypeScript + Vite）
    │   ├── package.json          # 前端依赖与脚本（dev / build）
    │   ├── vite.config.ts        # dev 时把 /api、/orders 代理到 :8000
    │   ├── index.html            # 页面外壳（referrer=no-referrer 防道旅 CDN 403）
    │   └── src/
    │       ├── App.tsx           # 根组件：顶栏 + 聊天区 + 输入区
    │       ├── api/              # client.ts（REST，409/404 语义）、sse.ts（POST SSE 解帧）
    │       ├── hooks/            # useSessions（localStorage 会话栈）、useChat（对话流程）
    │       ├── lib/              # imgQueue（图片串行队列）、richText、sessionOps、format
    │       ├── components/       # 卡片 / 消息 / 展开按钮 / 输入框等
    │       └── styles.css        # 与旧版同一套类名，视觉零变化
    ├── agent.py                  # 主版状态机：选店选房、验价确认编排
    ├── multi_agent.py            # 多 Agent：Supervisor/Search/Booking/Service/Review
    ├── agent_common.py           # 单/多 Agent 共用纯逻辑：合并参数、选店选房、渲染
    ├── cli_common.py             # 终端入口共用：显示宽度计算与欢迎横幅
    ├── text_rules.py             # 姓名/订单状态规则
    ├── text_utils.py             # 名称归一/相似度（携程、高德、评分匹配共用）
    ├── llm.py                    # LLM 封装：DashScope 真实模式 + 规则引擎降级
    ├── nlu_corrections.py        # NLU 后处理：改口 / 否定约束 / 跨月与倒挂日期
    ├── geo_data.py               # 地级市中心坐标表，校正 POI 编码偏差
    ├── models.py                 # dataclass：Hotel / RoomRatePlan / BookingParams / Order / AgentState
    ├── tools.py                  # 工具层：三级回退搜索、详情、计价、下单/取消
    ├── storage.py                # 订单存储抽象：SQLite / MySQL / 内存自动回退
    ├── rag.py                    # 酒店评价 RAG（Embedding 召回 + 重排）
    ├── rollinggo_mcp.py          # 道旅 MCP 客户端：搜索 + 房型报价
    ├── rollinggo_book.py         # 道旅 OAuth REST：登录/验价/下单/查单/取消
    ├── amap_poi.py               # 高德 Web 服务 POI 文本搜索适配器
    ├── ctrip_crawler.py          # 携程图集/评分抓取（仅教学演示，禁止商用）
    └── hotel_rating.py           # 真实住客评分富化：携程→百度→高德，7 天缓存
```


## 环境准备

- **Python 3.10+**（开发环境为 Windows 11 + Python 3.13）
- 依赖包：

```powershell
pip install requests python-dotenv
pip install dashscope      # 可选：RAG 真实 Embedding 需要；不装则 RAG 用 TF-IDF 模拟
```

> 道旅 OAuth 登录已在 `rollinggo_book.py` 中用 Python 原生实现（授权码 + PKCE，起本地回调服务），**不需要官方 `@rollinggo/hotel` CLI，也不需要 Node.js**。
>
> 只有构建 Web 聊天页（`hotel_booking_agent/frontend`）需要 **Node.js 20+**；`main.py` / `main_multi.py` 和 Docker 运行时都不需要（镜像里是独立的 node 构建阶段）。

## 配置项说明（.env）

进入 `hotel_booking_agent/` 目录，复制模板：

```powershell
Copy-Item .env.example .env   # PowerShell
```

| 配置项 | 必填 | 使用模块 | 作用 |
|---|---|---|---|
| `DASHSCOPE_API_KEY` | 否 | llm.py / rag.py | 通义千问 Key；留空 → 意图识别用规则引擎、Embedding 用 TF-IDF。**默认模型 deepseek-v4.1-flash**（走 DashScope OpenAI 兼容接口，可通过 `DASHSCOPE_MODEL` 覆盖） |
| `DASHSCOPE_MODEL` | 可选 | llm.py | 覆盖默认 LLM 模型名（如 `deepseek-v4.1-flash` / `qwen-flash` / `qwen3.8-flash`；120 条 NLU 评测见 `scripts/eval_nlu_report.md`） |
| `DASHSCOPE_TIMEOUT` | 可选 | llm.py | 单次对话读取超时（秒），默认 30；超时自动回退规则引擎，避免思考模型长尾卡住一条消息 |
| `AMAP_API_KEY` | 选填 | amap_poi.py | 高德「**Web 服务**」类型 Key；道旅不可用时的 POI 回退（无价格） |
| `BAIDU_MAP_AK` | 选填 | hotel_rating.py | 百度地图「**服务端**」AK；评分富化的次选源（携程 → 百度 → 高德），留空则跳过。线上需在仓库 Secrets 配置同名变量才生效 |
| `ROLLINGGO_API_KEY` | 真实搜索必填 | rollinggo_mcp.py | 道旅开发者 Key（`mcp_` 开头），用于酒店搜索与实时房价 |
| `ROLLINGGO_MCP_URL` | 可选 | 两个 rollinggo 模块 | 默认 `https://mcp.rollinggo.cn/mcp`；国际站改 `https://mcp.rollinggo.ai/mcp` |
| `ROLLINGGO_OAUTH_SERVER` | 可选 | rollinggo_book.py | OAuth 中转，默认 `https://rollinggo.store` |
| `ROLLINGGO_OAUTH_AUTHORIZE` | 可选 | rollinggo_book.py | 授权端点，默认 `https://api.rollinggo.cn/oauth2/authorize` |
| `ROLLINGGO_CLIENT_ID` | 可选 | rollinggo_book.py | 默认 `rollinggoskill`（国内版固定值） |
| `ROLLINGGO_ACCESS_TOKEN` | 可选 | rollinggo_book.py | 直接注入 access token，优先于本地 token 文件 |
| `ROLLINGGO_TOKEN_FILE` | 可选 | rollinggo_book.py | token 存储路径，默认 `~/.hotel-cli/token.json` |

评分富化的 QPS 节流可用 `BAIDU_QPS_LIMIT` / `AMAP_QPS_LIMIT` / `CTRIP_QPS_LIMIT` 调整（默认 2 / 5 / 2），一般无需修改。

Key 申请地址：

- 道旅：`https://travelportal-partner-center.dida.com/register?lang=zh`（注册后拿 `mcp_` 开头 Key；下单权限通过 OAuth 登录授权，见下节）
- DashScope：`https://dashscope.console.aliyun.com/`
- 高德：`https://lbs.amap.com/`（注意必须创建 **Web 服务** 类型 Key）
- 百度地图：`https://lbsyun.baidu.com/`（创建 **服务端** AK，用于评分次选源）

`.env` 已被 `.gitignore` 忽略，不会提交；仓库里只提供 `.env.example`。

## 快速开始

```powershell
# Windows PowerShell（中文环境建议开启 UTF-8）
$env:PYTHONUTF8 = "1"
cd hotel_booking_agent

# 首次使用真实下单：浏览器授权一次（token 存到 ~/.hotel-cli/token.json，约 14 天有效）
python rollinggo_book.py login     # 浏览器授权一次
python rollinggo_book.py status    # 查看登录状态（有效期以 status 输出为准）

# 启动主版（支持真实下单全链路）
python main.py

# 或启动多 Agent 协作版
python main_multi.py
```

只配置 `ROLLINGGO_API_KEY` 而不登录 OAuth：搜索/房价/验价前的浏览全部可用，走到真实下单时程序会提示登录命令，并自动回退为本地模拟下单，不阻断体验。

### 启动 Web 界面

聊天页是 React 构建产物（`hotel_booking_agent/frontend/dist`），第一次用、以及改过前端之后要构建一次：

```powershell
cd hotel_booking_agent/frontend
npm ci
npm run build          # 类型检查（tsc --noEmit）+ 打包，产物落在 frontend/dist
cd ..
python web.py          # http://127.0.0.1:8000（也可双击 启动前端.bat）
```

只想调界面时不用反复构建：另开一个终端跑 `npm run dev`（http://127.0.0.1:5173），它会把 `/api`、`/orders` 代理到 8000 上的 `python web.py`。

> 没构建就启动 `python web.py` 不会报错：接口和订单页照常可用，首页会显示一条「前端未构建」的提示页，照着上面的命令构建即可。Docker 镜像里由构建阶段自动完成，不需要宿主装 node。

## 对话下单全流程（真实链路）

```
你 > 帮我订 10 月 20 日成都的酒店，住一晚，预算 300 以内
       → 道旅实时搜索，返回酒店列表（序号、名称、星级、每晚价、设施）

你 > 1
       → 拉取该酒店实时房型报价（含早/床型/面积/退改，按价格排序，最多展示 8 个房型）
         每个房型最多 4 档报价：最低价 / 含早 / 可取消 / 申请确认，并标出差价

你 > 返回上一级     # 不喜欢这家：回到原酒店列表，不重新搜索
       → 重新展示之前的搜索结果，可直接选另一家

你 > 2-1，入住人陈晨   # 第 2 个房型的第 1 档；只回复 2 默认该房型最低价
       → 自动调用验价接口锁房：展示【最终总价】（含税费，可能与列表价不同）、
         退改截止时间、账号常用入住人/邮箱（须确认后才使用）
       → 进入下单二次确认阶段

你 > 确认下单
       → 调用创建订单接口：返回道旅订单号 + 收银台支付链接
       → 订单保存为 source=rollinggo；不支付不会扣款，超时未付订单自动取消

你 > 取消          # 验价后任何时候都可放弃，referenceNo 随即作废
```

验价若提示「该房型报价可能已变动或售罄」（道旅 `code=6201`），不会让你对着同一档反复重试：
助手会重新拉取该酒店的实时房型、剔除刚验价失败的那一档，并回到选房步骤——
已填的入住人姓名会保留，只需选新房型序号；整家酒店都查不到可订房型时，自动退回酒店列表换一家。

真实订单创建后不想要了：

```
你 > 取消订单 H179328570303748911
       → 调用 /hotelprecancel：展示取消罚金、预计退款、10 分钟有效凭证
       → 尚未支付的订单通常可免费取消；不可在线取消时给客服电话/邮箱

你 > 确认取消
       → 调用 /hotelcancelconfirm：远端成功后才更新本地状态
```

也可以在多轮对话中补充：「换一家」「返回上一级」「第 3 种房型」「2 含早」「2 可取消」「2-1」「使用常用入住人 1」「换个邮箱 xxx@xx.com」。

历史订单查询：

```
你 > 查我的历史订单        # 道旅账号全部真实订单（已登录时；未登录显示本次会话模拟单）
你 > 查待支付的订单        # 状态筛选：待支付 / 已完成已支付 / 已取消
你 > 查订单 2026XXXXXXXX   # 按订单号查单笔详情（本地没有会自动查道旅详情）
```

命令行直接操作道旅账号（不经过对话）：

```powershell
python rollinggo_book.py login     # 浏览器 OAuth 授权
python rollinggo_book.py           # 查看 token 状态
python rollinggo_book.py orders [all|pending|finished]  # 查历史订单
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
| `models.py` | 全部 dataclass；`Hotel` 含 `distance_m`（距 POI 直线距离米）与 `distance_to`（参照地标名）；`BookingParams.is_complete()/missing_fields()` 驱动参数追问；`Order.source` 区分 mock/rollinggo |
| `llm.py` | `LLMClient.extract_intent_and_params()`：意图 book/search/order_query/cancel/chat + 结构化参数；默认模型 deepseek-v4.1-flash（OpenAI 兼容接口，`DASHSCOPE_MODEL` 可覆盖）；含口语日期换算（今天/明天/后天/住三天）与裸数字日期对（回「12 15」= 12 号入住、15 号离店）；改口/否定/跨月日期走 `nlu_corrections` 确定性后处理；含错误日志直接打印 status/code/message |
| `nlu_corrections.py` | 模型抽取之后的确定性修正层：`apply_text_corrections()` 处理多轮改口（"不是成都，是重庆"）与否定约束（"不要海景房"），`normalize_dates()` 处理过去日期前滚、离店早于入住时的跨月推断；真实模型与规则引擎两条路径共用，单/多 Agent 一致 |
| `tools.py` | `search_hotels()` 三级回退；`get_hotel_detail(live=True)` 实时富化；`create_order()` 模拟单；`create_real_order()` 道旅真单；`find_order()` 本地/远端订单查找；`pre_cancel_real_order() / confirm_cancel_real_order()` 真实单两步取消；`list_history_orders()` 历史订单（远程+本地合并、状态筛选、按下单时间倒序）；运行时酒店缓存；`format_hotel_list()` 在有 POI 距离时输出距离行 |
| `storage.py` | 订单存储抽象：SQLite/MySQL/内存自动回退；orders 表含 `created_at/paid_at/updated_at`，首次启动自动补列并把旧 `created_at` 从 UTC 转本地时区；状态变更只刷新 `updated_at`，首次支付写 `paid_at`，永不覆盖 `created_at` |
| `rollinggo_mcp.py` | `RollingGoMCPClient`：MCP 握手、JSON/SSE 双解析；搜索参数构造（`hotelTags.maxPricePerNight` 等）；报价按「房型 + 餐食 + 退改 + 申请确认 + 床型 + 入住人数」分桶，每房型保留最多 4 档代表性报价；POI 距离透传（`distanceInMeters`）；地标识别扩展到 街/路/大道；排序策略：有 POI 距离时按"近→远"、未知沉底，否则地标词命中优先 |
| `rollinggo_book.py` | OAuth PKCE 登录与本地回调服务；token 存取（与官方 CLI 共享 `~/.hotel-cli/token.json`）；`price_confirm() / create_booking() / list_orders() / get_order_detail() / parse_orders()`；`pre_cancel() / confirm_cancel()` 两步取消；CLI 支持 `login/status/logout/orders` |
| `amap_poi.py` | 高德文本搜索 v3（`restapi.amap.com/v3/place/text`），住宿大类 100000，星级词映射 |
| `agent.py` | 主状态机：选店、验价确认卡编排、下单与取消（选择/解析/渲染细节走 `agent_common`） |
| `multi_agent.py` | `AgentMessage` 消息队列 + `AgentState` 共享黑板；Supervisor 分发 Search/Booking/Service/Review 四个 Worker |
| `agent_common.py` | 两套架构共用的纯逻辑：`merge_params / parse_hotel_selection / match_room_choice / extract_guest_name / detect_order_filter / render_hotel_results_parts / render_hotel_selection / chat_reply` 等，含同房型多档报价的分组、差价展示与 `1-2` 选择解析 |
| `web.py` | FastAPI 入口：托管 `frontend/dist`（首页）/ `templates/orders.html`（订单页）；会话池（30 分钟 TTL）、SSE 阶段推送、结构化酒店列表 `GET /api/session/{id}/hotels` 与按 `hotel_id + revision` 选店 `POST /api/session/{id}/select`（旧列表 409）；选中酒店后返回 `rooms_view`（房型 × 报价档，`plan_key` 即 `rate_plan_id`），按 `room_no + plan_no + rooms_revision` 选房 `POST /api/session/{id}/room`（不解析文本，旧房型列表 409）；`hotel_view` 只由服务端搜索结果生成 |
| `frontend/` | React + TypeScript 聊天页：`api/sse.ts` 用 fetch 解 POST-SSE 帧；`lib/imgQueue.ts` 串行加载图片（道旅 CDN 并发会 403）并做可视区懒加载；`lib/sessionOps.ts` 管 localStorage 多会话栈（键仍为 `hb_sessions_v1`）；卡片只渲染 `hotel_view` 结构化字段，能否点击由 `activeRevision` 派生 |
| `cli_common.py` | 终端入口共用：`display_width()` 与 `print_banner(rows)`（按显示宽度生成 ASCII 横幅） |
| `rag.py` | 模拟评价语料切块、Embedding 双模式、召回+重排+生成 |
| `text_rules.py` | 姓名与订单状态的文本规则，`llm.py` 与 `agent_common.py` 共用，避免两处各写一套 |
| `text_utils.py` | 名称归一与相似度打分，携程、高德、评分匹配三处共用同一套算法 |
| `geo_data.py` | 全国地级行政区中心坐标表。道旅 POI 地理编码不可靠（实测把"大连西安路"编到西安），用城市中心坐标校验返回酒店的经纬度距离；精度约 0.1 度，足够 60~150 公里级判定 |
| `ctrip_crawler.py` | 携程联想搜索拿 `hotelId` 与评分、详情页 SSR 拿图集，作为房型图片回退。**仅个人学习/教学演示，禁止生产或商用部署**（模块头有完整法律声明） |
| `hotel_rating.py` | 真实住客评分富化：携程优先 → 百度其次 → 高德兜底，每条结果标注来源、绝不编造评分。7 天正缓存 + 确认无评分时的 3 天负缓存，内含 QPS 令牌桶节流 |
| `main.py` / `main_multi.py` | 两个终端入口，仅做输入输出循环 |

## 核心数据模型

```python
RoomRatePlan   # room_name, price_per_night, meal, cancelable, cancel_policy,
               # bed_type, max_occupancy, room_size, on_request, rate_plan_id
Hotel          # hotel_id(道旅前缀 RLG_), name, star, price_per_night,
               # facilities, room_types, rate_plans[], booking_url,
               # distance_m（距搜索地标直线距离米，0=未知）, distance_to（参照地标）
Order          # order_id, ..., status(pending/paid/cancelled),
               # source(mock/rollinggo), payment_url, contact_email
BookingParams  # city / check_in / check_out（必填）, min_star,
               # max_price, facilities, keyword（选填）
```

## 测试与评测

```powershell
python tests/run_all.py          # 离线单测总入口（约 10 秒，不联网、不写 orders.db）
python tests/test_api_smoke.py   # 接口冒烟：需先起服务（BASE_URL 可覆盖，默认 8000）
python scripts/eval_nlu.py       # 120 条 NLU 评测，约 30 分钟（3 模型 × raw/pipeline）
```

- 单个测试文件都能直接跑：`python tests/test_cancel_unit.py`；装了 pytest 可 `pytest tests/ -v --ignore=tests/test_api_smoke.py`
- 跑 `test_api_smoke.py` 之前先 `cd hotel_booking_agent/frontend && npm run build`：没构建时服务照样能起，但 `GET /` 是 500 构建提示页，首页用例会失败
- 真实模型/供应链用例默认关闭，用 `RUN_LLM_TESTS=1` 单独开启，别塞进回归
- 快速跑评测时用 `--limit N --out <临时文件> --json-out <临时文件>`，避免覆盖正式报告
- 评测结果：最新看 `scripts/eval_nlu_report.md`，修复前基线看 `scripts/eval_nlu_baseline.md`，逐条明细在 `scripts/eval_nlu_results.json`

## 常见问题

**Q：搜索能调通，下单报「未登录」？**
A：`mcp_` API Key 只有搜索权限，下单必须先 `python rollinggo_book.py login` 完成 OAuth 授权。

**Q：验价总价和列表价不一样？**
A：列表价是缓存展示均价，验价返回的是含税费/服务费的实时锁定价，以下单确认卡上的验价为准。

**Q：token 过期怎么办？**
A：重新执行 `python rollinggo_book.py login`；token 文件在 `~/.hotel-cli/token.json`，在仓库目录之外，不会被提交。

**Q：Windows 终端中文乱码？**
A：先执行 `$env:PYTHONUTF8 = "1"` 再运行程序。

**Q：下单后不想支付，怎么取消？**
A：在对话里回复「取消订单 订单号」，助手会先查取消罚金（未支付订单通常为 0）并给出 10 分钟凭证；确认后回复【确认取消】才会真正提交。Web 订单页的待支付订单也有「取消订单」按钮，同样先展示罚金再确认。不可在线取消的订单会显示道旅客服电话/邮箱。

**Q：会真的扣钱吗？**
A：只有你本人打开 `paymentUrl` 在收银台完成支付才会扣款；只验价或创建订单后不支付，不会产生费用。

**Q：为什么重启程序后查不到之前的模拟订单？**
A：模拟订单只存在进程内存（`ORDER_DB`）中，重启即清空——这是未配置数据库时的默认内存模式；登录道旅后「查历史订单」走的是账号云端数据，`python rollinggo_book.py orders` 也可直接查询，跨设备/重启都在。

**Q：验价等待确认时能查订单吗？**
A：可以。在确认卡片阶段直接说「查我的订单」，系统会展示订单列表，并保留你的验价上下文（查完仍可回复【确认下单】，但锁价有时效，超时需重新验价）。

## 免责声明

本项目为开源项目：酒店数据与报价归道旅及对应供应商所有，价格与房态以下单收银台实时信息为准；请在你本人的道旅账号下使用真实接口，自行承担预订与支付后果。请勿将 `.env`、OAuth token 文件或任何真实密钥提交到公开仓库。
