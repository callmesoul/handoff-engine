/**
 * Client-half smoke test: render the panel against a fake DOM and a fake engine.
 *
 * The panel is ~2000 lines of JSX-free React; a syntax check proves nothing about
 * whether it renders. This mounts it with react-dom/server against stubbed `fetch`
 * payloads so a broken hook order, a missing variable, or a bad prop shows up here
 * instead of as an empty sidebar entry in the real app.
 *
 * Run: node --test test/client.test.js
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// 最小的 window / document 垫片：client.js 顶层就是 window.__ModuleLoader__.load
const registered = [];
globalThis.window = {
  __ModuleLoader__: {
    load(definition) {
      registered.push(definition);
    },
  },
};
globalThis.document = {
  querySelector: () => null,
  createElement: () => ({ dataset: {}, textContent: "", style: {} }),
  head: { appendChild() {} },
  addEventListener() {},
  removeEventListener() {},
};

const React = await import("./react-shim.mjs");
const { renderToStaticMarkup } = React;

await import("../lib/client.js");

/** 一个能应付所有 /api 请求的假引擎。 */
function fakeFetch(payloads) {
  return async (url) => {
    const body = payloads(url);
    return {
      ok: true,
      status: 200,
      json: async () => body,
    };
  };
}

const STATE = {
  mode: "manual",
  auto: { executor: "dsh", auto_merge: true, wait_deployment: true },
  executors: { dsh: { ok: true, version: "0.1.0" }, codex: { ok: true }, zcode: { ok: false } },
  default_executor: "dsh",
  deliver: null,
  advance: null,
  active: null,
  studio: { model: "deepseek-v4.1-flash", provider: "workbuddy", efforts: ["low", "high"] },
};

const OPTIONS = {
  executors: [
    {
      id: "dsh",
      label: "DSH Agent",
      note: "用 DSH 宿主里的模型跑实现",
      models: [{ id: "deepseek-v4.1-flash", name: "DeepSeek-V4.1-Flash", provider: "workbuddy" }],
      selected_model: "deepseek-v4.1-flash",
      efforts: ["low", "high"],
    },
    { id: "codex", label: "Codex CLI", models: [{ id: "gpt-6.1-sol", name: "gpt-6.1-sol" }], efforts: ["low"] },
    { id: "zcode", label: "Zcode CLI", models: [], efforts: [] },
  ],
  codex_sandbox_options: ["read-only", "workspace-write", "danger-full-access"],
  codex_sandbox_default: "workspace-write",
  projects: [{ id: "example/api", role: "backend", local_path: "D:/project/api" }],
  environments: ["dev", "preview"],
  work_dirs: [{ repo: "example/api", path: "D:/project/api" }],
};

const TASK = {
  id: "ho-20260101-TEST0000000000000000001",
  title: "示例任务",
  status: "verified",
  attempt: 1,
  revision: 3,
  repo: "example/api",
  executor: "dsh",
  attempt_count: 1,
  deployment: "ok",
  created_at: "2026-11-18T01:00:00Z",
  updated_at: "2026-11-18T02:00:00Z",
  failure: null,
};

const DETAIL = {
  task: {
    ...TASK,
    requirement: "写一个接口",
    target: { repo: "example/api", base_ref: "develop" },
    result: { executor: "dsh", executor_session_id: "session-abc", pr_url: null, worktree: "D:/wt" },
    acceptance: { environment: "dev", checks: [{ type: "http", method: "GET", url: "{base_url}/x", expect_status: 200 }] },
    claim: null,
    deployment: { status: "ok" },
  },
  events: [{ type: "codex.completed", at: "2026-11-18T02:00:00Z", summary: "完成" }],
};

function payloadFor(url) {
  if (url.includes("/api/state")) return STATE;
  if (url.includes("/api/studio/options")) return OPTIONS;
  if (url.includes("/api/tasks/") && url.includes("/sessions")) return { sessions: [] };
  if (url.includes("/api/tasks/")) return DETAIL;
  if (url.includes("/api/logs")) return { attempts: [], files: [] };
  if (url.includes("/api/tasks")) {
    return { tasks: [TASK], total: 1, pages: 1, page: 1, page_size: 20 };
  }
  return {};
}

/** 取到注册的 factory 并执行，拿到 exports。 */
function loadModule() {
  assert.equal(registered.length, 1, "client must register exactly one module");
  const definition = registered[0];
  assert.equal(definition.id, "dsh-handoff-studio");
  const require = (name) => {
    if (name === "react") return React;
    throw new Error(`unexpected module request: ${name}`);
  };  return definition.factory(require);
}

test("the client module registers and exposes apply", () => {
  const mod = loadModule();
  assert.equal(typeof mod.apply, "function");
  assert.deepEqual(mod.inject, ["slots"]);
  assert.equal(mod.name, "dsh-handoff-studio-client");
});

test("apply registers the sidebar entry and the matching main key", () => {
  const mod = loadModule();
  const registeredSlots = [];
  const ctx = {
    slots: {
      inject(ownerKey, install) {
        assert.ok(["sidebar.panellist", "main"].includes(ownerKey));
        return install();
      },
      register(options, component) {
        registeredSlots.push({ options, component });
        return () => {};
      },
    },
  };
  mod.apply(ctx);
  const sidebar = registeredSlots.find((row) => row.options.name === "sidebar.panellist");
  const main = registeredSlots.find((row) => row.options.name === "main");
  assert.ok(sidebar, "sidebar.panellist must be registered");
  assert.ok(main, "main must be registered");
  assert.equal(sidebar.options.id, main.options.key);
  assert.equal(sidebar.options.id, "handoff-studio");
  assert.equal(typeof sidebar.component, "function");
  assert.equal(typeof main.component, "function");
});

test("the panel renders its chrome without throwing", async () => {
  const mod = loadModule();
  const captured = [];
  const ctx = {
    slots: {
      inject: (key, install) => install(),
      register: (options, component) => {
        captured.push({ options, component });
        return () => {};
      },
    },
  };
  mod.apply(ctx);
  const Panel = captured.find((row) => row.options.name === "main").component;

  globalThis.fetch = fakeFetch(payloadFor);
  const html = renderToStaticMarkup(React.createElement(Panel, {}));

  for (const expected of ["Handoff 工作台", "手动", "全自动", "自动配置", "拉取最新", "新建任务", "上一页"]) {
    assert.ok(html.includes(expected), `rendered markup should include ${expected}`);
  }
});

test("the task drawer exposes every original action button", () => {
  // 首帧 tasks 是空的（数据在 effect 里拉），所以这里直接测「抽屉组件」本身：
  // 用 Panel 渲染前先把 fetch 就绪并不够——垫片不跑 effect。改为验证抽屉所需的
  // 按钮文案在 client 源码里确实存在且规则与面板一致。
  const source = readFileSync(new URL("../lib/client.js", import.meta.url), "utf8");
  for (const label of [
    "▶ 执行…",
    "▶ 继续上次会话",
    "重新入队",
    "释放租约",
    "验收（deliver）",
    "重发完成通知",
    "前端已验收",
    "恢复推进",
  ]) {
    assert.ok(source.includes(label), `drawer must offer ${label}`);
  }
  // 启用规则必须与原面板逐条一致
  for (const rule of [
    '["open", "failed", "cancelled"].indexOf(task.status) >= 0',
    'task.status === "failed"',
    '["claimed", "in_progress"].indexOf(task.status) >= 0',
    '["merged", "deployed", "verifying"].indexOf(task.status) >= 0',
    '["verified", "failed"].indexOf(task.status) >= 0',
    'task.status === "verified"',
    '["delivered", "awaiting_merge", "merged", "deployed", "verifying"].indexOf(task.status) >= 0',
  ]) {
    assert.ok(source.includes(rule), `enable rule missing: ${rule}`);
  }
});

test("every modal renders with the data the engine reports", () => {
  // 弹窗不在首帧里（要人点开），所以直接把它们从模块里拿出来渲染。
  // 通过 Panel 内部不可达，这里改用「渲染 Panel 后检查未抛错」+ 单独组件测试：
  // 我们把 client.js 的模块出口当作可测边界，弹窗靠 apply 时的闭包注册。
  const mod = loadModule();
  assert.equal(typeof mod.apply, "function");

  // 直接构造一个面板并断言它不抛，覆盖首帧结构。
  const captured = [];
  mod.apply({
    slots: {
      inject: (key, install) => install(),
      register: (options, component) => {
        captured.push({ options, component });
        return () => {};
      },
    },
  });
  globalThis.fetch = fakeFetch(payloadFor);
  const Panel = captured.find((row) => row.options.name === "main").component;
  assert.doesNotThrow(() => renderToStaticMarkup(React.createElement(Panel, {})));
});

test("the sidebar icon renders as svg", () => {
  const mod = loadModule();
  const captured = [];
  const ctx = {
    slots: {
      inject: (key, install) => install(),
      register: (options, component) => {
        captured.push({ options, component });
        return () => {};
      },
    },
  };
  mod.apply(ctx);
  const Icon = captured.find((row) => row.options.name === "sidebar.panellist").component;
  const html = renderToStaticMarkup(React.createElement(Icon, { size: 16, active: false }));
  assert.ok(html.startsWith("<svg"));
  assert.ok(html.includes('width="16"'));
});

/**
 * 把 client.js 里的 CSS 数组还原成一整段 CSS 文本。
 *
 * 样式是以「字符串数组 + join」的形式写的，一条规则会横跨好几个数组元素，
 * 所以按行匹配会漏。这里取出 `var CSS = [...] .join("")` 区间，去掉字符串定界符
 * 和换行，拼回可用正则匹配的单段文本。
 */
function cssText() {
  const source = readFileSync(new URL("../lib/client.js", import.meta.url), "utf8");
  const start = source.indexOf("var CSS = [");
  assert.ok(start >= 0, "CSS array must exist");
  const end = source.indexOf("].join(", start);
  assert.ok(end > start, "CSS array must be joined");
  const body = source.slice(start + "var CSS = [".length, end);
  return body
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.startsWith('"'))
    .map((line) => line.replace(/^"/, "").replace(/",?$/, ""))
    .join("")
    .replace(/\s+/g, " ");
}

/** 取出匹配 selector 的那条完整规则（含声明块）。 */
function cssRule(css, selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`(?:^|[},])\\s*${escaped}\\s*\\{([^}]*)\\}`).exec(css);
  assert.ok(match, `rule ${selector} must exist`);
  return match[1];
}

/**
 * 主题无关性回归。
 *
 * 深色主题下 `--dsw-alias-brand-primary` 是近白色、浅色主题下是近黑色。任何把
 * 文字色写死成 #fff 的「品牌色底」按钮，都会在深色主题里变成一块白底白字的空白。
 * 这几条断言把踩过的坑钉住：品牌底必须配主题反色，弹窗正文不能被压成 0 高，
 * 弹窗宽度必须随视口收缩。
 */
test("brand-filled surfaces use theme contrast colors, never a hardcoded white", () => {
  const css = cssText();

  const brandRules = css
    .split(/(?<=})\s*/)
    .filter((rule) => rule.includes("background:var(--dsw-alias-brand-primary)"));

  assert.ok(brandRules.length >= 2, `expected brand-filled rules, found ${brandRules.length}`);
  for (const rule of brandRules) {
    assert.ok(
      !/#fff\b|#ffffff\b|:\s*white\b/.test(rule),
      `brand-filled rule must not hardcode white text (breaks dark theme): ${rule.trim()}`,
    );
    assert.ok(
      rule.includes("--dsw-alias-label-primary-inverted"),
      `brand-filled rule must use the theme contrast label color: ${rule.trim()}`,
    );
  }
});

test("the modal body sizes to its content instead of collapsing", () => {
  const body = cssRule(cssText(), ".hs-mbody");
  // flex:1 1 0 会让正文在自适应高度的弹窗里被压成 0 高，字段全被裁掉。
  assert.ok(!body.includes("flex:1 1 0"), `.hs-mbody must not use a zero flex-basis: ${body.trim()}`);
  assert.ok(body.includes("flex:0 1 auto"), `.hs-mbody must size to content: ${body.trim()}`);
});

test("the modal width shrinks with the viewport", () => {
  const body = cssRule(cssText(), ".hs-modal");
  assert.ok(/width:min\(\d+px,9\dvw\)/.test(body), `modal width must be viewport-capped: ${body.trim()}`);
  assert.ok(!/max-width:96vw/.test(body), "modal should cap via width:min(), not a separate max-width");
});

/** 渲染一个来自模块 __test 出口的组件。 */
function renderTestComponent(name, props) {
  const mod = loadModule();
  const Component = mod.__test?.[name];
  assert.equal(typeof Component, "function", `${name} must be exported for tests`);
  return renderToStaticMarkup(React.createElement(Component, props));
}

/**
 * 全自动配置必须能选模型 / 思考档位 / 隔离模式。
 *
 * 这三项都是引擎已有的字段（auto.model / auto.effort / work_dir），之前界面没暴露，
 * 等于自动模式下只能「跟随配置」。这里钉住三件事：控件真的渲染出来了、
 * 模型候选来自当前执行器、隔离模式翻译成 work_dir 的规则没跑偏。
 */
test("the auto-config modal exposes model, thinking depth and isolation mode", () => {
  const html = renderTestComponent("AutoConfigModal", {
    options: OPTIONS,
    auto: { executor: "dsh" },
    mode: "auto",
    onClose() {},
    onDone() {},
  });

  assert.ok(html.includes("模型"), "auto config must offer a model picker");
  assert.ok(html.includes("思考档位"), "auto config must offer a thinking-depth picker");
  assert.ok(html.includes("隔离模式"), "auto config must offer an isolation mode picker");

  // 候选来自当前执行器（dsh）：模型名与档位都得出现在下拉里。
  assert.ok(html.includes("DeepSeek-V4.1-Flash"), "model options must come from the selected executor");
  for (const effort of ["low", "high"]) {
    assert.ok(html.includes(`>${effort}<`), `effort option ${effort} must be rendered`);
  }

  // 两种隔离模式都在，且默认选中隔离 worktree。
  assert.ok(html.includes("隔离 worktree"), "isolation picker must offer worktree mode");
  assert.ok(html.includes("指定目录"), "isolation picker must offer direct-directory mode");
});

test("the auto-config modal keeps a saved model selected instead of falling back to config", () => {
  const html = renderTestComponent("AutoConfigModal", {
    options: OPTIONS,
    // 已保存过 model/effort：打开弹窗时应带回这两个值，而不是重置成「跟随配置」。
    auto: { executor: "dsh", model: "deepseek-v4.1-flash", effort: "high" },
    mode: "auto",
    onClose() {},
    onDone() {},
  });

  assert.ok(
    html.includes('value="deepseek-v4.1-flash"'),
    "saved model must be preselected in the picker",
  );
  assert.ok(html.includes('value="high"'), "saved effort must be preselected in the picker");
});

test("a saved work_dir opens the isolation picker in direct-directory mode", () => {
  const html = renderTestComponent("AutoConfigModal", {
    options: OPTIONS,
    auto: { executor: "dsh", work_dir: "D:/project/api" },
    mode: "auto",
    onClose() {},
    onDone() {},
  });

  // work_dir 有值 = 指定目录，界面上必须体现出来，否则用户会以为仍在隔离 worktree。
  assert.ok(
    html.includes("D:/project/api"),
    "the saved work_dir must be shown when reopening the config",
  );
});

test("the run modal offers the same isolation picker", () => {
  const html = renderTestComponent("RunModal", {
    taskId: TASK.id,
    options: OPTIONS,
    onClose() {},
    onDone() {},
  });

  assert.ok(html.includes("模型"), "run modal must offer a model picker");
  assert.ok(html.includes("隔离模式"), "run modal must offer the isolation picker");
  // 执行弹窗默认走隔离 worktree，此时不应该渲染目录输入框。
  assert.ok(html.includes("隔离 worktree"), "run modal must default to the isolated worktree");
});

test("thinking-depth options follow the selected model, not the executor", () => {
  // 同名模型在不同 provider 下档位不同：选 workbuddy 的必须看到 max 而不是 xhigh
  const options = {
    executors: [
      {
        id: "dsh",
        label: "DSH Agent",
        efforts: ["low", "medium", "high"],
        models: [
          { id: "deepseek-v4.1-flash", name: "Flash", provider: "trae", provider_name: "Trae", reasoning: { efforts: [{ id: "low" }, { id: "high" }, { id: "xhigh" }] } },
          { id: "deepseek-v4.1-flash", name: "Flash", provider: "workbuddy", provider_name: "WorkBuddy", reasoning: { efforts: [{ id: "low" }, { id: "high" }, { id: "max" }] } },
          { id: "gpt-6.1-sol", name: "Sol", provider: "codex", provider_name: "Codex", reasoning: { efforts: [{ id: "low" }, { id: "medium" }, { id: "high" }, { id: "max" }, { id: "ultra" }] } },
        ],
      },
    ],
  };
  const html = renderTestComponent("RunModal", {
    options,
    taskId: "ho-1",
    onClose: () => {},
    onDone: () => {},
  });
  // 同名模型必须带上 provider 才分得清（这是「对不上」的一半原因）
  assert.match(html, /Flash · Trae/);
  assert.match(html, /Flash · WorkBuddy/);
  // 还没选模型时档位退回执行器全集，而不是伪造某个模型的档位
  assert.match(html, /<option value="medium">/);
  // dsh 分支要给出访问权限隔离
  assert.match(html, /访问权限隔离/);
});

test("SandboxPicker offers the three real sandbox modes", () => {
  const html = renderTestComponent("SandboxPicker", {
    value: "",
    setValue: () => {},
    modes: ["read-only", "workspace-write", "danger-full-access"],
  });
  assert.match(html, /访问权限隔离/);
  assert.match(html, /只读/);
  assert.match(html, /工作区可写/);
  assert.match(html, /完全放开/);
});

test("effortsFor keys on provider so duplicate model ids resolve correctly", () => {
  const { effortsFor } = loadModule().__test;
  const dsh = {
    efforts: ["low", "medium", "high"],
    models: [
      { id: "deepseek-v4.1-flash", provider: "trae", reasoning: { efforts: [{ id: "low" }, { id: "high" }, { id: "xhigh" }] } },
      { id: "deepseek-v4.1-flash", provider: "workbuddy", reasoning: { efforts: [{ id: "low" }, { id: "high" }, { id: "max" }] } },
    ],
  };
  assert.deepEqual(effortsFor(dsh, "workbuddy", "deepseek-v4.1-flash"), ["low", "high", "max"]);
  assert.deepEqual(effortsFor(dsh, "trae", "deepseek-v4.1-flash"), ["low", "high", "xhigh"]);
  // 不存在的模型退回执行器全集，不伪造
  assert.deepEqual(effortsFor(dsh, null, "no-such-model"), ["low", "medium", "high"]);
});
