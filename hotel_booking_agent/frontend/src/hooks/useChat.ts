/**
 * 对话流程 hook：发送、SSE 流式阶段、展开全部、结构化选店、会话存活检查。
 *
 * 这里只做"流程编排"，界面渲染交给组件；任何"列表已更新/会话过期"的
 * 判定都以后端状态码为准（409/404），不猜文本、不按序号选。
 */

import { useRef, useState } from "react";

import { fetchMoreHotels, fetchSessionInfo, selectHotel } from "../api/client";
import { streamChat } from "../api/sse";
import type { HotelItem, HotelView } from "../types";
import type { SessionsApi } from "./useSessions";

export interface ChatApi {
  busy: boolean;
  status: string;
  send: (text: string) => Promise<void>;
  expand: (index: number, onProgress?: (text: string) => void) => Promise<void>;
  pickHotel: (item: HotelItem, revision: string) => Promise<void>;
  checkServerSession: () => Promise<void>;
}

const EXPIRED_NOTICE =
  "⚠️ 服务端会话上下文已过期（30 分钟无活动或服务重启）。历史记录已保留，继续发送会从新上下文开始。";

export function useChat(
  sessions: SessionsApi,
  pushNotice: (text: string, ttlMs?: number) => void,
): ChatApi {
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const busyRef = useRef(false);
  const pickBusyRef = useRef(false);
  /** 会话存活检查序号：发新消息后，旧的检查结果作废 */
  const checkSeqRef = useRef(0);

  const send = async (raw: string): Promise<void> => {
    const text = raw.trim();
    if (!text || busyRef.current) return;
    busyRef.current = true;
    checkSeqRef.current += 1;
    setBusy(true);

    // 新一轮搜索会替换服务端列表：旧消息的"展开全部"先失效
    sessions.markExpandersStale();
    sessions.remember("user", text);
    sessions.scrollToBottom();
    setStatus("正在连接助手…");

    let reply: string | null = null;
    let currentOrder = "";
    let hotelView: HotelView | null = null;
    let hotelTotal = 0;
    let hotelShown = 0;
    let errorMessage = "";

    try {
      for await (const ev of streamChat({
        user_input: text,
        session_id: sessions.activeIdRef.current ?? "",
      })) {
        if (ev.event === "stage") {
          setStatus("⏳ " + ev.data.text);
        } else if (ev.event === "result") {
          const data = ev.data;
          reply = data.reply;
          currentOrder = data.current_order || "";
          hotelTotal = data.hotel_total || 0;
          hotelShown = data.hotel_shown || 0;
          hotelView = data.hotel_view || null;
          if (data.session_id && data.session_id !== sessions.activeIdRef.current) {
            sessions.adoptSession(data.session_id);
          }
        } else if (ev.event === "error") {
          errorMessage = ev.data.message;
        }
      }
    } catch (e) {
      errorMessage = e instanceof Error ? e.message : String(e);
    }

    setStatus("");

    if (errorMessage) {
      sessions.remember("bot", "❌ " + errorMessage, { rich: false });
    } else if (reply !== null) {
      if (hotelView) {
        // 结构化列表：文案只放 intro，卡片按 hotel_id 渲染并可点选
        sessions.remember("bot", reply, {
          rich: true,
          hotelTotal,
          hotelShown,
          next: (hotelView.items || []).length,
          hotelView,
        });
        sessions.setActiveRevision(hotelView.revision || null);
      } else {
        sessions.remember("bot", reply, {
          rich: true,
          hotelTotal,
          hotelShown,
          next: hotelShown,
        });
      }
      if (currentOrder) sessions.remember("sys", "📌 " + currentOrder);
    } else {
      sessions.remember("bot", "(连接中断，未收到完整回复)", { rich: false });
    }

    sessions.scrollToBottom();
    setStatus("");
    setBusy(false);
    busyRef.current = false;
  };

  const expand = async (
    index: number,
    onProgress?: (text: string) => void,
  ): Promise<void> => {
    const first = sessions.session()?.messages[index];
    if (!first || !first.hotelView) return;

    const total = first.hotelTotal || first.hotelView.total;
    const revision = first.hotelView.revision || "";
    let offset = first.next ?? first.hotelView.items.length;

    while (offset < total) {
      const current = sessions.session()?.messages[index];
      if (!current || current.stale) return; // 用户已发新消息，搜索结果被替换
      onProgress?.(`⏳ 正在补全评分…（已展示 ${offset}/${total} 家）`);

      const result = await fetchMoreHotels(
        sessions.activeIdRef.current ?? "",
        offset,
        5,
        revision,
      );

      if (result.kind === "stale") {
        sessions.setActiveRevision(null);
        sessions.remember("sys", "⚠️ 酒店列表已更新，请按最新列表重新选择。");
        return;
      }
      if (result.kind === "missing") {
        sessions.setActiveRevision(null);
        sessions.remember("sys", "⚠️ 会话已过期，请重新搜索。");
        return;
      }
      if (result.kind === "error") throw new Error(result.detail);

      const data = result.data;
      if (!data.next || data.next <= offset) {
        // 防御：后端没推进就退出，避免死循环
        if (data.items?.length) {
          sessions.appendHotelItems(index, data.items, data.next || offset);
        }
        break;
      }
      sessions.appendHotelItems(index, data.items || [], data.next);
      offset = data.next;
      sessions.scrollToBottom();
    }
  };

  const pickHotel = async (
    item: HotelItem,
    revision: string,
  ): Promise<void> => {
    if (pickBusyRef.current || !item.hotel_id) return;
    pickBusyRef.current = true;
    try {
      const result = await selectHotel(
        sessions.activeIdRef.current ?? "",
        item.hotel_id,
        revision,
      );

      if (result.kind === "stale") {
        // 旧列表：后端拒绝按位置猜，前端把旧卡片全置灰
        sessions.setActiveRevision(null);
        sessions.remember("sys", "⚠️ 酒店列表已更新，请按最新列表重新选择。");
        return;
      }
      if (result.kind === "missing") {
        sessions.setActiveRevision(null);
        sessions.remember(
          "sys",
          result.detail || "⚠️ 会话已过期或该酒店不在当前列表，请重新搜索。",
        );
        return;
      }
      if (result.kind === "error") {
        sessions.remember("sys", "❌ 选择失败：" + result.detail);
        return;
      }

      const data = result.data;
      sessions.remember("sys", `👉 已选择：${item.name || item.hotel_id}`);
      sessions.remember("bot", data.reply, {
        rich: true,
        hotelTotal: 0,
        hotelShown: 0,
        next: 0,
      });
      if (data.current_order) sessions.remember("sys", "📌 " + data.current_order);
      // 已进入选房阶段，旧酒店卡片不再可点
      sessions.setActiveRevision(null);
      sessions.scrollToBottom();
    } finally {
      pickBusyRef.current = false;
    }
  };

  const checkServerSession = async (): Promise<void> => {
    const current = sessions.session();
    if (!current || !current.messages.length) return;

    const id = current.id;
    const seq = (checkSeqRef.current += 1);
    const info = await fetchSessionInfo(id);

    if (seq !== checkSeqRef.current) return; // 等待期间已发送新消息
    if (sessions.activeIdRef.current !== id) return; // 等待期间已切走
    if (!info) return;

    if (!info.exists) {
      pushNotice(EXPIRED_NOTICE);
      sessions.setActiveRevision(null); // 会话没了：旧列表不能再点
    } else {
      // 服务端仍有列表：版本一致的卡片保持可点，旧版本全部置灰
      sessions.setActiveRevision(info.search_revision || null);
    }
  };

  return { busy, status, send, expand, pickHotel, checkServerSession };
}
