// Smoke test for desktop/plugin.js outside the app: loads the real file with a
// stub @hermes/plugin-sdk, real React 19 and nanostores, feeds it backend state
// produced by tests/desktop_state.py, and server-renders every surface.
//
//   node tests/desktop_smoke.mjs <state.json> <dir-with-node_modules>
//
// The second argument is any folder whose node_modules has react, react-dom
// and nanostores (the hermes-agent checkout works).

import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { pathToFileURL, fileURLToPath } from 'node:url'

const [statePath, modulesDir] = process.argv.slice(2)
if (!statePath || !modulesDir) throw new Error('usage: desktop_smoke.mjs <state.json> <dir-with-node_modules>')

const require = createRequire(join(resolve(modulesDir), 'package.json'))
const React = require('react')
const { renderToString } = require('react-dom/server')
const nanostores = await import(pathToFileURL(require.resolve('nanostores')).href)
const state = JSON.parse(readFileSync(statePath, 'utf8'))

const failures = []
const check = (cond, msg) => {
  if (!cond) failures.push(msg)
}
// Any React dev warning (keys, bad props, hooks) fails the run.
const printError = console.error.bind(console)
console.error = (...args) => failures.push('console.error: ' + args.map(String).join(' ').slice(0, 300))

// ---- stub SDK ----
const h = React.createElement
const useValue = store => React.useSyncExternalStore(cb => store.listen(cb), () => store.get(), () => store.get())
const notifications = []
const navigations = []
const sdk = {
  atom: nanostores.atom,
  useValue,
  compactNumber: n => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : String(n)),
  host: {
    state: {
      gateway: nanostores.atom('open'),
      model: nanostores.atom('claude-opus-4-5'),
      profile: nanostores.atom('default'),
      busy: nanostores.atom(true)
    },
    notify: n => notifications.push(n),
    navigate: p => navigations.push(p)
  },
  Button: ({ children, ...p }) => h('button', p, children),
  Input: p => h('input', p),
  ListRow: ({ title, description, action }) => h('div', null, title, description, action),
  ToggleRow: ({ label, description }) => h('div', null, label, description),
  SegmentedControl: ({ options }) => h('div', null, options.map(o => h('span', { key: o.id }, o.label))),
  StatusDot: ({ tone }) => h('i', { 'data-tone': tone }),
  Tip: ({ label, children }) => h('span', { 'data-tip': typeof label === 'string' ? label : '' }, children),
  PALETTE_AREA: 'palette',
  ROUTES_AREA: 'routes',
  SIDEBAR_NAV_AREA: 'sidebar.nav',
  STATUSBAR_AREAS: { left: 'statusBar.left', right: 'statusBar.right' }
}
globalThis.__MC_SDK__ = sdk
globalThis.__MC_REACT__ = React
globalThis.__MC_JSX__ = require('react/jsx-runtime')

// ---- load plugin.js with its three imports rewired to the stubs ----
const here = fileURLToPath(new URL('.', import.meta.url))
let src = readFileSync(join(here, '..', 'desktop', 'plugin.js'), 'utf8')
const imports = src.match(/^import[\s\S]*?from '[^']+'\s*$/gm) || []
check(imports.length === 3, `expected 3 imports, found ${imports.length}`)
for (const line of imports) {
  check(/'(@hermes\/plugin-sdk|react|react\/jsx-runtime)'/.test(line), `illegal import: ${line}`)
  const names = line.match(/\{([\s\S]*?)\}/)[1].replace(/\s+as\s+/g, ': ')
  const from = line.match(/'([^']+)'/)[1]
  const obj = from === '@hermes/plugin-sdk' ? '__MC_SDK__' : from === 'react' ? '__MC_REACT__' : '__MC_JSX__'
  src = src.replace(line, `const {${names}} = globalThis.${obj}`)
}
for (const banned of ['document.', 'MutationObserver', 'eval(', 'new Function', 'window.']) {
  check(!src.includes(banned), `plugin.js uses ${banned}`)
}
const tmp = mkdtempSync(join(tmpdir(), 'mc-smoke-'))
writeFileSync(join(tmp, 'plugin.mjs'), src)
const plugin = (await import(pathToFileURL(join(tmp, 'plugin.mjs')).href)).default

// ---- stub ctx ----
const contributions = []
const settingsPages = []
const listeners = {}
const intervals = []
const restCalls = []
const ctx = {
  storage: { get: (k, d) => d, set: () => {}, remove: () => {} },
  registerMany: cs => contributions.push(...cs),
  register: c => contributions.push(c),
  registerSettingsPage: p => settingsPages.push(p),
  setInterval: (fn, ms) => (intervals.push({ fn, ms }), () => {}),
  setTimeout: () => () => {},
  onEvent: (type, fn) => ((listeners[type] = fn), () => {}),
  os: { notify: () => {} },
  rest: async path => {
    restCalls.push(path)
    if (path.startsWith('/snapshot')) return state.snapshot
    if (path.startsWith('/events')) return { ...state.events, events: [] }
    if (path.startsWith('/reset')) return { ok: true }
    throw new Error('404')
  }
}

check(plugin.id === 'mission-control', 'plugin id')
plugin.register(ctx)
await new Promise(r => setTimeout(r, 20)) // let the first /snapshot resolve

const byId = Object.fromEntries(contributions.map(c => [c.id, c]))
for (const id of ['pane', 'page', 'nav', 'chip', 'open', 'reset']) check(byId[id], `missing contribution ${id}`)
check(settingsPages.length === 1, 'settings page registered')
check(listeners['plugin.mission-control.telemetry'], 'push listener registered')
check(restCalls[0] === '/snapshot', 'first call is /snapshot')

const render = (c, label) => {
  try {
    return renderToString(c.render())
  } catch (err) {
    failures.push(`${label} threw: ${err.stack}`)
    return ''
  }
}

let pane = render(byId.pane, 'pane')
for (const s of ['Now', 'tool waterfall', 'read_file', 'terminal', 'web_search', 'Subagents', 'explore', 'review', 'Today', 'cached', 'Activity', 'High token burn']) {
  check(pane.includes(s), `pane missing "${s}"`)
}
check(pane.includes('FileNotFoundError'), 'error tooltip on failed tool')
check(pane.includes('search_files'), 'subagent tool shows in the waterfall')
const elapsed = (pane.match(/>([^<>]+)<\/span><span[^>]*>elapsed</) || [])[1]
check(elapsed && elapsed !== '0ms', `Now card elapsed should be the turn's real duration (got ${elapsed})`)
check(state.snapshot.daily.turns === 4, `subagent turns not counted as turns (got ${state.snapshot.daily.turns})`)
check(state.snapshot.turns[0].tool_calls === 6, `subagent tool rolls into parent (got ${state.snapshot.turns[0].tool_calls})`)
check(notifications.some(n => /burn/.test(n.title || '')), 'over-budget toast fired once')

// A pushed event lands immediately and ends the in-flight web_search row.
listeners['plugin.mission-control.telemetry']({ payload: { kind: 'tool.end', id: 'c3', tool: 'web_search', status: 'ok', duration_ms: 1234, turn_id: 't1', seq: 10_000, ts: Date.now() / 1000 } })
pane = render(byId.pane, 'pane after push')
check(pane.includes('1.2s'), 'pushed tool.end shows its duration')
// The same seq again (poll overlap) is ignored.
const before = render(byId.pane, 'pane before dup')
listeners['plugin.mission-control.telemetry']({ payload: { kind: 'tool.end', id: 'c3', tool: 'web_search', status: 'ok', duration_ms: 1234, turn_id: 't1', seq: 10_000, ts: Date.now() / 1000 } })
check(render(byId.pane, 'pane after dup') === before, 'duplicate seq is ignored')

const page = render(byId.page, 'page')
check(page.includes('Mission Control') || page.includes('Today'), 'page renders')
const chip = render(byId.chip, 'chip')
check(/k|M/.test(chip) && chip.includes('$'), 'chip shows tokens and cost')
render(settingsPages[0], 'settings')

byId.open.data.run()
check(navigations.includes('/mission-control'), 'palette open navigates')

// Polling tick runs without throwing.
for (const i of intervals) i.fn()
await new Promise(r => setTimeout(r, 20))

// Backend missing → notice instead of a crash.
const ctx2 = { ...ctx, rest: async () => { throw new Error('404 Not Found') }, registerMany: () => {}, registerSettingsPage: () => {}, onEvent: () => () => {} }
const captured = []
ctx2.registerMany = cs => captured.push(...cs)
plugin.register(ctx2)
await new Promise(r => setTimeout(r, 20))
const paneMissing = renderToString(captured.find(c => c.id === 'pane').render())
check(paneMissing.includes('Backend not loaded'), 'no-backend notice')

// Loop banner follows the user's threshold; the pet speaks once, only when shown.
const loopPane = async (threshold, petAlerts = true) => {
  const said = []
  const got = []
  const c = {
    ...ctx,
    storage: { get: (k, d) => (k === 'prefs' ? { loopThreshold: threshold, petAlerts } : d), set: () => {}, remove: () => {} },
    pet: { say: (text, opts) => (said.push({ text, opts }), () => {}) },
    registerMany: cs => got.push(...cs),
    registerSettingsPage: () => {},
    onEvent: () => () => {}
  }
  plugin.register(c)
  await new Promise(r => setTimeout(r, 20))
  return { html: renderToString(got.find(x => x.id === 'pane').render()), said }
}
check(!pane.includes('Possible loop'), 'default threshold 5 hides a 3x streak')
const at3 = await loopPane(3)
check(at3.html.includes('Possible loop') && at3.html.includes('git_status ×3'), 'threshold 3 shows the 3x streak')
check(at3.said.length === 1 && /git_status/.test(at3.said[0].text) && at3.said[0].opts.tone === 'error', `pet speaks once (got ${at3.said.length})`)
const off = await loopPane(0)
check(!off.html.includes('Possible loop') && off.said.length === 0, 'threshold off hides banner and pet')
const quiet = await loopPane(3, false)
check(quiet.html.includes('Possible loop') && quiet.said.length === 0, 'pet toggle off keeps the banner, silences the pet')

// Unpriced model + tool budget hit (the copilot / claude-haiku-5.5 case): no "— —", no "$0".
{
  const snap = JSON.parse(JSON.stringify(state.snapshot))
  Object.assign(snap.turns[0], { priced: false, est_cost: 0, model: 'claude-haiku-5.5', provider: 'copilot' })
  Object.assign(snap.daily, { est_cost: 0, unpriced_calls: 3 })
  snap.events = snap.events.filter(e => e.kind !== 'api.done')   // they carry the priced totals
  snap.spark = []
  const got = []
  const toasts = notifications.length
  plugin.register({
    ...ctx,
    storage: { get: (k, d) => (k === 'prefs' ? { turnToolBudget: 3 } : d), set: () => {}, remove: () => {} },
    rest: async p => (p.startsWith('/snapshot') ? snap : { events: [], max_seq: snap.max_seq, now: snap.now }),
    registerMany: cs => got.push(...cs),
    registerSettingsPage: () => {},
    onEvent: () => () => {}
  })
  await new Promise(r => setTimeout(r, 20))
  const html = renderToString(got.find(x => x.id === 'pane').render())
  const chipHtml = renderToString(got.find(x => x.id === 'chip').render())
  check(html.includes('Tool budget hit — 6 tool calls this turn (budget 3)'), 'tool-only breach is named as such')
  check(!html.includes('— —') && !html.includes('High token burn'), 'no "— —" and no token-burn claim without a known cost')
  check(!/>\$0</.test(html) && !chipHtml.includes('$0'), 'unpriced day never shows $0')
  check(notifications.slice(toasts).some(n => /tool budget/.test(n.title)), 'toast names the tool budget')
}

if (failures.length) {
  printError('FAIL\n- ' + failures.join('\n- '))
  process.exit(1)
}
console.log(`DESKTOP SMOKE PASS — ${contributions.length} contributions, pane ${pane.length} chars, chip "${chip.replace(/<[^>]+>/g, '')}"`)
