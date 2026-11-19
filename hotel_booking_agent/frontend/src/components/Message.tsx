/**
 * 一条消息气泡（user / bot / sys）。
 *
 * 与旧版 append() 的 DOM 结构保持一致：.bot > span + .hotel-view + 展开按钮，
 * 这样 templates/index.html 里的样式表可以原样复用，视觉零变化。
 */

import type { HotelItem, MessageRecord } from "../types";
import { ExpandButton } from "./ExpandButton";
import { HotelView } from "./HotelView";
import { RichText } from "./RichText";

interface MessageProps {
  record: MessageRecord;
  index: number;
  stale: boolean;
  onSelect: (item: HotelItem, revision: string) => Promise<void> | void;
  onExpand: (index: number, onProgress?: (text: string) => void) => Promise<void>;
}

export function Message({ record, index, stale, onSelect, onExpand }: MessageProps) {
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
  // 结构化列表只把 intro 放进气泡正文，卡片另外渲染
  const body = record.hotelView ? record.hotelView.intro || "" : record.text;
  const total = record.hotelTotal ?? 0;
  const shown = record.next ?? 0;

  return (
    <div className="bot">
      <span>{rich ? <RichText text={body} /> : body}</span>
      {record.hotelView ? (
        <HotelView view={record.hotelView} stale={stale} onSelect={onSelect} />
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
