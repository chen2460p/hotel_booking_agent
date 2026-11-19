/**
 * 订单页的接口封装。
 *
 * 三个动作接口都是真实交易相关，这里只负责"发请求 + 把状态码翻译成人话"，
 * 确认/取消的交互顺序（先询价拿凭证、再确认提交）由页面决定：
 * - pay       仅模拟单；真实单后端 409 拒绝
 * - pre-cancel 只查罚金/拿 10 分钟凭证，**不提交取消**
 * - cancel    真实单必须带 pre-cancel 的 confirm_id + booking_id
 */

import type {
  OrdersActionResult,
  OrdersPayload,
  PreCancelPayload,
} from "../types";

export type CallResult<T> =
  | { ok: true; data: T }
  | { ok: false; status: number; detail: string };

async function call<T>(url: string, init?: RequestInit): Promise<CallResult<T>> {
  try {
    const response = await fetch(url, init);
    const data = (await response.json().catch(() => null)) as
      | { detail?: string; message?: string }
      | null;
    if (!response.ok) {
      return {
        ok: false,
        status: response.status,
        detail: data?.detail || data?.message || `HTTP ${response.status}`,
      };
    }
    return { ok: true, data: data as T };
  } catch (e) {
    return {
      ok: false,
      status: 0,
      detail: e instanceof Error ? e.message : String(e),
    };
  }
}

function jsonBody(body: unknown): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

export function fetchOrders(
  filter: string,
): Promise<CallResult<OrdersPayload>> {
  return call<OrdersPayload>(
    `/api/orders?filter=${encodeURIComponent(filter)}`,
    { cache: "no-store" },
  );
}

/** ⚠️ 仅模拟单：标记本地已支付（真实单会被后端 409 拒绝） */
export function payOrder(orderNo: string): Promise<CallResult<OrdersActionResult>> {
  return call<OrdersActionResult>(
    `/api/orders/${encodeURIComponent(orderNo)}/pay`,
    { method: "POST" },
  );
}

/** ⚠️ 真实交易：取消询价（罚金 + 10 分钟凭证），不提交取消 */
export function preCancelOrder(
  orderNo: string,
): Promise<CallResult<PreCancelPayload>> {
  return call<PreCancelPayload>(
    `/api/orders/${encodeURIComponent(orderNo)}/pre-cancel`,
    { method: "POST" },
  );
}

/** 取消：模拟单直接取消；真实单必须带询价凭证（等价 confirm_cancel） */
export function cancelOrder(
  orderNo: string,
  credentials?: { confirmId: string; bookingId: string },
): Promise<CallResult<OrdersActionResult>> {
  const url = `/api/orders/${encodeURIComponent(orderNo)}/cancel`;
  if (!credentials) return call<OrdersActionResult>(url, { method: "POST" });
  return call<OrdersActionResult>(
    url,
    jsonBody({
      confirm: true,
      confirm_id: credentials.confirmId,
      booking_id: credentials.bookingId,
    }),
  );
}
