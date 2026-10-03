# 高德地图 POI API 适配器
#
# 用途：用高德"文本搜索"接口获取国内城市的真实酒店 POI 列表，
#       解决模拟数据库 HOTEL_DB 只有 4 个城市的问题。
#
# 能力边界（重要）：
# - 高德 POI 只提供：名称、类型、地址、坐标、行政区划
# - 不提供：实时房价、空房、评分、设施、可下单房型
#   因此价格/评分/设施类筛选在真实模式下不生效（仅城市 + 关键词由服务端过滤）
#
# 申请方式（必须申请"Web服务"类型的 Key，JS API 的 Key 不能用于服务端调用）：
# 1. 注册并登录 https://lbs.amap.com/
# 2. 控制台 → 应用管理 → 创建新应用 → 添加 Key，服务平台选择【Web服务】
# 3. 把 Key 填入同目录 .env：AMAP_API_KEY=你的key
#
# 接口文档：https://lbs.amap.com/api/webservice/guide/api/search

import os
from typing import List

import requests

from models import BookingParams, Hotel

# 从 .env 文件加载环境变量（与 llm.py / rag.py 保持一致，兼容任意导入顺序与工作目录）
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
                override=True)
except ImportError:
    pass

# 文本搜索 v3 接口（REST，服务端 Key 直接调用，无需签名）
_AMAP_TEXT_SEARCH_URL = "https://restapi.amap.com/v3/place/text"

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


def _to_hotel(poi: dict, city: str) -> Hotel:
    """把高德 POI 映射成项目统一的 Hotel 模型"""
    type_text = poi.get("type", "")
    return Hotel(
        hotel_id=f"AMAP_{poi.get('id', '')}",
        name=poi.get("name", "未知酒店"),
        city=city,
        address=_build_address(poi),
        star=_parse_star(type_text),
        price_per_night=0.0,   # POI 无价格，0 表示未知（展示层特殊处理）
        facilities=[],         # POI 无设施明细
        room_types=list(_DEFAULT_ROOM_TYPES),
        rating=0.0,            # POI 无评分，0 表示未知
        review_count=0,
    )


def search_real_hotels(params: BookingParams,
                       page_size: int = 20, page: int = 1) -> List[Hotel]:
    """
    调用高德文本搜索查询真实酒店
    - 城市：city 参数直接传中文名（如"成都"），citylimit=true 限定本市
    - 关键词：海景/亲子等关键词拼入 keywords 由高德做名称匹配
    """
    keywords = "酒店"
    if params.keyword:
        keywords = f"{params.keyword}酒店"

    response = requests.get(
        _AMAP_TEXT_SEARCH_URL,
        params={
            "key": get_api_key(),
            "keywords": keywords,
            "types": _ACCOMMODATION_TYPE,
            "city": params.city,
            "citylimit": "true",
            "offset": page_size,
            "page": page,
            "extensions": "base",
            "output": "JSON",
        },
        timeout=8,
    )
    response.raise_for_status()
    data = response.json()

    # 高德固定用 status=="1" 表示成功；失败时返回 infocode/info 便于排查
    if data.get("status") != "1":
        raise RuntimeError(
            f"高德API错误 infocode={data.get('infocode')} info={data.get('info')}"
        )

    pois = data.get("pois") or []
    return [_to_hotel(poi, params.city)
            for poi in pois if poi.get("id") and poi.get("name")]
