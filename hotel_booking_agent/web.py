# -*- coding: utf-8 -*-
"""
酒店预订 Web 入口 —— FastAPI + Jinja2。

启动：
  pip install fastapi 'uvicorn[standard]' jinja2 requests dotenv
  uvicorn web:app --reload --host 127.0.0.1 --port 8000

功能：
  - GET  /              聊天页面（Jinja2 模板）
  - POST /api/chat      对话接口（JSON 入 user_input，出 reply）
  - POST /api/chat/stream  对话接口 SSE 版（先推阶段事件，最后推完整回复）
  - GET  /orders        订单列表页面
  - GET  /api/orders    订单列表 JSON
  - POST /api/orders/{id}/pay     模拟支付
  - POST /api/orders/{id}/cancel  取消订单（道旅真实订单给出手动指引）

存储：订单走 storage 层（SQLite 自动回退内存，配 MySQL 可选），评分/酒店缓存与 CLI 版共用同一套磁盘文件。
会话：session agent 池——前端携带 session_id，同一会话复用同一 Agent 实例（多轮对话状态保持），
      30 分钟不活跃自动回收，池容量上限 100（超出踢最旧），不同会话实例隔离互不污染。
"""

import os, sys, time, uuid, json, queue, asyncio, threading
from pathlib import Path

PROJ = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJ))
os.chdir(str(PROJ))

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from agent import HotelBookingAgent
import tools
import storage
import hotel_rating
import rollinggo_book as rlg

app = FastAPI(title="酒店预订 Agent Web 版", version="0.2.0")
templates = Jinja2Templates(directory=str(PROJ / "templates"))


# ---------- 会话 agent 池（多轮对话状态保持 + 会话间隔离）----------
_SESSION_TTL = 30 * 60     # 会话 30 分钟不活跃即回收
_SESSION_MAX = 100         # 池容量上限，超出踢最旧会话
_agents: dict = {}         # session_id -> {"agent": HotelBookingAgent, "ts": float}
_agents_lock = threading.Lock()


def _get_or_create_agent(session_id: str) -> HotelBookingAgent:
    """按 session_id 取/建 agent 实例；顺手清理过期会话。线程安全。"""
    now = time.time()
    with _agents_lock:
        # 回收过期会话（避免依赖后台定时任务）
        for k in [k for k, v in _agents.items() if now - v["ts"] > _SESSION_TTL]:
            del _agents[k]
        # 超容踢最旧
        if len(_agents) >= _SESSION_MAX:
            del _agents[min(_agents, key=lambda k: _agents[k]["ts"])]
        entry = _agents.get(session_id)
        if entry is None:
            entry = {"agent": HotelBookingAgent(), "ts": now}
            _agents[session_id] = entry
        entry["ts"] = now
        return entry["agent"]


def session_count() -> int:
    """当前活跃会话数（/api/health 用）。"""
    return len(_agents)


# ---------- 请求/响应模型 ----------
class ChatRequest(BaseModel):
    user_input: str
    session_id: str = ""      # 前端生成的会话标识（UUID）；同一会话复用同一 Agent 实例


class ChatResponse(BaseModel):
    reply: str
    current_order: str = ""       # 当前订单摘要（若有）
    session_id: str = ""          # 回传会话标识（前端未带时由服务端生成）
    storage_backend: str          # memory / sqlite / mysql
    storage_enabled: bool          # True = 真实持久化
    hotel_total: int = 0          # 本次搜索命中的酒店总数（>5 时前端显示“展开全部”）
    hotel_shown: int = 0          # 首屏已展示家数


class HotelMoreRequest(BaseModel):
    session_id: str
    offset: int = 0               # 从第 offset 家（0 基）开始补
    limit: int = 5                # 本次补全/展示家数


def _hotel_counts(agent: HotelBookingAgent, reply: str) -> tuple:
    """返回 (命中总数, 首屏已展示数)；仅对本次“推荐酒店”回复生效。

    不能只看 stage：推荐阶段里用户追问订单等，stage 可能仍是 recommend，
    但回复并不是酒店列表，此时不应给前端“展开全部”按钮。
    """
    state = agent.state
    # 用结果标题判断是否本次酒店列表，避免依赖具体引导语（引导语会随模板调整）
    if state.stage != "recommend" or "符合条件的酒店" not in reply:
        return 0, 0
    total = len(getattr(state, "search_results", None) or [])
    return total, min(tools.HOTEL_PREVIEW, total)


def _current_order_text(agent: HotelBookingAgent) -> str:
    """当前订单摘要（无订单返回空串）。"""
    order = agent.state.current_order
    if not order:
        return ""
    return (f"当前订单 {order.order_id}：{order.hotel_name} · "
            f"{order.room_type} · ¥{order.total_price:g}")


def _chat_payload(agent: HotelBookingAgent, reply: str,
                  session_id: str) -> dict:
    """普通接口与 SSE 接口共用的结果构造。"""
    total, shown = _hotel_counts(agent, reply)
    return {
        "reply": reply,
        "current_order": _current_order_text(agent),
        "session_id": session_id,
        "storage_backend": storage.storage_backend(),
        "storage_enabled": storage.storage_available(),
        "hotel_total": total,
        "hotel_shown": shown,
    }


# ---------- 页面路由 ----------
# HTML 页面一律 no-cache：前端模板更新后浏览器立即拿到新版，
# 避免缓存旧 JS（如旧版图片正则不认 .jpg-t 导致图片渲染成链接）
@app.get("/", response_class=HTMLResponse)
async def chat_page(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "storage_backend": storage.storage_backend(),
        "storage_enabled": storage.storage_available(),
    }, headers={"Cache-Control": "no-cache"})


@app.get("/orders", response_class=HTMLResponse)
async def orders_page(request: Request, filter: str = "ALL"):
    return templates.TemplateResponse(request, "orders.html", {
        "current_filter": filter.upper(),
        "storage_backend": storage.storage_backend(),
        "storage_enabled": storage.storage_available(),
    }, headers={"Cache-Control": "no-cache"})


# ---------- API 路由 ----------
@app.post("/api/chat", response_model=ChatResponse)
async def api_chat(req: ChatRequest):
    if not req.user_input.strip():
        raise HTTPException(status_code=400, detail="user_input 不能为空")
    # 会话池：同一 session_id 复用同一 Agent 实例，多轮对话状态（搜索结果/
    # 选中酒店/预订上下文）跨请求保持；未带 session_id 时由服务端生成
    session_id = (req.session_id or "").strip() or str(uuid.uuid4())
    agent = _get_or_create_agent(session_id)
    reply = agent.run(req.user_input.strip()) or "(助手无回复)"
    return ChatResponse(**_chat_payload(agent, reply, session_id))


@app.post("/api/chat/stream")
async def api_chat_stream(req: ChatRequest):
    """
    SSE 流式对话：先推阶段事件（event: stage），最后推完整回复（event: result）。

    agent.run 是同步阻塞函数，放后台线程跑；on_stage 回调把阶段事件写进
    queue，async 生成器轮询 queue 转 SSE 帧。事件协议：
      event: stage   data: {"stage": "searching", "text": "正在搜索酒店…"}
      event: result  data: {"reply": "...", ...}   （最终完整回复，ChatResponse 结构）
      event: error   data: {"message": "..."}
    """
    if not req.user_input.strip():
        raise HTTPException(status_code=400, detail="user_input 不能为空")
    session_id = (req.session_id or "").strip() or str(uuid.uuid4())
    agent = _get_or_create_agent(session_id)
    q: "queue.Queue" = queue.Queue()

    def on_stage(key: str, text: str) -> None:
        q.put(("stage", {"stage": key, "text": text}))

    def run_agent() -> None:
        try:
            agent.on_stage = on_stage
            reply = agent.run(req.user_input.strip()) or "(助手无回复)"
            q.put(("result", _chat_payload(agent, reply, session_id)))
        except Exception as e:            # 后台线程异常也要送到前端
            q.put(("error", {"message": f"{type(e).__name__}: {e}"}))
        finally:
            agent.on_stage = None
            q.put(("__end__", None))

    threading.Thread(target=run_agent, daemon=True).start()

    async def event_gen():
        while True:
            try:
                kind, data = q.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.1)   # 轮询间隔 100ms，阶段提示无感延迟
                continue
            if kind == "__end__":
                break
            yield (f"event: {kind}\n"
                   f"data: {json.dumps(data, ensure_ascii=False)}\n\n")

    return StreamingResponse(
        event_gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/hotels/more")
def api_hotels_more(req: HotelMoreRequest):
    """
    “展开全部”按需补全评分：为 search_results[offset:offset+limit] 补真实评分，
    再按首屏同一套规则格式化返回。

    - 评分只取真实数据（百度/高德 + 7 天磁盘缓存）；查不到仍显示“暂无评分”
    - 用同步 def，FastAPI 自动放线程池执行，不阻塞事件循环
    - 编号从 offset+1 延续，前端可直接追加到当前消息末尾
    """
    with _agents_lock:
        entry = _agents.get(req.session_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="会话不存在或已过期，请重新搜索")
    results = getattr(entry["agent"].state, "search_results", None) or []
    total = len(results)
    start = max(0, req.offset)
    if start >= total:
        return {"text": "", "next": start, "total": total, "done": True}
    limit = max(1, min(req.limit, 20))
    end = min(total, start + limit)
    chunk = results[start:end]
    try:
        # 只补尚无评分的酒店；缓存命中不产生请求
        hotel_rating.enrich_hotels_rating(chunk, limit=len(chunk))
    except Exception as e:
        print(f"[展开补评分失败，按暂无评分展示] {type(e).__name__}: {e}")
    return {
        "text": tools.format_hotel_slice(results, start, end - start),
        "next": end, "total": total, "done": end >= total,
    }


@app.get("/api/orders")
async def api_orders(filter: str = "ALL"):
    """返回订单列表（JSON）。filter: ALL / PENDING / FINISHED / CANCELLED"""
    result = tools.list_history_orders(filter)
    rows = result["rows"]
    # 简化：只返回 agent 页面需要的字段
    trimmed = []
    for r in rows:
        trimmed.append({
            "order_no": r.get("order_no"),
            "hotel_name": r.get("hotel_name"),
            "room_name": r.get("room_name"),
            "check_in": r.get("check_in"),
            "check_out": r.get("check_out"),
            "total_price": r.get("total_price"),
            "currency": r.get("currency"),
            "status_text": r.get("status_text"),
            "payment_url": r.get("payment_url", ""),
            "source": r.get("source", "mock"),
        })
    return {"rows": trimmed, "logged_in": result["logged_in"],
            "filter": result["filter"], "source": result["source"],
            "remote_error": result.get("remote_error")}


@app.post("/api/orders/{order_id}/pay")
async def api_pay(order_id: str):
    ok = tools.pay_order(order_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"订单 {order_id} 不存在")
    return {"ok": True, "order_id": order_id}


@app.post("/api/orders/{order_id}/cancel")
async def api_cancel(order_id: str):
    result = tools.cancel_order(order_id)
    if result == "not_found":
        raise HTTPException(status_code=404, detail=f"订单 {order_id} 不存在")
    if result == "remote_unsupported":
        # 道旅真实订单无取消接口，返回手动指引
        return {"ok": False, "order_id": order_id,
                "reason": "remote_unsupported",
                "guide": tools.real_order_cancel_guide(order_id)}
    return {"ok": True, "order_id": order_id}


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "storage_backend": storage.storage_backend(),
        "storage_enabled": storage.storage_available(),
        "active_sessions": session_count(),
        "rollinggo_logged_in": rlg.is_logged_in(),
    }


if __name__ == "__main__":
    import threading
    import webbrowser

    import uvicorn
    print(f"storage_backend={storage.storage_backend()}  "
          f"(SQLite/MySQL 可用={storage.storage_available()})")
    print("启动 Web 服务… http://127.0.0.1:8000")
    # 服务就绪后自动打开浏览器
    threading.Timer(1.5, webbrowser.open, args=("http://127.0.0.1:8000/",)).start()
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
