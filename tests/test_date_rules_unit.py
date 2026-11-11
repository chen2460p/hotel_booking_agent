# -*- coding: utf-8 -*-
"""日期规则测试：用户只回两个裸数字（如"12 15"）也要能识别成入住/离店日期。

背景：被追问"入住和离店日期"时，用户常直接回"12 15"。
旧规则只认"12号""12月15日""12号到15号"，裸数字对会被判成闲聊、掉回帮助菜单。

运行：
    python tests/test_date_rules_unit.py
    pytest tests/test_date_rules_unit.py -v
"""

import os
import sys
from datetime import datetime

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(PROJ, "hotel_booking_agent")
sys.path.insert(0, APP)
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

import agent as agent_module                # noqa: E402
from llm import LLMClient                   # noqa: E402
from models import BookingParams, Hotel     # noqa: E402


def _rules(text):
    return LLMClient()._extract_with_rules(text)


def _assert_range(params, nights):
    assert params.get("check_in") and params.get("check_out"), params
    d1 = datetime.strptime(params["check_in"], "%Y-%m-%d")
    d2 = datetime.strptime(params["check_out"], "%Y-%m-%d")
    assert d2 > d1, params
    assert (d2 - d1).days == nights, params
    # 不能落到过去
    assert d1.date() >= datetime.now().date(), params


def test_bare_pair_space():
    _, p = _rules("12 15")
    _assert_range(p, 3)


def test_bare_pair_with_units():
    _, p = _rules("12号 15号")
    _assert_range(p, 3)
    _, p2 = _rules("12日15日")
    _assert_range(p2, 3)


def test_bare_pair_separators():
    for text in ("12、15", "12-15", "12/15", "12到15号", "12 15号"):
        _, p = _rules(text)
        _assert_range(p, 3)


def test_numeric_string_is_not_a_pair():
    """1215 这种数字串不能被当成 12 号到 15 号。"""
    _, p = _rules("1215")
    assert "check_in" not in p and "check_out" not in p, p


def test_single_values_still_work():
    _, p = _rules("12月15日")
    assert p.get("check_in", "").endswith("12-15"), p
    assert "check_out" not in p, p

    _, p2 = _rules("15号")
    assert p2.get("check_in", "").endswith("-15"), p2
    assert "check_out" not in p2, p2


def test_cross_month_pair():
    """28 3 → 28 号入住、下月 3 号离店。"""
    _, p = _rules("28 3")
    assert p.get("check_in") and p.get("check_out"), p
    d1 = datetime.strptime(p["check_in"], "%Y-%m-%d")
    d2 = datetime.strptime(p["check_out"], "%Y-%m-%d")
    assert d2 > d1, p
    assert 2 <= (d2 - d1).days <= 6, p


def test_llm_merge_prefers_rule_pair():
    """LLM 把"12 15"误读成单个 12 月 15 日时，以规解析出的成对日期为准。"""
    c = LLMClient()
    c._chat = lambda prompt, json_mode=False: '{"intent":"chat","check_in":"2027-12-15"}'
    _, p = c._extract_with_llm("12 15")
    _assert_range(p, 3)


def test_llm_merge_never_leaves_inverted_dates():
    """任何来源拼出的倒挂日期都不能带进搜索。"""
    c = LLMClient()
    c._chat = lambda prompt, json_mode=False: (
        '{"intent":"book","check_in":"2027-12-20","check_out":"2027-12-10"}')
    _, p = c._extract_with_llm("12 15")
    if p.get("check_in") and p.get("check_out"):
        assert p["check_out"] > p["check_in"], p


def test_agent_promotes_bare_pair_to_search():
    """澄清阶段回"12 15"要直接进入搜索，而不是掉回帮助菜单。"""
    ag = agent_module.HotelBookingAgent()
    ag.state.params = BookingParams(city="沈阳")
    ag.state.stage = "clarify"
    orig_search = agent_module.search_hotels
    orig_extract = ag.llm.extract_intent_and_params
    try:
        agent_module.search_hotels = lambda params: [
            Hotel(hotel_id="H910", name="测试酒店", city="沈阳", address="测试路",
                  star=4, price_per_night=300, facilities=[], room_types=["大床房"],
                  rating=4.5, review_count=10)
        ]
        ag.llm.extract_intent_and_params = lambda text: ag.llm._extract_with_rules(text)
        reply = ag.run("12 15")
    finally:
        agent_module.search_hotels = orig_search
        ag.llm.extract_intent_and_params = orig_extract

    assert ag.state.params.check_in and ag.state.params.check_out, reply[:200]
    assert "为你找到" in reply, reply[:200]
    assert ag.state.params.check_in in reply and ag.state.params.check_out in reply, reply[:300]


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
