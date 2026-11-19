/**
 * 聊天记录的本地持久化（浏览器 localStorage）。
 *
 * 兼容说明：键名和数据结构沿用旧版（hb_sessions_v1），线上用户浏览器里
 * 已经有数据，所以字段只允许增加、不允许改名。旧版只存了 hb_session_id 的
 * 用户也要能平滑过渡（见 readStore 里的 legacy 分支）。
 */

import type { SessionStore } from "../types";

export const STORE_KEY = "hb_sessions_v1";
export const SID_KEY = "hb_session_id";

function safeGet(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function safeSet(key: string, value: string): boolean {
  try {
    localStorage.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}

export function uid(): string {
  return typeof crypto !== "undefined" && crypto.randomUUID
    ? crypto.randomUUID()
    : "s-" + Date.now() + "-" + Math.random().toString(36).slice(2);
}

export function readSid(): string {
  return safeGet(SID_KEY) || "";
}

export function writeSid(id: string): void {
  safeSet(SID_KEY, id);
}

/** 读取本地会话栈；数据被用户/插件改坏时重建，不抛异常 */
export function readStore(): SessionStore {
  try {
    const raw = safeGet(STORE_KEY);
    if (raw) {
      const data = JSON.parse(raw) as SessionStore;
      if (data && Array.isArray(data.sessions)) {
        data.sessions = data.sessions.filter(
          (s) => s && typeof s.id === "string" && s.id,
        );
        for (const s of data.sessions) {
          if (!Array.isArray(s.messages)) s.messages = [];
          s.messages = s.messages.filter(
            (m) => m && typeof m.text === "string",
          );
        }
        if (!data.active || !data.sessions.some((s) => s.id === data.active)) {
          // 兼容旧版只存了 hb_session_id 的浏览器
          const legacy = readSid();
          data.active = data.sessions.some((s) => s.id === legacy)
            ? legacy
            : data.sessions[0]?.id ?? "";
        }
        return data;
      }
    }
  } catch (e) {
    console.warn("会话历史解析失败，已重建：", e);
  }
  return { active: "", sessions: [] };
}

/**
 * 写入本地存储，并在配额满时尽力保住当前会话。
 *
 * 返回真正写进去的那份数据：调用方要拿它回填 state，否则内存和
 * localStorage 会不一致（下次刷新就"丢消息"）。
 */
export function persist(store: SessionStore): SessionStore {
  if (safeSet(STORE_KEY, JSON.stringify(store))) return store;

  // 浏览器配额满：先丢最旧的非当前会话，再压缩当前会话
  const copy: SessionStore = {
    active: store.active,
    sessions: store.sessions.map((s) => ({ ...s })),
  };
  while (copy.sessions.length > 1) {
    let victim = -1;
    for (let i = copy.sessions.length - 1; i >= 0; i--) {
      if (copy.sessions[i].id !== copy.active) {
        victim = i;
        break;
      }
    }
    if (victim < 0) break;
    copy.sessions.splice(victim, 1);
    if (safeSet(STORE_KEY, JSON.stringify(copy))) return copy;
  }

  const current = copy.sessions.find((s) => s.id === copy.active);
  if (current && current.messages.length > 50) {
    current.messages = current.messages.slice(-50);
    if (safeSet(STORE_KEY, JSON.stringify(copy))) return copy;
  }

  console.warn("聊天记录保存失败：浏览器本地存储空间不足");
  return store;
}
