# -*- coding: utf-8 -*-
"""NLU 后处理：改口、否定约束、跨月日期推断。

模型（或规则引擎）只负责"听到什么"，这里负责三件确定性的事：
  1. 多轮改口："不是成都，是重庆""成都改成重庆" → 以新值覆盖
  2. 否定约束："不要海景房""不含早餐" → 不再把被否定的词当筛选条件
  3. 跨月/倒挂日期："28号到3号"、模型把离店日算早 → 修正成合法的未来区间

放在独立模块里，单/多 Agent 共用，也方便单测；不依赖 llm/agent_common，避免循环导入。
"""

import calendar
import re
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional

from geo_data import find_city, find_similar_city, get_city_coord

# 改口句式：命中后取最后一个捕获组作为"新值"
_CORRECTION_PATTERNS = (
    # 不是成都，是重庆 / 不是成都而是重庆
    re.compile(r"不是\s*([^，,。；;！!？?\s]{1,10})\s*[，,。；;]?\s*(?:而)?是\s*"
               r"([^，,。；;！!？?\s]{1,10})"),
    # 成都不对，是重庆 / 成都说错了，改成重庆
    re.compile(r"([^，,。；;！!？?\s]{1,10})\s*(?:不对|错了|说错了)\s*[，,。；;]?\s*"
               r"(?:是|改成|换成|改为|换为)\s*([^，,。；;！!？?\s]{1,10})"),
    # 改成重庆 / 换成重庆 / 改到重庆
    re.compile(r"(?:改成|换成|改为|换为|改到)\s*([^，,。；;！!？?\s]{1,10})"),
)

# 否定词 + 约束词的窗口
_NEGATION_RE = re.compile(
    r"(不要|别要|不需要|不用|不含|避开|拒绝|除了)\s*([^，,。；;！!？?]{0,12})")

# 归一后的约束词 → 原文里可能出现的写法
_CONSTRAINT_ALIASES = {
    "海景": ("海景",),
    "温泉": ("温泉",),
    "亲子": ("亲子",),
    "泳池": ("泳池", "游泳池"),
    "早餐": ("早餐", "含早"),
    "大床": ("大床",),
    "双床": ("双床", "两张床", "2张床"),
    "浴缸": ("浴缸",),
}

# 改口目标不可能是地点的词（防止把"改成明天"当成地址）
_NOT_LOCATION = (
    "今天", "明天", "后天", "大后天", "今晚", "明晚", "昨天", "前天",
    "下周末", "这周末", "本周末", "下周", "本周", "这周",
    "入住", "离店", "退房", "酒店", "宾馆", "民宿",
)
_EXPLICIT_YEAR_RE = re.compile(r"\d{4}\s*[-/年]\s*\d{1,2}")


# ---------- 日期工具 ----------

def _parse(value) -> Optional[date]:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def add_month(day: date, months: int = 1) -> date:
    """按月推进，日号超出目标月天数时取该月最后一天。"""
    year = day.year + (day.month - 1 + months) // 12
    month = (day.month - 1 + months) % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def roll_forward(date_str: str, today: datetime) -> str:
    """把早于今天的口语日期向前滚到未来的同月日。"""
    current = _parse(date_str)
    if current is None or current >= today.date():
        return date_str
    for _ in range(24):
        current = add_month(current)
        if current >= today.date():
            return current.isoformat()
    return date_str


def normalize_dates(params: Dict, text: str = "", today: Optional[datetime] = None) -> Dict:
    """修正 params 里的日期：过去日期前滚、离店不早于入住（跨月推断）。

    只改 check_in / check_out；无法修正时丢弃不可信的离店日（让下一轮追问），
    绝不带着倒挂或过去的日期去搜索。
    """
    today = today or datetime.now()
    explicit_year = bool(_EXPLICIT_YEAR_RE.search(text or ""))
    check_in = _parse(params.get("check_in"))
    check_out = _parse(params.get("check_out"))

    if not explicit_year:
        if check_in and check_in < today.date():
            check_in = _parse(roll_forward(check_in.isoformat(), today))
            params["check_in"] = check_in.isoformat()
        if check_out and check_out < today.date():
            check_out = _parse(roll_forward(check_out.isoformat(), today))
            params["check_out"] = check_out.isoformat()

    if check_in and check_out and check_out <= check_in:
        candidate = check_out
        for _ in range(12):
            candidate = add_month(candidate)
            if candidate > check_in:
                break
        # 跨月推断最多接受 90 晚，超过就认为模型/用户信息不可信
        if candidate > check_in and (candidate - check_in).days <= 90:
            params["check_out"] = candidate.isoformat()
        else:
            params.pop("check_out", None)
    return params


# ---------- 改口 ----------

def _clean_target(target: str) -> str:
    return (target or "").strip(" 　的了吧")


def _resolve_city(value: str) -> str:
    """把改口目标解析成已知城市；解析不出返回空串。"""
    value = (value or "").strip(" 　的了吧")
    if not value:
        return ""
    city = find_city(value)
    if city:
        return city
    core = value.rstrip("市")
    if get_city_coord(core):
        return core
    if value.endswith("市"):
        return find_similar_city(core)
    return ""


def _looks_like_location(value: str) -> bool:
    text = (value or "").strip(" 　的了吧")
    if len(text) < 2 or len(text) > 12:
        return False
    if text.isdigit() or any(w in text for w in _NOT_LOCATION):
        return False
    return bool(re.search(r"[\u4e00-\u9fa5]", text))


def find_correction_target(text: str) -> str:
    """从"不是A，是B""A改成B"里取出新值 B；没有改口返回空串。"""
    for pattern in _CORRECTION_PATTERNS:
        match = pattern.search(text or "")
        if match:
            target = match.group(match.lastindex)
            cleaned = _clean_target(target)
            if cleaned:
                return cleaned
    return ""


def apply_text_corrections(text: str, params: Dict) -> Dict:
    """就地修正 params：改口目标覆盖城市/地点，否定词移除对应约束。"""
    target = find_correction_target(text)
    if target:
        city = _resolve_city(target)
        if city:
            params["city"] = city
            params["_corrected"] = True
        elif _looks_like_location(target):
            params["location"] = target
            params["_corrected"] = True

    negated = find_negated_terms(text)
    if negated:
        params["_negated"] = negated
        params["_corrected"] = True
        keyword = str(params.get("keyword") or "")
        if keyword and any(term in keyword for term in negated):
            params.pop("keyword", None)
        facilities = params.get("facilities")
        if isinstance(facilities, list):
            kept = [f for f in facilities
                    if not any(term in str(f) for term in negated)]
            if kept:
                params["facilities"] = kept
            else:
                params.pop("facilities", None)

    return params


# ---------- 否定 ----------

def find_negated_terms(text: str) -> List[str]:
    """返回被否定的约束词（归一后），如"不要海景房" → ["海景"]。"""
    found: List[str] = []
    for match in _NEGATION_RE.finditer(text or ""):
        tail = match.group(2) or ""
        for term, aliases in _CONSTRAINT_ALIASES.items():
            if term in found:
                continue
            if any(alias in tail for alias in aliases):
                found.append(term)
    return found


