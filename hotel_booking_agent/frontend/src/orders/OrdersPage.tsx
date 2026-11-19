/**
 * 历史订单页（/orders）。
 *
 * 数据来自 GET /api/orders；筛选在客户端切换（同时改 URL 的 ?filter=，
 * 收藏/分享仍然是同一个地址）。
 *
 * 交易动作的铁律（与后端一致，别在这里"顺手合并成一步"）：
 * - 模拟单：标记支付 / 直接取消，点一次确认即可
 * - 真实单：先 pre-cancel 拿罚金与 10 分钟凭证，把罚金和预计退款摆给用户看，
 *   用户点"确认取消"才提交 cancel（等价 confirm_cancel）
 */

import { useEffect, useRef, useState } from "react";

import { fetchHealth } from "../api/client";
import {
  cancelOrder,
  fetchOrders,
  payOrder,
  preCancelOrder,
} from "../api/orders";
import type { HealthPayload, OrderRow, PreCancelPayload } from "../types";
import { ConfirmDialog } from "./ConfirmDialog";
import { OrdersTable } from "./OrdersTable";
import {
  FILTERS,
  emptyText,
  normalizeFilter,
  type FilterKey,
} from "./format";

type DialogState =
  | { kind: "pay"; order: OrderRow }
  | { kind: "cancelMock"; order: OrderRow }
  | { kind: "cancelReal"; order: OrderRow; quote: PreCancelPayload };

type Notice = { kind: "ok" | "err" | "warn"; text: string };

export default function OrdersPage() {
  const [filter, setFilter] = useState<FilterKey>(() =>
    normalizeFilter(new URLSearchParams(window.location.search).get("filter")),
  );
  const [rows, setRows] = useState<OrderRow[] | null>(null);
  const [loggedIn, setLoggedIn] = useState<boolean | null>(null);
  const [remoteError, setRemoteError] = useState("");
  const [storage, setStorage] = useState<HealthPayload | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const [dialog, setDialog] = useState<DialogState | null>(null);

  const seqRef = useRef(0);
  const filterRef = useRef(filter);
  filterRef.current = filter;

  // 拉列表：切筛选 / 动作完成后重拉；过期响应直接丢弃
  useEffect(() => {
    const seq = ++seqRef.current;
    setError("");
    void (async () => {
      const result = await fetchOrders(filter);
      if (seq !== seqRef.current) return;
      if (!result.ok) {
        setRows([]);
        setError(result.detail);
        return;
      }
      setRows(result.data.rows || []);
      setLoggedIn(result.data.logged_in ?? null);
      setRemoteError(result.data.remote_error || "");
    })();
  }, [filter, reloadKey]);

  useEffect(() => {
    void fetchHealth().then(setStorage);
  }, []);

  // 成功提示自动消失，错误留着让人看清
  useEffect(() => {
    if (!notice || notice.kind !== "ok") return;
    const timer = window.setTimeout(() => setNotice(null), 4000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const chooseFilter = (key: FilterKey): void => {
    setFilter(key);
    const url = new URL(window.location.href);
    url.searchParams.set("filter", key);
    window.history.replaceState(null, "", url);
  };

  const reload = (): void => setReloadKey((k) => k + 1);

  const askPay = (order: OrderRow): void => setDialog({ kind: "pay", order });

  const askCancelMock = (order: OrderRow): void =>
    setDialog({ kind: "cancelMock", order });

  const askCancelReal = async (order: OrderRow): Promise<void> => {
    setBusy(true);
    try {
      const result = await preCancelOrder(order.order_no);
      if (!result.ok) {
        setNotice({ kind: "err", text: result.detail });
        return;
      }
      if (!result.data.ok) {
        setNotice({
          kind: "err",
          text:
            result.data.message || "该订单暂时无法在线取消，请联系道旅客服。",
        });
        return;
      }
      // 询价结果先摆出来，用户确认后才提交取消
      setDialog({ kind: "cancelReal", order, quote: result.data });
    } finally {
      setBusy(false);
    }
  };

  const submitDialog = async (): Promise<void> => {
    if (!dialog) return;
    setBusy(true);
    try {
      if (dialog.kind === "pay") {
        const result = await payOrder(dialog.order.order_no);
        setNotice(
          result.ok
            ? { kind: "ok", text: "已标记支付" }
            : { kind: "err", text: result.detail },
        );
      } else if (dialog.kind === "cancelMock") {
        const result = await cancelOrder(dialog.order.order_no);
        if (!result.ok) {
          setNotice({ kind: "err", text: result.detail });
        } else if (!result.data.ok) {
          setNotice({
            kind: "err",
            text: result.data.message || "操作失败",
          });
        } else {
          setNotice({ kind: "ok", text: result.data.message || "已取消" });
        }
      } else {
        const quote = dialog.quote;
        const result = await cancelOrder(dialog.order.order_no, {
          confirmId: quote.confirm_id || "",
          bookingId: quote.booking_id || "",
        });
        if (!result.ok) {
          setNotice({ kind: "err", text: result.detail });
        } else if (!result.data.ok) {
          setNotice({ kind: "err", text: result.data.message || "取消失败" });
        } else {
          setNotice({ kind: "ok", text: result.data.message || "已取消" });
        }
      }
    } finally {
      setBusy(false);
      setDialog(null);
      reload();
    }
  };

  const renderDialogLines = (state: DialogState) => {
    const { order } = state;
    const lines = [
      `订单：${order.order_no}`,
      `酒店：${order.hotel_name || "-"}`,
      `房型：${order.room_name || "-"}`,
      `总价：¥${order.total_price} ${order.currency || "CNY"}`,
    ];

    if (state.kind === "pay") {
      lines.push("", "这是模拟单：只把本地订单标记为已支付，不会产生真实支付。");
    } else if (state.kind === "cancelMock") {
      lines.push("", "模拟单取消后不可恢复。");
    } else {
      const quote = state.quote;
      const penalty = quote.penalty == null ? null : Number(quote.penalty);
      const total = Number(quote.total_price || 0);
      const refund = penalty == null ? null : Math.max(total - penalty, 0);
      lines.push(
        `取消罚金：${penalty == null ? "以道旅页面为准" : "¥" + penalty}`,
      );
      if (refund != null) lines.push(`预计退款：¥${refund}`);
      if (quote.message) lines.push("", quote.message);
      lines.push("", "取消不可撤销；凭证 10 分钟内有效，超时需重新询价。");
    }

    return lines;
  };

  return (
    <>
      <h1>📋 历史订单</h1>

      <div className="topbar">
        <span className="tag">存储：{storage?.storage_backend ?? "检查中…"}</span>
        {loggedIn === false ? (
          <span className="tag warn">道旅未登录 · 仅本地订单</span>
        ) : null}
        <a href="/">← 返回聊天</a>
      </div>

      <div className="filter">
        {FILTERS.map((item) => (
          <button
            key={item.key}
            type="button"
            className={item.key === filter ? "active" : ""}
            onClick={() => chooseFilter(item.key)}
          >
            {item.label}
          </button>
        ))}
      </div>

      {notice ? <div className={`notice ${notice.kind}`}>{notice.text}</div> : null}
      {error ? <div className="notice err">加载订单失败：{error}</div> : null}
      {remoteError ? (
        <div className="notice warn">
          远端订单查询异常，当前只显示本地订单：{remoteError}
        </div>
      ) : null}

      {rows === null ? (
        <div className="empty">加载中…</div>
      ) : rows.length === 0 ? (
        <div className="empty">{emptyText(filter)}</div>
      ) : (
        <OrdersTable
          rows={rows}
          busy={busy}
          onPay={askPay}
          onCancelMock={askCancelMock}
          onCancelReal={(order) => void askCancelReal(order)}
        />
      )}

      {dialog ? (
        <ConfirmDialog
          title={
            dialog.kind === "pay"
              ? "标记为已支付？"
              : dialog.kind === "cancelMock"
                ? "取消订单？"
                : "确认取消真实订单？"
          }
          confirmLabel={dialog.kind === "pay" ? "确认标记" : "确认取消"}
          danger={dialog.kind !== "pay"}
          busy={busy}
          onCancel={() => setDialog(null)}
          onConfirm={() => void submitDialog()}
        >
          {renderDialogLines(dialog).map((line, index) => (
            <div key={index}>{line}</div>
          ))}
        </ConfirmDialog>
      ) : null}
    </>
  );
}
