/**
 * 前后端数据契约的唯一来源。
 *
 * 字段名与后端 web.py 的 _hotel_item / _chat_payload 严格对齐：
 * 卡片只渲染这里声明的结构化字段，绝不解析回复文本（见 AGENTS.md 硬红线 4）。
 */

export type Role = "user" | "bot" | "sys";

/** 单家酒店的结构化视图（后端 _hotel_item） */
export interface HotelItem {
  hotel_id: string;
  name: string;
  star?: number | null;
  price_per_night?: number | null;
  rating?: number | null;
  review_count?: number | null;
  rating_source?: string | null;
  address?: string | null;
  distance_m?: number | null;
  distance_to?: string | null;
  facilities?: string[];
  room_types?: string[];
  image_url?: string | null;
}

/** 酒店列表视图（后端 _hotel_view），revision 用于选店版本校验 */
export interface HotelView {
  revision: string;
  total: number;
  shown: number;
  intro?: string;
  outro?: string;
  items: HotelItem[];
}

/**
 * 一条聊天记录。
 *
 * 字段名是 localStorage 的历史格式（hb_sessions_v1），线上用户浏览器里
 * 已经有数据，迁移时只允许加字段，不允许改名或删字段。
 */
export interface MessageRecord {
  role: Role;
  text: string;
  ts: number;
  rich?: boolean;
  hotelTotal?: number;
  hotelShown?: number;
  /** 已展示到第几家（"展开全部"的断点） */
  next?: number;
  hotelView?: HotelView | null;
  /** 新一轮搜索后旧消息的"展开全部"失效 */
  stale?: boolean;
}

/** 一段会话（本浏览器内），id 同时也是服务端 session_id */
export interface SessionRecord {
  id: string;
  createdAt: number;
  updatedAt: number;
  /** 从订单页返回时恢复的滚动位置 */
  scroll: number;
  messages: MessageRecord[];
}

export interface SessionStore {
  active: string;
  sessions: SessionRecord[];
}

/** POST /api/chat 与 /chat/stream 的 result 事件载荷 */
export interface ChatPayload {
  reply: string;
  current_order?: string;
  session_id?: string;
  storage_backend?: string;
  storage_enabled?: boolean;
  hotel_total?: number;
  hotel_shown?: number;
  stage?: string;
  hotel_view?: HotelView | null;
  rooms_view?: unknown;
}

/** GET /api/session/{id} */
export interface SessionInfoPayload {
  exists: boolean;
  session_id?: string;
  stage?: string;
  current_order?: string;
  hotel_total?: number;
  /** 服务端当前酒店列表版本；用于决定历史卡片是否仍可点 */
  search_revision?: string;
  rooms_revision?: string;
  rooms_view?: unknown;
  last_active?: number;
}

/** POST /api/hotels/more */
export interface HotelsMorePayload {
  revision: string;
  items: HotelItem[];
  next: number;
  total: number;
  done: boolean;
  text?: string;
}

/** GET /api/health（顶栏存储标识用） */
export interface HealthPayload {
  status: string;
  storage_backend: string;
  storage_enabled: boolean;
  active_sessions?: number;
  rollinggo_logged_in?: boolean;
}

/** 后台线程通过 SSE 推送的阶段事件 */
export interface StagePayload {
  stage: string;
  text: string;
}
