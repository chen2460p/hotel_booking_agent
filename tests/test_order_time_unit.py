# -*- coding: utf-8 -*-
"""订单时间字段测试：下单时间/支付时间/最近动态的存取与道旅字段映射。

运行：
    python tests/test_order_time_unit.py
    pytest tests/test_order_time_unit.py -v
"""

import importlib
import os
import shutil
import sys

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(PROJ, "hotel_booking_agent")
sys.path.insert(0, APP)
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from models import Order                     # noqa: E402
import rollinggo_book as rlg                 # noqa: E402

# 测试库放在工作区内；不用 tempfile.mkdtemp：沙箱下其新建目录不可写
_TMP = os.path.join(PROJ, f"hb_order_time_tmp_{os.getpid()}")
os.makedirs(_TMP, exist_ok=True)
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMP, "orders.db")

import storage                               # noqa: E402
storage = importlib.reload(storage)          # 确保用测试库而不是真实 orders.db


def _make_order(order_id="ORDT0001"):
    return Order(
        order_id=order_id, hotel_id="H001", hotel_name="测试酒店",
        check_in="2026-12-01", check_out="2026-12-02", room_type="大床房",
        guest_name="张三", total_price=300.0,
    )


def test_parse_orders_reads_order_date():
    """道旅列表用 orderDate 表示下单时间，必须映射到 create_time。"""
    raw = {"success": True, "orderList": [{
        "orderNo": "H179436156951465461",
        "hotelName": "港汇四季酒店公寓",
        "orderStatus": "Canceled",
        "orderDate": "2026-11-11 09:46:09",
        "checkInDate": "2026-11-12",
        "checkOutDate": "2026-11-15",
        "totalPrice": 427.0,
    }]}
    rows = rlg.parse_orders(raw)
    assert rows and rows[0]["create_time"] == "2026-11-11 09:46:09", rows


def test_insert_sets_created_at_and_never_overwrites():
    assert storage.storage_backend() == "sqlite", "测试应跑在临时 SQLite 上"
    storage.insert_order(_make_order())
    got = storage.get_order("ORDT0001")
    assert got is not None
    assert got.created_at, "插入时必须落 created_at"
    assert got.created_at == got.updated_at
    assert got.paid_at == ""

    first_created = got.created_at
    storage.update_status("ORDT0001", "paid")
    paid = storage.get_order("ORDT0001")
    assert paid.paid_at, "标记支付要写 paid_at"
    assert paid.created_at == first_created, "支付不能覆盖下单时间"
    assert paid.status == "paid"

    first_paid = paid.paid_at
    storage.update_status("ORDT0001", "paid")
    again = storage.get_order("ORDT0001")
    assert again.paid_at == first_paid, "重复置 paid 不应刷新首次支付时间"


def test_local_row_maps_all_times():
    storage.insert_order(_make_order("ORDT0002"))
    storage.update_status("ORDT0002", "paid")
    order = storage.get_order("ORDT0002")
    row = storage_local_row(order)
    assert row["create_time"] == order.created_at
    assert row["paid_time"] == order.paid_at
    assert row["updated_at"] == order.updated_at


def storage_local_row(order):
    # tools 依赖较重，这里直接调用其内部映射函数
    import tools
    return tools._local_order_to_row(order)


def test_sort_key_orders_by_create_time_desc():
    import tools
    rows = [
        {"create_time": "2026-11-01 10:00:00", "check_in": "2026-11-10"},
        {"create_time": "", "check_in": "2026-12-01"},
        {"create_time": "2026-11-11 09:46:09", "check_in": "2026-11-12"},
    ]
    rows.sort(key=tools._row_time_key, reverse=True)
    assert rows[0]["create_time"] == "2026-11-11 09:46:09", rows
    assert rows[-1]["create_time"] == "", rows


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
    shutil.rmtree(_TMP, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
