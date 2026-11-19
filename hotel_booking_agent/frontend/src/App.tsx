/**
 * 页面根组件：顶栏 + 聊天区 + 输入区。
 *
 * 状态分三层：会话栈（useSessions，落 localStorage）、对话流程（useChat，
 * 负责 SSE/展开/选店）、以及本组件的临时提醒（不落盘，刷新即消失）。
 */

import { useEffect, useRef, useState } from "react";

import { fetchHealth } from "./api/client";
import { Composer } from "./components/Composer";
import { MessageList } from "./components/MessageList";
import type { Notice } from "./components/MessageList";
import { TopBar } from "./components/TopBar";
import { useChat } from "./hooks/useChat";
import { useSessions } from "./hooks/useSessions";
import type { HealthPayload } from "./types";

const EXAMPLE =
  "提示：聊天记录按会话保存在本浏览器，点击「查看历史订单」再返回不会丢失；" +
  "顶栏可切换历史会话或新建会话。服务端上下文 30 分钟无活动会过期（历史记录仍保留）。" +
  "图片与链接可直接点击，等待期间可看到实时进度（理解需求 → 搜索酒店 → 获取报价）。";

export default function App() {
  const chatboxRef = useRef<HTMLDivElement | null>(null);
  const [notices, setNotices] = useState<Notice[]>([]);
  const [storage, setStorage] = useState<HealthPayload | null>(null);

  const pushNotice = (text: string, ttlMs?: number): void => {
    const id = Math.random().toString(36).slice(2);
    setNotices((prev) => [...prev, { id, text }]);
    if (ttlMs) {
      window.setTimeout(() => {
        setNotices((prev) => prev.filter((n) => n.id !== id));
      }, ttlMs);
    }
  };
  const clearNotices = (): void => setNotices([]);

  const sessions = useSessions(chatboxRef, pushNotice, clearNotices);
  const chat = useChat(sessions, pushNotice);

  // 启动：确保有一个会话，再问一次服务端上下文还在不在
  useEffect(() => {
    sessions.bootstrap();
    void chat.checkServerSession();
    void fetchHealth().then(setStorage);
    // 只在挂载时跑一次：会话栈在 ref 里，后续操作自己 commit
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 切会话：恢复保存的滚动位置，并重新校验服务端列表版本
  useEffect(() => {
    const el = chatboxRef.current;
    const current = sessions.session();
    if (el) el.scrollTop = current?.scroll || el.scrollHeight;
    void chat.checkServerSession();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessions.activeId]);

  // 离开页面（包括从 /orders 返回再刷新）时保存滚动位置
  useEffect(() => {
    const onHide = (): void => sessions.saveScroll();
    window.addEventListener("pagehide", onHide);
    return () => window.removeEventListener("pagehide", onHide);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const active = sessions.session();

  return (
    <>
      <h1>🏨 酒店预订 Agent</h1>
      <TopBar
        sessions={sessions.store.sessions}
        activeId={sessions.activeId}
        busy={chat.busy}
        storage={storage}
        onSelect={sessions.activateSession}
        onNew={sessions.newSession}
      />
      <MessageList
        messages={active?.messages ?? []}
        activeRevision={sessions.activeRevision}
        status={chat.status}
        notices={notices}
        chatboxRef={chatboxRef}
        onSelect={chat.pickHotel}
        onExpand={chat.expand}
      />
      <Composer busy={chat.busy} onSend={chat.send} />
      <div className="example">{EXAMPLE}</div>
    </>
  );
}
