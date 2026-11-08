# -*- coding: utf-8 -*-
"""取消订单链路的纯单元测试（不联网、不产生真实订单）。

覆盖：
  - 未支付道旅订单（Booked + paymentUrl）应归入 PENDING，而不是 FINISHED
  - /hotelprecancel 的成功/不可取消/凭证缺失三种响应解析
  - /hotelcancelconfirm 的请求体
  - 本地状态只在远端确认成功后才改成 cancelled

运行：
    python tests/test_cancel_unit.py
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "hotel_booking_agent"))

import agent as agent_mod             # noqa: E402
import multi_agent as multi_mod       # noqa: E402
import rollinggo_book as rlg          # noqa: E402
import tools                          # noqa: E402
from models import AgentState, Order  # noqa: E402


def _order(status="pending"):
    return Order(
        order_id="H179000000000000001",
        hotel_id="",
        hotel_name="测试酒店",
        check_in="2026-11-06",
        check_out="2026-11-08",
        room_type="大床房",
        guest_name="",
        total_price=472.0,
        status=status,
        source="rollinggo",
        payment_url="https://rollinggo.cn/pages/order/token-pay/index?token=test",
    )


class OrderBucketTests(unittest.TestCase):
    def test_booked_with_payment_url_is_pending(self):
        row = {
            "status": "Booked",
            "status_text": "已确认",
            "payment_url": "https://rollinggo.cn/pay",
            "source": "rollinggo",
        }
        self.assertEqual(tools.order_status_bucket(row), "PENDING")

    def test_paid_with_payment_url_is_not_pending(self):
        row = {
            "status": "Paid",
            "status_text": "已支付",
            "payment_url": "https://rollinggo.cn/pay",
            "source": "rollinggo",
        }
        self.assertEqual(tools.order_status_bucket(row), "FINISHED")

    def test_cancelled_wins(self):
        row = {
            "status": "Cancelled",
            "status_text": "已取消",
            "payment_url": "",
            "source": "rollinggo",
        }
        self.assertEqual(tools.order_status_bucket(row), "CANCELLED")


class PreCancelParseTests(unittest.TestCase):
    def test_cancellable_response(self):
        raw = {
            "success": True,
            "canCancel": True,
            "confirmId": "abc123",
            "bookingId": "B" * 19,
            "penaltyAmount": 0,
            "message": "该订单尚未支付，可免费取消。",
            "orderNo": "H1",
        }
        with patch.object(rlg, "_request", return_value=raw) as req:
            quote = rlg.pre_cancel("H1")
        req.assert_called_once_with("/hotelprecancel", {"orderNo": "H1"},
                                    raise_on_error=False)
        self.assertTrue(quote["can_cancel"])
        self.assertEqual(quote["confirm_id"], "abc123")
        self.assertEqual(quote["booking_id"], "B" * 19)
        self.assertEqual(quote["penalty"], 0)

    def test_non_cancellable_keeps_support_message(self):
        raw = {
            "success": False,
            "canCancel": False,
            "message": "该订单按规则无法在线取消，如需取消请联系客服处理。",
        }
        with patch.object(rlg, "_request", return_value=raw):
            quote = rlg.pre_cancel("H2")
        self.assertFalse(quote["can_cancel"])
        self.assertIn("联系客服", quote["message"])

    def test_missing_voucher_is_not_cancellable(self):
        raw = {"success": True, "canCancel": True, "confirmId": "abc",
               "message": "ok"}
        with patch.object(rlg, "_request", return_value=raw):
            quote = rlg.pre_cancel("H3")
        self.assertFalse(quote["can_cancel"])

    def test_confirm_cancel_payload(self):
        with patch.object(rlg, "_request",
                          return_value={"success": True, "message": "取消成功"}) as req:
            result = rlg.confirm_cancel("H1", "B1", "C1", description="行程有变")
        req.assert_called_once_with(
            "/hotelcancelconfirm",
            {"orderNo": "H1", "bookingId": "B1", "confirmId": "C1",
             "description": "行程有变"},
            raise_on_error=False,
        )
        self.assertTrue(result["success"])


class ToolCancelTests(unittest.TestCase):
    def test_pre_cancel_real_order_quote(self):
        quote = {
            "can_cancel": True,
            "confirm_id": "C1",
            "booking_id": "B1",
            "penalty": 0,
            "message": "尚未支付，可免费取消。",
        }
        with patch.object(tools, "find_order", return_value=_order()), \
                patch.object(tools, "sync_real_order_status"), \
                patch.object(rlg, "is_logged_in", return_value=True), \
                patch.object(rlg, "pre_cancel", return_value=quote):
            result = tools.pre_cancel_real_order("H179000000000000001")

        self.assertTrue(result["ok"])
        self.assertEqual(result["confirm_id"], "C1")
        self.assertEqual(result["booking_id"], "B1")
        self.assertEqual(result["penalty"], 0)
        self.assertGreater(result["expires_at"], 0)

    def test_pre_cancel_rejects_already_cancelled(self):
        with patch.object(tools, "find_order", return_value=_order("cancelled")), \
                patch.object(tools, "sync_real_order_status"):
            result = tools.pre_cancel_real_order("H179000000000000001")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "already_cancelled")

    def test_confirm_success_updates_local_status(self):
        order = _order()
        with patch.object(rlg, "is_logged_in", return_value=True), \
                patch.object(rlg, "confirm_cancel",
                             return_value={"success": True, "message": "已取消"}), \
                patch.object(tools.storage, "update_status", return_value=True) as upd, \
                patch.object(tools.storage, "get_order", return_value=order), \
                patch.object(tools, "_invalidate_history_cache"):
            result = tools.confirm_cancel_real_order(
                order.order_id, "B1", "C1")
        self.assertTrue(result["ok"])
        upd.assert_called_once_with(order.order_id, "cancelled")
        self.assertEqual(order.status, "cancelled")

    def test_confirm_failure_does_not_update_local_status(self):
        with patch.object(rlg, "is_logged_in", return_value=True), \
                patch.object(rlg, "confirm_cancel",
                             return_value={"success": False, "message": "凭证过期"}), \
                patch.object(tools.storage, "update_status") as upd:
            result = tools.confirm_cancel_real_order(
                "H179000000000000001", "B1", "C1")
        self.assertFalse(result["ok"])
        upd.assert_not_called()


class AgentCancelFlowTests(unittest.TestCase):
    def test_quote_builds_confirmation_card(self):
        a = agent_mod.HotelBookingAgent()
        quote = {
            "ok": True,
            "order_id": "H179000000000000001",
            "hotel_name": "测试酒店",
            "check_in": "2026-11-06",
            "check_out": "2026-11-08",
            "total_price": 472.0,
            "penalty": 0,
            "confirm_id": "C1",
            "booking_id": "B1",
            "message": "该订单尚未支付，可免费取消。凭证有效期 10 分钟。",
            "expires_at": 9999999999,
        }
        with patch.object(agent_mod, "pre_cancel_real_order", return_value=quote):
            text = a._real_cancel_quote(_order())
        self.assertIn("确认取消", text)
        self.assertIn("尚未支付", text)
        self.assertEqual(a.state.pending_cancel["confirm_id"], "C1")

    def test_confirm_success_clears_pending(self):
        a = agent_mod.HotelBookingAgent()
        a.state.pending_cancel = {
            "order_id": "H179000000000000001",
            "confirm_id": "C1",
            "booking_id": "B1",
            "expires_at": 9999999999,
        }
        with patch.object(agent_mod, "confirm_cancel_real_order",
                          return_value={"ok": True, "message": "已取消"}):
            text = a._confirm_real_cancel()
        self.assertIn("已成功取消", text)
        self.assertEqual(a.state.pending_cancel, {})

    def test_confirm_without_prior_prompt_does_not_cancel(self):
        a = agent_mod.HotelBookingAgent()
        a.state.current_order = _order()  # 例如刚创建、尚未发起取消
        with patch.object(agent_mod, "cancel_order") as cancel:
            text = a._handle_cancel({}, "确认取消")
        cancel.assert_not_called()
        self.assertIn("没有待确认", text)

    def test_mock_confirmation_requires_pending(self):
        a = agent_mod.HotelBookingAgent()
        order = _order()
        order.source = "mock"
        a.state.pending_cancel = {"order_id": order.order_id, "source": "mock",
                                  "expires_at": 9999999999}
        with patch.object(agent_mod, "cancel_order", return_value="ok"):
            text = a._handle_cancel({}, "确认取消")
        self.assertIn("已成功取消", text)
        self.assertEqual(a.state.pending_cancel, {})

    def test_expired_quote_is_rejected(self):
        a = agent_mod.HotelBookingAgent()
        a.state.pending_cancel = {
            "order_id": "H1", "confirm_id": "C1",
            "booking_id": "B1", "expires_at": 1,
        }
        with patch.object(agent_mod, "confirm_cancel_real_order") as confirm:
            text = a._confirm_real_cancel()
        confirm.assert_not_called()
        self.assertIn("过期", text)


class MultiAgentCancelTests(unittest.TestCase):
    def _service(self):
        llm = agent_mod.HotelBookingAgent().llm
        return multi_mod.ServiceAgent(llm)

    def _dispatch(self, service, state, order, confirmed):
        msg = multi_mod.AgentMessage(
            sender="Supervisor", receiver="ServiceAgent",
            msg_type="task", content="cancel_aftersale",
            data={"order_id": order.order_id, "confirmed": confirmed},
        )
        service.receive(msg)
        return service.process(state)

    def test_mock_requires_then_confirms(self):
        order = _order()
        order.source = "mock"
        order.payment_url = ""
        state = AgentState()
        state.current_order = order
        service = self._service()

        with patch.object(multi_mod, "find_order", return_value=order), \
                patch.object(multi_mod, "sync_real_order_status"):
            first = self._dispatch(service, state, order, confirmed=False)
        self.assertEqual(first.content, "need_confirmation")
        self.assertEqual(state.pending_cancel.get("source"), "mock")

        with patch.object(multi_mod, "find_order", return_value=order), \
                patch.object(multi_mod, "sync_real_order_status"), \
                patch.object(multi_mod, "cancel_order", return_value="ok"):
            second = self._dispatch(service, state, order, confirmed=True)
        self.assertEqual(second.content, "cancelled")
        self.assertEqual(state.pending_cancel, {})

    def test_real_quote_returns_penalty_and_stores_voucher(self):
        order = _order()
        state = AgentState()
        service = self._service()
        quote = {
            "ok": True,
            "order_id": order.order_id,
            "hotel_name": order.hotel_name,
            "check_in": order.check_in,
            "check_out": order.check_out,
            "total_price": 472.0,
            "penalty": 0,
            "confirm_id": "C1",
            "booking_id": "B1",
            "message": "尚未支付，可免费取消。",
            "expires_at": 9999999999,
        }
        with patch.object(multi_mod, "find_order", return_value=order), \
                patch.object(multi_mod, "sync_real_order_status"), \
                patch.object(multi_mod, "pre_cancel_real_order",
                             return_value=quote):
            result = self._dispatch(service, state, order, confirmed=False)
        self.assertEqual(result.content, "need_confirmation")
        self.assertIn("尚未支付", result.data["text"])
        self.assertEqual(state.pending_cancel["confirm_id"], "C1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
