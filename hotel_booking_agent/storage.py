# -*- coding: utf-8 -*-
"""
订单存储抽象层 —— 自动探测真实数据库，未配则回退内存 dict。

支持两种后端：
  - SQLite（Python 标准库自带，零依赖，适合单机部署）
      .env: DATABASE_URL=sqlite:///orders.db
  - MySQL（可选，需 pymysql）
      .env: DATABASE_URL=mysql+pymysql://user:pass@host:3306/dbname

未配置 DATABASE_URL 或连接失败时自动回退内存 dict（与 tools.py 原先 ORDER_DB 行为一致），
CLI 零配置可跑，Web 版有 DB 则持久化。
"""

import os
import re
import threading
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from models import Order

# 相对 SQLite 路径统一按项目目录解析，避免 Web（cwd=hotel_booking_agent）和
# CLI（cwd=项目根目录）各写一个 orders.db。
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------- 懒加载 ----------
_mem_db: Dict[str, Order] = {}
_conn = None
_conn_lock = threading.Lock()
_backend = "memory"          # memory / sqlite / mysql
_initialized = False
_init_lock = threading.Lock()  # 保护 _init_once 的线程安全（Double-checked locking）
# 表里是否有 paid_at / updated_at（老库/只读库迁移失败时为 False）：
# 没有就退回旧 SQL，避免整个存储层降级到内存、丢掉已有本地订单
_HAS_TIME_COLS = True


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
                path = os.path.join(_BASE_DIR, path)
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
            created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
            paid_at       DATETIME,
            updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    _migrate_sqlite_columns(conn)
    conn.commit()


def _check_time_cols_sqlite(conn) -> None:
    global _HAS_TIME_COLS
    cols = {r[1] for r in conn.execute("PRAGMA table_info(orders)").fetchall()}
    _HAS_TIME_COLS = {"paid_at", "updated_at"} <= cols


def _migrate_sqlite_columns(conn) -> None:
    """老库补列：created_at 建表就有，paid_at/updated_at 是后加的。

    SQLite 的 ALTER TABLE ADD COLUMN 不允许 CURRENT_TIMESTAMP 默认值，
    所以新列先加空列，再用 created_at 回填 updated_at。

    迁移失败（如数据库只读）不能把整个存储层拖回内存：只记警告，
    置 _HAS_TIME_COLS=False，读写退回旧表结构，已有订单仍可查。
    """
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(orders)").fetchall()}
        first_migration = "updated_at" not in cols
        if first_migration:
            # 旧表 created_at 用的是 CURRENT_TIMESTAMP（UTC），展示前转成本地时间。
            # 只在首次迁移做一次，避免重复 +8 小时。
            try:
                conn.execute(
                    "UPDATE orders SET created_at = datetime(created_at, 'localtime') "
                    "WHERE created_at IS NOT NULL")
            except Exception as e:
                print(f"[storage] created_at 本地化失败（保持原值）：{e}")
        if "paid_at" not in cols:
            conn.execute("ALTER TABLE orders ADD COLUMN paid_at DATETIME")
        if "updated_at" not in cols:
            conn.execute("ALTER TABLE orders ADD COLUMN updated_at DATETIME")
        conn.execute(
            "UPDATE orders SET updated_at = created_at WHERE updated_at IS NULL")
        conn.execute(
            "UPDATE orders SET created_at = updated_at "
            "WHERE created_at IS NULL AND updated_at IS NOT NULL")
    except Exception as e:
        print(f"[storage] orders 时间字段迁移失败（按旧表结构只读运行）：{e}")
    _check_time_cols_sqlite(conn)


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
            created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
            paid_at       DATETIME NULL,
            updated_at    DATETIME NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    _migrate_mysql_columns(conn)


def _migrate_mysql_columns(conn) -> None:
    """老库补列：paid_at / updated_at（created_at 建表就有）。"""
    def _has(col: str) -> bool:
        with conn.cursor() as cur:
            cur.execute(f"SHOW COLUMNS FROM orders LIKE '{col}'")
            return bool(cur.fetchall())

    try:
        if not _has("paid_at"):
            conn.execute("ALTER TABLE orders ADD COLUMN paid_at DATETIME NULL")
        if not _has("updated_at"):
            conn.execute("ALTER TABLE orders ADD COLUMN updated_at DATETIME NULL")
        conn.execute(
            "UPDATE orders SET updated_at = created_at WHERE updated_at IS NULL")
        conn.execute(
            "UPDATE orders SET created_at = updated_at "
            "WHERE created_at IS NULL AND updated_at IS NOT NULL")
    except Exception as e:
        print(f"[storage] orders 时间字段迁移失败（按旧表结构运行）：{e}")
    global _HAS_TIME_COLS
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW COLUMNS FROM orders")
            cols = {r.get("Field") for r in cur.fetchall()}
        _HAS_TIME_COLS = {"paid_at", "updated_at"} <= cols
    except Exception:
        _HAS_TIME_COLS = False


# ---------- 统一数据访问接口 ----------
def _now() -> str:
    """本地时区当前时间，统一 DB / 内存 / API 的时间字符串格式。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _fmt_dt(value) -> str:
    """DB 驱动可能返回 datetime 或字符串，统一成 YYYY-MM-DD HH:MM:SS。"""
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)[:19]


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
            created_at=_fmt_dt(row.get("created_at")),
            paid_at=_fmt_dt(row.get("paid_at")),
            updated_at=_fmt_dt(row.get("updated_at")),
        )
    # sqlite3.Row 带 keys()
    d = dict(row)
    return _row_to_order(d)


def insert_order(order: Order) -> None:
    _init_once()
    # 下单时间只在这里落一次，后续状态变更绝不覆盖
    order.created_at = order.created_at or _now()
    order.updated_at = order.updated_at or order.created_at
    if _backend == "memory":
        _mem_db[order.order_id] = order
        return
    try:
        with _conn_lock:
            if _HAS_TIME_COLS:
                _conn.execute(
                    """INSERT OR REPLACE INTO orders
                       (order_id, hotel_id, hotel_name, check_in, check_out,
                        room_type, guest_name, total_price, status, source,
                        payment_url, contact_email, created_at, paid_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (order.order_id, order.hotel_id, order.hotel_name,
                     order.check_in, order.check_out, order.room_type,
                     order.guest_name, order.total_price, order.status,
                     order.source, order.payment_url, order.contact_email,
                     order.created_at, order.paid_at or None,
                     order.updated_at),
                )
            else:
                # 老库/只读库：保持旧字段，created_at 用表默认值
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
    """更新订单状态，返回是否更新成功。

    - 每次状态变更都刷新 updated_at（“最近动态”时间）
    - 首次进入 paid 时写 paid_at；之后重复置 paid 不覆盖首次支付时间
    - created_at 永远不动
    """
    _init_once()
    now = _now()
    paid_now = status == "paid"
    if _backend == "memory":
        order = _mem_db.get(order_id)
        if order is None:
            return False
        order.status = status
        order.updated_at = now
        if paid_now and not order.paid_at:
            order.paid_at = now
        return True
    try:
        with _conn_lock:
            if not _HAS_TIME_COLS:
                # 老库/只读库：没有时间列，只更新状态
                cur = _conn.execute(
                    "UPDATE orders SET status = ? WHERE order_id = ?",
                    (status, order_id),
                )
            elif paid_now:
                cur = _conn.execute(
                    "UPDATE orders SET status = ?, updated_at = ?, "
                    "paid_at = COALESCE(paid_at, ?) WHERE order_id = ?",
                    (status, now, now, order_id),
                )
            else:
                cur = _conn.execute(
                    "UPDATE orders SET status = ?, updated_at = ? "
                    "WHERE order_id = ?",
                    (status, now, order_id),
                )
            # sqlite3 / pymysql 游标都有 rowcount
            affected = cur.rowcount
        # 更新内存副本，供同进程后续 get_order 立刻读到
        order = _mem_db.get(order_id)
        if order is not None:
            order.status = status
            order.updated_at = now
            if paid_now and not order.paid_at:
                order.paid_at = now
        return affected > 0
    except Exception as e:
        print(f"[storage] update_status 失败：{e}")
        order = _mem_db.get(order_id)
        if order is not None:
            order.status = status
            order.updated_at = now
            if paid_now and not order.paid_at:
                order.paid_at = now
            return True
        return False


def list_all_orders() -> List[Order]:
    """返回全部订单（不限状态，按创建时间倒序）。"""
    _init_once()
    if _backend == "memory":
        return sorted(_mem_db.values(),
                      key=lambda o: o.created_at or "", reverse=True)
    try:
        with _conn_lock:
            cur = _conn.execute(
                "SELECT * FROM orders ORDER BY created_at DESC")
            rows = cur.fetchall()
        return [_row_to_order(r) for r in rows]
    except Exception as e:
        print(f"[storage] list_all_orders 失败：{e}")
        return list(_mem_db.values())
