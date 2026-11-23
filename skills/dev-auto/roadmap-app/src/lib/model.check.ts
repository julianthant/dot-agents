// Self-check for the model: node src/lib/model.check.ts
import assert from 'node:assert/strict'
import { build, people, type Issue } from './model.ts'

const I = (n: number, o: Partial<Issue> = {}): Issue => ({ n, t: `t${n}`, type: 'Task', open: true, done: false, ms: 'Staging cut', later: false, parent: 1, kids: 0, kidsDone: 0, blockers: [], deps: [], who: null, updated: '2026-11-22T00:00:00Z', status: 'Todo', agent: null, area: null, ...o })
const m = build({ areas: [], milestones: ['Staging cut', 'Mac sign-off'], pipeline: { devPrs: [{ n: 9, t: 'pr', head: 'h', auto: false, draft: false, checks: 'green', issues: [3], updated: '' }] }, issues: [
  I(1, { type: 'Feature', parent: null, kids: 4, kidsDone: 1 }),
  I(2, { open: false, done: true }),
  I(3, { status: 'In Progress', agent: 'Reviewing', deps: [2] }),
  I(4, { blockers: [3], deps: [3], ready: false, holdReason: 'blocked by #3' }),
  I(5, { agent: 'Needs you', ms: null }),
  I(6, { type: 'Feature', parent: null, ms: 'Mac sign-off', blockers: [4], deps: [4] }),
] })
assert.deepEqual(people('coder gpt-6.2-sol high @a · PR worker claude-sonnet-5-5 medium @b'), [{ role: 'coder', model: 'gpt-6.2-sol high', acct: 'a' }, { role: 'PR worker', model: 'claude-sonnet-5-5 medium', acct: 'b' }])
assert.equal(m.stageOf(m.by.get(3)!), 3, 'a linked dev PR puts the ticket at PR')
assert.equal(m.kind(m.by.get(4)!), 'blk')
assert.equal(m.msOf(m.by.get(5)!), 'Staging cut', 'milestone falls back to the parent')
const sc = m.milestones[0]
assert.deepEqual([sc.total, sc.done, sc.running, sc.you, sc.blocked, sc.ready], [4, 1, 1, 1, 1, 0])
assert.deepEqual(m.milestones[1].gates.map((g) => g.n), [4], 'a cross-milestone blocker is a gate')
assert.deepEqual([...m.gridRows[0].cells.keys()].sort(), [1, 2, 3], 'wave = 1 + dependency chain')
assert.deepEqual(m.unblocks.get(3)?.map((i) => i.n), [4])
assert.equal(m.featureState(m.by.get(1)!).kind, 'you', 'a feature needs you when a ticket does')
assert.deepEqual(m.featureState(m.by.get(6)!).waits.map((w) => w.n), [4], 'an unticketed feature waits on its own blockers')
assert.equal(m.inFlight.length + m.readyTickets.length + m.blockedTickets.length, m.overall.remaining, 'header counts split the open tickets')

const ready = build({ areas: [], pipeline: {}, issues: [
  I(7, { type: 'Bug', agent: 'Queued', ready: true, holdReason: null }),
  I(8, { ready: false, holdReason: 'no seat' }),
  I(9, { ready: false, holdReason: 'needs operator', kids: 1 }),
  I(10, { type: 'Feature', parent: null }),
] })
assert.deepEqual(ready.readyTickets.map(i => i.n), [7])
assert.deepEqual(ready.blockedTickets.map(i => i.holdReason), ['no seat', 'needs operator'])
assert.deepEqual(ready.inFlight, [])
assert.equal(ready.kind(ready.by.get(8)!), 'blk')
console.log('model ok')
