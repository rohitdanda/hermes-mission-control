/**
 * Mission Control — live dashboard for Hermes Desktop.
 *
 * Feeds, fastest first:
 *   1. push: hooks broadcast `plugin.mission-control.telemetry` the instant a
 *      tool, model call, subagent or approval happens;
 *   2. poll: `/events?since=` (1 s while work is running, 5 s idle) fills any
 *      gap and is the only feed when push can't reach this window;
 *   3. `/snapshot` refreshes the aggregates (today, turns, subagents).
 * Events are de-duplicated by `seq`, so the feeds can overlap freely.
 *
 * Read-only: the plugin never writes to the agent. The budget watchdog only alerts.
 */

import {
  atom,
  Button,
  compactNumber,
  host,
  Input,
  ListRow,
  PALETTE_AREA,
  ROUTES_AREA,
  SegmentedControl,
  SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS,
  StatusDot,
  Tip,
  ToggleRow,
  useValue
} from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx as rawJsx, jsxs as rawJsxs } from 'react/jsx-runtime'

// React 19 wants `key` as jsx()'s third argument, not inside props.
const keyed = fn => (type, props) => {
  if (props && props.key !== undefined) {
    const { key, ...rest } = props
    return fn(type, rest, key)
  }
  return fn(type, props)
}
const jsx = keyed(rawJsx)
const jsxs = keyed(rawJsxs)

const PLUGIN_ID = 'mission-control'
const PUSH_EVENT = `plugin.${PLUGIN_ID}.telemetry`
const ROUTE = '/mission-control'

const DEFAULT_PREFS = { showCost: true, alertToast: true, turnBudgetUsd: 0.5, turnToolBudget: 40, feedSize: 30, loopThreshold: 5, petAlerts: true }
const MAX_ROWS = 40
const WATERFALL_ROWS = 12
const SEEN_MAX = 2000
const SPINNER = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'

// ---- formatting ---------------------------------------------------------------

const fmtMs = ms => {
  if (ms == null || !isFinite(ms)) return '—'
  if (ms < 1000) return `${Math.max(0, Math.round(ms))}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  const m = Math.floor(ms / 60000)
  return `${m}m ${Math.floor((ms % 60000) / 1000)}s`
}

const fmtUsd = v => {
  if (v == null || !isFinite(v)) return '—'
  if (v === 0) return '$0'
  if (v >= 1) return `$${v.toFixed(2)}`
  if (v >= 0.01) return `$${v.toFixed(3)}`
  return `$${v.toFixed(4)}`
}

// A turn's cost is only meaningful when every call in it was priced.
const fmtTurnCost = turn => {
  if (!turn) return '—'
  if (turn.priced === false) return turn.est_cost > 0 ? `≥${fmtUsd(turn.est_cost)}` : '—'
  return fmtUsd(turn.est_cost || 0)
}

// Today's cost: all calls unpriced → "—", some → "≥$x", never a misleading "$0".
const dailyCost = d => (d.unpriced_calls ? (d.est_cost > 0 ? `≥${fmtUsd(d.est_cost)}` : '—') : fmtUsd(d.est_cost))

const num = v => compactNumber(Number(v) || 0)
const prompt = t => (t ? (t.tokens_prompt ?? (t.tokens_in || 0) + (t.cache_read || 0) + (t.cache_write || 0)) : 0)

// Providers reached through a subscription rather than per-token billing.
const SUBSCRIPTION = /copilot|codex|claude-code|subscription/i
const costHint = (provider, priced) =>
  priced === false
    ? 'Model not in the price table'
    : provider && SUBSCRIPTION.test(provider)
      ? `List-price equivalent — ${provider} is a subscription, not billed per token`
      : 'List-price estimate'

// ---- styles (theme variables only) --------------------------------------------

const C = {
  text: 'var(--ui-text-primary)',
  text2: 'var(--ui-text-secondary)',
  text3: 'var(--ui-text-tertiary)',
  text4: 'var(--ui-text-quaternary)',
  stroke: 'var(--ui-stroke-tertiary)',
  track: 'var(--ui-bg-quaternary)',
  accent: 'var(--ui-accent)',
  green: 'var(--ui-green)',
  red: 'var(--ui-red)',
  amber: 'var(--ui-yellow)'
}

const tint = (color, pct) => `color-mix(in srgb, ${color} ${pct}%, transparent)`

const S = {
  root: { display: 'flex', flexDirection: 'column', gap: 10, padding: 10, height: '100%', overflowY: 'auto', fontSize: 12, color: C.text2 },
  card: { display: 'flex', flexDirection: 'column', gap: 6, padding: '8px 10px', borderRadius: 6, border: `1px solid ${C.stroke}` },
  label: { fontSize: 10, fontWeight: 600, letterSpacing: '0.06em', textTransform: 'uppercase', color: C.text4 },
  row: { display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 },
  between: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8, minWidth: 0 },
  nums: { fontVariantNumeric: 'tabular-nums' },
  ellipsis: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 },
  muted: { color: C.text3 },
  faint: { color: C.text4, fontSize: 11 }
}

const STATUS_COLOR = {
  running: C.accent,
  ok: C.green,
  completed: C.green,
  error: C.red,
  failed: C.red,
  blocked: C.amber,
  cancelled: C.text4,
  interrupted: C.text4,
  ask: C.amber
}
const STATUS_GLYPH = { ok: '✓', completed: '✓', error: '✕', failed: '✕', blocked: '⊘', cancelled: '⊘', interrupted: '⊘', ask: '?' }

const statusColor = s => STATUS_COLOR[s] || C.text3
// Hermes calls an ordinary (non-orchestrating) child a "leaf"; show that as plain "subagent".
const roleName = r => (!r || r === 'leaf' ? 'subagent' : r)

// Literal child lists go through jsxs (static, no per-child keys needed);
// .map() results are passed as one keyed array element inside them.
const el = (type, style, children, extra) => (Array.isArray(children) ? jsxs : jsx)(type, { style, children, ...extra })
const div = (style, children, extra) => el('div', style, children, extra)
const span = (style, children, extra) => el('span', style, children, extra)

// ---- store --------------------------------------------------------------------

const emptyLive = () => ({ rows: {}, order: [], feed: [], subs: {}, turn: null, spark: [], loop: null, lastEventAt: 0 })

// A possible loop worth showing: same tool + same arguments, N times in a row by one agent.
const loopShown = (loop, prefs) => Boolean(loop && prefs.loopThreshold > 0 && loop.count >= prefs.loopThreshold)

function createStore(ctx) {
  const $prefs = atom({ ...DEFAULT_PREFS, ...ctx.storage.get('prefs', {}) })
  const $conn = atom({ state: 'loading', detail: null }) // loading | ok | no-agent | no-backend | error
  const $snap = atom(null)
  const $live = atom(emptyLive())
  const $now = atom(Date.now())

  let skewMs = 0 // local clock − server clock
  let cursor = 0
  let seeded = false
  let everOk = false
  const seen = new Set()
  const seenOrder = []
  const alerted = new Set()
  const petted = new Set()

  // One pet line per turn+tool, only once the streak reaches the display threshold.
  const maybePet = loop => {
    const prefs = $prefs.get()
    if (!prefs.petAlerts || !loopShown(loop, prefs)) return
    const key = `${loop.turn_id}|${loop.tool}`
    if (petted.has(key)) return
    petted.add(key)
    try {
      ctx.pet?.say?.(`🔁 ${loop.tool} ×${loop.count} — possible loop`, { id: 'mc-loop', tone: 'error', ttlMs: 6000 })
    } catch {}
  }
  const local = ts => (ts ? ts * 1000 + skewMs : Date.now())
  const syncClock = serverNow => {
    if (serverNow) skewMs = Date.now() - serverNow * 1000
  }

  const setPrefs = patch => {
    $prefs.set({ ...$prefs.get(), ...patch })
    ctx.storage.set('prefs', $prefs.get())
  }

  const markSeen = seq => {
    seen.add(seq)
    seenOrder.push(seq)
    if (seenOrder.length > SEEN_MAX) seen.delete(seenOrder.shift())
  }

  const turnRatio = (turn, live, prefs) => {
    if (!turn) return 0
    const tools = Math.max(turn.tool_calls || 0, live ? countTurnTools(live, turn.turn_id) : 0)
    const byCost = prefs.turnBudgetUsd > 0 && turn.priced !== false ? (turn.est_cost || 0) / prefs.turnBudgetUsd : 0
    const byTools = prefs.turnToolBudget > 0 ? tools / prefs.turnToolBudget : 0
    return Math.max(byCost, byTools)
  }

  const maybeAlert = (turn, live) => {
    const prefs = $prefs.get()
    if (!turn || !prefs.alertToast || alerted.has(turn.turn_id)) return
    if (turnRatio(turn, live, prefs) < 1) return
    alerted.add(turn.turn_id)
    const b = breach(turn, live, prefs)
    try {
      host.notify({ kind: 'warning', title: `Mission Control: ${b.title}`, message: b.text })
    } catch {}
    try {
      ctx.os?.notify?.({ title: `Hermes: ${b.title}`, body: b.text, activate: ROUTE })
    } catch {}
  }

  // Apply a batch of events to one copy of the live state, then publish once.
  const ingest = events => {
    const fresh = (events || []).filter(e => e && e.seq != null && !seen.has(e.seq))
    if (!fresh.length) return false
    fresh.sort((a, b) => a.seq - b.seq)
    const prev = $live.get()
    const live = { ...prev, rows: { ...prev.rows }, order: prev.order.slice(), feed: prev.feed.slice(), subs: { ...prev.subs }, spark: prev.spark.slice() }
    const feedSize = $prefs.get().feedSize
    let refresh = false

    for (const e of fresh) {
      markSeen(e.seq)
      const at = local(e.ts)
      switch (e.kind) {
        case 'tool.start': {
          if (!live.rows[e.id]) live.order.push(e.id)
          live.rows[e.id] = { id: e.id, tool: e.tool, turn_id: e.root_turn_id || e.turn_id, start: at, end: null, status: 'running' }
          break
        }
        case 'tool.end': {
          const was = live.rows[e.id]
          const dur = typeof e.duration_ms === 'number' ? e.duration_ms : null
          const start = was ? was.start : dur != null ? at - dur : at
          if (!was) live.order.push(e.id)
          const row = {
            ...(was || { id: e.id }),
            turn_id: e.root_turn_id || e.turn_id || was?.turn_id,
            tool: e.tool || was?.tool || 'tool',
            start,
            end: dur != null ? start + dur : at,
            status: e.status || 'ok',
            duration_ms: dur ?? at - start,
            error_type: e.error_type
          }
          live.rows[e.id] = row
          live.feed.unshift({ seq: e.seq, kind: 'tool', at, ...row })
          // Keep the turn's longest identical-call streak (the backend sends the
          // running count on every tool.end, so any display threshold works).
          if (e.repeat >= 2 && row.turn_id) {
            const cur = live.loop && live.loop.turn_id === row.turn_id ? live.loop : null
            if (!cur || e.repeat >= cur.count) live.loop = { turn_id: row.turn_id, tool: row.tool, count: e.repeat, at }
          }
          break
        }
        case 'api.done': {
          const same = live.turn && live.turn.turn_id === e.turn_id
          const t = e.turn || {}
          live.turn = {
            ...(same ? live.turn : {}),
            ...t,
            turn_id: e.turn_id,
            model: e.model,
            provider: e.provider,
            api_duration: e.api_duration,
            started_at: t.started_at ? local(t.started_at) : same ? live.turn.started_at : at,
            updated_at: t.ended_at ? local(t.ended_at) : at
          }
          const point = { turn_id: e.turn_id, tokens: prompt(e.turn) + (e.turn?.tokens_out || 0), est_cost: e.turn?.est_cost || 0, priced: e.turn?.priced }
          const i = live.spark.findIndex(p => p.turn_id === e.turn_id)
          if (i >= 0) live.spark[i] = point
          else live.spark.push(point)
          if (live.spark.length > 12) live.spark.splice(0, live.spark.length - 12)
          refresh = true
          break
        }
        case 'api.error':
          live.feed.unshift({ seq: e.seq, kind: 'api', at, status: e.retryable ? 'blocked' : 'error', tool: e.retryable ? 'model call · retrying' : 'model call failed', error_type: [e.status_code, e.error_type, e.reason].filter(Boolean).join(' · ') })
          break
        case 'subagent.start':
          live.subs[e.id] = { ...(live.subs[e.id] || {}), id: e.id, role: e.role, goal: e.goal, turn_id: e.turn_id, start: at, status: 'running' }
          refresh = true
          break
        case 'subagent.end': {
          const s = live.subs[e.id] || { id: e.id, start: at - (e.duration_ms || 0) }
          live.subs[e.id] = { ...s, status: e.status || 'completed', end: at, duration_ms: e.duration_ms, summary: e.summary ?? s.summary }
          live.feed.unshift({ seq: e.seq, kind: 'sub', at, status: e.status || 'completed', tool: s.role && s.role !== 'leaf' ? `subagent · ${s.role}` : 'subagent', duration_ms: e.duration_ms })
          refresh = true
          break
        }
        case 'approval.ask':
          live.feed.unshift({ seq: e.seq, kind: 'approval', at, status: 'ask', tool: 'approval requested', error_type: e.surface })
          break
        case 'approval.answer': {
          const denied = e.choice === 'deny' || e.choice === 'smart_deny'
          live.feed.unshift({ seq: e.seq, kind: 'approval', at, status: denied ? 'error' : 'ok', tool: `approval · ${e.choice || '?'}` })
          refresh = true
          break
        }
        default:
          break
      }
    }

    // Bound memory: keep running rows, drop the oldest finished ones.
    while (live.order.length > MAX_ROWS) {
      const idx = live.order.findIndex(id => live.rows[id]?.status !== 'running')
      if (idx < 0) break
      delete live.rows[live.order[idx]]
      live.order.splice(idx, 1)
    }
    if (live.feed.length > feedSize) live.feed.length = feedSize
    live.lastEventAt = Date.now()
    $live.set(live)
    $now.set(Date.now())
    maybeAlert(live.turn, live)
    maybePet(live.loop)
    return refresh
  }

  // Merge a snapshot: aggregates replace; live rows/subagents are only seeded.
  const applySnapshot = snap => {
    $snap.set(snap)
    const prev = $live.get()
    const live = { ...prev, rows: { ...prev.rows }, order: prev.order.slice(), subs: { ...prev.subs } }
    for (const r of snap.inflight || []) {
      if (!live.rows[r.id]) {
        live.rows[r.id] = { id: r.id, tool: r.tool, turn_id: r.turn_id, start: local(r.started_at), end: null, status: 'running' }
        live.order.push(r.id)
      }
    }
    // Rows the server no longer reports in flight (reaped or missed end) stop pulsing.
    const inflight = new Set((snap.inflight || []).map(r => r.id))
    for (const id of live.order) {
      const r = live.rows[id]
      if (r && r.status === 'running' && !inflight.has(id) && Date.now() - r.start > 5000) {
        live.rows[id] = { ...r, status: 'cancelled', end: Date.now() }
      }
    }
    for (const s of snap.subagents || []) {
      const was = live.subs[s.id] || {}
      live.subs[s.id] = {
        ...was,
        id: s.id,
        role: s.role,
        goal: s.goal,
        turn_id: s.parent,
        status: s.status,
        start: was.start ?? local(s.started_at),
        end: s.ended_at ? local(s.ended_at) : was.end,
        duration_ms: s.duration_ms ?? was.duration_ms,
        summary: s.summary ?? was.summary
      }
    }
    const top = (snap.turns || [])[0]
    if (top && (!live.turn || live.turn.turn_id === top.turn_id || local(top.started_at) > (live.turn.started_at || 0))) {
      const same = live.turn && live.turn.turn_id === top.turn_id
      live.turn = { ...(same ? live.turn : {}), ...top, started_at: local(top.started_at), updated_at: top.ended_at ? local(top.ended_at) : live.turn?.updated_at }
    }
    // A pane opened mid-loop (or after a reload) still shows the turn's longest streak.
    if (top && top.loop_max >= 2) {
      const cur = live.loop && live.loop.turn_id === top.turn_id ? live.loop : null
      if (!cur || top.loop_max > cur.count) live.loop = { turn_id: top.turn_id, tool: top.loop_tool, count: top.loop_max, at: local(top.ended_at) }
    }
    live.spark = (snap.spark || []).map(p => ({ ...p }))
    $live.set(live)
  }

  const resetLocal = () => {
    seen.clear()
    seenOrder.length = 0
    alerted.clear()
    petted.clear()
    cursor = 0
    seeded = false
    $live.set(emptyLive())
  }

  const fail = err => {
    const msg = String(err?.message || err || '')
    $conn.set(everOk ? { state: 'error', detail: msg } : { state: 'no-backend', detail: msg })
  }

  let snapBusy = false
  const pollSnapshot = async () => {
    if (snapBusy) return
    snapBusy = true
    try {
      const snap = await ctx.rest('/snapshot', { timeoutMs: 8000 })
      everOk = true
      syncClock(snap.now)
      if (!snap.daily) {
        $conn.set({ state: 'no-agent', detail: null })
        $snap.set(snap)
        return
      }
      $conn.set({ state: 'ok', detail: null })
      applySnapshot(snap)
      if (!seeded) {
        seeded = true
        ingest(snap.events)
        cursor = Math.max(cursor, snap.max_seq || 0)
      }
    } catch (err) {
      fail(err)
    } finally {
      snapBusy = false
    }
  }

  let eventsBusy = false
  const pollEvents = async () => {
    if (eventsBusy) return
    eventsBusy = true
    try {
      const r = await ctx.rest(`/events?since=${cursor}`, { timeoutMs: 8000 })
      everOk = true
      syncClock(r.now)
      if (r.reset) {
        resetLocal()
        void pollSnapshot()
      }
      if (ingest(r.events)) scheduleSnapshot()
      cursor = Math.max(r.reset ? 0 : cursor, r.max_seq || 0)
    } catch (err) {
      fail(err)
    } finally {
      eventsBusy = false
    }
  }

  let snapTimer = null
  const scheduleSnapshot = () => {
    if (snapTimer) return
    snapTimer = ctx.setTimeout(() => {
      snapTimer = null
      void pollSnapshot()
    }, 400)
  }

  const onPush = ev => {
    const e = ev?.payload
    if (e && ingest([e])) scheduleSnapshot()
  }

  const resetAll = async () => {
    try {
      await ctx.rest('/reset', { method: 'POST' })
      resetLocal()
      await pollSnapshot()
      host.notify({ kind: 'info', message: "Mission Control: today's stats reset." })
    } catch (err) {
      host.notify({ kind: 'error', message: `Mission Control: reset failed (${String(err?.message || err)})` })
    }
  }

  const anyRunning = live => live.order.some(id => live.rows[id]?.status === 'running') || Object.values(live.subs).some(s => s.status === 'running')

  const start = () => {
    let tick = 0
    void pollSnapshot()
    ctx.setInterval(() => {
      tick++
      const live = $live.get()
      let busy = false
      try {
        busy = Boolean(host.state.busy.get())
      } catch {}
      const active = busy || anyRunning(live) || Date.now() - live.lastEventAt < 15000
      if (active || tick % 5 === 0) void pollEvents()
      if (tick % (active ? 4 : 10) === 0) void pollSnapshot()
      $now.set(Date.now())
    }, 1000)
    // Fast clock only while something is visibly moving (stopwatches, pulse).
    ctx.setInterval(() => {
      if (anyRunning($live.get())) $now.set(Date.now())
    }, 100)
    ctx.onEvent(PUSH_EVENT, onPush)
  }

  return { $prefs, $conn, $snap, $live, $now, setPrefs, resetAll, start, turnRatio }
}

function countTurnTools(live, turnId) {
  if (!turnId) return 0
  let n = 0
  for (const id of live.order) if (live.rows[id]?.turn_id === turnId && live.rows[id].status !== 'running') n++
  return n
}

// ---- small pieces -------------------------------------------------------------

function Card({ title, aside, children, style }) {
  return div({ ...S.card, ...style }, [
    title || aside ? div(S.between, [span(S.label, title, { key: 't' }), aside ? span(S.faint, aside, { key: 'a' }) : null], { key: 'h' }) : null,
    ...(Array.isArray(children) ? children : [children])
  ])
}

function Stat({ label, value, tone, hint }) {
  const body = div({ display: 'flex', flexDirection: 'column', gap: 1, minWidth: 0 }, [
    span({ ...S.nums, fontSize: 14, fontWeight: 600, color: tone || C.text }, value, { key: 'v' }),
    span(S.faint, label, { key: 'l' })
  ])
  return hint ? jsx(Tip, { label: hint, children: body }) : body
}

function Bar({ ratio, color, height = 4 }) {
  const pct = Math.max(0, Math.min(1, ratio || 0)) * 100
  return div({ height, borderRadius: height, background: C.track, overflow: 'hidden' }, div({ width: `${pct}%`, height: '100%', background: color, transition: 'width 300ms ease' }))
}

function Glyph({ status, now }) {
  if (status === 'running') return span({ color: C.accent, width: 12, display: 'inline-block', textAlign: 'center' }, SPINNER[Math.floor(now / 80) % SPINNER.length])
  return span({ color: statusColor(status), width: 12, display: 'inline-block', textAlign: 'center' }, STATUS_GLYPH[status] || '•')
}

function Empty({ title, hint }) {
  return div({ padding: '14px 4px', textAlign: 'center' }, [
    div({ color: C.text2, fontWeight: 500 }, title, { key: 't' }),
    hint ? div({ ...S.faint, marginTop: 4, lineHeight: 1.45 }, hint, { key: 'h' }) : null
  ])
}

// ---- sections -----------------------------------------------------------------

function Header({ store }) {
  const gateway = useValue(host.state.gateway)
  const model = useValue(host.state.model)
  const profile = useValue(host.state.profile)
  const live = useValue(store.$live)
  const conn = useValue(store.$conn)
  const tone = gateway !== 'open' ? 'bad' : conn.state === 'ok' ? 'good' : conn.state === 'loading' ? 'muted' : 'warn'
  const provider = live.turn?.provider
  return div(S.between, [
    div(S.row, [
      jsx(StatusDot, { tone, key: 'd' }),
      span({ ...S.ellipsis, color: C.text, fontWeight: 600 }, model || live.turn?.model || 'no model', { key: 'm' }),
      provider ? span(S.faint, provider, { key: 'p' }) : null
    ], { key: 'l' }),
    span(S.faint, profile || '', { key: 'r' })
  ])
}

function LoopBanner({ store, turn }) {
  const live = useValue(store.$live)
  const prefs = useValue(store.$prefs)
  const loop = live.loop
  if (!turn || !loop || loop.turn_id !== turn.turn_id || !loopShown(loop, prefs)) return null
  return jsx(Tip, {
    label: 'The same tool was called with identical arguments this many times in a row by one agent.',
    children: div(
      { padding: '6px 8px', borderRadius: 4, background: tint(C.amber, 16), border: `1px solid ${tint(C.amber, 45)}`, color: C.amber, fontWeight: 600 },
      `🔁 Possible loop — ${loop.tool} ×${loop.count} with identical arguments`
    )
  })
}

function BurnBanner({ store, turn, ratio }) {
  const now = useValue(store.$now)
  const prefs = useValue(store.$prefs)
  const live = useValue(store.$live)
  if (ratio < 1) return null
  const pulse = 0.65 + 0.35 * Math.abs(Math.sin(now / 350))
  return div(
    { padding: '6px 8px', borderRadius: 4, background: tint(C.red, 16), border: `1px solid ${tint(C.red, 45)}`, color: C.red, fontWeight: 600, opacity: pulse },
    `⚠ ${breach(turn, live, prefs).text}`
  )
}

// Which budget a turn crossed, in words. Cost is only named when it is known
// and shown, so an unpriced model never prints "— —".
function breach(turn, live, prefs) {
  const tools = Math.max(turn?.tool_calls || 0, live ? countTurnTools(live, turn?.turn_id) : 0)
  const costKnown = prefs.showCost && turn?.priced !== false && (turn?.est_cost || 0) > 0
  const overCost = costKnown && prefs.turnBudgetUsd > 0 && turn.est_cost >= prefs.turnBudgetUsd
  const overTools = prefs.turnToolBudget > 0 && tools >= prefs.turnToolBudget
  const toolsText = `${tools} tool call${tools === 1 ? '' : 's'} this turn (budget ${prefs.turnToolBudget})`
  if (overCost && overTools) return { title: 'high token burn', text: `High token burn — ${fmtTurnCost(turn)} and ${tools} tool calls this turn` }
  if (overCost) return { title: 'high token burn', text: `High token burn — ${fmtTurnCost(turn)} this turn (budget ${fmtUsd(prefs.turnBudgetUsd)})` }
  if (overTools) return { title: 'tool budget hit', text: `Tool budget hit — ${toolsText}` }
  return { title: 'over budget', text: `Over budget — ${tools} tool calls this turn` }
}

function NowCard({ store }) {
  const live = useValue(store.$live)
  const prefs = useValue(store.$prefs)
  const now = useValue(store.$now)
  const busy = useValue(host.state.busy)
  const turn = live.turn
  const running = live.order.filter(id => live.rows[id]?.status === 'running').length
  const subsRunning = Object.values(live.subs).filter(s => s.status === 'running').length
  const state = running ? `running ${running} tool${running > 1 ? 's' : ''}` : subsRunning ? `${subsRunning} subagent${subsRunning > 1 ? 's' : ''} working` : busy ? 'thinking' : 'idle'
  const stateColor = running || subsRunning ? C.accent : busy ? C.text : C.text4
  const ratio = store.turnRatio(turn, live, prefs)
  const barColor = ratio >= 1 ? C.red : ratio >= 0.8 ? C.amber : C.accent
  const active = busy || running || subsRunning
  const elapsed = turn?.started_at ? (active ? now : turn.updated_at || now) - turn.started_at : null
  const tools = Math.max(turn?.tool_calls || 0, countTurnTools(live, turn?.turn_id))
  // Idle: keep the last turn on screen, dimmed and labelled, so it never reads as live data.
  const label = active ? state : turn ? 'idle · last turn' : 'idle'

  return Card({
    title: 'Now',
    aside: jsxs('span', { style: { ...S.row, color: stateColor }, children: [active ? jsx(Glyph, { status: 'running', now }) : null, label] }),
    children: [
      turn
        ? div({ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0, 1fr))', gap: 6, opacity: active ? 1 : 0.6, transition: 'opacity 300ms ease' }, [
            jsx(Stat, { key: 'e', label: 'elapsed', value: fmtMs(elapsed > 0 ? elapsed : null) }),
            jsx(Stat, { key: 'i', label: 'tokens in', value: num(prompt(turn)), hint: `${num(turn.tokens_in)} new · ${num(turn.cache_read)} cached · ${num(turn.cache_write)} cache write` }),
            jsx(Stat, { key: 'o', label: 'tokens out', value: num(turn.tokens_out) }),
            prefs.showCost
              ? jsx(Stat, { key: 'c', label: 'est. cost', value: fmtTurnCost(turn), tone: ratio >= 1 ? C.red : ratio >= 0.8 ? C.amber : undefined, hint: costHint(turn.provider, turn.priced) })
              : jsx(Stat, { key: 't', label: 'tool calls', value: String(tools) })
          ], { key: 'stats' })
        : jsx(Empty, { key: 'empty', title: 'Run a task and watch it work here', hint: 'Tool calls, model calls and subagents stream in live.' }),
      turn
        ? div({ display: 'flex', flexDirection: 'column', gap: 3 }, [
            div(S.between, [
              span(S.faint, 'turn budget', { key: 'l' }),
              span({ ...S.faint, ...S.nums }, `${Math.round(ratio * 100)}% · ${tools}/${prefs.turnToolBudget || '∞'} tools${prefs.showCost && prefs.turnBudgetUsd > 0 ? ` · ${fmtUsd(prefs.turnBudgetUsd)}` : ''}`, { key: 'r' })
            ], { key: 'h' }),
            jsx(Bar, { key: 'b', ratio, color: barColor })
          ], { key: 'budget' })
        : null,
      turn ? jsx(BurnBanner, { key: 'burn', store, turn, ratio }) : null,
      turn ? jsx(LoopBanner, { key: 'loop', store, turn }) : null,
      jsx(Waterfall, { key: 'wf', store, hint: Boolean(turn) })
    ]
  })
}

function Waterfall({ store, hint }) {
  const live = useValue(store.$live)
  const now = useValue(store.$now)
  // The latest burst of activity, whatever turn it belongs to: a run that
  // finished (or handed off to a follow-up turn) stays on screen until the next.
  const all = live.order.map(id => live.rows[id]).filter(Boolean)
  if (!all.length) return hint ? div({ ...S.faint, padding: '4px 0' }, 'No tool calls yet — run a task and watch them stream in here.') : null
  const newest = Math.max(...all.map(r => r.start))
  const burst = all.filter(r => r.status === 'running' || (r.end || now) >= newest - 120000).sort((a, b) => a.start - b.start)
  const rows = burst.slice(-WATERFALL_ROWS)
  const hidden = burst.length - rows.length
  const anyRunning = rows.some(r => r.status === 'running')
  const t0 = Math.min(...rows.map(r => r.start))
  const t1 = Math.max(anyRunning ? now : 0, ...rows.map(r => r.end || now))
  const span_ = Math.max(1000, t1 - t0)
  const aside = anyRunning ? `span ${fmtMs(span_)}` : `last run · ${fmtMs(span_)} · ${fmtMs(now - t1)} ago`

  return div({ display: 'flex', flexDirection: 'column', gap: 3, marginTop: 2 }, [
    div(S.between, [span(S.label, 'tool waterfall', { key: 'l' }), span({ ...S.faint, ...S.nums }, aside, { key: 'r' })], { key: 'h' }),
    hidden > 0 ? div(S.faint, `+${hidden} earlier`, { key: 'more' }) : null,
    ...rows.map(r => {
      const running = r.status === 'running'
      const end = r.end || now
      const left = ((r.start - t0) / span_) * 100
      const width = Math.max(1.5, ((end - r.start) / span_) * 100)
      const color = statusColor(r.status)
      const pulse = running ? 0.55 + 0.45 * Math.abs(Math.sin(now / 260)) : 1
      const row = div({ display: 'grid', gridTemplateColumns: '14px minmax(0, 0.9fr) minmax(0, 1.4fr) 46px', alignItems: 'center', gap: 6 }, [
        jsx(Glyph, { key: 'g', status: r.status, now }),
        span({ ...S.ellipsis, color: running ? C.text : C.text2 }, r.tool || 'tool', { key: 'n' }),
        div({ position: 'relative', height: 8, borderRadius: 2, background: C.track }, div({ position: 'absolute', top: 0, bottom: 0, left: `${left}%`, width: `${Math.min(width, 100 - left)}%`, borderRadius: 2, background: color, opacity: pulse, boxShadow: running ? `0 0 6px ${tint(color, 60)}` : 'none', transition: running ? 'none' : 'width 200ms ease' }), { key: 'b' }),
        span({ ...S.nums, textAlign: 'right', color: running ? C.accent : color }, fmtMs(end - r.start), { key: 'd' })
      ], { key: r.id })
      return r.error_type ? jsx(Tip, { key: r.id, label: `${r.status}: ${r.error_type}`, children: row }) : row
    })
  ])
}

function Subagents({ store }) {
  const live = useValue(store.$live)
  const now = useValue(store.$now)
  const [open, setOpen] = useState(null)
  const subs = Object.values(live.subs)
    .sort((a, b) => (a.status === 'running') - (b.status === 'running') || a.start - b.start)
    .reverse()
    .slice(0, 6)
  if (!subs.length) return null
  const running = subs.filter(s => s.status === 'running').length

  return Card({
    title: 'Subagents',
    aside: running ? `${running} running` : `${subs.length} recent`,
    children: subs.map(s => {
      const isRunning = s.status === 'running'
      const color = statusColor(s.status)
      const dur = isRunning ? now - s.start : s.duration_ms ?? (s.end ? s.end - s.start : null)
      const expanded = open === s.id
      return div(
        { padding: '6px 8px', borderRadius: 4, border: `1px solid ${isRunning ? tint(C.accent, 50) : C.stroke}`, background: isRunning ? tint(C.accent, 7) : 'transparent', cursor: s.summary ? 'pointer' : 'default' },
        [
          div(S.between, [
            div(S.row, [jsx(Glyph, { key: 'g', status: isRunning ? 'running' : s.status, now }), span({ color: C.text, fontWeight: 600 }, roleName(s.role), { key: 'r' })], { key: 'l' }),
            span({ ...S.nums, color: isRunning ? C.accent : color }, `${isRunning ? '' : (s.status || '') + ' · '}${fmtMs(dur)}`, { key: 'd' })
          ], { key: 'h' }),
          s.goal ? div({ ...S.faint, ...S.ellipsis, marginTop: 2 }, s.goal, { key: 'g' }) : null,
          expanded && s.summary ? div({ marginTop: 4, color: C.text2, lineHeight: 1.45 }, s.summary, { key: 's' }) : null
        ],
        { key: s.id, onClick: () => s.summary && setOpen(expanded ? null : s.id) }
      )
    })
  })
}

function Feed({ store }) {
  const live = useValue(store.$live)
  const now = useValue(store.$now)
  if (!live.feed.length) return null
  return Card({
    title: 'Activity',
    aside: `${live.feed.length}`,
    children: live.feed.map(f => {
      const row = div({ display: 'grid', gridTemplateColumns: '14px minmax(0, 1fr) auto', gap: 6, alignItems: 'center' }, [
        jsx(Glyph, { key: 'g', status: f.status, now }),
        span({ ...S.ellipsis, color: f.status === 'error' || f.status === 'failed' ? C.red : C.text2 }, f.tool, { key: 'n' }),
        span({ ...S.faint, ...S.nums }, f.duration_ms != null ? fmtMs(f.duration_ms) : fmtMs(now - f.at) + ' ago', { key: 'd' })
      ], { key: f.seq })
      return f.error_type ? jsx(Tip, { key: f.seq, label: f.error_type, children: row }) : row
    })
  })
}

function Spark({ store, prefs }) {
  const live = useValue(store.$live)
  const points = live.spark
  if (!points.length) return null
  const byCost = prefs.showCost && points.every(p => p.priced !== false) && points.some(p => p.est_cost > 0)
  const vals = points.map(p => (byCost ? p.est_cost : p.tokens) || 0)
  const max = Math.max(...vals, 1e-9)
  const current = live.turn?.turn_id
  return div({ display: 'flex', flexDirection: 'column', gap: 3 }, [
    div(S.between, [span(S.faint, `last ${points.length} turns`, { key: 'l' }), span(S.faint, byCost ? 'est. cost' : 'tokens', { key: 'r' })], { key: 'h' }),
    div(
      { display: 'flex', alignItems: 'flex-end', gap: 3, height: 34 },
      points.map((p, i) => {
        const over = byCost ? prefs.turnBudgetUsd > 0 && p.est_cost > prefs.turnBudgetUsd : false
        const bar = div({ flex: 1, minWidth: 4, height: `${Math.max(6, (vals[i] / max) * 100)}%`, borderRadius: 2, background: over ? C.red : p.turn_id === current ? C.accent : tint(C.text3, 55), transition: 'height 300ms ease' })
        return jsx(Tip, { key: p.turn_id, label: `${num(p.tokens)} tokens${byCost ? ' · ' + fmtUsd(p.est_cost) : ''}`, children: bar })
      }),
      { key: 'b' }
    )
  ])
}

function Today({ store }) {
  const snap = useValue(store.$snap)
  const prefs = useValue(store.$prefs)
  const provider = useValue(store.$live).turn?.provider
  const d = snap?.daily
  if (!d) return null
  const promptTotal = prompt(d)
  const hit = promptTotal ? Math.round((d.cache_read / promptTotal) * 100) : 0
  const cells = [
    jsx(Stat, { key: 'turns', label: 'turns', value: String(d.turns) }),
    jsx(Stat, { key: 'in', label: 'tokens in', value: num(promptTotal) }),
    jsx(Stat, { key: 'out', label: 'tokens out', value: num(d.tokens_out) }),
    jsx(Stat, { key: 'tools', label: 'tool calls', value: String(d.tool_calls) }),
    jsx(Stat, { key: 'err', label: 'errors', value: String(d.errors), tone: d.errors ? C.red : undefined, hint: d.api_retries ? `${d.api_retries} model calls retried` : undefined }),
    jsx(Stat, { key: 'appr', label: 'approvals', value: d.approvals_denied ? `${d.approvals} (${d.approvals_denied}✕)` : String(d.approvals), hint: d.approvals_unanswered ? `${d.approvals_unanswered} timed out or cancelled` : undefined })
  ]
  if (prefs.showCost) {
    // All calls unpriced → "—", some unpriced → "≥$x", never a misleading "$0".
    cells.push(jsx(Stat, { key: 'cost', label: 'est. cost', value: dailyCost(d), hint: d.unpriced_calls ? `${d.unpriced_calls} calls on models without a price` : costHint(provider) }))
  }
  return Card({
    title: 'Today',
    aside: d.cache_read
      ? jsx(Tip, { label: `${hit}% of prompt tokens came from cache`, children: span({ color: C.green, fontWeight: 600 }, `⚡ ${num(d.cache_read)} cached`) })
      : d.date,
    children: [div({ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0, 1fr))', gap: 8 }, cells, { key: 'g' }), jsx(Spark, { key: 's', store, prefs })]
  })
}

function Notice({ store }) {
  const gateway = useValue(host.state.gateway)
  const conn = useValue(store.$conn)
  if (gateway !== 'open') return Card({ children: jsx(Empty, { title: 'Connect a gateway to stream telemetry', hint: 'Mission Control reads live hook data from the gateway this window is connected to.' }) })
  if (conn.state === 'no-backend')
    return Card({
      children: jsx(Empty, {
        title: 'Backend not loaded',
        hint: 'Enable the agent half with `hermes plugins enable mission-control`, then restart the gateway. If it is enabled, update Hermes.'
      })
    })
  if (conn.state === 'no-agent')
    return Card({
      children: jsx(Empty, {
        title: 'Hooks are not reporting to this gateway',
        hint: 'The backend is up but no telemetry reached it. Restart the gateway after enabling the plugin; with dashboard.turn_isolation on, turns run in a separate process.'
      })
    })
  if (conn.state === 'error') return div({ ...S.faint, color: C.amber }, `Reconnecting… ${conn.detail || ''}`)
  return null
}

function Dashboard({ store, wide }) {
  const conn = useValue(store.$conn)
  const ready = conn.state === 'ok' || conn.state === 'error'
  const main = [jsx(NowCard, { key: 'now', store }), jsx(Subagents, { key: 'subs', store })]
  const side = [jsx(Today, { key: 'today', store }), jsx(Feed, { key: 'feed', store })]
  return div({ ...S.root, ...(wide ? { padding: '16px 20px', maxWidth: 1100, margin: '0 auto', width: '100%' } : null) }, [
    jsx(Header, { key: 'h', store }),
    jsx(Notice, { key: 'n', store }),
    ready
      ? wide
        ? div({ display: 'grid', gridTemplateColumns: 'minmax(0, 1.3fr) minmax(0, 1fr)', gap: 12, alignItems: 'start' }, [div({ display: 'flex', flexDirection: 'column', gap: 10 }, main, { key: 'a' }), div({ display: 'flex', flexDirection: 'column', gap: 10 }, side, { key: 'b' })], { key: 'grid' })
        : div({ display: 'flex', flexDirection: 'column', gap: 10 }, [...main, ...side], { key: 'stack' })
      : null
  ])
}

function Chip({ store }) {
  const snap = useValue(store.$snap)
  const live = useValue(store.$live)
  const prefs = useValue(store.$prefs)
  const now = useValue(store.$now)
  const busy = useValue(host.state.busy)
  const d = snap?.daily
  const running = live.order.some(id => live.rows[id]?.status === 'running')
  const ratio = store.turnRatio(live.turn, live, prefs)
  const color = ratio >= 1 ? C.red : ratio >= 0.8 ? C.amber : C.text3
  const tokens = d ? prompt(d) + (d.tokens_out || 0) : 0
  const label = d ? `${num(tokens)}${prefs.showCost ? ' · ' + dailyCost(d) : ''}` : 'MC'
  return jsx(Tip, {
    label: d ? `Mission Control — today: ${d.turns} turns, ${d.tool_calls} tool calls, ${num(tokens)} tokens${prefs.showCost ? ', ' + dailyCost(d) + ' est.' : ''}` : 'Mission Control',
    children: jsxs('button', {
      type: 'button',
      onClick: () => host.navigate(ROUTE),
      style: { display: 'inline-flex', alignItems: 'center', gap: 4, height: '100%', padding: '0 6px', fontSize: '0.6875rem', color, background: 'transparent', border: 0, cursor: 'pointer', fontVariantNumeric: 'tabular-nums' },
      children: [
        span({ width: 10, display: 'inline-block', textAlign: 'center', color: busy || running ? C.accent : color }, busy || running ? SPINNER[Math.floor(now / 80) % SPINNER.length] : '◎'),
        label
      ]
    })
  })
}

// ---- settings -----------------------------------------------------------------

function NumberField({ value, onCommit, step, prefix }) {
  const [text, setText] = useState(String(value))
  return jsxs('div', {
    style: { display: 'inline-flex', alignItems: 'center', gap: 4, width: 110 },
    children: [
      prefix ? span(S.muted, prefix) : null,
      jsx(Input, {
        type: 'number',
        min: 0,
        step,
        value: text,
        onChange: e => setText(e.target.value),
        onBlur: () => {
          const v = Math.max(0, Number(text))
          if (isFinite(v)) onCommit(v)
          else setText(String(value))
        }
      })
    ]
  })
}

function SettingsPage({ store }) {
  const prefs = useValue(store.$prefs)
  const set = store.setPrefs
  return div({ display: 'flex', flexDirection: 'column' }, [
    jsx(ToggleRow, { key: 'cost', checked: prefs.showCost, label: 'Show estimated cost', description: 'List-price estimates from a bundled table. Models without a price show "—".', onChange: on => set({ showCost: on }) }),
    jsx(ToggleRow, { key: 'toast', checked: prefs.alertToast, label: 'Alert on high token burn', description: 'A toast (and an OS notification when Hermes is in the background) once per turn that crosses a budget.', onChange: on => set({ alertToast: on }) }),
    jsx(ListRow, { key: 'usd', title: 'Turn cost budget', description: 'Per-turn estimated cost that turns the budget bar red. 0 turns it off.', action: jsx(NumberField, { value: prefs.turnBudgetUsd, step: 0.05, prefix: '$', onCommit: v => set({ turnBudgetUsd: v }) }) }),
    jsx(ListRow, { key: 'tools', title: 'Turn tool-call budget', description: 'Tool calls in one turn before the budget bar turns red. 0 turns it off.', action: jsx(NumberField, { value: prefs.turnToolBudget, step: 1, onCommit: v => set({ turnToolBudget: Math.round(v) }) }) }),
    jsx(ListRow, {
      key: 'feed',
      title: 'Activity feed size',
      description: 'How many recent events the Activity card keeps.',
      action: jsx(SegmentedControl, { value: String(prefs.feedSize), options: [{ id: '15', label: '15' }, { id: '30', label: '30' }, { id: '60', label: '60' }], onChange: v => set({ feedSize: Number(v) }) })
    }),
    jsx(ListRow, {
      key: 'loop',
      title: 'Loop alert threshold',
      description: 'Identical tool calls (same tool, same arguments, one agent) in a row before the possible-loop banner shows.',
      action: jsx(SegmentedControl, {
        value: String(prefs.loopThreshold),
        options: [{ id: '3', label: '3' }, { id: '5', label: '5' }, { id: '10', label: '10' }, { id: '0', label: 'off' }],
        onChange: v => set({ loopThreshold: Number(v) })
      })
    }),
    jsx(ToggleRow, { key: 'pet', checked: prefs.petAlerts, label: 'Pet speaks up on loops', description: 'A line in the desktop pet’s speech bubble when a possible loop appears (if a pet is on screen).', onChange: on => set({ petAlerts: on }) }),
    jsx(ListRow, { key: 'reset', title: "Reset today's stats", description: 'Clears counters, turns and the feed. Nothing is sent to the agent.', action: jsx(Button, { variant: 'outline', size: 'sm', onClick: () => void store.resetAll(), children: 'Reset' }) })
  ])
}

// ---- plugin -------------------------------------------------------------------

export default {
  id: PLUGIN_ID,
  name: 'Mission Control',
  register(ctx) {
    const store = createStore(ctx)

    ctx.registerMany([
      {
        id: 'pane',
        area: 'panes',
        title: 'Mission Control',
        data: { placement: 'right', width: '340px' },
        render: () => jsx(Dashboard, { store, wide: false })
      },
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: ROUTE },
        render: () => jsx(Dashboard, { store, wide: true })
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        data: { path: ROUTE, label: 'Mission Control', codicon: 'dashboard' }
      },
      {
        id: 'chip',
        area: STATUSBAR_AREAS.right,
        order: 105,
        render: () => jsx(Chip, { store })
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: { id: 'mission-control.open', label: 'Mission Control: Open dashboard', keywords: ['mission', 'control', 'telemetry', 'tokens', 'cost'], run: () => host.navigate(ROUTE) }
      },
      {
        id: 'reset',
        area: PALETTE_AREA,
        data: { id: 'mission-control.reset', label: "Mission Control: Reset today's stats", keywords: ['mission', 'control', 'reset'], run: () => void store.resetAll() }
      }
    ])

    ctx.registerSettingsPage?.({
      id: 'settings',
      title: 'Mission Control',
      icon: 'dashboard',
      render: () => jsx(SettingsPage, { store })
    })

    store.start()
  }
}
