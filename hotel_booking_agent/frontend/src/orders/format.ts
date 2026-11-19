/**
 * 订单页的纯展示逻辑。
 *
 * 与旧版 templates/orders.html 的 fmtTime / lastEvent / 状态分桶逐条对齐，
 * 抽成纯函数是为了能直接跑断言（不依赖 DOM，也不依赖后端）。
 */

import type { OrderRow } from "../types";

export type FilterKey = "ALL" | "PENDING" | "FINISHED" | "CANCELLED";

export const FILTERS: Array<{ key: FilterKey; label: string }> = [
  { key: "ALL", label: "全部" },
  { key: "PENDING", label: "待支付" },
  { key: "FINISHED", label: "已完成" },
  { key: "CANCELLED", label: "已取消" },
];

/** URL 上的 ?filter= 只认这四个值，其余一律按「全部」处理 */
export function normalizeFilter(value: string | null | undefined): FilterKey {
  const key = String(value || "").toUpperCase();
  return key === "PENDING" || key === "FINISHED" || key === "CANCELLED"
    ? key
    : "ALL";
}

export function filterLabel(filter: FilterKey): string {
  return FILTERS.find((f) => f.key === filter)?.label || "全部";
}

/** 时间统一 "YYYY-MM-DD HH:MM"；接口给的是 "YYYY-MM-DD HH:MM:SS" */
export function fmtTime(value: string | null | undefined): string {
  if (!value) return "-";
  const text = String(value).replace("T", " ");
  return text.length >= 16 ? text.slice(0, 16) : text;
}

/** 最近动态：优先支付时间，其次状态变更时间，最后创建时间 */
export function lastEvent(order: OrderRow): string {
  if (order.paid_time) return `${fmtTime(order.paid_time)} 支付成功`;
  if (order.updated_at && order.updated_at !== order.create_time) {
    return `${fmtTime(order.updated_at)} ${order.status_text || order.status || "状态更新"}`;
  }
  if (order.create_time) return `${fmtTime(order.create_time)} 创建订单`;
  return "-";
}

/** 状态分桶：后端给了就用后端的，没给按 status 兜底 */
export function bucketOf(order: OrderRow): FilterKey {
  const given = order.status_bucket;
  if (given === "PENDING" || given === "FINISHED" || given === "CANCELLED") {
    return given;
  }
  if (order.status === "pending") return "PENDING";
  if (order.status === "cancelled") return "CANCELLED";
  return "FINISHED";
}

/** 状态文案与配色类名（真实单的待支付统一显示"待支付"） */
export function statusOf(order: OrderRow): { text: string; className: string } {
  const bucket = bucketOf(order);
  const text =
    bucket === "PENDING" && order.source === "rollinggo"
      ? "待支付"
      : order.status_text || order.status || "-";
  const className =
    bucket === "PENDING"
      ? "status-pending"
      : bucket === "CANCELLED"
        ? "status-cancel"
        : "status-paid";
  return { text, className };
}

export function sourceLabel(order: OrderRow): string {
  return order.source === "rollinggo" ? "【道旅】" : "本地";
}

export interface OrderActions {
  /** 模拟单：本地标记已支付 */
  payMock: boolean;
  /** 模拟单：直接取消 */
  cancelMock: boolean;
  /** 真实单：询价 + 凭证确认两步取消 */
  cancelReal: boolean;
  /** 真实单的收银台链接（可能为空） */
  paymentUrl: string;
}

export function actionsOf(order: OrderRow): OrderActions {
  const pending = bucketOf(order) === "PENDING";
  return {
    payMock: pending && order.source === "mock",
    cancelMock: pending && order.source === "mock",
    cancelReal: pending && order.source === "rollinggo",
    paymentUrl: pending && order.source === "rollinggo" ? order.payment_url || "" : "",
  };
}

export function emptyText(filter: FilterKey): string {
  return filter === "ALL" ? "暂无订单" : `暂无${filterLabel(filter)}订单`;
}
