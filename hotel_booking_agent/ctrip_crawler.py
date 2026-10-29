# -*- coding: utf-8 -*-
"""
用途声明：本模块仅用于个人学习与教学演示，属于酒店预订教学 Agent 的
图片回退组件。模块抓取的数据（酒店图片、名称、地址等）版权归原始平台所有，
本项目不存储、不分发、不用于商业目的。请勿在生产环境、面向用户的服务或
任何商业场景下部署此模块。批量抓取、绕过反爬措施或商用转卖数据均可能
违反《反不正当竞争法》及相关法律。

====================

携程酒店图片/评分爬虫 —— 联想搜索接口拿 hotelId 与评分，详情页 SSR 拿图集。

设计要点：
- 零依赖（仅 requests + re），无 WAF 反爬风险：携程详情页与联想接口对脚本请求放行
  （同程 ly.com 实测为 Nuxt SPA 壳 + 签名接口 + WAF 403，不作为数据源）
- hotelId 定位：m.ctrip.com 联想搜索接口（action=online）返回 JSON，
  一次请求同时给出 hotelId、评分（commentScore）、点评数（commentCount）、
  评价标签（commentlevel）、星级、封面缩略图与坐标；按名称相似度 + 同城过滤匹配
  （旧方案解析搜索页 SSR 只渲染热门酒店，实测 4 家全部定位失败，已废弃）
- 图集筛选：只取 dimg04.c-ctrip.com 的真实酒店图，剔除 webresource/pages 这类 UI 资源
- 房型图组：详情页 SSR 官方相册 JSON 里有「房型名 → 实拍图列表」（imgTitle 为完整房型名），
  按名称模糊匹配补全道旅房型图组（match_room_urls，一个房型多张图）
- 磁盘缓存（项目内 cache/ctrip_imgs.json，30 天）：图片变化慢，避免每次搜索都重爬
- 失败静默：网络/解析异常一律返回 []，绝不阻塞订房主流程
- 不替代道旅主图源，仅作回退：酒店封面在 RollingGo/高德均无图时启用，
  房型图用于把道旅的单张 roomInfo.images 补齐成多图图组

调用入口：
  from ctrip_crawler import fetch_all_images, match_room_urls, fetch_rating
  covers, rooms = fetch_all_images(hotel)   # (封面直链列表, {房型名: [直链]})
  urls = match_room_urls("高级大床房", rooms)  # 模糊匹配整个房型图组
  rec = fetch_rating(hotel)                  # {"rating","count","level","hotel_id"}
"""
import os
import re
import json
import time
import threading
import difflib
from typing import Dict, List, Optional

import requests

from models import Hotel
from text_utils import normalize_name as _norm, name_similarity

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
                override=True)
except ImportError:
    pass

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_HDR = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://hotels.ctrip.com/hotels/list",
}
# 联想搜索（m 站）：返回 JSON，含 hotelId / 评分 / 点评数 / 星级 / 封面缩略图
_GLOBAL_SEARCH_URL = "https://m.ctrip.com/restapi/h5api/globalsearch/search"
_JSON_HDR = {
    "User-Agent": _UA,
    "Accept": "application/json",
    "Referer": "https://m.ctrip.com/hotels/",
}
_DETAIL_TPL = "https://hotels.ctrip.com/hotels/{hotel_id}.html"
_TIMEOUT = (5, 10)
_CACHE_TTL = 30 * 24 * 3600
# imgTitle 含"房"但实为设施的固定词，拉黑防混入房型图组
_ROOM_FACILITY_WORDS = {"健身房", "厨房", "书房"}
# 道旅客型名常见的修饰前缀（如"高级大床房"→"大床房"），匹配前逐级剥离
_MODIFIER_PREFIX = re.compile(
    r"^(?:高级|豪华|商务|标准|尊享|行政|奢华|精品|舒适|简约|特色|超值|超级)")
# 缓存放项目内 cache/ 目录（git 已排除）：好找好清理，随项目删除
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
_CACHE_PATH = os.path.join(_CACHE_DIR, "ctrip_imgs.json")
_mem_cache: dict = {}
_cache_lock = threading.Lock()
_disk_loaded = False


def _ensure_disk_loaded() -> None:
    """磁盘缓存进程内只懒加载一次（调用方须持 _cache_lock）。"""
    global _disk_loaded
    if _disk_loaded:
        return
    for k, v in _load_cache().items():
        _mem_cache.setdefault(k, v)
    _disk_loaded = True


def _load_cache() -> dict:
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as f:
            items = json.load(f).get("items", {})
        # 只保留图片记录；评分缓存统一由 hotel_rating 负责，避免双层 TTL
        return {k: v for k, v in items.items() if isinstance(v, dict)
                and "rooms" in v}
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


def _cached_get(key: str) -> Optional[tuple]:
    """命中返回 (封面列表, 房型图映射)；负缓存返回 ([], {})；无缓存返回 None。"""
    with _cache_lock:
        _ensure_disk_loaded()
        rec = _mem_cache.get(key)
    if not rec or "rooms" not in rec:
        return None
    if time.time() - rec.get("ts", 0) > _CACHE_TTL:
        return None
    return (rec.get("urls", []), rec.get("rooms", {}))


def _cache_put(key: str, urls: list, rooms: dict) -> None:
    with _cache_lock:
        _mem_cache[key] = {
            "urls": urls, "rooms": rooms, "ts": int(time.time()),
        }
        _save_cache(_mem_cache)


def _search_hotels(keyword: str) -> List[dict]:
    """携程 m 站联想搜索（JSON）：一次请求拿到 hotelId、评分、点评数、星级。

    返回条目示例：
      {"id":78106107,"word":"沈阳中街故宫玫瑰亚朵酒店","type":"hotel",
       "cityId":451,"cityName":"沈阳","commentScore":4.7,
       "commentCount":6451,"commentlevel":"超棒","starRating":"高档型",
       "imageUrl":"https://dimg11.c-ctrip.com/...","lat":...,"lon":...}
    失败返回 []，绝不抛异常。
    """
    if not keyword:
        return []
    try:
        r = requests.get(_GLOBAL_SEARCH_URL, params={
            "source": "mobileweb",
            "action": "online",
            "contentType": "json",
            "keyword": keyword,
        }, headers=_JSON_HDR, timeout=_TIMEOUT)
        if r.status_code != 200:
            return []
        items = r.json().get("data") or []
    except Exception:
        return []
    return [d for d in items
            if isinstance(d, dict) and d.get("type") == "hotel" and d.get("id")]


def _same_city(a: str, b: str) -> bool:
    """城市名比对：容忍"沈阳"/"沈阳市"这类后缀差异。"""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    na = na[:-1] if na.endswith("市") else na
    nb = nb[:-1] if nb.endswith("市") else nb
    return na == nb


def _pick_hotel(hotel_name: str, city: str,
                candidates: List[dict]) -> Optional[dict]:
    """同城过滤 + 名称匹配，取最可信的一家；不达标返回 None。

    匹配规则（宁缺毋滥，宁可无图/无评分也不给错）：
    - 有城市信息时硬过滤：候选里没有同城的一律拒绝，避免跨城误匹配
    - 强匹配：归一后互相包含且长度比 ≥ 0.6（"XX酒店" vs "XX酒店(中街店)"）
    - 弱匹配：编辑距离相似度 ≥ 0.85（"沈阳故宫玫瑰亚朵酒店" vs
      "沈阳中街故宫玫瑰亚朵酒店" = 0.91）
    """
    if not candidates:
        return None
    pool = [c for c in candidates if _same_city(c.get("cityName"), city)] \
        if city else candidates
    if not pool:
        return None
    nn = _norm(hotel_name)
    best, best_key = None, None
    for c in pool:
        cn = _norm(c.get("word") or "")
        if not nn or not cn:
            continue
        sim = name_similarity(hotel_name, c.get("word") or "")
        shorter, longer = sorted((len(nn), len(cn)))
        strong = (nn in cn or cn in nn) and shorter / longer >= 0.6
        if not strong and sim < 0.85:
            continue
        key = (2 if strong else 1, sim)
        if best_key is None or key > best_key:
            best, best_key = c, key
    return best


def _find_hotel_id(hotel_name: str, city: str) -> Optional[str]:
    """定位携程 hotelId：联想搜索 + 同城名称匹配。"""
    hit = _pick_hotel(hotel_name, city, _search_hotels(hotel_name))
    return str(hit["id"]) if hit else None


def fetch_rating(hotel: Hotel) -> Optional[dict]:
    """携程真实用户评分：{"rating","count","level","hotel_id"}；查不到返回 None。

    评分取自联想搜索接口的 commentScore / commentCount / commentlevel，
    与详情页 hotelComment.comment 的 score / totalComment 一致（点评数有
    几条缓存延迟，误差可忽略）。缓存统一由 hotel_rating 负责，本函数只查接口。
    """
    if not hotel or not hotel.name:
        return None
    hit = _pick_hotel(hotel.name, hotel.city or "", _search_hotels(hotel.name))
    if not hit:
        return None
    try:
        score = float(hit.get("commentScore") or 0)
    except (TypeError, ValueError):
        score = 0.0
    if score <= 0:
        return None
    try:
        count = int(hit.get("commentCount") or 0)
    except (TypeError, ValueError):
        count = 0
    return {
        "rating": score,
        "count": count,
        "level": str(hit.get("commentlevel") or ""),
        "hotel_id": str(hit.get("id")),
    }


def _parse_detail_images(html: str) -> List[str]:
    """从详情页 HTML 提取真实酒店图集。

    保留域名 dimg*.c-ctrip.com，剔除 webresource/pages 这类 UI 资源。
    图片 URL 后面常带 &quot; 等转义字符，需要清洗。
    """
    urls = re.findall(r'https://dimg\d+\.c-ctrip\.com/images/[^\s"\'<>]+', html)
    out, seen = [], set()
    for u in urls:
        # 去掉尾巴上的 &quot;... 等转义残留
        u = re.sub(r'&quot.*$', '', u)
        u = re.sub(r'[\'")]+$', '', u)
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _parse_room_images(html: str) -> Dict[str, List[str]]:
    """从详情页 SSR 官方相册 JSON 提取「房型名 → 实拍图列表」。

    相册条目形如：
      {"pictureId":..., "categoryId":9, "rank":..., "link":"//dimg04.c-ctrip.com/images/x.jpg",
       "imgTitle":"尊享行政LOFT套房（带阳台+深睡级床品+会客空间）", ...}
    房型条目的 imgTitle 是完整房型名（必含"房"字），公共区域/酒店外部/前台等
    是固定设施词（不含"房"）。规则：imgTitle 含"房"即视为房型图，按名称分组。
    相册 JSON 以转义形式嵌在 SSR 里，键名可能带 \" 前缀，正则需兼容两种形态。
    """
    rooms: Dict[str, List[str]] = {}
    for m in re.finditer(r"\{[^{}]*\}", html):
        obj = m.group(0)
        if "imgTitle" not in obj or "link" not in obj:
            continue
        tm = re.search(r'\\?"imgTitle\\?"\s*:\s*\\?"([^"\\]+)', obj)
        lm = re.search(r'\\?"link\\?"\s*:\s*\\?"([^"\\]+)', obj)
        if not tm or not lm:
            continue
        title = tm.group(1)
        if "房" not in title or title in _ROOM_FACILITY_WORDS:
            continue
        link = lm.group(1)
        if not re.match(r"//[\w-]+\.(?:c-ctrip\.com|tripcdn\.com)/images/", link):
            continue
        url = "https:" + link
        lst = rooms.setdefault(title, [])
        if url not in lst:
            lst.append(url)
    # 每个房型最多保留 8 张：相册原始条目可能更多，展示用图组截断到 8 张
    return {t: u[:8] for t, u in rooms.items()}


def _candidate_room_names(room_name: str) -> List[str]:
    """生成候选名：原名 + 逐级剥离修饰前缀（高级大床房→大床房）。

    道旅客型名常带通用修饰词，携程营销名一般不含（如"豪华大床房"在携程
    叫"智享好梦大床房"），剥离后才能命中子串强匹配。最多剥两级。
    """
    names = [room_name]
    cur = room_name
    for _ in range(2):
        stripped = _MODIFIER_PREFIX.sub("", cur, count=1)
        if stripped == cur or not stripped:
            break
        names.append(stripped)
        cur = stripped
    return names


def match_room_urls(room_name: str, room_map: Dict[str, List[str]],
                    limit: int = 8) -> List[str]:
    """道旅房型名 → 携程相册里该房型的整组实拍图。

    匹配策略：归一后编辑距离 + 子串强匹配，修饰前缀逐级剥离重试。
    道旅客型名通常是通用短名（如"大床房"），携程是营销长名
    （如"一室宣光‖100寸巨幕‖全景大床房（语音智控+舒达床垫）"），
    短名 ⊂ 长名时视为强匹配。同分时取名称更短（更贴近通用名）的。
    阈值 0.45 宁缺毋滥：子串命中约 0.8+ 通过，无关名称 0.2~0.3 拒绝。

    返回命中房型的全部图片（最多 limit 张），未匹配返回 []。
    """
    n = _norm(room_name)
    if not n or not room_map:
        return []
    candidates = [_norm(x) for x in _candidate_room_names(room_name)]
    candidates = [x for x in candidates if x]
    if not candidates:
        return []
    best_urls: List[str] = []
    best_score, best_len = 0.0, 0
    for title, urls in room_map.items():
        if not urls:
            continue
        t = _norm(title)
        if not t:
            continue
        score = 0.0
        for c in candidates:
            s = difflib.SequenceMatcher(None, c, t).ratio()
            if c in t:
                s += 0.5
            score = max(score, s)
        if score > best_score or (score == best_score and best_len and len(t) < best_len):
            best_urls, best_score, best_len = urls, score, len(t)
    return list(best_urls[:limit]) if best_score >= 0.45 else []


def _fetch_detail_data(hotel_id: str) -> tuple:
    """GET 详情页，一次提取 (封面图集, 房型图映射)。"""
    try:
        r = requests.get(_DETAIL_TPL.format(hotel_id=hotel_id),
                         headers=_HDR, timeout=_TIMEOUT)
    except Exception:
        return [], {}
    if r.status_code != 200 or len(r.text) < 10000:
        return [], {}
    return _parse_detail_images(r.text), _parse_room_images(r.text)


def fetch_all_images(hotel: Hotel) -> tuple:
    """对外主入口：给一家酒店，返回 (封面直链列表, {携程房型名: [直链]})。

    用法：
      covers, rooms = fetch_all_images(hotel)
      if covers and not hotel.image_url:
          hotel.image_url = covers[0]
      urls = match_room_urls("高级大床房", rooms)

    失败/无匹配一律返回 ([], {})，不抛异常。
    """
    if not hotel.name:
        return [], {}
    cache_key = f"{hotel.city or ''}|{hotel.name}"
    cached = _cached_get(cache_key)
    if cached is not None:
        return cached
    hotel_id = _find_hotel_id(hotel.name, hotel.city or "")
    if not hotel_id:
        _cache_put(cache_key, [], {})
        return [], {}
    covers, rooms = _fetch_detail_data(hotel_id)
    _cache_put(cache_key, covers, rooms)
    return covers, rooms


# ---------- CLI 自测入口 ----------
if __name__ == "__main__":
    test_cases = [
        # 连锁/单体（联想接口应能命中）
        ("如家商旅酒店(北京花乡桥郭公庄地铁站店)", "北京"),
        ("全季酒店(北京郎辛庄地铁站店)", "北京"),
        ("上海外滩茂悦大酒店", "上海"),
    ]
    for name, city in test_cases:
        h = Hotel(
            hotel_id="TEST", name=name, city=city, address="", star=3,
            price_per_night=300, facilities=[], room_types=[],
            rating=0, review_count=0,
        )
        print(f"\n=== 自测: {h.name} ({h.city}) ===")
        print(f"rating: {fetch_rating(h)}")
        covers, room_map = fetch_all_images(h)
        print(f"covers: got {len(covers)} images")
        for u in covers[:3]:
            print(f"  {u}")
        print(f"rooms: {len(room_map)} 个房型")
        for t, u in list(room_map.items())[:5]:
            print(f"  {t} -> {len(u)} 张, 首图 {u[0][:80]}")
        # 模拟道旅客型名，验证匹配
        for rg_name in ["大床房", "双床房", "高级大床房", "行政套房", "亲子房", "海景房"]:
            urls = match_room_urls(rg_name, room_map)
            print(f"  match[{rg_name}] -> {len(urls)} 张"
                  f"{'，首图 ' + urls[0][:70] if urls else '（无匹配）'}")
        # 再调一次应该走缓存
        t0 = time.time()
        covers2, rooms2 = fetch_all_images(h)
        print(f"缓存命中：耗时 {(time.time()-t0)*1000:.1f}ms, "
              f"同结果={covers2 == covers and rooms2 == room_map}")
