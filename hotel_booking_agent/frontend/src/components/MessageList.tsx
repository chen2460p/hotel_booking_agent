/**
 * 聊天区：消息列表 + 阶段提示 + 临时提醒。
 *
 * 滚动容器 #chatbox 同时是图片懒加载的观察根（rootMargin 300px），
 * 所以 ref 回调里要把它交给 lib/imgQueue。
 */

import type { RefObject } from "react";

import { setScrollRoot } from "../lib/imgQueue";
import type { HotelItem, MessageRecord } from "../types";
import { Message } from "./Message";

const WELCOME =
  '👋 你好！告诉我想订哪的酒店就行，例如"帮我订下周末三亚海景房，预算 1000 以内"。';

export interface Notice {
  id: string;
  text: string;
}

interface MessageListProps {
  messages: MessageRecord[];
  /** undefined = 服务端状态未确认，卡片先保持可点 */
  activeRevision: string | null | undefined;
  status: string;
  notices: Notice[];
  chatboxRef: RefObject<HTMLDivElement | null>;
  onSelect: (item: HotelItem, revision: string) => Promise<void> | void;
  onExpand: (index: number, onProgress?: (text: string) => void) => Promise<void>;
}

export function MessageList({
  messages,
  activeRevision,
  status,
  notices,
  chatboxRef,
  onSelect,
  onExpand,
}: MessageListProps) {
  return (
    <div
      id="chatbox"
      ref={(el) => {
        chatboxRef.current = el;
        setScrollRoot(el);
      }}
    >
      {messages.length === 0 && !status ? (
        <div className="sys">{WELCOME}</div>
      ) : null}

      {messages.map((record, index) => {
        // 列表版本对不上就置灰：新版靠派生状态算，不再遍历 DOM 加 class
        const stale =
          !!record.hotelView &&
          activeRevision !== undefined &&
          activeRevision !== record.hotelView.revision;
        return (
          <Message
            key={index}
            record={record}
            index={index}
            stale={stale}
            onSelect={onSelect}
            onExpand={onExpand}
          />
        );
      })}

      {status ? (
        <div className="bot status">
          <span>{status}</span>
        </div>
      ) : null}

      {notices.map((notice) => (
        <div key={notice.id} className="sys notice">
          {notice.text}
        </div>
      ))}
    </div>
  );
}
