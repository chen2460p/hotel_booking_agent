# -*- coding: utf-8 -*-
"""NLU 后处理单元测试（不调真实模型）。

覆盖评测暴露的三类问题：
  1. prompt 里"12 15"的示例必须随今天滚动，不能再指向过去日期
  2. 城市笔误（大年市 → 大连）要纠正，location 前缀一起换
  3. 查订单没给状态时默认 ALL
  4. 口语日期兜底：过去日期滚到未来；带显式年份的原样保留
  5. 规则引擎能解析带前后缀的裸数字日期对（"帮我订成都12 15的酒店"）

运行：
    python tests/test_nlu_postprocess_unit.py
    pytest tests/test_nlu_postprocess_unit.py -v
"""

import json
import os
import sys
from datetime import date, datetime, timedelta

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(PROJ, "hotel_booking_agent")
sys.path.insert(0, APP)
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

import llm as llm_module              # noqa: E402
from llm import LLMClient             # noqa: E402

TODAY = datetime.now()


def _client_with_response(payload):
    """构造一个真实模式客户端，但把模型请求替换成固定 JSON。"""
    client = LLMClient()
    client.use_real_llm = True
    client._chat = lambda prompt, json_mode=False: json.dumps(payload, ensure_ascii=False)
    return client


def test_prompt_pair_example_is_future():
    """prompt 里给出的 "12 15" 示例日期必须不早于今天。"""
    captured = {}

    def fake_chat(prompt, json_mode=False):
        captured["prompt"] = prompt
        return None

    client = LLMClient()
    client.use_real_llm = True
    client._chat = fake_chat
    client.extract_intent_and_params("帮我订成都12 15的酒店")

    year, month = TODAY.year, TODAY.month
    if TODAY.day > 12:
        month += 1
        if month > 12:
            month, year = 1, year + 1
    expected_in = f"{year:04d}-{month:02d}-12"
    prompt = captured["prompt"]
    assert expected_in in prompt, prompt
    assert date.fromisoformat(expected_in) >= TODAY.date()


def test_city_typo_is_corrected():
    client = _client_with_response({
        "intent": "search", "city": "大年市",
        "location": "大年市西安路附近",
    })
    intent, params = client.extract_intent_and_params("大年市西安路附近有什么酒店")
    assert intent == "search"
    assert params["city"] == "大连", params
    assert params["location"].startswith("大连"), params


def test_order_query_defaults_to_all():
    client = _client_with_response({"intent": "order_query"})
    intent, params = client.extract_intent_and_params("查订单状态")
    assert intent == "order_query"
    assert params.get("order_status") == "ALL", params


def test_past_oral_dates_roll_forward():
    past_in = (TODAY - timedelta(days=10)).strftime("%Y-%m-%d")
    past_out = (TODAY - timedelta(days=7)).strftime("%Y-%m-%d")
    client = _client_with_response({
        "intent": "book", "city": "成都",
        "check_in": past_in, "check_out": past_out,
    })
    _, params = client.extract_intent_and_params("帮我订成都的酒店")
    assert params["check_in"] >= TODAY.strftime("%Y-%m-%d"), params
    assert params["check_out"] > params["check_in"], params


def test_explicit_past_year_is_kept():
    client = _client_with_response({
        "intent": "book", "city": "成都", "check_in": "2025-01-01",
    })
    _, params = client.extract_intent_and_params("帮我订2025-01-01成都的酒店")
    assert params["check_in"] == "2025-01-01", params


def test_budget_number_does_not_disable_roll():
    """预算 1000 是 4 位数字，不能被当成"显式年份"而跳过过去日期兜底。"""
    past_in = (TODAY - timedelta(days=10)).strftime("%Y-%m-%d")
    client = _client_with_response({
        "intent": "book", "city": "成都", "check_in": past_in,
    })
    _, params = client.extract_intent_and_params("帮我订成都的酒店，预算1000以内")
    assert params["check_in"] >= TODAY.strftime("%Y-%m-%d"), params


def test_empty_model_and_timeout_fall_back():
    """部署 Secret 未配置会写成空值：模型/超时都必须回退默认，不能崩。"""
    old_model = os.environ.get("DASHSCOPE_MODEL")
    old_timeout = os.environ.get("DASHSCOPE_TIMEOUT")
    try:
        os.environ["DASHSCOPE_MODEL"] = ""
        os.environ["DASHSCOPE_TIMEOUT"] = ""
        client = LLMClient()
        assert client.model == "deepseek-v4.1-flash", client.model
        assert llm_module._read_timeout() == 30.0
        os.environ["DASHSCOPE_TIMEOUT"] = "abc"
        assert llm_module._read_timeout() == 30.0
        os.environ["DASHSCOPE_TIMEOUT"] = "45"
        assert llm_module._read_timeout() == 45.0
    finally:
        for key, old in (("DASHSCOPE_MODEL", old_model),
                         ("DASHSCOPE_TIMEOUT", old_timeout)):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def test_rules_parse_pair_with_prefix_suffix():
    client = LLMClient()
    client.use_real_llm = False
    intent, params = client.extract_intent_and_params("帮我订成都12 15的酒店")
    assert intent == "book"
    assert params.get("check_in") and params.get("check_out"), params
    assert params["check_in"].endswith("-12"), params
    assert params["check_out"].endswith("-15"), params
    assert params["check_out"] > params["check_in"], params
    assert params["check_in"] >= TODAY.strftime("%Y-%m-%d"), params


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
