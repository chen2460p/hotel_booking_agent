# -*- coding: utf-8 -*-
"""
道旅 RollingGo 真实预订适配（OAuth 版）
========================================
官方 CLI @rollinggo/hotel 使用的是 REST 直连接口（非 MCP JSON-RPC）：
  POST {MCP_BASE}/hotelpriceconfirm  验价/锁房 -> referenceNo
  POST {MCP_BASE}/hotelbook          创建订单   -> orderNo + paymentUrl
  POST {MCP_BASE}/hotelorders        查询订单列表
  POST {MCP_BASE}/hotelorderdetail   查询订单详情

鉴权：OAuth 授权码 + PKCE（client_id=rollinggoskill），通过 rollinggo.store
中转完成授权。token 与官方 CLI 共享，存放在：
  Windows: %USERPROFILE%\\.hotel-cli\\token.json

首次使用请在项目目录执行：
  python rollinggo_book.py login

安全约定：本模块只负责“验价 + 下单拿到支付链接”，不会代替用户完成支付。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
try:  # 与其他模块保持一致的 .env 加载方式
    from dotenv import load_dotenv
    load_dotenv(_HERE / ".env", override=True)
except Exception:
    pass

MCP_BASE = os.getenv("ROLLINGGO_MCP_URL", "https://mcp.rollinggo.cn/mcp").rstrip("/")
OAUTH_SERVER = os.getenv("ROLLINGGO_OAUTH_SERVER", "https://rollinggo.store").rstrip("/")
OAUTH_AUTHORIZE = os.getenv(
    "ROLLINGGO_OAUTH_AUTHORIZE", "https://api.rollinggo.cn/oauth2/authorize"
)
CLIENT_ID = os.getenv("ROLLINGGO_CLIENT_ID", "rollinggoskill")

_SCOPE = "profile phone email hotel:order:read hotel:order:book hotel:order:cancel"
_TOKEN_PATH = Path(
    os.path.expanduser(os.getenv("ROLLINGGO_TOKEN_FILE", "~/.hotel-cli/token.json"))
)

_POLL_INTERVAL = 2          # 与官方 CLI 一致：2 秒
_POLL_MAX_RETRIES = 150     # 最长等待 5 分钟


class RollingGoAuthError(Exception):
    """未登录 / token 失效等鉴权问题"""


class RollingGoApiError(Exception):
    """道旅接口返回业务错误"""


# ---------------------------------------------------------------------------
# Token 存取（与官方 rgh CLI 共享同一份 token.json）
# ---------------------------------------------------------------------------
def load_token() -> Optional[Dict[str, Any]]:
    """读取本地 OAuth token；环境变量 ROLLINGGO_ACCESS_TOKEN 可直接覆盖。"""
    env_token = os.getenv("ROLLINGGO_ACCESS_TOKEN")
    if env_token:
        return {"access_token": env_token}
    try:
        if _TOKEN_PATH.exists():
            return json.loads(_TOKEN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return None


def save_token(token: Dict[str, Any]) -> None:
    _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = dict(token)
    data.setdefault("_saved_at", int(time.time()))
    _TOKEN_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def is_logged_in() -> bool:
    token = load_token()
    return bool(token and token.get("access_token"))


def token_status() -> str:
    token = load_token()
    if not token or not token.get("access_token"):
        return f"未登录，请运行：python {_HERE.name / Path('rollinggo_book.py')} login"
    access = str(token["access_token"])
    expires_in = token.get("expires_in")
    saved_at = token.get("_saved_at")
    tail = ""
    if expires_in and saved_at:
        left = int(saved_at) + int(expires_in) - int(time.time())
        tail = f"，约 {max(left, 0) // 60} 分钟后过期" if left > 0 else "（已过期，请重新登录）"
    return f"已登录（token: {access[:16]}...）{tail}"


# ---------------------------------------------------------------------------
# OAuth 登录（PKCE，复刻官方 CLI 流程，无需安装 Node）
# ---------------------------------------------------------------------------
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def login(open_browser: bool = True) -> str:
    """交互式 OAuth 登录，成功后返回 access_token。"""
    code_verifier = secrets.token_urlsafe(43)[:43]
    code_challenge = _b64url(hashlib.sha256(code_verifier.encode()).digest())
    session_id = secrets.token_hex(16)

    resp = requests.post(
        f"{OAUTH_SERVER}/skill/oauth/init",
        json={
            "session_id": session_id,
            "code_verifier": code_verifier,
            "client_id": CLIENT_ID,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RollingGoApiError(f"获取授权 state 失败: {resp.status_code} {resp.text[:300]}")
    init_data = resp.json()
    state = init_data["state"]
    poll_key = init_data.get("session_id", session_id)

    redirect_uri = f"{OAUTH_SERVER}/skill/oauth/callback"
    auth_url = (
        f"{OAUTH_AUTHORIZE}?response_type=code"
        f"&client_id={CLIENT_ID}"
        f"&redirect_uri={requests.utils.quote(redirect_uri, safe='')}"
        f"&state={requests.utils.quote(state, safe='')}"
        f"&code_challenge={code_challenge}"
        f"&code_challenge_method=S256"
        f"&scope={requests.utils.quote(_SCOPE, safe='')}"
        f"&resource={requests.utils.quote(MCP_BASE, safe='')}"
        f"&prompt=consent"
    )

    # 尝试短链接（失败则用长链接）
    short_url = auth_url
    try:
        sr = requests.post(f"{OAUTH_SERVER}/s/shorten", json={"url": auth_url}, timeout=15)
        if sr.ok and sr.json().get("shortUrl"):
            short_url = sr.json()["shortUrl"]
    except requests.RequestException:
        pass

    print("=" * 60)
    print("道旅 RollingGo OAuth 登录")
    print("=" * 60)
    print("请在浏览器中打开下面的链接，登录道旅账号并同意授权：\n")
    print(f"  {short_url}\n")
    if open_browser:
        try:
            webbrowser.open(short_url)
            print("（已尝试自动打开浏览器，如未弹出请手动复制链接）")
        except Exception:
            pass
    print(f"\n等待授权中，最长 5 分钟 ...", flush=True)

    token_url = f"{OAUTH_SERVER}/skill/oauth/token?session_id={poll_key}"
    for _ in range(_POLL_MAX_RETRIES):
        time.sleep(_POLL_INTERVAL)
        try:
            tr = requests.get(token_url, timeout=15)
            if not tr.ok:
                continue
            result = tr.json()
        except (requests.RequestException, json.JSONDecodeError):
            continue

        status = result.get("status")
        if status == "success" and result.get("token"):
            token = result["token"]
            save_token(token)
            print(f"\n登录成功，token 已保存到：{_TOKEN_PATH}")
            return token["access_token"]
        if status == "expired":
            raise RollingGoAuthError("授权会话已过期，请重新执行 login。")
        # pending：继续等待
    raise RollingGoAuthError("等待授权超时，请重新执行 login。")


# ---------------------------------------------------------------------------
# REST 通用请求
# ---------------------------------------------------------------------------
def _request(endpoint: str, payload: Optional[Dict[str, Any]] = None) -> Any:
    token = load_token()
    if not token or not token.get("access_token"):
        raise RollingGoAuthError(
            "道旅账号未登录。请先在项目目录运行：python rollinggo_book.py login"
        )
    resp = requests.post(
        f"{MCP_BASE}{endpoint}",
        headers={
            "Authorization": f"Bearer {token['access_token']}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json=payload or {},
        timeout=60,
    )
    if resp.status_code in (401, 403):
        raise RollingGoAuthError(
            f"道旅登录已失效（HTTP {resp.status_code}），请重新运行：python rollinggo_book.py login"
        )
    if resp.status_code >= 400:
        raise RollingGoApiError(f"道旅接口失败（HTTP {resp.status_code}）：{resp.text[:500]}")
    try:
        data = resp.json()
    except ValueError:
        raise RollingGoApiError(f"道旅接口返回非 JSON：{resp.text[:300]}")
    _raise_if_business_error(data)
    return data


def _raise_if_business_error(data: Any) -> None:
    """
    兼容道旅的响应包裹。实测成功响应为：
      {"success": true, "code": 2000, "message": "Price confirm success", ...}
    判定优先级：success 字段 > code 白名单。
    """
    if not isinstance(data, dict):
        return
    # success 明确为 true：无论 code 是什么（道旅成功码是 2000）都视为成功
    if data.get("success") is True:
        return
    code = data.get("code")
    accepted = (None, 0, "0", 200, "200", 2000, "2000", "SUCCESS", "success")
    if code in accepted and data.get("success") is not False:
        return
    msg = (data.get("message") or data.get("msg")
           or data.get("errorMsg") or str(data)[:300])
    raise RollingGoApiError(f"道旅业务错误 code={code}：{msg}")


def _unwrap(data: Any) -> Any:
    """脱掉常见外层包裹，取业务主体。"""
    if isinstance(data, dict):
        for key in ("data", "result", "body", "response"):
            inner = data.get(key)
            if isinstance(inner, (dict, list)) and inner not in ({}, []):
                return inner
    return data


def _find_key(obj: Any, candidates: List[str], default: Any = None) -> Any:
    """递归在嵌套 dict/list 中按候选键名（忽略大小写）找第一个值。"""
    wanted = {c.lower() for c in candidates}
    found: List[Any] = []

    def walk(node: Any) -> None:
        if found:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(k, str) and k.lower() in wanted and v not in (None, ""):
                    found.append(v)
                    return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(obj)
    return found[0] if found else default


# ---------------------------------------------------------------------------
# 业务接口
# ---------------------------------------------------------------------------
def price_confirm(
    hotel_id: int,
    rate_plan_id: str,
    check_in: str,
    check_out: str,
    num_of_rooms: int = 1,
    adult_count: int = 2,
    child_count: int = 0,
) -> Dict[str, Any]:
    """
    验价 / 锁房。成功返回归一化结果：
      reference_no   下单必须的预订参考号（有时效，需紧接着下单）
      total_price    总价
      per_night      每晚均价（拿不到时为 None）
      currency       币种
      room_name / meal / cancelable / cancel_policy  房型与退改信息
      raw            原始响应（调试用）
    """
    payload = {
        "hotelID": int(hotel_id),
        "ratePlanID": str(rate_plan_id),
        "numOfRooms": int(num_of_rooms),
        "dateParam": {"checkInDate": check_in, "checkOutDate": check_out},
        "occupancyDetails": [
            {
                "roomNum": i + 1,
                "adultCount": int(adult_count),
                "childCount": int(child_count),
            }
            for i in range(int(num_of_rooms))
        ],
    }
    raw = _request("/hotelpriceconfirm", payload)

    # 实测响应结构（2026-10）：
    # {success, code:2000, priceDetailsInfo: {
    #     referenceNo, checkInDate, checkOutDate,
    #     hotelList: [{hotelName, roomName, roomNameCn, totalPrice, currency,
    #                  cancelPolicy, bedTypeStr, isOnRequest, ...}]},
    #  guestProfile: {contactDefault, recentGuests:[{name,phone,email}]}}
    info = raw.get("priceDetailsInfo") if isinstance(raw, dict) else None
    if not isinstance(info, dict):
        info = _unwrap(raw)
    item = {}
    if isinstance(info, dict):
        hotels = info.get("hotelList")
        if isinstance(hotels, list) and hotels:
            item = hotels[0] if isinstance(hotels[0], dict) else {}

    reference_no = info.get("referenceNo") or _find_key(
        raw, ["referenceNo", "ReferenceNo", "reference_no"])
    total_price = item.get("totalPrice")
    if total_price is None:
        total_price = _find_key(
            item, ["totalPrice", "totalAmount", "total_price", "total", "amount"])
    per_night = _find_key(
        item, ["avgPricePerNight", "pricePerNight", "averagePrice", "perNightPrice"])
    currency = item.get("currency") or _find_key(item, ["currencyCode", "ccy"], "CNY")
    room_name = item.get("roomNameCn") or item.get("roomName")
    meal = item.get("mealType") or _find_key(
        item, ["meal", "boardCode", "breakfast", "mealTypeDesc"])
    cancel_policy = item.get("cancelPolicy") or _find_key(
        item, ["cancellationPolicy", "cancelRule"])
    bed_type = item.get("bedTypeStr") or item.get("bedType")
    on_request = item.get("isOnRequest")
    # 道旅未直接给布尔位，退改文案以"免费取消"开头即可免费取消
    cancelable = None
    if cancel_policy:
        cancelable = "免费取消" in str(cancel_policy)

    guest_profile = raw.get("guestProfile") if isinstance(raw, dict) else None
    recent_guests: List[Dict[str, str]] = []
    contact_default: Dict[str, str] = {}
    if isinstance(guest_profile, dict):
        rg = guest_profile.get("recentGuests")
        if isinstance(rg, list):
            recent_guests = [
                g for g in rg if isinstance(g, dict) and g.get("name")
            ]
        cd = guest_profile.get("contactDefault")
        if isinstance(cd, dict):
            contact_default = cd

    return {
        "reference_no": reference_no,
        "total_price": _to_number(total_price),
        "per_night": _to_number(per_night),
        "currency": str(currency or "CNY"),
        "room_name": room_name,
        "meal": str(meal) if meal is not None else None,
        "cancelable": cancelable,
        "cancel_policy": str(cancel_policy) if cancel_policy is not None else None,
        "bed_type": str(bed_type) if bed_type else None,
        "on_request": bool(on_request) if on_request is not None else None,
        "contact_default": contact_default,
        "recent_guests": recent_guests,
        "raw": raw,
    }


def create_booking(
    reference_no: str,
    first_name: str,
    last_name: str,
    email: str,
    customer_request: Optional[str] = None,
) -> Dict[str, Any]:
    """
    创建真实订单（1 间房、1 位成人住客的默认场景）。
    返回：order_no / payment_url / status / raw
    注意：拿到 payment_url 仅代表订单已创建、待支付，不会自动扣款。
    """
    guest = {"roomNum": 1, "guestInfo": [{"firstName": first_name, "lastName": last_name, "isAdult": True}]}
    payload: Dict[str, Any] = {
        "referenceNo": reference_no,
        "contact": {"firstName": first_name, "lastName": last_name, "email": email},
        "guestList": [guest],
    }
    if customer_request:
        payload["customerRequest"] = customer_request

    raw = _request("/hotelbook", payload)
    body = _unwrap(raw)

    order_no = _find_key(body, ["orderNo", "order_no", "bookingNo", "id"])
    payment_url = _find_key(
        body,
        ["paymentUrl", "payment_url", "payUrl", "cashierUrl", "alipayUrl", "payURL"],
    )
    status = _find_key(body, ["orderStatus", "status", "bookingStatus"])

    return {
        "order_no": str(order_no) if order_no is not None else None,
        "payment_url": str(payment_url) if payment_url else None,
        "status": str(status) if status is not None else None,
        "raw": raw,
    }


def list_orders(status: str = "ALL") -> Any:
    """查询订单列表：ALL / PENDING / FINISHED。返回原始响应。"""
    status = (status or "ALL").upper()
    if status not in ("ALL", "PENDING", "FINISHED"):
        status = "ALL"
    return _request("/hotelorders", {"status": status})


def get_order_detail(order_no: str) -> Any:
    """查询订单详情。"""
    return _request("/hotelorderdetail", {"orderNo": str(order_no)})


# ---------------------------------------------------------------------------
# 订单响应归一化（列表 / 详情共用）
# ---------------------------------------------------------------------------
# 道旅订单状态为英文码/短语，不同渠道取值不完全一致，这里按关键词保守映射；
# 含中文的状态直接透传，无法识别的英文码原样展示，绝不臆造状态。
def humanize_order_status(raw_status: Any) -> str:
    if raw_status in (None, ""):
        return "未知"
    s = str(raw_status).strip()
    upper = s.upper()
    if re.search(r"[\u4e00-\u9fa5]", s):
        return s                       # 接口已给中文，直接用
    if any(k in upper for k in ("CANCEL", "REFUND", "REJECT", "VOID", "FAIL")):
        return "已取消"
    if any(k in upper for k in ("PAY", "UNPAID")) or upper in ("PENDING", "WAIT"):
        return "待支付"
    if "REQUEST" in upper or "CONFIRMING" in upper:
        return "待酒店确认"
    if any(k in upper for k in ("FINISH", "COMPLET", "CHECKED", "STAYED", "CLOSED")):
        return "已完成"
    if any(k in upper for k in ("CONFIRM", "BOOKED", "SUCCESS", "ISSUED")):
        return "已确认"
    return s


def _extract_order_rows(raw: Any) -> List[Dict[str, Any]]:
    """从列表/详情响应中取出订单 dict 列表（实测列表在 orderList 字段）。"""
    # 1) 顶层就是列表
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]

    # 2) 优先递归找 orderList（可能嵌在 data/result 等包裹层下；空列表也是有效结果）
    listed = _find_key(raw, ["orderList", "orders", "bookingList"])
    if isinstance(listed, list):
        return [x for x in listed if isinstance(x, dict)]

    # 3) 脱包裹后再找订单数组或单个订单对象（详情响应）
    body = _unwrap(raw)
    if isinstance(body, list):
        return [x for x in body if isinstance(x, dict)]
    if isinstance(body, dict):
        for v in body.values():
            if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                return v
        if _find_key(body, ["orderNo", "order_no", "bookingNo"]):
            return [body]
    return []


def parse_orders(raw: Any) -> List[Dict[str, Any]]:
    """
    把道旅订单列表/详情响应归一化为统一行结构：
    order_no / hotel_name / room_name / check_in / check_out /
    total_price / currency / status(原始) / status_text(中文) /
    payment_url / create_time
    """
    rows: List[Dict[str, Any]] = []
    for it in _extract_order_rows(raw):
        status = _find_key(it, ["orderStatus", "status", "bookingStatus"])
        rows.append({
            "order_no": str(_find_key(
                it, ["orderNo", "order_no", "bookingNo"], "")) or "",
            "hotel_name": str(_find_key(
                it, ["hotelName", "hotelNameCn", "hotel"], "")) or "",
            "room_name": str(_find_key(
                it, ["roomName", "roomNameCn", "room"], "")) or "",
            "check_in": str(_find_key(
                it, ["checkInDate", "checkIn"], "")) or "",
            "check_out": str(_find_key(
                it, ["checkOutDate", "checkOut"], "")) or "",
            "total_price": _to_number(_find_key(
                it, ["totalPrice", "totalAmount", "total", "amount"])),
            "currency": str(_find_key(it, ["currency", "currencyCode", "ccy"], "CNY")),
            "status": status,
            "status_text": humanize_order_status(status),
            "payment_url": str(_find_key(
                it, ["paymentUrl", "payUrl", "cashierUrl", "payURL"], "")) or "",
            "create_time": str(_find_key(
                it, ["createTime", "createdAt", "orderTime", "bookingTime"], "")) or "",
            "source": "rollinggo",
        })
    return rows


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def extract_email(text: str) -> Optional[str]:
    m = _EMAIL_RE.search(text or "")
    return m.group(0) if m else None


def split_cn_name(full_name: str) -> Dict[str, str]:
    """
    拆分联系人姓名为 firstName/lastName。
    中文名：首字为姓，其余为名（如 张三丰 -> last=张, first=三丰）。
    拼音/英文：按空格，最后一段为姓。
    不确定时调用方应让用户在确认环节自行纠正。
    """
    name = (full_name or "").strip()
    if not name:
        return {"first_name": "", "last_name": ""}
    if re.fullmatch(r"[A-Za-z][A-Za-z .'\-]*", name):
        parts = name.split()
        if len(parts) == 1:
            return {"first_name": parts[0], "last_name": parts[0]}
        return {"first_name": " ".join(parts[:-1]), "last_name": parts[-1]}
    # 中文：单名时姓=名（少数接口要求两字段非空）
    if len(name) == 1:
        return {"first_name": name, "last_name": name}
    return {"first_name": name[1:], "last_name": name[0]}


def _to_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(m.group(0)) if m else None


def _to_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("true", "1", "yes", "y", "可免费取消"):
        return True
    if s in ("false", "0", "no", "n", "不可取消"):
        return False
    return None


# ---------------------------------------------------------------------------
# 命令行入口：python rollinggo_book.py login | status | logout | orders
# ---------------------------------------------------------------------------
def _cli_orders(status: str = "ALL") -> None:
    """命令行查询道旅历史订单：python rollinggo_book.py orders [all|pending|finished]"""
    try:
        raw = list_orders(status)
    except (RollingGoAuthError, RollingGoApiError) as e:
        print(f"查询失败：{e}")
        return
    rows = parse_orders(raw)
    if not rows:
        print(f"道旅账号下没有{'待支付' if status == 'PENDING' else ''}订单。")
        return
    print(f"共 {len(rows)} 笔订单：")
    for i, r in enumerate(rows, 1):
        line = (
            f"{i}. {r['order_no']} | {r['hotel_name']} | "
            f"{r['check_in']}→{r['check_out']} | "
            f"{r['total_price'] if r['total_price'] is not None else '?'}"
            f" {r['currency']} | {r['status_text']}"
        )
        print(line)
        if r["payment_url"] and r["status_text"] == "待支付":
            print(f"   支付链接：{r['payment_url']}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "login":
        login()
    elif cmd == "status":
        print(token_status())
    elif cmd == "logout":
        if _TOKEN_PATH.exists():
            _TOKEN_PATH.unlink()
            print(f"已删除 {_TOKEN_PATH}")
        else:
            print("当前未登录")
    elif cmd == "orders":
        _cli_orders((sys.argv[2] if len(sys.argv) > 2 else "ALL").upper())
    else:
        print("用法: python rollinggo_book.py [login|status|logout|orders [all|pending|finished]]")
