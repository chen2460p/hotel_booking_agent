/**
 * 输入区：Enter 发送、Shift+Enter 换行；请求期间禁发。
 *
 * 发送后立刻清空输入框（想改就重新打），请求结束再把焦点还给输入框，
 * 与旧版 sendMsg 的交互一致。
 */

import { useEffect, useRef, useState } from "react";

interface ComposerProps {
  busy: boolean;
  onSend: (text: string) => Promise<void> | void;
}

export function Composer({ busy, onSend }: ComposerProps) {
  const [draft, setDraft] = useState("");
  const inputRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    if (!busy) inputRef.current?.focus();
  }, [busy]);

  const submit = (): void => {
    if (busy) return;
    const text = draft.trim();
    if (!text) return;
    setDraft("");
    void onSend(text);
  };

  return (
    <div id="inputrow">
      <textarea
        id="input"
        ref={inputRef}
        value={draft}
        placeholder="输入你的需求…（Enter 发送，Shift+Enter 换行）"
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            submit();
          }
        }}
      />
      <button id="send" type="button" disabled={busy} onClick={submit}>
        发送
      </button>
    </div>
  );
}
