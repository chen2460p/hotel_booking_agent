/**
 * 订单页确认框。
 *
 * 替代旧版的 window.confirm / alert：取消真实订单的罚金、预计退款、
 * 凭证有效期要先摆给用户看清楚，再让他点"确认取消"。
 */

import type { ReactNode } from "react";

interface ConfirmDialogProps {
  title: string;
  children: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  busy?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}

export function ConfirmDialog({
  title,
  children,
  confirmLabel,
  danger,
  busy,
  onCancel,
  onConfirm,
}: ConfirmDialogProps) {
  return (
    <div className="overlay" role="dialog" aria-modal="true" aria-label={title}>
      <div className="dialog">
        <h2>{title}</h2>
        <div className="lines">{children}</div>
        <div className="actions">
          <button type="button" className="btn ghost" disabled={busy} onClick={onCancel}>
            再想想
          </button>
          <button
            type="button"
            className={danger ? "btn danger" : "btn"}
            disabled={busy}
            onClick={onConfirm}
          >
            {busy ? "处理中…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
