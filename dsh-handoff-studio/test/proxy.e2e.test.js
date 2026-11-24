/**
 * The panel proxy chain, for real: host half → spawned Python engine → JSON back.
 *
 * This is the one test that proves the two halves agree on route names and shapes.
 * It needs a Python with `handoff` importable; when that is unavailable the test
 * skips instead of failing, so `node --test` stays usable on any machine.
 *
 * Override with HANDOFF_STUDIO_TEST_PYTHON / HANDOFF_STUDIO_TEST_PYTHONPATH.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { existsSync } from "node:fs";
import { mkdtemp, rm } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawn, spawnSync } from "node:child_process";

import { apply, ROUTE } from "../lib/index.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const ENGINE_DIR = join(HERE, "..", "engine");
const PYTHON = process.env.HANDOFF_STUDIO_TEST_PYTHON || "python";
const PYTHONPATH = process.env.HANDOFF_STUDIO_TEST_PYTHONPATH || "";
const STORE_ROOT =
  process.env.HANDOFF_STUDIO_TEST_ROOT || process.env.HANDOFF_ROOT || join(homedir(), "code", "handoff-store");

/**
 * Boot a minimal fake Cordis ctx with a real HTTP listener and the plugin applied.
 * Returns the ctx plus the route handler and an async disposer for the effects.
 */
async function bootPlugin(dir, enginePort, token = "e2e-token") {
  const routes = [];
  const effects = [];
  const server = createServer((req, res) => routes[0]?.handler(req, res));
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = server.address().port;

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
    sessionController: { modelCatalog: async () => ({ default: {}, groups: [], routableProviders: [] }) },
    agentLoop: {},
    logger: { info: () => {}, warn: () => {}, debug: () => {} },
    on: () => () => {},
    effect: (factory) => {
      effects.push(factory());
      return () => {};
    },
  };

  apply(ctx, {
    token,
    tokenFile: join(dir, "handoff-studio.token"),
    endpointFile: join(dir, "handoff-studio.json"),
    loopbackOnly: true,
    maxBodyBytes: 4 * 1024 * 1024,
    panelProxy: true,
    enginePort,
    pythonPath: PYTHON,
    engineRoot: STORE_ROOT,
    engineModulePath: ENGINE_DIR,
    runTimeoutMs: 30_000,
  });

  const dispose = async () => {
    for (const effect of effects) {
      try {
        await effect?.();
      } catch {
        /* ignore */
      }
    }
    await new Promise((resolve) => server.close(resolve));
  };
  return { ctx, port, dispose };
}

/** Wait until /api/state answers through the plugin proxy, up to `tries` * 250ms. */
async function waitForEngine(port, tries = 60) {
  let lastError = "";
  for (let i = 0; i < tries; i += 1) {
    const response = await fetch(`http://127.0.0.1:${port}${ROUTE}/panel/api/state`);
    if (response.ok) return await response.json();
    lastError = `${response.status} ${await response.text()}`;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`engine did not come up: ${lastError}`);
}

/** True when the chosen interpreter can import the handoff package. */
function pythonReady() {
  if ((PYTHON.includes("/") || PYTHON.includes("\\")) && !existsSync(PYTHON)) return false;
  const probe = spawnSync(PYTHON, ["-c", "import handoff, jsonschema"], {
    env: { ...process.env, PYTHONPATH },
    encoding: "utf8",
  });
  return probe.status === 0;
}

function freePort() {
  return new Promise((resolve) => {
    const probe = createServer();
    probe.listen(0, "127.0.0.1", () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
  });
}

test("the panel proxy reaches a real studio engine", async (t) => {
  if (!pythonReady()) {
    t.skip(`python with handoff not available (${PYTHON})`);
    return;
  }

  const dir = await mkdtemp(join(tmpdir(), "studio-e2e-"));
  const enginePort = await freePort();
  const { port, dispose } = await bootPlugin(dir, enginePort);

  t.after(async () => {
    await dispose();
    await rm(dir, { recursive: true, force: true });
  });

  // 引擎是懒启动的：第一条代理请求会拉起它，所以要给它一点时间
  const body = await waitForEngine(port);
  // 面板真正依赖的字段
  assert.equal(body.default_executor, "dsh");
  assert.deepEqual(body.executors_order, ["dsh", "codex", "zcode"]);
  assert.ok(body.executors && "dsh" in body.executors);
  assert.ok(Array.isArray(body.projects));

  // 任务列表也能通
  const tasks = await fetch(`http://127.0.0.1:${port}${ROUTE}/panel/api/tasks`);
  assert.equal(tasks.status, 200);
  const list = await tasks.json();
  assert.ok(Array.isArray(list.tasks));
});

test("a stale process holding the engine port is cleared before spawn", async (t) => {
  if (!pythonReady()) {
    t.skip(`python with handoff not available (${PYTHON})`);
    return;
  }
  if (process.platform !== "win32") {
    t.skip("port-owner cleanup relies on Windows netstat/taskkill");
    return;
  }

  const dir = await mkdtemp(join(tmpdir(), "studio-e2e-stale-"));
  const enginePort = await freePort();

  // 用一个**独立子进程**占住引擎端口，模拟「上一次的引擎还活着」。
  // 它必须在另一个进程里——freePort 刻意跳过自己的 pid，所以同进程内
  // 的占位 socket 不会被清理，那测不到真实场景。这个占位进程不响应 API，
  // 因此 engineAlive 返回 false，必须走 freePort 把它杀掉才能 bind。
  const squatter = spawn(
    process.execPath,
    ["-e", `require('net').createServer().listen(${enginePort}, '127.0.0.1')`],
    { stdio: "ignore", windowsHide: true },
  );
  assert.ok(squatter.pid, "squatter process should have a pid");
  // 等占位进程真的开始监听
  await new Promise((resolve) => setTimeout(resolve, 300));

  const { port, dispose } = await bootPlugin(dir, enginePort);
  t.after(async () => {
    await dispose();
    try {
      squatter.kill("SIGKILL");
    } catch {
      /* already gone */
    }
    await rm(dir, { recursive: true, force: true });
  });

  // 关键点：旧进程占着端口时，面板代理依然能把真正的引擎拉起来。
  const body = await waitForEngine(port);
  assert.equal(body.default_executor, "dsh");
});
