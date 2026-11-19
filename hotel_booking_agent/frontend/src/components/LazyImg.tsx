/**
 * 懒加载图片。全页唯一的图片渲染入口。
 *
 * src 不交给 React：由 lib/imgQueue 串行加载（道旅 CDN 并发会 403）。
 * 重试后仍失败就退化成可点链接，保证"图挂了也不至于开天窗"。
 */

import { useEffect, useRef, useState } from "react";

import { observeImage } from "../lib/imgQueue";

interface LazyImgProps {
  src: string;
  alt?: string;
  className?: string;
  onClick?: () => void;
}

export function LazyImg({ src, alt, className, onClick }: LazyImgProps) {
  const imgRef = useRef<HTMLImageElement | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const img = imgRef.current;
    if (!img || !src) return;
    observeImage(img, src, () => setFailed(true));
  }, [src]);

  if (failed) {
    return (
      <a href={src} target="_blank" rel="noopener noreferrer">
        {src}
      </a>
    );
  }

  return (
    <img
      ref={imgRef}
      className={className}
      alt={alt || "酒店实拍图"}
      // 道旅 CDN 防盗链：必须逐图声明不发送 Referer
      referrerPolicy="no-referrer"
      onClick={onClick}
    />
  );
}
