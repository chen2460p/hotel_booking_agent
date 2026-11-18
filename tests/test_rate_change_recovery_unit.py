# -*- coding: utf-8 -*-
"""房型报价变动（6201 验价失败）的回退流程测试（不联网、不依赖真实 LLM）。

起因：用户回复入住人姓名后验价返回
  code=6201：Price confirm failed: No room available
旧逻辑只让用户「重新回复房型序号 + 入住人姓名」，但列表里还是同一档
失效报价，于是同一句失败文案反复出现（死循环）。

现在的要求：报价变动就回退到选房流程，重新给用户当前还能订的房型。

覆盖：
  1. 错误识别：6201 / No room available 判为「房型不可售」，其余报错不误判
  2. 验价 6201 → 刷新房型 + 摘掉失效报价 + 回到选房阶段，且记住入住人姓名
  3. 回退后用户只回复新序号即可重新验价（不用再报姓名）
  4. 整家酒店都售罄 → 自动退回酒店列表让用户换一家
  5. 下单（hotelbook）遇到 6201 同样回退选房
  6. 非售罄类接口报错保持原提示，不动当前房型列表
  7. rooms_view：只暂存姓名时仍下发房型卡；已选房型等姓名时不再重复下发

运行：
    python tests/test_rate_change_recovery_unit.py
    pytest tests/test_rate_change_recovery_unit.py -v
"""

import os
import sys
import time
from contextlib import contextmanager

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(PROJ, "hotel_booking_agent")
sys.path.insert(0, APP)
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from fastapi.testclient import TestClient                     # noqa: E402

import agent as agent_module                                  # noqa: E402
import rollinggo_book as rlg                                  # noqa: E402
import web as web_module                                      # noqa: E402
from agent_common import drop_rate_plan, plan_groups          # noqa: E402
from models import BookingParams, Hotel, RoomRatePlan         # noqa: E402

HOTEL_ID = "RLG_9001"
NO_ROOM_MSG = ("道旅业务错误 code=6201：Price confirm failed: "
               "No room available")


@contextmanager
def _patch(obj, **values):
    """临时替换模块属性，退出时还原（避免影响其它用例）。"""
    originals = {k: getattr(obj, k) for k in values}
    for k, v in values.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in originals.items():
            setattr(obj, k, v)


def _raise(message):
    def _fn(*args, **kwargs):
        raise rlg.RollingGoApiError(message)
    return _fn


def _hotel():
    """2 个房型、3 档报价（大床房 2 档 + 双床房 1 档）。"""
    return Hotel(
        hotel_id=HOTEL_ID, name="测试道旅酒店", city="成都", address="测试路1号",
        star=4, price_per_night=300, facilities=["WiFi"],
        room_types=["大床房", "双床房"], rating=4.5, review_count=10,
        rate_plans=[
            RoomRatePlan(room_name="大床房", price_per_night=300, meal="无早",
                         cancelable=True, cancel_policy="免费取消",
                         bed_type="1 大床", rate_plan_id="RP_A1",
                         room_no=1, plan_no=1),
            RoomRatePlan(room_name="大床房", price_per_night=360, meal="含早",
                         cancelable=False, cancel_policy="不可取消",
                         bed_type="1 大床", rate_plan_id="RP_A2",
                         room_no=1, plan_no=2),
            RoomRatePlan(room_name="双床房", price_per_night=420, meal="含早",
                         cancelable=True, cancel_policy="免费取消",
                         bed_type="2 单人床", rate_plan_id="RP_B1",
                         room_no=2, plan_no=1),
        ],
    )


def _agent():
    """已选酒店、正在选房的会话。"""
    ag = agent_module.HotelBookingAgent()
    ag.state.params = BookingParams(city="成都", check_in="2026-12-01",
                                    check_out="2026-12-03")
    ag.state.selected_hotel = _hotel()
    ag.state.stage = "booking"
    ag.state.rooms_revision = "rev-old"
    return ag


def _draft(ag, plan_id="RP_A1", guest_name="陈二"):
    plan = next(p for p in ag.state.selected_hotel.rate_plans
                if p.rate_plan_id == plan_id)
    return {"rate_plan_obj": plan, "room_type": plan.room_name,
            "guest_name": guest_name}


def _ok_price_info():
    return {
        "reference_no": "REF-1", "total_price": 720, "per_night": 360,
        "currency": "CNY", "room_name": "大床房", "meal": "含早",
        "cancelable": False, "cancel_policy": "不可取消",
        "recent_guests": [{"name": "陈二", "email": "chener@example.com"}],
    }


# ---------- 1. 错误识别 ----------

def test_rate_unavailable_classifier():
    assert rlg.is_rate_unavailable_error(NO_ROOM_MSG) is True
    assert rlg.is_rate_unavailable_error(rlg.RollingGoApiError(NO_ROOM_MSG)) is True
    assert rlg.is_rate_unavailable_error("code=6201: no room available") is True
    assert rlg.is_rate_unavailable_error("道旅业务错误 code=6201：该房型已售罄") is True
    assert rlg.is_rate_unavailable_error("道旅业务错误 code：6201 房型不可售") is True
    assert rlg.is_rate_unavailable_error("该房型库存不足") is True

    # 登录失效 / 参数错误不能被当成售罄（否则会把房型列表误清空）
    assert rlg.is_rate_unavailable_error("道旅登录已失效（HTTP 401）") is False
    assert rlg.is_rate_unavailable_error("道旅业务错误 code=5000：内部错误") is False
    assert rlg.is_rate_unavailable_error("") is False
    assert rlg.is_rate_unavailable_error(None) is False


# ---------- 2. 验价 6201 → 回退选房 ----------

def test_drop_rate_plan_renumbers_contiguously():
    """整组房型售罄后重排序号，避免列表出现空号（用户回复空号无法解析）。"""
    hotel = _hotel()
    drop_rate_plan(hotel, "RP_A1")      # 大床房还剩一档
    drop_rate_plan(hotel, "RP_A2")      # 大床房整组消失

    groups = dict(plan_groups(hotel.rate_plans))
    assert list(groups) == [1], groups
    assert groups[1][0].rate_plan_id == "RP_B1"
    assert groups[1][0].plan_no == 1
    assert hotel.room_types == ["双床房"], hotel.room_types
    assert hotel.price_per_night == 420

    # 不存在的报价 ID：原样返回，不乱删
    before = len(hotel.rate_plans)
    assert drop_rate_plan(hotel, "RP_NOT_EXIST") is hotel
    assert len(hotel.rate_plans) == before


def test_price_confirm_6201_falls_back_to_room_selection():
    ag = _agent()
    old_rev = ag.state.rooms_revision
    draft = _draft(ag, "RP_A1")

    # 详情接口「刷新」后仍返回同一档失效报价：本地必须摘掉它
    with _patch(rlg, price_confirm=_raise(NO_ROOM_MSG)), \
            _patch(agent_module,
                   load_hotel_selection_detail=lambda h, p: h):
        reply = ag._do_real_price_confirm(draft)

    # 状态：回到选房阶段，验价上下文清空，入住人姓名记住
    assert ag.state.stage == "booking", ag.state.stage
    assert ag.state.booking_context == {}, ag.state.booking_context
    assert ag.state.pending_booking == {"guest_name": "陈二"}, ag.state.pending_booking
    # 房型列表换了一份 → 新版本号（旧卡片/旧序号作废）
    assert ag.state.rooms_revision not in ("", "rev-old"), ag.state.rooms_revision

    remaining = [p.rate_plan_id for p in ag.state.selected_hotel.rate_plans]
    assert "RP_A1" not in remaining, remaining
    assert remaining == ["RP_A2", "RP_B1"], remaining

    # 文案：说明报价变动 + 给出刷新后的可订房型 + 不再要求重报姓名
    assert "6201" in reply, reply
    assert "报价已变动或售罄" in reply, reply
    assert "陈二" in reply, reply
    assert "刷新后的可订房型：" in reply, reply
    assert "1. 大床房" in reply and "2. 双床房" in reply, reply
    assert "请告诉我：选择哪个房型（回复序号或房型名）？" in reply, reply

    # 结构化房型卡仍要下发（只暂存了姓名，用户还没选房）
    view = ag.rooms_view()
    assert view is not None, "回退选房时前端应能拿到最新房型卡"
    assert view["rooms_revision"] == ag.state.rooms_revision
    assert [r["room_no"] for r in view["rooms"]] == [1, 2], view["rooms"]
    assert view["rooms"][0]["plans"][0]["plan_key"] == "RP_A2"


# ---------- 3. 回退后只回复新序号即可重新验价 ----------

def test_after_fallback_user_only_picks_room_again():
    ag = _agent()
    with _patch(rlg, price_confirm=_raise(NO_ROOM_MSG)), \
            _patch(agent_module,
                   load_hotel_selection_detail=lambda h, p: h):
        ag._do_real_price_confirm(_draft(ag, "RP_A1"))

    calls = {}

    def fake_price_confirm(hotel_id, rate_plan_id, check_in, check_out, **kwargs):
        calls["rate_plan_id"] = rate_plan_id
        return _ok_price_info()

    with _patch(rlg, price_confirm=fake_price_confirm,
                is_logged_in=lambda: True):
        reply = ag.process_booking_confirmation("1")

    # 序号 1 指向刷新后的「大床房」唯一档 RP_A2，姓名沿用上一轮
    assert calls.get("rate_plan_id") == "RP_A2", calls
    assert ag.state.stage == "booking_confirm", ag.state.stage
    assert "验价成功" in reply, reply
    assert "陈二" in reply, reply
    assert "chener@example.com" in reply, reply


def test_structured_room_select_after_fallback():
    """Web 结构化选房（POST /room）在回退后同样沿用记住的入住人。"""
    ag = _agent()
    with _patch(rlg, price_confirm=_raise(NO_ROOM_MSG)), \
            _patch(agent_module,
                   load_hotel_selection_detail=lambda h, p: h):
        ag._do_real_price_confirm(_draft(ag, "RP_A1"))

    calls = {}

    def fake_price_confirm(hotel_id, rate_plan_id, check_in, check_out, **kwargs):
        calls["rate_plan_id"] = rate_plan_id
        return _ok_price_info()

    with _patch(rlg, price_confirm=fake_price_confirm,
                is_logged_in=lambda: True):
        reply = ag.select_room_by_numbers(2, 1)     # 刷新后的双床房

    assert calls.get("rate_plan_id") == "RP_B1", calls
    assert ag.state.stage == "booking_confirm", ag.state.stage
    assert "验价成功" in reply, reply
    assert "陈二" in reply, reply


# ---------- 4. 整店售罄 → 回酒店列表 ----------
def test_sold_out_hotel_returns_to_hotel_list():
    ag = _agent()
    ag.state.search_results = [ag.state.selected_hotel]
    ag.state.search_revision = "rev-hotels"
    sold_out = Hotel(hotel_id=HOTEL_ID, name="测试道旅酒店", city="成都",
                     address="测试路1号", star=4, price_per_night=0,
                     facilities=[], room_types=[], rating=4.5, review_count=10)
    draft = _draft(ag, "RP_A1")

    with _patch(rlg, price_confirm=_raise(NO_ROOM_MSG)), \
            _patch(agent_module,
                   load_hotel_selection_detail=lambda h, p: sold_out):
        reply = ag._do_real_price_confirm(draft)

    assert "没有可预订的房型" in reply, reply
    assert ag.state.stage == "recommend", ag.state.stage
    assert ag.state.selected_hotel is None
    assert ag.state.pending_booking == {}, ag.state.pending_booking
    # 退回的仍是原酒店列表（可直接换一家），而不是空白页
    assert ag.state.last_view.get("kind") == "hotels", ag.state.last_view
    assert "测试道旅酒店" in reply, reply


# ---------- 5. 下单 6201 同样回退选房 ----------

def test_book_step_6201_recovers_to_room_selection():
    ag = _agent()
    plan = ag.state.selected_hotel.rate_plans[-1]      # RP_B1
    ctx = {
        "phase": "price_confirmed", "rate_plan_obj": plan,
        "room_type": plan.room_name, "guest_name": "陈二",
        "names": rlg.split_cn_name("陈二"),
        "email_override": "chener@example.com", "chosen_recent": None,
        "price_info": {"reference_no": "REF-1", "recent_guests": []},
    }
    ag.state.booking_context = ctx
    ag.state.stage = "booking_confirm"

    with _patch(rlg, create_booking=_raise(NO_ROOM_MSG)), \
            _patch(agent_module,
                   load_hotel_selection_detail=lambda h, p: h):
        reply = ag._execute_real_booking(ctx)

    assert "下单未通过" in reply, reply
    assert "报价已变动或售罄" in reply, reply
    assert ag.state.stage == "booking", ag.state.stage
    assert ag.state.booking_context == {}, ag.state.booking_context
    # 姓名和已确认的联系邮箱都保留，选新房型时不用再填
    assert ag.state.pending_booking == {"guest_name": "陈二",
                                        "email_override": "chener@example.com"}
    remaining = [p.rate_plan_id for p in ag.state.selected_hotel.rate_plans]
    assert "RP_B1" not in remaining, remaining


# ---------- 6. 非售罄报错不误伤房型列表 ----------

def test_other_api_error_keeps_room_list():
    ag = _agent()
    old_rev = ag.state.rooms_revision
    draft = _draft(ag, "RP_A1")

    with _patch(rlg, price_confirm=_raise("道旅业务错误 code=5000：内部错误")):
        reply = ag._do_real_price_confirm(draft)

    assert "验价失败" in reply, reply
    assert "不是房型售罄" in reply, reply
    assert ag.state.rooms_revision == old_rev
    assert ag.state.pending_booking == {}, ag.state.pending_booking
    assert len(ag.state.selected_hotel.rate_plans) == 3


# ---------- 7. rooms_view 的下发时机 ----------

def test_rooms_view_depends_on_what_is_pending():
    ag = _agent()
    # 还没选房：下发
    assert ag.rooms_view() is not None
    # 只记了姓名（还没选房）：仍下发，否则用户没列表可选
    ag.state.pending_booking = {"guest_name": "陈二"}
    assert ag.rooms_view() is not None
    # 已选房型、等姓名：不再重复下发房型卡
    ag.state.pending_booking = {"room_type": "大床房", "rate_plan_id": "RP_A1"}
    assert ag.rooms_view() is None
    # 已进入验价确认：不下发
    ag.state.pending_booking = {}
    ag.state.booking_context = {"phase": "price_confirmed"}
    assert ag.rooms_view() is None


# ---------- 8. HTTP 层复现截图里的那一轮对话 ----------

def test_chat_endpoint_returns_fresh_rooms_after_6201():
    """复现截图：房型已暂存 + 用户回复姓名 → 6201 → 回复里必须带最新房型。"""
    sid = "unit-rate-change"
    ag = _agent()
    ag.state.pending_booking = {"room_type": "大床房", "rate_plan_id": "RP_A1"}
    web_module._agents[sid] = {"agent": ag, "ts": time.time()}
    client = TestClient(web_module.app)

    with _patch(rlg, price_confirm=_raise(NO_ROOM_MSG),
                is_logged_in=lambda: True), \
            _patch(agent_module,
                   load_hotel_selection_detail=lambda h, p: h):
        r = client.post("/api/chat", json={"user_input": "陈二",
                                           "session_id": sid})

    assert r.status_code == 200, r.text
    d = r.json()
    assert d["stage"] == "booking", d
    assert "报价已变动或售罄" in d["reply"], d["reply"]
    assert d["rooms_view"] is not None, "回退选房必须下发最新房型"
    view = d["rooms_view"]
    assert view["rooms_revision"] == ag.state.rooms_revision
    assert [room["room_no"] for room in view["rooms"]] == [1, 2], view["rooms"]
    assert "RP_A1" not in [plan["plan_key"]
                           for room in view["rooms"] for plan in room["plans"]]


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
