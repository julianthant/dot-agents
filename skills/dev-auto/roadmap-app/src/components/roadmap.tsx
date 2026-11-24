import { ArrowUpRight, Check, Ellipsis, X } from 'lucide-react'
import { useEffect, useState, type ReactNode } from 'react'
import { Badge } from '@/components/ui/badge'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { KIND_LABEL, STAGES, ago, issueUrl, people, prUrl, type Issue, type Kind, type Model } from '@/lib/model'
import type { Feed } from '@/lib/use-data'
import { cn } from '@/lib/utils'

const ext = { target: '_blank', rel: 'noopener noreferrer' }

export function IssueLink({ i, children, className }: { i: Issue; children?: ReactNode; className?: string }) {
  return <a href={issueUrl(i.n)} {...ext} className={cn('hover:underline', className)}>{children ?? i.t}</a>
}

// One open ticket. Shape carries the state too: solid = moving, "!" = needs you, x = blocked, hollow = ready.
export function Mark({ kind, className }: { kind: Kind; className?: string }) {
  return (
    <span aria-hidden className={cn('relative inline-grid size-3.5 shrink-0 place-items-center rounded-[3px] border-[1.5px] text-[10px] leading-none font-extrabold',
      kind === 'run' && 'border-run bg-run', kind === 'you' && 'border-you bg-you text-[#1a1205]', kind === 'blk' && 'border-blk text-blk',
      kind === 'ready' && 'border-faint', className)}>
      {kind === 'you' && '!'}{kind === 'blk' && <X className="size-2.5" strokeWidth={3} />}
    </span>
  )
}

export function Legend({ className }: { className?: string }) {
  return (
    <div className={cn('flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground', className)}>
      {(['run', 'you', 'blk', 'ready'] as Kind[]).map((k) => <span key={k} className="inline-flex items-center gap-1.5"><Mark kind={k} />{KIND_LABEL[k]}</span>)}
      <span className="inline-flex items-center gap-1.5"><span className="text-done">3✓</span>done in that wave</span>
    </div>
  )
}

// The PR stage names its pull request and carries its checks on the dot.
export function StageTrack({ m, i, compact }: { m: Model; i: Issue; compact?: boolean }) {
  const cur = m.stageOf(i), you = m.needsYou(i), pr = i.now ? i.now.pr : m.prByIssue.get(i.n)
  const prDot = pr && (pr.checks === 'green' ? 'border-done bg-done ring-done-bg' : pr.checks === 'failing' ? 'border-blk bg-blk ring-3 ring-blk-bg' : 'border-run bg-run ring-3 ring-run-bg')
  return (
    <div className="inline-flex items-start" aria-label={`Stage ${pr && cur === 3 ? `PR ${pr.n}, checks ${pr.checks}` : STAGES[cur]}`} role="group">
      {STAGES.map((s, k) => (
        <div key={s} className="flex items-start">
          {k > 0 && <span className={cn('mt-[5px] h-px', compact ? 'w-2' : 'w-3', k <= cur ? 'bg-done/60' : 'bg-border')} />}
          <div className={cn('grid justify-items-center gap-1', compact ? 'w-10' : 'w-12')}>
            <span className={cn('grid size-2.5 place-items-center rounded-full border-[1.5px] border-faint text-background',
              k < cur && 'border-done bg-done', k === cur && (you ? 'border-you bg-you ring-3 ring-you-bg' : i.now?.warning ? 'border-blk bg-blk ring-3 ring-blk-bg' : 'border-run bg-run ring-3 ring-run-bg'), k === 3 && prDot, k === 3 && pr && '-mt-0.5 size-3.5')}>
              {k === 3 && pr && (pr.checks === 'green' ? <Check className="size-2.5" strokeWidth={3.5} /> : pr.checks === 'failing' ? <X className="size-2.5" strokeWidth={3.5} /> : <Ellipsis className="size-2.5" strokeWidth={3.5} />)}
            </span>
            {k === 3 && pr
              ? <a href={prUrl(pr.n, i.now?.pr?.repo)} {...ext} title={`${pr.t} · checks ${pr.checks}`} className={cn('text-[10.5px] whitespace-nowrap hover:underline', k === cur ? 'font-semibold text-foreground' : 'text-faint')}>PR {pr.n}</a>
              : <span className={cn('text-[10.5px] whitespace-nowrap', k === cur ? 'font-semibold text-foreground' : 'text-faint')}>{s}</span>}
          </div>
        </div>
      ))}
    </div>
  )
}

const PHASE: Record<string, string> = { Coding: 'coding', 'CI running': 'waiting on CI', Reviewing: 'in review', Fixing: 'fixing', 'Needs you': 'waiting on you', 'Handed off': 'handed off', Merging: 'merging', Queued: 'queued' }
export const phase = (i: Issue) => (i.agent ? PHASE[i.agent] ?? i.agent.toLowerCase() : 'working')

export function Who({ i, inline }: { i: Issue; inline?: boolean }) {
  const ps = people(i.who)
  if (!ps.length) return <span className="text-xs text-faint">no agent recorded</span>
  return (
    <div className={cn(inline ? 'flex flex-wrap gap-x-3 gap-y-0.5' : 'grid gap-1')}>
      {ps.map((p) => (
        <div key={p.role + p.acct} className="min-w-0 text-xs leading-snug">
          <span className="text-foreground">{p.role}</span> <span className="text-muted-foreground">{p.model}</span>
          {p.acct && <span className="font-mono text-[11px] text-faint"> @{p.acct}</span>}
        </div>
      ))}
    </div>
  )
}

function MiniRow({ m, i }: { m: Model; i: Issue }) {
  return (
    <li className="flex items-start gap-2 py-0.5">
      <Mark kind={m.kind(i)} className="mt-0.5" />
      <IssueLink i={i} className="min-w-0 flex-1 text-xs leading-snug"><span className="text-faint">#{i.n}</span> {i.t}</IssueLink>
    </li>
  )
}

// Popover body for a ticket or a feature: status, stage and who, waits on, unblocks, tickets, issue link.
function Details({ m, i }: { m: Model; i: Issue }) {
  const isFeat = i.kids > 0 || (!i.parent && i.type === 'Feature')
  const fs = isFeat ? m.featureState(i) : null
  const k = fs ? fs.kind : m.kind(i), parent = i.parent ? m.by.get(i.parent) : undefined
  const waits = fs ? fs.waits : i.blockers.map((n) => m.by.get(n)).filter((x): x is Issue => !!x)
  const frees = fs ? fs.frees : m.unblocks.get(i.n) ?? []
  const pr = m.prByIssue.get(i.n)
  const inFlight = !fs && (m.running(i) || !!pr)
  const tickets = fs ? [...fs.tickets].sort((a, b) => +b.open - +a.open || a.n - b.n) : []
  return (
    <>
      <div className="grid gap-1">
        <IssueLink i={i} className="font-semibold leading-snug">{i.t}</IssueLink>
        <div className="text-xs text-muted-foreground">#{i.n}{parent && <> · {parent.t}</>}{m.msOf(i) && <> · {m.msOf(i)}</>}</div>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge kind={k} />
        {fs && (i.kids > 0 ? <span className="text-xs text-muted-foreground">{i.kidsDone}/{i.kids} tickets done{fs.running ? ` · ${fs.running} in progress` : ''}</span> : <span className="text-xs text-muted-foreground">not ticketed yet</span>)}
        {!fs && i.agent && <span className="text-xs text-muted-foreground">{phase(i)} · updated {ago(i.updated)} ago</span>}
      </div>
      {inFlight && <StageTrack m={m} i={i} />}
      {inFlight && <Who i={i} />}
      <Section title={waits.length ? `Waits on ${waits.length}` : 'Waits on nothing'}>
        {waits.length > 0 && <ul>{waits.map((w) => <MiniRow key={w.n} m={m} i={w} />)}</ul>}
      </Section>
      <Section title={frees.length ? `Unblocks ${frees.length}` : 'Unblocks nothing directly'}>
        {frees.length > 0 && <ul>{frees.slice(0, 6).map((w) => <MiniRow key={w.n} m={m} i={w} />)}{frees.length > 6 && <li className="text-xs text-faint">and {frees.length - 6} more</li>}</ul>}
      </Section>
      {tickets.length > 0 && (
        <Section title={`Tickets ${tickets.length}`}>
          <ul className="max-h-48 overflow-y-auto">{tickets.map((t) => t.open ? <MiniRow key={t.n} m={m} i={t} /> : (
            <li key={t.n} className="flex items-start gap-2 py-0.5"><span className="w-3.5 shrink-0 text-center text-xs text-done">✓</span><IssueLink i={t} className="min-w-0 flex-1 text-xs leading-snug text-muted-foreground"><span className="text-faint">#{t.n}</span> {t.t}</IssueLink></li>
          ))}</ul>
        </Section>
      )}
      <a href={issueUrl(i.n)} {...ext} className="inline-flex items-center gap-1 justify-self-start text-xs font-medium text-run hover:underline">Open #{i.n} on GitHub<ArrowUpRight className="size-3.5" /></a>
    </>
  )
}
const POP = 'w-[min(360px,calc(100vw-24px))] gap-3 p-3.5 text-sm'

// The grid square: a button that opens the ticket's details.
export function Square({ m, i, size = 'md' }: { m: Model; i: Issue; size?: 'md' | 'lg' }) {
  const k = m.kind(i)
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button type="button" aria-label={`#${i.n} ${i.t}, ${KIND_LABEL[k]}`}
          className={cn('inline-grid place-items-center rounded-[5px] outline-offset-1 hover:bg-accent data-[state=open]:bg-accent', size === 'lg' ? 'size-6' : 'size-5')}>
          <Mark kind={k} />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" collisionPadding={12} className={POP}><Details m={m} i={i} /></PopoverContent>
    </Popover>
  )
}
function Section({ title, children }: { title: string; children?: ReactNode }) {
  return <div className="grid gap-1 border-t pt-2"><div className="text-[11px] font-medium text-muted-foreground">{title}</div>{children}</div>
}

export function StatusBadge({ kind }: { kind: Kind }) {
  return (
    <Badge variant="outline" className={cn('h-5 gap-1.5 border-transparent px-2 text-[11.5px] font-semibold',
      kind === 'run' && 'bg-run-bg text-run', kind === 'you' && 'bg-you-bg text-you', kind === 'blk' && 'bg-blk-bg text-blk', kind === 'ready' && 'bg-secondary text-muted-foreground')}>
      {KIND_LABEL[kind]}
    </Badge>
  )
}

// Stacked status bar: done, in progress, needs you, blocked, ready. Width = share of all items.
export type Tally = { total: number; done: number; running: number; you: number; blocked: number; ready: number; remaining: number }
export function StatusBar({ t, className }: { t: Tally; className?: string }) {
  const seg = [['done', t.done, 'bg-done'], ['in progress', t.running, 'bg-run'], ['needs you', t.you, 'bg-you'], ['blocked', t.blocked, 'bg-blk/55'], ['ready', t.ready, 'bg-faint/50']] as const
  return (
    <div role="img" aria-label={`${t.done} of ${t.total} done, ${t.running} in progress, ${t.you} need you, ${t.blocked} blocked, ${t.ready} ready`}
      className={cn('flex h-2 w-full gap-px overflow-hidden rounded-full bg-muted', className)}>
      {seg.map(([k, n, c]) => n > 0 && <span key={k} className={c} style={{ flexGrow: n, flexBasis: 0, minWidth: 3 }} />)}
    </div>
  )
}
export function Panel({ title, meta, right, children, className, id }: { title: ReactNode; meta?: ReactNode; right?: ReactNode; children: ReactNode; className?: string; id?: string }) {
  return (
    <section aria-labelledby={id} className={cn('min-w-0 rounded-[10px] border bg-card p-4', className)}>
      <div className="mb-3 flex flex-wrap items-baseline gap-x-2.5 gap-y-1">
        <h2 id={id} className="text-base font-bold tracking-[-0.01em]">{title}</h2>
        {meta && <span className="text-xs text-muted-foreground">{meta}</span>}
        {right && <div className="ml-auto flex flex-wrap items-center gap-3 text-xs text-muted-foreground max-sm:ml-0">{right}</div>}
      </div>
      {children}
    </section>
  )
}

export function Stamp({ feed }: { feed: Feed }) {
  const [, tick] = useState(0)
  useEffect(() => { const id = setInterval(() => tick((x) => x + 1), 15_000); return () => clearInterval(id) }, [])
  const gen = feed.data?.generated
  const stale = gen ? Date.now() - Date.parse(gen) > 10 * 60_000 : false
  if (feed.error && !feed.data) return null
  if (feed.error) return <span role="status" className="text-xs text-you">Board update failed ({feed.error}). Retrying every 30 s.</span>
  return (
    <span role="status" className={cn('text-xs', stale ? 'text-you' : 'text-muted-foreground')}>
      {gen ? `Board updated ${new Date(gen).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}` : 'Board update time unavailable'}
      {stale && ` · ${ago(gen)} old. Check the render job.`}
    </span>
  )
}

export function Counts({ m }: { m: Model }) {
  const o = m.overall
  const c = (n: number, label: string, cls?: string) => <span className={cls}><b className="mr-1.5 text-lg font-bold">{n}</b>{label}</span>
  return (
    <div className="flex flex-wrap items-baseline gap-x-5 gap-y-1.5 text-sm">
      {c(m.inFlight.length, 'in flight')}
      {c(m.inFlight.filter(m.needsYou).length, 'waiting on you', m.inFlight.some(m.needsYou) ? 'text-you' : undefined)}
      {c(m.blockedTickets.length, 'blocked')}
      {c(m.readyTickets.length, 'ready')}
      <span className="h-5 w-px self-center bg-border" />
      {c(o.done, `of ${o.total} tickets done`)}
      {c(o.remaining, 'remaining')}
    </div>
  )
}

// Now: tickets in flight with stage and who.
function NowWho({ i }: { i: Issue }) {
  const workers = i.now?.who ?? []
  if (!workers.length) return <span className="text-xs text-faint">no worker</span>
  return workers.map((w) => (
    <div key={w.role} className="text-xs leading-snug">
      {w.role}{w.model && <span className="text-muted-foreground"> {w.model}</span>}
      <span className="text-muted-foreground"> · {w.host}</span>
    </div>
  ))
}

function NowStatus({ m, i }: { m: Model; i: Issue }) {
  const kind = m.needsYou(i) ? 'you' : i.now?.warning ? 'blk' : i.now?.status === 'working' ? 'run' : 'ready'
  return <><Dot kind={kind} />{i.now?.status}</>
}

export function NowTable({ m, track = true }: { m: Model; track?: boolean }) {
  if (!m.inFlight.length) return <Empty>No tickets in flight.</Empty>
  return (
    <>
    <ul className="grid md:hidden">
      {m.inFlight.map((i) => {
        return (
          <li key={i.n} className="grid gap-1.5 border-b py-3 first:pt-0 last:border-b-0 last:pb-0">
            <IssueLink i={i} className="font-semibold [overflow-wrap:anywhere]">{i.t}</IssueLink>
            <div className="text-xs text-muted-foreground">#{i.n}{m.msOf(i) && ` · ${m.msOf(i)}`} · <NowStatus m={m} i={i} /> · {ago(i.updated)}</div>
            <StageTrack m={m} i={i} compact />
            <NowWho i={i} />
          </li>
        )
      })}
    </ul>
    <Table className="text-[13px] max-md:hidden">
      <TableHeader><TableRow className="hover:bg-transparent">
        <TableHead className="h-8 text-[11.5px]">Ticket</TableHead><TableHead className="h-8 text-[11.5px]">Stage</TableHead>
        <TableHead className="h-8 text-[11.5px]">Who</TableHead><TableHead className="h-8 text-[11.5px]">Status</TableHead><TableHead className="h-8 text-right text-[11.5px]">Updated</TableHead>
      </TableRow></TableHeader>
      <TableBody>
        {m.inFlight.map((i) => {
          const parent = i.parent ? m.by.get(i.parent) : undefined
          return (
            <TableRow key={i.n} className="align-top hover:bg-transparent">
              <TableCell className="max-w-[340px] whitespace-normal py-2.5">
                <IssueLink i={i} className="font-semibold">{i.t}</IssueLink>
                <div className="text-xs text-muted-foreground">#{i.n}{parent && ` · ${parent.t}`}{m.msOf(i) && ` · ${m.msOf(i)}`}</div>
              </TableCell>
              <TableCell className="py-2.5">{track ? <StageTrack m={m} i={i} /> : <span className="font-medium">{STAGES[m.stageOf(i)]}</span>}</TableCell>
              <TableCell className="py-2.5 whitespace-normal"><NowWho i={i} /></TableCell>
              <TableCell className="py-2.5 whitespace-normal"><NowStatus m={m} i={i} /></TableCell>
              <TableCell className="py-2.5 text-right text-xs text-muted-foreground">{ago(i.updated)}</TableCell>
            </TableRow>
          )
        })}
      </TableBody>
    </Table>
    </>
  )
}
export function Dot({ kind }: { kind: Kind }) {
  return <span aria-hidden className={cn('mr-1.5 inline-block size-[7px] rounded-full align-[1px]', kind === 'you' ? 'bg-you' : kind === 'run' ? 'bg-run' : kind === 'blk' ? 'bg-blk' : 'bg-faint')} />
}

export function MilestoneGates({ m, ms }: { m: Model; ms: Model['milestones'][number] }) {
  if (!ms.gates.length) return <div className="text-xs text-done">Nothing outside blocks it</div>
  return (
    <div className="grid gap-1">
      {ms.gates.length > 0 && <div className="text-[11px] font-medium text-muted-foreground">Waits on {ms.gates.length} from other milestones</div>}
      <ul className="grid gap-0.5">
        {ms.gates.slice(0, 5).map((g) => <MiniRow key={g.n} m={m} i={g} />)}
        {ms.gates.length > 5 && <li className="text-xs text-faint">and {ms.gates.length - 5} more</li>}
      </ul>
    </div>
  )
}

const STATE_ORDER: Record<Kind, number> = { you: 0, run: 1, blk: 2, ready: 3 }
// Every open feature of a milestone in one row design: what is moving first, then what is stuck, then what can start.
export function FeatureList({ m, feats }: { m: Model; feats: Issue[] }) {
  if (!feats.length) return <div className="text-xs text-faint">No open features</div>
  const rows = feats.map((f) => ({ f, s: m.featureState(f) }))
    .sort((a, b) => STATE_ORDER[a.s.kind] - STATE_ORDER[b.s.kind] || +(b.f.kids > 0) - +(a.f.kids > 0) || b.f.kidsDone / (b.f.kids || 1) - a.f.kidsDone / (a.f.kids || 1) || a.f.n - b.f.n)
  return (
    <ul className="-mx-1.5 grid">
      {rows.map(({ f, s }) => (
        <li key={f.n}>
          <Popover>
            <PopoverTrigger asChild>
              <button type="button" aria-label={`${f.t}, ${KIND_LABEL[s.kind]}, ${f.kids ? `${f.kidsDone} of ${f.kids} tickets done` : 'not ticketed yet'}`}
                className="grid w-full grid-cols-[14px_minmax(0,1fr)_auto] items-center gap-x-2.5 rounded-md px-1.5 py-[5px] text-left hover:bg-accent/70 data-[state=open]:bg-accent">
                <Mark kind={s.kind} />
                <span className="truncate text-[13px] font-medium">{f.t}</span>
                {f.kids > 0 ? (
                  <span className="flex items-center gap-2">
                    <span aria-hidden className="h-1.5 w-10 overflow-hidden rounded-full bg-border"><span className="block h-full bg-done" style={{ width: `${(100 * f.kidsDone) / f.kids}%` }} /></span>
                    <span className="w-8 text-right text-xs text-muted-foreground"><b className="font-semibold text-foreground">{f.kidsDone}</b>/{f.kids}</span>
                  </span>
                ) : <span className="rounded-[5px] border border-dashed border-faint/70 px-1.5 text-[11px] leading-[18px] whitespace-nowrap text-muted-foreground">not ticketed yet</span>}
              </button>
            </PopoverTrigger>
            <PopoverContent align="start" collisionPadding={12} className={POP}><Details m={m} i={f} /></PopoverContent>
          </Popover>
        </li>
      ))}
    </ul>
  )
}
// Status counts that head a feature list; the marks match the grid legend.
export function FeatureCounts({ m, feats }: { m: Model; feats: Issue[] }) {
  const n = (k: Kind) => feats.filter((f) => m.featureState(f).kind === k).length
  return (
    <span className="flex flex-wrap items-center gap-x-3 gap-y-0.5">
      {(['you', 'run', 'blk', 'ready'] as Kind[]).filter((k) => n(k)).map((k) => <span key={k} className="inline-flex items-center gap-1.5"><Mark kind={k} className="size-3" />{n(k)} {KIND_LABEL[k]}</span>)}
    </span>
  )
}

// A clickable ticket row: status mark, title, a note on the right; opens the ticket's popover.
function TicketRow({ m, i, note }: { m: Model; i: Issue; note: string }) {
  const k = m.kind(i)
  return (
    <li>
      <Popover>
        <PopoverTrigger asChild>
          <button type="button" aria-label={`#${i.n} ${i.t}, ${KIND_LABEL[k]}, ${note}`}
            className="grid w-full grid-cols-[14px_minmax(0,1fr)_auto] items-center gap-x-2.5 rounded-md px-1.5 py-[5px] text-left hover:bg-accent/70 data-[state=open]:bg-accent">
            <Mark kind={k} />
            <span className="truncate text-[13px] font-medium" title={i.t}>{m.label(i)}</span>
            <span className="max-w-[min(45vw,12rem)] truncate text-xs text-muted-foreground" title={note}>{note}</span>
          </button>
        </PopoverTrigger>
        <PopoverContent align="start" side="left" collisionPadding={12} className={POP}><Details m={m} i={i} /></PopoverContent>
      </Popover>
    </li>
  )
}
function SideList({ title, items, empty, children }: { title: string; items: number; empty: string; children: ReactNode }) {
  return (
    <section className="flex min-h-0 flex-1 flex-col gap-1.5 max-xl:max-h-80">
      <h3 className="flex items-baseline justify-between text-[13px] font-bold">{title}<span className="text-xs font-normal text-muted-foreground">{items}</span></h3>
      {items ? <ul className="-mx-1.5 min-h-0 flex-1 overflow-y-auto pr-1">{children}</ul> : <Empty>{empty}</Empty>}
    </section>
  )
}
// Ready now and Blocked beside the grid; the column never grows taller than the grid, each list scrolls inside.
export function GridSide({ m }: { m: Model }) {
  return (
    <div className="relative min-w-0 max-xl:border-t max-xl:pt-4 xl:border-l">
      <div className="flex flex-col gap-4 xl:absolute xl:inset-0 xl:pl-5">
        <SideList title="Ready now" items={m.readyTickets.length} empty="Nothing is ready to start.">
          {m.readyTickets.map((i) => <TicketRow key={i.n} m={m} i={i} note={m.holds(i.n) ? `unblocks ${m.holds(i.n)}` : 'unblocks none'} />)}
        </SideList>
        <SideList title="Blocked" items={m.blockedTickets.length} empty="Nothing is blocked.">
          {m.blockedTickets.map((i) => <TicketRow key={i.n} m={m} i={i} note={i.holdReason ?? 'readiness unavailable'} />)}
        </SideList>
      </div>
    </div>
  )
}

// Features x waves: one square per open ticket, done counts per wave.
export function RoadmapGrid({ m, groupByMilestone, large }: { m: Model; groupByMilestone?: boolean; large?: boolean }) {
  const waves = Array.from({ length: m.maxWave }, (_, k) => k + 1)
  const groups = groupByMilestone
    ? m.milestones.map((ms) => ({ name: ms.name, rows: m.gridRows.filter((r) => r.ms === ms.name) })).filter((g) => g.rows.length)
    : [{ name: '', rows: m.gridRows }]
  if (!m.gridRows.length) return <Empty>No feature is split into tickets yet.</Empty>
  return (
    <div className="overflow-x-auto max-md:[mask-image:linear-gradient(to_right,black_88%,transparent)]">
      <table className="w-full border-collapse text-[13px]">
        <thead><tr className="border-b text-left text-[11.5px] text-muted-foreground">
          <th className="sticky left-0 z-10 bg-card py-1.5 pr-3 font-medium whitespace-nowrap">Feature · wave →</th>
          {waves.map((w) => <th key={w} className="px-2 py-1.5 font-medium">{w}</th>)}
        </tr></thead>
        {groups.map((g) => (
          <tbody key={g.name}>
            {g.name && <tr><td colSpan={waves.length + 1} className="pt-4 pb-1 text-[11px] font-semibold tracking-[0.04em] text-muted-foreground uppercase">{g.name}</td></tr>}
            {g.rows.map((r) => (
              <tr key={r.f.n} className="border-b last:border-b-0">
                <td className="sticky left-0 z-10 max-w-[220px] bg-card py-1.5 pr-3 max-md:max-w-[132px]"><IssueLink i={r.f} className="block truncate font-semibold">{r.f.t}</IssueLink></td>
                {waves.map((w) => {
                  const c = r.cells.get(w)
                  return (
                    <td key={w} className="px-1.5 py-1 align-middle whitespace-nowrap">
                      {c && <span className="inline-flex items-center gap-0.5">
                        {c.done > 0 && <span className="mr-0.5 text-xs text-done">{c.done}✓</span>}
                        {c.open.map((i) => <Square key={i.n} m={m} i={i} size={large ? 'lg' : 'md'} />)}
                      </span>}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        ))}
      </table>
    </div>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="py-1.5 text-[13px] text-muted-foreground">{children}</div>
}
