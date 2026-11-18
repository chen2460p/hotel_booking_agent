# -*- coding: utf-8 -*-
"""结构化房型视图 + 结构化选房接口测试（不依赖真实 LLM 与道旅接口）。

覆盖：
  1. rooms_view 的编号/价格/退改字段与文本渲染同源（都用 plan_groups）
  2. 无实时报价时 rooms_view 返回 None（模拟/高德回退仍走文本链路）
  3. POST /api/session/{sid}/room 按「房型序号 + 报价序号」选房
  4. 旧 rooms_revision → 409；不存在的房型/报价 → 404
  5. 只带房型 → 暂存等姓名；带入住人 → 直接下单（mock，不联网/不写库）
  6. 选店后的 ChatResponse 带 rooms_view
  7. 旧文本入口（回复「2-1」）行为不回归

运行：
    python tests/test_rooms_view_unit.py
    pytest tests/test_rooms_view_unit.py -v
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
from agent_common import rooms_view, render_hotel_selection   # noqa: E402
from models import BookingParams, Hotel, Order, RoomRatePlan  # noqa: E402

PARAMS = dict(city="成都", check_in="2026-12-01", check_out="2026-12-03")


def _hotel_with_plans():
    """2 个房型、共 3 档报价（第一个房型两档，第二个房型一档）。"""
    return Hotel(
        hotel_id="H900", name="测试酒店A", city="成都", address="测试路1号",
        star=4, price_per_night=300, facilities=["WiFi"],
        room_types=["大床房", "双床房"], rating=4.5, review_count=100,
        rate_plans=[
            RoomRatePlan(room_name="大床房", price_per_night=300, meal="无早",
                         cancelable=True,
                         cancel_policy="免费取消截止至2026-11-30 18:00",
                         bed_type="1 大床", max_occupancy=2, room_size="25",
                         rate_plan_id="RP_A1", room_no=1, plan_no=1),
            RoomRatePlan(room_name="大床房", price_per_night=360, meal="含早",
                         cancelable=False, cancel_policy="不可取消",
                         bed_type="1 大床", max_occupancy=2, room_size="25",
                         rate_plan_id="RP_A2", room_no=1, plan_no=2),
            RoomRatePlan(room_name="双床房", price_per_night=420, meal="含早",
                         cancelable=True,
                         cancel_policy="免费取消截止至2026-11-29 18:00",
                         bed_type="2 单人床", max_occupancy=2, room_size="30",
                         rate_plan_id="RP_B1", room_no=2, plan_no=1),
        ],
    )


def _params():
    return BookingParams(**PARAMS)


def _client():
    return TestClient(web_module.app)


def _make_booking_agent(sid):
    """造一个「已选酒店、正在选房」的会话，注册进 web 会话池（不走网络）。"""
    ag = agent_module.HotelBookingAgent()
    ag.state.params = _params()
    ag.state.selected_hotel = _hotel_with_plans()
    ag.state.stage = "booking"
    ag.state.rooms_revision = "rev-rooms-1"
    web_module._agents[sid] = {"agent": ag, "ts": time.time()}
    return ag


# ---------- 1. 序列化：编号/字段与文本同源 ----------

def test_rooms_view_matches_text_numbering():
    hotel = _hotel_with_plans()
    params = _params()
    rv = rooms_view(hotel, params, "rev-rooms-1")

    assert rv["rooms_revision"] == "rev-rooms-1"
    assert rv["nights"] == 2
    assert rv["total_rooms"] == 2 and rv["shown_rooms"] == 2
    assert rv["truncated"] is False
    assert [r["room_no"] for r in rv["rooms"]] == [1, 2]

    room1 = rv["rooms"][0]
    assert room1["room_name"] == "大床房"
    assert [p["plan_no"] for p in room1["plans"]] == [1, 2]
    assert room1["plans"][0]["plan_key"] == "RP_A1"
    assert room1["plans"][1]["plan_key"] == "RP_A2"
    # 总价 = 每晚 × 晚数（2 晚）
    assert room1["plans"][0]["total_price"] == 600.0
    assert room1["plans"][1]["total_price"] == 720.0
    # 最低价标记只看本房型内部
    assert room1["plans"][0]["is_lowest"] is True
    assert room1["plans"][1]["is_lowest"] is False
    assert room1["plans"][1]["cancelable"] is False
    assert rv["rooms"][1]["plans"][0]["is_lowest"] is True

    # 文本渲染用同一套编号：房型 1 = 大床房，且提示「1-2」选档
    text = render_hotel_selection(hotel, params)
    assert "1. 大床房" in text
    assert "2. 双床房" in text
    assert "1-2" in text


def test_rooms_view_none_without_rate_plans():
    hotel = Hotel(hotel_id="H901", name="模拟酒店", city="成都", address="测试路2号",
                  star=3, price_per_night=180, facilities=[], room_types=["标准间"],
                  rating=0, review_count=0)
    assert rooms_view(hotel, _params(), "rev") is None
    assert rooms_view(None, _params(), "rev") is None


# ---------- 2. 接口：结构化选房 ----------

def test_rooms_endpoint_selects_plan_and_waits_for_guest():
    sid = "unit-room-select"
    ag = _make_booking_agent(sid)
    c = _client()

    r = c.post(f"/api/session/{sid}/room",
               json={"room_no": 1, "plan_no": 2, "rooms_revision": "rev-rooms-1"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert ag.state.pending_booking["rate_plan_id"] == "RP_A2", ag.state.pending_booking
    assert "大床房" in d["reply"] and "已记下" in d["reply"]
    # 已选房、等姓名：本轮不再下发房型卡
    assert d["rooms_view"] is None, d
    assert d["stage"] == "booking"


def test_rooms_endpoint_rejects_stale_rooms_revision():
    sid = "unit-room-stale"
    ag = _make_booking_agent(sid)
    c = _client()

    r = c.post(f"/api/session/{sid}/room",
               json={"room_no": 1, "plan_no": 1, "rooms_revision": "rev-old"})
    assert r.status_code == 409, r.text
    assert "已更新" in r.json()["detail"]
    assert ag.state.pending_booking == {}, "旧房型列表不能按序号猜档位"


def test_rooms_endpoint_rejects_unknown_room_or_plan():
    sid = "unit-room-unknown"
    _make_booking_agent(sid)
    c = _client()

    r = c.post(f"/api/session/{sid}/room", json={"room_no": 9, "plan_no": 1})
    assert r.status_code == 404, r.text
    r2 = c.post(f"/api/session/{sid}/room", json={"room_no": 1, "plan_no": 9})
    assert r2.status_code == 404, r2.text


def test_rooms_endpoint_with_guest_creates_mock_order():
    """带入住人 → 直接下单（mock create_order，不联网、不写库）。"""
    sid = "unit-room-guest"
    ag = _make_booking_agent(sid)
    c = _client()

    def fake_create_order(hotel, params, room_type, guest_name, price_per_night=None):
        return Order(order_id="ORDTEST01", hotel_id=hotel.hotel_id,
                     hotel_name=hotel.name, check_in=params.check_in,
                     check_out=params.check_out, room_type=room_type,
                     guest_name=guest_name,
                     total_price=(price_per_night or hotel.price_per_night) * 2)

    orig = agent_module.create_order
    agent_module.create_order = fake_create_order
    try:
        r = c.post(f"/api/session/{sid}/room",
                   json={"room_no": 2, "plan_no": 1, "guest_name": "陈晨"})
    finally:
        agent_module.create_order = orig

    assert r.status_code == 200, r.text
    assert ag.state.current_order is not None
    assert ag.state.current_order.room_type == "双床房"
    assert ag.state.current_order.total_price == 840.0
    assert "订单创建成功" in r.json()["reply"]


# ---------- 3. 选店后下发 rooms_view ----------

def test_select_hotel_returns_rooms_view():
    sid = "unit-rooms-view"
    ag = agent_module.HotelBookingAgent()
    ag.state.params = _params()
    ag.state.search_results = [_hotel_with_plans()]
    ag.state.search_revision = "rev-hotels-1"
    ag.state.stage = "recommend"
    web_module._agents[sid] = {"agent": ag, "ts": time.time()}
    c = _client()

    r = c.post(f"/api/session/{sid}/select",
               json={"hotel_id": "H900", "revision": "rev-hotels-1"})
    assert r.status_code == 200, r.text
    d = r.json()
    rv = d["rooms_view"]
    assert rv is not None, d
    assert rv["hotel_id"] == "H900"
    assert rv["rooms_revision"] == ag.state.rooms_revision != ""
    assert [room["room_no"] for room in rv["rooms"]] == [1, 2]
    assert rv["rooms"][0]["plans"][1]["plan_key"] == "RP_A2"


# ---------- 4. 旧文本入口不回归 ----------

def test_text_room_ordinal_still_works():
    sid = "unit-room-text"
    ag = _make_booking_agent(sid)
    reply = ag.process_booking_confirmation("2-1")
    assert "已记下" in reply, reply
    assert ag.state.pending_booking["rate_plan_id"] == "RP_B1"


def test_session_info_exposes_rooms_view():
    """会话恢复接口也要能拿到房型视图（刷新页面/换端后继续选房）。"""
    sid = "unit-room-info"
    ag = _make_booking_agent(sid)
    c = _client()

    r = c.get(f"/api/session/{sid}")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["stage"] == "booking"
    assert d["rooms_revision"] == "rev-rooms-1"
    assert d["rooms_view"] is not None
    assert d["rooms_view"]["rooms"][0]["plans"][0]["plan_key"] == "RP_A1"

    # 已暂存房型后（等姓名）不再重复下发
    ag.state.pending_booking = {"room_type": "大床房", "rate_plan_id": "RP_A1"}
    d2 = c.get(f"/api/session/{sid}").json()
    assert d2["rooms_view"] is None


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
