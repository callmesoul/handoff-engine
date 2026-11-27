/**
 * Host half of the Handoff Panel plugin.
 *
 * It owns one HTTP prefix route (`/handoff-panel`) that the browser half uses
 * to find out whether the Python task panel (`handoffctl panel`, loopback only)
 * is reachable, and to start it on demand:
 *
 *   GET  /handoff-panel/health   -> { ok: true }
 *   GET  /handoff-panel/status   -> { ok, url, reachable, status, role, error }
 *   POST /handoff-panel/launch   -> { ok, started, state, code? }
 *
 * Launch requests are same-origin-only, and the panel process is started from
 * an argv array (never a shell command string). Responses and logs deliberately
 * carry no command line, so paths/root/arguments cannot leak to the browser.
 *
 * The panel itself is NOT proxied: the browser half frames the panel's own
 * origin, so the panel's absolute `/api/...` fetches stay same-origin with it.
 */
import { spawn } from 'node:child_process'
import { homedir } from 'node:os'
import { join as joinPath } from 'node:path'

const ROUTE = '/handoff-panel'
const DEFAULT_PANEL_PORT = 8790
const DEFAULT_PANEL_URL = `http://127.0.0.1:${DEFAULT_PANEL_PORT}`
const DEFAULT_START_COMMAND =
  '~/code/handoff-engine/.venv/bin/handoffctl --root ~/code/handoff-store panel --role frontend --no-open'
const DEFAULT_WSL_DISTRO = process.platform === 'win32' ? 'Ubuntu' : ''
const PROBE_TIMEOUT_MS = 1500
const LAUNCH_PENDING_MS = 12000

/**
 * Fixed WSL shim. Program and args arrive as positional parameters ($1...),
 * never as part of this source string. The only transformation is expanding a
 * leading `~/` in every argument (e.g. both the program and `--root ~/store`);
 * everything else is passed through verbatim.
 */
const FIXED_SHIM =
  'args=(); for a in "$@"; do case "$a" in "~/"*) args+=("$HOME/${a:2}") ;; *) args+=("$a") ;; esac; done; set -- "${args[@]}"; exec "$@"'

const LAUNCH_CODES = new Set([
  'cross_origin_launch_forbidden',
  'panel_role_forbidden',
  'panel_role_conflict',
  'launch_spawn_failed',
  'launch_exited_early',
])

const name = 'handoff-panel'

/** Services this fiber needs before `apply` runs. */
const inject = ['webServer']

class ConfigError extends Error {
  constructor(message) {
    super(message)
    this.name = 'ConfigError'
  }
}

class LaunchError extends Error {
  constructor(code, message) {
    super(message)
    this.name = 'LaunchError'
    this.code = code
  }
}

/** Read a string config field, falling back when it is absent or blank. */
function text(value, fallback) {
  return typeof value === 'string' && value.trim() ? value.trim() : fallback
}

/**
 * Validate one raw config object. Mirrors the bridge plugin's hand-written
 * Standard Schema so a bad profile patch fails loudly at load.
 */
function validateConfig(raw) {
  if (raw === undefined || raw === null) return { value: {} }
  if (typeof raw !== 'object' || Array.isArray(raw)) {
    return { issues: [{ message: 'config must be an object', path: [] }] }
  }
  const issues = []
  for (const key of ['url', 'startCommand', 'wslDistro']) {
    if (raw[key] !== undefined && typeof raw[key] !== 'string') {
      issues.push({ message: `${key} must be a string`, path: [key] })
    }
  }
  if (raw.port !== undefined && (!Number.isInteger(raw.port) || raw.port < 1 || raw.port > 65535)) {
    issues.push({ message: 'port must be an integer in 1..65535', path: ['port'] })
  }
  if (issues.length > 0) return { issues }
  return { value: raw }
}

const Config = {
  '~standard': { version: 1, vendor: name, validate: validateConfig },
}

function isLoopbackHostname(hostname) {
  const value = String(hostname ?? '').toLowerCase()
  return value === '127.0.0.1' || value === 'localhost'
}

/**
 * `http://127.0.0.1:8790` with no trailing slash and a loopback-only,
 * IPv4-only host. `localhost` is normalized; IPv6 loopback is unsupported by
 * the launcher because the spawned panel must be pinned to `--host 127.0.0.1`.
 */
function resolvePanelUrl(config) {
  const raw = text(config.url, '')
  const fallback = config.port === undefined
    ? DEFAULT_PANEL_URL
    : `http://127.0.0.1:${config.port}`
  if (raw.toLowerCase().includes('::1')) {
    throw new ConfigError(
      'handoff panel: IPv6 loopback (::1) is unsupported by the launcher; use 127.0.0.1',
    )
  }
  let url
  try {
    url = new URL(raw || fallback)
  } catch {
    throw new ConfigError('handoff panel: invalid panel url')
  }
  if (url.protocol !== 'http:') {
    throw new ConfigError(`handoff panel: url must use http, got ${url.protocol}`)
  }
  const hostname = url.hostname.toLowerCase()
  if (hostname === '::1' || hostname === '[::1]' || hostname.includes(':')) {
    throw new ConfigError(
      'handoff panel: IPv6 loopback (::1) is unsupported by the launcher; use 127.0.0.1',
    )
  }
  if (!isLoopbackHostname(hostname)) {
    throw new ConfigError(`handoff panel: url must be loopback, got ${hostname}`)
  }
  if (config.port !== undefined && Number(url.port || DEFAULT_PANEL_PORT) !== config.port) {
    throw new ConfigError('handoff panel: url port and config.port must match')
  }
  const normalized = hostname === 'localhost' ? '127.0.0.1' : hostname
  const port = url.port ? `:${url.port}` : ''
  return `http://${normalized}${port}`
}

/**
 * Build the launch argv from a *command string* supplied by config, with no
 * shell involvement. Every occurrence of --role/--port/--host is inspected:
 * duplicates are refused and single occurrences must match the enforced value.
 */
function resolveStartArgv(config, panelUrl) {
  const raw = text(config.startCommand, DEFAULT_START_COMMAND)
  // Validate the untrimmed source: text() strips trailing whitespace, so a
  // trailing \r/\n would otherwise slip past the control-character check.
  const source = typeof config.startCommand === 'string' ? config.startCommand : raw
  // Reject every ASCII control character (including \r/\n) explicitly; the
  // printable-char regex below handles the shell syntax characters.
  const badControl = [...source].find((char) => char.codePointAt(0) < 0x20 || char === '\x7f')
  if (badControl !== undefined) {
    throw new ConfigError(
      `handoff panel: startCommand contains unsupported shell character ${JSON.stringify(badControl)}`,
    )
  }
  const printableMeta = /[;&|<>$`()'"\\]/
  const badMeta = [...source].find((char) => printableMeta.test(char))
  if (badMeta !== undefined) {
    throw new ConfigError(
      `handoff panel: startCommand contains unsupported shell character ${JSON.stringify(badMeta)}`,
    )
  }
  const tokens = raw.split(/\s+/).filter(Boolean)
  const program = tokens[0]
  if (!program) throw new ConfigError('handoff panel: startCommand is empty')

  const expectedPort = Number(new URL(panelUrl).port || DEFAULT_PANEL_PORT)
  if (!Number.isInteger(expectedPort) || expectedPort < 1 || expectedPort > 65535) {
    throw new ConfigError('handoff panel: panel url has an invalid port')
  }
  const enforced = { role: 'frontend', port: String(expectedPort), host: '127.0.0.1' }
  const seen = { role: 0, port: 0, host: 0 }
  const argv = [program]
  let panelCount = 0

  for (let i = 1; i < tokens.length; i++) {
    const token = tokens[i]
    let key = null
    let value = null
    let inline = false
    for (const option of ['role', 'port', 'host']) {
      if (token === `--${option}`) {
        key = option
        break
      }
      if (token.startsWith(`--${option}=`)) {
        key = option
        value = token.slice(option.length + 3)
        inline = true
        break
      }
    }

    if (key) {
      seen[key] += 1
      if (seen[key] > 1) {
        throw new ConfigError(`handoff panel: --${key} must not be specified more than once`)
      }
      if (!inline) {
        value = tokens[i + 1]
        if (value === undefined || value.startsWith('-')) {
          throw new ConfigError(`handoff panel: --${key} requires a value`)
        }
        i += 1
      }
      if (!value) {
        throw new ConfigError(`handoff panel: --${key} requires a value`)
      }
      if (value !== enforced[key]) {
        throw new ConfigError(`handoff panel: --${key} must be ${enforced[key]}`)
      }
      // Normalize --key=value to --key value so final argv is predictable.
      argv.push(`--${key}`, value)
      continue
    }

    if (token === 'panel') panelCount += 1
    argv.push(token)
  }

  if (panelCount !== 1) {
    throw new ConfigError('handoff panel: startCommand must contain exactly one panel subcommand')
  }
  for (const option of ['role', 'port', 'host']) {
    if (seen[option] > 1) {
      throw new ConfigError(`handoff panel: exactly one --${option} is required`)
    }
  }
  if (seen.role === 0) argv.push('--role', enforced.role)
  if (seen.port === 0) argv.push('--port', enforced.port)
  // Pin exactly one host: a config-provided --host was validated above, so
  // only append when none was given.
  if (seen.host === 0) argv.push('--host', enforced.host)
  return argv
}

/** Thin compatibility wrapper; all in-repo callers use resolveStartArgv. */
function resolveStartCommand(config, panelUrl) {
  return resolveStartArgv(config, panelUrl).join(' ')
}

/** Probe the panel's `/api/state`; resolves to a status object, never throws. */
async function probe(panelUrl) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), PROBE_TIMEOUT_MS)
  try {
    const response = await fetch(`${panelUrl}/api/state`, {
      signal: controller.signal,
      headers: { accept: 'application/json' },
    })
    if (!response.ok) {
      return { reachable: false, status: response.status, error: `HTTP ${response.status}` }
    }
    const state = await response.json()
    const role = typeof state?.role === 'string' ? state.role : null
    if (role !== 'frontend') {
      return {
        reachable: false,
        status: response.status,
        role,
        error: role
          ? `expected frontend panel, got ${role}`
          : 'panel does not report a frontend role',
      }
    }
    return {
      reachable: true,
      status: response.status,
      role,
    }
  } catch (error) {
    const aborted = controller.signal.aborted
    return {
      reachable: false,
      status: null,
      error: aborted ? 'probe timed out' : (error?.message ?? String(error)),
    }
  } finally {
    clearTimeout(timer)
  }
}

/**
 * Start the panel detached so it outlives this request (and this plugin).
 *
 * Native hosts spawn the program directly. WSL hosts use a fixed bash shim that
 * receives the program and args as positional parameters; no user-controlled
 * text is ever interpolated into a shell command line.
 */
function start(argv, wslDistro) {
  if (!Array.isArray(argv) || typeof argv[0] !== 'string' || !argv[0]) {
    return Promise.reject(new Error('handoff panel: argv must start with a program'))
  }
  const [rawProgram, ...rawArgs] = argv
  const distro = typeof wslDistro === 'string' ? wslDistro.trim() : DEFAULT_WSL_DISTRO
  // Native spawns never see a shell, so expand a leading ~/ on every argument
  // in JS. WSL spawns keep the raw values and let the fixed shim expand them
  // with the WSL $HOME (Windows homedir() would be wrong there).
  const expandTilde = (value) =>
    typeof value === 'string' && value.startsWith('~/')
      ? joinPath(homedir(), value.slice(2))
      : value
  const program = distro ? rawProgram : expandTilde(rawProgram)
  const args = distro ? rawArgs : rawArgs.map(expandTilde)

  // With `bash -lc <script> <args...>`, the first extra argument becomes bash's
  // $0 and is NOT part of "$@". Supply a dummy $0 so program/args stay intact.
  const child = distro
    ? spawn('wsl.exe', ['-d', distro, '--exec', '/bin/bash', '-lc', FIXED_SHIM, 'handoff-panel-shim', rawProgram, ...rawArgs], {
        detached: true,
        stdio: 'ignore',
        windowsHide: true,
      })
    : spawn(program, args, { detached: true, stdio: 'ignore' })

  let resolveSpawn
  let rejectSpawn
  const promise = new Promise((resolve, reject) => {
    resolveSpawn = resolve
    rejectSpawn = reject
  })
  let settled = false
  let exitedEarly = false

  child.once('spawn', () => {
    if (settled) return
    settled = true
    // Only unref once the spawn is confirmed; an async ENOENT must reject.
    child.unref?.()
    resolveSpawn({ exitedEarly: () => exitedEarly })
  })
  child.once('error', (error) => {
    if (settled) return
    settled = true
    rejectSpawn(new LaunchError('launch_spawn_failed', error?.message ?? String(error)))
  })
  child.once('exit', () => {
    exitedEarly = true
    if (!settled) {
      settled = true
      rejectSpawn(new LaunchError('launch_exited_early', 'panel process exited before readiness'))
    }
  })
  return promise
}

function sendJson(res, status, body) {
  const payload = JSON.stringify(body)
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(payload),
    'cache-control': 'no-store',
  })
  res.end(payload)
}

function roleFromArgv(argv) {
  if (!Array.isArray(argv)) return null
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--role') return argv[i + 1] ?? null
    if (typeof argv[i] === 'string' && argv[i].startsWith('--role=')) {
      return argv[i].slice('--role='.length)
    }
  }
  return null
}

/**
 * Same-origin check for the loopback POST. Host must name a loopback address
 * with an explicit port, Origin must match the incoming scheme/host/port
 * exactly, and Sec-Fetch-Site (when present) must be same-origin.
 */
function isSameOriginLaunch(req) {
  const origin = req.headers?.origin
  const host = req.headers?.host
  if (typeof origin !== 'string' || !origin || typeof host !== 'string' || !host) return false

  let incoming
  try {
    incoming = new URL(`${req.socket?.encrypted ? 'https' : 'http'}://${host}`)
  } catch {
    return false
  }
  if (!isLoopbackHostname(incoming.hostname) || !incoming.port) return false

  const expected = `${req.socket?.encrypted ? 'https' : 'http'}://${incoming.hostname}:${incoming.port}`
  if (origin !== expected) return false
  let parsed
  try {
    parsed = new URL(origin)
  } catch {
    return false
  }
  if (parsed.protocol !== incoming.protocol) return false
  if (!isLoopbackHostname(parsed.hostname) || parsed.port !== incoming.port) return false

  const site = req.headers?.['sec-fetch-site']
  if (site !== undefined && site !== 'same-origin') return false
  return true
}

/**
 * Build the hardened /launch request handler with injectable probe/start fns.
 * One probe→spawn promise is shared by concurrent POSTs; a successful spawn
 * starts a 12s pending window during which further POSTs report "starting".
 */
function createLaunchHandler(options = {}) {
  const {
    config = {},
    panelUrl = resolvePanelUrl(config),
    startArgv = resolveStartArgv(config, panelUrl),
    probeFn = probe,
    startFn = start,
    logger = null,
  } = options

  let launchInFlight = null
  let cooldownUntil = 0

  const log = (message) => {
    if (typeof logger?.info === 'function') logger.info(message)
  }

  const coordinate = () => {
    if (launchInFlight) return launchInFlight
    if (Date.now() < cooldownUntil) {
      return Promise.resolve({ status: 202, body: { ok: true, started: false, state: 'starting' } })
    }
    launchInFlight = (async () => {
      try {
        const status = await probeFn(panelUrl)
        if (status?.reachable && status.role === 'frontend') {
          return { status: 200, body: { ok: true, started: false, state: 'already_running' } }
        }
        // Any HTTP answer that is not a frontend panel wins over a spawn.
        if (status?.status !== null && status?.status !== undefined && status.role !== 'frontend') {
          return {
            status: 409,
            body: {
              ok: false,
              started: false,
              state: 'conflict',
              code: 'panel_role_conflict',
            },
          }
        }
        const spawned = await startFn(startArgv, config.wslDistro)
        cooldownUntil = Date.now() + LAUNCH_PENDING_MS
        if (spawned?.exitedEarly?.()) {
          return {
            status: 500,
            body: { ok: false, started: false, code: 'launch_exited_early' },
          }
        }
        log('handoff panel: frontend launch accepted')
        return { status: 202, body: { ok: true, started: true, state: 'starting' } }
      } catch (error) {
        const code = LAUNCH_CODES.has(error?.code) ? error.code : 'launch_spawn_failed'
        return { status: 500, body: { ok: false, started: false, code } }
      } finally {
        launchInFlight = null
      }
    })()
    return launchInFlight
  }

  return async (req, res) => {
    if (req.method !== 'POST') {
      res.writeHead(405, { 'content-type': 'text/plain; charset=utf-8', allow: 'POST' })
      res.end('method not allowed')
      return
    }
    if (!isSameOriginLaunch(req)) {
      sendJson(res, 403, { ok: false, code: 'cross_origin_launch_forbidden' })
      return
    }
    const role = roleFromArgv(startArgv)
    if (role && role !== 'frontend') {
      sendJson(res, 403, { ok: false, code: 'panel_role_forbidden' })
      return
    }
    const result = await coordinate()
    sendJson(res, result.status, result.body)
  }
}

/**
 * @param ctx - Cordis context of the plugin fiber.
 * @param config - optional plugin config from the profile patch.
 */
function apply(ctx, config = {}) {
  const panelUrl = resolvePanelUrl(config)
  const startArgv = resolveStartArgv(config, panelUrl)
  const launch = createLaunchHandler({ config, panelUrl, startArgv, logger: ctx.logger })

  const handle = async (req, res) => {
    const url = new URL(req.url ?? '/', 'http://handoff-panel.invalid')
    const route = url.pathname.slice(ROUTE.length).replace(/\/+$/, '') || '/'

    if (route === '/health') {
      if (req.method !== 'GET' && req.method !== 'HEAD') {
        res.writeHead(405, { 'content-type': 'text/plain; charset=utf-8', allow: 'GET, HEAD' })
        res.end('method not allowed')
        return
      }
      sendJson(res, 200, { ok: true })
      return
    }

    if (route === '/status') {
      if (req.method !== 'GET' && req.method !== 'HEAD') {
        res.writeHead(405, { 'content-type': 'text/plain; charset=utf-8', allow: 'GET, HEAD' })
        res.end('method not allowed')
        return
      }
      const status = await probe(panelUrl)
      sendJson(res, 200, { ok: true, url: panelUrl, ...status })
      return
    }

    if (route === '/launch') {
      await launch(req, res)
      return
    }

    sendJson(res, 404, { ok: false, error: `unknown route ${route}` })
  }

  ctx.effect(
    () => ctx.webServer.register({ kind: 'prefix', path: ROUTE, handler: handle }),
    'handoff-panel: route',
  )

  ctx.logger?.info?.('handoff panel: frontend route registered')
}

export {
  Config,
  apply,
  inject,
  name,
  resolvePanelUrl,
  resolveStartArgv,
  resolveStartCommand,
  createLaunchHandler,
  probe,
  start,
  ConfigError,
  LaunchError,
  FIXED_SHIM,
}
export default { name, apply, Config, inject }
