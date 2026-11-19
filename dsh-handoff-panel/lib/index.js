/**
 * Host half of the Handoff Panel plugin.
 *
 * It owns one HTTP prefix route (`/handoff-panel`) that the browser half uses
 * to find out whether the Python task panel (`handoffctl panel`, loopback only)
 * is reachable, and to start it on demand:
 *
 *   GET  /handoff-panel/health   -> { ok: true }
 *   GET  /handoff-panel/status   -> { ok, url, reachable, status, mode, error }
 *   POST /handoff-panel/launch   -> { ok, started, command }
 *
 * The panel itself is NOT proxied: the browser half frames the panel's own
 * origin, so the panel's absolute `/api/...` fetches stay same-origin with it.
 */
import { spawn } from 'node:child_process'

const ROUTE = '/handoff-panel'
const DEFAULT_PANEL_URL = 'http://127.0.0.1:8790'
const DEFAULT_START_COMMAND =
  '~/code/handoff-engine/.venv/bin/handoffctl --root ~/code/handoff-store panel --no-open'
const DEFAULT_WSL_DISTRO = process.platform === 'win32' ? 'Ubuntu' : ''
const PROBE_TIMEOUT_MS = 1500

const name = 'handoff-panel'

/** Services this fiber needs before `apply` runs. */
const inject = ['webServer']

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

/** `http://127.0.0.1:8790` with no trailing slash and a loopback host only. */
function resolvePanelUrl(config) {
  const raw = text(config.url, '')
  const url = new URL(raw || `${DEFAULT_PANEL_URL}`)
  if (url.hostname !== '127.0.0.1' && url.hostname !== 'localhost' && url.hostname !== '::1') {
    throw new Error(`handoff panel: url must be loopback, got ${url.hostname}`)
  }
  return `${url.protocol}//${url.host}`
}

/** The start command, with `port` pinned when the config names one. */
function resolveStartCommand(config, panelUrl) {
  const base = text(config.startCommand, DEFAULT_START_COMMAND)
  if (config.port === undefined) return base
  return `${base} --port ${config.port}`
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
    return {
      reachable: true,
      status: response.status,
      mode: typeof state?.mode === 'string' ? state.mode : null,
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
 * On Windows the panel lives inside WSL, so the command must be handed to
 * `wsl.exe`; running `/bin/bash` directly there always fails with ENOENT.
 * `DEFAULT_WSL_DISTRO` picks the usual distro; set `wslDistro` to override it,
 * or to `""` to run the command natively (Linux/macOS hosts).
 */
function start(command, wslDistro) {
  const distro = text(wslDistro, DEFAULT_WSL_DISTRO)
  const child = distro
    ? spawn('wsl.exe', ['-d', distro, '--exec', '/bin/bash', '-lc', command], {
        detached: true,
        stdio: 'ignore',
        windowsHide: true,
      })
    : spawn('/bin/bash', ['-lc', command], { detached: true, stdio: 'ignore' })
  child.on('error', () => {})
  child.unref()
  return child
}

/**
 * @param ctx - Cordis context of the plugin fiber.
 * @param config - optional plugin config from the profile patch.
 */
function apply(ctx, config = {}) {
  const panelUrl = resolvePanelUrl(config)
  const startCommand = resolveStartCommand(config, panelUrl)
  const dshLogger = ctx.logger

  const send = (res, status, body) => {
    const payload = JSON.stringify(body)
    res.writeHead(status, {
      'content-type': 'application/json; charset=utf-8',
      'content-length': Buffer.byteLength(payload),
      'cache-control': 'no-store',
    })
    res.end(payload)
  }

  const handle = async (req, res) => {
    const url = new URL(req.url ?? '/', 'http://handoff-panel.invalid')
    const route = url.pathname.slice(ROUTE.length).replace(/\/+$/, '') || '/'

    if (route === '/health') {
      if (req.method !== 'GET' && req.method !== 'HEAD') {
        res.writeHead(405, { 'content-type': 'text/plain; charset=utf-8', allow: 'GET, HEAD' })
        res.end('method not allowed')
        return
      }
      send(res, 200, { ok: true })
      return
    }

    if (route === '/status') {
      if (req.method !== 'GET' && req.method !== 'HEAD') {
        res.writeHead(405, { 'content-type': 'text/plain; charset=utf-8', allow: 'GET, HEAD' })
        res.end('method not allowed')
        return
      }
      const status = await probe(panelUrl)
      send(res, 200, { ok: true, url: panelUrl, ...status })
      return
    }

    if (route === '/launch') {
      if (req.method !== 'POST') {
        res.writeHead(405, { 'content-type': 'text/plain; charset=utf-8', allow: 'POST' })
        res.end('method not allowed')
        return
      }
      try {
        start(startCommand, config.wslDistro)
        dshLogger?.info?.(`handoff panel: started ${startCommand}`)
        send(res, 202, { ok: true, started: true, command: startCommand })
      } catch (error) {
        dshLogger?.warn?.(error)
        send(res, 500, { ok: false, started: false, error: error?.message ?? String(error) })
      }
      return
    }

    send(res, 404, { ok: false, error: `unknown route ${route}` })
  }

  ctx.effect(
    () => ctx.webServer.register({ kind: 'prefix', path: ROUTE, handler: handle }),
    'handoff-panel: route',
  )

  dshLogger?.info?.(`handoff panel: probing ${panelUrl}`)
}

export { Config, apply, inject, name, resolvePanelUrl }
export default { name, apply, Config, inject }
