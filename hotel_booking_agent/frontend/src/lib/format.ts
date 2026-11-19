/**
 * 纯展示逻辑：酒店卡片文案、会话下拉标签。
 *
 * 这些函数不碰 DOM、不碰存储，方便直接单测；文案必须与旧版
 * templates/index.html 的 _hcMetaLines / sessionLabel 逐字一致，
 * 避免迁移后用户看到措辞变化。
 */

import type { HotelItem, SessionRecord } from "../types";

/** 酒店卡片正文（多行文本，靠 CSS white-space: pre-line 换行） */
export function hotelMetaLines(item: HotelItem): string {
  const star = item.star ? `${item.star}星` : "星级未知";
  const price = item.price_per_night
    ? `¥${item.price_per_night}/晚`
    : "价格以实际预订为准";

  let rating = "暂无评分";
  if (item.rating) {
    rating = `评分 ${item.rating}`;
    if (item.review_count) rating += `（${item.review_count}条）`;
    if (item.rating_source) rating += ` · ${item.rating_source}`;
  }

  const dist =
    item.distance_m && item.distance_to
      ? ` · 距${item.distance_to}约${Math.round(item.distance_m / 10) * 10}米`
      : "";

  const lines = [
    `${star} · ${price}`,
    `${rating}${dist}`,
    `地址：${item.address || "待补充"}`,
  ];
  if (item.facilities && item.facilities.length) {
    lines.push(`设施：${item.facilities.join("、")}`);
  }
  if (item.room_types && item.room_types.length) {
    lines.push(`房型：${item.room_types.join("、")}`);
  }
  return lines.join("\n");
}

/** 会话下拉里显示的名字：取第一条用户消息，超长截断 */
export function sessionLabel(session: SessionRecord): string {
  const first = session.messages.find((m) => m.role === "user");
  if (!first) return "新会话";
  const text = first.text.replace(/\s+/g, " ").trim();
  return text.length > 14 ? text.slice(0, 14) + "…" : text;
}

/** 会话下拉里的时间戳：M/D HH:MM */
export function sessionStamp(ts: number): string {
  const d = new Date(ts || Date.now());
  const mm = String(d.getMonth() + 1);
  const dd = String(d.getDate());
  const hh = String(d.getHours()).padStart(2, "0");
  const mi = String(d.getMinutes()).padStart(2, "0");
  return `${mm}/${dd} ${hh}:${mi}`;
}
