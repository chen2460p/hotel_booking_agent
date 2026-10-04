# 模拟酒店数据库 + 工具实现
# 真实场景中这些会调用酒店 API（如携程/飞猪），这里用内存数据模拟

from typing import List, Optional
from datetime import datetime, timedelta
import uuid

from models import Hotel, Order, BookingParams
from amap_poi import is_enabled as amap_enabled, search_real_hotels as search_amap_hotels
from rollinggo_mcp import (
    is_enabled as rollinggo_enabled,
    search_real_hotels as search_rollinggo_hotels,
    enrich_hotel_detail,
)


# ========== 模拟酒店数据库 ==========
# 真实项目中这部分应替换为数据库查询或第三方 API 调用
HOTEL_DB: List[Hotel] = [
    Hotel(
        hotel_id="H001", name="三亚海景大酒店", city="三亚",
        address="三亚湾海坡开发区", star=5, price_per_night=888,
        facilities=["泳池", "WiFi", "早餐", "健身房", "海景"],
        room_types=["标准间", "海景大床房", "行政套房"],
        rating=4.7, review_count=3256
    ),
    Hotel(
        hotel_id="H002", name="三亚湾假日度假酒店", city="三亚",
        address="三亚湾路", star=4, price_per_night=568,
        facilities=["泳池", "WiFi", "早餐", "停车场"],
        room_types=["标准间", "豪华大床房"],
        rating=4.4, review_count=1893
    ),
    Hotel(
        hotel_id="H003", name="三亚亚龙湾希尔顿", city="三亚",
        address="亚龙湾国家旅游度假区", star=5, price_per_night=1288,
        facilities=["私人海滩", "泳池", "WiFi", "早餐", "SPA", "儿童乐园"],
        room_types=["花园景房", "海景房", "别墅"],
        rating=4.9, review_count=5621
    ),
    Hotel(
        hotel_id="H004", name="三亚如家快捷酒店", city="三亚",
        address="解放路", star=3, price_per_night=258,
        facilities=["WiFi", "停车场"],
        room_types=["标准间", "大床房"],
        rating=4.0, review_count=876
    ),
    Hotel(
        hotel_id="H005", name="北京王府井希尔顿", city="北京",
        address="王府井东街", star=5, price_per_night=1588,
        facilities=["WiFi", "早餐", "健身房", "行政酒廊"],
        room_types=["标准间", "行政房", "套房"],
        rating=4.8, review_count=4102
    ),
    Hotel(
        hotel_id="H006", name="北京如家精选酒店", city="北京",
        address="西单北大街", star=3, price_per_night=398,
        facilities=["WiFi", "早餐"],
        room_types=["标准间", "大床房"],
        rating=4.2, review_count=1567
    ),
    Hotel(
        hotel_id="H007", name="上海外滩茂悦大酒店", city="上海",
        address="黄浦路", star=5, price_per_night=1688,
        facilities=["江景", "WiFi", "早餐", "健身房", "SPA"],
        room_types=["标准间", "江景房", "套房"],
        rating=4.8, review_count=3890
    ),
    Hotel(
        hotel_id="H008", name="上海汉庭酒店", city="上海",
        address="南京东路", star=2, price_per_night=328,
        facilities=["WiFi"],
        room_types=["标准间", "大床房"],
        rating=4.1, review_count=2103
    ),
    Hotel(
        hotel_id="H009", name="杭州西湖国宾馆", city="杭州",
        address="杨公堤", star=5, price_per_night=1888,
        facilities=["湖景", "WiFi", "早餐", "园林", "茶室"],
        room_types=["园景房", "湖景房", "别墅"],
        rating=4.9, review_count=2876
    ),
    Hotel(
        hotel_id="H010", name="杭州七天连锁酒店", city="杭州",
        address="武林广场", star=2, price_per_night=198,
        facilities=["WiFi"],
        room_types=["标准间"],
        rating=3.8, review_count=945
    ),
]

# 订单存储（模拟数据库）
ORDER_DB: dict = {}

# 实时搜索结果缓存（道旅/高德）：进程内按 hotel_id 保存最近搜到的真实酒店，
# 使 get_hotel_detail 也能查到非模拟库的酒店（下单前详情确认等场景）
_RUNTIME_HOTEL_CACHE: dict = {}


def _cache_runtime_hotels(hotels: List[Hotel]) -> None:
    for h in hotels:
        _RUNTIME_HOTEL_CACHE[h.hotel_id] = h


# ========== 工具函数 ==========
# 每个工具对应 Agent 可调用的一个"能力"，遵循八股中的 Tool 设计原则：
# - 小而可组合，一个工具做一件事
# - 参数明确，返回结构化结果
# - 错误信息结构化，便于 Agent 自修正

def search_hotels(params: BookingParams) -> List[Hotel]:
    """
    工具1：搜索酒店
    这是 Workflow 的核心——参数齐全后走确定性筛选流程，不需要 LLM 参与

    三级数据源（按配置自动选择，失败逐级回退）：
    1. ROLLINGGO_API_KEY：道旅 MCP，真实酒店 + 实时房价 + 星级（全国城市）
    2. AMAP_API_KEY：高德 POI，真实酒店名/地址（无价格）
    3. 均未配置或调用失败：本地模拟数据库 HOTEL_DB
    """
    if params.city and rollinggo_enabled():
        try:
            results = search_rollinggo_hotels(params)
            _cache_runtime_hotels(results)
            return results
        except Exception as e:
            print(f"[道旅MCP调用失败，尝试高德POI] {e}")

    if params.city and amap_enabled():
        try:
            results = search_amap_hotels(params)
            _cache_runtime_hotels(results)
            # 高德 POI 无价格/星级/设施数据，这些筛选条件无法执行
            if params.min_star or params.max_price or params.facilities:
                print("[提示] 高德POI模式下仅支持城市/关键词筛选，"
                      "已忽略星级、价格、设施条件")
            return results
        except Exception as e:
            print(f"[高德API调用失败，回退模拟数据] {e}")

    results = HOTEL_DB

    # 按城市筛选（必填条件）
    if params.city:
        results = [h for h in results if h.city == params.city]

    # 按最低星级筛选
    if params.min_star:
        results = [h for h in results if h.star >= params.min_star]

    # 按价格上限筛选
    if params.max_price:
        results = [h for h in results if h.price_per_night <= params.max_price]

    # 按设施筛选（必须包含所有指定设施）
    if params.facilities:
        results = [
            h for h in results
            if all(f in h.facilities for f in params.facilities)
        ]

    # 按关键词模糊匹配（酒店名或设施）
    if params.keyword:
        kw = params.keyword
        results = [
            h for h in results
            if kw in h.name or any(kw in f for f in h.facilities)
        ]

    # 排序：评分高 + 评论多的优先（确定性排序规则）
    results.sort(key=lambda h: (h.rating, h.review_count), reverse=True)

    return results


def get_hotel_detail(hotel_id: str, check_in: Optional[str] = None,
                     check_out: Optional[str] = None,
                     live: bool = True) -> Optional[Hotel]:
    """
    工具2：获取酒店详情
    - 道旅酒店且传入了入离店日期：实时拉取房型报价/退改政策（失败回退缓存）
    - 否则：返回最近搜索缓存或本地模拟库数据
    """
    cached = _RUNTIME_HOTEL_CACHE.get(hotel_id)
    if cached is None:
        for h in HOTEL_DB:
            if h.hotel_id == hotel_id:
                cached = h
                break
    if cached is None:
        return None

    if live and check_in and check_out and cached.hotel_id.startswith("RLG_"):
        try:
            enrich_hotel_detail(cached, check_in, check_out)
        except Exception as e:
            print(f"[道旅详情获取失败，使用搜索缓存数据] {e}")
    return cached


def calculate_total_price(hotel: Hotel, check_in: str, check_out: str) -> float:
    """工具3：计算总价（Workflow 中的确定性计算）"""
    try:
        d1 = datetime.strptime(check_in, "%Y-%m-%d")
        d2 = datetime.strptime(check_out, "%Y-%m-%d")
        nights = (d2 - d1).days
        if nights <= 0:
            return 0.0
        return hotel.price_per_night * nights
    except ValueError:
        return 0.0


def create_order(hotel: Hotel, params: BookingParams,
                 room_type: str, guest_name: str,
                 price_per_night: Optional[float] = None) -> Order:
    """
    工具4：创建订单
    注意：这是高危操作，真实场景需要人工确认 + 支付流程
    这里模拟创建，状态为 pending（待支付）

    price_per_night：指定房型的实时每晚价（道旅详情报价）；
                     不传时使用酒店搜索阶段的最低价。
    """
    if price_per_night is not None:
        try:
            d1 = datetime.strptime(params.check_in, "%Y-%m-%d")
            d2 = datetime.strptime(params.check_out, "%Y-%m-%d")
            nights = max((d2 - d1).days, 1)
            total = price_per_night * nights
        except ValueError:
            total = 0.0
    else:
        total = calculate_total_price(hotel, params.check_in, params.check_out)
    order = Order(
        order_id=f"ORD{uuid.uuid4().hex[:8].upper()}",
        hotel_id=hotel.hotel_id,
        hotel_name=hotel.name,
        check_in=params.check_in,
        check_out=params.check_out,
        room_type=room_type,
        guest_name=guest_name,
        total_price=total,
        status="pending"
    )
    ORDER_DB[order.order_id] = order
    return order


def create_real_order(hotel: Hotel, params: BookingParams, room_type: str,
                      guest_name: str, contact_email: str,
                      price_info: dict, booking_result: dict) -> Order:
    """
    工具4b：道旅真实订单入库（订单已在道旅侧通过 hotelbook 创建）
    - order_id 使用道旅真实订单号
    - 状态固定为 pending（待支付），支付需用户自行打开 payment_url 完成，
      Agent 绝不能代替用户完成真实支付
    price_info：rollinggo_book.price_confirm 的归一化结果
    booking_result：rollinggo_book.create_booking 的归一化结果
    """
    total = price_info.get("total_price")
    if total is None:
        total = calculate_total_price(hotel, params.check_in, params.check_out)
    order = Order(
        order_id=str(booking_result.get("order_no") or f"RLG{uuid.uuid4().hex[:10].upper()}"),
        hotel_id=hotel.hotel_id,
        hotel_name=hotel.name,
        check_in=params.check_in,
        check_out=params.check_out,
        room_type=room_type or (price_info.get("room_name") or ""),
        guest_name=guest_name,
        total_price=float(total or 0),
        status="pending",
        source="rollinggo",
        payment_url=booking_result.get("payment_url") or "",
        contact_email=contact_email,
    )
    ORDER_DB[order.order_id] = order
    return order


def pay_order(order_id: str) -> bool:
    """
    工具5：支付订单
    高危操作——真实场景需要接入支付网关 + 短信验证
    这里模拟支付成功
    """
    if order_id in ORDER_DB:
        ORDER_DB[order_id].status = "paid"
        return True
    return False


def cancel_order(order_id: str) -> bool:
    """工具6：取消订单（售后场景）"""
    if order_id in ORDER_DB and ORDER_DB[order_id].status != "cancelled":
        ORDER_DB[order_id].status = "cancelled"
        return True
    return False


def get_order_status(order_id: str) -> Optional[Order]:
    """工具7：查询订单状态（售后场景）"""
    return ORDER_DB.get(order_id)


# ========== 历史订单列表（道旅真实订单 + 本地内存订单）==========

# 本地订单状态 → 统一中文文案
_LOCAL_STATUS_TEXT = {
    "pending": "待支付",
    "paid": "已支付",
    "cancelled": "已取消",
}

# 列表筛选器允许的状态桶
_ORDER_FILTERS = ("ALL", "PENDING", "FINISHED", "CANCELLED")


def _local_order_to_row(order: Order) -> dict:
    """把本地 Order 转成与道旅订单一致的归一化行结构。"""
    return {
        "order_no": order.order_id,
        "hotel_name": order.hotel_name,
        "room_name": order.room_type,
        "check_in": order.check_in,
        "check_out": order.check_out,
        "total_price": float(order.total_price or 0),
        "currency": "CNY",
        "status": order.status,
        "status_text": _LOCAL_STATUS_TEXT.get(order.status, order.status),
        "payment_url": order.payment_url or "",
        "create_time": "",
        "source": order.source,                       # mock / rollinggo
        "session_local": True,                        # 来自本次进程内存
    }


def _order_status_bucket(row: dict) -> str:
    """把任意订单行归入 PENDING / CANCELLED / FINISHED 桶，用于本地筛选。"""
    text = str(row.get("status_text") or "")
    raw = str(row.get("status") or "").upper()
    if "取消" in text or "退" in text or any(
            k in raw for k in ("CANCEL", "REFUND", "REJECT", "VOID")):
        return "CANCELLED"
    if "待支付" in text or "未支付" in text \
            or "PAY" in raw or "UNPAID" in raw or raw == "PENDING":
        return "PENDING"
    return "FINISHED"


def list_history_orders(status_filter: str = "ALL") -> dict:
    """
    工具7b：查询历史订单列表。

    数据源：
    - 已登录道旅 OAuth：拉取道旅账号订单（真正的"历史"，跨会话保留），
      并补充本次进程刚创建、远程可能尚未同步的本地真实单/模拟单；
    - 未登录：仅返回本地内存订单（程序重启后为空）。

    status_filter：ALL / PENDING（待支付）/ FINISHED（已支付等终态）/ CANCELLED
    返回：{logged_in, source, filter, rows, remote_error}
    """
    flt = (status_filter or "ALL").upper()
    if flt not in _ORDER_FILTERS:
        flt = "ALL"

    rows: List[dict] = []
    remote_error: Optional[str] = None
    logged_in = False
    try:
        import rollinggo_book as rlg
        logged_in = rlg.is_logged_in()
    except Exception:
        logged_in = False

    if logged_in:
        # FINISHED/CANCELLED 统一拉全量再在本地过滤，避免对接口语义做过度假设
        api_status = "PENDING" if flt == "PENDING" else "ALL"
        try:
            import rollinggo_book as rlg
            rows = rlg.parse_orders(rlg.list_orders(api_status))
        except Exception as e:
            remote_error = str(e)

        # 补充本地有、远程列表里没有的订单（刚下单未同步 / 模拟单）
        remote_ids = {r.get("order_no") for r in rows}
        for order in ORDER_DB.values():
            if order.order_id not in remote_ids:
                rows.append(_local_order_to_row(order))
        source = "rollinggo"
    else:
        rows = [_local_order_to_row(o) for o in ORDER_DB.values()]
        source = "local" if rows else "none"

    if flt != "ALL":
        rows = [r for r in rows if _order_status_bucket(r) == flt]

    return {
        "logged_in": logged_in,
        "source": source,
        "filter": flt,
        "rows": rows,
        "remote_error": remote_error,
    }


def format_order_rows(rows: List[dict], max_show: int = 10) -> str:
    """把归一化订单行列表格式化为终端可读文本（供 Agent / 多 Agent 版复用）。"""
    if not rows:
        return ""
    lines = []
    for i, r in enumerate(rows[:max_show], 1):
        tag = "【道旅】" if r.get("source") == "rollinggo" \
            and not r.get("session_local") else "【本地】"
        total = r.get("total_price")
        if total is None or total == "":
            total_text = "金额待确认"
        else:
            # 归一化链路本就产出数值；这里兼容外部直接传入字符串金额的情况
            try:
                total_text = f"{float(total):g} {r.get('currency') or 'CNY'}"
            except (TypeError, ValueError):
                total_text = f"{total} {r.get('currency') or 'CNY'}"
        head = (
            f"{i}. {tag}{r.get('order_no') or '未知订单号'}\n"
            f"   {r.get('hotel_name') or '酒店未知'}"
        )
        if r.get("room_name"):
            head += f" · {r['room_name']}"
        if r.get("check_in") or r.get("check_out"):
            head += f"\n   入住：{r.get('check_in') or '?'} → {r.get('check_out') or '?'}"
        head += f"\n   总价：{total_text} · 状态：{r.get('status_text') or '未知'}"
        lines.append(head)
        # 待支付订单附上支付链接（真实订单由用户自行付款）
        if r.get("payment_url") and _order_status_bucket(r) == "PENDING":
            lines.append(f"   支付链接：{r['payment_url']}")
    if len(rows) > max_show:
        lines.append(f"... 另有 {len(rows) - max_show} 笔订单未展示")
    return "\n".join(lines)


def format_hotel_list(hotels: List[Hotel], max_show: int = 5) -> str:
    """工具8：格式化酒店列表为可读文本（供 Agent 展示给用户）"""
    if not hotels:
        return "没有找到符合条件的酒店，建议放宽筛选条件。"
    lines = []
    for i, h in enumerate(hotels[:max_show], 1):
        # 高德 POI 数据中星级/价格/评分为 0 表示未知，展示层做友好兜底
        star_text = f"{h.star}星" if h.star else "星级未知"
        price_text = f"{h.price_per_night:g}元/晚" if h.price_per_night else "价格以实际预订为准"
        if h.rating:
            rating_text = f"评分：{h.rating}（{h.review_count}条评论）"
        else:
            rating_text = "暂无评分"
        facilities_str = "、".join(h.facilities[:4]) if h.facilities else "信息待补充"
        lines.append(
            f"{i}. {h.name}（{star_text}）\n"
            f"   价格：{price_text} | {rating_text}\n"
            f"   地址：{h.address}\n"
            f"   设施：{facilities_str}\n"
            f"   房型：{'、'.join(h.room_types)}"
        )
    if len(hotels) > max_show:
        lines.append(f"... 还有 {len(hotels) - max_show} 家，可告知更多偏好帮你筛选")
    return "\n".join(lines)
