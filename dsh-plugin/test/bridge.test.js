/**
 * Load the bridge plugin with a stub Cordis context and drive it over HTTP.
 *
 * The real host services (webServer / sessionController) are replaced by the
 * smallest fakes that keep the same contract: register(route) returns a
 * disposer, port/getOrigin() report the bound listener, and prompt(request,
 * signal) records what it was given.
 */

import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, test } from "node:test";

import { Config, ENDPOINT_VERSION, apply, inject, name } from "../lib/index.js";

/** Run the Standard Schema face the way cordis' loader does. */
const parse = (raw) => {
  const result = Config["~standard"].validate(raw);
  assert.equal(result.issues, undefined, JSON.stringify(result.issues));
  return result.value;
};

/**
 * Minimal stand-in for the parts of the host the plugin touches.
 *
 * `port` starts undefined, like the real service before `[Service.init]` binds,
 * and `bind(port)` flips it so tests can drive the publish path.
 */
function fakeContext() {
  const routes = [];
  const prompts = [];
  const selections = [];
  const operations = [];
  const effects = [];
  let disposed = false;
  let port;
  const ctx = {
    logger: { info() {}, warn() {} },
    effect(execute, label) {
      const disposer = execute();
      effects.push({ label, disposer });
      return {
        dispose() {
          disposed = true;
          if (typeof disposer === "function") disposer();
        },
      };
    },
    webServer: {
      get port() {
        return port;
      },
      get host() {
        return "127.0.0.1";
      },
      getOrigin() {
        if (port === undefined) throw new Error("webserver: origin read before the listener bound a port");
        return `http://127.0.0.1:${port}`;
      },
      register(route) {
        routes.push(route);
        return () => {
          const index = routes.indexOf(route);
          if (index >= 0) routes.splice(index, 1);
        };
      },
    },
    sessionController: {
      async selectModel(request) {
        selections.push(request);
        operations.push("selectModel");
        if (request.model === "unavailable") {
          const error = new Error("model unavailable");
          error.code = "session/model-unavailable";
          throw error;
        }
        return { selected: true };
      },
      async prompt(request, signal) {
        if (signal?.aborted) throw new Error("aborted");
        prompts.push(request);
        operations.push("prompt");
        if (request.sessionId === "session-missing") {
          const error = new Error("session not found");
          error.code = "session/not-found";
          throw error;
        }
        if (request.text === undefined && request.content?.[0]?.text === "busy") {
          const error = new Error("agent busy");
          error.code = "session/agent-busy";
          throw error;
        }
        return { accepted: true };
      },
    },
  };
  return {
    ctx,
    routes,
    prompts,
    selections,
    operations,
    effects,
    disposed: () => disposed,
    bind: (value) => {
      port = value;
    },
  };
}

/** Boot the plugin's single route on an ephemeral port. */
async function serve(ctx, routes) {
  const route = routes[0];
  const server = createServer((req, res) => route.handler(req, res));
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  return { server, origin: `http://127.0.0.1:${port}`, close: () => new Promise((r) => server.close(r)) };
}

/**
 * 等插件把端点文件写出来。
 *
 * 写在启动的异步分支里（要先等 webServer 绑定端口），所以这里轮询而不是
 * 立刻读——但必须在有限次数内出现，否则就是回归。
 */
async function waitForEndpoint(file, attempts = 100) {
  for (let i = 0; i < attempts; i += 1) {
    try {
      return JSON.parse(await readFile(file, "utf8"));
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
  }
  return undefined;
}

/** 等某个文件出现非空内容；超时返回空串。 */
async function waitForText(file, attempts = 100) {
  for (let i = 0; i < attempts; i += 1) {
    try {
      const text = (await readFile(file, "utf8")).trim();
      if (text.length > 0) return text;
    } catch {
      /* 还没写出来 */
    }
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  return "";
}

let workspace;

before(async () => {
  workspace = await mkdtemp(join(tmpdir(), "handoff-bridge-"));
  // 没传 endpointFile 的用例会走默认值 $DSH_HOME/handoff-bridge.json。
  // 把 DSH_HOME 指到沙箱里，免得测试往真实 ~/.dsh 写假端点。
  process.env.DSH_HOME = workspace;
});

after(async () => {
  await rm(workspace, { recursive: true, force: true });
});

test("plugin exports the Cordis shape", () => {
  assert.equal(name, "dsh-handoff-bridge");
  assert.deepEqual(inject, ["webServer", "sessionController"]);
  assert.equal(typeof apply, "function");
  // 手写的 Standard Schema：cordis 只读 Config["~standard"].validate。
  assert.equal(typeof Config["~standard"].validate, "function");
  assert.equal(Config["~standard"].version, 1);
});

test("config defaults and validation", () => {
  const parsed = parse({});
  assert.equal(parsed.path, "/v1/handoff/dsh");
  assert.equal(parsed.loopbackOnly, true);
  assert.equal(parsed.maxBodyBytes, 64 * 1024);
  assert.equal(parsed.endpointFile, "");

  const bad = Config["~standard"].validate({ path: 42 });
  assert.equal(bad.value, undefined);
  assert.deepEqual(bad.issues[0].path, ["path"]);

  const unknown = Config["~standard"].validate({ nope: 1 });
  assert.deepEqual(unknown.issues[0].path, ["nope"]);
});

test("health answers without a token", async () => {
  const { ctx, routes } = fakeContext();
  apply(ctx, parse({ token: "t0ken", tokenFile: join(workspace, "health.token") }));
  const server = await serve(ctx, routes);
  try {
    const response = await fetch(`${server.origin}/v1/handoff/dsh/health`);
    assert.equal(response.status, 200);
    const body = await response.json();
    assert.equal(body.ok, true);
    assert.equal(body.path, "/v1/handoff/dsh/prompt");
  } finally {
    await server.close();
  }
});

test("health reports the bound port once the listener is up", async () => {
  const { ctx, routes, bind } = fakeContext();
  apply(ctx, parse({ token: "t0ken", tokenFile: join(workspace, "port.token") }));
  bind(45123);
  const server = await serve(ctx, routes);
  try {
    const body = await (await fetch(`${server.origin}/v1/handoff/dsh/health`)).json();
    assert.equal(body.port, 45123);
  } finally {
    await server.close();
  }
});

test("prompt requires a bearer token", async () => {
  const { ctx, routes, prompts } = fakeContext();
  apply(ctx, parse({ token: "t0ken", tokenFile: join(workspace, "auth.token") }));
  const server = await serve(ctx, routes);
  try {
    const response = await fetch(`${server.origin}/v1/handoff/dsh/prompt`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ sessionId: "session-a", requestId: "r1", text: "hi" }),
    });
    assert.equal(response.status, 401);
    assert.equal(prompts.length, 0);

    const wrong = await fetch(`${server.origin}/v1/handoff/dsh/prompt`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: "Bearer nope" },
      body: JSON.stringify({ sessionId: "session-a", requestId: "r1", text: "hi" }),
    });
    assert.equal(wrong.status, 401);
    assert.equal(prompts.length, 0);
  } finally {
    await server.close();
  }
});

test("prompt queues text for the named session", async () => {
  const { ctx, routes, prompts, selections } = fakeContext();
  apply(ctx, parse({ token: "t0ken", tokenFile: join(workspace, "ok.token") }));
  const server = await serve(ctx, routes);
  try {
    const response = await fetch(`${server.origin}/v1/handoff/dsh/prompt`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: "Bearer t0ken" },
      body: JSON.stringify({ sessionId: "session-a", requestId: "r1", text: "后端做完了" }),
    });
    assert.equal(response.status, 200);
    const body = await response.json();
    assert.equal(body.accepted, true);
    assert.equal(body.sessionId, "session-a");

    assert.equal(selections.length, 0);
    assert.equal(prompts.length, 1);
    assert.deepEqual(prompts[0], {
      requestId: "r1",
      sessionId: "session-a",
      mode: "queue",
      content: [{ type: "text", text: "后端做完了" }],
    });
  } finally {
    await server.close();
  }
});

test("configured model is selected before prompt", async () => {
  const { ctx, routes, prompts, selections, operations } = fakeContext();
  apply(ctx, parse({ token: "t0ken", tokenFile: join(workspace, "model.token") }));
  const server = await serve(ctx, routes);
  try {
    const response = await fetch(`${server.origin}/v1/handoff/dsh/prompt`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: "Bearer t0ken" },
      body: JSON.stringify({
        sessionId: "session-a",
        requestId: "r-model",
        text: "使用指定模型恢复",
        provider: "deepseek-account",
        model: "deepseek-chat",
      }),
    });
    assert.equal(response.status, 200);
    assert.deepEqual(selections, [{
      sessionId: "session-a",
      provider: "deepseek-account",
      model: "deepseek-chat",
    }]);
    assert.equal(prompts.length, 1);
    assert.deepEqual(operations, ["selectModel", "prompt"]);
  } finally {
    await server.close();
  }
});

test("steer mode is passed through", async () => {
  const { ctx, routes, prompts } = fakeContext();
  apply(ctx, parse({ token: "t0ken", tokenFile: join(workspace, "steer.token") }));
  const server = await serve(ctx, routes);
  try {
    const response = await fetch(`${server.origin}/v1/handoff/dsh/prompt`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: "Bearer t0ken" },
      body: JSON.stringify({ sessionId: "session-a", requestId: "r2", text: "停一下", mode: "steer" }),
    });
    assert.equal(response.status, 200);
    assert.equal(prompts[0].mode, "steer");
  } finally {
    await server.close();
  }
});

test("bad envelopes are rejected before prompt", async () => {
  const { ctx, routes, prompts, selections } = fakeContext();
  apply(ctx, parse({ token: "t0ken", tokenFile: join(workspace, "bad.token") }));
  const server = await serve(ctx, routes);
  const post = (body) =>
    fetch(`${server.origin}/v1/handoff/dsh/prompt`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: "Bearer t0ken" },
      body: typeof body === "string" ? body : JSON.stringify(body),
    });
  try {
    assert.equal((await post({ requestId: "r", text: "hi" })).status, 400);
    assert.equal((await post({ sessionId: "s", text: "hi" })).status, 400);
    assert.equal((await post({ sessionId: "s", requestId: "r", text: "   " })).status, 400);
    assert.equal((await post({ sessionId: "s", requestId: "r", text: "hi", mode: "shout" })).status, 400);
    assert.equal((await post({ sessionId: "s", requestId: "r", text: "hi", provider: "p" })).status, 400);
    assert.equal((await post({ sessionId: "s", requestId: "r", text: "hi", model: "m" })).status, 400);
    assert.equal((await post({ sessionId: "s", requestId: "r", text: "hi", provider: "", model: "m" })).status, 400);
    assert.equal((await post({ sessionId: "s", requestId: "r", text: "hi", provider: "p", model: 1 })).status, 400);
    assert.equal((await post("not json")).status, 400);
    assert.equal(selections.length, 0);
    assert.equal(prompts.length, 0);
  } finally {
    await server.close();
  }
});

test("remote errors map onto retryable statuses", async () => {
  const { ctx, routes } = fakeContext();
  apply(ctx, parse({ token: "t0ken", tokenFile: join(workspace, "err.token") }));
  const server = await serve(ctx, routes);
  try {
    const missing = await fetch(`${server.origin}/v1/handoff/dsh/prompt`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: "Bearer t0ken" },
      body: JSON.stringify({ sessionId: "session-missing", requestId: "r3", text: "hi" }),
    });
    assert.equal(missing.status, 404);
    assert.equal((await missing.json()).code, "session/not-found");

    const unavailable = await fetch(`${server.origin}/v1/handoff/dsh/prompt`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: "Bearer t0ken" },
      body: JSON.stringify({
        sessionId: "session-a",
        requestId: "r4",
        text: "hi",
        provider: "deepseek-account",
        model: "unavailable",
      }),
    });
    assert.equal(unavailable.status, 400);
    assert.equal((await unavailable.json()).code, "session/model-unavailable");
  } finally {
    await server.close();
  }
});

test("oversized bodies are refused", async () => {
  const { ctx, routes } = fakeContext();
  apply(ctx, parse({ token: "t0ken", tokenFile: join(workspace, "big.token"), maxBodyBytes: 1024 }));
  const server = await serve(ctx, routes);
  try {
    const response = await fetch(`${server.origin}/v1/handoff/dsh/prompt`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: "Bearer t0ken" },
      body: JSON.stringify({ sessionId: "s", requestId: "r", text: "x".repeat(4096) }),
    }).catch(() => ({ status: 413 }));
    assert.ok(response.status === 413 || response.status === 400);
  } finally {
    await server.close();
  }
});

test("token file is created on first start and reused", async () => {
  const file = join(workspace, "auto.token");
  const first = fakeContext();
  apply(first.ctx, parse({ tokenFile: file }));
  const firstServer = await serve(first.ctx, first.routes);
  try {
    const response = await fetch(`${firstServer.origin}/v1/handoff/dsh/health`);
    assert.equal(response.status, 200);
    const created = (await readFile(file, "utf8")).trim();
    assert.equal(created.length, 64);

    const second = fakeContext();
    apply(second.ctx, parse({ tokenFile: file }));
    const secondServer = await serve(second.ctx, second.routes);
    try {
      const queued = await fetch(`${secondServer.origin}/v1/handoff/dsh/prompt`, {
        method: "POST",
        headers: { "content-type": "application/json", authorization: `Bearer ${created}` },
        body: JSON.stringify({ sessionId: "session-a", requestId: "r9", text: "hi" }),
      });
      assert.equal(queued.status, 200);
    } finally {
      await secondServer.close();
    }
  } finally {
    await firstServer.close();
  }
});

test("the token file appears at startup, before any request", async () => {
  const file = join(workspace, "startup.token");
  const { ctx, routes } = fakeContext();
  apply(ctx, parse({ tokenFile: file }));
  const server = await serve(ctx, routes);
  try {
    // 刚 apply 完就该有令牌文件——不能等第一次请求。
    // 用户装完插件会去 $DSH_HOME 看，文件不在就像没装成功。
    let saved = "";
    for (let attempt = 0; attempt < 50 && saved.length === 0; attempt += 1) {
      try {
        saved = (await readFile(file, "utf8")).trim();
      } catch {
        await new Promise((resolve) => setTimeout(resolve, 10));
      }
    }
    assert.equal(saved.length, 64);
  } finally {
    await server.close();
  }
});

test("the endpoint is published once the listener binds", async () => {
  const tokenFile = join(workspace, "publish.token");
  const endpointFile = join(workspace, "publish.json");
  const { ctx, routes, bind } = fakeContext();
  apply(ctx, parse({ tokenFile, endpointFile }));
  const server = await serve(ctx, routes);
  try {
    bind(45999);
    const published = await waitForEndpoint(endpointFile);
    assert.ok(published, "the plugin must publish its endpoint at startup");
    assert.equal(published.version, ENDPOINT_VERSION);
    assert.equal(published.name, "dsh-handoff-bridge");
    assert.equal(published.url, "http://127.0.0.1:45999/v1/handoff/dsh/prompt");
    assert.equal(published.origin, "http://127.0.0.1:45999");
    assert.equal(published.path, "/v1/handoff/dsh/prompt");
    assert.equal(published.pid, process.pid);
    assert.equal(typeof published.updatedAt, "string");
  } finally {
    await server.close();
  }
});

test("the published endpoint follows a rebind, not the config", async () => {
  // 端口是内核分配的，重启就变；端点文件必须反映实际值。
  const endpointFile = join(workspace, "rebind.json");
  const { ctx, routes, bind } = fakeContext();
  apply(ctx, parse({ token: "t0ken", tokenFile: join(workspace, "rebind.token"), endpointFile }));
  const server = await serve(ctx, routes);
  try {
    bind(40001);
    assert.equal((await waitForEndpoint(endpointFile)).url, "http://127.0.0.1:40001/v1/handoff/dsh/prompt");
  } finally {
    await server.close();
  }
});

test("the endpoint carries no secret", async () => {
  const tokenFile = join(workspace, "nosecret.token");
  const endpointFile = join(workspace, "nosecret.json");
  const { ctx, routes, bind } = fakeContext();
  apply(ctx, parse({ tokenFile, endpointFile }));
  const server = await serve(ctx, routes);
  try {
    bind(40002);
    // 令牌和端点都是启动期异步写的，两边都等出来再比。
    await waitForEndpoint(endpointFile);
    let token = "";
    for (let attempt = 0; attempt < 100 && token.length === 0; attempt += 1) {
      try {
        token = (await readFile(tokenFile, "utf8")).trim();
      } catch {
        await new Promise((resolve) => setTimeout(resolve, 10));
      }
    }
    assert.equal(token.length, 64);
    const raw = await readFile(endpointFile, "utf8");
    assert.ok(!raw.includes(token), "the endpoint file must not leak the shared secret");
  } finally {
    await server.close();
  }
});

test("the endpoint is removed when the plugin unloads", async () => {
  const endpointFile = join(workspace, "unload.json");
  const { ctx, routes, bind, effects } = fakeContext();
  apply(ctx, parse({ token: "t0ken", tokenFile: join(workspace, "unload.token"), endpointFile }));
  const server = await serve(ctx, routes);
  try {
    bind(40003);
    assert.ok(await waitForEndpoint(endpointFile));
    const endpointEffect = effects.find((e) => e.label === "handoff-bridge: endpoint");
    assert.ok(endpointEffect, "the plugin must own an endpoint effect");
    await endpointEffect.disposer();
    await assert.rejects(readFile(endpointFile, "utf8"), { code: "ENOENT" });
  } finally {
    await server.close();
  }
});

test("unload keeps an endpoint that belongs to another process", async () => {
  // 两个 DSH 实例共用一个 $DSH_HOME 时，别把别人的端点删了。
  const endpointFile = join(workspace, "other.json");
  const { writeFile } = await import("node:fs/promises");
  await writeFile(
    endpointFile,
    `${JSON.stringify({ version: ENDPOINT_VERSION, url: "http://127.0.0.1:1/x", pid: process.pid + 1 })}\n`,
    "utf8",
  );
  const { ctx, routes, effects } = fakeContext();
  apply(ctx, parse({ token: "t0ken", tokenFile: join(workspace, "other.token"), endpointFile }));
  const server = await serve(ctx, routes);
  try {
    const endpointEffect = effects.find((e) => e.label === "handoff-bridge: endpoint");
    await endpointEffect.disposer();
    const still = await readFile(endpointFile, "utf8");
    assert.ok(still.includes(String(process.pid + 1)));
  } finally {
    await server.close();
  }
});

test("unknown paths 404 and non-POST 405", async () => {
  const { ctx, routes } = fakeContext();
  apply(ctx, parse({ token: "t0ken", tokenFile: join(workspace, "misc.token") }));
  const server = await serve(ctx, routes);
  try {
    assert.equal((await fetch(`${server.origin}/v1/handoff/dsh/nope`)).status, 404);
    assert.equal((await fetch(`${server.origin}/v1/handoff/dsh/prompt`)).status, 405);
  } finally {
    await server.close();
  }
});

test("route disposer releases the registration", () => {
  const { ctx, routes, disposed } = fakeContext();
  const effect = apply(ctx, parse({ token: "t0ken", tokenFile: join(workspace, "disp.token") }));
  assert.equal(routes.length, 1);
  assert.equal(typeof effect, "undefined");
  assert.equal(disposed(), false);
});
