/**
 * 对话流（SSE）。
 *
 * 注意：/api/chat/stream 是 POST + SSE，EventSource 只能 GET 且不能带 body，
 * 所以必须用 fetch + ReadableStream 手动解帧（旧版就是这么做的）。
 * 这里是全页唯一的 SSE 解析入口，帧边界、跨 chunk 的中文都由它兜住。
 */

import type { ChatPayload, StagePayload } from "../types";

export type StreamEvent =
  | { event: "stage"; data: StagePayload }
  | { event: "result"; data: ChatPayload }
  | { event: "error"; data: { message: string } }
  | { event: "message"; data: unknown };

const ENDPOINT = "/api/chat/stream";

export async function* streamChat(
  body: { user_input: string; session_id: string },
  signal?: AbortSignal,
): AsyncGenerator<StreamEvent> {
  const response = await fetch(ENDPOINT, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok || !response.body) {
    throw new Error(`HTTP ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    // stream: true 不能省：中文/emoji 可能被切在两个 chunk 之间
    buffer += decoder.decode(value, { stream: true });

    let boundary = buffer.indexOf("\n\n"); // SSE 帧以空行分隔
    while (boundary >= 0) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);

      let event = "message";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }

      if (data) {
        yield { event, data: JSON.parse(data) } as StreamEvent;
      }
      boundary = buffer.indexOf("\n\n");
    }
  }
}
