import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Mood } from '../types'

// Ein kleines Claude-Tierchen, komplett ohne Tokens: es liest nur Events mit
// (was Claude tut, was du tippst) und spiegelt sie
//  1. im Spinner links neben "Thinking…" (Client-Animation in critter.tsx) und
//  2. über ~/.claude/critter/state.json an das Desktop-Overlay (overlay/critter.pyw).

const mood = atom({ plugin: 'critter', key: 'mood' } as const, {
  mode: 'idle',
  tool: '',
  detail: '',
  typing: 0,
  text: '',
  event: 'hello',
  eventAt: 0,
  agents: [],
})

let statePath = ''
let writeQueued = false

const short = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1)}…` : s)

const flush = async ($: EngineInterface) => {
  if (!statePath || writeQueued) return
  writeQueued = true
  $.clock.after(120, async () => {
    writeQueued = false
    const m = await read($, mood)
    await $.fs.write(statePath, JSON.stringify({ ...m, at: await $.clock.now() })).catch(() => {})
  })
}

const set = async ($: EngineInterface, patch: Partial<Mood>) => {
  await update($, mood, m => ({ ...m, ...patch }))
  await flush($)
}

const fire = async ($: EngineInterface, event: string, patch: Partial<Mood> = {}) =>
  set($, { ...patch, event, eventAt: await $.clock.now() })

// Was der User abgeschickt hat, grob eingeordnet (rein per Regex).
const classify = (text: string) => {
  const t = text.toLowerCase()
  if (/\b(danke|thx|thanks|merci|perfekt|super|geil|cool|nice)\b/.test(t)) return 'love'
  if (/\b(fuck|scheiße|scheisse|mist|verdammt|wtf|nervt)\b/.test(t)) return 'scared'
  if (/\b(hallo|hi|hey|moin|servus|jo)\b/.test(t) && t.length < 25) return 'wave'
  if (/(bug|fehler|error|kaputt|geht nicht|funktioniert nicht)/.test(t)) return 'determined'
  if (/(test|teste)/.test(t)) return 'curious'
  if (t.trim().endsWith('?')) return 'question'
  if (t.includes('!')) return 'excited'
  if (t.length > 400) return 'yawn'
  return 'submit'
}

const toolDetail = (e: { tool: string } & Record<string, unknown>) => {
  const s = (k: string) => (typeof e[k] === 'string' ? (e[k] as string) : '')
  return short(s('file_path').split(/[\\/]/).pop() || s('description') || s('command') || s('pattern') || s('query') || s('url'), 40)
}

const activity = (tool: string, cmd: string, url: string) => {
  if (/(\brm\s+-r|\bdel\s|Remove-Item|\brmdir\b|\bshred\b)/i.test(cmd)) return 'delete'
  if (/(nvidia-smi|\bcuda\b|\bgpu\b|nvenc|moderngl|torch|tensorflow|\bopengl\b)/i.test(cmd)) return 'gpu'
  if (/(Get-Process|Get-Counter|tasklist|\bwmic\b|\btop\b|htop|psutil|systeminfo|Win32_Processor|\bnproc\b|Get-CimInstance)/i.test(cmd)) return 'cpu'
  if (/(\bcurl\b|\bwget\b|Invoke-WebRequest|\biwr\b|git\s+(clone|pull|fetch)|\b(npm|pnpm|yarn)\s+(i|install|add)\b|pip3?\s+install|yt-dlp|download)/i.test(cmd) || tool === 'WebFetch' || url) return 'cloud'
  if (/(\bpython3?\b|\bnode\b|\bnpm\s+run\b|\bnpx\b|cargo\s+run|go\s+run|\bjava\b|dotnet\s+run|\.exe\b|\bffmpeg\b|pythonw)/i.test(cmd)) return 'machine'
  if (tool === 'Write' || tool === 'Edit' || tool === 'NotebookEdit') return 'laptop'
  return ''
}

const startOverlay = async ($: EngineInterface) => {
  const script = `${$.plugin.root}/overlay/critter.pyw`
  // ohne Konsolenfenster: pythonw startet das Overlay losgelöst und beendet sich sofort
  const code = `import subprocess,sys;subprocess.Popen([sys.executable, ${JSON.stringify(script)}], creationflags=0x00000008|0x00000200, close_fds=True)`
  await $.process.run(['pythonw', '-c', code], { timeoutMs: 8000 }).catch(() => {})
}


export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const home = (await $.env.get('USERPROFILE')) ?? (await $.env.get('HOME')) ?? ''
    statePath = home ? `${home}/.claude/critter/state.json` : ''
    await $.command.register({ name: 'critter', description: 'Start the desktop critter overlay (again)' })
    await update($, mood, m => ({ ...m, agents: [] }))  // alte Agenten aus früheren Sitzungen weg
    await fire($, 'hello', { mode: 'idle' })
    void startOverlay($)
    return next(e)
  })

  on('command.run', { command: 'critter' }, async $ => {
    await startOverlay($)
    return { text: 'Critter started (if one is already running, it stays at one).' }
  })

  on('prompt.edit', async ($, e, next) => {
    const m = await read($, mood)
    await set($, { typing: m.typing + 1, text: short(e.text, 80), event: e.text.length === 0 ? 'cleared' : m.event })
    return next(e)
  })

  on('prompt.submit', async ($, e, next) => {
    const ended = e.text.match(/<task-id>(\w+)<\/task-id>/)?.[1]
    if (ended) {
      await update($, mood, m => ({ ...m, agents: (m.agents ?? []).filter(a => a.id !== ended) }))
      await flush($)
    }
    if (e.origin.kind === 'composer') await fire($, classify(e.text), { mode: 'thinking', typing: 0, text: '' })
    return next(e)
  })

  on('turn.start', async ($, e, next) => {
    await set($, { mode: 'thinking', tool: '' })
    return next(e)
  })

  on('tool.call', { tool: 'Agent' }, async ($, e, next) => {
    const input = e as unknown as { model?: string; run_in_background?: boolean; description?: string }
    const id = e.tool_use_id
    const model = (input.model ?? 'opus').toLowerCase()
    await set($, { mode: 'tool-use', tool: 'Agent', detail: short(input.description ?? '', 40) })
    await update($, mood, m => ({ ...m, agents: [...(m.agents ?? []), { id, model, t: Date.now() }] }))
    await flush($)
    const ran = await next(e)
    const bg = (ran.text ?? '').match(/agentId: (\w+)/)?.[1]
    // Hintergrund-Agent: bleibt bis zur Fertig-Meldung, sonst ist er jetzt durch
    await update($, mood, m => ({ ...m, agents: (m.agents ?? []).map(a => (a.id === id && bg ? { ...a, id: bg } : a)).filter(a => bg || a.id !== id) }))
    await flush($)
    return ran
  })

  on('tool.call', async ($, e, next) => {
    if (e.tool === 'Agent') return next(e)
    const tool = e.tool.startsWith('mcp__') ? e.tool.split('__').pop() ?? e.tool : e.tool
    const ee = e as never as { command?: string; url?: string }
    await set($, { mode: 'tool-use', tool, detail: toolDetail(e as never), act: activity(tool, ee.command ?? '', ee.url ?? '') })
    const ran = await next(e)
    if (ran.deny !== undefined) await fire($, 'denied')
    else if (ran.isError === true) await fire($, 'error')
    else {
      // genaue Ereignisse aus Befehl + Ausgabe (keine Schätzung über die Oberfläche)
      const cmd = typeof (e as never as { command?: string }).command === 'string' ? (e as never as { command: string }).command : ''
      const out = (ran.text ?? '').slice(-4000)
      const ev =
        /\bgit\s+commit\b/.test(cmd) ? 'commit'
        : /\bgit\s+push\b/.test(cmd) ? 'push'
        : /(\d+\s+(failed|failing)|\bFAIL(ED)?\b|tests? failed)/i.test(out) && /(test|pytest|jest|vitest|mocha|cargo)/i.test(cmd) ? 'tests_fail'
        : /(\d+\s+passed|\bPASS(ED)?\b|all tests passed|\bOK\b)/.test(out) && /(test|pytest|jest|vitest|mocha|cargo)/i.test(cmd) ? 'tests_pass'
        : /\b(npm|pnpm|yarn)\s+(i|install|add)\b|\bpip3?\s+install\b/.test(cmd) ? 'install'
        : tool === 'Write' || tool === 'Edit' ? 'saved'
        : tool === 'WebSearch' || tool === 'WebFetch' ? 'found'
        : ''
      if (ev) await fire($, ev)
    }
    return ran
  })

  on('tool.check', async ($, e, next) => {
    const v = await next(e)
    if (v && (v as { decision?: string }).decision === 'ask') await fire($, 'ask', { mode: 'waiting' })
    return v
  })

  on('turn.complete', async ($, e, next) => {
    // Agenten ohne Fertig-Meldung nach 10 min aufräumen
    const cutoff = Date.now() - 10 * 60 * 1000
    await update($, mood, m => ({ ...m, agents: (m.agents ?? []).filter(a => (a.t ?? 0) > cutoff) }))
    let limit = -1
    try {
      const u = await $.session.usage()
      const five = (u.rateLimits ?? []).find(r => /five|5h|hour/i.test(r.kind))
      if (five) limit = Math.round(five.percentUsed)
    } catch {}
    await fire($, 'done', { mode: 'idle', tool: '', detail: '', act: '', limit })
    return next(e)
  })
}
