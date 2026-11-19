/**
 * 会话栈 hook：把 localStorage 里的多会话数据升成 React 状态。
 *
 * 所有写操作都走 commit()：先落盘（配额满时 persist 会裁掉最旧会话），
 * 再用"真正写进去的那份"回填 state —— 内存与 localStorage 永远一致。
 * 切会话前会保存滚动位置并清空图片队列（旧 DOM 已经销毁）。
 */

import { useRef, useState } from "react";
import type { RefObject } from "react";

import { resetImageQueue } from "../lib/imgQueue";
import {
  activeSession,
  activate,
  adopt,
  appendHotelItems as appendItems,
  appendMessage,
  createSession,
  ensureActive,
  isBlankSession,
  markExpandersStale as staleExpanders,
  patchMessage,
  setScroll,
} from "../lib/sessionOps";
import { persist, readStore, writeSid } from "../lib/sessionStore";
import type { MessagePatch } from "../lib/sessionOps";
import type {
  HotelItem,
  MessageRecord,
  Role,
  SessionRecord,
  SessionStore,
} from "../types";

export interface SessionsApi {
  store: SessionStore;
  activeId: string;
  activeIdRef: RefObject<string>;
  session: () => SessionRecord | undefined;
  /** undefined = 服务端状态未确认（不置灰）；null = 没有可点的列表（全灰） */
  activeRevision: string | null | undefined;
  setActiveRevision: (revision: string | null | undefined) => void;
  bootstrap: () => void;
  remember: (
    role: Role,
    text: string,
    extra?: Partial<MessageRecord>,
  ) => { index: number; record: MessageRecord };
  patch: (index: number, delta: MessagePatch) => void;
  appendHotelItems: (index: number, items: HotelItem[], next: number) => void;
  markExpandersStale: () => void;
  activateSession: (id: string) => void;
  newSession: () => void;
  adoptSession: (newId: string) => void;
  saveScroll: () => void;
  scrollToBottom: () => void;
}

export function useSessions(
  chatboxRef: RefObject<HTMLDivElement | null>,
  pushNotice: (text: string, ttlMs?: number) => void,
  clearNotices: () => void,
): SessionsApi {
  const [store, setStore] = useState<SessionStore>(() => readStore());
  const storeRef = useRef(store);
  const [activeRevision, setActiveRevision] = useState<
    string | null | undefined
  >(undefined);

  const activeId = store.active;
  const activeIdRef = useRef(activeId);
  activeIdRef.current = activeId;

  /** 落盘 → 用落盘结果回填内存（配额裁剪后两边仍然一致） */
  const commit = (next: SessionStore): void => {
    const saved = persist(next);
    storeRef.current = saved;
    setStore(saved);
  };

  const session = (): SessionRecord | undefined => activeSession(storeRef.current);

  const saveScroll = (): void => {
    const el = chatboxRef.current;
    if (!el) return;
    commit(setScroll(storeRef.current, el.scrollTop));
  };

  const scrollToBottom = (): void => {
    window.requestAnimationFrame(() => {
      const el = chatboxRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    });
  };

  const bootstrap = (): void => {
    const { store: next } = ensureActive(storeRef.current);
    commit(next);
    writeSid(next.active);
  };

  const remember = (
    role: Role,
    text: string,
    extra: Partial<MessageRecord> = {},
  ): { index: number; record: MessageRecord } => {
    const record: MessageRecord = { role, text, ts: Date.now(), ...extra };
    const result = appendMessage(storeRef.current, record);
    commit(result.store);
    return { index: result.index, record };
  };

  const patch = (index: number, delta: MessagePatch): void => {
    commit(patchMessage(storeRef.current, index, delta));
  };

  const appendHotelItems = (
    index: number,
    items: HotelItem[],
    next: number,
  ): void => {
    commit(appendItems(storeRef.current, index, items, next));
  };

  const markExpandersStale = (): void => {
    commit(staleExpanders(storeRef.current).store);
  };

  const activateSession = (id: string): void => {
    if (id === storeRef.current.active) return;
    saveScroll();
    resetImageQueue();
    clearNotices();
    commit(activate(storeRef.current, id));
    writeSid(id);
    setActiveRevision(null);
  };

  const newSession = (): void => {
    if (isBlankSession(session())) {
      // 已经是空白会话，不必再叠一个（旧版的 2.5 秒提示）
      pushNotice("当前已经是新会话，直接输入需求即可。", 2500);
      return;
    }
    saveScroll();
    resetImageQueue();
    clearNotices();
    const { store: next } = createSession(storeRef.current);
    commit(next);
    writeSid(next.active);
    setActiveRevision(null);
  };

  const adoptSession = (newId: string): void => {
    // 服务端回传了新的 session_id：就地改名，聊天记录跟着走
    commit(adopt(storeRef.current, newId));
    writeSid(newId);
  };

  return {
    store,
    activeId,
    activeIdRef,
    session,
    activeRevision,
    setActiveRevision,
    bootstrap,
    remember,
    patch,
    appendHotelItems,
    markExpandersStale,
    activateSession,
    newSession,
    adoptSession,
    saveScroll,
    scrollToBottom,
  };
}
