/**
 * 房型结构化视图：房型 × 报价档的卡片列表。
 *
 * 编号与后端、与回复文本完全同源（都走 plan_groups），所以点卡片和回复
 * 「2-1」是同一件事；区别是卡片不再解析文本，也不会把序号猜错。
 *
 * 点「选这间」只提交 room_no + plan_no + rooms_revision（不带入住人姓名）：
 * 后端只把房型记进 pending_booking 并反问入住人，不会验价、不会下单。
 */

import { useState } from "react";

import type { RoomItem, RoomPlan, RoomsView as RoomsViewData } from "../types";
import { LazyImg } from "./LazyImg";

interface RoomsViewProps {
  view: RoomsViewData;
  /** 列表版本对不上（选了别家酒店 / 列表已更新）→ 置灰 */
  stale: boolean;
  /** 这一组房型已经选过了 → 按钮收起为不可选，避免重复提交覆盖 */
  picked: boolean;
  onPick: (room: RoomItem, plan: RoomPlan) => Promise<void> | void;
}

function roomMeta(room: RoomItem): string {
  const parts: string[] = [];
  if (room.bed_type) parts.push(room.bed_type);
  if (room.room_size) parts.push(room.room_size);
  if (room.max_occupancy) parts.push(`可住${room.max_occupancy}人`);
  if (room.on_request) parts.push("需申请确认");
  return parts.join(" · ");
}

function planDesc(plan: RoomPlan, nights: number): string {
  const parts: string[] = [];
  if (plan.meal) parts.push(plan.meal);
  parts.push(
    plan.cancelable ? "免费取消" : plan.cancel_known ? "不可取消" : "退改未知",
  );
  if (plan.on_request) parts.push("需申请确认");
  if (plan.is_lowest) parts.push("本房型最低");
  if (nights > 0) parts.push(`${nights}晚约¥${plan.total_price}`);
  return parts.join(" · ");
}

export function RoomsView({ view, stale, picked, onPick }: RoomsViewProps) {
  const [pending, setPending] = useState<string | null>(null);

  const pick = async (room: RoomItem, plan: RoomPlan): Promise<void> => {
    if (stale || picked || pending) return;
    setPending(`${room.room_no}-${plan.plan_no}`);
    try {
      await onPick(room, plan);
    } finally {
      setPending(null);
    }
  };

  return (
    <div className="rooms-view" data-revision={view.rooms_revision}>
      <div className="rooms-head">
        {`已选：${view.hotel_name} · ${view.check_in} → ${view.check_out}（${view.nights}晚）`}
      </div>
      <div className="rooms-sub">
        {`可选房型 ${view.total_rooms} 种`}
        {view.truncated ? `，当前展示前 ${view.shown_rooms} 种` : ""}
        {`；点「选这间」记下房型，再补入住人姓名`}
      </div>

      {(view.rooms || []).map((room) => (
        <div
          className={`room-card${stale || picked ? " stale" : ""}`}
          key={room.room_no}
        >
          <div className="room-head">
            {room.room_no}. {room.room_name}
          </div>
          {roomMeta(room) ? <div className="room-meta">{roomMeta(room)}</div> : null}

          {(room.images || []).length ? (
            <div className="room-imgs">
              {room.images.slice(0, 6).map((url, i) => (
                <LazyImg
                  key={`${i}-${url}`}
                  className="chat-img"
                  src={url}
                  onClick={() =>
                    window.open(url, "_blank", "noopener,noreferrer")
                  }
                />
              ))}
            </div>
          ) : null}

          {room.plans.map((plan) => {
            const key = `${room.room_no}-${plan.plan_no}`;
            return (
              <div className="room-plan" key={plan.plan_no}>
                <div className="price">{`¥${plan.price_per_night}/晚`}</div>
                <div className="plan-desc">{planDesc(plan, view.nights)}</div>
                <button
                  type="button"
                  disabled={stale || picked || pending !== null}
                  onClick={() => pick(room, plan)}
                >
                  {pending === key
                    ? "选择中…"
                    : picked
                      ? "已选"
                      : stale
                        ? "列表已更新"
                        : "选这间"}
                </button>
              </div>
            );
          })}
        </div>
      ))}
    </div>
  );
}
