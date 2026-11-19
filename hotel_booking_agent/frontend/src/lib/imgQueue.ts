/**
 * 图片串行加载队列。
 *
 * 道旅图片 CDN 有突发并发风控：实测 8 张同时请求 → 5 张 403。
 * 旧版靠"逐张加载 + 失败重试一次 + 退化成链接"绕开，逻辑必须原样保留：
 * 这里是全页唯一的图片加载入口，任何组件都不要再直接给 <img> 赋 src。
 *
 * 与 React 的配合：组件只提供一个还没设置 src 的 <img>，本模块负责
 * 进入队列、进入可视区才加载、失败回调（组件自己改成 <a> 渲染）。
 */

type FailCallback = () => void;

const queue: Array<{ img: HTMLImageElement; url: string }> = [];
const failCallbacks = new WeakMap<HTMLImageElement, FailCallback>();

let busy = false;
let observer: IntersectionObserver | null = null;
let root: Element | null = null;

/** 懒加载的观察根必须是聊天区那个滚动容器，而不是视口 */
export function setScrollRoot(el: Element | null): void {
  if (root === el) return;
  root = el;
  if (observer) {
    observer.disconnect();
    observer = null;
  }
}

/** 切会话时旧 DOM 全部销毁：断开观察器并清空队列，避免队列卡在已移除的节点上 */
export function resetImageQueue(): void {
  if (observer) {
    observer.disconnect();
    observer = null;
  }
  queue.length = 0;
  busy = false;
}

function push(img: HTMLImageElement, url: string): void {
  queue.push({ img, url });
  pump();
}

function pump(): void {
  if (busy || !queue.length) return;
  const { img, url } = queue.shift()!;
  busy = true;

  const finish = (): void => {
    busy = false;
    pump();
  };

  img.onload = finish;
  img.onerror = () => {
    const retried = Number(img.dataset.retry || 0);
    if (retried < 1) {
      // 403 是临时风控，稍候重试一次
      img.dataset.retry = "1";
      window.setTimeout(() => {
        img.src = url;
      }, 800);
      return; // 不 finish：重试结果出来再放行下一张
    }
    failCallbacks.get(img)?.();
    finish();
  };
  img.src = url;
}

/**
 * 登记一张图片：可视区附近才真正请求。
 *
 * @param img 尚未设置 src 的节点（src 由本模块赋值，React 不要接管）
 * @param url 图片地址
 * @param onFail 重试后仍失败时回调（组件改成渲染链接）
 */
export function observeImage(
  img: HTMLImageElement,
  url: string,
  onFail: FailCallback,
): void {
  failCallbacks.set(img, onFail);
  img.dataset.src = url;

  if (!("IntersectionObserver" in window) || !root) {
    // 没有观察器/容器时直接排队，宁可慢一点也不要图片永远不加载
    push(img, url);
    return;
  }

  if (!observer) {
    observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          observer?.unobserve(entry.target);
          const el = entry.target as HTMLImageElement;
          push(el, el.dataset.src || "");
        }
      },
      { root, rootMargin: "300px 0px" },
    );
  }
  observer.observe(img);
}
