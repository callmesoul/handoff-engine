/**
 * dsh-handoff-bridge — 把 handoff 的完成事件排回创建任务的原 DSH 会话。
 *
 * DSH 没有 `codex queue --thread` 那样的 CLI，回推只能走宿主进程内的
 * `sessionController.prompt()`：它是唯一会做 requestId 幂等、按需 resume
 * 会话、并把消息当作 owner 用户消息入队的入口。
 *
 * 因此本插件在本地 DSH webserver 上开一个前缀路由，由 handoff 前端监听器
 * （`handoffctl frontend-listener`）用 HTTP POST 投递；插件校验令牌与来源后
 * 调用 `prompt()`。这条路由不走 `/api` 的 cookie 认证层，所以令牌与
 * loopback 限制都由本插件自己守。
 *
 * 端口是 DSH 启动时由内核分配的，每次重启都可能变，所以插件在监听真正
 * 绑定之后，把实际地址写进 `$DSH_HOME/handoff-bridge.json`；handoff 侧读
 * 这个文件就能自己找到端口，不必依赖启动时抓到的 `DSH_WEB_URL`。
 *
 * 插件不依赖任何 npm 包：`Config` 直接写成 Standard Schema，省掉 profile 里
 * 的 peerDependency 解析，装进任意 profile 都能跑。
 *
 * @module dsh-handoff-bridge
 */

import { randomBytes, timingSafeEqual } from "node:crypto";
import { mkdir, readFile, rename, unlink, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

/** Stable Cordis plugin name. */
const name = "dsh-handoff-bridge";

/** Services required before the route can be claimed. */
const inject = ["webServer", "sessionController"];

/** 端点文件格式版本；改结构时一起改。 */
const ENDPOINT_VERSION = 1;

const FIELDS = {
  path: { kind: "string", fallback: "/v1/handoff/dsh", check: (v) => v.startsWith("/") || "must start with /" },
  token: { kind: "string", fallback: "", check: () => true },
  tokenFile: { kind: "string", fallback: "", check: () => true },
  endpointFile: { kind: "string", fallback: "", check: () => true },
  maxBodyBytes: {
    kind: "number",
    fallback: 64 * 1024,
    check: (v) => (Number.isInteger(v) && v >= 1024) || "must be an integer >= 1024",
  },
  loopbackOnly: { kind: "boolean", fallback: true, check: () => true },
  promptTimeoutMs: {
    kind: "number",
    fallback: 60_000,
    check: (v) => (Number.isInteger(v) && v >= 1000) || "must be an integer >= 1000",
  },
};

/**
 * Validate one raw config object against {@link FIELDS}.
 * @param raw - the user config.
 * @returns the standardized value, or the standard-schema issue list.
 */
function validateConfig(raw) {
  const input = raw === undefined || raw === null ? {} : raw;
  if (typeof input !== "object" || Array.isArray(input)) {
    return { issues: [{ message: "config must be an object", path: [] }] };
  }
  const value = {};
  const issues = [];
  for (const [key, spec] of Object.entries(FIELDS)) {
    const present = input[key];
    if (present === undefined || present === null || present === "") {
      value[key] = spec.fallback;
      continue;
    }
    if (typeof present !== spec.kind) {
      issues.push({ message: `must be a ${spec.kind}`, path: [key] });
      continue;
    }
    const verdict = spec.check(present);
    if (verdict !== true) {
      issues.push({ message: String(verdict), path: [key] });
      continue;
    }
    value[key] = present;
  }
  for (const key of Object.keys(input)) {
    if (!(key in FIELDS)) issues.push({ message: "unknown option", path: [key] });
  }
  return issues.length > 0 ? { issues } : { value };
}

/** Standard Schema face cordis reads (`runtime.Config["~standard"].validate`). */
const Config = {
  "~standard": { version: 1, vendor: "dsh-handoff-bridge", validate: validateConfig },
};

/** DSH 配置根；插件与 handoff 两侧用同一套规则。 */
function dshHome() {
  return process.env.DSH_HOME || join(homedir(), ".dsh");
}

/** Where the token lives when the config names no path. */
function defaultTokenFile() {
  return join(dshHome(), "handoff-bridge.token");
}

/** Where the announced endpoint lives when the config names no path. */
function defaultEndpointFile() {
  return join(dshHome(), "handoff-bridge.json");
}

/**
 * Read the shared secret, creating one on first start so that a fresh install
 * works without the user copying a secret anywhere.
 * @param config - validated plugin config.
 * @returns the token both sides compare against.
 */
async function resolveToken(config) {
  if (config.token) return config.token;
  const file = config.tokenFile || defaultTokenFile();
  try {
    const saved = (await readFile(file, "utf8")).trim();
    if (saved) return saved;
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  const created = randomBytes(32).toString("hex");
  await mkdir(dirname(file), { recursive: true });
  const temp = `${file}.${process.pid}`;
  await writeFile(temp, `${created}\n`, { mode: 0o600 });
  await rename(temp, file);
  return created;
}

/** Constant-time token comparison that does not leak the length. */
function tokenMatches(expected, presented) {
  const a = Buffer.from(expected, "utf8");
  const b = Buffer.from(presented, "utf8");
  if (a.length !== b.length) return false;
  return timingSafeEqual(a, b);
}

/** Pull the bearer token out of an Authorization header. */
function bearerOf(header) {
  const value = typeof header === "string" ? header.trim() : "";
  const match = /^Bearer\s+(\S+)$/i.exec(value);
  return match ? match[1] : "";
}

/** Loopback only, including the IPv4-mapped form a dual-stack listener reports. */
function isLoopback(address) {
  if (typeof address !== "string") return false;
  const normalized = address.startsWith("::ffff:") ? address.slice(7) : address;
  return normalized === "::1" || normalized.startsWith("127.");
}

/**
 * Read the whole request body as UTF-8 text, refusing anything oversized.
 * @param req - the incoming request.
 * @param limit - largest accepted byte count.
 * @returns the decoded body.
 */
function readBody(req, limit) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    let settled = false;
    const fail = (error) => {
      if (settled) return;
      settled = true;
      reject(error);
    };
    req.on("data", (chunk) => {
      if (settled) return;
      size += chunk.length;
      if (size > limit) {
        fail(Object.assign(new Error("request body too large"), { status: 413 }));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => {
      if (settled) return;
      settled = true;
      resolve(Buffer.concat(chunks).toString("utf8"));
    });
    req.on("error", fail);
  });
}

/** Write one JSON response. */
function sendJSON(res, status, body) {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
    "content-length": Buffer.byteLength(payload),
  });
  res.end(payload);
}

/** Map a session-controller failure onto the HTTP status the listener retries on. */
function statusOfRemoteError(code) {
  if (code === "session/not-found") return 404;
  if (code === "gateway/bad-request" || code === "session/invalid-time-zone") return 400;
  if (code === "session/agent-busy") return 409;
  return 500;
}

/**
 * Validate one prompt envelope.
 * @param body - parsed JSON body.
 * @returns the normalized envelope, or the reason it is unusable.
 */
function normalizePrompt(body) {
  if (body === null || typeof body !== "object" || Array.isArray(body)) {
    return { error: "body must be a JSON object" };
  }
  const sessionId = typeof body.sessionId === "string" ? body.sessionId.trim() : "";
  const requestId = typeof body.requestId === "string" ? body.requestId.trim() : "";
  const text = typeof body.text === "string" ? body.text : "";
  const mode = body.mode === undefined ? "queue" : body.mode;
  if (!sessionId) return { error: "sessionId is required" };
  if (!requestId) return { error: "requestId is required" };
  if (!text.trim()) return { error: "text must contain non-whitespace" };
  if (mode !== "queue" && mode !== "steer") return { error: 'mode must be "queue" or "steer"' };
  return { sessionId, requestId, text, mode };
}

/**
 * 等 webserver 真正绑定端口。
 *
 * `apply()` 跑在服务启动阶段，此时 `webServer.port` 还是 `undefined`——
 * 只有 `[Service.init]` 的 listen 回调里才会写进去。这里轮询等它出现，
 * 超时就放弃（不阻塞插件卸载，也不让定时器吊住进程）。
 *
 * @param ctx - plugin context.
 * @param timeoutMs - 最多等多久。
 * @returns 绑定的端口，或 `undefined`。
 */
function awaitPort(ctx, timeoutMs = 10_000) {
  return new Promise((resolve) => {
    const deadline = Date.now() + timeoutMs;
    const step = () => {
      const port = ctx.webServer?.port;
      if (typeof port === "number" && port > 0) {
        resolve(port);
        return;
      }
      if (Date.now() >= deadline) {
        resolve(undefined);
        return;
      }
      const timer = setTimeout(step, 50);
      timer.unref?.();
    };
    step();
  });
}

/**
 * 拼出本插件的完整地址。
 *
 * 优先用 `getOrigin()`（它会把 `::ffff:127.0.0.1` 之类规范化成浏览器认的
 * loopback 文本）；带 zone id 的地址没有 URL 形式，此时退回自己拼。
 *
 * @param ctx - plugin context.
 * @param port - 已绑定的端口。
 * @returns origin 文本，或 `undefined`（地址无法表达成 URL）。
 */
function originOf(ctx, port) {
  try {
    return ctx.webServer.getOrigin();
  } catch {
    const host = ctx.webServer?.host;
    if (typeof host !== "string" || !host || host.includes("%")) return undefined;
    const literal = host.includes(":") ? `[${host}]` : host;
    return `http://${literal}:${port}`;
  }
}

/**
 * 把实际地址写到 `$DSH_HOME/handoff-bridge.json`，供 handoff 侧自动发现。
 *
 * 端口每次重启都会变，写死配置没法用；而这个文件由运行中的插件自己维护，
 * 谁在监听、监听在哪，以它为准。
 *
 * @param file - 端点文件路径。
 * @param payload - 要写的内容。
 */
async function writeEndpoint(file, payload) {
  await mkdir(dirname(file), { recursive: true });
  const temp = `${file}.${process.pid}`;
  // 里面没有秘密（令牌在另一个文件），所以 0644 就够了。
  await writeFile(temp, `${JSON.stringify(payload, null, 2)}\n`, { mode: 0o644 });
  await rename(temp, file);
}

/**
 * 卸载时删掉自己写的端点文件。
 *
 * 只在 pid 对得上时删：多个 DSH 实例共用一个 home 时，别把别人的删了。
 *
 * @param file - 端点文件路径。
 */
async function removeEndpoint(file) {
  try {
    const raw = await readFile(file, "utf8");
    const parsed = JSON.parse(raw);
    if (parsed?.pid !== process.pid) return;
    await unlink(file);
  } catch {
    // 文件不在、读不动、内容坏了：都不是需要报的错。
  }
}

/**
 * Claim the bridge route.
 * @param ctx - plugin context carrying webServer and sessionController.
 * @param config - validated plugin config.
 */
function apply(ctx, config) {
  const prefix = config.path.endsWith("/") ? config.path.slice(0, -1) : config.path;
  const promptPath = `${prefix}/prompt`;
  const healthPath = `${prefix}/health`;
  const endpointFile = config.endpointFile || defaultEndpointFile();
  let tokenPromise;

  const token = () => (tokenPromise ??= resolveToken(config));

  const handle = async (req, res) => {
    const target = new URL(req.url ?? "/", "http://localhost");
    if (target.pathname === healthPath) {
      // 健康检查顺手确认令牌可用：前端监听器启动时会先打这里，
      // 免得等到第一条完成事件才发现插件令牌文件读不到。
      try {
        await token();
      } catch (error) {
        sendJSON(res, 503, { ok: false, error: error?.message ?? String(error) });
        return;
      }
      const port = ctx.webServer?.port;
      sendJSON(res, 200, {
        ok: true,
        name,
        path: promptPath,
        ...(typeof port === "number" && port > 0 ? { port } : {}),
      });
      return;
    }
    if (target.pathname !== promptPath) {
      sendJSON(res, 404, { error: "unknown bridge endpoint" });
      return;
    }
    if (req.method !== "POST") {
      res.writeHead(405, { allow: "POST" });
      res.end();
      return;
    }
    if (config.loopbackOnly && !isLoopback(req.socket?.remoteAddress)) {
      sendJSON(res, 403, { error: "loopback only" });
      return;
    }
    const expected = await token();
    if (!tokenMatches(expected, bearerOf(req.headers.authorization))) {
      sendJSON(res, 401, { error: "unauthorized" });
      return;
    }
    let envelope;
    try {
      envelope = normalizePrompt(JSON.parse(await readBody(req, config.maxBodyBytes)));
    } catch (error) {
      sendJSON(res, error.status ?? 400, { error: error.message });
      return;
    }
    if (envelope.error) {
      sendJSON(res, 400, { error: envelope.error });
      return;
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), config.promptTimeoutMs);
    try {
      await ctx.sessionController.prompt(
        {
          requestId: envelope.requestId,
          sessionId: envelope.sessionId,
          mode: envelope.mode,
          content: [{ type: "text", text: envelope.text }],
        },
        controller.signal,
      );
      sendJSON(res, 200, { accepted: true, sessionId: envelope.sessionId, requestId: envelope.requestId });
    } catch (error) {
      const code = typeof error?.code === "string" ? error.code : "gateway/internal";
      ctx.logger?.warn?.(error);
      sendJSON(res, statusOfRemoteError(code), {
        error: error?.message ?? String(error),
        code,
      });
    } finally {
      clearTimeout(timer);
    }
  };

  ctx.effect(() => ctx.webServer.register({ kind: "prefix", path: prefix, handler: handle }), "handoff-bridge: route");

  // 卸载时清掉端点文件，免得 handoff 读到一个已经不在监听的端口。
  ctx.effect(() => () => removeEndpoint(endpointFile), "handoff-bridge: endpoint");

  // 启动期就把令牌和端点准备好，别等到第一条完成事件：
  // 否则刚装完插件的人去看 $DSH_HOME，什么也看不到，像是没装成功。
  (async () => {
    await token();
    const port = await awaitPort(ctx);
    if (port === undefined) {
      ctx.logger?.warn?.("handoff bridge: the web server did not bind a port; no endpoint was published");
      return;
    }
    const origin = originOf(ctx, port);
    if (origin === undefined) {
      ctx.logger?.warn?.("handoff bridge: the bind address has no URL form; no endpoint was published");
      return;
    }
    const url = `${origin.replace(/\/+$/, "")}${promptPath}`;
    await writeEndpoint(endpointFile, {
      version: ENDPOINT_VERSION,
      name,
      url,
      origin,
      path: promptPath,
      pid: process.pid,
      updatedAt: new Date().toISOString(),
    });
    ctx.logger?.info?.(`handoff bridge listening on ${url}`);
  })().catch((error) => {
    ctx.logger?.warn?.(`handoff bridge: cannot prepare the bridge endpoint: ${error?.message ?? String(error)}`);
  });
}

export {
  Config,
  ENDPOINT_VERSION,
  apply,
  awaitPort,
  defaultEndpointFile,
  inject,
  name,
  normalizePrompt,
  statusOfRemoteError,
};
