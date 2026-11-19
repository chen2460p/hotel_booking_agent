/**
 * "展开全部"按钮。
 *
 * 后端每段补 5 家真实评分（约 2-3 秒），这里逐段追加并实时更新进度；
 * 列表被新一轮搜索替换（409 / stale）时按钮随消息一起消失，不需要手工 remove。
 */

import { useState } from "react";

interface ExpandButtonProps {
  total: number;
  shown: number;
  /** 首次落盘时已展示的家数：用来区分"展开全部"和"继续展开" */
  progressFrom: number;
  run: (onProgress: (text: string) => void) => Promise<void>;
}

export function ExpandButton({
  total,
  shown,
  progressFrom,
  run,
}: ExpandButtonProps) {
  const [working, setWorking] = useState(false);
  const [progress, setProgress] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  const label =
    progress ??
    (failed
      ? "展开失败，点击重试"
      : shown > progressFrom
        ? `继续展开（已展示 ${shown}/${total} 家）`
        : `展开全部（共 ${total} 家）`);

  const click = async (): Promise<void> => {
    if (working) return;
    setWorking(true);
    setFailed(false);
    setProgress(null);
    try {
      await run((text) => setProgress(text));
    } catch {
      // 保留已展开部分，允许从断点重试
      setProgress(null);
      setFailed(true);
    } finally {
      setWorking(false);
    }
  };

  return (
    <button
      type="button"
      className="expand-hotels"
      disabled={working}
      onClick={click}
    >
      {label}
    </button>
  );
}
