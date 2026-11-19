/**
 * 会话栈的纯函数操作。
 *
 * 旧版这些逻辑混在 DOM 流程里（ensureSession / remember / trimSessions /
 * activateSession / adoptServerSession），迁移后抽成纯函数：
 * 输入旧 store，返回新 store，方便直接单测，也避免 React 里的隐式共享修改。
 */

import type {
  HotelItem,
  MessageRecord,
  SessionRecord,
  SessionStore,
} from "../types";
import { readSid, uid } from "./sessionStore";

export const MAX_SESSIONS = 20; // 本浏览器最多保留 20 段会话
export const MAX_MESSAGES = 200; // 单段会话最多保留 200 条消息

export type MessagePatch =
  | Partial<MessageRecord>
  | ((record: MessageRecord) => Partial<MessageRecord>);

export function findSession(
  store: SessionStore,
  id: string,
): SessionRecord | undefined {
  return store.sessions.find((s) => s.id === id);
}

export function activeSession(store: SessionStore): SessionRecord | undefined {
  return findSession(store, store.active);
}

export function newSessionRecord(id: string): SessionRecord {
  const now = Date.now();
  return { id, createdAt: now, updatedAt: now, scroll: 0, messages: [] };
}

/** 取当前会话；一个都没有时新建（沿用旧版 hb_session_id） */
export function ensureActive(store: SessionStore): {
  store: SessionStore;
  session: SessionRecord;
} {
  const current = activeSession(store);
  if (current) return { store, session: current };
  const session = newSessionRecord(readSid() || uid());
  return {
    store: { active: session.id, sessions: [session, ...store.sessions] },
    session,
  };
}

/** 对当前会话做一次变换，返回新 store 与新会话 */
export function mapActive(
  store: SessionStore,
  fn: (session: SessionRecord) => SessionRecord,
): { store: SessionStore; session: SessionRecord } {
  const base = ensureActive(store);
  const next = fn(base.session);
  return {
    store: {
      active: next.id,
      sessions: base.store.sessions.map((s) =>
        s.id === base.session.id ? next : s,
      ),
    },
    session: next,
  };
}

/** 追加一条消息（超长时从头部裁剪），返回新消息在数组里的下标 */
export function appendMessage(
  store: SessionStore,
  record: MessageRecord,
): { store: SessionStore; session: SessionRecord; index: number } {
  const result = mapActive(store, (s) => {
    const messages = [...s.messages, record];
    if (messages.length > MAX_MESSAGES) {
      messages.splice(0, messages.length - MAX_MESSAGES);
    }
    return { ...s, messages, updatedAt: Date.now() };
  });
  return {
    store: result.store,
    session: result.session,
    index: result.session.messages.length - 1,
  };
}

/** 修改某条消息（展开全部回写 items/next、清 stale 等） */
export function patchMessage(
  store: SessionStore,
  index: number,
  patch: MessagePatch,
  touch = true,
): SessionStore {
  const result = mapActive(store, (s) => ({
    ...s,
    messages: s.messages.map((m, i) => {
      if (i !== index) return m;
      const delta = typeof patch === "function" ? patch(m) : patch;
      return { ...m, ...delta };
    }),
    updatedAt: touch ? Date.now() : s.updatedAt,
  }));
  return result.store;
}

/** 展开全部：把新一批酒店追加进这条消息的结构化快照 */
export function appendHotelItems(
  store: SessionStore,
  index: number,
  items: HotelItem[],
  next: number,
): SessionStore {
  return patchMessage(
    store,
    index,
    (m) => {
      if (!m.hotelView) return {};
      return {
        hotelView: {
          ...m.hotelView,
          items: [...m.hotelView.items, ...items],
        },
        next,
        hotelShown: Math.max(m.hotelShown || 0, next),
      };
    },
    false,
  );
}

/** 新一轮搜索：旧消息的"展开全部"全部失效（按钮不再渲染） */
export function markExpandersStale(store: SessionStore): {
  store: SessionStore;
  changed: boolean;
} {
  let changed = false;
  const sessions = store.sessions.map((s) => {
    const next = s.messages.map((m) => {
      if (m.hotelTotal && !m.stale) {
        changed = true;
        return { ...m, stale: true };
      }
      return m;
    });
    return changed ? { ...s, messages: next } : s;
  });
  return { store: changed ? { ...store, sessions } : store, changed };
}

/** 切换会话（滚动位置先由调用方保存好） */
export function activate(store: SessionStore, id: string): SessionStore {
  return { ...store, active: id };
}

/** 新建会话；超出上限时丢最旧的 */
export function createSession(store: SessionStore): {
  store: SessionStore;
  session: SessionRecord;
} {
  const session = newSessionRecord(uid());
  let sessions = [session, ...store.sessions];
  if (sessions.length > MAX_SESSIONS) {
    const others = sessions.filter((s) => s.id !== session.id);
    while (sessions.length > MAX_SESSIONS && others.length) {
      const victim = others.pop()!;
      sessions = sessions.filter((s) => s.id !== victim.id);
    }
  }
  return { store: { active: session.id, sessions }, session };
}

/** 服务端生成了新的 session_id：就地改名，聊天记录跟着走 */
export function adopt(store: SessionStore, newId: string): SessionStore {
  return {
    active: newId,
    sessions: store.sessions.map((s) =>
      s.id === store.active ? { ...s, id: newId } : s,
    ),
  };
}

/** 记录当前会话的滚动位置（切会话/离开页面时用） */
export function setScroll(store: SessionStore, top: number): SessionStore {
  return {
    ...store,
    sessions: store.sessions.map((s) =>
      s.id === store.active ? { ...s, scroll: top } : s,
    ),
  };
}

/** 当前会话是否是空会话（"新建会话"时避免叠一个空白会话） */
export function isBlankSession(session: SessionRecord | undefined): boolean {
  return !!session && session.messages.length === 0;
}
