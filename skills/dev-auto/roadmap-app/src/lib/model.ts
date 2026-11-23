// Derives everything the page shows from data.json (written by roadmap.py).
export type Issue = {
  n: number; t: string; type: string | null; open: boolean; done: boolean; ms: string | null; later: boolean
  parent: number | null; kids: number; kidsDone: number; blockers: number[]; deps: number[]
  who: string | null; updated: string; status: string | null; agent: string | null; area: string | null
  ready?: boolean; holdReason?: string | null
  now?: NowState | null
}
export type DevPr = { n: number; t: string; head: string; auto: boolean; draft: boolean; checks: string; issues: number[]; updated: string }
type NowState = {
  stage: 0 | 1 | 2 | 3; status: string; warning: boolean
  who: { role: string; model: string | null; host: string }[]
  pr: (DevPr & { repo: 'hr-relay' | 'dot-agents' }) | null
}
export type Data = {
  generated?: string; areas: string[]; milestones?: string[]; issues: Issue[]
  pipeline: {
    devPrs?: DevPr[]; mergedSinceRelease?: number; devAheadOfStaging?: number | null; devAheadOfMain?: number | null
    stagingPr?: { n: number; head: string; checks: string }[]; release?: { tagName: string | null; publishedAt: string | null }
    cuts?: string[]; fullRun?: { conclusion: string | null; status: string; createdAt: string; url: string } | null
  }
}
export type Kind = 'you' | 'run' | 'blk' | 'ready'
export type Person = { role: string; model: string; acct: string }

export const REPO = 'https://github.com/julianthant/hr-relay'
export const issueUrl = (n: number) => `${REPO}/issues/${n}`
export const prUrl = (n: number, repo: 'hr-relay' | 'dot-agents' = 'hr-relay') => `https://github.com/julianthant/${repo}/pull/${n}`
export const STAGES = ['Code', 'Task CI', 'Review', 'PR'] as const
export const KIND_LABEL: Record<Kind, string> = { you: 'needs you', run: 'in progress', blk: 'blocked', ready: 'ready' }
const DEFAULT_MS = ['Staging cut', 'Mac sign-off', 'Windows', 'Later']

export function ago(iso: string | null | undefined) {
  if (!iso) return '—'
  const m = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 60000))
  return m < 60 ? `${m}m` : m < 1440 ? `${Math.round(m / 60)}h` : `${Math.round(m / 1440)}d`
}

export function people(who: string | null): Person[] {
  // "coder gpt-6.2-sol high @acct · PR worker claude-sonnet-5-5 medium @acct": the model is the first word with a digit or dash.
  return (who || '').split(' · ').filter(Boolean).map((p) => {
    const [body, acct = ''] = p.split(' @'), w = body.split(' ')
    const k = w.findIndex((x) => /[\d-]/.test(x))
    return k < 0 ? { role: body, model: '', acct } : { role: w.slice(0, k).join(' '), model: w.slice(k).join(' '), acct }
  })
}
export function build(d: Data) {
  const all = d.issues
  const by = new Map(all.map((i) => [i.n, i]))
  const kids = new Map<number, Issue[]>()
  for (const i of all) if (i.parent && by.has(i.parent)) kids.set(i.parent, [...(kids.get(i.parent) ?? []), i])
  const P = d.pipeline ?? {}
  const prByIssue = new Map<number, DevPr>()
  for (const p of P.devPrs ?? []) for (const n of p.issues) prByIssue.set(n, p)

  const running = (i: Issue) => i.open && (i.status === 'In Progress' || (!!i.agent && i.agent !== 'Queued'))
  const needsYou = (i: Issue) => i.open && (i.now ? i.now.status.startsWith('needs you') : i.agent === 'Needs you')
  const isTicket = (i: Issue) => i.type === 'Task' || i.type === 'Bug'
  const kind = (i: Issue): Kind => (
    needsYou(i) ? 'you' : running(i) ? 'run' : (isTicket(i) ? i.ready !== true : i.blockers.length > 0) ? 'blk' : 'ready'
  )
  const stageOf = (i: Issue) => i.now?.stage ?? (prByIssue.has(i.n) || i.agent === 'Merging' ? 3 : i.agent === 'Reviewing' || i.agent === 'Needs you' ? 2 : i.agent === 'CI running' ? 1 : 0)
  const msOf = (i: Issue) => i.ms ?? (i.parent ? by.get(i.parent)?.ms : null) ?? (i.later ? 'Later' : null)
  const label = (i: Issue) => i.t.split(' / ').pop()!

  // A work item is a leaf: a ticket, or a feature not split into tickets yet.
  const items = all.filter((i) => i.kids === 0 && (i.open || i.done))
  const isFeature = (i: Issue) => i.kids > 0 || (!i.parent && i.type === 'Feature')
  const features = all.filter(isFeature)

  const unblocks = new Map<number, Issue[]>()
  for (const i of all) if (i.open) for (const b of i.blockers) unblocks.set(b, [...(unblocks.get(b) ?? []), i])
  const holds = (n: number) => unblocks.get(n)?.length ?? 0

  const tally = (list: Issue[]) => {
    const o = list.filter((i) => i.open)
    return {
      total: list.length, done: list.filter((i) => i.done).length, remaining: o.length,
      running: o.filter((i) => running(i) && !needsYou(i)).length, you: o.filter(needsYou).length,
      blocked: o.filter((i) => kind(i) === 'blk').length,
      ready: o.filter((i) => kind(i) === 'ready').length,
    }
  }
  const msNames = [...new Set([...(d.milestones?.length ? d.milestones : DEFAULT_MS), ...items.filter((i) => i.open).map(msOf).filter((m): m is string => !!m)])]
  const milestones = msNames.map((name) => {
    const list = items.filter((i) => msOf(i) === name)
    const feats = features.filter((f) => f.open && msOf(f) === name)
    const gate = new Map<number, Issue>() // open blockers that live outside this milestone
    for (const i of list) if (i.open) for (const b of i.blockers) {
      const bi = by.get(b); if (!bi) continue
      if (msOf(bi) !== name) gate.set(b, bi)
    }
    return { name, ...tally(list), features: feats, gates: [...gate.values()].sort((a, b) => holds(b.n) - holds(a.n) || a.n - b.n) }
  })

  // Wave = 1 + longest dependency chain (open or closed) in front of a ticket.
  const memo = new Map<number, number>()
  const wave = (i: Issue, seen = new Set<number>()): number => {
    if (memo.has(i.n)) return memo.get(i.n)!
    if (seen.has(i.n)) return 1
    seen.add(i.n)
    let w = 1
    for (const dn of i.deps) { const di = by.get(dn); if (di && di.kids === 0 && di.parent) w = Math.max(w, 1 + wave(di, seen)) }
    memo.set(i.n, w); return w
  }
  const order: Record<Kind, number> = { you: 0, run: 1, blk: 2, ready: 3 }
  const gridRows = features.filter((f) => f.open && f.kids > 0).map((f) => {
    const ks = kids.get(f.n) ?? [], cells = new Map<number, { done: number; open: Issue[] }>()
    for (const k of ks) {
      const w = wave(k), c = cells.get(w) ?? { done: 0, open: [] }
      if (k.open) c.open.push(k); else if (k.done) c.done++
      cells.set(w, c)
    }
    for (const c of cells.values()) c.open.sort((a, b) => order[kind(a)] - order[kind(b)] || a.n - b.n)
    return { f, ms: msOf(f), cells, you: ks.some(needsYou), run: ks.some(running), left: ks.filter((k) => k.open).length }
  }).sort((a, b) => +b.you - +a.you || +b.run - +a.run || a.left - b.left || a.f.t.localeCompare(b.f.t))
  const maxWave = Math.max(1, ...gridRows.flatMap((r) => [...r.cells.keys()]))

  // A feature's state rolls up its open tickets: needs you > in progress > ready > blocked. Waits lists blockers outside the feature.
  const featureState = (f: Issue): { kind: Kind; running: number; waits: Issue[]; frees: Issue[]; tickets: Issue[] } => {
    const ks = (kids.get(f.n) ?? []).filter((k) => k.open)
    const src = f.kids === 0 ? [f] : ks
    const own = new Set(ks.map((k) => k.n))
    const waits = [...new Set(src.flatMap((k) => k.blockers))].filter((n) => !own.has(n)).map((n) => by.get(n)).filter((x): x is Issue => !!x?.open)
    const kinds = src.map(kind)
    const k: Kind = kinds.includes('you') ? 'you' : kinds.includes('run') ? 'run' : kinds.includes('ready') || !kinds.length ? 'ready' : 'blk'
    const tickets = kids.get(f.n) ?? []
    const frees = [...new Map([f, ...tickets].flatMap((x) => unblocks.get(x.n) ?? []).filter((x) => !own.has(x.n)).map((x) => [x.n, x])).values()]
    return { kind: k, running: kinds.filter((x) => x === 'run' || x === 'you').length, waits, frees, tickets }
  }
  const tickets = all.filter(isTicket).filter((i) => i.open || i.done)
  const open = tickets.filter((i) => i.open)
  const inFlight = open.filter((i) => i.now).sort((a, b) => +needsYou(b) - +needsYou(a) || +!!b.now?.warning - +!!a.now?.warning || stageOf(b) - stageOf(a) || a.n - b.n)
  const moving = new Set(inFlight.map((i) => i.n))
  // Header counts and the side lists share one set: open tickets, split into in flight, ready and blocked.
  const readyTickets = open.filter((i) => i.ready === true && !moving.has(i.n)).sort((a, b) => holds(b.n) - holds(a.n) || a.n - b.n)
  const blockedTickets = open.filter((i) => !moving.has(i.n) && i.ready !== true).sort((a, b) => a.blockers.length - b.blockers.length || holds(b.n) - holds(a.n) || a.n - b.n)

  return {
    by, kids, running, needsYou, kind, stageOf, msOf, label, holds, unblocks, prByIssue,
    overall: tally(tickets), milestones, gridRows, maxWave, inFlight, readyTickets, blockedTickets, featureState,
  }
}
export type Model = ReturnType<typeof build>
