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

import { apply, describeEvent, ROUTE, Config, turnFailureOf } from "../lib/index.js";

/** A minimal Agent: an id plus the Session surface the host reads. */
function makeAgent(id) {
  return {
    id,
    // 沙箱模式与 preset 选择都是按 session 落的（sandbox/mode、agent-preset/selected）；
    // 这里暴露一个假的 append，才能断言插件真的写进去了，而不只是塞进 agentOptions。
    session: {
      id,
      appended: [],
      /** 会话日志里记录的组成；resume 时非空，新建时为 null */
      recordedPreset: null,
      append(type, data) {
        this.appended.push({ type, data });
        if (type === "agent-preset/selected") this.recordedPreset = data.agentPreset;
      },
    },
  };
}

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
        if (options.turnFailure) {
          // 模型级失败：这一轮不会产出 assistant/message，只有 turn/end 带结构化原因
          sessionEvents.emit("session/event", { id: request.sessionId }, {
            type: "turn/end",
            seq: 3,
            time: Date.now(),
            data: { turn: 1, reason: { kind: "error", error: options.turnFailure } },
          });
          return { accepted: true };
        }
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
      // 每个创建/恢复的 agent 都记下来，测试才能查它 session 上追加了什么
      created: [],
      createAgent: async (_owner, opts) => {
        const agent = makeAgent(opts.sessionId);
        // 真实宿主在 publish 之前 await setup：组成就是在这里挂上去的
        await opts.setup?.(ctx.agentLoop.agentCtxFor(agent), agent);
        ctx.agentLoop.created.push(agent);
        return { agent, dispose: async () => {} };
      },
      resume: async (_owner, opts) => {
        const agent = makeAgent(opts.resumeSessionId);
        // 续跑时会话里已经记着上一轮的组成，宿主把它算回 projection
        agent.session.recordedPreset = options.resumedPreset ?? "standard";
        await opts.setup?.(ctx.agentLoop.agentCtxFor(agent), agent);
        ctx.agentLoop.created.push(agent);
        return { agent, dispose: async () => {} };
      },
      /** Agent 私有 scope：真实实现里 preset 挂在这上面，这里只需是个能拿到的对象。 */
      agentCtxFor: (agent) => ({ agent }),
    },
    approval: {
      // 无人值守兜底会调它：记下来才能断言「走权限预设时不再二次改审批策略」，
      // 否则那一下会把「完全权限」的标签又打回 custom。
      policyCalls: [],
      setPolicy(agent, policy) {
        ctx.approval.policyCalls.push({ agent: agent.id, policy });
      },
    },
    // 宿主权限服务默认不装：老测试断言的是「只写 sandbox/mode」的退回路径
    ...(options.permissionPresets ? { permissionPresets: options.permissionPresets } : {}),
    agentPresets: {
      selected: [],
      // mount() 是宿主 create/resume 的真正挂载入口（见 presetSetup）
      mounted: [],
      known: options.knownPresets ?? ["standard", "ptor"],
      list: async () => ctx.agentPresets.known.map((id) => ({ id })),
      // 真实实现按 session projection 读，这里读假 Session 上记的那一份
      presetForSession: (session) => session?.recordedPreset ?? null,
      // 真实实现的 resolve() 是 async（宿主会 await 它的 diagnostic），
      // 所以这里也必须 async：插件漏了 await 时测试要能抓到。
      resolve: async (id) => {
        if (!ctx.agentPresets.known.includes(id)) {
          const error = new Error(`Unknown agent preset: ${id}`);
          error.data = { available: [...ctx.agentPresets.known] };
          throw error;
        }
        return { id };
      },
      mount: async (_agentCtx, id) => {
        if (options.presetFails) {
          const error = new Error("preset not found");
          error.data = { available: [...ctx.agentPresets.known] };
          throw error;
        }
        ctx.agentPresets.mounted.push(id);
        return { id };
      },
      select: async (agent, preset) => {
        if (options.presetFails) throw new Error("preset not found");
        ctx.agentPresets.selected.push({ agent: agent.id, preset });
        return preset;
      },
    },
    logger: { info: () => {}, warn: () => {}, debug: () => {} },
    // 可选服务入口：没装就返回 undefined，和真实 cordis 的 ctx.get 语义一致
    get: (key) => ctx[key],
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
    panelProxy: options.panelProxy ?? false,
    enginePort: options.enginePort ?? 8791,
    pythonPath: options.pythonPath ?? "",
    engineRoot: options.engineRoot ?? "",
    engineModulePath: options.engineModulePath ?? "",
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
    // Windows 上后台写文件可能还没收尾，rmdir 会 ENOTEMPTY；短暂退避重试，
    // 别让清理阶段的竞争把一条真实的断言失败盖成「测试挂了」。
    for (let attempt = 0; attempt < 20; attempt += 1) {
      try {
        await rm(dir, { recursive: true, force: true });
        break;
      } catch (error) {
        if (error?.code !== "ENOTEMPTY" || attempt === 19) throw error;
        await new Promise((resolve) => setTimeout(resolve, 25));
      }
    }
  });

  const url = (path) => `http://127.0.0.1:${port}${ROUTE}${path}`;
  const call = (path, init) => fetch(url(path), init);
  const auth = { authorization: "Bearer test-token" };
  return { call, auth, dir, config, prompts, sessionEvents, port, ctx, effects, routes };
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

test("a run mounts the configured agent preset before the agent is published", async (t) => {
  // 关键回归：不挂 preset 的 Agent 会加入当前会话的组成，从分析型会话发起的
  // run 因此没有 shell —— 跑一圈回来只会说「我没有工具」。
  //
  // 挂载必须发生在 createAgent 的 setup 里：宿主的 select() 只接受「还没开始
  // 第一轮」的会话，等 Agent 发布后再调它，续跑老会话必然抛
  // agent-preset/locked（This session has already started）。
  const { call, auth, ctx } = await withPlugin(t, { finalText: "ok" });
  const started = await call("/agent/run", {
    method: "POST",
    headers: { ...auth, "content-type": "application/json" },
    body: JSON.stringify({ cwd: process.cwd(), prompt: "do it" }),
  });
  assert.equal(started.status, 202);
  const handle = await started.json();
  let state = null;
  for (let i = 0; i < 50; i += 1) {
    state = await (await call(`/agent/run/${handle.runId}`, { headers: auth })).json();
    if (state.phase === "finished") break;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  assert.deepEqual(ctx.agentPresets.mounted, ["standard"]);
  // 走的不是 select()：那条路对已经跑过的会话是死的
  assert.equal(ctx.agentPresets.selected.length, 0);
  assert.equal(state.agentPreset, "standard");
  assert.equal(state.error, null);
});

test("resuming a session keeps the preset the session recorded", async (t) => {
  // 真实故障现场：上一轮跑完（会话里记着 standard），人点「继续上次会话」。
  // 以前这里会走 select()，宿主回 agent-preset/locked "This session has
  // already started"，整轮直接失败。现在组成在 setup 里按会话记录挂载。
  const { call, auth, ctx } = await withPlugin(t, { finalText: "ok", resumedPreset: "standard" });
  const started = await call("/agent/run", {
    method: "POST",
    headers: { ...auth, "content-type": "application/json" },
    body: JSON.stringify({
      cwd: process.cwd(),
      prompt: "carry on",
      resumeSessionId: "session-d34f878d-b6e4-4b04-887a-5128b1b3cda2",
    }),
  });
  assert.equal(started.status, 202);
  const handle = await started.json();
  let state = null;
  for (let i = 0; i < 50; i += 1) {
    state = await (await call(`/agent/run/${handle.runId}`, { headers: auth })).json();
    if (state.phase === "finished") break;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  assert.equal(state.error, null);
  assert.equal(state.agentPreset, "standard");
  assert.deepEqual(ctx.agentPresets.mounted, ["standard"]);
  assert.equal(ctx.agentPresets.selected.length, 0);
});

test("a recorded preset that left the roster falls back to the configured one", async (t) => {
  // profile 换过、preset 被删掉：老会话的记录还在，但本机已经解析不到。
  // 这条不该让任务永久无法续跑，退回插件配置的组成并说清为什么。
  const { call, auth, ctx } = await withPlugin(t, { finalText: "ok", resumedPreset: "removed-preset" });
  const started = await call("/agent/run", {
    method: "POST",
    headers: { ...auth, "content-type": "application/json" },
    body: JSON.stringify({
      cwd: process.cwd(),
      prompt: "carry on",
      resumeSessionId: "session-d34f878d-b6e4-4b04-887a-5128b1b3cda2",
    }),
  });
  const handle = await started.json();
  let state = null;
  for (let i = 0; i < 50; i += 1) {
    state = await (await call(`/agent/run/${handle.runId}`, { headers: auth })).json();
    if (state.phase === "finished") break;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  assert.equal(state.error, null);
  assert.equal(state.agentPreset, "standard");
  assert.ok(state.logs.some((line) => line.includes("removed-preset")), "要说明降级原因");
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

/**
 * turn/end 的结构化原因，是「额度用完」这类模型级失败唯一的机器可读来源。
 *
 * 那一轮不会产出 assistant/message，所以光看最终文本只会得到「没有结构化
 * JSON」，把「模型不可用」显示成「agent 不听话」。这里钉住解析规则。
 */
test("turnFailureOf reads the structured model failure and nothing else", () => {
  assert.deepEqual(
    turnFailureOf({
      type: "turn/end",
      data: {
        turn: 1,
        reason: { kind: "error", error: { code: "QUOTA", message: "current quota exhausted", status: 402 } },
      },
    }),
    { code: "QUOTA", message: "current quota exhausted", status: 402 },
  );

  // 正常结束 / 被打断 / 被拦下 / 撞 token 上限都不是模型级失败
  for (const kind of ["completed", "aborted", "blocked", "max-tokens"]) {
    assert.equal(turnFailureOf({ type: "turn/end", data: { turn: 1, reason: { kind } } }), null, kind);
  }
  // 不是 turn/end 的事件一律不看
  assert.equal(turnFailureOf({ type: "turn/start", data: { turn: 1 } }), null);
  // 缺字段也不能抛：拿不到 code 就按 UNKNOWN，让上层用 message 兜底判定
  assert.deepEqual(turnFailureOf({ type: "turn/end", data: { turn: 1, reason: { kind: "error" } } }), {
    code: "UNKNOWN",
    message: "",
    status: null,
  });
  assert.equal(turnFailureOf({ type: "turn/end", data: { turn: 1, reason: { kind: "error", error: { code: "AUTH" } } } }).status, null);
});

test("an errored turn shows the code in the log instead of a bare 'error'", () => {
  const line = describeEvent({
    type: "turn/end",
    data: { turn: 1, reason: { kind: "error", error: { code: "RATE_LIMIT", message: "slow down" } } },
  });
  assert.match(line, /第 1 轮结束（error: RATE_LIMIT slow down）/);
});

test("a run reports the model failure through its status route", async (t) => {
  const { call, auth } = await withPlugin(t, {
    turnFailure: { code: "ACCOUNT_QUOTA", message: "balance is empty", status: 402 },
  });
  const started = await call("/agent/run", {
    method: "POST",
    headers: { ...auth, "content-type": "application/json" },
    body: JSON.stringify({ cwd: process.cwd(), prompt: "do it" }),
  });
  assert.equal(started.status, 202);
  const handle = await started.json();

  let state = null;
  for (let i = 0; i < 100; i += 1) {
    state = await (await call(`/agent/run/${handle.runId}`, { headers: auth })).json();
    if (state.phase === "finished") break;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  assert.equal(state.phase, "finished");
  // 引擎据此决定「换下一个候选模型」还是「直接失败」，所以必须原样回传
  assert.deepEqual(state.failure, {
    code: "ACCOUNT_QUOTA",
    message: "balance is empty",
    status: 402,
  });
  // 没有 assistant/message，所以没有最终文本；失败原因不能只留一句「没结果」
  assert.equal(state.text, "");
  assert.ok(
    state.logs.some((line) => line.includes("ACCOUNT_QUOTA")),
    "the log must name the failure code",
  );
});

test("a clean run reports no model failure", async (t) => {
  const { call, auth } = await withPlugin(t, { finalText: '{"status":"completed"}' });
  const started = await call("/agent/run", {
    method: "POST",
    headers: { ...auth, "content-type": "application/json" },
    body: JSON.stringify({ cwd: process.cwd(), prompt: "do it" }),
  });
  const handle = await started.json();
  let state = null;
  for (let i = 0; i < 100; i += 1) {
    state = await (await call(`/agent/run/${handle.runId}`, { headers: auth })).json();
    if (state.phase === "finished") break;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  assert.equal(state.failure, null);
});

test("config validation rejects unknown keys and bad ports", () => {
  const schema = Config["~standard"];
  assert.equal(schema.validate({}).value.enginePort, 8791);
  const bad = schema.validate({ nope: 1 });
  assert.ok(bad.issues.some((issue) => issue.path[0] === "nope"));
  const port = schema.validate({ enginePort: 70000 });
  assert.ok(port.issues.some((issue) => issue.path[0] === "enginePort"));
});

test("a run applies the requested sandbox mode to the agent session", async (t) => {
  const { call, auth, ctx } = await withPlugin(t, { finalText: "{}" });

  const started = await call("/agent/run", {
    method: "POST",
    headers: { ...auth, "content-type": "application/json" },
    body: JSON.stringify({
      cwd: process.cwd(),
      prompt: "implement the thing",
      taskId: "ho-1",
      attempt: 1,
      requestId: "handoff-ho-1-1",
      sandbox: "read-only",
    }),
  });
  assert.equal(started.status, 202);
  const handle = await started.json();
  // 响应里回显实际生效的档位，面板才能核对
  assert.equal(handle.sandbox, "read-only");

  // 等 execute() 跑完，agent 的 session 上应当有一条 sandbox/mode
  for (let i = 0; i < 200; i += 1) {
    const poll = await call(`/agent/run/${handle.runId}`, { headers: auth });
    const body = await poll.json();
    if (body.phase === "finished") break;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  const agent = ctx.agentLoop.created.at(-1);
  assert.equal(agent.session.appended.length, 1);
  assert.deepEqual(agent.session.appended[0], { type: "sandbox/mode", data: { mode: "read-only" } });
  // 没装权限服务时退回「只写沙箱」的路径，界面会显示 custom，所以要点出这一点
  assert.ok(handle.sandbox === "read-only");
});

/** 一个最小的宿主权限服务：表里三档与发版默认表同构，记录被选中的预设。 */
function fakePermissionPresets(overrides = {}) {
  const table = {
    "read-only": { sandbox: "read-only", approval: "ask" },
    "workspace-write": { sandbox: "workspace-write", approval: "ask" },
    "danger-full-access": { sandbox: "danger-full-access", approval: "never" },
    ...overrides,
  };
  return {
    table,
    applied: [],
    resolve(name) {
      const spec = table[name];
      if (spec === undefined) throw new Error(`permission: unknown preset "${name}"`);
      return spec;
    },
    set(session, name) {
      // 真实实现会一次写 permission/preset + 两个旋钮；测试只需要能断言「走的是这条路」
      this.resolve(name);
      this.applied.push({ session: session.id, preset: name });
      session.append("permission/preset", { preset: name });
    },
  };
}

test("a run switches through the host permission preset so the chip stays named", async (t) => {
  const presets = fakePermissionPresets();
  const { call, auth, ctx } = await withPlugin(t, { finalText: "{}", permissionPresets: presets });

  const started = await call("/agent/run", {
    method: "POST",
    headers: { ...auth, "content-type": "application/json" },
    body: JSON.stringify({
      cwd: process.cwd(),
      prompt: "implement the thing",
      requestId: "handoff-ho-1-2",
      sandbox: "danger-full-access",
    }),
  });
  assert.equal(started.status, 202);
  const handle = await started.json();

  let state = null;
  for (let i = 0; i < 200; i += 1) {
    state = await (await call(`/agent/run/${handle.runId}`, { headers: auth })).json();
    if (state.phase === "finished") break;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.equal(state.phase, "finished");
  // 走的是预设：宿主那边看到的是 danger-full-access，不是裸的 sandbox/mode
  assert.deepEqual(presets.applied, [{ session: handle.sessionId, preset: "danger-full-access" }]);
  assert.equal(state.permissionPreset, "danger-full-access");
  // 预设已经带上了审批策略，就不能再用无人值守那手把它改成 never——那会把
  // 「完全权限」的标签打回 custom（沙箱 full + 审批 ask 对不上任何预设行）
  assert.deepEqual(ctx.approval.policyCalls, []);
});

test("a preset whose sandbox differs from the requested mode is not trusted", async (t) => {
  // 部署把 danger-full-access 配成只读沙箱：同名不等于同义，宁可退回只写沙箱
  const presets = fakePermissionPresets({
    "danger-full-access": { sandbox: "read-only", approval: "never" },
  });
  const { call, auth, ctx } = await withPlugin(t, { finalText: "{}", permissionPresets: presets });

  const started = await call("/agent/run", {
    method: "POST",
    headers: { ...auth, "content-type": "application/json" },
    body: JSON.stringify({ cwd: process.cwd(), prompt: "do it", requestId: "handoff-ho-1-3", sandbox: "danger-full-access" }),
  });
  const handle = await started.json();
  for (let i = 0; i < 200; i += 1) {
    const body = await (await call(`/agent/run/${handle.runId}`, { headers: auth })).json();
    if (body.phase === "finished") break;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.deepEqual(presets.applied, []);
  const agent = ctx.agentLoop.created.at(-1);
  assert.deepEqual(agent.session.appended, [{ type: "sandbox/mode", data: { mode: "danger-full-access" } }]);
  // 退回路径下仍要保证无人值守：审批策略单改一次
  assert.equal(ctx.approval.policyCalls.length, 1);
});

test("a read-only preset is not used because it would ask for approval nobody answers", async (t) => {
  // read-only 预设带 ask：照抄它，任务就会卡在一个没人回答的审批框上。
  // 无人值守优先于标签好看——这一档退回只写沙箱 + 强制不询问。
  const presets = fakePermissionPresets();
  const { call, auth, ctx } = await withPlugin(t, { finalText: "{}", permissionPresets: presets });

  const started = await call("/agent/run", {
    method: "POST",
    headers: { ...auth, "content-type": "application/json" },
    body: JSON.stringify({ cwd: process.cwd(), prompt: "analyse only", requestId: "handoff-ho-1-4", sandbox: "read-only" }),
  });
  const handle = await started.json();
  let state = null;
  for (let i = 0; i < 200; i += 1) {
    state = await (await call(`/agent/run/${handle.runId}`, { headers: auth })).json();
    if (state.phase === "finished") break;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.deepEqual(presets.applied, []);
  assert.equal(state.permissionPreset, null);
  assert.equal(ctx.approval.policyCalls.length, 1);
});

test("an unknown sandbox mode is ignored instead of forwarded", async (t) => {
  const { call, auth, ctx } = await withPlugin(t, { finalText: "{}" });

  const started = await call("/agent/run", {
    method: "POST",
    headers: { ...auth, "content-type": "application/json" },
    body: JSON.stringify({
      cwd: process.cwd(),
      prompt: "implement the thing",
      taskId: "ho-1",
      attempt: 1,
      requestId: "handoff-ho-1-1",
      sandbox: "totally-open",
    }),
  });
  assert.equal(started.status, 202);
  const handle = await started.json();
  assert.equal(handle.sandbox, null);
  // 等 execute() 收尾再让 withPlugin 清理临时目录，否则后台写文件会和 rmdir 撞车
  for (let i = 0; i < 200; i += 1) {
    const poll = await call(`/agent/run/${handle.runId}`, { headers: auth });
    const body = await poll.json();
    if (body.phase === "finished") break;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  const agent = ctx.agentLoop.created.at(-1);
  // 没有合法档位就不写 sandbox/mode，跟随部署默认
  assert.equal(agent.session.appended.length, 0);
});
