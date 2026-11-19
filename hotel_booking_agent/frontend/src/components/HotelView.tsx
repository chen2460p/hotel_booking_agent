/**
 * 酒店列表视图：卡片列表 + 结尾引导语。
 *
 * 新版"展开全部"把新卡片插在引导语之前，这里靠 items 数组增长天然实现，
 * 不再像旧版那样 insertBefore 找 .hv-outro。
 */

import type { HotelItem, HotelView as HotelViewData } from "../types";
import { HotelCard } from "./HotelCard";

interface HotelViewProps {
  view: HotelViewData;
  stale: boolean;
  onSelect: (item: HotelItem, revision: string) => Promise<void> | void;
}

export function HotelView({ view, stale, onSelect }: HotelViewProps) {
  const items = view.items || [];
  const revision = view.revision || "";

  return (
    <div className="hotel-view" data-revision={revision}>
      {items.map((item, index) => (
        <HotelCard
          key={`${index}-${item.hotel_id}`}
          item={item}
          pos={index + 1}
          stale={stale}
          onSelect={(picked) => onSelect(picked, revision)}
        />
      ))}
      {view.outro ? (
        <div className="hv-outro">{view.outro.replace(/^\s+/, "")}</div>
      ) : null}
    </div>
  );
}
