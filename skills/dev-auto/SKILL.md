---
name: dev-auto
description: >-
  Use this skill when the user types /dev-auto to run as the orchestrator over a repo's Concord
  tickets: take ready tickets, choose each one's tier and agents, launch workers through the
  supervisor script and act only on what that script delivers. Not for writing tickets (tickets),
  running a coder and reviewer pair (duo) or shipping a change (ship).
disable-model-invocation: true
---

# dev-auto

You make the judgement calls; the supervisor script does everything mechanical. Below, `S` means
`python3 <this skill's folder>/scripts/dev_auto.py`. Every `S` command except `ledger-init` reads the
ledger at `$CONCORD_REPO_ROOT`.

## Start

1. Ledger: when `~/.concord-ledger/<repo>` is missing, run `S ledger-init <repo>`, export the
   `CONCORD_REPO_ROOT` it prints. For each remote host, add the `from="…"` key line it prints to this
   machine's `~/.ssh/authorized_keys` and run the printed host-key command once on that host; a new
   host needs an `address` in the `[hosts]` table of `accounts.toml`. Enable a host's rows in
   `accounts.toml` only after checking on that host that the login exists. Before the first Grok
   launch on this machine, run `~/.agents/platforms/grok/install.sh` once; Grok runs on this machine
   only.
2. Run `S set-orchestrator "$ORCA_TERMINAL_HANDLE" --account <your account id in accounts.toml>`,
   adding `--host <host>` when you are not on this machine. Run it from your own Claude Code: it
   records your session (`CLAUDE_CODE_SESSION_ID`, or `--session <id>`), whose transcript alone
   gives your context. Use `--new-cycle` only when the operator starts a new cycle after `STOPPED`.
   After an Orca restart, run it again: the restart gives your terminal a new handle.
3. When no tick loop runs for this ledger (check `orca terminal list`), start one in its own Orca
   terminal:

   ```sh
   export CONCORD_REPO_ROOT=~/.concord-ledger/<repo>
   while true; do python3 <this skill's folder>/scripts/dev_auto.py tick; sleep 30; done
   ```

A new orchestrator does steps 2 and 3 only, then reads the ledger (`S features` and the open
tickets' latest updates) and tells no worker.

## Loop

1. Use `S features --repo <repo>` for an inventory with each feature's spec file and one line per
   open ticket. For readiness, run `roadmap --text` inside the repo or inspect the ticket's blockers
   directly. Ready means status `proposed`, no owner, and every ticket on the
   `Blocked by:` line in its notes (`inspect_work`) complete or closed. Work the frontier first. With
   auto-fill on (see Auto-fill), the tick starts them up to each host's target and you take the
   rest.
2. For each one:
   - Its owned paths must not overlap a live ticket's; overlapping work waits. A ticket that changes
     a schema, data format or live write path merges before any ticket that builds on it launches.
   - It must stand alone, because its worker starts without your conversation: outcome, stop point,
     owned paths, checks, what not to touch, acceptance and `Skills:`. Add what is missing as a new
     update before launching.
   - Choose agent, model and effort from its `Coder`, `Reviewer` and `Tier` lines, using the roles
     in your tool's own instruction file.
   - `S slot --agent <agent> --model <model> [--host <host>] [--account <id or label>]`. On `no capacity`, leave the ticket for
     the next pass.
   - `slot` and `launch` take `--account <id or label>` and still refuse that row when it is disabled, full, or over its usage rules.
   - An account row's caps bound `slot`: `max_workers` is its total on any model; on a Claude row,
     `opus` and `sonnet` cap each model, and with `max_workers` set the two may run together. Caps
     count live records only, on every host that shares the row's id: a record that is done,
     stopped, lost, held after exits, or exited with no slot to relaunch in holds no slot. A
     `[hosts.<name>]` table's `max_workers` caps the live records of every agent kind on that
     host; `slot` refuses the host once it is reached, so launch, relaunch and usage moves all
     honour it, and stderr names it. Usage
     rules are checked first; `usage_grant = true` on a row with `max_workers` lets it take workers
     past `usage_full_percent` and the Pro Opus cap up to that total, never on a row without it.
     stderr names each passed-over row and its limit, and any grant used.
   - Weekly use: a row's `weekly_reset_by_operator` (default true on Claude rows, false on the
     others) says the operator resets its weekly window. Such a row is never blocked by weekly use.
     Any other row takes no new coder or reviewer, `usage_grant` or not, while its weekly window
     is at 100 percent, or while a limit screen stored for its account (a ticket's
     `limit_reset`, finished or not) has not reset, whatever the reading says, until a fresh
     reading below the limit follows a full one on that wait (the rule that frees its waiting
     worker); stderr names it with its weekly percent and the reset. A stored screen blocks whatever its window, a 5-hour
     one too: no worker starts on an account and stops at once. The 5-hour rule on readings is
     unchanged. Unread usage alone blocks nothing.
   - Launch rules (accounts.toml top-level keys, each off while unset, as the file ships).
     `usage_reset_minutes` (40 when set) turns on
     the 5-hour stop: a 5-hour window at `usage_full_percent` takes no new worker unless it resets
     within that many minutes, and the refusal names the percent and the reset, or that the window
     has no reset time. `max_hard_tickets` (2) and `max_medium_tickets` (3, with `ui` and `easy`
     counted as medium) cap the live coder tickets of every agent kind by their `Tier:` line;
     review tickets, finished tickets, records lost, stopped, held or exited, and tickets you
     run yourself do not count. `launch` refuses a
     ticket over its cap before any terminal is created, naming the tickets counted, and refuses a
     coder ticket without a `Tier:` line while a cap is set. On the operator's word, `--over-cap`
     launches past the cap and records one finding on the ticket saying so. Never pick accounts or
     count workers by hand instead.
   - Host memory: before `slot` offers a row, it reads the host once with `orca diagnostics
     memory`; `launch`, and the tick's relaunch of an exited worker and its move after a usage
     limit, all take rows through `slot`, so none starts a worker on a host short of memory. A
     Linux host needs more available memory than `memory_floor_gb` (top-level key, 8 when
     unset; a `[hosts.<name>]` table may set its own). A WSL host whose table sets `windows =
     "<Orca environment>"` is also read on that Windows side, which needs more free memory than
     `windows_floor_gb` (4; the host's table may override it). Under a floor, `slot` prints
     `no capacity` with `<host>: 6.5 GB memory available, under its floor of 8 GB; no worker
     starts there` on stderr and `launch` exits with that line; an unread host reading (failed,
     or none in 30 seconds) refuses the same way, naming the failure. An unread Windows side
     leaves the Linux reading to decide, and the output says so. A host that is not Linux, such
     as Mac2, is not checked: Orca reads `/proc/meminfo` on Linux and memory pressure on macOS,
     and when either fails it gives a free-memory reading on both, so then the host's own
     `uname -s` (run in one of its Orca worktrees) decides; a fallback with no answer is
     unread. A local machine that is not Linux is not read. A floor of 0 turns that check off.
     A refusal is read once more 60 seconds later and stands only when that reading refuses
     too; for 60 seconds after a refusal the host is refused without another read, so a batch
     waits once. A host refused under a floor takes workers again only 1 GB above it, and the
     refusal names both figures. Each tick also reads the Windows side of every WSL host with
     live workers and keeps its free-memory trend (`<ledger>/supervisor/memory.json`): under
     `windows_floor_gb` it is one `STUCK <host>: its Windows side <side> has <n> GB memory
     free, under its floor of <f> GB, trend <a> then <b> GB. Its <n> workers keep running and
     no worker starts there` per stretch under the floor; close workers there before the VM
     runs out. The host-down `STUCK` ends with that trend. Never check memory by hand instead.
   - Heavy checks: the worker brief says to run no typecheck, lint, unit, architecture or
     whole-suite tests on the host (GitHub Actions runs those), and only the named integration
     test files the ticket needs, one file at a time, each through
     `~/.agents/skills/dev-auto/scripts/host-check-lock <command>`, which holds the host's one
     check lock (a flock in `~/.cache/dev-auto`) so a second check waits for the first.
   - API overflow: when `slot --agent claude` says `no capacity` on a hard ticket, try
     `S slot --agent sdk --model <model>` and launch with `--agent sdk`. An `agent = "sdk"` row
     runs Claude through the Agent SDK (`scripts/sdk_worker.py`, started with `uv run --with
     claude-agent-sdk`, so the ledger's host needs `uv`), billed to the Console API key in its
     `key_file`. `slot` gives it only when no enabled Claude row on the host can take the model,
     or when `--account` names it, and never once the month's spend reaches `monthly_usd`.
     - A ticket that ran on an sdk row stays on it: a relaunch reuses its key and skips the
       overflow rule, and no launch moves it to another key mid-ticket, which would start its
       prompt cache cold.
     - It runs on the ledger's host only, with a Claude worker's user and project settings,
       skills, hooks and Concord server, and no other MCP server. It takes the brief and typed
       wakes, and exits at once unless the session bills `ANTHROPIC_API_KEY`. The key is never
       on a command line or screen.
     - Claude Code's subprocess env scrub keeps the key out of Bash, hooks and MCP servers. The
       scrub holds the session in default permission mode and runs Bash in Claude Code's
       sandbox, so on Linux the host needs bubblewrap and socat. The worker's tools are allowed
       by name (`TOOLS` in the runner); a tool that asks for permission otherwise is refused.
     - Prompt caching is Claude Code's own; the runner asks for the 1-hour TTL for the main
       loop and subagents, since wakes come more than 5 minutes apart, and keeps the cached
       prefix byte-stable. Each turn prints a numbers-only cache row: hit rate, tokens read,
       written, uncached and output.
     - Spend: the runner reserves its allowance in `<ledger>/supervisor/sdk-spend.json` before its first request, records
       readings that only grow, and releases the reservation only after a complete reading
       with no turn open, or at once when the session fails before its first request. Any
       other ending (a kill, a crash, no result, a zeroed result, an unpriced request, an
       unreadable transcript) leaves it open as a hold, which `slot` counts in full and the
       ticket's next launch refuses until `settle`.
     - Cleanup of a finished sdk ticket never closes a live runner, since the close's SIGHUP
       ends the session with no result, which is a hold. An idle runner is sent Ctrl-U then
       Ctrl-D (end of input, which releases on proof), and its tab closes once its terminal
       exited or shows its shell. A busy runner, or one on a screen Orca could not read, is
       left for a later tick. A tick waits for
       one runner at most, for up to 20 seconds.
     - A reading prices every request in the session's and its subagents' transcripts on its
       own from the published table of Claude 5 models (cache writes by their TTL, reads at
       the read rate, 10 USD per 1000 web searches, 1.1 times for `inference_geo` "us",
       Haiku 5.5's higher rates for a prompt over 100k tokens), and what the result's
       `model_usage` counts beyond them at the highest rates that could apply; the CLI's
       estimate is never spend. Readings come at every result, every 60 seconds inside a
       turn and on every exit. Each lands in its UTC month and the ticket, and the cost row
       names the share priced at upper bounds. Fast mode, background tasks and MCP tasks
       are kept off, so nothing finishes and starts the model while the runner is idle. A typed `/clear`, `/reset` or `/new` is not forwarded.
     - At 80 percent of the row's `ticket_usd` the worker gets one message to finish its step
       and write its handoff; at the cap, or at the month's cap, measured on recorded spend,
       the turn is interrupted and the runner exits under its fixed budget row.
   - Usage comes from this Mac's `orca account list` first, then, for a row on this Mac, its CLI's own file, which is parsed again only when it changes, so every tick reads usage cheaply. A row on this Mac whose `id` Orca does not read takes the reading of the Orca login whose home is the row's `home`, or whose id names a folder of that home. A reading older than `usage_max_age_minutes` (default 5), or with every window reset, is unread, never free, and so is a 5-hour window the reading lacks; a Claude Pro account takes no Opus worker while its 5-hour use is unread or above `hard_pro_full_percent`. The relay and a worker's limit screen still read every window.
   - Create its worktree from the project's base with `orca worktree create --name <lane>
     --repo <ticket repo> --base-branch feature/<feature>
     --branch task/<feature>/<lane>` (no `--agent`; add `--environment <host>` for another host).
     Run `S launch` within a minute of `orca worktree create`, before anything else is opened in the
     worktree. `launch` records that the ticket owns the worktree when its branch is
     `task/<feature>/<lane>` or the one its brief's `Branch:` field names (at a line start, or
     after that line's leading `Field: value ·` fields, never in prose); a review ticket's are its
     coder ticket's; a detached HEAD never. It records that branch and its repository and owns
     only them: cleanup and the sweep remove the worktree and branch only while that branch of
     that repository is checked out there, and a record or remembered worktree from before
     launches recorded them is kept. In any other worktree, or
     one whose branch could not be read,
     cleanup of the finished ticket closes only its terminal and drops its record; the worktree, its
     branch and every other terminal stay.
   - `S launch <ticket> --agent <agent> [--model <model>] [--effort <effort>] --worktree <path>
     [--host <host>] [--account <id or label>] [--over-cap] [--override-route]`.
     Launch takes omitted model and effort from the selected kind on the ticket. Supply
     `--model` when the ticket names none. Use `--override-route` on the operator's word
     to change the named model, effort or coder kind.
   - A launch whose worktree is gone, no folder on this Mac or no Orca record of it on a remote host, is one `STUCK` saying `worktree missing` with the path and the host, and nothing is launched. A remote launch runs the `ssh -n` ledger self-check `ledger-init` prints (`concord-mcp script:<prefix>-selfcheck` with closed stdin). Every launch, remote or local, checks each `Skills:` name has a `SKILL.md` on the host in `~/.agents/skills`, the worktree's `.agents/skills` or its `.claude/skills`; on failure one `STUCK` names what is missing and where it looked, and nothing is launched.
   - Every worker's `concord-mcp` starts with the ledger root, its agent id and its kind. New
     ids use the Concord ticket ID, with the host prefix for remote workers. Relaunch on
     the same host and kind keeps the stored id, or the matching legacy claim if the
     worker record has been dropped. Grok runs with `--no-leader`, because a
     shared Grok leader process would start the server with the leader's own env. A start
     whose server would lack them exits before its terminal is created. That covers a
     ledger root that is not an absolute path, and a kind other than Claude, Codex or
     Cursor on a remote host, since the ssh gate accepts only those.
   - Before any worker start (launch, relaunch or a usage-limit move), dev-auto updates that agent's CLI on the host (npm global, with `sudo -n` when the prefix is root-owned, else the tool's own `update`) and checks the host's running Orca (`orca status`) against the latest release, each at most once a day per host. A headless AppImage Orca behind it is replaced and restarted only with no agent terminal on the host. Orca desktop behind it is a `STUCK`, since it installs on restart. A change is noted on the ticket; a failure is one `STUCK <ticket>: <tool> update on <host> failed: …` and the start goes on.
   - Reviewer, per `duo`: for every ticket, create the review ticket `<ticket>-review` with
     `S review-ticket <ticket> --worktree <coder's worktree> [--host <host>]`. It publishes it
     unowned, titled `<feature> / <lane>-review`, as `tickets` publishes one (`start_work`, then
     `transfer_work` release), with notes naming the coder's ticket, its worktree and host, the
     coder's `Reviewer` line as its `Coder` line, and `Skills: duo, review` (plus `interrogate` on
     hard work and `ui-review` on ui work); it refuses a ticket with no `Reviewer` line naming an
     agent kind, or one whose review ticket exists. Slot and launch the reviewer on
     it in the coder's worktree on its host, using a kind the coder ticket's `Reviewer` line names.
     Launch checks the current coder's placement, or the review brief's placement when the
     coder has no record. A kind after `never` is excluded. A kind written as a word in
     parentheses is allowed, and a model id such as `grok-4.7-xhigh` names no kind.
     `tick` wakes that reviewer once for each coder review request. Two forms count, on any line of
     the update: a line starting `REVIEW READY <sha>`, and a line starting `Waiting on: review` that
     names exactly one full or short SHA, `REVIEW READY` or not. A digit-only run id, date or
     pull-request number is not a SHA. Both forms for one SHA are one wake. The words
     mid-sentence elsewhere, `REVIEW READY` with no SHA as its next word or a lookalike such as
     `REVIEW READYING`, a wait on anything but review, no SHA or two in a wait line, a superseded
     update, or an update by anyone but the coder, wake nobody. A superseded verdict answers no
     request, and a wait naming a SHA is answered only by a live verdict naming it. A request whose review ticket is open while
     no live record runs its reviewer (none launched, or its terminal gone) is one `STUCK
     <ticket>: review requested, no reviewer running` per request, all requests for one SHA since its last verdict being one: launch the reviewer. A review
     ticket claimed by an id no record holds, such as yours, raises none. A review ticket's
     `launch` on a host other than its coder record's is refused with one line naming both
     hosts: a coder and its reviewer run on the same host. Otherwise a reviewer waiting on its coder's
     ticket is woken only by an update there that names its review ticket, and once when that
     ticket is done, wait note or not; a reviewer whose latest verdict is newer than every review
     request gets no silence nudge.
3. End your turn. The tick appends each event to `<ledger>/supervisor/events.md`, one line
   `- <UTC yyyy-mm-ddThh:mmZ> <event text>` (a repeat of a line still open ends in ` (2)`, ` (3)` …), and after a tick that appended any it types one line into your
   terminal: `EVENTS: <n> new in <path>, <m> open`, plus `, <k> older than 30 min` once for each
   line left open that long. Read the file, act on each event, then remove its line with
   `S event-done '<line>' …` (each line as the file shows it), so the file stays short. Never edit
   the file yourself: event-done rereads it under the tick's lock, so a line appended since you
   read it stays. dev-auto appends (creating the file when missing) and removes only what
   event-done names; an event it has delivered is never appended again, and one whose append a
   kill or a full disk cut short is appended by the next tick. RELAY and STOPPED are also typed in full.
   A line typed into your terminal counts only once your screen shows it: an EVENTS wake or RELAY
   it does not show keeps its count, and the pointer, and is read for again each tick and typed at
   most twice; nothing is typed while your screen shows a turn. Each EVENTS wake names its batch (`batch 3`), a new number for each new snapshot, saved before its keys are typed, so no earlier wake confirms it. STOPPED is typed on the first tick
   your prompt can take it, and no EVENTS wake counts it.
   Then return to step 1, because a merge or a freed slot can make more tickets ready.

## Roadmap page

`roadmap`, run inside the repo with no arguments, writes `<ledger>/roadmap.html`, opens it and keeps
rewriting it every 15 s in the background (`roadmap stop` ends that); the page reloads itself every
15 s. It shows a dark single view: the render and last-tick times, one line of feature counts (in
the repo's `base` (default `staging`), merging, remaining), the tickets being worked on with each agent's role, agent, model,
account, live state and how long (lost agents apart and not counted), the top blockers, the tickets
ready to start, and the features-by-wave grid with one square per open ticket and each feature's PR.

It reads the ledger, `supervisor/status.json` for every agent row, `supervisor/state.json` for
`tick` (and for the workers only on a ledger whose tick has written no `status.json` yet), the repo's
`.work/specs/*.md` and the gh pull request list, cached for 60 s in `<ledger>/roadmap-prs.json`.
`roadmap.py` puts that data as JSON into the prebuilt page `scripts/roadmap-ui/dist/index.html`;
after changing `roadmap-ui/src`, rebuild it with `npm ci && npx vite build` in `roadmap-ui` and
commit `dist/index.html`.

## Status and context limits

- Each tick writes `<ledger>/supervisor/status.json` and renders `supervisor/status.md` from it,
  each atomically. One row per record (coder or reviewer) and one for the orchestrator: role,
  agent kind, model, effort, account label, host, state (the record's status, or `lost`), minutes
  since launch, context in thousands of tokens for Claude sessions, and the ticket's phase. The
  roadmap's agent rows read that same file, so a ticket's row there and in status.md agree;
  `roadmap.py` keeps the ledger reading (ticket state, blockers, plan) and computes no agent
  state, phase or context of its own while `status.json` exists. Read `status.md` for who runs
  where.
- Context: a Claude session's size is the last assistant entry's input plus cache-read plus
  cache-creation tokens in its transcript (`<home>/projects/*/<session>.jsonl`; for the
  orchestrator, the session set-orchestrator recorded, and none read, so no context RELAY, without
  one), read on its host once per
  tick on this Mac and at most every 5 minutes on a remote host (each read opens one probe
  terminal there).
- A coder or reviewer at 300k: once its `Handoff:` update of this launch is on the ticket and its
  terminal has been quiet a minute or is gone, the session is closed with its tab and a fresh one
  starts on the same ticket, worktree, model, effort and host, on its own account first, else
  through `slot`. Its brief names the handoff to read; it is never `--resume`. One `RESTARTED
  <ticket> <role>: context <n>k, handoff #<id>` event follows. With no handoff
  `handoff_wait_minutes` after the limit was first read, the handoff `/goal` is typed once. With
  no slot it is one `NO CAPACITY` event and waits. A worker whose terminal is already gone after
  that handoff is restarted the same way, never marked lost, unless a terminal no record holds
  in its worktree may still be that session (its agent listed, or none listed and no proof it
  exited): that is one `STUCK` and no restart or stop while it stays, and the record is not
  marked lost, so the restart, or with `limit_handoff` on the stop and its proposal, follows
  once that terminal exits. A close Orca refuses starts nothing;
  the record keeps its terminal and the close is tried again next tick. With `limit_handoff` on,
  such a worker is stopped with a proposal instead (Limit handoff, below).
- The orchestrator at 400k is typed `RELAY: context <n>k. …` once per terminal, also written to
  events.md: write your handoff and start your replacement, which runs `set-orchestrator`.
  dev-auto never closes the orchestrator.

## Limit handoff

Off unless `limit_handoff = true` in accounts.toml; off, the tick does exactly what it did before.
On, for Claude workers only:

- Trigger: a Claude account's 5-hour use reaches `handoff_percent` (85) with more than
  `handoff_minutes_left` (60) to its reset. One worker on it goes on: the one named by
  `S keep <ticket>`, else the highest `Tier:` (hard, medium, ui, easy), then the earliest launched.
  Every other worker on that account that had output in the last 60 minutes, is at its prompt or
  working, and has no open `Waiting on:` note is typed one `/goal` once per usage window: finish
  the step, write the handoff with the handoff skill, stop. Waiting and silent workers, and a
  reviewer whose latest verdict is newer than every review request, are left alone, and nothing
  more is typed into an asked one, not even a nudge. `slot` and `launch` place no new Claude
  worker on an account at the trigger, grant or not, and name it on stderr.
- Proof is the `Handoff:` update on the ticket, not the screen. Once it is there and the terminal
  has been quiet a minute, or has exited, the session is closed with its tab, the worktree kept,
  and a proposal made. A worker that hands off at the context limit is stopped the same way. A
  `Handoff:` update superseded since counts as withdrawn; a stop retried after a refused close
  proposes from the handoff that stands then. One
  with no handoff after `handoff_wait_minutes` (15) is one `STUCK`.
- A Claude usage-limit screen is a stop with a proposal: the account is still held until its
  reset, but no session is moved, resumed or waited on. Codex, Cursor, Grok and sdk workers keep
  the usual limit handling.
- The tick never starts or restarts a worker for usage or a handoff; only an accepted proposal
  does. A stopped record holds no slot. Auto-fill (below) is the one way the tick starts work.

## Auto-fill

With `[fill] enabled = true` in accounts.toml the tick starts ready tickets itself; without it
you take them as Loop step 1 says.

- Targets: `local_target_tickets` (top level) for this Mac and `target_tickets` in a host's
  `[hosts.<name>]` table. A host with no target is never filled. Each tick counts the tickets in
  progress on each host: a record not done and not lost on a ticket not complete or closed, a
  coder and its review counted as one ticket.
- Below its target, a host that passes its memory check gets one ready ticket per tick, in
  ledger order: status `proposed`, no owner or assignment, every known ticket on its `Blocked by:` line
  complete or closed, a `Host:` line that allows the host (`main Mac` is this Mac, another host
  by its whole name, an absent `Host:` line allows any), owned paths that overlap no live ticket's, and under
  its tier cap. A `Host:` line naming an operator step or a `Coder:` line saying
  `claude (orchestrator)` is never filled; take those yourself.
- Model: fill takes the first kind the `Coder:` line names before any `never` that has a
  slot for its named model, and uses the named effort. For omitted values it uses
  `[fill.models]`. The `hard` and `ui` defaults are Claude Opus 5.5 medium and Claude
  Sonnet 5.5 medium. Those tier entries supply the kind when the line has none, and
  omitted values when the kind matches. The per-kind defaults are Claude Opus 5.5 medium,
  Codex GPT-6.1 Sol xhigh, Grok 4.7 xhigh on this Mac, and Cursor grok-4.7-xhigh.
  A ticket with no Tier line or no kind with a slot waits.
- Worktree: `orca [--environment <host>] worktree create --name <feature>.<lane> --repo <repo>
  --base-branch feature/<feature> --branch task/<feature>/<lane> --no-parent`, or with
  `Feature branch: none` the branch and base its ``Branch: `x` … from `base` `` line names. The
  repo is `[fill] repo`, or the host table's `fill_repo`; with neither, nothing is filled.
- Fill rechecks that a ticket is proposed and unowned before making its worktree and
  before starting a terminal. Each start appends
  `FILLED <ticket> on <host>: <agent> <model> <effort> in <worktree>` to events.md; a failed
  worktree or launch is one `STUCK <ticket>: auto-fill on <host> failed: …` and the ticket is not
  tried again by auto-fill: fix it and launch it yourself.
- Reviewers: for a coder record with an open review request, fill uses the first kind on
  its `Reviewer:` line with a slot for the named model on the coder's host. The Claude
  fallback for omitted values is `[fill.models] reviewer`, Sonnet 5.5 medium by default.
  Fill checks memory before publishing the review ticket. If capacity changes during the
  launch, fill retries its proposed, unowned review ticket on a later tick. A start in
  the coder's worktree adds one `FILLED <ticket>-review …` event.
- `Reviewer: none`, fill or not: each open review request raises one `REVIEW <ticket> <sha>:
  orchestrator review` event instead of a no-review-ticket `STUCK`. Review it yourself and post
  the verdict.

## Events

Each event below arrives as a line of `events.md` (Loop step 3); act on it, then remove the line with `S event-done`.

- `STUCK <ticket> …`: read the ticket's latest updates and the screen recorded there (`S screen
  <ticket> [--lines <n>]` prints the record's terminal tail on its host). Then answer the
  worker in one message, add a corrected brief as a new update, relaunch it on another agent with
  `S launch` (it refuses while the ticket's old terminal may still hold an agent; close that terminal
  first to replace the worker), or split the ticket. A launch on another host or agent runs under a new
  agent id: when the ledger's owner is one of the ticket's own ids (any agent kind or host, or its
  stored record's id), dev-auto first releases that claim, as that id, at the version in the ledger,
  so the new worker can claim it. This holds for a reopened ticket whose record cleanup dropped. An
  unowned ticket needs no release and a ticket another agent owns is left alone. Before releasing,
  dev-auto looks for the earlier owner's session on its host, in the worktree it registered with
  Concord, the launch worktree and the stored record's worktree. A terminal there that is listed as
  that agent kind, or listed with no agent but not provably back at a shell, counts as live. The claim
  is kept and the launch starts nothing, raising one `STUCK` that names both ids and why, when that
  session may be live, when the ledger records no worktree for the owner, when a terminal list fails,
  or when Concord refuses the release (with Concord's message). Close that session or release the
  claim, then launch again. A STUCK from cleanup means the worktree is dirty, unmerged or holds other terminals:
  find where that work belongs and never delete unmerged work. Cleanup checks it again only when the
  ticket, the branch head or the worktree's terminals change, or after 15 minutes. With
  `merge_wait_hours` set in accounts.toml (0, the default, is off) the tick settles one cleanup
  case itself: a finished ticket whose unmerged head is the SHA of the latest unsuperseded verdict
  by someone other than its owner, a `REVIEW VERDICT: PUSH`, on the ticket or, for a review ticket,
  on its coder's ticket, raises no STUCK. Its worktree is kept with one `SETTLED <ticket>: … waits
  for its cleared PUSH <sha> to merge` line in `delivered.log`, nothing sent, and goes by the
  merged check once the pull request merges. Still unmerged, an open pull request included,
  `merge_wait_hours` later is one `STUCK <ticket>: … still unmerged <n> hours after its cleared
  PUSH <sha>`. A head no PUSH cleared, a later FIX, a dirty worktree and every other cleanup case
  stay today's STUCK.
- `NEEDS YOU <ticket> …`: read the update or screen it names; answer it yourself when the operator's standing
  decisions cover it; otherwise ask the operator once, plainly, with your recommendation. Send the
  answer to the worker as one message.
- `CLEARED REVIEW <ticket>: PUSH <sha>`: arrives once for each standing PUSH (see
  `verify-merges` below), even after the coder's terminal is gone or its ticket closed, and one
  withdrawn before it is sent is dropped; one held
  for 3 ticks also leaves a `STUCK` note naming it on the ticket. Accept on evidence, not on the report:
  the cleared SHA's diff meets the ticket's acceptance and the recorded checks really ran. When the
  project holds that merge for an operator step, arrange it and enable auto-merge afterwards;
  otherwise the coder merges the SHA into `feature/<feature>`. When a feature's tickets are all merged
  into it, run `S verify-merges feature/<feature> --worktree <checkout> [--host <host>]` in a checkout
  that has the branch, then push `feature/<feature>` and open the feature's one pull request.
  `verify-merges` fetches the branch, fails when that fetch fails, and exits 0 with `VERIFIED` only when every first-parent commit
  it has over the commit its fetch returned is a merge of exactly two parents whose second parent
  is the one commit, in that checkout, a SHA with a standing PUSH names (read as an object id,
  never a ref of that name; an ambiguous short SHA names none) on a ticket whose `Feature branch:` names it, with the merge's tree equal to the
  clean merge of its two parents, and the branch fast-forwards origin; otherwise it prints each
  commit's line and `FAILED`. A standing PUSH is a first line reading `REVIEW VERDICT: PUSH <sha>`
  from its first character, by someone other than the ticket's owner, not superseded and with no
  later unsuperseded `REVIEW VERDICT: FIX <sha>` naming that SHA on the same ticket. A feature
  branch origin does not have yet is compared with origin's tip of its repo's configured `base`;
  with no `base` set it fails. On a ticket whose notes say
  `Feature branch: none`, the PUSH wake and its goal, only for a standing PUSH on the SHA of the coder's latest
  review request, where both resolve, in the coder's worktree and as object ids, to the same
  one commit, and only while that very verdict and that very request stand (a later PUSH or request
  earns its own check), tell the coder to record the PR title and body
  and complete the ticket; push that SHA and open its pull request from that text.
- `UNPUSHED <ticket>: <branch> on <host> at <sha>, <n> commits ahead of origin` (or `, not on
  origin`): the ticket's feature branch, or with `Feature branch: none` its own branch, holds a
  merge of a SHA with a standing PUSH, or that SHA, that origin's copy of that branch lacks; with
  no origin copy, a head origin has only on another branch still counts, unless origin's default
  branch has it. It arrives once per head, through the same writer as
  `CLEARED REVIEW`, also after the ticket closed and its worktree went; a host that cannot be read
  raises nothing. Verify the head on that host (`S verify-merges` for a feature branch), then push
  the branch there and open or update its pull request. `S run <host> <worktree> '<command>'` runs
  a shell command in that worktree on that host, through an Orca probe terminal off this machine,
  prints its output and exits with its code (`--timeout`, default 90 seconds).
- Auto push (`auto_push = true`, top-level in `accounts.toml`; off as the file ships, and while off
  the tick pushes and opens nothing). Each tick, before `UNPUSHED`, every branch a reviewer cleared
  is delivered: push the head, open its pull request, and merge it once its checks are green. The
  branch comes from the notes as for `UNPUSHED`. A feature branch is pushed on each cleared merge,
  and its one pull request, into its repo's `base`, opens once every ticket naming that feature
  branch is complete or closed. Merged checks follow the repo's `base` (default `staging`). With
  `Feature branch: none`, the PR base is the one its notes name (`PR into development`,
  `pull request into dot-agents main`). Each repo needs its `[repos."<owner>/<name>"]` entry
  (from origin's URL); a repo without one is refused (`repo`). The entry sets `check` (the
  pre-push command; none runs no check), `base` (a feature branch's pull request base),
  `protected` (never a base; `["main"]` when unset, so dot-agents sets `protected = []`) and
  `migrations` (a regex on added paths, default `(^|/)migrations/`; paths are read raw and
  unquoted, as UTF-8, and a rename into the directory counts as an addition). The migration
  hold also covers additions and edits to HR Relay's `src/backend/storage/sqlite/migrations.ts`.
  A new pull request's title and body come from the newest update with a line starting
  `PR title: <title>`, anywhere in it, then a `PR body:` line and the body; an already open pull
  request needs none.
  Every guard must hold: a standing PUSH (as for `verify-merges`) whose SHA names one commit in the repo (a short SHA is resolved
  there); the head is that SHA or reaches it through clean `--no-ff` merges of cleared SHAs (or of
  commits already in the base), and a commit already on origin's branch counts as accepted, as
  `verify-merges` reads it, so only a commit after the clearance that origin lacks needs a new
  clearance; origin's branch is behind the head, never diverged, and the push is `git push origin
  <sha>:refs/heads/<branch>`, never forced; a base is named, exists on origin and is not
  `protected`, and the branch's open pull request, read before every push, has that base (two
  open pull requests on one branch refuse); the title and body are recorded for a new pull
  request (an open ticket without them waits); where the push runs, git pushes origin to exactly its
  fetch URL, character for character, and nowhere else (`remote`: a `pushurl`, even an alias of
  the same repo, refuses; so does a relative origin URL); that is checked again right before
  the push by a hash, and no push URL leaves its host or is printed; each `gh` call names that
  repo (`--repo host/owner/repo`), and an origin URL gh cannot name that way without dropping
  part of it, a port or a deeper path, or a local path, refuses (`remote`); a fork's pull request from a branch of the same name is not the branch's,
  and a list of 200 pull requests is unread history, which refuses (`pull-request`); a branch with no
  open pull request whose last one into its base was closed unmerged is held, with nothing pushed or opened, until a person reopens it or opens its replacement
  (`closed`).
  - Which host: every host with a worker record on a ticket naming the branch is read; the
    delivery runs on the one holding the newest head (an older copy, on another's first-parent
    chain, or proved by git to be in the base both read from one origin URL at one tip, is
    passed over; anything else is a conflict), and unrelated heads on two hosts refuse
    (`hosts`). While a ticket naming the branch is open it is read again after `RECHECK`, so a
    merge made after its PUSH, with no ledger update, still goes out.
  - Where it pushes: a branch checked out in a worktree is pushed from there, which must be
    clean; `check` runs there right before the push, and the head and a clean worktree are
    checked again after it. A branch checked out nowhere (a feature merged from the task
    worktree, or one whose worktree went) is pushed from the ref in any worktree of the repo on
    that host: when the head's tree equals a cleared SHA's no check runs, otherwise `check` runs in
    a throwaway detached worktree at the head, removed after.
  - A local branch behind origin's (a stale ref on a host) is read at origin's tip, and
    fast-forwarded to it when no worktree has it checked out: nothing to push, no refusal.
  - Merge: auto-merge is never turned on by a push. Once the pull request's check rollup at the
    pushed head is all passed (success, neutral or skipped), a pull request GitHub calls `CLEAN`
    is merged at once (`gh pr merge <n> --merge --match-head-commit <sha>`); otherwise
    auto-merge is turned on at that head (`--auto`). While a check runs, or none is listed yet,
    it waits and reads again next tick. A failed check is one `PR RED <ticket>: pull request <n>
    for <branch> at <sha>, check <names> failed` per head: wake the ticket's worker to fix it
    (`fix-ci`). An auto-merge an earlier head turned on is turned off (`gh pr merge <n>
    --disable-auto`) before a new head is pushed; while that fails, nothing is pushed.
  - An auto-merge marker saved before it named its pull request is dropped while GitHub shows
    auto-merge on for the open one, so the migration hold reads afresh; off, the branch refuses
    (`auto-merge`) until you turn it on or merge by hand.
  - Cleanup keeps a finished ticket's worktree while its delivery has a pushed head or pull
    request that has not merged, and removes it once the pull request merged or git proves the
    head is in the base; an empty chain without that proof is read again next tick.
  A failed guard is one `PUSH REFUSED <ticket>: <branch> at <sha> on <host>, <guard>: <detail>` per
  head and guard; a failed `git push`, `gh pr create` or `gh pr merge` is one event for that
  stage and is tried again each tick. The tick publishes the reviewed head and opens its pull request
  for CI. When the migration hold applies, the tick disables any existing auto-merge on first hold
  and sends one `NO AUTO-MERGE <ticket>: …` event per repository, pull request and head, even if
  a new ticket supplies the clearance. Merge the pull request yourself once CI passes. Every push,
  pull request, merge and auto-merge leaves one line in
  `supervisor/delivered.log`. The script never rewrites, rebases or deletes a remote branch.
  Still yours: accepting a `CLEARED REVIEW` on evidence, a clearance that a guard refused, a red
  check, a migration's merge, every pull request into a protected branch, and when to turn
  `auto_push` on.
- `STUCK` on a worker whose screen the supervisor does not recognise carries that screen text on the
  ticket: open the worker's terminal and decide.
- Codex wakes: `tick` reads the Codex status line before typing. A stalled or paused goal gets
  `/goal resume`, then the wake. An open menu or question takes no wake; Codex's switch-model menu is
  answered with Keep current model, never by an Enter. A Codex goal is met by an unanswered
  `Waiting on:` note, so a waiting worker marks its goal complete instead of repeating the note.
- `STUCK <ticket> …: its status line shows <model effort>, not the launched <model effort>`: the
  session switched model, and nothing is typed into it, a stop included; while `STOPPED` it arrives
  on the first tick of the new cycle. Reviewers run only on the model and effort
  they were launched with, so replace the worker yourself: close its terminal, then `S launch` it
  again on an account with room. A switch-model menu in wording `tick` does not know is the same
  kind of `STUCK`.
- A lost Concord server: a worker whose screen shows its last Concord call failing gets one repair
  from `tick`, once it is idle (no output for a minute, and no spinner or `esc to interrupt`) and
  off any limit screen. A Claude worker
  gets `/mcp reconnect concord` typed once; its goal had it write the handoff file and stop, so
  wake it after the reconnect. A Codex session showing `Transport closed` is relaunched once, after
  a note on its ticket telling it to read that ticket and the coder's first, and re-run only the
  checks of a SHA that has no verdict. A later Concord call that works, or a new update from the
  worker, clears the failure. A Claude worker that made no Concord call since the drop shows
  nothing, and is repaired only after its next call fails.
- `STUCK <ticket> …: its Concord tools fail on its screen …`: the repair did not hold, or there is
  none. The suffix `after /mcp reconnect concord` means the reconnect failed: reconnect `concord`
  from `/mcp`, or relaunch the worker. `again after its relaunch` means a Codex session lost it a
  second time. With no suffix, it is a Cursor or Grok worker, which gets no keystroke. Restart its
  server or relaunch it.
- `STUCK local: the Concord call of dev-auto itself to the ledger fails`: Concord fails on a worker,
  and the supervisor's own Concord call fails too. Nothing is typed or relaunched until that call
  works again. Check `concord-mcp` and the ledger on this machine.
- `STUCK <host>: its ssh self-check to the ledger fails`: Concord fails on a worker there and the
  host's own `ledger_ssh` self-check fails too. Nothing is typed or relaunched on that host. Restore
  the link.
- Both checks run before every repair. A failed check runs again every 5 minutes, and the repair
  follows once it passes.
- `STUCK <ticket> …: its runner did not exit 20s after end of input`: a finished sdk ticket's
  runner is still up, so its terminal stays; closing it would leave its spend held open. Read
  its screen. Cleanup closes the tab once the runner is gone, and sends it nothing more.
- `STUCK <ticket> …: its runner stopped: sdk worker: <row>`: an sdk worker exited; it is never
  relaunched. The row above it on the worker's screen gives the codes and counts. A runner that
  exited with no such row on its screen reads `exited with no stop line on its screen`.
  - `budget cap reached`: it spent the ticket's `ticket_usd` or the month's `monthly_usd`. Read
    its handoff, then continue the ticket on a subscription account, or ask the operator before
    raising a cap.
  - `API turn failed, exited`: an API error or a turn with no usage anywhere, such as a key or
    billing error (`api_error_status`, `error`, `terminal_reason`), or a session that failed
    to start before its first request (`session failed to start: <error type>`), which holds
    nothing. Fix the cause, then launch it again.
  - `unpriced usage, exited`: spend it could not establish: an unlisted model or tariff, a
    missing or unreadable transcript, a missing or zeroed `model_usage`, or a turn with no
    result. What could be priced is recorded and the rest of the allowance is held. Read what
    the Console billed for the ticket's key in each UTC month the hold touched, then run
    `python3 <this skill's folder>/scripts/sdk_worker.py settle --ticket <ticket> --spend
    <ledger>/supervisor/sdk-spend.json --usd <billed>`, or one `--month YYYY-MM=<billed>` per
    month when it touched several. settle refuses a live reservation, any other set of months,
    and a figure below the hold's exact share.
  - `key scrub not in force`: the session started in a permission mode other than default,
    which the scrub forces, so the key could reach its tools. Its query was sent, so its spend
    is held: settle it as above once the cause is fixed.
  - `not billed to the API key`, `key file missing or empty`, `spend file unreadable`, `ticket
    already running`: fix the key, the key file or the spend file, or close the ticket's other
    runner.
- A limit screen is acted on the tick it appears, and the worker moves at once to a free
  account of its kind on its host.
- `WAITING` (usage limit, no free account): type nothing into the worker; every tick `tick` moves
  it to another account of its kind the moment one has a free slot, and otherwise relaunches or
  resumes it at the reset, or sooner when a fresh reading of its account drops below the limit
  after a full one. When the operator says an account was reset, run `S usage-reset <id or label>`:
  its windows count as reset until a reading or a new limit screen newer than that shows the
  account full, so `slot` offers it and the next tick resumes its waiting workers. A limit
  screen showing only its dialog's footer (`Enter to confirm · Esc to cancel`) counts too:
  `tick` reads the Claude worker's transcript, on its host, and a usage limit it ends in,
  written by the time the frame was drawn, is handled as that limit screen. Claude Code's own
  `⏺ Usage limit reached ·` and `⏺ Goal paused · usage limit reached ·` lines under its limit
  line are part of that screen, and a screen that prints several resets waits for the latest
  dated one, else the latest time-only one (a weekly line's date over "continuing
  automatically at 4am", even after 4am has passed). A seen limit keeps
  its account from new workers, whatever its reading, until the reset, a usage-reset, or, once a
  reading has shown the account at its limit, a fresh reading below `usage_full_percent`.
- `PROPOSALS: <n> restart proposals wait in <ledger>/supervisor/proposals.md: <tickets>` (only
  with `limit_handoff = true`): each names a Claude worker dev-auto stopped, why (`usage handoff
  at N%`, `context handoff` or `usage limit reached`) with its handoff update id, the fresh
  `S launch … --account <label>` command, every Claude account on its host with 5-hour and weekly
  use, reset and free slots, and dev-auto's pick with its reason. Check the pick against the
  usage shown, then run `S proposals accept <ticket> [<ticket> …]` or `S proposals accept --all`,
  with `--account`, `--model`, `--effort` or `--host` to change it, or `S proposals drop
  <ticket>`; `S proposals` lists them. Accepting runs `launch` with the account checked again
  then, under one lock with its choice, its check and its end, so a drop, a tick or a newer
  proposal waits for it: a fresh session (never `--resume`) that reads the ticket's latest
  handoff. One whose worker is no longer the stopped one it restarts, or whose ticket is done
  when the launch reads it again, is not launched. A proposal
  that no longer fits prints `<ticket>: not launched: <why>` and stays. Each end is a
  `PROPOSAL accepted|changed|dropped` line in `delivered.log`; a proposal whose ticket finished or
  was launched another way is dropped by the tick. `RESET <account>: …` comes once when the
  account's 5-hour window has reset and lists every proposal that still waits.
- `STUCK <ticket> …: asked <n> minutes ago to write its handoff and stop …`: it is left running
  and nothing more is typed into it; read its screen and decide.
- `STUCK <ticket> …: a dialog shows only its footer …`: the transcript does not explain a settled
  footer-only frame, or cannot be read. Nothing is typed into it; read the worker's screen in Orca
  and answer or relaunch it yourself. Raised once per frame.
- `NO CAPACITY` (a reset or exit with no free slot): leave the ticket; `tick` relaunches it when a slot
  frees.
- `RELAY`: record your open decisions on the tickets they concern, start a fresh orchestrator
  session on an account with room with the prompt `/dev-auto`, and stop. The relay re-arms when
  the usage window that raised it resets, a week after it when the reading gave no reset; a
  second `relay_percent` reading inside that window is
  `STOPPED`.
- `CHECK-IN: <n> flagged, see <ledger>/supervisor/checkin.txt`: read the file; for each flagged
  ticket read its latest updates and act as for `STUCK` (answer, corrected brief, relaunch, or
  escalate per the roles). With 0 flagged, do nothing. The script sends it every `checkin_minutes`
  from `accounts.toml`.
- `STOPPED`: tell the operator and wait; only the operator starts a new cycle.

## Rules

- When the operator changes an account rule, edit that account's row in `accounts.toml` (its header
  explains the fields). The next tick reads it. A grant of workers on an account is its row's
  `max_workers`, `opus` and `sonnet`, plus `usage_grant` when the grant goes past its usage rules;
  never wrap `slot` to enforce one.
- Tickets come from the planner; take ready tickets from the ledger and write none. A ticket that turns out
  wrong gets a note on it for the planner; do not rewrite it.
- Message a worker only when it must act: an answer it waits for or a changed brief. Never
  broadcast, and never announce yourself.
- Never do by hand what `tick` does: usage checks, account swaps, wake-ups, nudges, stall handling,
  relaunches, and cleanup. A ticket's worktree, branch and terminal stay while the ticket is
  open, even with its branch already merged, and a worktree another record shares stays until
  every ticket in it is complete or closed: a record that owns it passes the ownership to the
  record that keeps it, so the last one out removes it. When a ticket is complete or closed, its own terminal closes and its
  record frees its slot at once (the terminal is its own when the record's handle is listed,
  whatever pty Orca now shows there, or when its launch ptyId is listed under a new handle); a terminal that already exited or was closed by hand is skipped,
  and its worktree gets the same checks. Its worktree and branch go once the branch is merged into
  the feature branch; a task branch with no commits of its own counts as merged. A detached
  checkout is never its launch's branch, so it is kept, untracked files and all. Its shells
  close first, and a refused close keeps it; then, just before removal, the worktree must
  still show the clean checkout (branch, head and repository) the check judged and a fresh
  listing no terminal there, else it is checked again next tick. A worktree already gone drops the record with no STUCK. A review ticket whose coder's
  ticket is complete with PUSH as the coder's latest verdict is done: its reviewer's terminal is
  closed with its tab whether it still runs or exited, never a fast-exit `STUCK`, and the review
  ticket is closed by `tick` through `finish_work` run as its owner, with one progress line saying why; a
  close Concord refuses is one `STUCK` naming the ticket, tried again each tick. With the coder's
  ticket still open, or no PUSH as the latest verdict, it stays the lost terminal's one `STUCK`.
  When cleanup drops a finished record it keeps a retired entry for 7 days (ticket, host,
  worktree, branch, agent, session, terminal handles and pty). Each tick, an agent pane Orca
  revived (after an app or WSL restart, or a tab restore) that no record or the orchestrator
  holds, and that a retired entry names by handle or launch ptyId, or by the same worktree,
  branch (as cleanup last saw the worktree: a pane there, or its merge check) and agent, is
  closed with its tab while that ticket is still complete or closed, with one line in the
  tick's output. The worktree match holds only in a worktree the record owned, and never for
  a pane (by handle or ptyId, in any worktree) that was listed in a retiring record's
  worktree on that host: each retirement there renews them all, so they are kept until 7
  days after the host's last retirement and an expiry never widens what an entry ties. Shells,
  panes no entry names, a reopened ticket's panes and a host whose terminals could not be
  listed are left alone.
- Every worktree a record owned is kept in `state.json` (`worktrees`) past its record. Each
  tick, one no record holds, whose ticket is complete or closed, that no open ticket's title
  or notes name by path or branch, that holds only shells at their prompt before and after the
  cleanup check (terminals listed again once it ends), and that the check finds clean and
  merged, on the branch its launch owned and unchanged since, and not the main checkout, is removed with its shells
  and its branch, with one line in the tick's output; a new pane, a busy shell or a refused
  shell close keeps it, as does anything else, checked again after 15 minutes. Only Orca's
  `selector_not_found` forgets one already gone; a lookup that fails keeps it.
- Each tick closes with its tab every plain shell (no agent) at its prompt in a recorded
  worktree with no output for 10 minutes that no record or the orchestrator holds; a dev-auto
  probe is never one.
- Every terminal dev-auto closes (a probe, a relaunched or moved session, a finished worker) goes
  with its tab (`orca terminal close --tab`), so no resume record of it is left for an Orca or WSL
  restart to bring back. A tab that also holds another terminal, or a host whose terminals could
  not be listed, loses only that pane, and the tick's output says so. A close Orca refuses is
  reported as before; a relaunch, a Concord repair or a usage move whose old terminal Orca refuses
  to close starts nothing, keeps the record and tries again next tick. A terminal create that
  gives no answer, a failed exit, or one without a handle, keeps its launch record, with no
  terminal (a launch first refuses, saving nothing, while its host cannot list terminals); a later
  `launch` or `resume` of that ticket refuses while a terminal no record holds is open in its
  worktree. With the record's own terminal gone, `launch` and `resume` also refuse while such a
  terminal may still hold its agent. A Claude launch leaves a profile's files as they are when
  another session may write them: another record on that account not proved gone (lost, or
  finished with its close refused), the orchestrator on it, a live session no record holds, or
  this Mac's default profile. On another host only missing profile files are written.
- A host whose Orca stops answering: one failed `terminal list` is only a line in the tick's
  output; a second in a row is one `STUCK <host>: its terminal list failed twice in a row, its <n>
  workers wait until it answers`, and none follows until the host has answered again. Meanwhile
  its workers get no lost, silence or wake event. When it answers with every open record's
  terminal gone (after a failed listing, or for two or more records in the same tick), that is an
  Orca restart: one `STUCK <host>: Orca restarted and lost <n> workers: <tickets>` instead of one
  per ticket, each left alone until a new launch, or for a Claude record until `S resume <ticket>`.
  `resume` starts the record's own session again (`--resume`) on its account and host and clears
  its lost mark; with the record's terminal still listed it first closes it with its tab, which
  restarts the session on an updated Claude Code, and it refuses while that screen shows a running
  turn or cannot be read, and keeps the record unchanged when that terminal does not close.
  Before closing anything it refuses a record stopped for a restart proposal (accept the
  proposal instead) and an account or host `slot` would not give the ticket now: disabled,
  held at its limit, its slots taken by other tickets, or the host short of memory. It takes Claude records only; replace any other with `S launch`. At that restart, and only then, dev-auto closes
  with its tab every agent terminal (one with an `agentIdentity`) on that host that sits in a
  recorded worktree and whose handle no record or the orchestrator holds: the sessions Orca
  restored, and every agent or bare shell that no record holds in a worktree of a finished or
  moved ticket that no open record owns. Other shells, other worktrees and recorded terminals
  stay; a close Orca refuses is named in the same `STUCK`. A single worker gone while the host's others are listed keeps its own `STUCK`.
- With `stuck_launch_command = true` in accounts.toml (off by default), a worker whose terminal
  is gone (`… Left alone until a new launch`) or exited too soon or too often (`… It waits for a
  new launch`) is one `STUCK` that ends with its filled launch command,
  `python3 <dev_auto.py> launch <ticket> --agent … --model … [--effort …] --worktree … --host …`.
  Run it or drop it: dev-auto never runs it. A command `send` could not type goes in the ticket
  note, and the STUCK says `Its launch command is on the ticket`.
- A launch saves its record with its account and launch time before the daily update, so one
  stopped there leaves a record with no terminal, reported as lost. A saved record without its
  account or launch time is one `STUCK <ticket>: its record has no <keys>. It is skipped until a
  new launch`; slot, check-in and tick skip it, and a launch of that ticket replaces it. With no
  launch time, a bare shell in its terminal is no proof of exit: the launch refuses until that
  terminal's process has ended or it is closed.
- Silence: a worker with no output for `silent_minutes` (30), or `idle_minutes` (10) while its
  screen is its agent's own prompt with no turn running, and no open `Waiting on:` note, gets
  one nudge, then one `STUCK` as long after it.
- `tick` wakes a waiting worker once: `ci` on the run id or URL its note names, else its task branch's PR, else its brief's `Feature branch:` PR; `process <pid>` once that pid is gone on the worker's host, while a probe that cannot read it counts as untrackable, probed again each tick; any other untrackable wait is one `STUCK`. A review wait is that `STUCK` when the ticket has no review ticket, or when its owner has no unanswered review request (a request with one SHA, as wakes a reviewer) and no verdict after the note. A later verdict, or an unanswered request a review ticket can receive, is unchanged. That `STUCK` is once per note, with no grace. A wake counts as answered only by an update from that worker or new terminal output; a wake answered by neither within about 3 minutes (`wake_minutes`) is one `STUCK` naming it, waiting or not, and is never typed again; an unanswered nudge stays silence's `STUCK`. A wake (or a handoff ask or STOP) counts as shown only when the screen after its Enter shows its own last 80 characters, spaces aside so a wrapped line counts, more times than the screen just before it did: an earlier copy of the same text never confirms a later one, and a text already typed into that worker launch, or any text to a worker launched before this rule for the rest of its launch, ends in its event (`Event verdict 2.`) so no copy of an earlier one can stand for it, and Orca's accumulated stream, an Unknown command screen or an unread screen before the send confirms nothing. Nothing is typed, not even the clearing Ctrl-U, unless the last screen read before the keys is a rendered running agent screen that shows the agent itself (its own idle screen, a running turn or its `❯`/`›` input line; an empty frame or an unknown prompt shows none, and a shell prompt on the bottom line, Starship's path line and bare glyph included, is the active input whatever sits above it; a status line ending in a percentage such as `opus@max: 87%` is no prompt); a send held so waits for a later tick. One the screen did not show is read again about 10 seconds later: shown then, or followed by output or an update from the worker, it is `rechecked`; otherwise it is typed once more, the same text and only into a rendered running agent screen (no bare shell, exited pane, footer-only dialog or accumulated stream), `retried` when that shows, and a second miss is one `STUCK` naming the wake, logged `missed`; a missed nudge is logged only, and a missed handoff ask no longer holds the worker; an ask not yet shown, or missed, starts no handoff timeout. A STOP not yet shown is rechecked on each stopped tick whose terminal list shows its worker's row. A tick waits for one recheck only, so a later unshown wake in it is logged `unconfirmed` and rechecked by the next tick. `delivered.log` carries each mark.
- After each tick, `<ledger>/supervisor/state.json` holds `tick = {at, seconds}`, when the tick
  ended and how long it took, and on each worker record it supervised `status = {state, detail,
  since, seen, output_at}`, epoch seconds: `state` is `working` (output within a minute), `quiet`,
  `waiting` (detail: the note's target, such as `review`, `ci`, `operator`, `ticket:<id>` or
  `process <pid>`; since: the note's time), `asking` (detail: `question`, `menu` or `first-run`),
  `limited` (detail: the reset), `nudged` (since: the nudge), `stuck` (detail: the STUCK text;
  it stands until the worker outputs or updates, or cleanup checks its worktree again), `lost`, `exited`, `no-agent` or `done` (detail: why the record
  is kept). `since` stays while state and detail stay the same; `seen` is the tick that saw it;
  `output_at` is the terminal's last output. A host that could not be listed, or a screen that
  could not be read, keeps its old status; a first unread screen gives `working` or `quiet` from
  the output time. Readers treat a `seen` older than three ticks as stale. Read it; never write it.
- Evidence goes when its feature closes.
- Workers never merge by hand or start other workers. A ticket that no allowed agent or account
  can continue stays recorded and waits; never broaden its scope, weaken its proof or enable paid
  overage to get it through.
- Behaviour disagreements between tickets are yours to settle, or the operator's when they change
  intent.
