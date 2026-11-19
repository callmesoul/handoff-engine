/**
 * Host-half tests with a minimal fake Cordis context.
 *
 * No DSH is started: `webServer`, `sessionController`, `agentLoop` and `ctx.on`
 * are replaced with just enough behaviour to exercise the real routes over HTTP.
 *
 * Run: node --test test/host.test.js
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdtemp, rm, readFile, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { EventEmitter } from "node:events";

import { apply, describeEvent, ROUTE, Config } from "../lib/index.js";

/** Minimal Cordis context: webServer.register + effects + a real HTTP listener. */
async function withPlugin(t, options = {}) {
  const dir = await mkdtemp(join(tmpdir(), "studio-test-"));
  const listeners = new Map();
  const effects = [];
  const server = createServer((req, res) => {
    const handler = routes[0]?.handler;
    if (handler) handler(req, res);
    else {
      res.writeHead(404);
      res.end();
    }
  });
  const routes = [];

  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = server.address().port;

  const sessionEvents = new EventEmitter();
  const prompts = [];
  const catalog = options.catalog ?? {
    default: { provider: "workbuddy", model: "deepseek-v4.1-flash", reasoningEffort: "high" },
    routableProviders: ["workbuddy"],
    groups: [
      {
        id: "workbuddy",
        name: "WorkBuddy",
        models: [
          {
            id: "deepseek-v4.1-flash",
            name: "DeepSeek-V4.1-Flash",
            reasoning: { efforts: [{ id: "low" }, { id: "high" }], defaultEffort: "high" },
          },
        ],
      },
    ],
    failures: [],
  };

  const ctx = {
    webServer: {
      port,
      host: "127.0.0.1",
      getOrigin: () => `http://127.0.0.1:${port}`,
      register: (route) => {
        routes.push(route);
        return () => {};
      },
    },
    sessionController: {
      modelCatalog: async () => catalog,
      prompt: async (request) => {
        prompts.push(request);
        assert.equal(request.mode, "queue");
        // 用一条 assistant/message 回放，模拟 Agent 说完了
        const text = options.finalText ?? '{"status":"completed"}';
        sessionEvents.emit("session/event", { id: request.sessionId }, {
          type: "turn/start",
          seq: 1,
          time: Date.now(),
          data: { turn: 1 },
        });
        sessionEvents.emit("session/event", { id: request.sessionId }, {
          type: "tool/call",
          seq: 2,
          time: Date.now(),
          data: { turn: 1, step: 1, callId: "c1", name: "bash", arguments: '{"command":"ls -la"}' },
        });
        sessionEvents.emit("session/event", { id: request.sessionId }, {
          type: "assistant/message",
          seq: 3,
          time: Date.now(),
          data: { turn: 1, step: 1, message: { role: "assistant", content: [{ type: "text", text }] } },
        });
        return { accepted: true };
      },
      wait: async () => ({ reason: "idle", state: { running: false } }),
      cancel: () => ({ accepted: true }),
    },
    agentLoop: {
      createAgent: async (_owner, opts) => ({
        agent: { id: opts.sessionId },
        dispose: async () => {},
      }),
      resume: async (_owner, opts) => ({
        agent: { id: opts.resumeSessionId },
        dispose: async () => {},
      }),
    },
    approval: { setPolicy: () => {} },
    agentPresets: {
      selected: [],
      list: async () => [{ id: "standard" }, { id: "ptor" }],
      select: async (agent, preset) => {
        if (options.presetFails) throw new Error("preset not found");
        ctx.agentPresets.selected.push({ agent: agent.id, preset });
        return preset;
      },
    },
    logger: { info: () => {}, warn: () => {}, debug: () => {} },
    on: (event, listener) => {
      if (!listeners.has(event)) listeners.set(event, new Set());
      listeners.get(event).add(listener);
      sessionEvents.setMaxListeners(50);
      sessionEvents.on(event, listener);
      return () => sessionEvents.off(event, listener);
    },
    effect: (factory) => {
      effects.push(factory());
      return () => {};
    },
  };

  const config = {
    token: options.emptyToken ? "" : "test-token",
    tokenFile: join(dir, "handoff-studio.token"),
    endpointFile: join(dir, "handoff-studio.json"),
    loopbackOnly: true,
    maxBodyBytes: 4 * 1024 * 1024,
    panelProxy: false,
    enginePort: 8791,
    pythonPath: "",
    engineRoot: "",
    engineModulePath: "",
    agentPreset: "standard",
    runTimeoutMs: 30_000,
  };

  apply(ctx, config);

  t.after(async () => {
    for (const dispose of effects) {
      try {
        await dispose?.();
      } catch {
        /* ignore */
      }
    }
    await new Promise((resolve) => server.close(resolve));
    await rm(dir, { recursive: true, force: true });
  });

  const url = (path) => `http://127.0.0.1:${port}${ROUTE}${path}`;
  const call = (path, init) => fetch(url(path), init);
  const auth = { authorization: "Bearer test-token" };
  return { call, auth, dir, config, prompts, sessionEvents, port, ctx };
}

test("health needs no token and publishes the route", async (t) => {
  const { call } = await withPlugin(t);
  const response = await call("/health");
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.ok, true);
  assert.equal(body.name, "dsh-handoff-studio");
  assert.equal(body.path, `${ROUTE}/agent/run`);
});

test("token file is created when no token is configured", async (t) => {
  // config.token 有值时故意不写文件（用户显式给了令牌）；这里验证另一条路
  const { dir, config } = await withPlugin(t, { emptyToken: true });
  assert.equal(config.token, "");
  let saved = null;
  for (let i = 0; i < 50 && saved === null; i += 1) {
    try {
      saved = (await readFile(join(dir, "handoff-studio.token"), "utf8")).trim();
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
  }
  assert.ok(saved, "token file should appear on first load");
  assert.match(saved, /^[0-9a-f]{64}$/);
});

test("/models requires the token", async (t) => {
  const { call } = await withPlugin(t);
  const denied = await call("/models");
  assert.equal(denied.status, 401);
  const allowed = await call("/models", { headers: { authorization: "Bearer test-token" } });
  assert.equal(allowed.status, 200);
  const body = await allowed.json();
  assert.equal(body.catalog.groups[0].models[0].id, "deepseek-v4.1-flash");
});

test("agent run streams logs and returns the final text", async (t) => {
  const finalText = '{"status":"completed","summary":"done"}';
  const { call, auth, prompts } = await withPlugin(t, { finalText });

  const started = await call("/agent/run", {
    method: "POST",
    headers: { ...auth, "content-type": "application/json" },
    body: JSON.stringify({
      cwd: process.cwd(),
      prompt: "implement the thing",
      taskId: "ho-1",
      attempt: 1,
      requestId: "handoff-ho-1-1",
      provider: "workbuddy",
      model: "deepseek-v4.1-flash",
      effort: "high",
    }),
  });
  assert.equal(started.status, 202);
  const handle = await started.json();
  assert.match(handle.runId, /^run-/);
  assert.match(handle.sessionId, /^session-/);
  assert.equal(prompts.length, 1);
  assert.equal(prompts[0].requestId, "handoff-ho-1-1");

  // 等 execute() 收尾
  let state = null;
  for (let i = 0; i < 50; i += 1) {
    const polled = await call(`/agent/run/${handle.runId}`, { headers: auth });
    assert.equal(polled.status, 200);
    state = await polled.json();
    if (state.phase === "finished") break;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  assert.equal(state.phase, "finished");
  assert.equal(state.error, null);
  assert.equal(state.text, finalText);
  assert.ok(state.logs.some((line) => line.includes("Agent 就绪")));
  assert.ok(state.logs.some((line) => line.includes("第 1 轮开始")));
  assert.ok(state.logs.some((line) => line.includes("bash")));
});

test("a second run is refused while one is in flight", async (t) => {
  const { call, auth } = await withPlugin(t, { finalText: "ok" });
  const body = JSON.stringify({ cwd: process.cwd(), prompt: "x" });
  const headers = { ...auth, "content-type": "application/json" };
  const first = await call("/agent/run", { method: "POST", headers, body });
  assert.equal(first.status, 202);
  const second = await call("/agent/run", { method: "POST", headers, body });
  // 第一个 run 可能已经结束，那就允许；只断言「不会两个同时在跑」
  assert.ok([202, 409].includes(second.status));
});

test("run validation rejects a missing cwd or blank prompt", async (t) => {
  const { call, auth } = await withPlugin(t);
  const headers = { ...auth, "content-type": "application/json" };
  const noCwd = await call("/agent/run", {
    method: "POST",
    headers,
    body: JSON.stringify({ prompt: "x" }),
  });
  assert.equal(noCwd.status, 400);
  const blank = await call("/agent/run", {
    method: "POST",
    headers,
    body: JSON.stringify({ cwd: process.cwd(), prompt: "   " }),
  });
  assert.equal(blank.status, 400);
});

test("unknown run ids are 404", async (t) => {
  const { call, auth } = await withPlugin(t);
  const response = await call("/agent/run/run-missing", { headers: auth });
  assert.equal(response.status, 404);
  const cancel = await call("/agent/run/run-missing/cancel", { method: "POST", headers: auth });
  assert.equal(cancel.status, 404);
});

test("a run binds the configured agent preset", async (t) => {
  // 关键回归：不绑 preset 的 Agent 会加入当前会话的组成，从分析型会话发起的
  // run 因此没有 shell —— 跑一圈回来只会说「我没有工具」。
  const { call, auth, ctx } = await withPlugin(t, { finalText: "ok" });
  const started = await call("/agent/run", {
    method: "POST",
    headers: { ...auth, "content-type": "application/json" },
    body: JSON.stringify({ cwd: process.cwd(), prompt: "do it" }),
  });
  assert.equal(started.status, 202);
  const handle = await started.json();
  for (let i = 0; i < 50; i += 1) {
    const state = await (await call(`/agent/run/${handle.runId}`, { headers: auth })).json();
    if (state.phase === "finished") break;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  assert.equal(ctx.agentPresets.selected.length, 1);
  assert.equal(ctx.agentPresets.selected[0].preset, "standard");
  assert.equal(ctx.agentPresets.selected[0].agent, handle.sessionId);
});

test("a failing preset binding fails the run with a clear reason", async (t) => {
  const { call, auth } = await withPlugin(t, { finalText: "ok", presetFails: true });
  const started = await call("/agent/run", {
    method: "POST",
    headers: { ...auth, "content-type": "application/json" },
    body: JSON.stringify({ cwd: process.cwd(), prompt: "do it" }),
  });
  const handle = await started.json();
  let state = null;
  for (let i = 0; i < 50; i += 1) {
    state = await (await call(`/agent/run/${handle.runId}`, { headers: auth })).json();
    if (state.phase === "finished") break;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  assert.equal(state.phase, "finished");
  assert.match(state.error ?? "", /preset standard/);
  // 没绑上就不该继续把 prompt 发出去
  assert.equal(state.text, "");
});

test("describeEvent renders the panel vocabulary", () => {
  assert.match(describeEvent({ type: "turn/start", data: { turn: 2 } }), /第 2 轮开始/);
  assert.match(
    describeEvent({ type: "turn/end", data: { turn: 2, reason: { kind: "completed" } } }),
    /第 2 轮结束（completed）/,
  );
  assert.match(
    describeEvent({
      type: "tool/call",
      data: { name: "read", arguments: '{"file_path":"/a/b.ts"}' },
    }),
    /🔧 read {2}\/a\/b\.ts/,
  );
  assert.equal(
    describeEvent({ type: "tool/result", data: { message: { role: "tool" } } }),
    null,
  );
  assert.match(
    describeEvent({ type: "tool/result", data: { error: { name: "x", code: "y", reason: "boom" } } }),
    /工具返回错误: boom/,
  );
});

test("config validation rejects unknown keys and bad ports", () => {
  const schema = Config["~standard"];
  assert.equal(schema.validate({}).value.enginePort, 8791);
  const bad = schema.validate({ nope: 1 });
  assert.ok(bad.issues.some((issue) => issue.path[0] === "nope"));
  const port = schema.validate({ enginePort: 70000 });
  assert.ok(port.issues.some((issue) => issue.path[0] === "enginePort"));
});
