# 高德地图 POI API 适配器
#
# 用途：用高德"文本搜索"接口获取国内城市的真实酒店 POI 列表，
#       解决模拟数据库 HOTEL_DB 只有 4 个城市的问题。
#
# 能力边界（重要）：
# - 高德 POI 提供：名称、类型、地址、坐标、行政区划；extensions=all 时
#   酒店类 POI 还可能在 biz_ext.rating 返回真实住客评分（无评价条数/好评率）
# - 不提供：实时房价、空房、评价条数、设施、可下单房型
#   因此价格/设施类筛选在真实模式下不生效（仅城市 + 关键词由服务端过滤）
#
# 申请方式（必须申请"Web服务"类型的 Key，JS API 的 Key 不能用于服务端调用）：
# 1. 注册并登录 https://lbs.amap.com/
# 2. 控制台 → 应用管理 → 创建新应用 → 添加 Key，服务平台选择【Web服务】
# 3. 把 Key 填入同目录 .env：AMAP_API_KEY=你的key
#
# 接口文档：https://lbs.amap.com/api/webservice/guide/api/search

import os
import re
from typing import List

import requests

from geo_data import haversine_km
from models import BookingParams, Hotel
from text_utils import normalize_name as _norm_name, name_similarity

# 从 .env 文件加载环境变量（与 llm.py / rag.py 保持一致，兼容任意导入顺序与工作目录）
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
                override=True)
except ImportError:
    pass

# 文本搜索 v3 接口（REST，服务端 Key 直接调用，无需签名）
_AMAP_TEXT_SEARCH_URL = "https://restapi.amap.com/v3/place/text"
_TIMEOUT = (5, 8)

# 住宿服务大类（含宾馆酒店、旅馆招待所、民宿等）
_ACCOMMODATION_TYPE = "100000"

# POI type 文本里的星级词 → 数字
_STAR_WORDS = {"五": 5, "五钻": 5, "四": 4, "四钻": 4,
               "三": 3, "三钻": 3, "二": 2, "一": 1}

# 高德 POI 无房型数据，预订流程又要求房型在可选列表中，
# 这里给一个通用默认值，保证演示闭环（选酒店→下单）不中断
_DEFAULT_ROOM_TYPES = ["标准间", "大床房"]


def get_api_key() -> str:
    """读取高德 Web 服务 Key（.env 已由 llm.py/rag.py 顶部的 load_dotenv 加载）"""
    return os.environ.get("AMAP_API_KEY", "").strip()


def is_enabled() -> bool:
    """是否启用高德真实数据模式"""
    return bool(get_api_key())


def _parse_star(type_text: str) -> int:
    """
    从 POI 类型串中解析星级，例如：
    "住宿服务;宾馆酒店;五星级宾馆酒店" → 5
    "住宿服务;宾馆酒店;经济型连锁酒店" → 0（未知）
    """
    for word, star in _STAR_WORDS.items():
        if f"{word}星级" in type_text:
            return star
    return 0


def _build_address(poi: dict) -> str:
    """拼接 省+市+区+详细地址，自动跳过空段和重复段（直辖市 cityname 常为空）"""
    parts = [poi.get("pname", ""), poi.get("cityname", ""),
             poi.get("adname", ""), poi.get("address", "")]
    seen = set()
    result = []
    for p in parts:
        p = (p or "").strip()
        if p and p not in seen:
            seen.add(p)
            result.append(p)
    return "".join(result)


def _extract_photo(poi: dict) -> str:
    """
    取 POI 实拍首图：extensions=all 时高德返回 photos 数组，
    形如 [{"title": "", "url": "http://1024.../xxx.jpg"}]。无图返回空串。
    """
    photos = poi.get("photos")
    if isinstance(photos, list):
        for p in photos:
            if isinstance(p, dict):
                url = str(p.get("url") or "").strip()
                if url:
                    return url
            elif isinstance(p, str) and p.strip():
                return p.strip()
    return ""


# 高德会把酒店的附属设施单列成 POI（大堂/停车场/餐厅…），它们的图片不是
# 酒店外观，单店补图时必须排除，否则会把停车场照片当封面
_FACILITY_POI = re.compile(
    r"(停车场|大堂|前台|礼宾部|商务中心|会议室|宴会厅|餐厅|入口|侧门|电梯|售票处)")


def _poi_lat_lng(poi: dict) -> tuple:
    """高德 location 形如 '经度,纬度'（GCJ02），返回 (纬度, 经度)。"""
    loc = str(poi.get("location") or "")
    if "," not in loc:
        return 0.0, 0.0
    try:
        lng_str, lat_str = loc.split(",", 1)
        return float(lat_str), float(lng_str)
    except ValueError:
        return 0.0, 0.0


def _to_hotel(poi: dict, city: str) -> Hotel:
    """把高德 POI 映射成项目统一 Hotel 模型"""
    type_text = poi.get("type", "")
    # 高德 location 形如 "经度,纬度"（GCJ02 火星坐标），用于后续评分 POI 匹配
    lat = lng = 0.0
    loc = str(poi.get("location") or "")
    if "," in loc:
        try:
            lng_str, lat_str = loc.split(",", 1)
            lat, lng = float(lat_str), float(lng_str)
        except ValueError:
            pass
    # extensions=all 时 biz_ext.rating 为真实住客评分（字符串，如 "4.8"），
    # 直接随搜索响应取到，无需额外请求；无评分的 POI 留 0 由 hotel_rating 补
    try:
        rating = float((poi.get("biz_ext") or {}).get("rating") or 0)
    except (TypeError, ValueError):
        rating = 0.0
    return Hotel(
        hotel_id=f"AMAP_{poi.get('id', '')}",
        name=poi.get("name", "未知酒店"),
        city=city,
        address=_build_address(poi),
        star=_parse_star(type_text),
        price_per_night=0.0,   # POI 无价格，0 表示未知（展示层特殊处理）
        facilities=[],         # POI 无设施明细
        room_types=list(_DEFAULT_ROOM_TYPES),
        rating=rating,         # 真实评分（无则 0），来源标注高德地图
        review_count=0,        # 高德接口不提供评价条数
        rating_source="高德地图" if rating > 0 else "",
        latitude=lat,
        longitude=lng,
        image_url=_extract_photo(poi),
    )


def search_pois(keywords: str, city: str = "", *,
                offset: int = 20, page: int = 1,
                location: str = "", radius: int = 5000,
                sortrule: str = "", timeout=_TIMEOUT) -> List[dict]:
    """
    高德文本搜索统一入口：返回 pois 原始列表，失败抛异常。

    location 传入 "经度,纬度" 时按距离排序并限定 radius；调用方自行决定
    是否静默降级（搜酒店需要报错，补图/评分可以静默）。
    """
    params = {
        "key": get_api_key(),
        "keywords": keywords,
        "types": _ACCOMMODATION_TYPE,
        "city": city or "",
        "citylimit": "true",
        "offset": offset,
        "page": page,
        # all：返回 biz_ext.rating 真实住客评分（base 不含该字段）
        "extensions": "all",
        "output": "JSON",
    }
    if location:
        params.update({
            "location": location,
            "radius": radius,
            "sortrule": sortrule or "distance",
        })
    response = requests.get(_AMAP_TEXT_SEARCH_URL, params=params,
                            timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if str(data.get("status")) != "1":
        raise RuntimeError(
            f"高德API错误 infocode={data.get('infocode')} info={data.get('info')}"
        )
    return data.get("pois") or []


def search_real_hotels(params: BookingParams,
                       page_size: int = 20, page: int = 1) -> List[Hotel]:
    """
    调用高德文本搜索查询真实酒店
    - 城市：city 参数直接传中文名（如"成都"），citylimit=true 限定本市
    - 关键词：海景/亲子等关键词拼入 keywords 由高德做名称匹配
    """
    # 位置（市中心/商圈/地标附近）与酒店属性词都拼入检索词，
    # 高德文本搜索支持地标语义，如"春熙路步行街附近酒店"
    keyword_parts = []
    if params.location:
        keyword_parts.append(params.location)
    if params.keyword:
        keyword_parts.append(params.keyword)
    keywords = "".join(keyword_parts) + "酒店" if keyword_parts else "酒店"
    pois = search_pois(keywords, params.city, offset=page_size, page=page)
    return [_to_hotel(poi, params.city)
            for poi in pois if poi.get("id") and poi.get("name")]


def fetch_hotel_photo(hotel: Hotel) -> str:
    """单店高德补图（图片三级源）：返回真实实拍图直链，失败返回空串。

    只在道旅、携程都给不出封面时调用。按酒店名 + 城市做一次文本检索，
    名称相似度 ≥ 0.85 且（坐标可得时）距离 ≤ 3km 才采纳，避免同名错图。
    道旅坐标 WGS84、高德 GCJ02，两者偏差约数百米，3km 阈值已留足余量。
    """
    if not is_enabled() or not hotel.name:
        return ""
    # 有坐标时按距离排序：泛泛的酒店名（如"北京饭店"）在无坐标检索里
    # 常被同名/近名 POI 挤到 25 名之外，带坐标可稳定把目标拉进视野
    location = (f"{hotel.longitude},{hotel.latitude}"
                if hotel.latitude and hotel.longitude else "")
    try:
        pois = search_pois(hotel.name, hotel.city or "", offset=25,
                           location=location, radius=5000,
                           sortrule="distance")
    except Exception:
        return ""
    best, best_key = None, None
    nn = _norm_name(hotel.name)
    for poi in pois:
        name = poi.get("name", "")
        if _FACILITY_POI.search(name):
            continue
        cn = _norm_name(name)
        if not nn or not cn:
            continue
        sim = name_similarity(hotel.name, name)
        # 强匹配：归一后互相包含且长度比 ≥ 0.6
        # （"广州白天鹅宾馆" vs "白天鹅宾馆"、"XX酒店" vs "XX酒店(中街店)"）
        shorter, longer = sorted((len(nn), len(cn)))
        strong = (nn in cn or cn in nn) and shorter / longer >= 0.6
        if not strong and sim < 0.85:
            continue
        if hotel.latitude and hotel.longitude:
            lat, lng = _poi_lat_lng(poi)
            if (lat and lng and haversine_km(hotel.latitude, hotel.longitude,
                                             lat, lng) > 3.0):
                continue
        key = (2 if strong else 1, sim)
        if best_key is None or key > best_key:
            best, best_key = poi, key
    if best is None:
        return ""
    # 优先取高德官方店铺图（store.is.autonavi.com），用户点评图（aos-comment）
    # 常是客房内景，作为酒店封面不如官方外观图合适，仅作兜底
    best_poi_urls = [str(p.get("url") or "").strip()
                     for p in (best.get("photos") or []) if isinstance(p, dict)]
    best_poi_urls = [u for u in best_poi_urls if u]
    official = [u for u in best_poi_urls if "store.is.autonavi.com" in u]
    url = (official or best_poi_urls or [""])[0]
    # 部分图片是 http://，统一升级 https，避免网页混合内容被浏览器拦截
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return url
