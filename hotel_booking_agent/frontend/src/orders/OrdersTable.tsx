/**
 * 订单表格。
 *
 * 与旧版服务端渲染的表格逐列对齐；有支付时间的订单多一行明细
 * （下单/支付/最近动态分开，互不覆盖——created_at 与 paid_at 语义不能糊）。
 */

import { Fragment } from "react";

import type { OrderRow } from "../types";
import { actionsOf, fmtTime, lastEvent, sourceLabel, statusOf } from "./format";

interface OrdersTableProps {
  rows: OrderRow[];
  busy: boolean;
  onPay: (order: OrderRow) => void;
  onCancelMock: (order: OrderRow) => void;
  onCancelReal: (order: OrderRow) => void;
}

export function OrdersTable({
  rows,
  busy,
  onPay,
  onCancelMock,
  onCancelReal,
}: OrdersTableProps) {
  return (
    <table>
      <thead>
        <tr>
          <th>订单号</th>
          <th>酒店</th>
          <th>房型</th>
          <th>入住</th>
          <th>下单时间</th>
          <th>总价</th>
          <th>状态</th>
          <th>来源</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((order) => {
          const status = statusOf(order);
          const actions = actionsOf(order);
          const hasAction =
            actions.paymentUrl ||
            actions.payMock ||
            actions.cancelMock ||
            actions.cancelReal;

          return (
            <Fragment key={order.order_no}>
              <tr>
                <td>{order.order_no}</td>
                <td>{order.hotel_name}</td>
                <td>{order.room_name || "-"}</td>
                <td className="time">
                  {order.check_in || "?"} → {order.check_out || "?"}
                </td>
                <td className="time">{fmtTime(order.create_time)}</td>
                <td>
                  {order.total_price} {order.currency || "CNY"}
                </td>
                <td className={status.className}>
                  {status.text}
                  {order.paid_time ? <span className="pay-badge">已支付</span> : null}
                </td>
                <td>{sourceLabel(order)}</td>
                <td>
                  {actions.paymentUrl ? (
                    <a
                      className="pay-link"
                      target="_blank"
                      rel="noopener noreferrer"
                      href={actions.paymentUrl}
                    >
                      去支付
                    </a>
                  ) : null}
                  {actions.payMock ? (
                    <button
                      type="button"
                      className="btn"
                      disabled={busy}
                      onClick={() => onPay(order)}
                    >
                      标记支付
                    </button>
                  ) : null}
                  {actions.cancelMock ? (
                    <button
                      type="button"
                      className="btn danger"
                      disabled={busy}
                      onClick={() => onCancelMock(order)}
                    >
                      取消
                    </button>
                  ) : null}
                  {actions.cancelReal ? (
                    <button
                      type="button"
                      className="btn danger"
                      disabled={busy}
                      onClick={() => onCancelReal(order)}
                    >
                      取消订单
                    </button>
                  ) : null}
                  {hasAction ? null : "-"}
                </td>
              </tr>

              {/* 有支付时间才展示明细：创建/支付/最近动态分开，互不覆盖 */}
              {order.paid_time ? (
                <tr className="detail-row">
                  <td colSpan={9}>
                    <div className="detail-box">
                      <span>
                        下单时间：<b>{fmtTime(order.create_time)}</b>
                      </span>
                      <span>
                        支付时间：<b>{fmtTime(order.paid_time)}</b>
                      </span>
                      <span>
                        最近动态：<b>{lastEvent(order)}</b>
                      </span>
                    </div>
                  </td>
                </tr>
              ) : null}
            </Fragment>
          );
        })}
      </tbody>
    </table>
  );
}
