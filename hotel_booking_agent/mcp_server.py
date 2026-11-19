# -*- coding: utf-8 -*-
"""酒店预订 MCP Server —— 把本项目的只读能力按 MCP 协议暴露给外部 Agent。

这个文件解决什么问题
--------------------
Agent 生态里两件事经常混在一起：
  1. **Host/Client 侧**：我们的 Agent 去调用别人的远程 MCP（`rollinggo_mcp.py`，
     手写 JSON-RPC：initialize → tools/list → tools/call）。
  2. **Server 侧**：把**我们自己的**能力按同一套协议暴露出去，让 Claude Desktop /
     Cursor / 别的 Agent 直接把「搜酒店 / 查房型 / 查评价 / 查订单」当工具用。

本文件只做第 2 件事，而且**只做协议适配**：业务逻辑仍然全部在 `tools.py` /
`rag.py` / `storage.py` 里，这里一行都不复制。新增能力只改 `TOOLS` 注册表。

设计边界（面试和代码评审都会被问，所以写死在这里）
--------------------------------------------------
- **只读优先**：注册进去的 4 个工具全部只读（搜索 / 详情 / 评价 / 查单）。
  验价、下单、取消**故意不暴露**——它们要用户二次确认、要凭证、
  会动真钱，属于 Host 侧的人工闸门，不能变成模型随时可调的 tool。
- **不进 MCP 的还有**：会话池、ReAct 状态机、revision 乐观锁、前端渲染。
  这些是"跨轮次状态"和"UI"，MCP 的语义是一次性工具调用，塞进来只会把
  有状态的东西暴露给任意客户端。
- **不回显内部异常**：调用失败只回结构化错误，堆栈留在服务端日志。
- **不记 PII**：审计日志只记工具名、参数键、耗时、成败，不记参数值。

两种传输
--------
- `stdio`：给 Claude Desktop / 本地客户端用（`python mcp_server.py`）。
- `Streamable HTTP`：`POST /mcp`，由 `web.py` 挂载（Docker 里已经有了，
  不需要再起一个服务）。无状态，不下发 `Mcp-Session-Id`。

用法
----
    python mcp_server.py                       # stdio，给本地 MCP 客户端
    python mcp_server.py --transport http --port 8010   # 单独跑 HTTP
    # 生产：随 web.py 一起起，POST /mcp

环境变量
--------
    MCP_AUTH_TOKEN  非空则 HTTP 侧强制 Bearer 鉴权；为空表示不鉴权（仅建议本地）
    MCP_AUDIT_FILE  审计日志路径，默认 cache/mcp_audit.jsonl
"""

from __future__ import annotations

import hmac
import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

import tools

PROJ = Path(__file__).resolve().parent

SERVER_NAME = "hotel-booking-mcp"
SERVER_VERSION = "1.0.0"
# 与 rollinggo_mcp.py 的客户端保持同一代协议；客户端请求别的版本时回退到最新支持版
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")

MAX_CONCURRENCY = 4
_semaphore = threading.BoundedSemaphore(MAX_CONCURRENCY)
_audit_lock = threading.Lock()

# 故意不暴露的写操作：留在这里是为了让"为什么不做"有据可查
WITHHELD_TOOLS = ("price_confirm", "create_booking", "confirm_cancel", "pay_order")


# ========== 错误与工具异常 ==========


class _ToolError(Exception):
    """工具级错误：回给模型的是 isError 内容，不是协议错误，便于模型自我纠正。"""


class _RpcError(Exception):
    """协议级错误：方法不存在、参数结构不对等，直接回 JSON-RPC error。"""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ========== 参数校验（工具契约） ==========

_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def _coerce(key: str, value: Any, spec: Dict[str, Any]) -> Tuple[Any, Optional[str]]:
    """按 schema 校验单个值；数字型允许从字符串强转（模型常把 300 写成 "300"）。"""
    want = spec.get("type")
    if want is None:
        return value, None
    check = _TYPE_CHECKS.get(want)
    if check is None:
        return value, None
    if not check(value):
        if want in ("number", "integer") and isinstance(value, str):
            try:
                value = int(value) if want == "integer" else float(value)
            except ValueError:
                return value, f"{key} 必须是{'整数' if want == 'integer' else '数字'}"
        else:
            return value, f"{key} 类型应为 {want}"
    if "enum" in spec and value not in spec["enum"]:
        return value, f"{key} 只能是 {spec['enum']} 之一"
    if want in ("number", "integer"):
        if "minimum" in spec and value < spec["minimum"]:
            return value, f"{key} 不能小于 {spec['minimum']}"
        if "maximum" in spec and value > spec["maximum"]:
            return value, f"{key} 不能大于 {spec['maximum']}"
    if want == "string":
        if "minLength" in spec and len(value) < spec["minLength"]:
            return value, f"{key} 不能为空"
        if "maxLength" in spec and len(value) > spec["maxLength"]:
            return value, f"{key} 长度不能超过 {spec['maxLength']}"
        if spec.get("format") == "date" and not _is_date(value):
            return value, f"{key} 必须是 YYYY-MM-DD 格式的日期"
    return value, None


def _is_date(text: str) -> bool:
    try:
        datetime.strptime(text, "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


def validate_args(args: Any, schema: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """按 schema 校验并清洗入参；返回（干净参数, 错误列表）。

    `additionalProperties: False` 时未知参数直接拒绝——模型编参数是常态，
    静默忽略会让它在错误假设上继续跑。
    """
    if not isinstance(args, dict):
        return {}, ["arguments 必须是 JSON 对象"]
    props: Dict[str, Dict[str, Any]] = schema.get("properties", {})
    errors: List[str] = []

    for key in schema.get("required", []):
        value = args.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            errors.append(f"缺少必填参数 {key}")

    if schema.get("additionalProperties") is False:
        for key in args:
            if key not in props:
                errors.append(f"未知参数 {key}")

    clean: Dict[str, Any] = {}
    for key, value in args.items():
        spec = props.get(key)
        if spec is None or value is None:
            continue
        value, error = _coerce(key, value, spec)
        if error:
            errors.append(error)
        else:
            clean[key] = value
    return clean, errors


# ========== 业务工具（全部只读） ==========


def _hotel_brief(hotel: Any) -> Dict[str, Any]:
    """酒店摘要：只给决策必需的字段，避免把整份详情灌进模型上下文。"""
    return {
        "hotel_id": hotel.hotel_id,
        "name": hotel.name,
        "city": hotel.city,
        "address": hotel.address,
        "star": hotel.star,
        "price_per_night": round(float(hotel.price_per_night or 0), 2),
        "rating": hotel.rating,
        "review_count": hotel.review_count,
        "rating_source": hotel.rating_source,
        "facilities": list(hotel.facilities or [])[:8],
        "room_types": list(hotel.room_types or [])[:6],
        "distance_m": hotel.distance_m,
        "distance_to": hotel.distance_to,
        "image_url": hotel.image_url,
    }


def _tool_search_hotels(args: Dict[str, Any]) -> Dict[str, Any]:
    from models import BookingParams

    if args["check_out"] <= args["check_in"]:
        raise _ToolError("离店日期必须晚于入住日期")
    params = BookingParams(
        city=args["city"],
        check_in=args["check_in"],
        check_out=args["check_out"],
        max_price=args.get("max_price"),
        min_star=args.get("min_star"),
        location=args.get("location"),
        keyword=args.get("keyword"),
    )
    hotels = tools.search_hotels(params) or []
    return {
        "count": len(hotels),
        "city": params.city,
        "check_in": params.check_in,
        "check_out": params.check_out,
        "hotels": [_hotel_brief(h) for h in hotels[:15]],
        "note": "只读结果，价格以实时房源为准；下单需回到带二次确认的 Host 侧。",
    }


def _tool_get_hotel_detail(args: Dict[str, Any]) -> Dict[str, Any]:
    hotel = tools.get_hotel_detail(args["hotel_id"], args.get("check_in"),
                                   args.get("check_out"))
    if hotel is None:
        raise _ToolError(f"未找到酒店 {args['hotel_id']}（可能已过期，请重新搜索）")
    plans = []
    for plan in (hotel.rate_plans or [])[:20]:
        plans.append({
            "room_no": plan.room_no,
            "plan_no": plan.plan_no,
            "room_name": plan.room_name,
            "price_per_night": round(float(plan.price_per_night or 0), 2),
            "meal": plan.meal,
            "cancelable": plan.cancelable,
            "cancel_policy": plan.cancel_policy,
            "on_request": plan.on_request,
            "image_urls": list(plan.image_urls or [])[:3],
        })
    data = _hotel_brief(hotel)
    data["rate_plans"] = plans
    data["rate_plan_count"] = len(plans)
    return data


def _tool_search_reviews(args: Dict[str, Any]) -> Dict[str, Any]:
    from rag import get_rag

    hits = get_rag().retrieve(args["query"], hotel_id=args.get("hotel_id"),
                              top_k=int(args.get("top_k", 3)))
    items = []
    for chunk, score in hits:
        items.append({
            "chunk_id": chunk.get("chunk_id"),
            "hotel_id": chunk.get("hotel_id"),
            "text": chunk.get("text", "")[:300],
            "score": round(float(score), 4),
        })
    return {"query": args["query"], "count": len(items), "results": items}


def _tool_get_order(args: Dict[str, Any]) -> Dict[str, Any]:
    """查订单：**脱敏**返回。住客姓名/手机/邮箱一律不出 Server。"""
    order = tools.find_order(args["order_id"])
    if order is None:
        return {"found": False, "order_id": args["order_id"],
                "note": "本地与远端均未查到该订单。"}
    return {
        "found": True,
        "order": {
            "order_id": order.order_id,
            "hotel_name": order.hotel_name,
            "room_type": order.room_type,
            "check_in": order.check_in,
            "check_out": order.check_out,
            "total_price": float(order.total_price or 0),
            "status": order.status,
            "source": order.source,
            "created_at": order.created_at,
            "paid_at": order.paid_at,
            "has_payment_url": bool(order.payment_url),
        },
        "note": "住客姓名/联系方式按最小必要原则不返回；支付需用户自行打开收银台。",
    }


# ========== 工具注册表（唯一扩展点） ==========

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "search_hotels",
        "title": "搜索可订酒店",
        "description": "按城市、入离店日期、预算、星级、地标搜索真实可订酒店（只读）。"
                       "返回酒店 ID 与展示价，可用 ID 继续查房型。",
        "readOnlyHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名，如 成都", "minLength": 1,
                         "maxLength": 20},
                "check_in": {"type": "string", "format": "date",
                             "description": "入住日期 YYYY-MM-DD"},
                "check_out": {"type": "string", "format": "date",
                              "description": "离店日期 YYYY-MM-DD"},
                "max_price": {"type": "number", "minimum": 0, "maximum": 100000,
                              "description": "每晚预算上限（元）"},
                "min_star": {"type": "integer", "minimum": 1, "maximum": 5,
                             "description": "最低星级 1-5"},
                "location": {"type": "string", "maxLength": 40,
                             "description": "地标/商圈，如 春熙路、故宫"},
                "keyword": {"type": "string", "maxLength": 40,
                            "description": "偏好关键词，如 海景、亲子"},
            },
            "required": ["city", "check_in", "check_out"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_hotel_detail",
        "title": "查房型与报价",
        "description": "按 hotel_id（可选入离店日期）拉取实时房型与报价档位（只读）。",
        "readOnlyHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "hotel_id": {"type": "string", "minLength": 1, "maxLength": 64},
                "check_in": {"type": "string", "format": "date"},
                "check_out": {"type": "string", "format": "date"},
            },
            "required": ["hotel_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_reviews",
        "title": "检索酒店评价",
        "description": "对酒店评价做 RAG 检索，回答“隔音怎么样”“有没有海景”这类问题（只读）。",
        "readOnlyHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 200},
                "hotel_id": {"type": "string", "maxLength": 64},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_order",
        "title": "查订单（脱敏）",
        "description": "按订单号查询订单状态与金额（只读，不含住客联系方式）。",
        "readOnlyHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "minLength": 1, "maxLength": 64},
            },
            "required": ["order_id"],
            "additionalProperties": False,
        },
    },
]

_TOOL_HANDLERS = {
    "search_hotels": _tool_search_hotels,
    "get_hotel_detail": _tool_get_hotel_detail,
    "search_reviews": _tool_search_reviews,
    "get_order": _tool_get_order,
}
_TOOL_MAP = {item["name"]: item for item in TOOLS}


# ========== 资源（只读上下文） ==========

POLICY_URI = "hotel://policy/booking-safety"
POLICY_TEXT = """# 酒店预订 Agent 的资金安全策略

1. **验价与下单分离**：先调 `/hotelpriceconfirm` 锁价拿 referenceNo，展示总价/退改/邮箱，
   用户明确回复「确认下单」后才允许 `create_booking`。
2. **程序不代付**：只返回收银台 `paymentUrl`，支付动作必须由用户本人完成。
3. **真实订单取消两步**：先 `pre_cancel` 查罚金并拿 10 分钟凭证，用户确认后再 `confirm_cancel`。
4. **本 MCP Server 不提供写操作**：search_hotels / get_hotel_detail / search_reviews /
   get_order 全部只读；下单与取消必须在带二次确认的 Host 侧完成。
5. **数据最小必要**：订单查询不返回住客姓名、手机号、邮箱；日志不记录参数值。
"""

RESOURCES = [
    {
        "uri": POLICY_URI,
        "name": "预订与资金安全策略",
        "description": "调用本 Server 前应遵守的只读边界与下单/取消闸门。",
        "mimeType": "text/markdown",
    }
]


# ========== 鉴权与审计 ==========

def _expected_token() -> str:
    return os.environ.get("MCP_AUTH_TOKEN", "").strip()


def auth_ok(authorization: str) -> bool:
    """HTTP 侧鉴权：未配置 token 视为不鉴权（本地开发）；配置了就必须带对。"""
    expected = _expected_token()
    if not expected:
        return True
    if not authorization or not authorization.lower().startswith("bearer "):
        return False
    return hmac.compare_digest(authorization[7:].strip(), expected)


def _audit_path() -> Path:
    override = os.environ.get("MCP_AUDIT_FILE", "").strip()
    return Path(override) if override else PROJ / "cache" / "mcp_audit.jsonl"


def _audit(tool: str, arg_keys: List[str], ok: bool, elapsed_ms: int,
           error: str = "", transport: str = "http") -> None:
    """审计：只记“调了什么、几个参数、耗时、成败”，**不记参数值**（防 PII 入日志）。"""
    record = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "transport": transport,
        "tool": tool,
        "arg_keys": arg_keys,
        "ok": ok,
        "ms": elapsed_ms,
    }
    if error:
        record["error"] = error[:200]
    path = _audit_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with _audit_lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError:
        # 审计失败不能影响工具调用本身
        pass


# ========== JSON-RPC 调度 ==========

def _rpc_result(msg_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _rpc_error(msg_id: Any, code: int, message: str,
               data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    error: Dict[str, Any] = {"code": code, "message": message}
    if data:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": error}


def _text_content(payload: Any, is_error: bool = False) -> Dict[str, Any]:
    return {
        "content": [{
            "type": "text",
            "text": json.dumps(payload, ensure_ascii=False),
        }],
        "isError": is_error,
    }


def _initialize(params: Dict[str, Any]) -> Dict[str, Any]:
    requested = (params or {}).get("protocolVersion")
    version = requested if requested in SUPPORTED_VERSIONS else PROTOCOL_VERSION
    return {
        "protocolVersion": version,
        "capabilities": {
            "tools": {"listChanged": False},
            "resources": {"subscribe": False, "listChanged": False},
        },
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "instructions": (
            "本 Server 只提供酒店与订单的只读能力。验价、下单、支付、取消一律不在"
            "这里暴露：需要用户二次确认并由 Host 侧执行。查订单返回结果已脱敏，"
            "不含住客联系方式。"
        ),
    }


def _call_tool(name: str, arguments: Any, transport: str) -> Dict[str, Any]:
    spec = _TOOL_MAP.get(name)
    if spec is None:
        raise _RpcError(-32602, f"未知工具：{name}（可用：{', '.join(_TOOL_MAP)}）")

    clean, errors = validate_args(arguments or {}, spec["inputSchema"])
    if errors:
        # 参数错误回 isError 内容，让模型能自己改参数重试
        _audit(name, sorted((arguments or {}).keys()) if isinstance(arguments, dict) else [],
               False, 0, "invalid_params", transport)
        return _text_content({"error": "参数不合法", "details": errors}, is_error=True)

    handler = _TOOL_HANDLERS[name]
    started = time.time()
    acquired = _semaphore.acquire(timeout=10)
    try:
        if not acquired:
            raise _ToolError("服务繁忙，请稍后重试")
        result = handler(clean)
        elapsed = int((time.time() - started) * 1000)
        _audit(name, sorted(clean.keys()), True, elapsed, "", transport)
        return _text_content(result)
    except _ToolError as exc:
        elapsed = int((time.time() - started) * 1000)
        _audit(name, sorted(clean.keys()), False, elapsed, str(exc), transport)
        return _text_content({"error": str(exc)}, is_error=True)
    except Exception as exc:  # noqa: BLE001 —— 外部接口异常一律转结构化错误，不泄漏堆栈
        elapsed = int((time.time() - started) * 1000)
        _audit(name, sorted(clean.keys()), False, elapsed, type(exc).__name__, transport)
        print(f"[MCP] 工具 {name} 执行失败：{exc!r}", file=sys.stderr)
        return _text_content(
            {"error": "内部查询失败，请稍后重试", "type": type(exc).__name__},
            is_error=True,
        )
    finally:
        if acquired:
            _semaphore.release()


def handle_message(message: Any, transport: str = "http") -> Optional[Dict[str, Any]]:
    """处理一条 JSON-RPC 消息。通知类（无 id）返回 None，由传输层决定 202/忽略。"""
    if not isinstance(message, dict):
        return _rpc_error(None, -32600, "Invalid Request")
    if message.get("jsonrpc") != "2.0":
        return _rpc_error(message.get("id"), -32600, "jsonrpc 必须是 2.0")

    method = message.get("method")
    msg_id = message.get("id")
    is_notification = "id" not in message

    if not isinstance(method, str):
        return _rpc_error(msg_id, -32600, "method 必须是字符串")

    params = message.get("params") or {}

    try:
        if method == "initialize":
            return _rpc_result(msg_id, _initialize(params))
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None if is_notification else _rpc_result(msg_id, {})
        if method == "ping":
            return _rpc_result(msg_id, {})
        if method == "tools/list":
            return _rpc_result(msg_id, {"tools": TOOLS})
        if method == "tools/call":
            if not isinstance(params, dict) or not params.get("name"):
                raise _RpcError(-32602, "tools/call 需要 name 字段")
            return _rpc_result(msg_id, _call_tool(params["name"],
                                                  params.get("arguments"),
                                                  transport))
        if method == "resources/list":
            return _rpc_result(msg_id, {"resources": RESOURCES})
        if method == "resources/read":
            uri = (params or {}).get("uri")
            if uri != POLICY_URI:
                raise _RpcError(-32602, f"未知资源：{uri}")
            return _rpc_result(msg_id, {
                "contents": [{"uri": POLICY_URI, "mimeType": "text/markdown",
                              "text": POLICY_TEXT}],
            })
        raise _RpcError(-32601, f"未知方法：{method}")
    except _RpcError as exc:
        return _rpc_error(msg_id, exc.code, exc.message)
    except Exception as exc:  # noqa: BLE001
        print(f"[MCP] {method} 处理异常：{exc!r}", file=sys.stderr)
        return _rpc_error(msg_id, -32603, "Internal error")


def handle_payload(payload: Any, transport: str = "http") -> Optional[Any]:
    """支持单条与批量（JSON-RPC batch）。"""
    if isinstance(payload, list):
        if not payload:
            return _rpc_error(None, -32600, "空批量请求")
        responses = [r for r in (handle_message(m, transport) for m in payload) if r]
        return responses or None
    return handle_message(payload, transport)


# ========== 传输 1：stdio ==========

def serve_stdio() -> None:
    """给本地 MCP 客户端用。stdout 只允许出现协议 JSON，业务 print 全部改走 stderr。"""
    proto_out = sys.stdout
    sys.stdout = sys.stderr  # 关键：tools/rag 里的 print 不能污染协议流

    for line in sys.stdin:
        # 行首 BOM 会让 json.loads 直接报 Parse error，先剥掉（部分 Windows 客户端会带）
        line = line.strip().lstrip("\ufeff")
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            proto_out.write(json.dumps(_rpc_error(None, -32700, "Parse error")) + "\n")
            proto_out.flush()
            continue
        try:
            response = handle_payload(payload, transport="stdio")
        except Exception as exc:  # noqa: BLE001
            print(f"[MCP] stdio 处理异常：{exc!r}", file=sys.stderr)
            response = _rpc_error(None, -32603, "Internal error")
        if response is not None:
            proto_out.write(json.dumps(response, ensure_ascii=False) + "\n")
            proto_out.flush()


# ========== 传输 2：Streamable HTTP（挂到 FastAPI） ==========

def build_router() -> APIRouter:
    """返回 APIRouter（POST /mcp）。web.py 挂载它，单独跑也可以。"""
    import asyncio

    router = APIRouter()

    @router.post("/mcp")
    async def mcp_endpoint(request: Request):
        authorization = request.headers.get("authorization", "")
        if not auth_ok(authorization):
            return JSONResponse(
                _rpc_error(None, -32001, "未授权：需要 Bearer Token"),
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        raw = await request.body()
        try:
            payload = json.loads(raw.decode("utf-8") or "null")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JSONResponse(_rpc_error(None, -32700, "Parse error"), status_code=400)

        response = await asyncio.to_thread(handle_payload, payload, "http")
        if response is None:
            # 纯通知（如 notifications/initialized）：管道没有回包，回 202
            return Response(status_code=202)
        return JSONResponse(response)

    @router.get("/mcp")
    async def mcp_stream_unsupported():
        # 本 Server 无状态，不提供服务端推送流；明确回 405 而不是留一个挂住的连接
        return JSONResponse(
            _rpc_error(None, -32600, "本 Server 不提供 GET SSE 流，请用 POST"),
            status_code=405,
            headers={"Allow": "POST"},
        )

    return router


# ========== CLI ==========

def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="酒店预订 MCP Server")
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args(argv)

    if args.transport == "stdio":
        serve_stdio()
        return 0

    import uvicorn
    from fastapi import FastAPI

    app = FastAPI(title="hotel-booking-mcp")
    app.include_router(build_router())
    print(f"MCP Server (Streamable HTTP) → http://{args.host}:{args.port}/mcp",
          file=sys.stderr)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
