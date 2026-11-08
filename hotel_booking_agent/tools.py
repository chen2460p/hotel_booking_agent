# 模拟酒店数据库 + 工具实现
# 真实场景中这些会调用酒店 API（如携程/飞猪），这里用内存数据模拟

from typing import List, Optional
from datetime import datetime
import time
import uuid

from models import Hotel, Order, BookingParams
import storage
from amap_poi import is_enabled as amap_enabled, search_real_hotels as search_amap_hotels
from rollinggo_mcp import (
    is_enabled as rollinggo_enabled,
    search_real_hotels as search_rollinggo_hotels,
    enrich_hotel_detail,
)
from hotel_rating import (
    is_enabled as rating_enabled,
    enrich_hotels_rating,
    enrich_hotel_rating,
)


# ========== 模拟酒店数据库 ==========
# 真实项目中这部分应替换为数据库查询或第三方 API 调用。
# 覆盖 16 个城市（三亚/北京/上海/杭州/成都/重庆/西安/广州/深圳/南京/武汉/长沙/
# 厦门/青岛/天津/苏州），每个城市 2~4 家，价格与星级拉开档位，
# 用于无 Key 的降级运行与规则引擎回归测试。
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
    # ---------- 成都 ----------
    Hotel(
        hotel_id="H011", name="成都春熙路亚朵酒店", city="成都",
        address="春熙路商圈 红星路三段", star=4, price_per_night=468,
        facilities=["WiFi", "早餐", "健身房", "停车场"],
        room_types=["高级大床房", "高级双床房", "亲子房"],
        rating=4.6, review_count=2890
    ),
    Hotel(
        hotel_id="H012", name="成都天府广场希尔顿酒店", city="成都",
        address="人民南路一段", star=5, price_per_night=1288,
        facilities=["WiFi", "早餐", "健身房", "行政酒廊", "泳池"],
        room_types=["标准间", "行政房", "套房"],
        rating=4.8, review_count=3612
    ),
    Hotel(
        hotel_id="H013", name="成都宽窄巷子青旅", city="成都",
        address="下同仁路 宽窄巷子旁", star=2, price_per_night=168,
        facilities=["WiFi"],
        room_types=["标准间", "大床房"],
        rating=4.1, review_count=742
    ),
    # ---------- 重庆 ----------
    Hotel(
        hotel_id="H014", name="重庆解放碑洪崖洞江景酒店", city="重庆",
        address="沧白路 洪崖洞景区旁", star=4, price_per_night=528,
        facilities=["江景", "WiFi", "早餐"],
        room_types=["江景大床房", "标准间"],
        rating=4.7, review_count=2103
    ),
    Hotel(
        hotel_id="H015", name="重庆江北机场如家快捷酒店", city="重庆",
        address="江北国际机场 T3 航站楼", star=2, price_per_night=218,
        facilities=["WiFi", "停车场"],
        room_types=["标准间", "大床房"],
        rating=3.9, review_count=660
    ),
    # ---------- 西安 ----------
    Hotel(
        hotel_id="H016", name="西安钟楼美居酒店", city="西安",
        address="南大街 钟楼旁", star=4, price_per_night=498,
        facilities=["WiFi", "早餐", "健身房"],
        room_types=["大床房", "双床房"],
        rating=4.6, review_count=1876
    ),
    Hotel(
        hotel_id="H017", name="西安临潼兵马俑驿站客栈", city="西安",
        address="临潼区 秦陵北路", star=2, price_per_night=158,
        facilities=["WiFi", "停车场"],
        room_types=["标准间"],
        rating=4.0, review_count=512
    ),
    # ---------- 广州 ----------
    Hotel(
        hotel_id="H018", name="广州珠江新城江景大酒店", city="广州",
        address="珠江新城 花城大道", star=5, price_per_night=1388,
        facilities=["江景", "WiFi", "早餐", "健身房", "SPA"],
        room_types=["江景房", "行政房", "套房"],
        rating=4.8, review_count=2984
    ),
    Hotel(
        hotel_id="H019", name="广州北京路步行街快捷酒店", city="广州",
        address="北京路步行街", star=2, price_per_night=238,
        facilities=["WiFi"],
        room_types=["标准间", "大床房"],
        rating=4.0, review_count=1203
    ),
    # ---------- 深圳 ----------
    Hotel(
        hotel_id="H020", name="深圳福田会展中心大酒店", city="深圳",
        address="福田区 福华三路 会展中心", star=5, price_per_night=1188,
        facilities=["WiFi", "早餐", "健身房", "泳池"],
        room_types=["标准间", "行政房", "套房"],
        rating=4.7, review_count=2456
    ),
    Hotel(
        hotel_id="H021", name="深圳华强北全季酒店", city="深圳",
        address="福田区 华强北路", star=3, price_per_night=358,
        facilities=["WiFi", "早餐"],
        room_types=["大床房", "双床房"],
        rating=4.4, review_count=1780
    ),
    # ---------- 南京 ----------
    Hotel(
        hotel_id="H022", name="南京夫子庙秦淮河景酒店", city="南京",
        address="秦淮区 夫子庙贡院街", star=4, price_per_night=588,
        facilities=["河景", "WiFi", "早餐"],
        room_types=["河景大床房", "标准间"],
        rating=4.6, review_count=1543
    ),
    Hotel(
        hotel_id="H023", name="南京新街口如家精选酒店", city="南京",
        address="新街口 中山南路", star=2, price_per_night=268,
        facilities=["WiFi"],
        room_types=["标准间", "大床房"],
        rating=4.1, review_count=987
    ),
    # ---------- 武汉 ----------
    Hotel(
        hotel_id="H024", name="武汉黄鹤楼江景酒店", city="武汉",
        address="武昌区 临江大道 黄鹤楼旁", star=4, price_per_night=468,
        facilities=["江景", "WiFi", "早餐"],
        room_types=["江景房", "标准间"],
        rating=4.5, review_count=1320
    ),
    Hotel(
        hotel_id="H025", name="武汉光谷广场汉庭酒店", city="武汉",
        address="洪山区 光谷广场", star=2, price_per_night=208,
        facilities=["WiFi"],
        room_types=["标准间"],
        rating=3.9, review_count=845
    ),
    # ---------- 长沙 ----------
    Hotel(
        hotel_id="H026", name="长沙五一广场橘子洲酒店", city="长沙",
        address="芙蓉区 五一大道", star=4, price_per_night=438,
        facilities=["WiFi", "早餐", "健身房"],
        room_types=["大床房", "双床房"],
        rating=4.5, review_count=1678
    ),
    Hotel(
        hotel_id="H027", name="长沙坡子街青年旅舍", city="长沙",
        address="天心区 坡子街", star=2, price_per_night=138,
        facilities=["WiFi"],
        room_types=["标准间"],
        rating=4.0, review_count=604
    ),
    # ---------- 厦门 ----------
    Hotel(
        hotel_id="H028", name="厦门鼓浪屿海景别墅酒店", city="厦门",
        address="鼓浪屿 龙头路", star=5, price_per_night=1088,
        facilities=["海景", "泳池", "WiFi", "早餐"],
        room_types=["海景房", "别墅"],
        rating=4.8, review_count=2233
    ),
    Hotel(
        hotel_id="H029", name="厦门中山路步行街客栈", city="厦门",
        address="思明区 中山路步行街", star=2, price_per_night=228,
        facilities=["WiFi"],
        room_types=["标准间", "大床房"],
        rating=4.2, review_count=1105
    ),
    # ---------- 青岛 ----------
    Hotel(
        hotel_id="H030", name="青岛栈桥海景假日酒店", city="青岛",
        address="市南区 太平路 栈桥旁", star=4, price_per_night=598,
        facilities=["海景", "WiFi", "早餐", "停车场"],
        room_types=["海景大床房", "标准间"],
        rating=4.6, review_count=1965
    ),
    Hotel(
        hotel_id="H031", name="青岛市南五四广场快捷酒店", city="青岛",
        address="市南区 香港中路 五四广场", star=2, price_per_night=248,
        facilities=["WiFi"],
        room_types=["标准间", "大床房"],
        rating=4.0, review_count=872
    ),
    # ---------- 天津 ----------
    Hotel(
        hotel_id="H032", name="天津意式风情区精品酒店", city="天津",
        address="河北区 意式风情区 胜利路", star=4, price_per_night=468,
        facilities=["WiFi", "早餐"],
        room_types=["大床房", "双床房"],
        rating=4.4, review_count=1188
    ),
    Hotel(
        hotel_id="H033", name="天津站前如家快捷酒店", city="天津",
        address="河东区 海河东路 天津站", star=2, price_per_night=198,
        facilities=["WiFi"],
        room_types=["标准间"],
        rating=3.8, review_count=634
    ),
    # ---------- 苏州 ----------
    Hotel(
        hotel_id="H034", name="苏州平江路园林酒店", city="苏州",
        address="姑苏区 平江路历史街区", star=5, price_per_night=988,
        facilities=["园林", "WiFi", "早餐", "茶室"],
        room_types=["园景房", "套房"],
        rating=4.7, review_count=1742
    ),
    Hotel(
        hotel_id="H035", name="苏州观前街快捷酒店", city="苏州",
        address="姑苏区 观前街", star=2, price_per_night=218,
        facilities=["WiFi"],
        room_types=["标准间", "大床房"],
        rating=4.1, review_count=1063
    ),
]

# 订单存储：由 storage 模块统一管理（自动回退内存 dict）
#  - 未配置 DATABASE_URL / SQLite/MySQL 不可用 → 内存（CLI 重启丢失，与原先 ORDER_DB 行为一致）
#  - 配了且可用 → 真实持久化（SQLite 零依赖 / MySQL 可选）
# 详见 storage.py

# 订单读取节流（进程内即可，CLI 重启归零，无需落盘）：
# - 远端状态同步：同笔订单 60 秒内不重复拉道旅详情（连续查询不重复打接口）
# - 历史列表：60 秒内同筛选条件复用结果，下单/支付/取消后主动失效
_SYNC_TTL = 60
_HISTORY_TTL = 60
_ORDER_SYNC_TS: dict = {}      # order_id -> 上次同步尝试时间戳
_HISTORY_CACHE: dict = {}      # status_filter -> (时间戳, 结果)


def _invalidate_history_cache() -> None:
    """订单写入/状态变更后失效历史列表缓存，保证写后读最新。"""
    _HISTORY_CACHE.clear()

# 实时搜索结果缓存（道旅/高德）：进程内按 hotel_id 保存最近搜到的真实酒店，
# 使 get_hotel_detail 也能查到非模拟库的酒店（下单前详情确认等场景）
_RUNTIME_HOTEL_CACHE: dict = {}


def _cache_runtime_hotels(hotels: List[Hotel]) -> None:
    for h in hotels:
        _RUNTIME_HOTEL_CACHE[h.hotel_id] = h


def _enrich_ratings_safely(hotels: List[Hotel]) -> None:
    """为真实酒店批量补真实住客评分（默认只补前8家，模块内7天缓存）；
    评分服务任何异常都不影响订房主流程。"""
    if not rating_enabled():
        return
    try:
        n = enrich_hotels_rating(hotels)
        if n:
            print(f"[评分] 已为 {n} 家酒店补全真实住客评分（来源：携程/百度/高德）")
    except Exception as e:
        print(f"[评分服务异常，已忽略] {type(e).__name__}: {e}")


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
            _enrich_ratings_safely(results)
            _cache_runtime_hotels(results)
            return results
        except Exception as e:
            print(f"[道旅MCP调用失败，尝试高德POI] {e}")

    if params.city and amap_enabled():
        try:
            results = search_amap_hotels(params)
            _enrich_ratings_safely(results)
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

    # 按位置/商圈匹配（酒店名或地址命中地标词；纯"市中心"等泛词无法判定则跳过）
    if params.location:
        from rollinggo_mcp import _location_keywords
        loc_keys = [k for k in _location_keywords(params.location)
                    if k != params.city]   # 剥后缀可能得到城市名本身，排除
        if loc_keys:
            results = [
                h for h in results
                if any(k in h.name or k in (h.address or "") for k in loc_keys)
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

    # 搜索阶段没配上评分（或当时还没配 Key）时，查详情再补一次真实评分
    if live and rating_enabled() and cached.rating == 0:
        try:
            enrich_hotel_rating(cached)
        except Exception as e:
            print(f"[评分服务异常，已忽略] {type(e).__name__}: {e}")
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
    storage.insert_order(order)
    _invalidate_history_cache()
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
    storage.insert_order(order)
    _invalidate_history_cache()
    return order


def pay_order(order_id: str) -> bool:
    """
    工具5：支付订单
    高危操作——真实场景需要接入支付网关 + 短信验证
    这里模拟支付成功
    """
    ok = storage.update_status(order_id, "paid")
    if ok:
        _invalidate_history_cache()
    return ok


def sync_real_order_status(order: Order) -> None:
    """
    道旅真实订单以远端状态为准：本地 ORDER_DB 只是下单时的快照，
    用户在网页上支付/取消后本地不会自动更新。查询类操作前调用本函数
    拉一次订单详情同步状态（失败静默，沿用本地快照）。
    """
    if order.source != "rollinggo":
        return
    now = time.time()
    if now - _ORDER_SYNC_TS.get(order.order_id, 0) < _SYNC_TTL:
        return
    # 记录尝试时刻：远端暂时失败时也不会在 TTL 内被连环重试
    _ORDER_SYNC_TS[order.order_id] = now
    try:
        import rollinggo_book as rlg
        if not rlg.is_logged_in():
            return
        rows = rlg.parse_orders(rlg.get_order_detail(order.order_id))
        if not rows:
            return
        bucket = _order_status_bucket(rows[0])
        order.status = {"CANCELLED": "cancelled",
                        "PENDING": "pending"}.get(bucket, "paid")
    except Exception:
        pass


def cancel_order(order_id: str) -> str:
    """
    工具6：取消本地模拟订单。
    真实订单请走 pre_cancel_real_order() / confirm_cancel_real_order()。
    返回："ok" 已取消 / "remote_unsupported" 真实单不可本地取消 / "not_found"
    """
    order = storage.get_order(order_id)
    if order is None or order.status == "cancelled":
        return "not_found"
    # 真实订单必须走 pre_cancel_real_order / confirm_cancel_real_order
    # （远端成功后才能改本地状态）；这里只处理本地模拟单，避免误标已取消。
    if order.source == "rollinggo":
        return "remote_unsupported"
    storage.update_status(order_id, "cancelled")
    _invalidate_history_cache()
    return "ok"


def real_order_cancel_guide(order_id: str) -> str:
    """取消接口不可用时的兜底指引（agent / multi_agent 共用）。"""
    return (
        f"订单 {order_id} 暂时无法通过助手在线取消。\n"
        f"请打开道旅订单详情页手动取消：\n"
        f"  https://rollinggo.cn/pc/#/hotel/detail?subOrderNo={order_id}\n"
        f"是否免费取消以页面【取消政策】的截止时间为准；"
        f"订单尚未支付的话，不支付就不会扣款。"
    )


def find_order(order_id: str) -> Optional[Order]:
    """
    按订单号查本地订单；本地没有时回退查道旅订单详情。

    Web 订单页的订单来自远端列表，并不一定写入本地 SQLite；
    取消/查详情不能只查 storage，否则远端订单会一直被判定“未找到”。
    """
    order = storage.get_order(order_id)
    if order is not None:
        return order

    if not order_id:
        return None
    try:
        import rollinggo_book as rlg
        if not rlg.is_logged_in():
            return None
        rows = rlg.parse_orders(rlg.get_order_detail(str(order_id)))
        if not rows:
            return None
        row = rows[0]
        bucket = _order_status_bucket(row)
        return Order(
            order_id=row.get("order_no") or str(order_id),
            hotel_id="",
            hotel_name=row.get("hotel_name") or "道旅订单",
            check_in=row.get("check_in") or "",
            check_out=row.get("check_out") or "",
            room_type=row.get("room_name") or "",
            guest_name="",
            total_price=float(row.get("total_price") or 0),
            status={"CANCELLED": "cancelled",
                    "PENDING": "pending"}.get(bucket, "paid"),
            source="rollinggo",
            payment_url=row.get("payment_url") or "",
        )
    except Exception:
        return None


def pre_cancel_real_order(order_id: str) -> dict:
    """
    真实订单取消询价：返回罚金、凭证和客服提示，不修改订单。
    结果中的 ok=True 才表示“可以进入确认取消”；confirm_id 10 分钟有效。
    """
    local = storage.get_order(order_id)
    if local is not None and local.source != "rollinggo":
        return {"ok": False, "reason": "not_real",
                "message": "该订单是本地模拟订单，可直接在对话里回复【取消订单】取消。"}

    try:
        import rollinggo_book as rlg
        if not rlg.is_logged_in():
            return {"ok": False, "reason": "not_logged_in",
                    "order_id": order_id,
                    "message": "道旅账号未登录，请先在项目目录运行：python rollinggo_book.py login"}
    except Exception:
        pass

    order = local or find_order(order_id)
    if order is None:
        return {"ok": False, "reason": "not_found",
                "message": f"未找到订单 {order_id}，请确认订单号是否正确。"}
    if order.source != "rollinggo":
        return {"ok": False, "reason": "not_real",
                "message": "该订单是本地模拟订单，可直接在对话里回复【取消订单】取消。"}

    sync_real_order_status(order)
    if order.status == "cancelled":
        return {"ok": False, "reason": "already_cancelled",
                "order_id": order.order_id,
                "message": f"订单 {order.order_id} 已经是取消状态了。"}

    try:
        import rollinggo_book as rlg
        if not rlg.is_logged_in():
            return {"ok": False, "reason": "not_logged_in",
                    "order_id": order.order_id,
                    "message": "道旅账号未登录，请先在项目目录运行：python rollinggo_book.py login"}
        quote = rlg.pre_cancel(order.order_id)
    except Exception as e:
        return {"ok": False, "reason": "api_error",
                "order_id": order.order_id,
                "message": f"取消询价失败：{e}"}

    penalty = quote.get("penalty")
    result = {
        "ok": bool(quote.get("can_cancel")),
        "reason": "quoted" if quote.get("can_cancel") else "not_cancellable",
        "order_id": order.order_id,
        "hotel_name": order.hotel_name,
        "check_in": order.check_in,
        "check_out": order.check_out,
        "total_price": float(order.total_price or 0),
        "can_cancel": bool(quote.get("can_cancel")),
        "penalty": penalty,
        "confirm_id": quote.get("confirm_id") or "",
        "booking_id": quote.get("booking_id") or "",
        "message": quote.get("message") or "",
        "expires_at": time.time() + 600,
    }
    if not result["ok"]:
        result["message"] = result["message"] or \
            "该订单按规则无法在线取消，请联系道旅客服处理。"
    return result


def confirm_cancel_real_order(order_id: str, booking_id: str,
                              confirm_id: str, description: str = "") -> dict:
    """
    确认取消真实订单（不可逆）。只有远端返回成功后才更新本地状态，
    绝不在失败时把本地订单标成 cancelled。
    """
    if not order_id or not booking_id or not confirm_id:
        return {"ok": False, "reason": "invalid_request",
                "message": "取消凭证不完整，请重新发起取消并在 10 分钟内确认。"}

    try:
        import rollinggo_book as rlg
        if not rlg.is_logged_in():
            return {"ok": False, "reason": "not_logged_in",
                    "message": "道旅账号未登录，请先运行：python rollinggo_book.py login"}
        result = rlg.confirm_cancel(order_id, booking_id, confirm_id,
                                    description or None)
    except Exception as e:
        return {"ok": False, "reason": "api_error",
                "order_id": order_id,
                "message": f"取消失败：{e}"}

    if not result.get("success"):
        return {"ok": False, "reason": "failed",
                "order_id": order_id,
                "message": result.get("message") or "道旅未确认取消，请稍后重试或联系客服。"}

    storage.update_status(order_id, "cancelled")
    _invalidate_history_cache()
    order = storage.get_order(order_id)
    if order is not None:
        order.status = "cancelled"
    return {"ok": True, "reason": "cancelled",
            "order_id": order_id,
            "message": result.get("message") or "订单已成功取消。"}


def get_order_status(order_id: str) -> Optional[Order]:
    """工具7：查询订单状态（售后场景）"""
    return storage.get_order(order_id)


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
        "payment_deadline": "",
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
    # 道旅未支付订单常见 orderStatus=Booked（展示为“已确认”）+ paymentUrl。
    # 不能只看订单状态，否则这类订单会被误归到 FINISHED，PENDING 过滤为空。
    if row.get("payment_url") and not any(
            k in text for k in ("已支付", "已完成", "已取消")):
        return "PENDING"
    return "FINISHED"


def order_status_bucket(row: dict) -> str:
    """公开的状态桶（PENDING / CANCELLED / FINISHED），Web 层据此渲染操作按钮。"""
    return _order_status_bucket(row)


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

    # 60 秒内同筛选条件直接复用上次结果（连续两次"查历史订单"只打一次接口）
    cached = _HISTORY_CACHE.get(flt)
    if cached and time.time() - cached[0] < _HISTORY_TTL:
        return cached[1]

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
        all_local = storage.list_all_orders()
        remote_ids = {r.get("order_no") for r in rows}
        for order in all_local:
            if order.order_id not in remote_ids:
                rows.append(_local_order_to_row(order))
        source = "rollinggo"
    else:
        rows = [_local_order_to_row(o) for o in storage.list_all_orders()]
        source = "local" if rows else "none"

    if flt != "ALL":
        rows = [r for r in rows if _order_status_bucket(r) == flt]

    result = {
        "logged_in": logged_in,
        "source": source,
        "filter": flt,
        "rows": rows,
        "remote_error": remote_error,
    }
    _HISTORY_CACHE[flt] = (time.time(), result)
    return result


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


HOTEL_PREVIEW = 5   # 首屏展示家数；其余由前端“展开全部”按需补评分后展示


def _format_hotel_block(i: int, h: Hotel) -> str:
    """格式化单家酒店；序号由调用方传入，展开时分段展示可延续编号。"""
    # 高德 POI 数据中星级/价格/评分为 0 表示未知，展示层做友好兜底
    star_text = f"{h.star}星" if h.star else "星级未知"
    price_text = f"{h.price_per_night:g}元/晚" if h.price_per_night else "价格以实际预订为准"
    if h.rating:
        meta = []
        if h.review_count:
            meta.append(f"{h.review_count}条真实评价")
        if h.recommend_rate:
            meta.append(f"好评率{h.recommend_rate}")
        if h.rating_source:
            meta.append(f"来源：{h.rating_source}")
        rating_text = f"评分：{h.rating:g}"
        if meta:
            rating_text += f"（{'，'.join(meta)}）"
    else:
        rating_text = "暂无评分"
    facilities_str = "、".join(h.facilities[:4]) if h.facilities else "信息待补充"
    distance_text = ""
    if h.distance_m and h.distance_to:
        # 距离由数据方给出，取整到 10 米，避免伪精确
        dist_m = int(round(h.distance_m / 10.0) * 10)
        distance_text = f"\n   距离：距{h.distance_to}约{dist_m}米"
    block = (
        f"{i}. {h.name}（{star_text}）\n"
        f"   价格：{price_text} | {rating_text}\n"
        f"   地址：{h.address}{distance_text}\n"
        f"   设施：{facilities_str}\n"
        f"   房型：{'、'.join(h.room_types)}"
    )
    if h.image_url:
        block += f"\n   实拍图：{h.image_url}"
    return block


def format_hotel_list(hotels: List[Hotel], max_show: int = HOTEL_PREVIEW) -> str:
    """工具8：格式化酒店列表为可读文本（供 Agent 展示给用户）"""
    if not hotels:
        return "没有找到符合条件的酒店，建议放宽筛选条件。"
    lines = [_format_hotel_block(i, h)
             for i, h in enumerate(hotels[:max_show], 1)]
    if len(hotels) > max_show:
        lines.append(f"... 还有 {len(hotels) - max_show} 家，可告知更多偏好帮你筛选")
    # 有真实图片时提示链接可点击（模拟库无实拍图则不提示，避免噪音）
    if any(h.image_url for h in hotels[:max_show]):
        lines.append("（实拍图链接可在终端中 Ctrl+点击 打开查看，图片由数据方提供）")
    return "\n".join(lines)


def format_hotel_slice(hotels: List[Hotel], start: int,
                       count: int) -> str:
    """格式化 hotels[start:start+count]，编号从 start+1 延续。

    供 Web 端“展开全部”分段调用：每段先补真实评分再格式化，
    保证展开部分与首屏使用同一套展示规则。
    """
    rows = hotels[start:start + count]
    return "\n".join(_format_hotel_block(start + i, h)
                     for i, h in enumerate(rows, 1))
