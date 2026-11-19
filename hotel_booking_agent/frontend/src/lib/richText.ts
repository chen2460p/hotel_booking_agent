/**
 * 富文本解析：把回复正文里的 URL 切成 文本 / 图片 / 链接 三类片段。
 *
 * 旧版 renderRich 是边走 DOM 边分组图片，迁移后拆成纯函数：
 * 解析结果可以直接快照测试，也不用担心 React 的元素复用。
 * 仍然不拼 innerHTML —— 文本走文本节点、图片走 <img>，XSS 面为零。
 */

export type Token =
  | { type: "text"; text: string }
  | { type: "img"; url: string }
  | { type: "link"; url: string };

/** 渲染单元：图片可能被合并成一个横向图组 */
export type Part =
  | { type: "text"; text: string }
  | { type: "link"; url: string }
  | { type: "images"; urls: string[] };

// 扩展名后允许 CDN 尾巴（如 .jpg-t、?x-oss-…）
const URL_RE = /https?:\/\/[^\s，。；、！？（）)】"'<>]+/g;
const IMG_RE = /\.(jpe?g|png|webp|gif)[^\s]*$/i;

/** 按 URL 切分正文；图片判定与后端返回的图片地址保持一致 */
export function tokenize(text: string): Token[] {
  const out: Token[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  URL_RE.lastIndex = 0;
  while ((m = URL_RE.exec(text)) !== null) {
    if (m.index > last) out.push({ type: "text", text: text.slice(last, m.index) });
    const url = m[0];
    out.push(IMG_RE.test(url) ? { type: "img", url } : { type: "link", url });
    last = m.index + url.length;
  }
  if (last < text.length) out.push({ type: "text", text: text.slice(last) });
  return out;
}

/**
 * 把连续的图片合并成一个图组（房型一次返回几十张图时不至于把消息拉成长条）。
 *
 * 与旧版 groupGalleries 的取舍一致：图与图之间的空白丢弃，单张图前后的文本保留。
 */
export function groupImages(tokens: Token[]): Part[] {
  const parts: Part[] = [];
  let i = 0;
  while (i < tokens.length) {
    const t = tokens[i];
    if (t.type !== "img") {
      parts.push(t);
      i++;
      continue;
    }
    const urls = [t.url];
    let j = i + 1;
    let gap = ""; // 图片之后、下一个非空白片段之前的空白
    while (j < tokens.length) {
      const n = tokens[j];
      if (n.type === "text" && !n.text.trim()) {
        gap += n.text;
        j++;
        continue;
      }
      if (n.type === "img") {
        urls.push(n.url);
        gap = "";
        j++;
        continue;
      }
      break;
    }
    parts.push({ type: "images", urls });
    // 图组之间的空白不要（旧版也是删掉的）；单张图后面的空白保留
    if (urls.length === 1 && gap) parts.push({ type: "text", text: gap });
    i = j;
  }
  return parts;
}

/** 一步到位：正文 → 渲染单元 */
export function parseRich(text: string): Part[] {
  return groupImages(tokenize(text));
}
