# -*- coding: utf-8 -*-
"""接口冒烟测试：对「已经部署好」的实例做黑盒验证。

设计原则：
  1. 只依赖标准库 —— 现场不用装包，`python tests/test_api_smoke.py` 直接跑。
  2. 同时 pytest 兼容 —— 装了 pytest 也能 `pytest tests/ -v`。
  3. **确定性优先**：默认只跑不依赖模型/外部接口的用例，1~2 秒出结果、可重复；
     依赖真实 LLM 与供应链的用例用 `RUN_LLM_TESTS=1` 单独开启，避免"模型慢/挂"污染回归结果。
  4. 断言分层：状态码、协议头、字段结构这类确定性断言在测试里；
     自然语言回复只断言"非空 + 结构正确"，绝不逐字比对（LLM 输出非确定）。
  5. 无副作用：不调用下单接口，不产生真实订单。

用法：
    python tests/test_api_smoke.py                      # 确定性用例（默认）
    RUN_LLM_TESTS=1 python tests/test_api_smoke.py      # 加上需要真实模型的用例
    python tests/test_api_smoke.py --base http://127.0.0.1:8001
    pytest tests/test_api_smoke.py -v
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

try:                                    # 有 pytest 就用它的 skip 标记，没有也能跑
    import pytest

    def _skipif(cond, reason=""):
        return pytest.mark.skipif(cond, reason=reason)
except ImportError:                     # pragma: no cover
    def _skipif(cond, reason=""):
        def deco(fn):
            return fn
        return deco


BASE = os.getenv("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
TIMEOUT = 120
LLM_TESTS = os.getenv("RUN_LLM_TESTS", "").strip().lower() not in ("", "0", "false", "no")


def needs_llm(fn):
    """标记：该用例会触发真实 LLM / 外部供应链，默认不跑。"""
    fn._needs_llm = True
    return _skipif(not LLM_TESTS, reason="需要真实 LLM/外部接口，设 RUN_LLM_TESTS=1 开启")(fn)


class HTTPResult:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = {k.lower(): v for k, v in headers.items()}   # HTTP 头大小写不敏感
        self.body = body

    def header(self, name):
        return self.headers.get(name.lower(), "")

    @property
    def text(self):
        return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.body.decode("utf-8"))


def http(path, payload=None, timeout=TIMEOUT, method=None):
    """发一个请求；4xx/5xx 也正常返回，由断言自己判状态码。"""
    url = BASE + path
    data = None
    headers = {"Accept": "*/*"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return HTTPResult(r.status, dict(r.headers), r.read())
    except urllib.error.HTTPError as e:
        return HTTPResult(e.code, dict(e.headers or {}), e.read())


# --------------------------------------------------------------------------
# 确定性用例：健康检查 / 页面 / 接口契约 / 入参边界
# --------------------------------------------------------------------------

def test_health_endpoint():
    r = http("/api/health")
    assert r.status == 200, f"/api/health 期望 200，实际 {r.status}"
    d = r.json()
    assert d.get("status") == "ok", d
    assert d.get("storage_backend") in {"sqlite", "mysql", "memory"}, d
    assert isinstance(d.get("active_sessions"), int), d
    assert isinstance(d.get("rollinggo_logged_in"), bool), d


def test_index_page_renders():
    r = http("/")
    assert r.status == 200, f"首页期望 200，实际 {r.status}"
    assert "text/html" in r.header("Content-Type"), r.headers
    assert r.header("Cache-Control") == "no-cache", "页面应禁用缓存，否则可能拿到旧模板"
    body = r.text
    assert "酒店" in body, "首页内容缺少中文文案，模板可能没加载"
    assert len(body) > 3000, f"首页只有 {len(body)} 字节，疑似模板渲染异常"


def test_orders_page_renders():
    r = http("/orders")
    assert r.status == 200, f"/orders 期望 200，实际 {r.status}"
    assert "text/html" in r.header("Content-Type")


def test_openapi_contract():
    """接口契约：这些路由必须存在，防止改代码时误删。"""
    r = http("/openapi.json")
    assert r.status == 200
    paths = set(r.json().get("paths", {}))
    expected = {"/api/health", "/api/chat", "/api/chat/stream", "/api/orders",
                "/api/orders/{order_id}/cancel",
                "/api/orders/{order_id}/pre-cancel", "/orders", "/"}
    missing = expected - paths
    assert not missing, f"OpenAPI 缺少路由: {missing}"


def test_chat_rejects_empty_input():
    r = http("/api/chat", payload={"user_input": "   ", "session_id": "smoke-empty"})
    assert r.status == 400, f"空白输入期望 400，实际 {r.status}: {r.text[:200]}"
    assert "user_input" in r.text, r.text[:200]


def test_chat_rejects_missing_field():
    r = http("/api/chat", payload={})
    assert r.status == 422, f"缺字段期望 422（pydantic 校验），实际 {r.status}"


def test_orders_default_filter_returns_schema():
    r = http("/api/orders")
    assert r.status == 200
    d = r.json()
    for key in ("rows", "logged_in", "filter", "source"):
        assert key in d, f"/api/orders 缺少字段 {key}: {list(d)}"
    assert isinstance(d["rows"], list)


# --------------------------------------------------------------------------
# 依赖真实模型/供应链的用例（默认跳过）
# --------------------------------------------------------------------------

@needs_llm
def test_chat_returns_structured_reply():
    sid = f"smoke-{int(time.time())}"
    r = http("/api/chat", payload={"user_input": "你好", "session_id": sid})
    assert r.status == 200, f"期望 200，实际 {r.status}: {r.text[:200]}"
    d = r.json()
    assert isinstance(d.get("reply"), str) and d["reply"].strip(), d
    assert d.get("session_id") == sid, "session_id 应回显，前端靠它维持多轮状态"
    assert d.get("storage_backend") in {"sqlite", "mysql", "memory"}
    assert isinstance(d.get("hotel_total"), int)


@needs_llm
def test_session_state_is_isolated():
    """两个会话各自查询，互不污染——会话池最容易写错的地方。"""
    stamp = int(time.time())
    a, b = f"smoke-a-{stamp}", f"smoke-b-{stamp}"
    ra = http("/api/chat", payload={"user_input": "查一下我待支付的订单", "session_id": a})
    rb = http("/api/chat", payload={"user_input": "查一下我待支付的订单", "session_id": b})
    assert ra.status == 200 and rb.status == 200
    assert ra.json().get("session_id") == a
    assert rb.json().get("session_id") == b


@needs_llm
def test_sse_stream_frames():
    r = http("/api/chat/stream",
             payload={"user_input": "你好", "session_id": f"smoke-sse-{int(time.time())}"})
    assert r.status == 200, f"SSE 期望 200，实际 {r.status}"
    assert r.header("Content-Type").startswith("text/event-stream"), \
        f"Content-Type 应为 SSE，实际 {r.header('Content-Type')}"
    body = r.text
    assert "event:" in body, f"响应里没有 SSE 事件帧：{body[:200]}"
    assert "data:" in body


# --------------------------------------------------------------------------
# 零依赖运行器（没装 pytest 也能跑，现场首选）
# --------------------------------------------------------------------------

def main(argv):
    global BASE
    if "--base" in argv:
        BASE = argv[argv.index("--base") + 1].rstrip("/")
    print(f"BASE_URL = {BASE}    RUN_LLM_TESTS = {LLM_TESTS}\n")

    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]

    passed = failed = skipped = 0
    failures = []
    for name, fn in tests:
        if getattr(fn, "_needs_llm", False) and not LLM_TESTS:
            skipped += 1
            print(f"  SKIP  {name:<42} (需 RUN_LLM_TESTS=1)")
            continue
        t0 = time.time()
        try:
            fn()
            passed += 1
            print(f"  PASS  {name:<42} {time.time() - t0:5.2f}s")
        except AssertionError as e:
            failed += 1
            failures.append((name, str(e)))
            print(f"  FAIL  {name:<42} {time.time() - t0:5.2f}s\n        -> {e}")
        except Exception as e:
            failed += 1
            failures.append((name, f"{type(e).__name__}: {e}"))
            print(f"  ERROR {name:<42} {time.time() - t0:5.2f}s\n        -> {type(e).__name__}: {e}")

    print(f"\n{passed} passed, {failed} failed, {skipped} skipped, {len(tests)} total")
    for name, msg in failures:
        print(f"  ✗ {name}: {msg}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
