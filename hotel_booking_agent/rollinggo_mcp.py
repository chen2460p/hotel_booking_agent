# 道旅 RollingGo 酒店 MCP 适配器
#
# 用途：获取全国城市真实酒店的实时房价/房型（B2B 供应链可预订价），
#       弥补高德 POI 只有名称地址、没有价格的短板。
#
# 渠道：深圳市道旅科技 RollingGo Hotel MCP（个人可免费申请、无调用量限制）
# 申请：https://travelportal-partner-center.dida.com/register?lang=zh
#       拿到 mcp_ 开头的 Key 后填入同目录 .env：ROLLINGGO_API_KEY=mcp_xxx
# 协议：MCP Streamable HTTP（JSON-RPC 2.0，响应可能是普通 JSON 或 SSE 流）
#
# 能力：searchHotels（酒店+展示价）、getHotelDetail（实时房型/报价/退改）
# 注意：返回的是供应链价格，与携程等 C 端零售价可能不同，但真实可预订。

import os
import json
from typing import Any, Dict, List, Optional

import requests

from models import BookingParams, Hotel, RoomRatePlan

# 从 .env 加载环境变量（与 llm.py / rag.py / amap_poi.py 保持一致）
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
                override=True)
except ImportError:
    pass

# 国内站默认 .cn；若用的是 global.rollinggo.store 的国际 Key（mcp_ 开头），
# 可在 .env 设置 ROLLINGGO_MCP_URL=https://mcp.rollinggo.ai/mcp
_MCP_URL = os.environ.get(
    "ROLLINGGO_MCP_URL", "https://mcp.rollinggo.cn/mcp").strip()
_PROTOCOL_VERSION = "2025-03-26"

# 搜索无房型数据时的通用默认房型，保证"选酒店→下单"演示闭环不断
_DEFAULT_ROOM_TYPES = ["标准间", "大床房"]


# ========== 配置 ==========

def get_api_key() -> str:
    return os.environ.get("ROLLINGGO_API_KEY", "").strip()


def is_enabled() -> bool:
    return bool(get_api_key())


# ========== MCP Streamable HTTP 客户端 ==========

class RollingGoMCPClient:
    """
    极简 MCP 客户端：initialize → notifications/initialized → tools/call
    服务端响应可能是 application/json 或 text/event-stream，两种都兼容。
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            # 缺这个 Accept 头服务端会返回 400（官方文档明确要求）
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {get_api_key()}",
        })
        self._req_id = 0
        self._initialized = False

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _post(self, payload: dict) -> dict:
        resp = self.session.post(_MCP_URL, json=payload, timeout=20)
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "")
        if "text/event-stream" in content_type:
            return self._parse_sse(resp.text)
        return resp.json()

    @staticmethod
    def _parse_sse(text: str) -> dict:
        """解析 SSE：取最后一个 data: 帧的 JSON（消息可能分多帧）"""
        data_lines = []
        for line in text.splitlines():
            if line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        if not data_lines:
            raise RuntimeError(f"MCP SSE 响应中没有数据帧：{text[:200]}")
        return json.loads("\n".join(data_lines))

    def initialize(self):
        """按 MCP 规范完成握手（失败不阻断，部分网关支持无状态直调）"""
        if self._initialized:
            return
        try:
            self._rpc("initialize", {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "hotel-booking-agent", "version": "1.0"},
            }, notify=False)
            # 通知服务端初始化完成（通知类消息无 id，通常无响应体）
            self.session.post(_MCP_URL, json={
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            }, timeout=10)
        except Exception as e:
            print(f"[道旅] initialize 握手异常（继续尝试直调）：{e}")
        self._initialized = True

    def _rpc(self, method: str, params: dict, notify: bool = False) -> dict:
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not notify:
            payload["id"] = self._next_id()
        payload["params"] = params
        data = self._post(payload)
        if "error" in data:
            err = data["error"]
            raise RuntimeError(f"MCP 错误 {err.get('code')}: {err.get('message')}")
        return data.get("result", {})

    def list_tools(self) -> list:
        """tools/list —— 联调时用于确认工具清单与参数 schema"""
        return self._rpc("tools/list", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict) -> Any:
        """
        tools/call —— 返回工具结果。
        MCP 文本内容约定在 result.content[].text，通常是 JSON 字符串，
        这里自动尝试反序列化，失败则返回原始文本。
        """
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})

        if result.get("isError"):
            raise RuntimeError(f"工具 {name} 返回错误：{result}")

        texts = [c.get("text", "") for c in result.get("content", [])
                 if c.get("type") == "text"]
        raw = "\n".join(t for t in texts if t).strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw


# 模块级单例（首次调用时惰性初始化）
_client: Optional[RollingGoMCPClient] = None


def _get_client() -> RollingGoMCPClient:
    global _client
    if _client is None:
        _client = RollingGoMCPClient()
        _client.initialize()
    return _client


# ========== 参数构造 ==========

def _build_arguments(params: BookingParams, size: int = 20) -> dict:
    """把项目的 BookingParams 映射成 searchHotels 的官方参数结构"""
    nights = 1
    if params.check_in and params.check_out:
        from datetime import datetime
        try:
            d1 = datetime.strptime(params.check_in, "%Y-%m-%d")
            d2 = datetime.strptime(params.check_out, "%Y-%m-%d")
            nights = max((d2 - d1).days, 1)
        except ValueError:
            pass

    origin_parts = [params.city]
    if params.keyword:
        origin_parts.append(params.keyword)
    if params.max_price:
        origin_parts.append(f"预算{int(params.max_price)}元以内")
    origin_parts.append("酒店")

    arguments: Dict[str, Any] = {
        "originQuery": "".join(origin_parts),
        "place": params.city,
        "placeType": "城市",
        "size": size,
    }
    if params.check_in:
        arguments["checkInParam"] = {
            "checkInDate": params.check_in,
            "stayNights": nights,
        }
    filter_options: Dict[str, Any] = {}
    if params.min_star:
        # 官方约定：starRatings [4,5] 表示 4-5 星
        filter_options["starRatings"] = [float(params.min_star), 5.0]
    if filter_options:
        arguments["filterOptions"] = filter_options

    # 预算由服务端按每晚最高价筛选（官方 hotelTags.maxPricePerNight，已实测生效）
    if params.max_price:
        arguments["hotelTags"] = {"maxPricePerNight": float(params.max_price)}
    return arguments


# ========== 结果映射（已按真实 searchHotels 响应校准，字段名保留防御式兜底）==========
# 实测响应（2026-10）：hotelInformationList[]，单项含
#   hotelId / name / address / starRating(0.5梯度) /
#   price{hasPrice,currency,lowestPrice(每晚均价)} /
#   hotelAmenities[字符串] / tags[含"提供大床房"等房型标签]
# 搜索结果暂无客人评分字段。

_PICK_ID = ("hotelId", "hotelID", "id", "hotelCode", "masterHotelId")
_PICK_NAME = ("name", "hotelName", "nameCn", "cnName")
_PICK_ADDR = ("address", "addr", "hotelAddress", "location")
_PICK_STAR = ("starRating", "star", "starRate", "hotelStar")
_PICK_RATING = ("guestRating", "rating", "score", "commentScore")
_PICK_REVIEW = ("reviewCount", "commentCount", "commentNum")

# tags 中房型标签 → 统一房型名
_ROOM_TAG_MAP = (
    ("大床房", "大床房"),
    ("双床房", "双床房"),
    ("三人间", "三人间"),
    ("四人间", "四人间"),
    ("家庭房", "家庭房"),
)


def _pick(item: dict, keys: tuple, default=None):
    """按候选 key 列表取值（支持嵌套在 price 对象里的 amount 字段）"""
    for k in keys:
        v = item.get(k)
        if v not in (None, "", 0, "0"):
            return v
    return default


def _to_float(v, default: float = 0.0) -> float:
    try:
        if isinstance(v, dict):
            v = v.get("lowestPrice") or v.get("amount") or v.get("value")
        return float(v)
    except (TypeError, ValueError):
        return default


def _extract_price(item: dict) -> float:
    """
    提取每晚均价。实测 price 为对象：
    {"hasPrice": true, "currency": "CNY", "lowestPrice": 310.0, "message": ...}
    hasPrice 为假或缺对象时返回 0（展示层会显示"价格以实际预订为准"）。
    """
    price_obj = item.get("price")
    if isinstance(price_obj, dict):
        if price_obj.get("hasPrice"):
            return _to_float(price_obj.get("lowestPrice"))
        return 0.0
    # 兼容直接给数字/字符串的形态
    return _to_float(price_obj or _pick(item, (
        "lowestPrice", "displayPrice", "minPrice", "pricePerNight")))


def _extract_room_types(item: dict) -> List[str]:
    """优先从 tags 中的"提供X房"标签推断房型；否则用默认房型保证下单闭环"""
    tags = item.get("tags") or []
    if isinstance(tags, list):
        joined = " ".join(str(t) for t in tags)
        rooms = [name for key, name in _ROOM_TAG_MAP if key in joined]
        if rooms:
            return rooms[:4]

    rooms_raw = item.get("roomTypes") or item.get("rooms")
    if isinstance(rooms_raw, list) and rooms_raw:
        return [r.get("name", str(r)) if isinstance(r, dict) else str(r)
                for r in rooms_raw][:4]
    return list(_DEFAULT_ROOM_TYPES)


# 高关注度设施优先展示（命中关键词的设施排到前面）
_FACILITY_PRIORITY = (
    "泳池", "健身", "早餐", "餐厅", "停车场", "SPA", "温泉", "接送",
    "接机", "宠物", "儿童", "酒吧", "行政", "会议", "wifi", "WiFi",
)


def _extract_facilities(item: dict) -> List[str]:
    fac_raw = (item.get("hotelAmenities") or item.get("facilities")
               or item.get("amenities"))
    if not isinstance(fac_raw, list):
        return []
    facs = [f.get("name", str(f)) if isinstance(f, dict) else str(f)
            for f in fac_raw]
    facs.sort(key=lambda f: min(
        (i for i, kw in enumerate(_FACILITY_PRIORITY) if kw in f),
        default=len(_FACILITY_PRIORITY)))
    return facs[:6]


def _to_hotel(item: dict, city: str) -> Optional[Hotel]:
    """把 searchHotels 返回的单个酒店对象映射为统一 Hotel 模型"""
    name = _pick(item, _PICK_NAME)
    if not name:
        return None

    hotel_id = str(_pick(item, _PICK_ID, default=""))
    price = _extract_price(item)
    star = int(_to_float(_pick(item, _PICK_STAR, default=0)))
    rating = _to_float(_pick(item, _PICK_RATING, default=0))
    review_count = int(_to_float(_pick(item, _PICK_REVIEW, default=0)))
    address = str(_pick(item, _PICK_ADDR, default="地址待补充"))

    room_types = _extract_room_types(item)

    # 设施：实测字段 hotelAmenities 为字符串数组；保留 facilities/amenities 兜底。
    # 原数组前面多是"叫醒服务/行李寄存"等低关注项，按旅客关注度重排后取前 6。
    facilities = _extract_facilities(item)

    return Hotel(
        hotel_id=f"RLG_{hotel_id}" if hotel_id else f"RLG_{name}",
        name=str(name),
        city=city,
        address=address,
        star=star,
        price_per_night=price,
        facilities=facilities,
        room_types=room_types,
        rating=rating,
        review_count=review_count,
        booking_url=str(item.get("bookingUrl") or ""),
    )


def _extract_hotel_list(data: Any) -> list:
    """
    从 tools/call 结果中提取酒店数组。
    兼容多种可能形态：直接列表 / {hotels:[...]} / {data:{list:[...]}} /
    {result:{hotels:[...]}} 等。
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("hotelInformationList", "hotels", "hotelList", "list",
                    "items", "records", "data"):
            v = data.get(key)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                nested = _extract_hotel_list(v)
                if nested:
                    return nested
    return []


def search_real_hotels(params: BookingParams, size: int = 20) -> List[Hotel]:
    """调用 searchHotels 并映射为 Hotel 列表"""
    client = _get_client()
    data = client.call_tool("searchHotels", _build_arguments(params, size))

    # 实测失败响应形态：{"success": false, "code": xxxx, "message": "..."}
    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(f"道旅搜索失败 code={data.get('code')}: "
                           f"{data.get('message')}")

    raw_hotels = _extract_hotel_list(data)

    hotels = []
    for item in raw_hotels:
        if isinstance(item, dict):
            h = _to_hotel(item, params.city)
            if h:
                hotels.append(h)

    # 预算已通过服务端 hotelTags.maxPricePerNight 筛选；
    # 本地再兜一次，防止个别无价/价格波动酒店漏网
    if params.max_price:
        hotels = [h for h in hotels
                  if h.price_per_night <= 0 or h.price_per_night <= params.max_price]
    return hotels


# ========== 酒店详情（实时房型报价 / 退改政策）==========

def _to_rate_plan(plan: dict) -> RoomRatePlan:
    """把单个 roomRatePlans 报价映射为 RoomRatePlan"""
    info = plan.get("roomInfo") or {}
    return RoomRatePlan(
        room_name=str(plan.get("roomName") or "未知房型"),
        price_per_night=_to_float(plan.get("averagePrice")),
        meal=str(plan.get("mealTypeStr") or ""),
        cancelable=bool(plan.get("cancelable")),
        cancel_policy=str(plan.get("cancelPolicy") or ""),
        bed_type=str(plan.get("bedTypeDescription") or ""),
        max_occupancy=int(_to_float(info.get("maxOccupancy"))),
        room_size=str(info.get("size") or ""),
        on_request=bool(plan.get("isOnRequest")),
        rate_plan_id=str(plan.get("ratePlanId") or ""),
    )


def _aggregate_rate_plans(plans_raw: list) -> List[RoomRatePlan]:
    """
    同一家酒店可能返回上百条报价（同房型 × 含早/退改组合）。
    按房型名聚合，每个房型保留最便宜的一档作为代表，按价格升序返回。
    """
    cheapest: Dict[str, RoomRatePlan] = {}
    for p in plans_raw:
        if not isinstance(p, dict):
            continue
        rp = _to_rate_plan(p)
        if rp.price_per_night <= 0:
            continue
        existed = cheapest.get(rp.room_name)
        if existed is None or rp.price_per_night < existed.price_per_night:
            cheapest[rp.room_name] = rp
    return sorted(cheapest.values(), key=lambda x: x.price_per_night)


def enrich_hotel_detail(hotel: Hotel, check_in: str,
                        check_out: str) -> Optional[Hotel]:
    """
    用 getHotelDetail 的实时房型报价富化搜索阶段的 Hotel 对象。
    仅对道旅酒店（hotel_id 以 RLG_ 开头）生效；返回富化后的同一对象。
    """
    if not hotel.hotel_id.startswith("RLG_"):
        return hotel
    raw_id = hotel.hotel_id[len("RLG_"):]
    try:
        hid_int = int(raw_id)
    except ValueError:
        return hotel

    client = _get_client()
    data = client.call_tool("getHotelDetail", {
        "hotelId": hid_int,
        "dateParam": {"checkInDate": check_in, "checkOutDate": check_out},
    })
    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(f"道旅详情查询失败 code={data.get('code')}: "
                           f"{data.get('message')}")

    plans = _aggregate_rate_plans(data.get("roomRatePlans", [])
                                  if isinstance(data, dict) else [])
    if plans:
        hotel.rate_plans = plans
        hotel.room_types = [rp.room_name for rp in plans]
        # 详情最低价更新为实时报价的最低价（搜索价可能是缓存/近似值）
        hotel.price_per_night = plans[0].price_per_night
    if isinstance(data, dict) and data.get("bookingUrl"):
        hotel.booking_url = data["bookingUrl"]
    return hotel
