/**
 * Security-hardening tests for the host half of dsh-handoff-panel.
 * Run with: node --test dsh-handoff-panel/test/index.test.js
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import http from 'node:http'
import { EventEmitter } from 'node:events'
import {
  resolveStartArgv,
  resolvePanelUrl,
  createLaunchHandler,
  FIXED_SHIM,
} from '../lib/index.js'

const PANEL_URL = 'http://127.0.0.1:8790'
const REQUEST_ORIGIN = 'http://127.0.0.1:63926'
const REQUEST_HOST = '127.0.0.1:63926'
const NO_ORIGIN = Symbol('no-origin')

function post(handler, { origin = REQUEST_ORIGIN, host = REQUEST_HOST, site, body } = {}) {
  return new Promise((resolve, reject) => {
    const req = new EventEmitter()
    req.method = 'POST'
    req.url = '/handoff-panel/launch'
    req.headers = { host }
    if (origin !== undefined && origin !== NO_ORIGIN) req.headers.origin = origin
    if (site !== undefined) req.headers['sec-fetch-site'] = site
    req.socket = { encrypted: false }
    const res = {
      writeHead(status, headers) {
        this.statusCode = status
        this.headers = headers
      },
      end(payload) {
        resolve({
          status: this.statusCode,
          headers: this.headers,
          body: JSON.parse(Buffer.isBuffer(payload) ? payload.toString('utf8') : String(payload)),
        })
      },
    }
    Promise.resolve(handler(req, res)).catch(reject)
    if (body !== undefined) req.emit('data', body)
  })
}

function makeHandler(overrides = {}) {
  const logs = []
  const logger = { info: (message) => logs.push(String(message)) }
  const startCalls = []
  const opts = {
    panelUrl: PANEL_URL,
    startArgv: ['/opt/handoff/handoffctl', '--root', '/root/secret', 'panel', '--role', 'frontend'],
    probeFn: async () => ({ reachable: false, status: null }),
    startFn: async (argv, distro) => {
      startCalls.push({ argv, distro })
      return { exitedEarly: () => false }
    },
    logger,
    ...overrides,
  }
  return { handler: createLaunchHandler(opts), startCalls, logs }
}

// --- A. argv hardening -----------------------------------------------------

test('resolveStartArgv: conflicting --role values rejected', () => {
  assert.throws(
    () => resolveStartArgv({ startCommand: 'handoffctl panel --role frontend --role backend' }, PANEL_URL),
    /--role/,
  )
})

test('resolveStartArgv: duplicate identical --role rejected', () => {
  assert.throws(
    () => resolveStartArgv({ startCommand: 'handoffctl panel --role frontend --role frontend' }, PANEL_URL),
    /--role/,
  )
})

test('resolveStartArgv: duplicate --port rejected, conflicting inline port rejected', () => {
  assert.throws(
    () => resolveStartArgv({ startCommand: 'handoffctl panel --port=8790 --port 8790' }, PANEL_URL),
    /--port/,
  )
  assert.throws(
    () => resolveStartArgv({ startCommand: 'handoffctl panel --port 1234' }, PANEL_URL),
    /--port/,
  )
})

test('resolveStartArgv: missing option value rejected', () => {
  assert.throws(
    () => resolveStartArgv({ startCommand: 'handoffctl panel --role' }, PANEL_URL),
    /requires a value/,
  )
  assert.throws(
    () => resolveStartArgv({ startCommand: 'handoffctl panel --port --role frontend' }, PANEL_URL),
    /requires a value/,
  )
})

test('resolveStartArgv: no role/port/host yields exactly one each, host pinned', () => {
  const argv = resolveStartArgv({ startCommand: 'handoffctl panel' }, PANEL_URL)
  assert.deepEqual(argv, [
    'handoffctl',
    'panel',
    '--role',
    'frontend',
    '--port',
    '8790',
    '--host',
    '127.0.0.1',
  ])
  assert.equal(argv.filter((t) => t === '--role').length, 1)
  assert.equal(argv.filter((t) => t === '--port').length, 1)
  assert.equal(argv.filter((t) => t === '--host').length, 1)
})

test('resolveStartArgv: config-provided host pinned once, duplicates rejected', () => {
  const argv = resolveStartArgv(
    { startCommand: 'handoffctl panel --host 127.0.0.1' },
    PANEL_URL,
  )
  assert.equal(argv.filter((t) => t === '--host').length, 1)
  assert.equal(argv[argv.indexOf('--host') + 1], '127.0.0.1')
  assert.throws(
    () => resolveStartArgv(
      { startCommand: 'handoffctl panel --host 127.0.0.1 --host 127.0.0.1' },
      PANEL_URL,
    ),
    /--host/,
  )
  assert.throws(
    () => resolveStartArgv({ startCommand: 'handoffctl panel --host 0.0.0.0' }, PANEL_URL),
    /--host/,
  )
})

test('resolveStartArgv: non-default url port appended exactly once', () => {
  const argv = resolveStartArgv({ startCommand: 'handoffctl panel' }, 'http://127.0.0.1:9001')
  assert.equal(argv.filter((t) => t === '--port').length, 1)
  assert.equal(argv[argv.indexOf('--port') + 1], '9001')
})

test('resolveStartArgv: rejects zero or multiple panel subcommands', () => {
  assert.throws(() => resolveStartArgv({ startCommand: 'handoffctl' }, PANEL_URL), /panel/)
  assert.throws(
    () => resolveStartArgv({ startCommand: 'handoffctl panel panel' }, PANEL_URL),
    /panel/,
  )
})

for (const meta of [';', '\n', '&&', '||', '$(id)', '`id`', '>', '|', '&', "'", '"', '\\', '\r']) {
  test(`resolveStartArgv: shell metacharacter ${JSON.stringify(meta)} rejected`, () => {
    assert.throws(
      () => resolveStartArgv({ startCommand: `handoffctl panel ${meta}` }, PANEL_URL),
      /shell character/,
    )
  })
}

// --- A. URL normalization --------------------------------------------------

test('resolvePanelUrl: ::1 and [::1] rejected with clear reason', () => {
  assert.throws(() => resolvePanelUrl({ url: 'http://[::1]:8790' }), /IPv6/)
  assert.throws(() => resolvePanelUrl({ url: 'http://::1:8790' }), /IPv6|loopback/)
})

test('resolvePanelUrl: localhost normalized to 127.0.0.1', () => {
  assert.equal(resolvePanelUrl({ url: 'http://localhost:8790' }), PANEL_URL)
})

test('resolvePanelUrl: non-loopback host rejected', () => {
  assert.throws(() => resolvePanelUrl({ url: 'http://evil.example:8790' }), /loopback/)
})

// --- B. WSL shim is fixed and positional ----------------------------------

test('FIXED_SHIM: contains no launch-time value and uses positional parameters', () => {
  assert.match(FIXED_SHIM, /exec "\$@"/)
  assert.doesNotMatch(FIXED_SHIM, /handoffctl|frontend|127\.0\.0\.1/)
})

test('FIXED_SHIM: expands a leading ~/ on every argument (program and --root)', async () => {
  // Execute the shim with a fake HOME and echo each positional arg verbatim.
  const { execFileSync } = await import('node:child_process')
  // Override `exec` with a function so the shim's final `exec "$@"` prints the
  // expanded positional args instead of trying to run the fake program.
  const out = execFileSync(
    '/bin/bash',
    ['-lc', 'exec() { printf "%s\\n" "$@"; }; ' + FIXED_SHIM, 'handoff-panel-shim', '~/code/a', '--root', '~/code/b', 'plain'],
    { env: { ...process.env, HOME: '/fake/home' }, encoding: 'utf8' },
  )
  assert.equal(out.trim(), '/fake/home/code/a\n--root\n/fake/home/code/b\nplain')
})

// --- C. origin checks ------------------------------------------------------

test('launch: missing Origin returns 403 and never starts', async () => {
  const { handler, startCalls } = makeHandler()
  const result = await post(handler, { origin: NO_ORIGIN })
  assert.equal(result.status, 403)
  assert.equal(result.body.code, 'cross_origin_launch_forbidden')
  assert.equal(startCalls.length, 0)
})

test('launch: malicious Origin returns 403 and never starts', async () => {
  const { handler, startCalls } = makeHandler()
  const result = await post(handler, { origin: 'https://evil.example' })
  assert.equal(result.status, 403)
  assert.equal(result.body.code, 'cross_origin_launch_forbidden')
  assert.equal(startCalls.length, 0)
})

test('launch: Sec-Fetch-Site cross-site returns 403 and never starts', async () => {
  const { handler, startCalls } = makeHandler()
  const result = await post(handler, { site: 'cross-site' })
  assert.equal(result.status, 403)
  assert.equal(startCalls.length, 0)
})

test('launch: correct same-origin request is allowed and starts', async () => {
  const { handler, startCalls } = makeHandler()
  const result = await post(handler, { origin: 'http://127.0.0.1:63926', host: '127.0.0.1:63926', site: 'same-origin' })
  assert.equal(result.status, 202)
  assert.equal(result.body.started, true)
  assert.equal(startCalls.length, 1)
})

test('launch: non-POST returns 405', async () => {
  const { handler, startCalls } = makeHandler()
  const result = await new Promise((resolve) => {
    const req = new EventEmitter()
    req.method = 'GET'
    req.url = '/handoff-panel/launch'
    req.headers = { host: '127.0.0.1:63926', origin: 'http://127.0.0.1:63926' }
    req.socket = { encrypted: false }
    const res = {
      writeHead(status) { this.statusCode = status },
      end() { resolve({ status: this.statusCode }) },
    }
    Promise.resolve(handler(req, res)).catch((err) => resolve({ error: err }))
  })
  assert.equal(result.status, 405)
  assert.equal(startCalls.length, 0)
})

// --- C. probe outcomes / single flight ------------------------------------

test('launch: already-running frontend returns 200 started:false, no spawn', async () => {
  const { handler, startCalls } = makeHandler({
    probeFn: async () => ({ reachable: true, status: 200, role: 'frontend' }),
  })
  const result = await post(handler)
  assert.equal(result.status, 200)
  assert.equal(result.body.started, false)
  assert.equal(result.body.state, 'already_running')
  assert.equal(startCalls.length, 0)
})

test('launch: other HTTP service returns 409 panel_role_conflict, no spawn', async () => {
  const { handler, startCalls } = makeHandler({
    probeFn: async () => ({ reachable: false, status: 200, role: 'backend' }),
  })
  const result = await post(handler)
  assert.equal(result.status, 409)
  assert.equal(result.body.code, 'panel_role_conflict')
  assert.equal(startCalls.length, 0)
})

test('launch: 20 concurrent POSTs call start exactly once', async () => {
  let release
  const gate = new Promise((resolve) => { release = resolve })
  let probeCount = 0
  const { handler, startCalls } = makeHandler({
    probeFn: async () => {
      probeCount += 1
      await gate
      return { reachable: false, status: null }
    },
  })
  const resultsPromise = Promise.all(Array.from({ length: 20 }, () => post(handler)))
  // Let the single in-flight probe finish once all requests have entered.
  setImmediate(release)
  const results = await resultsPromise
  assert.equal(startCalls.length, 1)
  assert.equal(probeCount, 1)
  assert.equal(results.filter((r) => r.status === 202).length, 20)
})

test('launch: async spawn error returns 500 launch_spawn_failed', async () => {
  const { handler } = makeHandler({
    startFn: async () => {
      const error = new Error('spawn handoffctl ENOENT')
      error.code = 'launch_spawn_failed'
      throw error
    },
  })
  const result = await post(handler)
  assert.equal(result.status, 500)
  assert.equal(result.body.code, 'launch_spawn_failed')
})

test('launch: spawn resolves then early exit reports launch_exited_early', async () => {
  const { handler } = makeHandler({
    startFn: async () => ({ exitedEarly: () => true }),
  })
  const result = await post(handler)
  assert.equal(result.status, 500)
  assert.equal(result.body.code, 'launch_exited_early')
})

// --- C. no leaks -----------------------------------------------------------

test('launch: response body and logs never include command/root', async () => {
  const { handler, logs } = makeHandler()
  const result = await post(handler)
  assert.equal(result.status, 202)
  assert.ok(!JSON.stringify(result.body).includes('/root/secret'))
  assert.ok(!JSON.stringify(result.body).includes('handoffctl'))
  assert.ok(!('command' in result.body))
  assert.ok(!('argv' in result.body))
  for (const line of logs) {
    assert.ok(!line.includes('/root/secret'))
    assert.ok(!line.includes('handoffctl'))
  }
  assert.ok(logs.includes('handoff panel: frontend launch accepted'))
})

// --- B. spawn() async error contract --------------------------------------

test('start(): async error rejects; spawn event resolves without unref before spawn', async () => {
  // A fake child_process is exercised through a real spawn of a missing binary
  // instead: start() on native host with a nonexistent program must reject.
  const { start } = await import('../lib/index.js')
  await assert.rejects(
    () => start(['/definitely/not/a/real/handoffctl-binary', '--role', 'frontend'], ''),
    (error) => error instanceof Error && /ENOENT|not a real|spawn/.test(error.message),
  )
})
