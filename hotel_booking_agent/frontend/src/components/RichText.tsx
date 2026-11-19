/**
 * 回复正文的富文本渲染：文本 / 图片 / 链接。
 *
 * 解析在 lib/richText（纯函数，可单测），这里只负责把片段变成 React 元素。
 * 全程不拼 innerHTML，和旧版"DOM API 拼装"一样没有 XSS 面。
 */

import { Fragment, useMemo } from "react";

import { parseRich } from "../lib/richText";
import { LazyImg } from "./LazyImg";

function openInNewTab(url: string): void {
  window.open(url, "_blank", "noopener,noreferrer");
}

export function RichText({ text }: { text: string }) {
  const parts = useMemo(() => parseRich(text), [text]);

  return (
    <>
      {parts.map((part, index) => {
        if (part.type === "text") {
          return <Fragment key={index}>{part.text}</Fragment>;
        }
        if (part.type === "link") {
          return (
            <a
              key={index}
              href={part.url}
              target="_blank"
              rel="noopener noreferrer"
            >
              🔗 {part.url}
            </a>
          );
        }
        if (part.urls.length === 1) {
          const url = part.urls[0];
          return (
            <LazyImg
              key={index}
              className="chat-img"
              src={url}
              onClick={() => openInNewTab(url)}
            />
          );
        }
        // 房型多图：横向图组，可左右滑动
        return (
          <span key={index} className="chat-gallery">
            {part.urls.map((url, i) => (
              <LazyImg
                key={`${i}-${url}`}
                className="chat-img"
                src={url}
                onClick={() => openInNewTab(url)}
              />
            ))}
          </span>
        );
      })}
    </>
  );
}
