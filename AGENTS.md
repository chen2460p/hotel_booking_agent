# AGENTS.md — 酒店预订智能体

> 给 AI 编码助手的项目约定。人看 `README.md`，动手前看这里。
> 最近更新：2026-11-18。冲突时优先级：**安全红线 > 本文件 > README**。

## 项目一句话

Python 终端对话 Agent，跑通「搜酒店 → 选房型 → 验价锁房 → 真实下单 → 拿支付链接」。
LLM 负责意图识别，道旅 RollingGo 提供真实房价，**同一套工具层服务两种架构**（ReAct 单 Agent / Supervisor-Worker 多 Agent）。

## 动手前先记四条

1. **运行目录分清**：业务代码（`main.py`/`web.py`/`rollinggo_book.py`）在 `hotel_booking_agent/` 里执行；`tests/`、`scripts/`（含 `eval_nlu.py`、`preflight.py`）从**仓库根目录**执行。
2. **没有 Key 也能跑**，程序按降级矩阵自动回退。**降级不是 bug，不要去"修"它**。
3. **`.env` 分本地与线上两套**：本地改只影响本地；**线上由 `.gitea/workflows/deploy.yml` 用 Secrets 整份重写**，所以新增任何变量都必须同时改 `.env.example`、`deploy.yml` 的 echo 块，并在仓库 Secrets 配值（不配则线上为空、静默降级）。`.env` 本身不提交，也禁止读取/回显其中密钥；**`orders.db*` 是运行态真实订单，不碰**。
4. **push 到 `main` 会自动部署**（`.gitea/workflows/deploy.yml`），改坏 main 等于改坏线上。

## 环境与启动

```powershell
$env:PYTHONUTF8 = "1"          # 中文 Windows 终端必开，否则横幅和 emoji 乱码
cd hotel_booking_agent

# 首次（只装一次）
pip install -r requirements-web.txt

# 三个入口，按需选一个
python web.py                  # Web 界面 http://127.0.0.1:8000（也可双击 启动前端.bat）
python main.py                 # 终端·单 Agent（真实下单链路在这）
python main_multi.py           # 终端·多 Agent 协作
```

Web 首页是 React 构建产物，`python web.py` 之前必须先构建一次（或走 `npm run dev`）：

```powershell
cd hotel_booking_agent/frontend
npm ci
npm run build            # 产物 frontend/dist；未构建时 GET / 返回 500 构建提示页
npm run dev              # 只调界面：:5173，自动把 /api、/orders 代理到 :8000
```

- Python 3.10+（开发环境 Windows 11 + 3.13）
- `requirements-web.txt` 同时包含 Web 依赖（fastapi/uvicorn）与通用依赖（requests/python-dotenv）；`main.py` / `main_multi.py` **不需要** Web 那三个
- **node 只在前端目录用**：`hotel_booking_agent/frontend`；CLI 入口和 Docker 运行时都不需要 node（镜像里是独立的构建阶段）
- `templates/` 目录已删除：聊天页和订单页**都是** `frontend/dist` 的构建产物（Vite 多页构建：`index.html` + `orders.html`），不要再加 Jinja 模板
- 首次真实下单要授权一次：`python rollinggo_book.py login`（token 落 `~/.hotel-cli/token.json`，有效期以 `python rollinggo_book.py status` 为准）
- **时区固定 `Asia/Shanghai`**（Docker 里靠 `TZ`）：口语日期、跨月推断都用本地 `datetime.now()`，不要改成 UTC
- Docker：仓库根目录 `docker compose up -d --build`，先 `python scripts/preflight.py` 自检；线上宿主端口 **9122**，容器内 8000

## 模型与 NLU

- 默认模型 **`deepseek-v4.1-flash`**（120 条评测全对、比 qwen3.8-flash 快约 2.5 倍）；`DASHSCOPE_MODEL` 可覆盖（`qwen-flash` / `qwen3.8-flash` 等）
- 单次对话读取超时 **`DASHSCOPE_TIMEOUT=30`（秒）**，到点自动回退规则引擎，防长尾卡死
- **分工原则**：模型只负责"抽取"；**确定性逻辑放代码**——日期归一、城市笔误、否定约束、多轮改口都在 `nlu_corrections.py` / `llm.py`，不要往 prompt 里堆规则
- 改 prompt 或后处理后，至少跑 20 条抽查（输出到临时文件），合并前跑全量对照基线

## 测试

**一条命令跑完全部离线单测**（约 10 秒）：

```powershell
python tests/run_all.py
```

| 命令 | 说明 |
|---|---|
| `python tests/run_all.py` | 离线单测总入口，不含需要服务的冒烟 |
| `python tests/test_api_smoke.py` | **需要先起服务**（默认 `http://127.0.0.1:8000`，`BASE_URL` 可覆盖）；不是离线用例。**先 `npm run build`**，否则 `/` 是 500 构建提示页 |
| `python tests/test_cancel_unit.py` | 单个文件都能直接跑；装了 pytest 可 `pytest tests/ -v --ignore=tests/test_api_smoke.py`（当前环境未必装） |
| `RUN_LLM_TESTS=1 python tests/test_api_smoke.py` | 才跑真实模型/SSE 用例；慢且不确定，**不要塞进回归** |
| `cd hotel_booking_agent/frontend && npm run build` | 前端类型检查（`tsc --noEmit`）+ 构建；改过 `frontend/` 必须跑通 |

**测试红线**：

- 自动化测试**只允许真实调用只读接口**（`search` / `getHotelDetail` / 查单）；**禁止真实调用** `price_confirm`、`create_booking`（REST 端点 `hotelbook`）、`confirm_cancel`（会产生真实锁价/订单/取消）；这些函数只能在 **mock** 下被测。`pay_order` 只接受模拟单，真实单走 `/pay` 会被 409 拒绝。
- 涉及订单的测试必须 mock `storage` / `tools`，**不得写 `orders.db`**、不得污染 `cache/`
- `test_api_smoke.py` 需要服务；纯离线回归用 `run_all.py`
- 新增测试文件命名为 `tests/test_<主题>_unit.py`，`run_all.py` 会自动发现；需要服务的冒烟单独命名

**断言规则**：状态码、字段结构、协议头可以硬断言；自然语言回复只断言"非空 + 结构正确"，绝不逐字比对。

**评测规范**：

- 快速冒烟：`python scripts/eval_nlu.py --limit 10 --no-pipeline --out <临时文件> --json-out <临时文件>`；**不要用默认路径**——会覆盖正式报告
- 全量：`python scripts/eval_nlu.py`（120 条 × 3 模型 × 2 模式，约 30 分钟）
- 对照修复前基线看 `scripts/eval_nlu_baseline.md`；最新结果看 `scripts/eval_nlu_report.md`
- 复现先核对 `case_sha256`（应为 `821d6080e1a9bdcc`）；单次运行有采样波动，1 条差异别当结论

## 代码分层（改之前先确认改在哪一层）

| 层 | 文件 | 规矩 |
|---|---|---|
| 共用纯逻辑 | `agent_common.py`、`text_rules.py`、`text_utils.py`、`models.py` | **单/多 Agent 都依赖**。动这里必须两个入口都过一遍 |
| NLU 层 | `llm.py`、`nlu_corrections.py`、`geo_data.py` | 模型抽取 + 确定性后处理（改口/否定/跨月）；改完跑评测 |
| 业务编排 | `agent.py`、`multi_agent.py` | 只放状态机与流程编排，选择/解析/渲染细节丢回 `agent_common` |
| 工具层 | `tools.py`、`rag.py` | 三级回退的统一入口 / 评价检索。新增数据源改这里，别在 `agent.py` 里直接调外部 API |
| 外部适配 | `rollinggo_mcp.py`、`rollinggo_book.py`、`amap_poi.py`、`ctrip_crawler.py`、`hotel_rating.py` | 一个文件对一个外部服务，超时/异常必须在内部消化成降级结果 |
| 存储 | `storage.py` | SQLite / MySQL / 内存自动回退。表结构变更**只加列**，不改不删 |
| Web 前端 | `frontend/src/`（React + TypeScript） | 只渲染 `hotel_view` 的结构化字段；请求统一走 `api/client.ts`、SSE 统一走 `api/sse.ts`，组件里不直接 fetch |
| 入口 | `main.py`、`main_multi.py`、`web.py`、`cli_common.py` | 只做输入输出循环/终端展示，不写业务逻辑 |

**并发约束**：`web.py` 的会话池、SQLite 句柄都在单进程内存里，**禁止 `--workers > 1`**（Dockerfile 已注明）。多 worker = 多轮状态丢失、订单互相不可见。

## 六条硬红线

1. **绝不在程序内完成支付。** 下单接口只返回 `paymentUrl`，用户自己去收银台付。任何"帮我自动付掉"的改动都不要做。
2. **验价与下单必须分离。** `price_confirm` 拿 `referenceNo` → 用户二次确认 → 才 `create_booking`（REST 端点 `/hotelbook`）。不许合并成一步。
3. **真实订单取消必须两步。** `pre_cancel` 查罚金/退款并拿 10 分钟凭证 → 用户明确回"确认取消" → 才 `confirm_cancel`。不许一步直取消。
4. **酒店/房型列表只走结构化下发。** Web 端用 `hotel_id + revision`、`room_no + plan_no + rooms_revision` 选；**不要退回解析回复文本里的"2-1"或序号猜**，旧列表返回 409 是设计，不是故障。
5. **`created_at` / `paid_at` 永不互相覆盖。** 时间字段语义一旦糊了，订单审计就废了。
6. **自动化/脚本不得调用真实交易接口。** 任务本身没要求真实下单时，只做搜索、详情、只读查询；验价/下单/取消必须有人明确确认。

## 降级语义（看到这些现象别当故障）

| 现象 | 真实原因 |
|---|---|
| 意图识别像模板、逻辑走规则 | 没配 `DASHSCOPE_API_KEY`，正常降级 |
| 日志 `403 AccessDenied.Unpurchased` | 该模型未开通/无权限（账号侧，不是代码） |
| 日志 `400 Arrearage` | 账号欠费，付费模型一律拒绝 |
| 日志 `429 limit_requests` | 限流/额度超限，等一会儿或降并发 |
| 回复突然改走规则引擎 | `DASHSCOPE_TIMEOUT`（默认 30s）到点自动回退，防长尾 |
| 酒店是"XX市模拟酒店" | 没配 `ROLLINGGO_API_KEY` 或接口异常，回退内置模拟库 |
| 有酒店名但没价格 | 只配了 `AMAP_API_KEY`，高德 POI 回退，无报价 |
| 走到下单却提示"模拟下单" | 有 `ROLLINGGO_API_KEY` 但没做 OAuth 登录 |
| 评分显示"暂无评分" | 评分源不可用，或命中**负缓存**（确认无评分缓存 3 天，正常源 7 天），正常 |
| RAG 走 TF-IDF 而非 Embedding | 没装 dashscope 或没配 Key，正常 |
| `storage_backend=memory` | MySQL/SQLite 不可用，自动回退内存（重启丢模拟单） |
| "历史仍在、上下文已重置" | 会话池 30 分钟 TTL 到期，前端行为正确 |

## API / 前端契约

| 接口 | 用途 | 关键约束 |
|---|---|---|
| `GET /` / `GET /orders` | 聊天页 / 订单页 | 都是 `frontend/dist` 构建产物；订单筛选 `?filter=` 由前端读 URL |
| `POST /api/chat` | 对话（返回 `hotel_view` / `rooms_view` / `stage`） | `session_id` 复用同一 Agent 实例 |
| `POST /api/chat/stream` | 对话 SSE | 与 `/api/chat` 同逻辑，先推 `stage` |
| `GET /api/session/{id}` | 会话存活 / 阶段 / 房型视图 | 只读，不创建会话 |
| `GET /api/session/{id}/hotels` | 结构化酒店列表 | 带 `revision` 校验，旧列表 409 |
| `POST /api/session/{id}/select` | 按 `hotel_id + revision` 选店 | 不经过 LLM，旧列表 409 |
| `POST /api/session/{id}/room` | 按 `room_no + plan_no + rooms_revision` 选房 | `plan_key` 即 `rate_plan_id`；旧房型列表 409 |
| `POST /api/hotels/more` | 展开更多酒店 | 复用当前会话的 `search_results` |
| `GET /api/orders` | 订单列表 | 真实单以远端状态为准 |
| `POST /api/orders/{id}/pay` | ⚠️ 仅模拟单：标记本地已支付 | **真实单返回 409**；真实支付只走收银台链接 |
| `POST /api/orders/{id}/pre-cancel` | ⚠️ 真实交易：取消询价 | 只查罚金/拿 10 分钟凭证，**不提交取消** |
| `POST /api/orders/{id}/cancel` | ⚠️ 真实交易：提交取消 | 必须带前置凭证，等价 `confirm_cancel` |
| `GET /api/health` | 健康检查 | 存储后端 / 会话数 / 道旅登录态 |

前端（`hotel_booking_agent/frontend/src`，构建产物由 `web.py` 托管）注意：

- 道旅 CDN 需要 `referrer=no-referrer`：页面 `index.html` 的 `<meta>` + 每张 `<img>` 的 `referrerPolicy`，缺一处图片就 403
- **图片只能走 `lib/imgQueue.ts`**：串行 + 可视区懒加载 + 失败重试一次 + 退化成链接。别直接给 `<img>` 赋 `src`，CDN 有并发风控（实测 8 张并发 5 张 403）
- **不要开 `React.StrictMode`**：开发环境 effect 双跑会让同一张图入队两次，正好撞风控
- `/api/chat/stream` 是 POST + SSE，`EventSource` 用不了；解帧只在 `api/sse.ts` 做（`TextDecoder` 必须 `stream: true`）
- 聊天记录存 `localStorage`（键 `hb_sessions_v1`），字段是历史格式，**只加不改**；服务端 30 分钟无活动会丢上下文，要提示"历史仍在、上下文已重置"
- **卡片只渲染结构化字段**，不要退回解析回复文本；选店走 `hotel_id + revision`；卡片能否点击由 `activeRevision` 派生，不手工改 DOM
- 选房走 `rooms_view` 的 `room_no + plan_no + rooms_revision`，**提交时不要带 `guest_name`**：带了会直接进入验价锁价，那是用户补完姓名后的下一步，不是一个按钮该干的事
- 订单页（`src/orders/`）的真实单取消仍是**两步**：先 `pre-cancel` 拿罚金和 10 分钟凭证并把罚金/预计退款摆给用户看，用户点「确认取消」才 `cancel` 带凭证提交；模拟单的「标记支付」只允许对 `source=mock` 显示

## 编码约定

- **注释和 docstring 用中文**，跟现有风格一致；模块头写清"这个文件解决什么问题"
- 面向 Windows 终端输出要按**显示宽度**算（中文占 2 列），别用 `len()` 直接对齐
- 所有文件 UTF-8；`requirements-web.txt` 里区分"Web 依赖"和"通用依赖"两段，别混
- 新增外部服务 → 新建一个适配器文件，并在 `tools.py` 里挂到回退链上
- **不引入新依赖**，除非确有必要并同步 `requirements-web.txt` 与 Dockerfile。**前端例外**：node 依赖只允许出现在 `hotel_booking_agent/frontend`，`package.json` + `package-lock.json` 一起提交，锁文件不许手改
- **PII 不进日志/回复**：`recent_guests` 含姓名/手机/邮箱，只展示必要字段；token、`.env` 值绝不回显
- 改 `.env.example` 必须同步 `README.md` 配置表；新增模块必须同步 README 模块表与本文件分层表

## 改哪层 → 至少跑哪些测试

| 改动 | 测试 |
|---|---|
| `llm.py` / `nlu_corrections.py` | `test_nlu_postprocess_unit`、`test_corrections_unit`、`test_date_rules_unit` + `scripts/eval_nlu.py` |
| `agent_common.py` / `models.py` | `test_selection_unit`、`test_rooms_view_unit`、`test_corrections_unit`、`test_cancel_unit` |
| `agent.py` | `test_selection_unit`、`test_rooms_view_unit`、`test_corrections_unit` |
| `web.py` | 起服务后 `test_api_smoke.py` + `test_selection_unit` + `test_rooms_view_unit` |
| `tools.py` / `rollinggo_book.py` | `test_cancel_unit`、`test_order_time_unit`、`test_rate_change_recovery_unit`、`test_pay_guard_unit` |
| `storage.py` | `test_order_time_unit`、`test_cancel_unit` |
| `frontend/`（React 前端） | `npm run build`（含 `tsc --noEmit`）+ 浏览器手工验收 + `test_api_smoke.py` |

## 提交前自检

```powershell
python tests/run_all.py                                      # 离线单测（约 10 秒）
cd hotel_booking_agent/frontend; npm run build; cd ../..      # 前端类型检查 + 构建（web.py 托管 dist）
python tests/test_api_smoke.py                               # 需先起服务（dist 已构建，否则 / 是 500）
Select-String -LiteralPath .gitignore -Pattern "orders.db"   # 确认仍被忽略
git status --short                                           # 不该出现 .env / *.db / cache/ / node_modules/
```

不要提交：`.env`、`orders.db` 及 `-shm`/`-wal`/`.bak`、`hotel_booking_agent/cache/`、`__pycache__/`、`个人资料/`、含真实订单的预览图。

**评测产物是资产，随代码提交**：`scripts/eval_nlu_cases.json`、`eval_nlu_report.md`、`eval_nlu_results.json`、`eval_nlu_baseline.md`。临时跑分用 `--out` / `--json-out` 分流到临时文件，别覆盖正式报告。
