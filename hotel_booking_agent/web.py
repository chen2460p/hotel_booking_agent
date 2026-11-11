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
  - GET  /api/session/{session_id}  查询会话在服务端是否仍存活（前端恢复本地历史用）
  - GET  /api/session/{session_id}/hotels  结构化酒店列表（卡片渲染用，带 revision）
  - POST /api/session/{session_id}/select  按 hotel_id + revision 选店（不经过 LLM/序号）
  - GET  /orders        订单列表页面
  - GET  /api/orders    订单列表 JSON
  - POST /api/orders/{id}/pay     模拟支付
  - POST /api/orders/{id}/pre-cancel  真实订单取消询价（罚金 + confirmId）
  - POST /api/orders/{id}/cancel      取消订单（真实订单需带询价凭证并确认）

存储：订单走 storage 层（SQLite 自动回退内存，配 MySQL 可选），评分/酒店缓存与 CLI 版共用同一套磁盘文件。
会话：session agent 池——前端携带 session_id，同一会话复用同一 Agent 实例（多轮对话状态保持），
      30 分钟不活跃自动回收，池容量上限 100（超出踢最旧），不同会话实例隔离互不污染。
"""

import os, sys, time, uuid, json, queue, asyncio, threading
from pathlib import Path
from typing import Optional

PROJ = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJ))
os.chdir(str(PROJ))

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from agent import HotelBookingAgent, SelectionError
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


def _peek_agent(session_id: str):
    """只读获取已存在的会话（不创建）；顺手回收过期会话并续期。

    返回 {"agent": ..., "ts": ...} 或 None。结构化列表/选店接口用它，
    避免前端一个 GET 就把不存在的会话创建出来。
    """
    sid = (session_id or "").strip()
    if not sid:
        return None
    now = time.time()
    with _agents_lock:
        entry = _agents.get(sid)
        if entry is not None and now - entry["ts"] > _SESSION_TTL:
            del _agents[sid]
            entry = None
        elif entry is not None:
            entry["ts"] = now
        return entry


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
    stage: str = ""               # 当前阶段（intent/recommend/booking/...）
    # 酒店列表视图（本次回复是列表时才有）：结构化 items + revision + 文案，
    # 前端据此渲染卡片并按 hotel_id 选店，不再解析回复文本
    hotel_view: Optional[dict] = None


class HotelMoreRequest(BaseModel):
    session_id: str
    offset: int = 0               # 从第 offset 家（0 基）开始补
    limit: int = 5                # 本次补全/展示家数
    revision: str = ""            # 搜索结果版本号；带上则校验，避免展开到旧列表


class SelectHotelRequest(BaseModel):
    """结构化选店：只认稳定 hotel_id + 列表版本，序号仅用于展示。"""
    hotel_id: str
    revision: str = ""


class CancelRequest(BaseModel):
    """真实订单确认取消：凭证来自 /pre-cancel，必须显式 confirm=true。"""
    confirm: bool = False
    confirm_id: str = ""
    booking_id: str = ""
    description: str = ""


def _hotel_item(h) -> dict:
    """单家酒店的结构化视图：前端卡片只渲染这些字段，不再解析文本。"""
    return {
        "hotel_id": h.hotel_id,
        "name": h.name,
        "star": h.star,
        "price_per_night": h.price_per_night,
        "rating": h.rating,
        "review_count": h.review_count,
        "rating_source": h.rating_source,
        "address": h.address,
        "distance_m": h.distance_m,
        "distance_to": h.distance_to,
        "facilities": list(h.facilities[:4]),
        "room_types": list(h.room_types),
        "image_url": h.image_url,
    }


def _hotel_view(agent: HotelBookingAgent) -> Optional[dict]:
    """本次回复若为酒店列表，返回结构化视图；否则 None。

    以 agent 登记的 last_view 为准（而不是靠匹配回复文案），
    这样推荐阶段用户追问订单等非列表回复不会误挂“展开全部”。
    """
    view = getattr(agent.state, "last_view", None) or {}
    if view.get("kind") != "hotels":
        return None
    results = getattr(agent.state, "search_results", None) or []
    total = len(results)
    if not total:
        return None
    shown = min(tools.HOTEL_PREVIEW, total)
    return {
        "revision": view.get("revision") or getattr(agent.state, "search_revision", ""),
        "total": total,
        "shown": shown,
        "intro": view.get("intro", ""),
        "outro": view.get("outro", ""),
        "items": [_hotel_item(h) for h in results[:shown]],
    }


def _hotel_counts(agent: HotelBookingAgent) -> tuple:
    """返回 (命中总数, 首屏已展示数)；仅对本次“酒店列表”回复生效。"""
    view = _hotel_view(agent)
    if view is None:
        return 0, 0
    return view["total"], view["shown"]


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
    total, shown = _hotel_counts(agent)
    return {
        "reply": reply,
        "current_order": _current_order_text(agent),
        "session_id": session_id,
        "storage_backend": storage.storage_backend(),
        "storage_enabled": storage.storage_available(),
        "hotel_total": total,
        "hotel_shown": shown,
        "stage": getattr(agent.state, "stage", ""),
        "hotel_view": _hotel_view(agent),
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


@app.get("/api/session/{session_id}")
async def api_session_info(session_id: str):
    """
    查询会话在服务端是否还存活（不创建 Agent；命中时顺手续期活跃时间）。

    前端把聊天记录缓存在浏览器 localStorage：从订单页返回/刷新后先恢复气泡，
    但服务端 Agent 只在 30 分钟内有状态；前端用这个接口判断是否需要提示
    “历史记录还在，但继续对话会从新上下文开始”。
    """
    sid = (session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="session_id 不能为空")
    entry = _peek_agent(sid)
    if entry is None:
        return {"exists": False, "session_id": sid}
    agent = entry["agent"]
    state = agent.state
    return {
        "exists": True,
        "session_id": sid,
        "stage": state.stage,
        "current_order": _current_order_text(agent),
        "hotel_total": len(getattr(state, "search_results", None) or []),
        "search_revision": getattr(state, "search_revision", ""),
        "last_active": entry["ts"],
    }


@app.get("/api/session/{session_id}/hotels")
def api_session_hotels(session_id: str, offset: int = 0, limit: int = 5,
                       revision: str = ""):
    """结构化酒店列表（卡片渲染 / 展开全部 / 恢复历史共用）。

    - 只读，不创建会话；返回 items（字段与首屏一致）
    - 带 revision 时做版本校验：不一致返回 409，前端提示列表已更新
    - 展示前按需补真实评分（只补没有评分的，7 天缓存）
    """
    entry = _peek_agent(session_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="会话不存在或已过期，请重新搜索")
    state = entry["agent"].state
    results = getattr(state, "search_results", None) or []
    current_rev = getattr(state, "search_revision", "")
    if revision and current_rev and revision != current_rev:
        raise HTTPException(status_code=409,
                            detail="酒店列表已更新，请按最新列表重新选择")
    total = len(results)
    start = max(0, offset)
    if start >= total:
        return {"revision": current_rev, "total": total, "offset": start,
                "items": [], "done": True}
    limit = max(1, min(limit, 20))
    end = min(total, start + limit)
    chunk = results[start:end]
    try:
        # 只补尚无评分的酒店；缓存命中不产生请求
        hotel_rating.enrich_hotels_rating(chunk, limit=len(chunk))
    except Exception as e:
        print(f"[展开补评分失败，按暂无评分展示] {type(e).__name__}: {e}")
    return {
        "revision": current_rev,
        "total": total,
        "offset": start,
        "items": [_hotel_item(h) for h in chunk],
        "done": end >= total,
    }


@app.post("/api/session/{session_id}/select", response_model=ChatResponse)
def api_select_hotel(session_id: str, req: SelectHotelRequest):
    """按 hotel_id + revision 选店：不经过 LLM，也不按序号猜。

    版本不一致（旧列表）→ 409；酒店已不在列表 → 404；
    成功返回房型报价回复与最新会话状态。
    """
    entry = _peek_agent(session_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="会话不存在或已过期，请重新搜索")
    agent = entry["agent"]
    try:
        reply = agent.select_hotel_by_id(req.hotel_id, req.revision)
    except SelectionError as e:
        # stale=旧列表（可重取最新列表）；其余=酒店不存在/无列表
        status = 409 if e.code == "stale" else 404
        raise HTTPException(status_code=status, detail=e.message)
    return ChatResponse(**_chat_payload(agent, reply, session_id))


@app.post("/api/hotels/more")
def api_hotels_more(req: HotelMoreRequest):
    """
    “展开全部”按需补全评分并返回结构化 items（兼容旧前端的 text 字段）。

    - 评分只取真实数据（百度/高德 + 7 天磁盘缓存）；查不到仍显示“暂无评分”
    - 用同步 def，FastAPI 自动放线程池执行，不阻塞事件循环
    - 带 revision 时做版本校验：列表已更新则 409
    """
    entry = _peek_agent(req.session_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="会话不存在或已过期，请重新搜索")
    state = entry["agent"].state
    results = getattr(state, "search_results", None) or []
    current_rev = getattr(state, "search_revision", "")
    if req.revision and current_rev and req.revision != current_rev:
        raise HTTPException(status_code=409,
                            detail="酒店列表已更新，请按最新列表重新选择")
    total = len(results)
    start = max(0, req.offset)
    if start >= total:
        return {"revision": current_rev, "text": "", "items": [],
                "next": start, "total": total, "done": True}
    limit = max(1, min(req.limit, 20))
    end = min(total, start + limit)
    chunk = results[start:end]
    try:
        # 只补尚无评分的酒店；缓存命中不产生请求
        hotel_rating.enrich_hotels_rating(chunk, limit=len(chunk))
    except Exception as e:
        print(f"[展开补评分失败，按暂无评分展示] {type(e).__name__}: {e}")
    return {
        "revision": current_rev,
        "items": [_hotel_item(h) for h in chunk],
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
            "status": r.get("status", ""),
            "status_text": r.get("status_text"),
            "status_bucket": tools.order_status_bucket(r),
            "payment_url": r.get("payment_url", ""),
            "payment_deadline": r.get("payment_deadline", ""),
            "create_time": r.get("create_time", ""),
            "paid_time": r.get("paid_time", ""),
            "updated_at": r.get("updated_at", ""),
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


@app.post("/api/orders/{order_id}/pre-cancel")
async def api_pre_cancel(order_id: str):
    """真实订单取消询价：返回罚金、confirmId/bookingId（10 分钟有效）。"""
    result = tools.pre_cancel_real_order(order_id)
    if result.get("reason") == "not_found":
        raise HTTPException(status_code=404, detail=result.get("message"))
    return result


@app.post("/api/orders/{order_id}/cancel")
async def api_cancel(order_id: str, req: Optional[CancelRequest] = None):
    """
    取消订单：
    - 本地模拟单：直接取消（兼容旧前端）
    - 道旅真实单：必须带 pre-cancel 返回的 confirm_id + booking_id 且 confirm=true
    """
    order = tools.find_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"订单 {order_id} 不存在")

    if order.source == "rollinggo":
        if not req or not req.confirm or not req.confirm_id or not req.booking_id:
            return {
                "ok": False,
                "order_id": order_id,
                "reason": "confirmation_required",
                "message": "真实订单取消需先询价，并在确认罚金后提交取消凭证。",
            }
        result = tools.confirm_cancel_real_order(
            order_id, req.booking_id, req.confirm_id, req.description)
        return result

    result = tools.cancel_order(order_id)
    if result == "not_found":
        raise HTTPException(status_code=404, detail=f"订单 {order_id} 不存在")
    if result == "remote_unsupported":
        return {"ok": False, "order_id": order_id,
                "reason": "remote_unsupported",
                "message": tools.real_order_cancel_guide(order_id)}
    return {"ok": True, "order_id": order_id, "message": "订单已取消。"}


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
