/**
 * REST 接口封装。
 *
 * 统一把「旧列表 409 / 会话过期 404 / 其它错误」翻译成三种结果，
 * 免得每个组件各写一遍 if (r.status === 409)：选店、选房、展开全部
 * 都靠这个区分"列表已更新"和"真出错了"。
 */

import type {
  ChatPayload,
  HealthPayload,
  HotelsMorePayload,
  SessionInfoPayload,
} from "../types";

export type ApiResult<T> =
  | { kind: "ok"; data: T }
  | { kind: "stale"; status: number; detail: string }
  | { kind: "missing"; status: number; detail: string }
  | { kind: "error"; status: number; detail: string };

async function request<T>(url: string, init?: RequestInit): Promise<ApiResult<T>> {
  try {
    const response = await fetch(url, init);
    const data = (await response.json().catch(() => null)) as
      | { detail?: string }
      | null;
    const detail = data?.detail || "";

    if (response.status === 409) {
      return { kind: "stale", status: 409, detail: detail || "列表已更新" };
    }
    if (response.status === 404) {
      return {
        kind: "missing",
        status: 404,
        detail: detail || "会话不存在或已过期，请重新搜索",
      };
    }
    if (!response.ok) {
      return {
        kind: "error",
        status: response.status,
        detail: detail || `HTTP ${response.status}`,
      };
    }
    return { kind: "ok", data: data as T };
  } catch (e) {
    return {
      kind: "error",
      status: 0,
      detail: e instanceof Error ? e.message : String(e),
    };
  }
}

function jsonBody(body: unknown): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

/** 会话是否还活着 + 服务端当前的列表版本 */
export async function fetchSessionInfo(
  sessionId: string,
): Promise<SessionInfoPayload | null> {
  const result = await request<SessionInfoPayload>(
    `/api/session/${encodeURIComponent(sessionId)}`,
    { cache: "no-store" },
  );
  return result.kind === "ok" ? result.data : null;
}

/** 结构化选店：只提交 hotel_id + revision，绝不按序号猜 */
export function selectHotel(
  sessionId: string,
  hotelId: string,
  revision: string,
): Promise<ApiResult<ChatPayload>> {
  return request<ChatPayload>(
    `/api/session/${encodeURIComponent(sessionId)}/select`,
    jsonBody({ hotel_id: hotelId, revision }),
  );
}

/** 展开全部：按 offset 逐段补全（后端每次补 5 家真实评分） */
export function fetchMoreHotels(
  sessionId: string,
  offset: number,
  limit: number,
  revision: string,
): Promise<ApiResult<HotelsMorePayload>> {
  return request<HotelsMorePayload>(
    "/api/hotels/more",
    jsonBody({ session_id: sessionId, offset, limit, revision }),
  );
}

/** 顶栏的存储标识（memory / sqlite / mysql） */
export async function fetchHealth(): Promise<HealthPayload | null> {
  const result = await request<HealthPayload>("/api/health", { cache: "no-store" });
  return result.kind === "ok" ? result.data : null;
}
