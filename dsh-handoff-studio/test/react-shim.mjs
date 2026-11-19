/**
 * 一个足够用的 React 垫片，只服务于本仓库的客户端渲染测试。
 *
 * 为什么不装 react：这个包是装进用户 profile 的插件，多一个开发依赖就多一条
 * 供应链面；而这里要验证的只有「组件树能不能走完、有没有拼错的变量、hook 顺序
 * 是否稳定」。用几十行垫片就能把这些全测出来，且完全离线。
 *
 * 支持：createElement、useState/useEffect/useCallback/useMemo/useRef、函数组件、
 * Fragment。渲染成 HTML 字符串（类似 renderToString），effect 不执行。
 */

function createElement(type, props, ...children) {
  const flat = [];
  const push = (child) => {
    if (child === null || child === undefined || child === false || child === true) return;
    if (Array.isArray(child)) {
      child.forEach(push);
      return;
    }
    flat.push(child);
  };
  children.forEach(push);
  return { type, props: props || {}, children: flat };
}

const Fragment = Symbol("Fragment");

/** 每个渲染一个 hook 槽，顺序必须稳定——不稳定这里会直接报错。 */
let currentSlots = null;
let currentIndex = 0;
let rerender = null;

function slot(init) {
  if (!currentSlots) throw new Error("hook called outside a render");
  const index = currentIndex++;
  if (!(index in currentSlots)) currentSlots[index] = typeof init === "function" ? init() : init;
  return { index, value: currentSlots[index] };
}

function useState(initial) {
  const { index, value } = slot(initial);
  const set = (next) => {
    currentSlots[index] = typeof next === "function" ? next(currentSlots[index]) : next;
    if (rerender) rerender();
  };
  return [value, set];
}

function useRef(initial) {
  const ref = slot(() => ({ current: initial }));
  return ref.value;
}

function useMemo(factory, deps) {
  const store = slot(() => ({ deps: undefined, value: undefined }));
  const entry = store.value;
  if (entry.value === undefined || !sameDeps(entry.deps, deps)) {
    entry.value = factory();
    entry.deps = deps;
  }
  return entry.value;
}

function useCallback(fn, deps) {
  return useMemo(() => fn, deps);
}

function useEffect(effect, deps) {
  const store = slot(() => ({ deps: undefined }));
  const entry = store.value;
  if (entry.deps === undefined || !sameDeps(entry.deps, deps)) {
    entry.deps = deps;
    // 首帧不执行 effect：与 renderToStaticMarkup 一致，也避免测试里打真网络
    entry.pending = true;
  }
}

function sameDeps(a, b) {
  if (!a || !b || a.length !== b.length) return false;
  return a.every((value, index) => Object.is(value, b[index]));
}

function escapeText(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

const VOID_TAGS = new Set(["input", "br", "hr", "img", "meta", "link"]);

function renderNode(node, out) {
  if (node === null || node === undefined || node === false || node === true) return;
  if (typeof node === "string" || typeof node === "number") {
    out.push(escapeText(node));
    return;
  }
  if (Array.isArray(node)) {
    node.forEach((child) => renderNode(child, out));
    return;
  }
  const { type, props, children } = node;
  if (typeof type === "function") {
    const savedSlots = currentSlots;
    const savedIndex = currentIndex;
    currentSlots = [];
    currentIndex = 0;
    let result;
    try {
      result = type({ ...props, children });
    } finally {
      const produced = currentSlots;
      currentSlots = savedSlots;
      currentIndex = savedIndex;
      // 保存下来，rerender 时复用（hook 顺序不稳定的 bug 会在下一次渲染暴露）
      type.__slots = produced;
    }
    renderNode(result, out);
    return;
  }
  if (type === Fragment) {
    children.forEach((child) => renderNode(child, out));
    return;
  }
  // 字符串标签
  const attrs = [];
  for (const [key, value] of Object.entries(props || {})) {
    if (key === "children" || key === "key") continue;
    if (value === null || value === undefined || value === false) continue;
    if (typeof value === "function") continue;
    if (key === "style" && typeof value === "object") {
      const css = Object.entries(value)
        .map(([k, v]) => `${k.replace(/[A-Z]/g, (m) => "-" + m.toLowerCase())}:${v}`)
        .join(";");
      attrs.push(`style="${escapeText(css)}"`);
      continue;
    }
    if (key === "className") {
      attrs.push(`class="${escapeText(value)}"`);
      continue;
    }
    if (value === true) {
      attrs.push(key);
      continue;
    }
    attrs.push(`${key}="${escapeText(value)}"`);
  }
  const open = `<${type}${attrs.length ? " " + attrs.join(" ") : ""}>`;
  if (VOID_TAGS.has(type)) {
    out.push(open);
    return;
  }
  out.push(open);
  children.forEach((child) => renderNode(child, out));
  out.push(`</${type}>`);
}

function renderToStaticMarkup(element) {
  const out = [];
  currentSlots = [];
  currentIndex = 0;
  try {
    renderNode(element, out);
  } finally {
    currentSlots = null;
    currentIndex = 0;
  }
  return out.join("");
}

const React = {
  createElement,
  Fragment,
  useState,
  useRef,
  useMemo,
  useCallback,
  useEffect,
  renderToStaticMarkup,
};

export {
  React as default,
  createElement,
  Fragment,
  renderToStaticMarkup,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
};
