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

import { execFile, spawn } from "node:child_process";
import { promisify } from "node:util";
import { randomBytes, timingSafeEqual } from "node:crypto";
import { mkdir, readFile, rename, unlink, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

/** Stable Cordis plugin name. */
const name = "dsh-handoff-studio";

const execFileAsync = promisify(execFile);

/** engineAlive 探测超时（毫秒）。 */
const ENGINE_PROBE_TIMEOUT_MS = 1200;

/**
 * Windows 上 `venv/Scripts/python.exe` 会把真正的解释器再 exec 一层：
 * 外层 wrapper 退出后，监听端口的是内层进程。`netstat /taskkill` 都按 pid 走，
 * 所以必须知道这个「监听 pid」才能可靠释放端口。
 *
 * 这些是可调常量，测试里可以缩短等待时间。
 */
const ENGINE_PROBE_INTERVAL_MS = 50;
const ENGINE_STOP_TIMEOUT_MS = 3000;

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

/**
 * 访问权限隔离的三档，与 `@deepseek-ai/dsh-sandbox` 的 `SandboxMode` 完全一致。
 *
 * 这不是我们自己发明的枚举：宿主 sandboxPolicy 的 `SANDBOX_MODES` 就是这三个值
 * （dsh-sandbox-policy/src/session-mode.ts），传别的值会被静默忽略成「跟随默认」。
 */
const SANDBOX_MODES = Object.freeze(["read-only", "workspace-write", "danger-full-access"]);

/**
 * 沙箱档位 → 宿主权限预设名。
 *
 * 宿主的权限是「沙箱 + 审批」两个旋钮的组合，界面上的名字（只读 / 工作区内修改 /
 * 完全权限）是拿这两个旋钮去预设表里**反查**出来的；两个旋钮对不上任何一行就显示
 * `custom`。发版默认表里档位名与预设名一一同名（dsh-base/cordis.patch.yml 的
 * `permission` 条目），所以直接按同名映射。表被改过时下面的代码会先查目录再决定。
 */
const PRESET_FOR_SANDBOX = Object.freeze({
  "read-only": "read-only",
  "workspace-write": "workspace-write",
  "danger-full-access": "danger-full-access",
});

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
 * The machine-readable reason a turn ended, or null for a clean ending.
 *
 * Only `turn/end` with `reason.kind === "error"` carries one: the agent loop
 * writes `{ kind: "error", error: <LlmError failure> }` and the failure's `code`
 * is the stable taxonomy of `@deepseek-ai/dsh-llm` (`QUOTA`, `ACCOUNT_QUOTA`,
 * `RATE_LIMIT`, `UNKNOWN_MODEL`, …). `completed` / `aborted` / `blocked` /
 * `max-tokens` are not failures and must not be reported as one.
 *
 * Why the panel needs it: when a turn dies because the model's quota is gone,
 * the turn ends *before* an assistant message, so the run only shows "no
 * structured JSON" — which reads like the agent misbehaved instead of like the
 * model being unavailable. The code says which it was, and the studio engine
 * uses exactly this to decide whether switching to another model can help.
 */
function turnFailureOf(event) {
  if (!event || event.type !== "turn/end") return null;
  const reason = event.data?.reason;
  if (!reason || reason.kind !== "error") return null;
  const raw = reason.error;
  if (!raw || typeof raw !== "object") {
    return { code: "UNKNOWN", message: "", status: null };
  }
  const code = typeof raw.code === "string" && raw.code ? raw.code : "UNKNOWN";
  const message = typeof raw.message === "string" ? raw.message : "";
  const status = Number.isInteger(raw.status) ? raw.status : null;
  return { code, message: oneLine(message, 400), status };
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
      const failure = turnFailureOf(event);
      if (failure) {
        const detail = failure.message ? ` ${failure.message}` : "";
        return `■ 第 ${data.turn} 轮结束（error: ${failure.code}${detail}）`;
      }
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
   * 把这一轮选定的沙箱模式落到 Agent 所属的 Session 上。
   *
   * `AgentOptions` 只有 provider/model/reasoningEffort/maxTokens/subagentDepth，
   * 没有权限字段——权限是**按会话**的策略。写它有两条路：
   *
   *  1. 只往 session 追加一条 `sandbox/mode`（老做法）。沙箱确实变了，但审批
   *     策略还停在部署默认（通常是 `ask`），于是 DSH 界面拿「沙箱 + 审批」两个
   *     旋钮去预设表反查时对不上任何一行，权限 chip 显示成 `Custom`。用户会以为
   *     自己没切成，其实只是标签反查失败。
   *  2. `ctx.permissionPresets.set(session, 预设名)`（DSH 自带的权限服务）。它会
   *     一次性写 `permission/preset` + 两个旋钮，和用户在界面上点那一档完全等价，
   *     chip 显示的就是「只读 / 工作区内修改 / 完全权限」。
   *
   * 所以优先走 (2)；(2) 不可用（老版本宿主、或该 profile 没装权限服务）时退回 (1)，
   * 这样至少沙箱隔离是真的生效的，只是标签可能显示 custom——失败要可解释，
   * 不能假装成功。
   *
   * 但 (2) 只在预设自带「不审批」时才用。原因：这个插件跑的是无人值守任务，没人
   * 守着那个会话回答审批框。预设表里只有 `danger-full-access` 是 `never`，
   * 另外两档都带 `ask`——照抄预设会让「只读/工作区可写」的任务卡在一个没人回答
   * 的审批上。宁可让那两档显示 custom，也不能让任务停在那儿。
   *
   * 预设表被部署改过时（比如 `danger-full-access` 被配成别的沙箱），同名预设
   * 未必还是同一个沙箱：那种情况也退回 (1)，保证沙箱正确，不按名字盲选。
   */
  function applySandboxMode(run) {
    if (!run.sandbox) return null;
    const agent = run.handle?.agent;
    const session = agent?.session;
    if (!session || typeof session.append !== "function") {
      // 拿不到 session 就没法保证隔离真的生效，明确失败比假装成功好
      throw new Error("无法应用访问权限隔离：Agent 没有暴露 session");
    }

    // 用 ctx.get 读可选服务：宿主没装权限服务时它返回 undefined；直接读
    // ctx.permissionPresets 在 cordis 里会抛「cannot get property without inject」。
    const presets = ctx.get?.("permissionPresets");
    const name = PRESET_FOR_SANDBOX[run.sandbox];
    if (presets && name) {
      let spec;
      try {
        // resolve() 在不认识这个名字时会抛错，正好判断「这个部署有没有这一档」
        spec = presets.resolve(name);
      } catch {
        spec = undefined;
      }
      // 沙箱必须对得上（部署改过表就不信任），审批必须是 never（无人值守能跑）
      if (spec && spec.sandbox === run.sandbox && spec.approval === "never") {
        try {
          presets.set(session, name);
          run.permissionPreset = name;
          return run.sandbox;
        } catch (error) {
          pushLine(run, `⚠ 权限预设 ${name} 应用失败，退回只写沙箱档位：${oneLine(error?.message ?? String(error), 200)}`);
        }
      }
    }

    session.append("sandbox/mode", { mode: run.sandbox });
    return run.sandbox;
  }

  /**
   * 在 Agent **发布之前**把 preset 组成挂上去，这是宿主自己创建会话时走的同一条路
   * （`dsh-api-session-controller` 的 `composeAgent()`：`presets.resolve()` → setup
   * 里 `presets.mount()`）。
   *
   * 为什么不能在 createAgent/resume 返回之后再 `select()`：`select()` 只允许在
   * 会话开始第一轮**之前**改组成，否则抛 `agent-preset/locked`
   * “This session has already started”。续跑一个跑过的会话必然踩到它——上一轮
   * 的日志明明就在那儿，人点「继续上次会话」却只能得到一句绑定失败。
   *
   * 挂载失败的语义保持不变：宁可这一轮直接失败并说清原因，也不要让 Agent 带着
   * 分析型的工具集跑一圈回来报「我没有工具」。
   */
  function presetSetup(run) {
    if (!config.agentPreset) return undefined;
    return async (agentCtx, agent) => {
      let recorded = null;
      try {
        // projection 读的是会话日志里的 agent-preset/selected（新建会话则读
        // header 的 meta.agentPreset）。读不到就当没有记录，别让一次投影故障
        // 把整轮拦下来。
        recorded = ctx.agentPresets.presetForSession?.(agent.session) ?? null;
      } catch {
        recorded = null;
      }
      let id = config.agentPreset;
      if (recorded) {
        try {
          // 宿主的 resolve() 是 async 的：不 await 就会拿一个 Promise 的 .id，
          // 而且它的 not-found 会变成没人处理的 rejection。
          id = (await ctx.agentPresets.resolve(recorded))?.id ?? recorded;
        } catch {
          pushLine(
            run,
            `⚠ 会话记录的组成 ${recorded} 已不在本机花名册里，改用插件配置的 ${config.agentPreset}`,
          );
          id = config.agentPreset;
        }
      }
      if (typeof ctx.agentPresets.mount !== "function") {
        // 老宿主没有 mount()：留给 createAgent 之后的 select() 兜底
        run.presetTarget = id;
        return;
      }
      try {
        const mounted = await ctx.agentPresets.mount(agentCtx, id);
        run.preset = mounted?.id ?? id;
      } catch (error) {
        throw presetFailure(id, error);
      }
      // 日志必须诚实：续跑时若挂的组成与会话记录的不同（老会话没记录、或记录
      // 的已下架），就把这次选择写进会话事件，而不是让 projection 停在旧值上。
      // 新建会话不用补：宿主已经把它写进 session header（meta.agentPreset），
      // 再追加一条只会让「这一轮动了什么」变得难读。
      if (recorded !== run.preset && run.resumeSessionId) {
        try {
          agent.session.append("agent-preset/selected", { agentPreset: run.preset });
        } catch {
          // 记录失败不影响已经挂好的组成，只是历史里少一行
        }
      }
      pushLine(run, `▶ 执行组成：${run.preset}`);
    };
  }

  /** 把 preset 绑定失败翻成一句能照做的话（含本机实际有哪些）。 */
  function presetFailure(id, error) {
    // 宿主的 not-found 是 RemoteError：结构化字段在 `details`，老版本/普通
    // Error 上可能落在 `data`。两个都看，拿不到就不硬塞提示。
    const payload = error?.details ?? error?.data;
    const available = Array.isArray(payload?.available) ? payload.available : [];
    const hint = available.length > 0 ? `本机可用：${available.join(", ")}` : "";
    const reason = error?.message ?? String(error);
    const suffix = hint ? `（${hint}）` : "";
    return new Error(
      `执行型 Agent 需要 preset ${id}，但绑定失败：${reason}${suffix}。` +
        "确认该 preset 在 DSH 里存在且未损坏（插件配置项 agentPreset）。",
    );
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
      // 模型不可用（额度耗尽 / 限流 / 鉴权 / 未知模型）只能从 turn/end 的
      // 结构化 reason 里读出来：那一轮不会产出 assistant/message，光看最终文本
      // 只会得到「没有结构化结果」，把「额度用完了」显示成「agent 不听话」。
      // 记在这里，Python 侧据此决定「换下一个候选模型」还是「直接失败」。
      const failure = turnFailureOf(event);
      if (failure) run.failure = failure;
      const line = describeEvent(event);
      if (line) pushLine(run, line);
    });

    let handle = null;
    try {
      const agentOptions = {};
      if (run.provider) agentOptions.provider = run.provider;
      if (run.model) agentOptions.model = run.model;
      if (run.effort) agentOptions.reasoningEffort = run.effort;

      // 组成在 publish 之前挂载：宿主的 createAgent/resume 都接受 setup，
      // 而这时候会话还没有任何一轮，正是唯一允许改组成的窗口（见 presetSetup）。
      const setup = presetSetup(run);
      if (run.resumeSessionId) {
        handle = await ctx.agentLoop.resume(ctx, {
          resumeSessionId: run.resumeSessionId,
          agentOptions,
          ...(setup ? { setup } : {}),
          signal: controller.signal,
        });
        run.sessionId = handle.agent.id;
      } else {
        handle = await ctx.agentLoop.createAgent(ctx, {
          sessionId: run.sessionId,
          meta: { cwd: run.cwd, agentPreset: config.agentPreset || undefined },
          agentOptions,
          ...(setup ? { setup } : {}),
          signal: controller.signal,
        });
      }
      run.handle = handle;

      // 访问权限隔离要在 Agent 干活之前就位：它作用于此后的每一次受限调用
      // （bash / fs / terminal），晚一步就等于第一轮无隔离地跑过。
      const appliedSandbox = applySandboxMode(run);
      if (appliedSandbox) {
        pushLine(
          run,
          run.permissionPreset
            ? `▶ 访问权限：${appliedSandbox}（权限模式 ${run.permissionPreset}）`
            : `▶ 访问权限：${appliedSandbox}（宿主的权限模式会显示 custom）`,
        );
      }

      // 无人值守的兜底：不弹审批，别让一次 run 卡在一个等不到人的对话框上。
      //
      // 注意顺序上的因果关系：审批策略是预设的一部分。走了预设（下面
      // permissionPreset 非空）时，审批已经由预设定好——完全权限本来就是
      // never，所以无需再动；此时如果硬改成 never，遇到「只读/工作区可写」
      // 反而会把「沙箱 + 审批」拧成预设表里没有的组合，标签又变回 custom。
      // 所以只有退回「只写沙箱」那条路（没装权限服务）时才补这一手，保证
      // 无人值守这个行为不丢。
      if (!run.permissionPreset) {
        try {
          ctx.approval?.setPolicy?.(handle.agent, "never");
        } catch {
          // 没有 approval 服务的 profile 不影响执行
        }
      }

      // 老宿主没有 agentPresets.mount()：setup 里只记下了目标 id，这里补一次
      // select()。新宿主上 setup 已经把组成挂好，run.preset 非空，跳过。
      if (config.agentPreset && !run.preset && run.presetTarget) {
        try {
          run.preset = await ctx.agentPresets.select(handle.agent, run.presetTarget);
          pushLine(run, `▶ 执行组成：${run.preset ?? run.presetTarget}`);
        } catch (error) {
          throw presetFailure(run.presetTarget, error);
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
  async function engineAlive(port, timeoutMs = ENGINE_PROBE_TIMEOUT_MS) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/api/studio/models`, {
        signal: AbortSignal.timeout(timeoutMs),
        headers: { accept: "application/json" },
      });
      if (!response.ok) return false;
      const body = await response.json();
      return typeof body === "object" && body !== null;
    } catch {
      return false;
    }
  }

  /** Which pid is listening on a loopback TCP port?  `[]` when nobody is. */
  async function portOwnerPids(port) {
    if (process.platform !== "win32") return [];
    try {
      const { stdout } = await execFileAsync(
        "netstat",
        ["-ano", "-p", "TCP"],
        { windowsHide: true, timeout: ENGINE_PROBE_TIMEOUT_MS, encoding: "utf8" },
      );
      const pids = new Set();
      for (const line of stdout.split(/\r?\n/)) {
        if (!/LISTENING/i.test(line)) continue;
        const cells = line.trim().split(/\s+/);
        // IPv4: TCP  127.0.0.1:8791  0.0.0.0:0  LISTENING  1234
        // IPv6: TCP  [::1]:8791      [::]:0     LISTENING  1234
        const local = cells[1] ?? "";
        const pid = Number(cells.at(-1));
        if (!Number.isInteger(pid) || pid <= 0) continue;
        if (/^127\.0\.0\.1:/.test(local) || /^\[::1\]:/.test(local)) {
          if (Number(local.replace(/^.*:/, "")) === port) pids.add(pid);
        }
      }
      return [...pids];
    } catch {
      return [];
    }
  }

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  /**
   * Wait for the port to be free, killing any listener we find.
   * The venv python re-execs a base interpreter, so the pid we spawned is only
   * a wrapper — the real listener must be found by port and killed too.
   * Returns `{ ok, pids }`; `ok: false` means a foreign process refused to die.
   */
  async function freePort(port, timeoutMs = ENGINE_STOP_TIMEOUT_MS) {
    const deadline = Date.now() + timeoutMs;
    const killed = new Set();
    while (Date.now() < deadline) {
      const pids = (await portOwnerPids(port)).filter((pid) => pid !== process.pid);
      if (pids.length === 0) return { ok: true, pids: [...killed] };
      for (const pid of pids) {
        if (killed.has(pid)) continue;
        killed.add(pid);
        try {
          process.kill(pid, "SIGKILL");
          ctx.logger?.info?.(`handoff studio: killed stale engine pid ${pid} on port ${port}`);
        } catch {
          // 进程可能刚好自己退了，下一轮探测会确认
        }
      }
      await sleep(ENGINE_PROBE_INTERVAL_MS);
    }
    const remaining = (await portOwnerPids(port)).filter((pid) => pid !== process.pid);
    return { ok: remaining.length === 0, pids: [...killed] };
  }

  function ensureEngine() {
    // 无论成功与否，都复用同一条 in-flight promise：
    // 否则并发的面板请求会在 engineAlive 的时间窗里各自 spawn 一个引擎，
    // 第二个 bind 8791 就得到 WinError 10048。
    if (engine) return engine.ready;
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

    const record = { child: null, port, ready: null };
    engine = record;
    record.ready = (async () => {
      if (await engineAlive(port)) {
        ctx.logger?.info?.(`handoff studio: reusing the engine already listening on ${port}`);
        return { ok: true, port, reused: true };
      }
      // 有进程占着端口但不是健康引擎（半死不活的旧实例）：先清掉再拉新的。
      const freed = await freePort(port);
      if (!freed.ok) {
        return {
          ok: false,
          error:
            `端口 ${port} 被占用且无法释放（pid ${(await portOwnerPids(port)).join(", ")}）。` +
            "关闭占用程序或改用其它 enginePort 后重启 DSH。",
        };
      }
      return await spawnEngine(paths, port, env, args, record);
    })().then((result) => {
      // 失败的 ready 不能永久缓存：否则一次超时/启动错误会让面板 forever 503，
      // 后续请求再也没有重试机会。清掉 record，下一条请求重新探测并拉起。
      if (!result?.ok && engine === record) engine = null;
      return result;
    });
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
      const done = (ok, result) => {
        if (settled) return;
        settled = true;
        resolve(result !== undefined ? result : { ok, port });
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
          {
            ok: false,
            error:
              `无法启动 studio 引擎（${paths.python}）：${error?.message ?? String(error)}。` +
              "在插件配置里设 pythonPath 指向一个能 import handoff 的解释器。",
          },
        );
      });
      child.on("exit", (code) => {
        const detail = stderrTail.trim().split("\n").slice(-6).join(" | ");
        // WinError 10048 是端口冲突：按错误码精确识别，换成可操作的提示，
        // 不再把 pythonPath / engineModulePath / engineRoot 一并倒给用户（它们与冲突无关）。
        const bindFailure = code === 1 && detail.includes("WinError 10048");
        const error = bindFailure
          ? `studio 引擎启动失败：端口 ${port} 已被占用（WinError 10048）。` +
            "通常是上一次的引擎进程还没退出（venv python 会再 exec 一层基础解释器，外层被杀掉后内层仍占着端口）。" +
            "请在 DSH 里重载/禁用再启用本插件以释放旧引擎，或改用其它 enginePort 后重启 DSH。"
          : `studio 引擎退出（code ${code}）${detail ? `：${detail}` : ""}。` +
            `检查 pythonPath=${paths.python}、engineModulePath=${paths.modulePath || "(空)"}、engineRoot=${paths.root || "(空)"}。`;
        done(false, { ok: false, error });
        if (engine === record) engine = null;
      });
      // 等不到横幅也别一直挂着：端口可能已经能连（横幅被缓存/丢失），
      // 但要先确认真的连得上，避免把失败当成成功而静默挂掉。
      setTimeout(async () => {
        const alive = await engineAlive(port, 600);
        if (alive) done(true);
        else if (!settled) {
          const detail = stderrTail.trim().split("\n").slice(-6).join(" | ");
          done(false, {
            ok: false,
            error:
              `studio 引擎启动超时（4s）且端口 ${port} 无响应${detail ? `：${detail}` : ""}。` +
              `检查 pythonPath=${paths.python}、engineModulePath=${paths.modulePath || "(空)"}、engineRoot=${paths.root || "(空)"}。`,
          });
        }
      }, 4000).unref?.();
    });
  }

  async function ensureEngineAlive() {
    const status = await ensureEngine();
    if (!status.ok) throw Object.assign(new Error(status.error), { status: 503 });
    return status.port;
  }

  async function stopEngine() {
    const record = engine;
    engine = null;
    // 先杀 wrapper；venv python 会再 exec 一层基础解释器，真正的监听 pid
    // 只能按端口找到，所以随后用 freePort 把它一起收掉。
    if (record?.child) {
      try {
        record.child.kill();
      } catch {
        // 已经退出
      }
    }
    if (record?.port) await freePort(record.port);
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
        // 组成是怎么挂上去的：setup（发布前 mount，唯一对续跑也成立的路）或
        // select（老宿主的退回路径）。人排查「续跑报 This session has already
        // started」时先看这一行。
        agentPresetBinding: typeof ctx.agentPresets?.mount === "function" ? "setup" : "select",
        // 访问权限隔离靠宿主的权限服务才写得成「命名预设」；没有它就只能退回
        // 只写沙箱档位，界面会显示 custom。自检时一眼能看出是哪条路。
        permissionPresets: Boolean(ctx.get?.("permissionPresets")),
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
        // 访问权限隔离：三档沙箱模式，null 表示跟随部署默认。
        // 这不是 AgentOptions 能表达的东西（AgentOptions 只有 provider/model/
        // reasoningEffort/maxTokens/subagentDepth），必须走每会话覆盖，
        // 见 applySandboxMode()。
        sandbox: SANDBOX_MODES.includes(payload.sandbox) ? payload.sandbox : null,
        // 实际应用的宿主权限预设名；null = 只写了沙箱档位（界面会显示 custom）
        permissionPreset: null,
        resumeSessionId,
        sessionId: resumeSessionId || newSessionId(),
        // 实际挂载的执行组成；null = 这轮没挂（旧宿主只走 select 时有 presetTarget）
        preset: null,
        // 旧宿主退回 select() 时才用得到（没有 agentPresets.mount）
        presetTarget: null,
        logs: [],
        text: "",
        error: null,
        // 本轮最后一轮的模型级失败（额度 / 限流 / 鉴权…），来自 turn/end 的
        // 结构化 reason；null 表示没观察到这类失败。
        failure: null,
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
        sandbox: run.sandbox,
        // 权限预设随后在 execute() 里才写入，这里先不报；状态接口里有
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
        sandbox: run.sandbox,
        // 实际写入的宿主权限预设；null 表示这轮只写了沙箱档位（界面显示 custom）
        permissionPreset: run.permissionPreset,
        // 实际挂载的执行组成（agent preset）；null 表示这轮没挂
        agentPreset: run.preset ?? null,
        cursor: from + slice.length,
        logs: slice,
        text: run.phase === "finished" ? run.text : "",
        error: run.error,
        // 模型级失败（额度 / 限流 / 鉴权 / 未知模型）的结构化原因：Python 侧
        // 用它区分「这个模型这轮用不了，换下一个候选」和「agent 真的没做好」。
        failure: run.failure,
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
  turnFailureOf,
  validateConfig,
};
export default { name, apply, Config, inject };
