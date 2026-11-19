/**
 * 顶栏：存储标识、订单入口、会话切换。
 *
 * 存储标识原来由 Jinja 注入，改成构建后的静态页后只能问后端：
 * GET /api/health 已经返回 storage_backend / storage_enabled。
 */

import { sessionLabel, sessionStamp } from "../lib/format";
import type { HealthPayload, SessionRecord } from "../types";

interface TopBarProps {
  sessions: SessionRecord[];
  activeId: string;
  busy: boolean;
  storage: HealthPayload | null;
  onSelect: (id: string) => void;
  onNew: () => void;
}

function storageTag(storage: HealthPayload | null): {
  className: string;
  text: string;
} {
  if (!storage) return { className: "", text: "存储：检查中…" };
  const backend = storage.storage_backend;
  const className = backend === "memory" ? "mem" : backend === "sqlite" ? "sql" : "mys";
  const mode = storage.storage_enabled ? "真实持久化" : "内存回退";
  return { className, text: `存储：${backend}（${mode}）` };
}

export function TopBar({
  sessions,
  activeId,
  busy,
  storage,
  onSelect,
  onNew,
}: TopBarProps) {
  const tag = storageTag(storage);
  // 最近活动的排前面
  const ordered = [...sessions].sort(
    (a, b) => (b.updatedAt || 0) - (a.updatedAt || 0),
  );

  return (
    <div className="topbar">
      <span className={`tag ${tag.className}`}>{tag.text}</span>
      <a href="/orders">📋 查看历史订单</a>
      <span className="spacer" />
      <label htmlFor="session-select">会话</label>
      <select
        id="session-select"
        title="切换历史会话"
        value={activeId}
        // 请求期间禁止切会话：回复必须落到当前这段对话里
        disabled={busy}
        onChange={(e) => onSelect(e.target.value)}
      >
        {ordered.map((s) => (
          <option key={s.id} value={s.id}>
            {`${sessionLabel(s)} · ${sessionStamp(s.updatedAt || s.createdAt)}`}
          </option>
        ))}
      </select>
      <button
        id="new-session"
        className="mini"
        type="button"
        title="开始一段新的对话"
        disabled={busy}
        onClick={onNew}
      >
        ＋ 新建会话
      </button>
    </div>
  );
}
