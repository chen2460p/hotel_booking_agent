# -*- coding: utf-8 -*-
"""/api/health 的 active_sessions 计数测试。

背景：过期会话只在新建/读取会话时回收，长时间无人聊天时旧对象会一直留在
池子里，直接 len() 会让“活跃会话数”虚高（11 号测试留下的 4 个对象一直显示
到 14 号）。session_count() 必须先清过期再计数。

运行：
    python tests/test_session_count_unit.py
    pytest tests/test_session_count_unit.py -v
"""

import os
import sys
import time

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(PROJ, "hotel_booking_agent")
sys.path.insert(0, APP)
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

import web  # noqa: E402


def _reset():
    with web._agents_lock:
        web._agents.clear()


def test_expired_sessions_are_purged_before_counting():
    _reset()
    now = time.time()
    web._agents["expired"] = {"agent": None, "ts": now - web._SESSION_TTL - 5}
    web._agents["fresh"] = {"agent": None, "ts": now - 10}

    assert web.session_count() == 1, "过期会话不应计入 active_sessions"
    assert "expired" not in web._agents, "过期会话应被顺手回收"
    assert "fresh" in web._agents, "未过期会话不能被误删"


def test_all_expired_returns_zero():
    _reset()
    old = time.time() - web._SESSION_TTL - 1
    web._agents["a"] = {"agent": None, "ts": old}
    web._agents["b"] = {"agent": None, "ts": old}
    assert web.session_count() == 0


def test_empty_pool():
    _reset()
    assert web.session_count() == 0


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
