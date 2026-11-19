/**
 * 单家酒店的卡片。
 *
 * 只渲染后端结构化字段；"选这家"提交的是 hotel_id + revision，不是序号。
 * 列表过期（revision 对不上）时置灰并改文案，与旧版 markStaleCards 的
 * 视觉效果一致，但状态由上层派生，不用再手工同步 DOM。
 */

import { useState } from "react";

import { hotelMetaLines } from "../lib/format";
import type { HotelItem } from "../types";
import { LazyImg } from "./LazyImg";

interface HotelCardProps {
  item: HotelItem;
  pos: number;
  stale: boolean;
  onSelect: (item: HotelItem) => Promise<void> | void;
}

export function HotelCard({ item, pos, stale, onSelect }: HotelCardProps) {
  const [pending, setPending] = useState(false);

  const pick = async (): Promise<void> => {
    if (stale || pending) return;
    setPending(true);
    try {
      await onSelect(item);
    } finally {
      setPending(false);
    }
  };

  const imageUrl = item.image_url || "";

  return (
    <div
      className={`hotel-card${stale ? " stale" : ""}`}
      data-hotel-id={item.hotel_id || ""}
    >
      {imageUrl ? (
        <LazyImg
          className="hc-thumb"
          src={imageUrl}
          alt={item.name || "酒店实拍图"}
          onClick={() =>
            window.open(imageUrl, "_blank", "noopener,noreferrer")
          }
        />
      ) : null}

      <div className="hc-main">
        <div className="hc-name">
          {pos}. {item.name || "酒店"}
        </div>
        <div className="hc-meta">{hotelMetaLines(item)}</div>
        <div className="hc-actions">
          <button type="button" disabled={stale || pending} onClick={pick}>
            {pending ? "选择中…" : stale ? "列表已更新" : "选这家"}
          </button>
        </div>
      </div>
    </div>
  );
}
