# 真实住客评分富化 —— 百度地图优先、高德地图兜底
#
# 背景：道旅 RollingGo 只提供官方星级（starRating），不提供用户点评；
#       OTA（携程等）接口有签名风控无法对接。本模块只展示地图厂商接口
#       返回的真实用户评分，并在每条结果上标注数据来源，绝不编造评分。
#
# 数据源（任一可用即生效）：
# 1. 百度地图（优先：评分 + 评价条数 + 好评率三字段最全）
#    需【服务端】AK，填入 .env：BAIDU_MAP_AK=你的AK
#    申请：https://lbsyun.baidu.com/ → 控制台 → 应用管理 → 创建应用
#    应用类型选【服务端】（注意：服务端 AK 需要企业认证）
# 2. 高德地图（兜底：百度未配置/未命中时自动使用；个人身份证实名即可）
#    申请：https://console.amap.com/ → 应用管理 → 创建应用 → 添加 Key
#    服务平台必须选【Web服务】，把 Key 填入同目录 .env：AMAP_API_KEY=你的key
#    文本搜索 extensions=all 时，酒店类 POI 的 biz_ext.rating 返回真实评分，
#    但接口不提供评价条数 / 好评率。
#
# 设计要点：
# - 名称 + 坐标双重匹配，避免同名酒店张冠李戴：道旅坐标为 WGS84、
#   高德返回 GCJ02、百度返回 BD09，匹配前按数据源做坐标转换
# - 两家都配置时优先用百度（字段更全），百度未命中再回退高德；
#   只配置一家也能独立工作
# - 磁盘缓存（~/.hotel-cli/ratings_cache.json，7 天有效）：评分变化慢，
#   避免每次搜索都批量请求；查无结果做 3 天负缓存
# - 免费接口额度有限，默认只富化列表前 8 家酒店（教学演示足够）
# - 未配置 Key 或任何调用失败都静默降级为"暂无评分"，绝不阻塞订房主流程

import os
import re
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

import requests

from models import Hotel

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
                override=True)
except ImportError:
    pass

_AMAP_SEARCH_URL = "https://restapi.amap.com/v3/place/text"
_BAIDU_SEARCH_URL = "https://api.map.baidu.com/place/v2/search"
_ACCOMMODATION_TYPE = "100000"      # 高德住宿服务大类
_TIMEOUT = (5, 8)                   # (连接, 读取) 秒
_MAX_WORKERS = 5                    # 批量富化并发数
_ENRICH_LIMIT = 8                   # 每次搜索最多富化前 N 家（节省免费额度）
_CACHE_TTL = 7 * 24 * 3600         # 命中缓存 7 天
_NEG_CACHE_TTL = 3 * 24 * 3600     # 查无评分 3 天内不再请求
_CACHE_PATH = os.path.join(os.path.expanduser("~"), ".hotel-cli",
                           "ratings_cache.json")

# 名称与坐标双匹配的距离阈值（公里，已预留坐标系转换误差）
_DIST_EXACT = 3.0
_DIST_FUZZY = 1.0

_notice_shown = False
_mem_cache: Dict[str, dict] = {}


# ========== Key 与开关 ==========

def amap_api_key() -> str:
    return os.environ.get("AMAP_API_KEY", "").strip()


def baidu_api_key() -> str:
    return os.environ.get("BAIDU_MAP_AK", "").strip()


def amap_enabled() -> bool:
    return bool(amap_api_key())


def baidu_enabled() -> bool:
    return bool(baidu_api_key())


def is_enabled() -> bool:
    """高德 / 百度任一配置即可用"""
    return amap_enabled() or baidu_enabled()


def _notice(msg: str) -> None:
    """配置/接口类提示只打印一次，避免批量场景刷屏"""
    global _notice_shown
    if not _notice_shown:
        print(msg)
        _notice_shown = True


# ========== 坐标转换：WGS84（GPS/道旅）→ GCJ02（高德/火星）→ BD09（百度）==========

_A = 6378245.0
_EE = 0.00669342162296594323
_X_PI = math.pi * 3000.0 / 180.0


def _transform_lat(x: float, y: float) -> float:
    ret = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y +
           0.1 * x * y + 0.2 * math.sqrt(abs(x)))
    ret += (20.0 * math.sin(6.0 * x * math.pi) +
            20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) +
            40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) +
            320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(x: float, y: float) -> float:
    ret = (300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y +
           0.1 * math.sqrt(abs(x)))
    ret += (20.0 * math.sin(6.0 * x * math.pi) +
            20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) +
            40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) +
            300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lat: float, lng: float) -> tuple:
    """WGS84（GPS/道旅坐标）→ GCJ02（火星坐标/高德坐标）。"""
    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / (_A / sqrtmagic * math.cos(radlat) * math.pi)
    return lat + dlat, lng + dlng


def gcj02_to_bd09(lat: float, lng: float) -> tuple:
    """GCJ02（火星坐标）→ BD09（百度坐标）。入参/返回均为 (纬度, 经度)。"""
    z = math.sqrt(lng * lng + lat * lat) + 0.00002 * math.sin(lat * _X_PI)
    theta = math.atan2(lat, lng) + 0.000003 * math.cos(lng * _X_PI)
    return z * math.sin(theta) + 0.006, z * math.cos(theta) + 0.0065


def wgs84_to_bd09(lat: float, lng: float) -> tuple:
    """WGS84 经纬度转百度 BD09 经纬度。入参/返回均为 (纬度, 经度)。"""
    mglat, mglng = wgs84_to_gcj02(lat, lng)
    return gcj02_to_bd09(mglat, mglng)


def hotel_to_gcj02(hotel: "Hotel") -> Optional[tuple]:
    """酒店坐标转 GCJ02：高德来源原生即 GCJ02，道旅等 WGS84 需转换。"""
    if not (hotel.latitude and hotel.longitude):
        return None
    if hotel.hotel_id.startswith("AMAP_"):
        return hotel.latitude, hotel.longitude
    return wgs84_to_gcj02(hotel.latitude, hotel.longitude)


def hotel_to_bd09(hotel: "Hotel") -> Optional[tuple]:
    """酒店坐标转 BD09：高德来源 GCJ02→BD09，道旅等 WGS84→GCJ02→BD09。"""
    if not (hotel.latitude and hotel.longitude):
        return None
    if hotel.hotel_id.startswith("AMAP_"):
        return gcj02_to_bd09(hotel.latitude, hotel.longitude)
    return wgs84_to_bd09(hotel.latitude, hotel.longitude)


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    dl, dn = math.radians(lng2 - lng1), math.radians(lat2 - lat1)
    a = (math.sin(dn / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dl / 2) ** 2)
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ========== 缓存 ==========

def _cache_key(hotel: Hotel) -> str:
    return f"{hotel.city or ''}|{hotel.name}"


def _load_cache() -> dict:
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("items", {})
    except Exception:
        return {}


def _save_cache(items: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        tmp = _CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 2, "items": items}, f, ensure_ascii=False)
        os.replace(tmp, _CACHE_PATH)
    except Exception:
        pass


def _cached_get(key: str) -> Optional[dict]:
    """返回缓存记录；过期/不存在返回 None。负缓存记录 rating 为 0。"""
    rec = _mem_cache.get(key)
    if rec is None:
        rec = _load_cache().get(key)
        if rec:
            _mem_cache[key] = rec
    # 旧版（v1 百度单源）记录无 source 字段，视为未缓存以便按新双源重查
    if not rec or "source" not in rec:
        return None
    ttl = _CACHE_TTL if rec.get("rating", 0) > 0 else _NEG_CACHE_TTL
    if time.time() - rec.get("ts", 0) > ttl:
        return None
    return rec


def _cache_put(key: str, rec: dict) -> None:
    rec = dict(rec)
    rec["ts"] = int(time.time())
    _mem_cache[key] = rec
    try:
        items = _load_cache()
        items[key] = rec
        _save_cache(items)
    except Exception:
        pass


# ========== 名称归一与双匹配 ==========

def _norm_name(name: str) -> str:
    """名称归一：去括号/标点/空格，只留中英文数字与汉字核心。"""
    return re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]", "", name or "")


def _name_score(a: str, b: str) -> int:
    """名称相似度分级：3 完全相同；2 互相包含；1 高重叠；0 不相似。"""
    a, b = _norm_name(a), _norm_name(b)
    if not a or not b:
        return 0
    if a == b:
        return 3
    if a in b or b in a:
        return 2
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 4:
        overlap = sum(1 for ch in set(short) if ch in long_)
        if overlap / len(set(short)) >= 0.75:
            return 1
    return 0


def _to_int(v) -> int:
    """comment_num 可能是 '1,234' / 1234 / '' 等形态"""
    if isinstance(v, (int, float)):
        return int(v)
    digits = re.sub(r"[^\d]", "", str(v or ""))
    return int(digits) if digits else 0


def _match_candidate(hotel: Hotel, candidates: List[tuple],
                     hotel_xy: Optional[tuple]) -> Optional[dict]:
    """
    名称 + 坐标双重匹配最可能的同一家酒店。
    candidates 每项：(POI名称, 纬度, 经度, 原始POI dict)，坐标须与 hotel_xy 同坐标系。
    """
    best, best_key = None, None
    for name, lat, lng, raw in candidates:
        ns = _name_score(hotel.name, name)
        if ns == 0:
            continue
        if hotel_xy and lat and lng:
            dist = _haversine_km(hotel_xy[0], hotel_xy[1], lat, lng)
        else:
            dist = None
        if ns >= 2 and (dist is None or dist <= _DIST_EXACT):
            key = (2, -(dist if dist is not None else 99.0))
        elif ns == 1 and dist is not None and dist <= _DIST_FUZZY:
            key = (1, -dist)
        else:
            continue
        if best_key is None or key > best_key:
            best, best_key = raw, key
    return best


# ========== 高德源（GCJ02，只有评分）==========

def _fetch_amap_pois(hotel: Hotel) -> List[dict]:
    """按酒店名 + 城市做高德文本检索（extensions=all 取 biz_ext.rating）。"""
    resp = requests.get(
        _AMAP_SEARCH_URL,
        params={
            "key": amap_api_key(),
            "keywords": hotel.name,
            "types": _ACCOMMODATION_TYPE,
            "city": hotel.city or "",
            "citylimit": "true",
            "offset": 20,
            "page": 1,
            "extensions": "all",
            "output": "json",
        },
        timeout=_TIMEOUT,
    )
    data = resp.json()
    # 高德固定用 status=="1" 表示成功
    if str(data.get("status")) != "1":
        _notice(f"[高德评分服务] 接口返回 status={data.get('status')} "
                f"infocode={data.get('infocode')} {data.get('info') or ''}"
                f"（本次酒店列表不显示真实评分，不影响订房）")
        return []
    return data.get("pois") or []


def _amap_candidates(pois: List[dict]) -> List[tuple]:
    """高德 location 形如 '经度,纬度'（GCJ02）。"""
    out = []
    for poi in pois:
        lat = lng = 0.0
        loc = str(poi.get("location") or "")
        if "," in loc:
            try:
                lng_str, lat_str = loc.split(",", 1)
                lat, lng = float(lat_str), float(lng_str)
            except ValueError:
                pass
        out.append((poi.get("name", ""), lat, lng, poi))
    return out


def _rating_from_amap(hotel: Hotel) -> Optional[dict]:
    pois = _fetch_amap_pois(hotel)
    poi = _match_candidate(hotel, _amap_candidates(pois), hotel_to_gcj02(hotel))
    if not poi:
        return None
    biz = poi.get("biz_ext") or {}
    try:
        rating = float(biz.get("rating") or 0)
    except (TypeError, ValueError):
        rating = 0.0
    if rating <= 0:
        return None
    # 高德接口不提供评价条数 / 好评率
    return {"rating": rating, "count": 0, "recommend": "", "source": "高德地图"}


# ========== 百度增强源（BD09，评分 + 评价数 + 好评率）==========

def _fetch_baidu_pois(hotel: Hotel) -> List[dict]:
    """按酒店名 + 城市做百度文本检索（scope=2 取评价明细）。"""
    resp = requests.get(
        _BAIDU_SEARCH_URL,
        params={
            "query": hotel.name,
            "region": hotel.city or "",
            "city_limit": "true",
            "scope": "2",
            "tag": "酒店",
            "page_size": 20,
            "output": "json",
            "ak": baidu_api_key(),
        },
        timeout=_TIMEOUT,
    )
    data = resp.json()
    if data.get("status") != 0:
        _notice(f"[百度评分服务] 接口返回 status={data.get('status')} "
                f"{data.get('message') or ''}（本次酒店列表不显示真实评分，"
                f"不影响订房）")
        return []
    return data.get("results", []) or []


def _baidu_candidates(pois: List[dict]) -> List[tuple]:
    """百度 location 为 {lat, lng}（BD09）。"""
    out = []
    for poi in pois:
        loc = poi.get("location") or {}
        try:
            lat, lng = float(loc.get("lat") or 0), float(loc.get("lng") or 0)
        except (TypeError, ValueError):
            lat = lng = 0.0
        out.append((poi.get("name", ""), lat, lng, poi))
    return out


def _rating_from_baidu(hotel: Hotel) -> Optional[dict]:
    pois = _fetch_baidu_pois(hotel)
    poi = _match_candidate(hotel, _baidu_candidates(pois), hotel_to_bd09(hotel))
    if not poi:
        return None
    detail = poi.get("detail_info") or {}
    try:
        rating = float(detail.get("overall_rating") or 0)
    except (TypeError, ValueError):
        rating = 0.0
    if rating <= 0:
        return None
    return {"rating": rating,
            "count": _to_int(detail.get("comment_num")),
            "recommend": str(detail.get("good_recommend") or ""),
            "source": "百度地图"}


# ========== 单店查询（双源回退）==========

def fetch_rating(hotel: Hotel) -> Optional[dict]:
    """
    查询单家酒店的真实评价摘要。
    两家都配置时优先百度（字段更全），未命中回退高德。
    返回 {rating, count, recommend, source} 或 None（无数据/未配置/失败）。
    """
    if not is_enabled():
        return None
    key = _cache_key(hotel)
    cached = _cached_get(key)
    if cached is not None:
        return cached if cached.get("rating", 0) > 0 else None
    # 顺序即优先级：百度（三字段）→ 高德（仅评分）
    runners = []
    if baidu_enabled():
        runners.append(_rating_from_baidu)
    if amap_enabled():
        runners.append(_rating_from_amap)
    for runner in runners:
        try:
            rec = runner(hotel)
        except Exception as e:
            _notice(f"[评分服务] 获取失败（不影响订房）：{type(e).__name__}")
            rec = None
        if rec:
            _cache_put(key, rec)
            return rec
    # 所有源都查不到：负缓存，避免 key 暂时失效后长期不重试的反面——
    # 注意接口报错（异常）不在此分支，不会写负缓存
    _cache_put(key, {"rating": 0, "count": 0, "recommend": "", "source": ""})
    return None


# ========== 批量富化（搜索结果）==========

def enrich_hotel_rating(hotel: Hotel) -> bool:
    """把真实评分写入单个 Hotel，返回是否成功。已有评分则跳过。"""
    if hotel.rating_source or hotel.rating > 0:
        return False
    rec = fetch_rating(hotel)
    if not rec:
        return False
    hotel.rating = rec["rating"]
    hotel.review_count = rec["count"]
    hotel.recommend_rate = rec["recommend"]
    hotel.rating_source = rec["source"]
    return True


def enrich_hotels_rating(hotels: List[Hotel],
                         limit: int = _ENRICH_LIMIT) -> int:
    """
    并发为一批酒店补真实评分，返回成功条数。
    - 只处理真实来源（RLG_/AMAP_）且尚无评分的酒店，模拟库自带评分不覆盖
    - 默认只补前 limit 家，节省免费接口额度（缓存命中不计额度）
    - 高德 / 百度 Key 都未配置时直接返回 0，不产生任何请求
    """
    targets = [h for h in hotels
               if not h.rating_source and h.rating == 0
               and h.hotel_id.startswith(("RLG_", "AMAP_"))][:limit]
    if not targets:
        return 0
    if not is_enabled():
        _notice("[提示] 未配置评分服务 Key，真实住客评分暂不显示；"
                "在 .env 配置 BAIDU_MAP_AK（百度服务端 AK，字段最全）或 "
                "AMAP_API_KEY（高德 Web服务 Key，个人实名即可）任一个即可，"
                "申请方式见 hotel_rating.py 顶部说明")
        return 0
    ok = 0
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {pool.submit(enrich_hotel_rating, h): h for h in targets}
        for fut in as_completed(futures):
            try:
                ok += 1 if fut.result() else 0
            except Exception:
                continue
    return ok
