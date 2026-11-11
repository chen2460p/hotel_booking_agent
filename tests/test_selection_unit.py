# -*- coding: utf-8 -*-
"""结构化选店单元/接口测试（不依赖真实 LLM 与道旅接口）。

覆盖：
  1. 搜索后回复带结构化酒店视图（revision + items + intro/outro）
  2. GET /api/session/{sid}/hotels：正常返回 items；旧 revision → 409
  3. POST /api/session/{sid}/select：hotel_id + revision 选店成功
  4. 旧 revision 选店 → 409；不存在的 hotel_id → 404
  5. 纯文本序号选择仍可用（旧入口不回归）

运行：
    python tests/test_selection_unit.py
    pytest tests/test_selection_unit.py -v
"""

import os
import sys
import time

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(PROJ, "hotel_booking_agent")
sys.path.insert(0, APP)
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from fastapi.testclient import TestClient   # noqa: E402

import agent as agent_module                # noqa: E402
import web as web_module                    # noqa: E402
from models import BookingParams, Hotel     # noqa: E402


def _fake_hotels():
    return [
        Hotel(hotel_id="H900", name="测试酒店A", city="成都", address="测试路1号",
              star=4, price_per_night=300, facilities=["WiFi"], room_types=["大床房"],
              rating=4.5, review_count=100),
        Hotel(hotel_id="H901", name="测试酒店B", city="成都", address="测试路2号",
              star=5, price_per_night=600, facilities=["泳池"], room_types=["双床房"],
              rating=4.8, review_count=200),
        Hotel(hotel_id="H902", name="测试酒店C", city="成都", address="测试路3号",
              star=3, price_per_night=180, facilities=[], room_types=["标准间"],
              rating=0, review_count=0),
    ]


def _make_agent(sid):
    """造一个已完成搜索的会话，并注册进 web 会话池（不走网络）。"""
    ag = agent_module.HotelBookingAgent()
    ag.state.params = BookingParams(city="成都", check_in="2026-12-01",
                                    check_out="2026-12-02")
    ag.state.search_results = _fake_hotels()
    ag.state.search_revision = "rev-test-1"
    ag.state.stage = "recommend"
    ag.state.last_view = {
        "kind": "hotels", "revision": "rev-test-1",
        "intro": "为你找到 3 家符合条件的酒店：\n\n",
        "outro": "\n\n回复序号或酒店名选一家。",
    }
    web_module._agents[sid] = {"agent": ag, "ts": time.time()}
    return ag


def _client():
    return TestClient(web_module.app)


def test_search_result_has_hotel_view():
    """agent 搜索后要登记视图与 revision（Web payload 才能下发卡片）。"""
    ag = agent_module.HotelBookingAgent()
    ag.state.params = BookingParams(city="成都", check_in="2026-12-01",
                                    check_out="2026-12-02")
    orig_search = agent_module.search_hotels
    orig_llm = ag.llm.extract_intent_and_params
    try:
        agent_module.search_hotels = lambda params: _fake_hotels()
        ag.llm.extract_intent_and_params = lambda text: ("book", {})
        reply = ag.run("帮我订成都的酒店")
    finally:
        agent_module.search_hotels = orig_search
        ag.llm.extract_intent_and_params = orig_llm

    assert "测试酒店A" in reply, reply[:200]
    assert ag.state.search_revision, "搜索后必须生成 revision"
    view = ag.state.last_view
    assert view.get("kind") == "hotels", view
    assert view.get("revision") == ag.state.search_revision
    assert view.get("intro") and view.get("outro")


def test_hotels_endpoint_returns_items_and_checks_revision():
    sid = "unit-hotels"
    _make_agent(sid)
    c = _client()

    r = c.get(f"/api/session/{sid}/hotels", params={"offset": 0, "limit": 2,
                                                    "revision": "rev-test-1"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["revision"] == "rev-test-1"
    assert d["total"] == 3
    assert [it["hotel_id"] for it in d["items"]] == ["H900", "H901"], d
    assert d["items"][0]["name"] == "测试酒店A"
    assert d["done"] is False

    # 旧版本号：拒绝，避免前端拿到错位的列表
    r2 = c.get(f"/api/session/{sid}/hotels", params={"revision": "rev-old"})
    assert r2.status_code == 409, r2.text


def test_select_by_hotel_id_and_revision():
    sid = "unit-select"
    ag = _make_agent(sid)
    c = _client()

    r = c.post(f"/api/session/{sid}/select",
               json={"hotel_id": "H901", "revision": "rev-test-1"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["stage"] == "booking", d
    assert ag.state.selected_hotel.hotel_id == "H901"
    assert d["hotel_view"] is None, "选店回复不是酒店列表，不应再下发卡片"
    assert "测试酒店B" in d["reply"]


def test_select_rejects_stale_revision():
    sid = "unit-stale"
    ag = _make_agent(sid)
    c = _client()

    r = c.post(f"/api/session/{sid}/select",
               json={"hotel_id": "H900", "revision": "rev-old"})
    assert r.status_code == 409, r.text
    assert "已更新" in r.json()["detail"], r.json()
    assert ag.state.selected_hotel is None, "旧列表不能按位置/名字猜出酒店"


def test_select_rejects_unknown_hotel():
    sid = "unit-unknown"
    _make_agent(sid)
    c = _client()

    r = c.post(f"/api/session/{sid}/select",
               json={"hotel_id": "H999", "revision": "rev-test-1"})
    assert r.status_code == 404, r.text


def test_text_ordinal_selection_still_works():
    """旧入口（输入“2”）按当前列表序号选店，行为保持不变。"""
    sid = "unit-ordinal"
    ag = _make_agent(sid)
    orig_llm = ag.llm.extract_intent_and_params
    try:
        ag.llm.extract_intent_and_params = lambda text: ("chat", {})
        reply = ag.run("2")
    finally:
        ag.llm.extract_intent_and_params = orig_llm

    assert ag.state.selected_hotel is not None, reply[:200]
    assert ag.state.selected_hotel.hotel_id == "H901"


def test_hotels_endpoint_unknown_session():
    c = _client()
    r = c.get("/api/session/no-such-session/hotels")
    assert r.status_code == 404, r.text
    # 只读接口不能把会话创建出来
    assert "no-such-session" not in web_module._agents


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
