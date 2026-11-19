/**
 * Host half of `dsh-handoff-studio`.
 *
 * Two jobs, both on the loopback DSH webserver:
 *
 *  1. **Agent executor.** handoff's Python worker posts an implementation task and
 *     this plugin runs it as a real DSH Agent on a model chosen in the panel:
 *       POST /handoff-studio/agent/run            start one run
 *       GET  /handoff-studio/agent/run/:id        poll phase / new log lines / final text
 *       POST /handoff-studio/agent/run/:id/cancel stop it
 *
 *  2. **Panel backend proxy.** The React panel talks to the Python studio engine
 *     (`python -m handoff_studio`, loopback JSON API) through this same origin, so
 *     the browser never needs CORS and the engine never needs to be publicly
 *     reachable. The engine is spawned on demand and reaped on unload:
 *       ALL  /handoff-studio/panel/*              → engine :8791/*
 *
 * Plus `GET /handoff-studio/health` (token + model catalog probe) and
 * `GET /handoff-studio/models` (the live model catalog the panel's picker uses).
 *
 * The token file and self-announced endpoint file are the same convention as
 * `dsh-handoff-bridge`, so the Python side discovers this plugin without config.
 *
 * @module dsh-handoff-studio
 */

import { spawn } from "node:child_process";
import { randomBytes, timingSafeEqual } from "node:crypto";
import { mkdir, readFile, rename, unlink, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

/** Stable Cordis plugin name. */
const name = "dsh-handoff-studio";

/** Services required before the routes can be claimed. */
const inject = ["webServer", "agentLoop", "sessionController", "agentPresets"];

/** Endpoint file format version; bump together with the Python side. */
const ENDPOINT_VERSION = 1;

/** Prefix for every route this plugin owns. */
const ROUTE = "/handoff-studio";

/** One Agent at a time: a handoff worker is single-concurrency by design too. */
const MAX_CONCURRENT_RUNS = 1;

/** How many log lines one poll may return. */
const LOG_PAGE = 400;

/** Default engine port (8790 is the original panel, 8789 the listener). */
const DEFAULT_ENGINE_PORT = 8791;

const FIELDS = {
  token: { kind: "string", fallback: "", check: () => true },
  tokenFile: { kind: "string", fallback: "", check: () => true },
  endpointFile: { kind: "string", fallback: "", check: () => true },
  loopbackOnly: { kind: "boolean", fallback: true, check: () => true },
  maxBodyBytes: {
    kind: "number",
    fallback: 4 * 1024 * 1024,
    check: (v) => (Number.isInteger(v) && v >= 1024) || "must be an integer >= 1024",
  },
  panelProxy: { kind: "boolean", fallback: true, check: () => true },
  enginePort: {
    kind: "number",
    fallback: DEFAULT_ENGINE_PORT,
    check: (v) => (Number.isInteger(v) && v >= 1 && v <= 65535) || "must be a port in 1..65535",
  },
  pythonPath: { kind: "string", fallback: "", check: () => true },
  engineRoot: { kind: "string", fallback: "", check: () => true },
  engineModulePath: { kind: "string", fallback: "", check: () => true },
  /**
   * Agent preset used for implementation runs. This matters: an Agent created
   * without one joins the *current* Session's composition, so a run started from
   * an analysis-only preset would inherit analysis tools and have no shell at all.
   * `standard` is the full coding composition.
   */
  agentPreset: { kind: "string", fallback: "standard", check: () => true },
  runTimeoutMs: {
    kind: "number",
    fallback: 3_600_000,
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

/** Standard Schema face cordis reads (`runtime.Config['~standard'].validate`). */
const Config = {
  "~standard": { version: 1, vendor: "dsh-handoff-studio", validate: validateConfig },
};

/** DSH config root; the Python side resolves it with the same rules. */
function dshHome() {
  return process.env.DSH_HOME || join(homedir(), ".dsh");
}

function defaultTokenFile() {
  return join(dshHome(), "handoff-studio.token");
}

function defaultEndpointFile() {
  return join(dshHome(), "handoff-studio.json");
}

/**
 * Read the shared secret, creating one on first start so a fresh install works
 * without the user copying anything between the two halves.
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

function sendJSON(res, status, body) {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
    "content-length": Buffer.byteLength(payload),
  });
  res.end(payload);
}

/**
 * 等 webserver 真正绑定端口。
 *
 * `apply()` 跑在服务启动阶段，此时 `webServer.port` 还是 `undefined`——
 * 只有 listen 回调里才会写进去。这里轮询等它出现，超时就放弃（不阻塞卸载）。
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

async function writeEndpoint(file, payload) {
  await mkdir(dirname(file), { recursive: true });
  const temp = `${file}.${process.pid}`;
  // No secret in here (the token lives in another file), so 0644 is enough.
  await writeFile(temp, `${JSON.stringify(payload, null, 2)}\n`, { mode: 0o644 });
  await rename(temp, file);
}

async function removeEndpoint(file) {
  try {
    const raw = await readFile(file, "utf8");
    const parsed = JSON.parse(raw);
    if (parsed?.pid !== process.pid) return;
    await unlink(file);
  } catch {
    // Missing, unreadable or malformed: nothing worth reporting.
  }
}

/** A fresh Session identity in the shape DSH uses. */
function newSessionId() {
  return `session-${randomUUIDish()}`;
}

function randomUUIDish() {
  const bytes = randomBytes(16);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = bytes.toString("hex");
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20, 32),
  ].join("-");
}

/** Flatten one content block array into one display line. */
function textOfContent(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  const parts = [];
  for (const block of content) {
    if (block && block.type === "text" && typeof block.text === "string") {
      parts.push(block.text);
    }
  }
  return parts.join("\n");
}

/** One-line summary of a tool call, the way the panel renders it. */
function briefArguments(raw) {
  if (typeof raw !== "string" || !raw.trim()) return "";
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return raw.slice(0, 120);
  }
  if (parsed === null || typeof parsed !== "object") return String(parsed).slice(0, 120);
  for (const key of ["command", "file_path", "path", "pattern", "url", "description", "prompt"]) {
    const value = parsed[key];
    if (typeof value === "string" && value.trim()) return value.replace(/\s+/g, " ").slice(0, 160);
  }
  try {
    return JSON.stringify(parsed).slice(0, 120);
  } catch {
    return "";
  }
}

/** Collapse whitespace and bound one log line. */
function oneLine(text, limit = 400) {
  const flat = String(text ?? "").replace(/\s+/g, " ").trim();
  return flat.length > limit ? `${flat.slice(0, limit)}…` : flat;
}

/**
 * Turn one committed session event into a human log line, or null.
 * Mirrors what the panel showed for codex/zcode so the log reads the same way.
 */
function describeEvent(event) {
  if (!event || typeof event !== "object") return null;
  const data = event.data ?? {};
  switch (event.type) {
    case "turn/start":
      return `▶ 第 ${data.turn} 轮开始`;
    case "turn/end": {
      const reason = data.reason?.kind ?? "?";
      return `■ 第 ${data.turn} 轮结束（${reason}）`;
    }
    case "assistant/message": {
      const text = textOfContent(data.message?.content);
      return text ? oneLine(text) : null;
    }
    case "tool/call": {
      const brief = briefArguments(data.arguments);
      return `🔧 ${data.name ?? "tool"}${brief ? `  ${brief}` : ""}`;
    }
    case "tool/result": {
      if (!data.error) return null;
      const detail = data.error.reason || data.error.message || data.error.code || "";
      return `⚠ 工具返回错误: ${oneLine(detail, 200)}`;
    }
    default:
      return null;
  }
}

/** This module lives at `<packageDir>/lib/index.js`, so the engine is a sibling. */
function bundledEngineDir() {
  try {
    return join(dirname(dirname(fileURLToPath(import.meta.url))), "engine");
  } catch {
    return "";
  }
}

/**
 * @param ctx - Cordis context of the plugin fiber.
 * @param config - validated plugin config from the profile patch.
 */
function apply(ctx, config) {
  const endpointFile = config.endpointFile || defaultEndpointFile();
  const prefix = ROUTE;
  const runs = new Map();
  let tokenPromise;
  let engine = null;

  const token = () => (tokenPromise ??= resolveToken(config));

  // -- 模型目录 -------------------------------------------------------------
  /** The live catalog the panel's picker shows. Never cached: models come and go. */
  async function modelCatalog() {
    return await ctx.sessionController.modelCatalog();
  }

  // -- Agent 执行 ------------------------------------------------------------
  function activeRun() {
    for (const run of runs.values()) {
      if (run.phase === "running") return run;
    }
    return null;
  }

  function pushLine(run, text) {
    if (!text) return;
    for (const line of String(text).split("\n")) {
      if (!line.trim()) continue;
      run.logs.push(line);
    }
    // 日志只需要最近这么多行：面板本来就只显示尾巴
    if (run.logs.length > 5000) run.logs.splice(0, run.logs.length - 5000);
  }

  /**
   * Create (or resume) the agent, admit one prompt, and drive it to quiescence.
   * Everything is recorded on the run record so the Python side can poll it.
   */
  async function execute(run) {
    const controller = new AbortController();
    run.controller = controller;
    const stopListening = ctx.on("session/event", (session, event) => {
      if (session?.id !== run.sessionId) return;
      if (event.type === "assistant/message") {
        const text = textOfContent(event.data?.message?.content);
        if (text) run.text = text;
        if (event.data?.interrupted) run.interrupted = true;
      }
      const line = describeEvent(event);
      if (line) pushLine(run, line);
    });

    let handle = null;
    try {
      const agentOptions = {};
      if (run.provider) agentOptions.provider = run.provider;
      if (run.model) agentOptions.model = run.model;
      if (run.effort) agentOptions.reasoningEffort = run.effort;

      if (run.resumeSessionId) {
        handle = await ctx.agentLoop.resume(ctx, {
          resumeSessionId: run.resumeSessionId,
          agentOptions,
          signal: controller.signal,
        });
        run.sessionId = handle.agent.id;
      } else {
        handle = await ctx.agentLoop.createAgent(ctx, {
          sessionId: run.sessionId,
          meta: { cwd: run.cwd, agentPreset: config.agentPreset || undefined },
          agentOptions,
          signal: controller.signal,
        });
      }
      run.handle = handle;

      // 无人值守：不弹审批，别让一次 run 卡在一个等不到人的对话框上
      try {
        ctx.approval?.setPolicy?.(handle.agent, "never");
      } catch {
        // 没有 approval 服务的 profile 不影响执行
      }

      // Agent 的执行能力来自它的 preset 组成。不显式绑定时，新建 Agent 会加入
      // **当前 Session** 的组成——从分析型会话里发起的 run 因此没有 shell，
      // 连写一个文件都做不到。这里显式选 preset，并在选不上时直接失败，
      // 而不是让 Agent 跑一圈回来报「我没有工具」。
      if (config.agentPreset) {
        try {
          await ctx.agentPresets.select(handle.agent, config.agentPreset);
          pushLine(run, `▶ 执行组成：${config.agentPreset}`);
        } catch (error) {
          throw new Error(
            `执行型 Agent 需要 preset ${config.agentPreset}，但绑定失败：${error?.message ?? String(error)}。` +
              "确认该 preset 在 DSH 里存在且未损坏（插件配置项 agentPreset）。",
          );
        }
      }

      pushLine(run, `▶ Agent 就绪：${run.sessionId}`);
      await ctx.sessionController.prompt(
        {
          requestId: run.requestId,
          sessionId: run.sessionId,
          mode: "queue",
          content: [{ type: "text", text: run.prompt }],
        },
        controller.signal,
      );

      // 等到 Agent 真正闲下来（或超时）
      const deadline = Date.now() + config.runTimeoutMs;
      for (;;) {
        if (run.cancelRequested) break;
        const remaining = Math.max(1000, Math.min(30_000, deadline - Date.now()));
        const waited = await ctx.sessionController.wait(
          { sessionId: run.sessionId, timeoutMs: remaining },
          controller.signal,
        );
        if (waited.reason === "idle") break;
        if (waited.reason === "input-required") {
          // 没有人能回答：让 Agent 自己按「做不完」处理，别无限等
          pushLine(run, "⚠ Agent 在等用户输入，但本轮无人值守；等待超时后按未完成处理");
        }
        if (Date.now() >= deadline) {
          run.timedOut = true;
          break;
        }
      }
      if (run.cancelRequested) {
        try {
          ctx.sessionController.cancel({ sessionId: run.sessionId });
        } catch {
          // 已经结束就没什么可取消的
        }
      }
      run.phase = "finished";
    } catch (error) {
      run.phase = "finished";
      run.error = oneLine(error?.message ?? String(error), 800) || "agent run failed";
      pushLine(run, `⚠ ${run.error}`);
    } finally {
      stopListening?.();
      run.controller = null;
      try {
        await handle?.dispose?.();
      } catch {
        // 释放失败不影响已经拿到的结果
      }
    }
  }

  // -- 引擎进程 -------------------------------------------------------------
  /**
   * Locate the studio engine package. The bundle ships it under `engine/`, so the
   * default is `<packageDir>/engine`; a profile install can override it.
   */
  function enginePaths() {
    return {
      python: config.pythonPath || process.env.HANDOFF_STUDIO_PYTHON || "python",
      root: config.engineRoot || process.env.HANDOFF_ROOT || "",
      modulePath: config.engineModulePath || bundledEngineDir(),
    };
  }

  /** Is something already answering as a studio engine on this port? */
  async function engineAlive(port) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/api/studio/models`, {
        signal: AbortSignal.timeout(1200),
        headers: { accept: "application/json" },
      });
      if (!response.ok) return false;
      const body = await response.json();
      return typeof body === "object" && body !== null;
    } catch {
      return false;
    }
  }

  function ensureEngine() {
    if (engine && engine.child && engine.child.exitCode === null) return engine.ready;
    const paths = enginePaths();
    const port = config.enginePort;
    const env = {
      ...process.env,
      PYTHONPATH: paths.modulePath
        ? `${paths.modulePath}${process.platform === "win32" ? ";" : ":"}${process.env.PYTHONPATH ?? ""}`
        : process.env.PYTHONPATH,
      PYTHONUTF8: "1",
      PYTHONIOENCODING: "utf-8",
      PYTHONUNBUFFERED: "1",
    };
    const args = ["-m", "handoff_studio", "--port", String(port)];
    if (paths.root) args.push("--root", paths.root);

    // 端口上已经有一个引擎在跑（比如插件重启、而旧引擎还活着）：直接用它，
    // 别去 spawn 第二个——第二个会在 bind 时失败，或者更糟，让请求落到旧代码上。
    const record = { child: null, port, ready: null };
    engine = record;
    record.ready = (async () => {
      if (await engineAlive(port)) {
        ctx.logger?.info?.(`handoff studio: reusing the engine already listening on ${port}`);
        return { ok: true, port, reused: true };
      }
      return await spawnEngine(paths, port, env, args, record);
    })();
    return record.ready;
  }

  function spawnEngine(paths, port, env, args, record) {
    const child = spawn(paths.python, args, {
      env,
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    });
    record.child = child;
    return new Promise((resolve) => {
      let settled = false;
      // 引擎把真正的失败原因写在 stderr（比如缺 handoff 包、config 加载失败）。
      // 只报 "exited (1)" 等于让人猜，所以把尾巴留着一起返回。
      let stderrTail = "";
      const done = (ok, reason) => {
        if (settled) return;
        settled = true;
        resolve(ok ? { ok: true, port } : { ok: false, error: reason });
      };
      child.stdout?.on("data", () => {});
      child.stderr?.on("data", (chunk) => {
        const text = String(chunk);
        stderrTail = `${stderrTail}${text}`.slice(-1200);
        ctx.logger?.debug?.(`studio engine: ${text.trim()}`);
        if (text.includes("studio 引擎已启动")) done(true);
      });
      child.on("error", (error) => {
        done(
          false,
          `无法启动 studio 引擎（${paths.python}）：${error?.message ?? String(error)}。` +
            "在插件配置里设 pythonPath 指向一个能 import handoff 的解释器。",
        );
      });
      child.on("exit", (code) => {
        const detail = stderrTail.trim().split("\n").slice(-6).join(" | ");
        done(
          false,
          `studio 引擎退出（code ${code}）${detail ? `：${detail}` : ""}。` +
            `检查 pythonPath=${paths.python}、engineModulePath=${paths.modulePath || "(空)"}、engineRoot=${paths.root || "(空)"}。`,
        );
        if (engine === record) engine = null;
      });
      // 等不到横幅也别一直挂着：端口可能已经能连（横幅被缓存/丢失）
      setTimeout(() => done(true), 4000).unref?.();
    });
  }

  async function ensureEngineAlive() {
    const status = await ensureEngine();
    if (!status.ok) throw Object.assign(new Error(status.error), { status: 503 });
    return status.port;
  }

  function stopEngine() {
    const record = engine;
    engine = null;
    if (!record?.child) return;
    try {
      record.child.kill();
    } catch {
      // 已经退出
    }
  }

  /** Proxy one panel request to the studio engine, same origin, no CORS. */
  async function proxyPanel(req, res, rest, search) {
    let port;
    try {
      port = await ensureEngineAlive();
    } catch (error) {
      sendJSON(res, 503, { ok: false, error: error?.message ?? String(error) });
      return;
    }
    const target = `http://127.0.0.1:${port}${rest}${search}`;
    let body;
    if (req.method !== "GET" && req.method !== "HEAD") {
      try {
        body = await readBody(req, config.maxBodyBytes);
      } catch (error) {
        sendJSON(res, error.status ?? 413, { ok: false, error: error.message });
        return;
      }
    }
    try {
      const upstream = await fetch(target, {
        method: req.method,
        headers: { "content-type": "application/json; charset=utf-8", accept: "application/json" },
        body: body === undefined ? undefined : body,
      });
      const text = await upstream.text();
      res.writeHead(upstream.status, {
        "content-type": "application/json; charset=utf-8",
        "cache-control": "no-store",
        "content-length": Buffer.byteLength(text),
      });
      res.end(text);
    } catch (error) {
      sendJSON(res, 502, { ok: false, error: `studio 引擎不可达: ${error?.message ?? String(error)}` });
    }
  }

  // -- 路由 -----------------------------------------------------------------
  const handle = async (req, res) => {
    const target = new URL(req.url ?? "/", "http://handoff-studio.invalid");
    let rest = target.pathname.slice(prefix.length);
    if (!rest.startsWith("/")) rest = `/${rest}`;

    if (config.loopbackOnly && !isLoopback(req.socket?.remoteAddress)) {
      sendJSON(res, 403, { ok: false, error: "loopback only" });
      return;
    }

    // 面板代理不需要令牌：它由浏览器同源请求，走的是 DSH 自己的会话认证层
    if (config.panelProxy && (rest === "/panel" || rest.startsWith("/panel/"))) {
      const inner = rest.slice("/panel".length) || "/";
      await proxyPanel(req, res, inner, target.search);
      return;
    }

    if (rest === "/health") {
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
        version: "0.1.0",
        path: `${prefix}/agent/run`,
        panel: `${prefix}/panel`,
        ...(typeof port === "number" && port > 0 ? { port } : {}),
      });
      return;
    }

    // 令牌守门：以下都是 Python 侧或面板用的内部接口
    const expected = await token();
    if (!tokenMatches(expected, bearerOf(req.headers.authorization))) {
      sendJSON(res, 401, { ok: false, error: "unauthorized" });
      return;
    }

    if (rest === "/models") {
      try {
        const catalog = await modelCatalog();
        sendJSON(res, 200, { ok: true, catalog });
      } catch (error) {
        sendJSON(res, 500, { ok: false, error: error?.message ?? String(error) });
      }
      return;
    }

    // 诊断用：执行型 Agent 必须绑定到有 shell 的 preset，所以先能看清有哪些。
    if (rest === "/presets") {
      try {
        const presets = await ctx.agentPresets.list();
        sendJSON(res, 200, {
          ok: true,
          configured: config.agentPreset,
          presets: presets.map((preset) => ({
            id: preset.id,
            name: preset.name ?? null,
            isDefault: Boolean(preset.isDefault),
            broken: preset.broken ?? null,
          })),
        });
      } catch (error) {
        sendJSON(res, 500, { ok: false, error: error?.message ?? String(error) });
      }
      return;
    }

    // 诊断用：确认浏览器半真的被登记进了 boot graph（否则侧边栏不会出现入口），
    // 顺带对齐引擎状态。装完插件想知道「到底有没有加载」时打这里。
    if (rest === "/selfcheck") {
      const graph = ctx.get?.("clientModules")?.graph?.() ?? null;
      const entry = graph?.entries?.find((row) => row.id === name) ?? null;
      const engineStatus = await ensureEngine().catch((error) => ({ ok: false, error: String(error) }));
      sendJSON(res, 200, {
        ok: true,
        name,
        client: {
          registered: Boolean(entry),
          entry,
          totalEntries: graph?.entries?.length ?? 0,
        },
        engine: engineStatus,
        agentPreset: config.agentPreset,
        runs: [...runs.values()].length,
      });
      return;
    }

    if (rest === "/agent/run" && req.method === "POST") {
      let payload;
      try {
        payload = JSON.parse(await readBody(req, config.maxBodyBytes));
      } catch (error) {
        sendJSON(res, error.status ?? 400, { ok: false, error: error?.message ?? String(error) });
        return;
      }
      const cwd = typeof payload?.cwd === "string" ? payload.cwd.trim() : "";
      const prompt = typeof payload?.prompt === "string" ? payload.prompt : "";
      if (!cwd) {
        sendJSON(res, 400, { ok: false, error: "cwd is required" });
        return;
      }
      if (!prompt.trim()) {
        sendJSON(res, 400, { ok: false, error: "prompt must contain non-whitespace" });
        return;
      }
      if (runs.size >= MAX_CONCURRENT_RUNS && activeRun()) {
        sendJSON(res, 409, { ok: false, error: "a studio Agent run is already in progress" });
        return;
      }
      const runId = `run-${randomUUIDish()}`;
      const resumeSessionId =
        typeof payload.resumeSessionId === "string" && payload.resumeSessionId.trim()
          ? payload.resumeSessionId.trim()
          : null;
      const run = {
        id: runId,
        phase: "running",
        cwd,
        prompt,
        requestId: typeof payload.requestId === "string" && payload.requestId.trim()
          ? payload.requestId.trim()
          : `handoff-studio-${runId}`,
        provider: typeof payload.provider === "string" ? payload.provider : null,
        model: typeof payload.model === "string" ? payload.model : null,
        effort: typeof payload.effort === "string" ? payload.effort : null,
        resumeSessionId,
        sessionId: resumeSessionId || newSessionId(),
        logs: [],
        text: "",
        error: null,
        timedOut: false,
        interrupted: false,
        cancelRequested: false,
        startedAt: new Date().toISOString(),
        handle: null,
        controller: null,
      };
      runs.set(runId, run);
      ctx.logger?.info?.(`handoff studio: run ${runId} (${run.model ?? "default model"}) in ${cwd}`);
      execute(run).catch((error) => {
        run.phase = "finished";
        run.error = oneLine(error?.message ?? String(error), 800);
      });
      sendJSON(res, 202, {
        ok: true,
        runId,
        sessionId: run.sessionId,
        provider: run.provider,
        model: run.model,
        effort: run.effort,
      });
      return;
    }

    const runMatch = /^\/agent\/run\/([A-Za-z0-9_-]+)$/.exec(rest);
    if (runMatch && req.method === "GET") {
      const run = runs.get(runMatch[1]);
      if (!run) {
        sendJSON(res, 404, { ok: false, error: "run not found" });
        return;
      }
      const after = Number(target.searchParams.get("after") ?? "0");
      const from = Number.isFinite(after) && after >= 0 ? after : 0;
      const slice = run.logs.slice(from, from + LOG_PAGE);
      sendJSON(res, 200, {
        ok: true,
        runId: run.id,
        phase: run.phase,
        sessionId: run.sessionId,
        provider: run.provider,
        model: run.model,
        effort: run.effort,
        cursor: from + slice.length,
        logs: slice,
        text: run.phase === "finished" ? run.text : "",
        error: run.error,
        timedOut: run.timedOut,
        interrupted: run.interrupted,
        startedAt: run.startedAt,
      });
      return;
    }

    const cancelMatch = /^\/agent\/run\/([A-Za-z0-9_-]+)\/cancel$/.exec(rest);
    if (cancelMatch && req.method === "POST") {
      const run = runs.get(cancelMatch[1]);
      if (!run) {
        sendJSON(res, 404, { ok: false, error: "run not found" });
        return;
      }
      run.cancelRequested = true;
      sendJSON(res, 200, { ok: true, cancelling: run.id });
      return;
    }

    sendJSON(res, 404, { ok: false, error: `unknown studio endpoint: ${rest}` });
  };

  ctx.effect(() => ctx.webServer.register({ kind: "prefix", path: prefix, handler: handle }), "handoff-studio: routes");
  ctx.effect(() => () => stopEngine(), "handoff-studio: engine");
  ctx.effect(() => () => removeEndpoint(endpointFile), "handoff-studio: endpoint");

  // Publish the endpoint early so the Python side can find the port without config.
  (async () => {
    await token();
    const port = await awaitPort(ctx);
    if (port === undefined) {
      ctx.logger?.warn?.("handoff studio: the web server did not bind a port; no endpoint was published");
      return;
    }
    const origin = originOf(ctx, port);
    if (origin === undefined) {
      ctx.logger?.warn?.("handoff studio: the bind address has no URL form; no endpoint was published");
      return;
    }
    const url = `${origin.replace(/\/+$/, "")}${prefix}`;
    await writeEndpoint(endpointFile, {
      version: ENDPOINT_VERSION,
      name,
      url,
      origin,
      path: prefix,
      pid: process.pid,
      updatedAt: new Date().toISOString(),
    });
    ctx.logger?.info?.(`handoff studio listening on ${url}`);
  })().catch((error) => {
    ctx.logger?.warn?.(`handoff studio: cannot publish the endpoint: ${error?.message ?? String(error)}`);
  });
}

export {
  Config,
  ENDPOINT_VERSION,
  ROUTE,
  apply,
  awaitPort,
  defaultEndpointFile,
  defaultTokenFile,
  describeEvent,
  inject,
  name,
  validateConfig,
};
export default { name, apply, Config, inject };
