# -*- coding: utf-8 -*-
"""改口 / 否定约束 / 跨月日期 的单元与集成测试（不调真实模型）。

覆盖用户提出的三类"需要想一下"的场景：
  1. 多轮改口："不是成都，是重庆""成都改成重庆"
  2. 一句话带否定："不要海景房""不含早餐"
  3. 跨月/倒挂日期："28号到3号"、模型把离店日算早

运行：
    python tests/test_corrections_unit.py
    pytest tests/test_corrections_unit.py -v
"""

import json
import os
import sys
from datetime import datetime, timedelta

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(PROJ, "hotel_booking_agent")
sys.path.insert(0, APP)
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

import agent as agent_module                    # noqa: E402
from agent_common import merge_params           # noqa: E402
from llm import LLMClient                       # noqa: E402
from models import AgentState, BookingParams    # noqa: E402
from nlu_corrections import (                   # noqa: E402
    apply_text_corrections, find_correction_target, find_negated_terms,
    normalize_dates,
)

TODAY = datetime.now()


def _client(payload):
    client = LLMClient()
    client.use_real_llm = True
    client._chat = lambda prompt, json_mode=False: json.dumps(payload, ensure_ascii=False)
    return client


# ---------- 1. 改口 ----------

def test_correction_target_phrasings():
    assert find_correction_target("不是成都，是重庆") == "重庆"
    assert find_correction_target("不是成都而是重庆，明天入住") == "重庆"
    assert find_correction_target("成都改成重庆") == "重庆"
    assert find_correction_target("说错了，是春熙路附近") == "春熙路附近"
    assert find_correction_target("帮我订成都的酒店") == ""


def test_correction_sets_city_and_marks_flag():
    client = _client({"intent": "chat"})
    intent, params = client.extract_intent_and_params("不是成都，是重庆，明天")
    assert params["city"] == "重庆", params
    assert params.get("_corrected") is True
    assert params.get("check_in") == (TODAY + timedelta(days=1)).strftime("%Y-%m-%d")


def test_correction_to_location_keeps_city():
    params = {"city": "成都"}
    apply_text_corrections("不是春熙路，是太古里", params)
    assert params["city"] == "成都"
    assert params["location"] == "太古里", params


def test_correction_to_date_is_not_a_location():
    params = {}
    apply_text_corrections("改成明天", params)
    assert "location" not in params and "city" not in params, params


# ---------- 2. 否定约束 ----------

def test_negation_removes_keyword_and_facility():
    params = {"keyword": "海景", "facilities": ["泳池", "早餐"]}
    apply_text_corrections("不要海景房，也不要泳池", params)
    assert "keyword" not in params, params
    assert params["facilities"] == ["早餐"], params
    assert params["_negated"] == ["海景", "泳池"], params


def test_negation_terms():
    assert find_negated_terms("不要海景房") == ["海景"]
    assert find_negated_terms("不含早餐") == ["早餐"]
    assert find_negated_terms("帮我订成都的酒店") == []


def test_merge_clears_previous_keyword():
    state = AgentState()
    state.params = BookingParams(city="三亚", check_in="2026-12-01",
                                 check_out="2026-12-02", keyword="海景")
    merge_params(state, {"_negated": ["海景"]}, "不要海景房了")
    assert state.params.keyword is None, state.params


# ---------- 3. 跨月 / 倒挂日期 ----------

def test_inverted_dates_roll_to_next_month():
    check_in = (TODAY + timedelta(days=10)).strftime("%Y-%m-%d")
    check_out = (TODAY + timedelta(days=5)).strftime("%Y-%m-%d")
    params = {"check_in": check_in, "check_out": check_out}
    normalize_dates(params, "帮我订成都的酒店")
    assert params["check_out"] > params["check_in"], params


def test_range_across_month_end():
    """28 号到 3 号：离店必须落到下个月，且晚于入住。"""
    client = _client({"intent": "book", "city": "成都"})
    _, params = client.extract_intent_and_params("帮我订成都28号到3号的酒店")
    assert params.get("check_in") and params.get("check_out"), params
    assert params["check_out"] > params["check_in"], params
    assert params["check_in"][5:7] != params["check_out"][5:7] or \
           params["check_in"][:4] != params["check_out"][:4], params


def test_past_oral_date_rolls_forward():
    past = (TODAY - timedelta(days=12)).strftime("%Y-%m-%d")
    params = {"check_in": past}
    normalize_dates(params, "帮我订成都的酒店")
    assert params["check_in"] >= TODAY.strftime("%Y-%m-%d"), params


def test_explicit_past_year_kept():
    params = {"check_in": "2025-01-01"}
    normalize_dates(params, "2025-01-01 的订单")
    assert params["check_in"] == "2025-01-01", params


# ---------- 4. 集成：改口后会自动重新搜索 ----------

def test_correction_triggers_re_search():
    ag = agent_module.HotelBookingAgent()
    ag.state.params = BookingParams(city="成都", check_in="2026-12-01",
                                    check_out="2026-12-02")
    ag.state.stage = "recommend"
    ag.state.search_results = []
    ag.llm.extract_intent_and_params = lambda text: (
        "chat", {"city": "重庆", "_corrected": True})

    calls = []
    original = agent_module.search_hotels
    agent_module.search_hotels = lambda params: calls.append(params.city) or []
    try:
        ag.run("不是成都，是重庆")
    finally:
        agent_module.search_hotels = original

    assert ag.state.params.city == "重庆", ag.state.params
    assert calls == ["重庆"], calls
    assert ag.state.stage == "recommend"


def main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
