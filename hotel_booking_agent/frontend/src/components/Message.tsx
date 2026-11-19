/**
 * 一条消息气泡（user / bot / sys）。
 *
 * 与旧版 append() 的 DOM 结构保持一致：.bot > span + 结构化视图 + 展开按钮
 * （样式表复用旧版类名，视觉零变化）。
 *
 * 回复里有 room_view 时不再把整段房型文案铺在气泡里：卡片才是主视图，
 * 原文收进「查看完整文字列表」（只渲染链接、不加载图片，避免重复请求 CDN）。
 */

import type { HotelItem, MessageRecord, RoomItem, RoomPlan } from "../types";
import { ExpandButton } from "./ExpandButton";
import { HotelView } from "./HotelView";
import { RichText } from "./RichText";
import { RoomsView } from "./RoomsView";

interface MessageProps {
  record: MessageRecord;
  index: number;
  /** 酒店卡片是否因列表版本不符而置灰 */
  stale: boolean;
  /** 房型卡片是否因列表版本不符而置灰 */
  roomsStale: boolean;
  onSelect: (item: HotelItem, revision: string) => Promise<void> | void;
  onPickRoom: (index: number, room: RoomItem, plan: RoomPlan) => Promise<void> | void;
  onExpand: (index: number, onProgress?: (text: string) => void) => Promise<void>;
}

export function Message({
  record,
  index,
  stale,
  roomsStale,
  onSelect,
  onPickRoom,
  onExpand,
}: MessageProps) {
  if (record.role === "sys") {
    return <div className="sys">{record.text}</div>;
  }

  if (record.role === "user") {
    return (
      <div className="user">
        <span>{record.text}</span>
      </div>
    );
  }

  const rich = record.rich !== false;
  const hasRooms = !!record.roomsView;
  // 结构化列表只把 intro（酒店）/ 卡片（房型）当主视图，正文不再重复整段列表
  const body = record.hotelView
    ? record.hotelView.intro || ""
    : hasRooms
      ? ""
      : record.text;
  const total = record.hotelTotal ?? 0;
  const shown = record.next ?? 0;

  return (
    <div className="bot">
      {hasRooms ? (
        <RoomsView
          view={record.roomsView!}
          stale={roomsStale}
          picked={!!record.roomsPicked}
          onPick={(room, plan) => onPickRoom(index, room, plan)}
        />
      ) : (
        <span>{rich ? <RichText text={body} /> : body}</span>
      )}

      {record.hotelView ? (
        <HotelView view={record.hotelView} stale={stale} onSelect={onSelect} />
      ) : null}

      {hasRooms ? (
        <details className="rooms-text">
          <summary>查看完整文字列表</summary>
          <RichText text={record.text} textOnly />
        </details>
      ) : null}

      {!record.stale && total > shown ? (
        <ExpandButton
          total={total}
          shown={shown}
          progressFrom={record.hotelShown ?? 0}
          run={(onProgress) => onExpand(index, onProgress)}
        />
      ) : null}
    </div>
  );
}
