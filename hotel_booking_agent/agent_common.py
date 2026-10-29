# -*- coding: utf-8 -*-
"""单 Agent 版（agent.py）与多 Agent 版（multi_agent.py）共用的纯逻辑。

这两套架构曾各自复制了一份参数合并、姓名校验、订单筛选、房型匹配与
展示逻辑，已经出现行为漂移（例如多 Agent 版选酒店用裸数字正则，
会把"10月20日"里的数字当成序号）。这里集中为无状态的辅助函数，
两边只保留各自的编排/分发代码。
"""

import re
from datetime import datetime
from typing import List, Optional

from models import BookingParams, Hotel, RoomRatePlan
from llm import shift_date
from geo_data import (
    find_city, find_similar_city, get_city_coord, infer_city_from_location,
)
from text_rules import (
    NAME_STOPWORDS, NAME_DENY_WORDS, NAME_PATTERNS as _NAME_PATTERNS,
    ORDER_FILTER_NAMES, ORDER_LIST_WORDS,
    ORDER_STATUS_TEXT, match_order_status, extract_order_id,
)
from tools import calculate_total_price, get_hotel_detail, format_hotel_list


# ========== 预订参数 ==========

# 澄清阶段用户补充这些字段时，即使被 LLM 判成闲聊也继续走预订流程
CLARIFY_INTENT_KEYS = (
    "city", "check_in", "check_out", "_nights", "min_star",
    "max_price", "facilities", "location",
)

# 这些字段出现，说明用户本轮确实在补充预订信息（而非闲聊）
BOOKING_PARAM_KEYS = (
    "city", "check_in", "check_out", "min_star",
    "max_price", "facilities", "keyword", "guest_name",
)

# 位置描述里常被 LLM 连带提取进来的口语前缀
_LOCATION_LEAD_WORDS = (
    "帮我查询", "帮忙查询", "请帮我查询", "我查询", "查询",
    "帮我查", "帮忙查", "我查", "查一下", "查查",
    "搜索一下", "搜索", "搜一下", "搜", "找一下", "找一找", "找",
    "我想去", "我要去", "想去", "我要", "我想", "看看", "在",
)
# 地名实体后缀：用于判断“南宁是第二中学”里的“是”是不是“市”的笔误
_LOCATION_ENTITY_WORDS = (
    "中学", "小学", "高中", "初中", "大学", "学院", "学校", "幼儿园",
    "医院", "机场", "火车站", "高铁站", "地铁站", "车站", "广场", "公园",
    "景区", "景点", "酒店", "饭店", "大厦", "政府", "中心", "步行街",
    "街", "路", "湾", "站",
)


def normalize_location(location: str, city: str = "") -> str:
    """清理地点里连带提取的“我查询/帮我查”等口语前缀。"""
    loc = (location or "").strip(" 　")
    changed = True
    while changed and loc:
        changed = False
        for word in _LOCATION_LEAD_WORDS:
            if loc.startswith(word) and len(loc) - len(word) >= 2:
                loc = loc[len(word):].lstrip(" 　的")
                changed = True
                break
    return loc


def suggest_location_fix(location: str, city: str) -> str:
    """
    高置信度地名笔误纠正：城市名后紧跟“是/事”且后面是地标实体时，
    认为用户想写“市”，返回纠正后的完整地名；否则返回空串。
    例：南宁是第二中学附近 → 南宁市第二中学附近
    """
    loc = normalize_location(location, city)
    core = (city or "").rstrip("市")
    if not loc or not core:
        return ""
    m = re.match(rf"^{re.escape(core)}(?:市)?\s*([是事])\s*(.+)$", loc)
    if not m:
        return ""
    rest = m.group(2).strip()
    if len(rest) < 2 or not any(w in rest for w in _LOCATION_ENTITY_WORDS):
        return ""
    return f"{core}市{rest}"


def location_fix_prompt(fix: dict) -> str:
    """地名纠错确认话术。"""
    suggested = str(fix.get("suggested") or "").strip()
    display = re.sub(r"(附近|周边|旁边|一带)$", "", suggested)
    suffix = "附近" if display != suggested else ""
    return (
        f"我猜你想说的是「{display}」{suffix}，对吗？\n"
        "回复「对」我就按这个搜索；如果不对，请直接告诉我正确的地名。"
    )


def location_fix_reply(text: str) -> str:
    """判断用户对纠错建议的回答：accept / reject / 空串（未明确回答）。"""
    t = (text or "").strip()
    if not t:
        return ""
    if any(w in t for w in ("不对", "不是", "错了", "错", "重新", "改一下", "改成", "换一个")):
        return "reject"
    if t in ("对", "是", "是的", "对的", "没错", "正确", "可以", "好", "嗯",
             "按这个", "就这个"):
        return "accept"
    if t.startswith(("对", "是的", "没错", "正确", "按这个", "就这个")):
        return "accept"
    return ""


def location_fix_action(state, text: str):
    """
    地名纠错确认的统一入口（单/多 Agent 共用）。

    返回 (action, reply)：
      ("reply", 文本)   需要直接回复用户（确认/拒绝/重复确认）
      ("accepted", "")  已采用纠正后的地名，调用方继续预订/搜索流程
      ("none", "")      无需处理，继续原流程
    """
    params = state.params
    fix = getattr(state, "pending_location_fix", None) or {}
    if fix:
        if any(w in text for w in QUIT_BOOKING_WORDS):
            reset_booking_state(state)
            return "reply", quit_booking_message()
        current = normalize_location(params.location, params.city)
        # 用户没回答“对/不对”，而是重新给了一个地点：以新地点为准
        if current and current != fix.get("original"):
            state.pending_location_fix = {}
        else:
            answer = location_fix_reply(text)
            if answer == "accept":
                if fix.get("city"):
                    params.city = fix["city"]
                params.location = fix.get("suggested") or params.location
                state.pending_location_fix = {}
                return "accepted", ""
            if answer == "reject":
                state.pending_location_fix = {}
                return ("reply", "好的，请重新告诉我正确的地名"
                                 "（例如：南宁市第二中学附近）。")
            return "reply", location_fix_prompt(fix)

    suggestion = suggest_location_fix(params.location, params.city)
    if suggestion:
        state.pending_location_fix = {
            "original": normalize_location(params.location, params.city),
            "suggested": suggestion,
        }
        return "reply", location_fix_prompt(state.pending_location_fix)
    return "none", ""


_CITY_TOKEN_RE = re.compile(r"^([\u4e00-\u9fa5]{2,4})市")


def city_fix_action(state, text: str):
    """
    显式城市名纠错：输入里出现「大年市」这种不是已知城市的 X市 时，
    先猜测相近城市并让用户确认；识别不出则要求用户重新确认城市。
    """
    norm = normalize_location(text)
    m = _CITY_TOKEN_RE.match(norm)
    if not m:
        return "none", ""
    core = m.group(1)
    old_city = (state.params.city or "").rstrip("市")
    if get_city_coord(core):
        # 显式写了合法城市：优先于 LLM 从“西安路”里误抓的“西安”
        loc = normalize_location(state.params.location, state.params.city)
        if old_city and old_city != core and loc.startswith(old_city + "市"):
            loc = f"{core}市{loc[len(old_city) + 1:].lstrip(' 　的')}"
            state.params.location = loc
        elif loc.startswith("市"):
            state.params.location = f"{core}{loc}"
        state.params.city = core
        return "none", ""

    suggestion = find_similar_city(core)
    original_loc = normalize_location(state.params.location, state.params.city)
    loc = original_loc
    for prefix in (core + "市", core, old_city + "市"):
        if prefix and loc.startswith(prefix):
            loc = loc[len(prefix):].lstrip(" 　的")
            break
    if not suggestion:
        return ("reply", f"「{core}市」我没查到，请确认城市名后再搜索"
                         "（例如：大连市西安路附近）。")
    suggested = f"{suggestion}市{loc}" if loc else f"{suggestion}市"
    state.pending_location_fix = {
        "original": original_loc,
        "suggested": suggested,
        "city": suggestion,
    }
    return "reply", location_fix_prompt(state.pending_location_fix)


def merge_params(state, extracted: dict, user_input: str = "") -> None:
    """把本轮提取到的参数合并进 state.params（支持多轮累积补充）。"""
    p = state.params
    # 澄清阶段追问"哪天离店/住几晚"时，用户常只回一个裸数字（"5""住5晚"）。
    # 无状态提取器看不出这是晚数，会判成闲聊被踢回帮助菜单；
    # 这里结合上下文（已有入住日期、缺离店日期）把裸数字按晚数理解。
    # 仅在 clarify 阶段生效，避免选房/选酒店阶段的序号数字被误当成晚数
    if (getattr(state, "stage", "") == "clarify" and p.check_in
            and not p.check_out and not extracted.get("_nights")
            and "check_out" not in extracted):
        m = re.fullmatch(r'\s*(?:住\s*)?(\d{1,2})\s*(?:晚|天|夜)?\s*', user_input)
        if m and 1 <= int(m.group(1)) <= 30:
            # 必须原地写入：agent/multi_agent 在 merge 后还要检查 extracted
            # 里有没有预订参数来决定是否把 chat 意图纠正回预订流程
            extracted["_nights"] = int(m.group(1))
    if "city" in extracted:
        p.city = extracted["city"]
    if "check_in" in extracted:
        new_ci = extracted["check_in"]
        # 已有入住日期、正等离店日期时，用户只回一个日期（"27号""后天"）
        # 应落到 check_out；否则会覆盖入住日期，导致反复追问离店日期
        wants_check_in = any(w in user_input for w in ("入住", "改到", "提前", "推迟"))
        if (p.check_in and not p.check_out and new_ci > p.check_in
                and "check_out" not in extracted and not wants_check_in):
            p.check_out = new_ci
        else:
            p.check_in = new_ci
    if "check_out" in extracted:
        p.check_out = extracted["check_out"]
    # 用户说"住三天/住3晚"时，用已有的入住日期推算离店日期；
    # 已有离店日期也允许覆盖——搜索后用户再说"我想住3天"就是在改晚数
    if extracted.get("_nights") and p.check_in:
        p.check_out = shift_date(p.check_in, int(extracted["_nights"]))
    if "min_star" in extracted:
        p.min_star = extracted["min_star"]
    if "max_price" in extracted:
        p.max_price = extracted["max_price"]
    if "facilities" in extracted:
        existing = set(p.facilities)
        existing.update(extracted["facilities"])
        p.facilities = list(existing)
    if "keyword" in extracted:
        p.keyword = extracted["keyword"]
    if "location" in extracted:
        p.location = extracted["location"]
    # 清理 LLM 误带进 location 的口语前缀（“我查询南宁…”）
    p.location = normalize_location(p.location, p.city)
    # LLM 剥离城市前缀时可能留下孤立的“市”（“大连市西安路”→“市西安路”），
    # 补回城市名，保证距离展示和 POI 解析拿到完整地标
    if p.city and p.location.startswith("市"):
        p.location = f"{p.city.rstrip('市')}{p.location}"
    # LLM/规则有时只给了“来宾高级中学附近”却没给城市：从位置或原句补城市，
    # 避免明明说了地点还反复追问“哪个城市”
    if not p.city:
        p.city = (infer_city_from_location(p.location or "")
                  or find_city(user_input))


def calc_nights(check_in: str, check_out: str) -> int:
    """计算入住晚数；日期缺失/非法时返回 0。"""
    try:
        d1 = datetime.strptime(check_in, "%Y-%m-%d")
        d2 = datetime.strptime(check_out, "%Y-%m-%d")
        return (d2 - d1).days
    except (ValueError, TypeError):
        return 0


def generate_clarify_question(missing: List[str], params=None) -> str:
    """生成参数追问：优先用已识别的地点/城市，避免固定列清单式的生硬模板。"""
    city = getattr(params, "city", "") or ""
    location = getattr(params, "location", "") or ""
    if city and location:
        place = location if location.startswith(city.rstrip("市")) else f"{city}{location}"
    else:
        place = city or location
    if len(missing) == 1:
        if missing[0] == "城市":
            return "请问你想去哪个城市？"
        if missing[0] == "入住日期":
            return (f"好的，按{place}来搜。计划哪天入住？"
                    if place else "计划哪天入住？（如：下周末、2026-10-15）")
        if missing[0] == "离店日期":
            if getattr(params, "check_in", ""):
                return f"入住 {params.check_in} 收到，计划住几晚/哪天离店？"
            return "计划哪天离店？或住几晚？"
        return f"请问{missing[0]}是什么？"
    if set(missing) == {"入住日期", "离店日期"}:
        if place:
            return (f"好的，按{place}来搜。还需要入住和离店日期——"
                    "告诉我哪天到、住几晚就行。")
        return "好的，还需要入住和离店日期——告诉我哪天到、住几晚就行。"
    if "城市" in missing:
        rest = [m for m in missing if m != "城市"]
        if place:
            return (f"地点我先记下了（{place}），还需要确认城市；"
                    f"{'、'.join(rest)}也一起告诉我吧。")
        return f"好的，先确认一下：{'、'.join(missing)}？告诉我之后我直接搜。"
    if place:
        return (f"好的，按{place}来搜；还差{'、'.join(missing)}，"
                "一起告诉我就行。")
    return f"好的，先确认一下：{'、'.join(missing)}？告诉我之后我直接搜。"


def reset_booking_state(state) -> None:
    """重置预订状态，准备下一次预订。"""
    state.params = BookingParams()
    state.search_results = []
    state.selected_hotel = None
    state.current_order = None
    state.booking_context = {}
    state.pending_booking = {}
    state.pending_location_fix = {}
    state.stage = "intent"


# ========== 订单查询 ==========


def detect_order_filter(extracted: dict, raw_text: str) -> str:
    """从 LLM 提取结果或原文识别订单状态筛选，识别不出默认 ALL。"""
    flt = str(extracted.get("order_status") or "").upper()
    if flt in ("ALL", "PENDING", "FINISHED", "CANCELLED"):
        return flt
    return match_order_status(raw_text or "") or "ALL"


def wants_order_list(extracted: dict, raw_text: str) -> bool:
    """历史清单词或具体状态筛选 → 清单；否则视为查当前那笔订单。"""
    text = raw_text or ""
    if any(w in text for w in ORDER_LIST_WORDS):
        return True
    return detect_order_filter(extracted, raw_text) != "ALL"


def format_order_detail(order, status_text: Optional[str] = None) -> str:
    """渲染单笔订单详情（status_text 可由调用方用远端最新状态覆盖）。"""
    if not status_text:
        status_text = ORDER_STATUS_TEXT.get(order.status, order.status)
    response = (
        f"{'【道旅真实】' if order.source == 'rollinggo' else ''}订单信息：\n"
        f"订单号：{order.order_id}\n"
        f"酒店：{order.hotel_name}\n"
        f"入住：{order.check_in} → {order.check_out}\n"
        f"房型：{order.room_type}\n"
        f"入住人：{order.guest_name}\n"
        f"总价：{order.total_price:g}元\n"
        f"状态：{status_text}"
    )
    if order.payment_url and order.status == "pending":
        response += (
            f"\n\n该订单尚未支付，请尽快通过下面的链接完成支付（未支付不会扣款，"
            f"超时未支付订单将自动取消）：\n{order.payment_url}"
        )
    return response


# ========== 入住人姓名 ==========


def valid_person_name(name: str) -> bool:
    """姓名候选校验：2-4 个汉字且不含业务/时间类词语。"""
    if not name or not re.fullmatch(r'[一-龥]{2,4}', name):
        return False
    if name in NAME_STOPWORDS:
        return False
    return not any(w in name for w in NAME_DENY_WORDS)


def extract_guest_name(user_input: str,
                       waiting_for_name: bool = False) -> Optional[str]:
    """
    从消息提取入住人姓名：
    1) "入住人张三 / 姓名：张三 / 我叫张三 / 张三入住" 等显式说法
    2) waiting_for_name=True 时，接受裸姓名（如只回复"陈老二"）
    注意："入住"前缀必须带"人"字，否则"入住时间改到…"会被误抓成姓名。
    """
    text = user_input.strip()
    for pat in _NAME_PATTERNS:
        m = re.search(pat, text)
        if m and valid_person_name(m.group(1)):
            return m.group(1)
    if waiting_for_name:
        t = text.strip("。.!！?？,， ")
        if valid_person_name(t):
            return t
        if re.fullmatch(r"[A-Za-z][A-Za-z .'\-]{1,30}", t):
            return t
    return None


# ========== 选酒店 ==========

# 选择句模式：动词(订/选/要/来/住) + 序数（阿拉伯/中文数字），
# 避免裸数字把"10月20日"这类日期误当序号；
# 数字后禁止跟 天/晚/夜——"我想住3天""订两晚"说的是入住晚数，
# 不是"第3家/第2家"，否则会被误选成序号酒店
_SELECT_MARK = re.compile(
    r'(?:订|选|要|来|住)\s*第?\s*([一二三四五六七八九十]|\d{1,2})'
    r'(?!\s*(?:天|晚|夜))\s*[家号个]?')
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def parse_hotel_selection(user_input: str, results: List[Hotel],
                          allow_bare_number: bool = False) -> Optional[Hotel]:
    """
    解析用户的酒店选择（回复序号或酒店名）。

    allow_bare_number：是否接受纯序号（如"2"）。仅在选房确认场景
    （用户正对着列表回复）开启；搜索拦截场景必须关闭，否则
    "换上海10月20日"里的"10"会被当成第10家。
    """
    if not results:
        return None

    m = _SELECT_MARK.search(user_input)
    if m:
        raw = m.group(1)
        idx = (int(raw) if raw.isdigit() else _CN_NUM.get(raw, 0)) - 1
        if 0 <= idx < len(results):
            return results[idx]
        return None

    if allow_bare_number:
        t = user_input.strip()
        # 接受 "3"、"第3"、"3号"、"3家"：面对编号列表时这些都是序号表达
        m = re.fullmatch(r'第?\s*(\d{1,3})\s*[家号个]?', t)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(results):
                return results[idx]
            return None

    for hotel in results:
        if hotel.name in user_input or hotel.hotel_id in user_input:
            return hotel
    return None


def is_selection_attempt(user_input: str) -> bool:
    """判断输入是否"像在选序号"（用于超范围时给出序号提示）。

    必须排除 "我想住3天""改到25号"这类携带数字但与本意无关的说法，
    否则推荐阶段任何带数字的消息都会收到"请输入 1-N 之间的序号"。
    """
    t = user_input.strip()
    if re.fullmatch(r'第?\s*(?:\d{1,3}|[一二三四五六七八九十])\s*[家号个]?', t):
        return True
    return bool(_SELECT_MARK.search(t))


# ========== 选房型 ==========

# 模拟库房型的床型关键词兜底（"大床"→"大床房"）
BED_KEYWORDS = (
    ("特大床", "特大床"), ("大床", "大床"),
    ("双床", "双床"), ("单人床", "单人床"),
    ("标间", "标准"), ("标房", "标准"),
    ("家庭", "家庭"), ("套房", "套房"),
)
# 实时报价的床型别名（用户常只说"大床房/双床房"）
BED_ALIASES = {
    "特大床": ("特大床",), "大床": ("大床",),
    "双床": ("双床", "单人床"), "单人床": ("单人床",),
    "家庭": ("家庭",), "套房": ("套房",),
}
_ROOM_INDEX_RE = re.compile(
    r'(?:^|[第#号\s])(\d+)(?!\s*(?:天|晚|夜))\s*[号#\.、，,]?')
# “1-2” / “第 1 个房型第 2 档”：房型序号-报价序号
_PLAN_INDEX_RE = re.compile(
    r'(?<![\d年月/.\-])(?:第\s*)?(\d{1,2})\s*[-－—]\s*(\d{1,2})\s*(?:档|个|号)?')
_PLAN_INDEX_CN_RE = re.compile(
    r'第?\s*(\d{1,2})\s*(?:个|号|种)?\s*房型\s*(?:的)?\s*'
    r'第?\s*(\d{1,2})\s*(?:档|个|种|号)?')
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"
_CANCEL_DEADLINE_RE = re.compile(r'(\d{4})-(\d{2})-(\d{2})\s+(\d{2}:\d{2})')


def plan_groups(plans: List[RoomRatePlan]):
    """按 room_no 分房型返回 [(房型序号, [报价...])]，兼容无 room_no 的旧数据。"""
    if not plans:
        return []
    if not any(getattr(rp, "room_no", 0) for rp in plans):
        return [(i, [rp]) for i, rp in enumerate(plans, 1)]
    groups = {}
    for rp in plans:
        groups.setdefault(rp.room_no or 0, []).append(rp)
    return [(no, sorted(groups[no],
                        key=lambda x: (x.plan_no or 0, x.price_per_night)))
            for no in sorted(groups)]


def plan_group_images(plans: List[RoomRatePlan]) -> List[str]:
    """房型图组：同一房型多档报价的图合并，展示时只出现一次。"""
    out: List[str] = []
    for rp in plans:
        for url in rp.image_urls or []:
            if url not in out:
                out.append(url)
    return out[:8]


def _meal_text(rp: RoomRatePlan) -> str:
    return rp.meal or rp.meal_category or "餐食未知"


def _cancel_text(rp: RoomRatePlan) -> str:
    if not getattr(rp, "cancel_known", True):
        return "退改未知"
    if not rp.cancelable:
        return "不可取消"
    m = _CANCEL_DEADLINE_RE.search(rp.cancel_policy or "")
    if m:
        return f"可免费取消，截止 {m.group(2)}-{m.group(3)} {m.group(4)}"
    return "可免费取消"


def _size_text(size: str) -> str:
    """面积展示：32.0 → 32㎡；区间（如 30-35）原样保留。"""
    s = (size or "").strip()
    if not s:
        return ""
    if "㎡" in s or "m²" in s:
        return s
    try:
        return f"{float(s):g}㎡"
    except ValueError:
        return f"{s}㎡"


def _room_header_bits(rp: RoomRatePlan) -> List[str]:
    bits: List[str] = []
    if rp.bed_type and rp.bed_type not in rp.room_name:
        bits.append(rp.bed_type)
    if rp.room_size:
        bits.append(_size_text(rp.room_size))
    return bits


def _pick_plan_in_group(plans: List[RoomRatePlan], text: str) -> RoomRatePlan:
    """在房型内按“含早/可取消/申请确认”等关键词选最低价的一档。"""
    meal = None
    if "早晚餐" in text:
        meal = ("含早晚餐",)
    elif ("不要早餐" in text or "无需早餐" in text
          or "不含早" in text or "无早" in text):
        meal = ("无早",)
    elif "含早" in text or "双早" in text or "早餐" in text:
        meal = ("含早", "含早晚餐")
    cancel = None
    if "不可取消" in text or "不能取消" in text:
        cancel = False
    elif "可取消" in text or "免费取消" in text or "能取消" in text:
        cancel = True
    need_request = "申请确认" in text or "需确认" in text
    cands = list(plans)
    if meal:
        cands = [p for p in cands if p.meal_category in meal]
    if cancel is not None:
        cands = [p for p in cands
                 if p.cancel_known and p.cancelable is cancel]
    if need_request:
        cands = [p for p in cands if p.on_request]
    return min(cands, key=lambda p: p.price_per_night) if cands else plans[0]


def match_rate_plan(hotel: Hotel, user_input: str):
    """从用户输入匹配实时房型报价，未匹配返回 None。

    支持：
      - 1-2 / 第 1 个房型第 2 档：房型序号 + 房型内报价序号
      - 1 / 2：房型序号，默认该房型最低价一档
      - 1 含早 / 1 可取消 / 1 申请确认：房型内按条件筛最低价
      - 房型名、床型关键词：默认最低价一档
    """
    groups = dict(plan_groups(hotel.rate_plans))
    if not groups:
        return None

    m = None
    for pattern in (_PLAN_INDEX_RE, _PLAN_INDEX_CN_RE):
        m = pattern.search(user_input)
        if m:
            break
    if m:
        room_no, plan_no = int(m.group(1)), int(m.group(2))
        plans = groups.get(room_no)
        if plans and 1 <= plan_no <= len(plans):
            return plans[plan_no - 1]

    # 以房型名开头时优先按房型名：像“2张单人床”这种房型名本身以数字
    # 开头，先按裸序号解析会被误当成第 2 个房型
    text = user_input.strip()
    for plans in groups.values():
        name = plans[0].room_name
        if name and text.startswith(name):
            return _pick_plan_in_group(plans, user_input)

    m = _ROOM_INDEX_RE.search(user_input)
    if m:
        plans = groups.get(int(m.group(1)))
        if plans:
            return _pick_plan_in_group(plans, user_input)

    # 房型名出现在句中（如“我想选标准大床房”）
    for plans in groups.values():
        name = plans[0].room_name
        if name and name in user_input:
            return _pick_plan_in_group(plans, user_input)

    for alias, keys in BED_ALIASES.items():
        if alias in user_input:
            for plans in groups.values():
                target = plans[0].room_name + " " + plans[0].bed_type
                if any(k in target for k in keys):
                    return _pick_plan_in_group(plans, user_input)
    return None


def match_mock_room_type(hotel: Hotel, user_input: str) -> Optional[str]:
    """匹配模拟库房型：先房型名完整包含，再按床型关键词兜底。"""
    room_types = hotel.room_types or []
    for rt in room_types:                       # 完整房型名优先
        if rt in user_input:
            return rt
    for keyword, room_key in BED_KEYWORDS:
        if keyword in user_input:
            for rt in room_types:
                if room_key in rt:
                    return rt
    return None


def match_room_choice(hotel: Hotel, user_input: str):
    """
    统一房型解析，返回 (room_type, rate_plan)：
    - 有道旅实时报价 → 按报价匹配（序号/房型名/床型）
    - 模拟库 → 支持序号、房型名、床型关键词
    未匹配返回 (None, None)。
    """
    if hotel.rate_plans:
        rp = match_rate_plan(hotel, user_input)
        if rp:
            return rp.room_name, rp
        # 未命中报价时再按模拟房型名兜底（room_types 与报价名一致）
        return match_mock_room_type(hotel, user_input), None

    m = re.match(r'\s*第?\s*(\d+)(?!\s*(?:天|晚|夜))\s*[号#\.、，,]?', user_input)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(hotel.room_types or []):
            return hotel.room_types[idx], None
    return match_mock_room_type(hotel, user_input), None


# ========== 选房阶段的逃生口 ==========

# 选房阶段用户可能想"跳出"当前流程：明确的重查指令
RESEARCH_WORDS = (
    "重新查询", "重新搜索", "重新搜", "重新查", "再搜", "再查", "重搜",
    "重查", "换一家", "换个酒店", "换酒店", "重新选", "重选", "重新开始",
)
# 彻底放弃本次预订
QUIT_BOOKING_WORDS = ("不订了", "取消预订", "算了不订", "不买了")
# 选房阶段想回到上一级酒店列表：只回列表，不重新搜索
BACK_TO_HOTEL_WORDS = (
    "返回上一级", "返回上一页", "上一级", "上一页",
    "返回酒店列表", "回到酒店列表", "返回酒店", "返回列表",
    "重新选酒店", "重选酒店", "返回重新选", "返回",
)
_QUIT_MESSAGE = ("好的，本次预订已取消。下次想订酒店时，直接告诉我城市和入住"
                 "日期就可以～")


def looks_like_new_search(text: str) -> bool:
    """
    识别"一句话发起新搜索"：提到酒店/宾馆且带位置、价格、日期、晚数等
    搜索信号（如"帮我查下周末大连站附近的酒店300内住两晚"）。
    """
    if not re.search(r"酒店|宾馆|住宿|旅店", text):
        return False
    return bool(re.search(
        r"附近|住.{0,4}晚|周末|周[一二三四五六日天]|"
        r"\d{4}\s*[-/.]\s*\d{1,2}|\d{1,2}月\d{1,2}日",
        text))


def booking_exit_action(state, text: str) -> Optional[str]:
    """
    选房/入住人收集阶段的逃生口，命中时已更新 state。

    返回：
      "new_search" 新搜索条件句 → 调用方交回主流程重新识别意图
      "research"   重查指令     → 调用方沿用原条件重搜
      "back"       返回酒店列表 → 调用方直接展示原搜索结果
      "quit"       彻底放弃     → 调用方直接回复 _QUIT_MESSAGE
      None         未命中
    """
    if looks_like_new_search(text):
        state.selected_hotel = None
        state.booking_context = {}
        state.pending_booking = {}
        state.pending_location_fix = {}
        state.params = BookingParams()   # 新旧条件不混杂
        state.stage = "intent"
        return "new_search"
    if any(w in text for w in BACK_TO_HOTEL_WORDS):
        if getattr(state, "search_results", None):
            state.selected_hotel = None
            state.booking_context = {}
            state.pending_booking = {}
            state.pending_location_fix = {}
            state.stage = "recommend"
            return "back"
        # 没有上一级列表可回：按重查处理，避免用户卡在选房阶段
        return "research"
    if any(w in text for w in RESEARCH_WORDS):
        state.selected_hotel = None
        state.booking_context = {}
        state.pending_booking = {}
        state.pending_location_fix = {}
        # stage 交给调用方重新置位；沿用原参数重搜
        return "research"
    if any(w in text for w in QUIT_BOOKING_WORDS):
        reset_booking_state(state)
        return "quit"
    return None


def quit_booking_message() -> str:
    return _QUIT_MESSAGE


# ========== 展示 ==========

def render_hotel_results(params, results: List[Hotel]) -> str:
    """渲染酒店搜索结果列表（搜索完成与“返回酒店列表”共用）。"""
    if not results:
        return "没有找到符合条件的酒店，建议放宽筛选条件。"
    nights = calc_nights(params.check_in, params.check_out)
    location = (params.location or "").strip()
    city_core = (params.city or "").rstrip("市")
    if location:
        area_text = (f"{location}，" if location.startswith(city_core)
                     else f"{params.city}{location}，")
    else:
        area_text = ""
    nearest_hint = ""
    located = [h for h in results if h.distance_m]
    if location and located:
        nearest = min(located, key=lambda h: h.distance_m)
        dist = int(round(nearest.distance_m / 10.0) * 10)
        nearest_hint = (
            f"按距{location.replace('附近', '')}由近到远排列，"
            f"最近的是 {nearest.name}（约{dist}米）。\n"
        )
    return (
        f"为你找到 {len(results)} 家符合条件的酒店"
        f"（{area_text}{params.check_in} 入住，{params.check_out} 离店，共{nights}晚）：\n\n"
        f"{nearest_hint}"
        f"{format_hotel_list(results)}\n\n"
        f"回复序号或酒店名选一家；想按价格、星级或距离再筛，"
        f"直接告诉我就行。"
    )


def load_hotel_selection_detail(hotel: Hotel, params) -> Hotel:
    """选中酒店后拉实时详情；失败/无详情时回退搜索阶段对象。"""
    detail = get_hotel_detail(hotel.hotel_id, params.check_in, params.check_out)
    return detail or hotel


def render_hotel_selection(hotel: Hotel, params,
                           show_login_hint: bool = False) -> str:
    """渲染选中酒店后的房型/价格展示（单 Agent / 多 Agent 共用）。"""
    nights = calc_nights(params.check_in, params.check_out)
    header = (
        f"好的，你选择了：{hotel.name}\n"
        f"地址：{hotel.address}\n"
        f"入住：{params.check_in} → {params.check_out}（{nights}晚）\n"
    )
    if hotel.image_url:
        header += f"实拍图：{hotel.image_url}\n"

    if hotel.rate_plans:
        # 实时报价模式：按房型分组，每个房型最多 4 档报价
        # （最低价 + 尽量覆盖“可取消/含早”的权益组合）
        max_rooms = 8
        groups = plan_groups(hotel.rate_plans)
        shown = groups[:max_rooms]
        room_lines = [f"可选房型（实时报价，按价格排序，共{len(groups)}种）："]
        multi_plan_exists = False
        for group_no, plans in shown:
            head = plans[0]
            bits = _room_header_bits(head)
            line = f"{group_no}. {head.room_name}"
            if bits:
                line += f"（{' · '.join(bits)}）"

            # 同一房型的多档报价共用一组实拍图，只展示一次
            images = plan_group_images(plans)
            if images:
                line += (f"\n   房间实拍（{len(images)}张）：{images[0]}")
                for url in images[1:]:
                    line += f"\n             {url}"

            lowest = min(p.price_per_night for p in plans)
            if len(plans) == 1:
                p = plans[0]
                extra = f"{_meal_text(p)} · {_cancel_text(p)}"
                if p.on_request:
                    extra += " · 需申请确认"
                line += f"\n   价格：{p.price_per_night:g}元/晚（{extra}）"
            else:
                multi_plan_exists = True
                for i, p in enumerate(plans):
                    delta = p.price_per_night - lowest
                    delta_text = ("本房型最低" if delta <= 0.001
                                  else f"比最低 +{delta:g}元/晚")
                    extras = [_meal_text(p), _cancel_text(p)]
                    if p.on_request:
                        extras.append("需申请确认")
                    mark = _CIRCLED[i] if i < len(_CIRCLED) else f"{i + 1}."
                    line += (f"\n   {mark} {p.price_per_night:g}元/晚 ｜"
                             f"{'｜'.join(extras)} ｜{delta_text}")
            room_lines.append(line)

        if len(groups) > max_rooms:
            room_lines.append(
                f"... 另有 {len(groups) - max_rooms} 种更高价位房型，"
                f"需要的话告诉我预算或床型帮你筛")
        if any(plan_group_images(plans) for _, plans in shown):
            room_lines.append("（点击房型实拍图可查看大图）")
        if multi_plan_exists:
            room_lines.append(
                "（同一房型有多档报价时，回复「房型序号-报价序号」选择，"
                "如 1-2；只回复 1 默认最低价）")
        room_lines.append(
            "\n请告诉我：选择哪个房型（回复序号或房型名）+ 入住人姓名？"
            "\n（例如：1，入住人张三）"
            "\n（不喜欢这家酒店？回复「返回上一级」回到酒店列表重新选）")
        if show_login_hint:
            import rollinggo_book as rlg
            if rlg.is_logged_in():
                room_lines.append(
                    "🔓 已登录道旅：回复房型+入住人后我会先【验价锁价】，"
                    "向你展示订单信息并经你【确认下单】后，才会创建真实订单。")
        return header + "\n" + "\n".join(room_lines)

    # 模拟库/高德回退：沿用酒店的每晚最低价
    total = calculate_total_price(hotel, params.check_in, params.check_out)
    return (
        header
        + f"房价：{hotel.price_per_night}元/晚 × {nights}晚 = {total}元\n\n"
        f"可选房型：{'、'.join(hotel.room_types)}\n\n"
        f"请告诉我：\n1. 选择哪个房型？\n2. 入住人姓名？\n"
        f"（例如：海景大床房，入住人张三）"
    )


def chat_reply(llm, user_input: str) -> str:
    """闲聊回复：真实 LLM 模式自然作答，模拟模式按场景给模板。"""
    text = user_input.strip().lower()
    if any(w in user_input for w in ("谢谢", "感谢", "多谢", "辛苦")):
        return "不客气～还有酒店查询、预订或订单相关的需要，随时告诉我。"
    if any(w in user_input for w in ("再见", "拜拜", "拜")) or text in ("bye", "goodbye"):
        return "再见！祝你旅途愉快，需要订酒店时再来找我。"
    if user_input.strip() in ("你好", "您好", "嗨", "hi", "hello", "你好啊") \
            or text in ("hi", "hello"):
        return (
            "你好！我是酒店预订智能助手，可以帮你：\n"
            "1. 搜索和预订酒店（告诉我城市、日期、预算等）\n"
            "2. 查当前订单（如「我的订单」「订单状态」）\n"
            "3. 查历史订单（如「查我的历史订单」「查待支付的订单」）\n"
            "4. 取消订单\n\n"
            "想订哪里的酒店？直接告诉我就行。"
        )

    if getattr(llm, "use_real_llm", False):
        context = (
            "你是一个酒店预订智能助手，具备酒店搜索预订、历史订单查询、取消订单能力，"
            "语气亲切简洁。请简短回应用户的寒暄或无关问题（不超过两句话），"
            "并自然引导用户说出订房需求（城市、日期、预算）。不要编造酒店信息。"
        )
        reply = llm.generate_response(context, user_input)
        if reply and reply != "好的，我明白了。":
            return reply

    return (
        "我主要能帮你处理这些事：\n"
        "1. 搜索和预订酒店（例如：帮我订下周末成都 300 元以内的酒店）\n"
        "2. 查当前那笔订单（例如：我的订单 / 订单状态）\n"
        "3. 查历史订单清单（例如：查我的历史订单 / 查待支付的订单）\n"
        "4. 取消订单\n\n"
        "告诉我你的需求吧。"
    )
