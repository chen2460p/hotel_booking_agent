# -*- coding: utf-8 -*-
"""
用途声明：本模块仅用于个人学习与教学演示，属于酒店预订教学 Agent 的
图片回退组件。模块抓取的数据（酒店图片、名称、地址等）版权归原始平台所有，
本项目不存储、不分发、不用于商业目的。请勿在生产环境、面向用户的服务或
任何商业场景下部署此模块。批量抓取、绕过反爬措施或商用转卖数据均可能
违反《反不正当竞争法》及相关法律。

====================

携程酒店图片爬虫 —— 搜索页 SSR 拿 hotelId，详情页 SSR 拿图集。

设计要点：
- 零依赖（仅 requests + re），无 WAF 反爬风险：携程详情页对脚本请求放行
- 名称+城市匹配：搜索页 HTML 里有 data-offline-hotelId + hotelName 卡片，按编辑距离最近匹配
- 图集筛选：只取 dimg04.c-ctrip.com 的真实酒店图，剔除 webresource/pages 这类 UI 资源
- 磁盘缓存（项目内 cache/ctrip_imgs.json，30 天）：图片变化慢，避免每次搜索都重爬
- 失败静默：网络/解析异常一律返回 []，绝不阻塞订房主流程
- 不替代道旅主图源，仅作回退：当 Hotel.image_url 为空且高德 photos 也无图时启用

调用入口：
  from ctrip_crawler import enrich_hotel_images
  urls = enrich_hotel_images(hotel)  # 返回直链列表，首张可用作封面图
"""
import os
import re
import json
import time
import threading
import difflib
from typing import List, Optional

import requests

from models import Hotel

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
_SEARCH_URL = "https://hotels.ctrip.com/hotels/list"
_DETAIL_TPL = "https://hotels.ctrip.com/hotels/{hotel_id}.html"
_TIMEOUT = (5, 10)
_CACHE_TTL = 30 * 24 * 3600
# 缓存放项目内 cache/ 目录（git 已排除）：好找好清理，随项目删除
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
_CACHE_PATH = os.path.join(_CACHE_DIR, "ctrip_imgs.json")
_mem_cache: dict = {}
_cache_lock = threading.Lock()
_disk_loaded = False

# 携程城市ID映射（搜索页 URL 必须带 cityId 才能拿到对应城市的酒店）
# 实测从 hotel.city 中文名直接搜也能命中，但带 cityId 更精准
_CITY_IDS = {
    "北京": 1, "上海": 2, "广州": 32, "深圳": 26, "成都": 28, "杭州": 14,
    "重庆": 27, "西安": 18, "南京": 11, "武汉": 9, "天津": 3, "苏州": 15,
    "长沙": 19, "青岛": 22, "厦门": 23, "大连": 4, "昆明": 24, "三亚": 20,
    "济南": 17, "郑州": 36, "合肥": 16, "福州": 21, "南昌": 12, "南宁": 25,
    "贵阳": 33, "兰州": 47, "海口": 30, "沈阳": 6, "哈尔滨": 8, "长春": 7,
    "石家庄": 5, "太原": 10, "呼和浩特": 13, "拉萨": 39, "银川": 41,
    "乌鲁木齐": 38, "西宁": 40, "银川": 41, "温州": 49, "宁波": 50,
}


def _load_cache() -> dict:
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("items", {})
    except Exception:
        return {}


def _save_cache(items: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        tmp = _CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "items": items}, f, ensure_ascii=False)
        os.replace(tmp, _CACHE_PATH)
    except Exception:
        pass


def _cached_get(key: str) -> Optional[list]:
    """命中返回 URL 列表；负缓存返回 []；无缓存返回 None。"""
    global _disk_loaded
    with _cache_lock:
        if not _disk_loaded:
            for k, v in _load_cache().items():
                _mem_cache.setdefault(k, v)
            _disk_loaded = True
        rec = _mem_cache.get(key)
    if not rec:
        return None
    if time.time() - rec.get("ts", 0) > _CACHE_TTL:
        return None
    return rec.get("urls", [])


def _cache_put(key: str, urls: list) -> None:
    with _cache_lock:
        _mem_cache[key] = {"urls": urls, "ts": int(time.time())}
        _save_cache(_mem_cache)


def _norm(name: str) -> str:
    """名称归一：去括号/标点/空格。"""
    return re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]", "", name or "")


def _name_similarity(a: str, b: str) -> float:
    """名称相似度：归一后取 SequenceMatcher 比率。"""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def _parse_search_hotels(html: str, city_name: str) -> List[dict]:
    """从搜索页 SSR HTML 解析酒店卡片列表。

    搜索页 SSR 把每家酒店渲染成卡片，包含：
      data-offline-hotelId="128128509" 和 <span class="hotelName">全季酒店(...)</span>
    两步法：先找所有 hotelId 位置，再向后在 1500 字符窗口内提取最近 hotelName。
    """
    out = []
    hid_pat = re.compile(r'data-offline-hotelId="(\d{4,12})"')
    name_pat = re.compile(r'class="hotelName">([^<]+)</span>')
    for m in hid_pat.finditer(html):
        hid = m.group(1)
        # 向后 1500 字符内找 hotelName
        window = html[m.end():m.end() + 1500]
        nm = name_pat.search(window)
        if nm:
            out.append({"hotel_id": hid, "name": nm.group(1).strip()})
    return out


def _find_hotel_id(hotel_name: str, city: str) -> Optional[str]:
    """从携程搜索页 SSR 找到最匹配的 hotelId。"""
    cid = _CITY_IDS.get(city or "", "")
    # 用城市名兜底（搜索结果会基于城市名匹配）
    params = {
        "checkIn": "2026-12-19",
        "checkOut": "2026-12-20",
        "city": cid or 1,
        "cityName": city or "北京",
    }
    try:
        r = requests.get(_SEARCH_URL, params=params, headers=_HDR, timeout=_TIMEOUT)
    except Exception:
        return None
    if r.status_code != 200 or len(r.text) < 10000:
        return None
    hotels = _parse_search_hotels(r.text, city or "")
    if not hotels:
        return None
    # 按 name 编辑距离匹配，取最相似的
    scored = [(h, _name_similarity(hotel_name, h["name"])) for h in hotels]
    scored.sort(key=lambda x: x[1], reverse=True)
    # 调试：打印 top3 候选（自测用，正式调用静默）
    if __name__ == "__main__":
        print(f"[ctrip_debug] search_got={len(hotels)} top3: {[(s[0]['name'], round(s[1],2)) for s in scored[:3]]}")
    # 阈值 0.85：宁缺毋滥。实测：
    #   自身完全匹配 = 1.0 ✓
    #   "全季酒店" vs "如家商旅酒店" = 0.58 ✗（避免误匹配给错图）
    #   "外滩茂悦" vs "外滩璞硯" = 0.71 ✗（不同酒店不误匹配）
    if scored[0][1] >= 0.85:
        return scored[0][0]["hotel_id"]
    return None


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


def _fetch_detail_images(hotel_id: str) -> List[str]:
    """GET 详情页，提取真实图集。"""
    try:
        r = requests.get(_DETAIL_TPL.format(hotel_id=hotel_id),
                         headers=_HDR, timeout=_TIMEOUT)
    except Exception:
        return []
    if r.status_code != 200 or len(r.text) < 10000:
        return []
    return _parse_detail_images(r.text)


def enrich_hotel_images(hotel: Hotel) -> List[str]:
    """对外入口：给一家酒店，返回其携程真实图片直链列表。

    用法：
      urls = enrich_hotel_images(hotel)
      if urls and not hotel.image_url:
          hotel.image_url = urls[0]

    失败/无匹配一律返回 []，不抛异常。
    """
    if not hotel.name:
        return []
    cache_key = f"{hotel.city or ''}|{hotel.name}"
    cached = _cached_get(cache_key)
    if cached is not None:
        return cached
    hotel_id = _find_hotel_id(hotel.name, hotel.city or "")
    if not hotel_id:
        _cache_put(cache_key, [])
        return []
    urls = _fetch_detail_images(hotel_id)
    _cache_put(cache_key, urls)
    return urls


# ---------- CLI 自测入口 ----------
if __name__ == "__main__":
    import sys
    test_cases = [
        # SSR 渲染的酒店（应能命中）
        ("如家商旅酒店(北京花乡桥郭公庄地铁站店)", "北京"),
        # 冷门酒店（SSR 不渲染，匹配不上）
        ("全季酒店(北京郎辛庄地铁站店)", "北京"),
        # 上海酒店
        ("上海外滩茂悦大酒店", "上海"),
    ]
    for name, city in test_cases:
        h = Hotel(
            hotel_id="TEST", name=name, city=city, address="", star=3,
            price_per_night=300, facilities=[], room_types=[],
            rating=0, review_count=0,
        )
        print(f"\n=== 自测: {h.name} ({h.city}) ===")
        urls = enrich_hotel_images(h)
        print(f"got {len(urls)} images")
        for u in urls[:3]:
            print(f"  {u}")
        # 再调一次应该走缓存
        t0 = time.time()
        urls2 = enrich_hotel_images(h)
        print(f"缓存命中：耗时 {(time.time()-t0)*1000:.1f}ms, 同结果={urls2==urls}")
