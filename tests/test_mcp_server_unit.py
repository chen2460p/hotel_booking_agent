# -*- coding: utf-8 -*-
"""MCP Server 单测：协议握手、工具契约、只读边界、鉴权与审计。

全部离线：不访问道旅/高德/模型，工具层一律 mock。

运行：
    python tests/test_mcp_server_unit.py
    pytest tests/test_mcp_server_unit.py -v
"""

import json
import os
import sys
import tempfile
from unittest.mock import patch

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(PROJ, "hotel_booking_agent")
sys.path.insert(0, APP)
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

# 审计日志写到临时目录，避免污染仓库 cache/
_AUDIT = os.path.join(tempfile.gettempdir(), "mcp_audit_test.jsonl")
os.environ["MCP_AUDIT_FILE"] = _AUDIT
if os.path.exists(_AUDIT):
    os.remove(_AUDIT)

from fastapi.testclient import TestClient   # noqa: E402

import mcp_server                            # noqa: E402
import tools                                 # noqa: E402
import web                                   # noqa: E402
from models import Hotel, Order              # noqa: E402

client = TestClient(web.app)


def _hotel():
    return Hotel(
        hotel_id="RLG_1", name="测试酒店", city="成都",
        address="春熙路 1 号", star=4, price_per_night=321.5,
        facilities=["WiFi", "泳池"], room_types=["大床房"],
        rating=4.7, review_count=88, rating_source="百度地图",
    )


def _order():
    return Order(
        order_id="H123", hotel_id="RLG_1", hotel_name="测试酒店",
        check_in="2026-12-01", check_out="2026-12-02", room_type="大床房",
        guest_name="张三", total_price=321.5, status="pending",
        source="rollinggo", contact_email="zhangsan@example.com",
        created_at="2026-11-19 10:00:00",
    )


def _post(body):
    return client.post("/mcp", json=body)


def _call(name, arguments):
    resp = _post({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": name, "arguments": arguments}})
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


def _payload(result):
    return json.loads(result["content"][0]["text"])


# ---------- 协议 ----------


def test_initialize_handshake():
    resp = _post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-03-26",
                             "capabilities": {}, "clientInfo": {"name": "t"}}})
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["serverInfo"]["name"] == mcp_server.SERVER_NAME
    assert "tools" in result["capabilities"]
    assert "只读" in result["instructions"]


def test_initialize_falls_back_on_unknown_version():
    resp = _post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "1999-01-01"}})
    assert resp.json()["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSION


def test_notification_returns_202_without_body():
    resp = _post({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert resp.status_code == 202
    assert resp.content == b""


def test_unknown_method_is_32601():
    resp = _post({"jsonrpc": "2.0", "id": 9, "method": "nope"})
    assert resp.json()["error"]["code"] == -32601


def test_parse_error_returns_400():
    resp = client.post("/mcp", content=b"{not json")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32700


# ---------- 工具契约 ----------


def test_tools_list_exposes_only_readonly_tools():
    result = _post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]
    names = {t["name"] for t in result["tools"]}
    assert names == {"search_hotels", "get_hotel_detail",
                     "search_reviews", "get_order"}
    for tool in result["tools"]:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["inputSchema"]["properties"], tool["name"]
        assert tool["readOnlyHint"] is True


def test_write_tools_are_not_exposed():
    result = _post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]
    names = {t["name"] for t in result["tools"]}
    for withheld in mcp_server.WITHHELD_TOOLS:
        assert withheld not in names


def test_search_hotels_ok():
    with patch.object(tools, "search_hotels", return_value=[_hotel()]):
        result = _call("search_hotels", {"city": "成都", "check_in": "2026-12-01",
                                         "check_out": "2026-12-02"})
    assert result["isError"] is False
    data = _payload(result)
    assert data["count"] == 1
    assert data["hotels"][0]["hotel_id"] == "RLG_1"
    assert data["hotels"][0]["price_per_night"] == 321.5


def test_missing_required_field_is_tool_error():
    result = _call("search_hotels", {"check_in": "2026-12-01",
                                     "check_out": "2026-12-02"})
    assert result["isError"] is True
    assert "缺少必填参数 city" in json.dumps(_payload(result), ensure_ascii=False)


def test_unknown_argument_is_rejected():
    with patch.object(tools, "search_hotels", return_value=[_hotel()]):
        result = _call("search_hotels", {"city": "成都", "check_in": "2026-12-01",
                                         "check_out": "2026-12-02",
                                         "hotel_id": "RLG_1"})
    assert result["isError"] is True
    assert "未知参数 hotel_id" in json.dumps(_payload(result), ensure_ascii=False)


def test_bad_date_order_is_tool_error():
    result = _call("search_hotels", {"city": "成都", "check_in": "2026-12-02",
                                     "check_out": "2026-12-01"})
    assert result["isError"] is True


def test_bad_date_format_is_rejected():
    result = _call("search_hotels", {"city": "成都", "check_in": "12/01",
                                     "check_out": "2026-12-02"})
    assert result["isError"] is True
    assert "YYYY-MM-DD" in json.dumps(_payload(result), ensure_ascii=False)


def test_numeric_string_is_coerced():
    with patch.object(tools, "search_hotels", return_value=[_hotel()]):
        result = _call("search_hotels", {"city": "成都", "check_in": "2026-12-01",
                                         "check_out": "2026-12-02",
                                         "max_price": "500"})
    assert result["isError"] is False


def test_unknown_tool_is_32602():
    resp = _post({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "create_booking", "arguments": {}}})
    assert resp.json()["error"]["code"] == -32602


def test_tool_internal_error_is_masked():
    with patch.object(tools, "search_hotels", side_effect=RuntimeError("上游 500")):
        result = _call("search_hotels", {"city": "成都", "check_in": "2026-12-01",
                                         "check_out": "2026-12-02"})
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "内部查询失败" in text
    # 原始异常信息不落到模型侧
    assert "上游 500" not in text


# ---------- 只读与脱敏 ----------


def test_get_order_masks_guest_pii():
    with patch.object(tools, "find_order", return_value=_order()):
        result = _call("get_order", {"order_id": "H123"})
    text = result["content"][0]["text"]
    assert result["isError"] is False
    assert "H123" in text
    for pii in ("张三", "zhangsan@example.com", "13800"):
        assert pii not in text, pii


def test_get_order_not_found_is_not_error():
    with patch.object(tools, "find_order", return_value=None):
        result = _call("get_order", {"order_id": "NOPE"})
    assert result["isError"] is False
    assert _payload(result)["found"] is False


# ---------- 资源 ----------


def test_resources_list_and_read():
    listed = _post({"jsonrpc": "2.0", "id": 1, "method": "resources/list"}).json()
    uris = {r["uri"] for r in listed["result"]["resources"]}
    assert mcp_server.POLICY_URI in uris

    read = _post({"jsonrpc": "2.0", "id": 2, "method": "resources/read",
                  "params": {"uri": mcp_server.POLICY_URI}}).json()
    text = read["result"]["contents"][0]["text"]
    assert "不代付" in text
    assert "两步" in text


# ---------- 鉴权 ----------


def test_auth_required_when_token_configured():
    os.environ["MCP_AUTH_TOKEN"] = "s3cret"
    try:
        assert _post({"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
        ok = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                         headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200
        bad = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                          headers={"Authorization": "Bearer wrong"})
        assert bad.status_code == 401
    finally:
        os.environ.pop("MCP_AUTH_TOKEN", None)


def test_no_auth_when_token_absent():
    os.environ.pop("MCP_AUTH_TOKEN", None)
    assert _post({"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 200


# ---------- 审计 ----------


def test_audit_records_tool_without_pii():
    with patch.object(tools, "find_order", return_value=_order()):
        _call("get_order", {"order_id": "H123"})
    assert os.path.exists(_AUDIT), "审计文件没写出来"
    with open(_AUDIT, encoding="utf-8") as fh:
        lines = [json.loads(ln) for ln in fh if ln.strip()]
    record = [ln for ln in lines if ln["tool"] == "get_order"][-1]
    assert record["ok"] is True
    assert record["arg_keys"] == ["order_id"]
    assert "H123" not in json.dumps(record, ensure_ascii=False)
    assert "张三" not in json.dumps(record, ensure_ascii=False)


# ---------- 批量 ----------


def test_batch_requests():
    resp = _post([
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ])
    assert resp.status_code == 200
    assert [r["id"] for r in resp.json()] == [1, 2]


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
