import { NextRequest, NextResponse } from 'next/server'
import { spawn, type ChildProcess } from 'child_process'
import { createConnection } from 'net'
import { readFileSync, existsSync, openSync } from 'fs'
import { resolve } from 'path'
export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'
const BOT_PORT = 8080
const BOT_DIR = resolve(process.cwd(), 'mini-services/polymarket-bot')
const ENV_FILE = resolve(BOT_DIR, '.env')
const LOG_FILE = resolve(BOT_DIR, 'server.log')

function isPortListening(port: number, host = '127.0.0.1'): Promise<boolean> {
  return new Promise((r) => {
    const s = createConnection({ port, host })
    s.setTimeout(1500)
    s.once('connect', () => { s.destroy(); r(true) })
    s.once('error', () => { s.destroy(); r(false) })
    s.once('timeout', () => { s.destroy(); r(false) })
  })
}

function parseEnv(path: string): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { ...process.env }
  if (!existsSync(path)) return env
  for (const line of readFileSync(path, 'utf8').split('\n')) {
    const l = line.trim()
    if (!l || l.startsWith('#')) continue
    const eq = l.indexOf('=')
    if (eq === -1) continue
    env[l.slice(0, eq).trim()] = l.slice(eq + 1).trim()
  }
  return env
}

interface SpawnResult { ok: boolean; error?: string }

async function spawnBackend(): Promise<SpawnResult> {
  if (await isPortListening(BOT_PORT)) return { ok: true }
  try {
    const env = parseEnv(ENV_FILE)
    const out = openSync(LOG_FILE, 'w')
    const child: ChildProcess = spawn(
      'bash',
      ['-c', 'set -a && . ./.env && set +a && exec python3 -m uvicorn api.server:app --host 0.0.0.0 --port 8080 --log-level info'],
      { cwd: BOT_DIR, env, detached: true, stdio: ['ignore', out, out] },
    )
    child.unref()
    return { ok: true }
  } catch (e: unknown) {
    const message = e instanceof Error ? e.message : String(e)
    return { ok: false, error: message }
  }
}

export async function GET(req: NextRequest): Promise<NextResponse> {
  const action = req.nextUrl.searchParams.get('action') ?? 'status'
  let listening = await isPortListening(BOT_PORT)
  if (action === 'start' && !listening) {
    const res = await spawnBackend()
    if (!res.ok) return NextResponse.json({ ok: false, error: res.error }, { status: 500 })
    for (let i = 0; i < 25; i++) {
      await new Promise<void>((r) => setTimeout(r, 1000))
      if (await isPortListening(BOT_PORT)) break
    }
    listening = await isPortListening(BOT_PORT)
  }
  let health: unknown = null
  if (listening) {
    try {
      const r = await fetch(`http://127.0.0.1:${BOT_PORT}/api/health`, { signal: AbortSignal.timeout(4000) })
      if (r.ok) health = await r.json()
    } catch {
      // bot not yet responding — leave health null
    }
  }
  return NextResponse.json({ ok: !!health, listening, health, bot_dir: BOT_DIR, port: BOT_PORT })
}
