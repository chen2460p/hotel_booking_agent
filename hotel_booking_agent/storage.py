# -*- coding: utf-8 -*-
"""
订单存储抽象层 —— 自动探测真实数据库，未配则回退内存 dict。

支持两种后端：
  - SQLite（Python 标准库自带，零依赖，生产教学用）
      .env: DATABASE_URL=sqlite:///orders.db
  - MySQL（可选，需 pymysql）
      .env: DATABASE_URL=mysql+pymysql://user:pass@host:3306/dbname

未配置 DATABASE_URL 或连接失败时自动回退内存 dict（与 tools.py 原先 ORDER_DB 行为一致），
CLI 零配置可跑，Web 版有 DB 则持久化。
"""

import os
import re
import json
import threading
from typing import Dict, List, Optional, Tuple

from models import Order

# ---------- 懒加载 ----------
_mem_db: Dict[str, Order] = {}
_conn = None
_conn_lock = threading.Lock()
_backend = "memory"          # memory / sqlite / mysql
_initialized = False
_init_lock = threading.Lock()  # 保护 _init_once 的线程安全（Double-checked locking）


def _init_once() -> None:
    """进程内只探测一次数据库后端；线程安全。"""
    global _conn, _backend, _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        _initialized = True

        url = os.environ.get("DATABASE_URL", "").strip()
        if not url:
            _backend = "memory"
            return

        # SQLite: sqlite:///path/to/file.db  或 sqlite:////absolute/path.db
        sqlite_m = re.match(r"^sqlite:///(.*)$", url)
        if sqlite_m:
            path = sqlite_m.group(1)
            if path and not os.path.isabs(path):
                path = os.path.abspath(path)
            _backend = "sqlite"
            _try_sqlite(path)
            return

        # MySQL: mysql+pymysql://user:pass@host:3306/dbname
        mysql_m = re.match(
            r"^mysql\+pymysql://([^:/]+):([^@/]+)@([^:/]+)(?::(\d+))?/(.+)$", url)
        if mysql_m:
            _backend = "mysql"
            _try_mysql(mysql_m.groups())
            return

    # 无法识别的 DATABASE_URL → 回退
    _backend = "memory"


def storage_backend() -> str:
    """对外只读：当前实际使用的后端。CLI 可用于 banner 提示。"""
    _init_once()
    return _backend


def storage_available() -> bool:
    """True = 有真实持久化后端；False = 回退内存 dict。"""
    _init_once()
    return _backend != "memory"


# ---------- SQLite ----------
def _try_sqlite(path: str) -> None:
    global _conn, _backend
    try:
        import sqlite3
    except ImportError:
        _backend = "memory"
        return
    try:
        if path:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        _conn = sqlite3.connect(path or ":memory:",
                                check_same_thread=False,
                                detect_types=sqlite3.PARSE_DECLTYPES,
                                isolation_level=None)  # 开启 autocommit
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")     # 写并发友好
        _ensure_schema_sqlite(_conn)
    except Exception as e:
        print(f"[storage] SQLite 初始化失败，回退内存：{e}")
        _conn = None
        _backend = "memory"


def _ensure_schema_sqlite(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id      TEXT PRIMARY KEY,
            hotel_id      TEXT NOT NULL,
            hotel_name    TEXT NOT NULL,
            check_in      TEXT NOT NULL,
            check_out     TEXT NOT NULL,
            room_type     TEXT NOT NULL,
            guest_name    TEXT NOT NULL,
            total_price   REAL NOT NULL,
            status        TEXT NOT NULL DEFAULT 'pending',
            source        TEXT NOT NULL DEFAULT 'mock',
            payment_url   TEXT NOT NULL DEFAULT '',
            contact_email TEXT NOT NULL DEFAULT '',
            created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()


# ---------- MySQL ----------
def _try_mysql(groups: Tuple) -> None:
    global _conn, _backend
    try:
        import pymysql
    except ImportError:
        print("[storage] 未安装 pymysql，MySQL 不可用，回退内存 dict")
        _backend = "memory"
        return
    user, pwd, host, port, db = groups
    port = int(port or 3306)
    try:
        _conn = pymysql.connect(host=host, port=port, user=user, password=pwd,
                                database=db, charset="utf8mb4",
                                cursorclass=pymysql.cursors.DictCursor,
                                autocommit=True)
        _ensure_schema_mysql(_conn)
    except Exception as e:
        print(f"[storage] MySQL 初始化失败，回退内存：{e}")
        _conn = None
        _backend = "memory"


def _ensure_schema_mysql(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id      VARCHAR(32) PRIMARY KEY,
            hotel_id      VARCHAR(32) NOT NULL,
            hotel_name    VARCHAR(255) NOT NULL,
            check_in      DATE NOT NULL,
            check_out     DATE NOT NULL,
            room_type     VARCHAR(120) NOT NULL,
            guest_name    VARCHAR(40) NOT NULL,
            total_price   DECIMAL(10,2) NOT NULL,
            status        VARCHAR(16) NOT NULL DEFAULT 'pending',
            source        VARCHAR(16) NOT NULL DEFAULT 'mock',
            payment_url   TEXT NOT NULL,
            contact_email VARCHAR(120) NOT NULL DEFAULT '',
            created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)


# ---------- 统一数据访问接口 ----------
def _row_to_order(row) -> Order:
    if isinstance(row, dict):
        return Order(
            order_id=row["order_id"], hotel_id=row["hotel_id"],
            hotel_name=row["hotel_name"], check_in=row["check_in"],
            check_out=row["check_out"], room_type=row["room_type"],
            guest_name=row["guest_name"],
            total_price=float(row["total_price"] or 0),
            status=row["status"], source=row["source"],
            payment_url=row.get("payment_url", "") or "",
            contact_email=row.get("contact_email", "") or "",
        )
    # sqlite3.Row 带 keys()
    d = dict(row)
    return _row_to_order(d)


def insert_order(order: Order) -> None:
    _init_once()
    if _backend == "memory":
        _mem_db[order.order_id] = order
        return
    try:
        with _conn_lock:
            _conn.execute(
                """INSERT OR REPLACE INTO orders
                   (order_id, hotel_id, hotel_name, check_in, check_out,
                    room_type, guest_name, total_price, status, source,
                    payment_url, contact_email)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (order.order_id, order.hotel_id, order.hotel_name,
                 order.check_in, order.check_out, order.room_type,
                 order.guest_name, order.total_price, order.status,
                 order.source, order.payment_url, order.contact_email),
            )
    except Exception as e:
        print(f"[storage] insert_order 失败（回退内存）：{e}")
        _mem_db[order.order_id] = order


def get_order(order_id: str) -> Optional[Order]:
    _init_once()
    if _backend == "memory":
        return _mem_db.get(order_id)
    try:
        with _conn_lock:
            cur = _conn.execute(
                "SELECT * FROM orders WHERE order_id = ?", (order_id,))
            row = cur.fetchone()
        return _row_to_order(row) if row else None
    except Exception as e:
        print(f"[storage] get_order 失败：{e}")
        return _mem_db.get(order_id)


def update_status(order_id: str, status: str) -> bool:
    """更新订单状态，返回是否更新成功。"""
    _init_once()
    if _backend == "memory":
        if order_id not in _mem_db:
            return False
        _mem_db[order_id].status = status
        return True
    try:
        with _conn_lock:
            cur = _conn.execute(
                "UPDATE orders SET status = ? WHERE order_id = ?",
                (status, order_id),
            )
            affected = getattr(cur, "rowcount", cur.rowcount) if hasattr(cur, "rowcount") else 0
        # sqlite3 返回 sqlite3.Cursor，pymysql 返回 pymysql.cursors.DictCursor
        try:
            affected = cur.rowcount
        except Exception:
            affected = 0
        # 更新内存副本，供同进程后续 get_order 立刻读到
        if order_id in _mem_db:
            _mem_db[order_id].status = status
        return affected > 0
    except Exception as e:
        print(f"[storage] update_status 失败：{e}")
        if order_id in _mem_db:
            _mem_db[order_id].status = status
            return True
        return False


def list_all_orders() -> List[Order]:
    """返回全部订单（不限状态，按创建时间倒序）。"""
    _init_once()
    if _backend == "memory":
        return list(_mem_db.values())
    try:
        with _conn_lock:
            cur = _conn.execute(
                "SELECT * FROM orders ORDER BY created_at DESC")
            rows = cur.fetchall()
        return [_row_to_order(r) for r in rows]
    except Exception as e:
        print(f"[storage] list_all_orders 失败：{e}")
        return list(_mem_db.values())


# ---------- CLI 运行期的同步钩子 ----------
# CLI 进程用内存 dict 起步；如果同时也有 DB（比如用户中途配了 DATABASE_URL），
# 我们不想让工具层同时维护两份状态。这里暴露一个 hook，让 storage 可以接管内存里
# 已有的初始化数据（由 tools.py 启动时调用一次）。

def bootstrap_from_memory(mem_db: Dict[str, Order]) -> None:
    """把 CLI 启动前内存 dict 里已有的订单写入 DB（DB 模式下）。"""
    _init_once()
    if _backend == "memory":
        _mem_db.update(mem_db)
        return
    # 逐条 upsert
    for o in mem_db.values():
        insert_order(o)
