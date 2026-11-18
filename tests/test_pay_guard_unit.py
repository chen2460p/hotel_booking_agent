# -*- coding: utf-8 -*-
"""支付守卫测试：真实道旅订单不允许被本地标记为已支付。

背景：`POST /api/orders/{id}/pay` 原本不区分订单来源，对真实单调用会写
`paid_at`，造成“已支付”的假象并破坏订单审计。这里锁住守卫行为。

运行：
    python tests/test_pay_guard_unit.py
"""

import os
import sys
from unittest.mock import patch

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(PROJ, "hotel_booking_agent")
sys.path.insert(0, APP)
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from fastapi.testclient import TestClient   # noqa: E402

import tools                               # noqa: E402
import web                                 # noqa: E402
from models import Order                    # noqa: E402


def _order(source: str) -> Order:
    return Order(order_id="H1" if source == "rollinggo" else "ORD1",
                 hotel_id="RLG_1", hotel_name="测试酒店",
                 check_in="2026-12-01", check_out="2026-12-02",
                 room_type="大床房", guest_name="陈晨",
                 total_price=300.0, source=source)


def test_real_order_cannot_be_marked_paid():
    with patch.object(tools.storage, "get_order", return_value=_order("rollinggo")), \
            patch.object(tools.storage, "update_status", return_value=True) as update:
        assert tools.pay_order("H1") is False
        update.assert_not_called()


def test_mock_order_can_be_marked_paid():
    with patch.object(tools.storage, "get_order", return_value=_order("mock")), \
            patch.object(tools.storage, "update_status", return_value=True) as update:
        assert tools.pay_order("ORD1") is True
        update.assert_called_once_with("ORD1", "paid")


def test_missing_order_returns_false():
    with patch.object(tools.storage, "get_order", return_value=None):
        assert tools.pay_order("NOPE") is False


def test_pay_endpoint_rejects_real_order():
    with patch.object(web.storage, "get_order", return_value=_order("rollinggo")):
        client = TestClient(web.app)
        resp = client.post("/api/orders/H1/pay")
        assert resp.status_code == 409, resp.text
        assert "真实订单" in resp.json().get("detail", "")


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
