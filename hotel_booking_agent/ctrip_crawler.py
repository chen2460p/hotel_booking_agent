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
  （同程 ly.com 实测为 Nuxt SPA 壳 + 签名接口 + WAF 403，不作为数据源）
- 名称+城市匹配：搜索页 HTML 里有 data-offline-hotelId + hotelName 卡片，按编辑距离最近匹配
- 图集筛选：只取 dimg04.c-ctrip.com 的真实酒店图，剔除 webresource/pages 这类 UI 资源
- 房型图组：详情页 SSR 官方相册 JSON 里有「房型名 → 实拍图列表」（imgTitle 为完整房型名），
  按名称模糊匹配回填道旅缺失的房型图（match_room_url）
- 磁盘缓存（项目内 cache/ctrip_imgs.json，30 天）：图片变化慢，避免每次搜索都重爬
- 失败静默：网络/解析异常一律返回 []，绝不阻塞订房主流程
- 不替代道旅主图源，仅作回退：酒店封面在 RollingGo/高德均无图时启用，
  房型图在 roomInfo.images 缺失时启用

调用入口：
  from ctrip_crawler import fetch_all_images, match_room_url
  covers, rooms = fetch_all_images(hotel)   # (封面直链列表, {房型名: [直链]})
  url = match_room_url("高级大床房", rooms)  # 模糊匹配单个房型的首图
  # 兼容旧接口：
  from ctrip_crawler import enrich_hotel_images
  urls = enrich_hotel_images(hotel)          # 仅封面直链列表
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
            items = json.load(f).get("items", {})
        # v2 起记录含 rooms 字段；旧版记录（无 rooms）直接丢弃，保持结构统一
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
    return (rec.get("urls", []), rec.get("rooms", {}))


def _cache_put(key: str, urls: list, rooms: dict) -> None:
    with _cache_lock:
        _mem_cache[key] = {"urls": urls, "rooms": rooms, "ts": int(time.time())}
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
    # 每个房型最多保留 8 张（回填只用首图，多留几张备用）
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


def match_room_url(room_name: str, room_map: Dict[str, List[str]]) -> str:
    """道旅房型名 → 携程相册房型首图。

    匹配策略：归一后编辑距离 + 子串强匹配，修饰前缀逐级剥离重试。
    道旅客型名通常是通用短名（如"大床房"），携程是营销长名
    （如"一室宣光‖100寸巨幕‖全景大床房（语音智控+舒达床垫）"），
    短名 ⊂ 长名时视为强匹配。同分时取名称更短（更贴近通用名）的。
    阈值 0.45 宁缺毋滥：子串命中约 0.8+ 通过，无关名称 0.2~0.3 拒绝。
    """
    n = _norm(room_name)
    if not n or not room_map:
        return ""
    candidates = [_norm(x) for x in _candidate_room_names(room_name)]
    candidates = [x for x in candidates if x]
    if not candidates:
        return ""
    best_url, best_score, best_len = "", 0.0, 0
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
            best_url, best_score, best_len = urls[0], score, len(t)
    return best_url if best_score >= 0.45 else ""


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
      url = match_room_url("高级大床房", rooms)

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


def enrich_hotel_images(hotel: Hotel) -> List[str]:
    """兼容旧接口：仅返回封面直链列表（首张可用作封面图）。"""
    covers, _ = fetch_all_images(hotel)
    return covers


# ---------- CLI 自测入口 ----------
if __name__ == "__main__":
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
        covers, room_map = fetch_all_images(h)
        print(f"covers: got {len(covers)} images")
        for u in covers[:3]:
            print(f"  {u}")
        print(f"rooms: {len(room_map)} 个房型")
        for t, u in list(room_map.items())[:5]:
            print(f"  {t} -> {len(u)} 张, 首图 {u[0][:80]}")
        # 模拟道旅客型名，验证匹配
        for rg_name in ["大床房", "双床房", "高级大床房", "行政套房", "亲子房", "海景房"]:
            url = match_room_url(rg_name, room_map)
            print(f"  match[{rg_name}] -> {'✓ ' + url[:70] if url else '✗ 无匹配'}")
        # 再调一次应该走缓存
        t0 = time.time()
        covers2, rooms2 = fetch_all_images(h)
        print(f"缓存命中：耗时 {(time.time()-t0)*1000:.1f}ms, "
              f"同结果={covers2 == covers and rooms2 == room_map}")
