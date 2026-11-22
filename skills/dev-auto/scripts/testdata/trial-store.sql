-- tasks and task_updates copied read-only from the Concord 0.22.0 trial store.
CREATE TABLE "tasks" (
    task_id           TEXT NOT NULL,
    title             TEXT NOT NULL,
    owner             TEXT,
    agent             TEXT,
    branch            TEXT,
    worktree          TEXT,
    expected_files    TEXT NOT NULL DEFAULT '[]',
    modules           TEXT NOT NULL DEFAULT '[]',
    domains           TEXT NOT NULL DEFAULT '[]',
    risk_tags         TEXT NOT NULL DEFAULT '[]',
    notes             TEXT,
    status            TEXT NOT NULL DEFAULT 'active',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    parent_task_id    TEXT,
    agent_id          TEXT,
    version           INTEGER NOT NULL DEFAULT 1,
    assigned_agent_id TEXT,
    lease_expires_at  TEXT,
    project           TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (project, task_id)
  );
INSERT INTO "tasks" (task_id, title, owner, agent, branch, worktree, expected_files, modules, domains, risk_tags, notes, status, created_at, updated_at, parent_task_id, agent_id, version, assigned_agent_id, lease_expires_at, project) VALUES ('trial-lane', 'trial-feature / trial-lane', NULL, 'script', NULL, NULL, '["README.md"]', '[]', '[]', '[]', 'Brief: protocol trial task created by rpc.py.', 'review_ready', '2026-11-11T21:52:49.051Z', '2026-11-11T21:52:49.058Z', NULL, 'script:mac-trial-lane', 2, NULL, NULL, '');
INSERT INTO "tasks" (task_id, title, owner, agent, branch, worktree, expected_files, modules, domains, risk_tags, notes, status, created_at, updated_at, parent_task_id, agent_id, version, assigned_agent_id, lease_expires_at, project) VALUES ('claude-lane', 'trial-feature / claude-lane', NULL, 'claude-code', NULL, NULL, '[]', '[]', '[]', '[]', NULL, 'review_ready', '2026-11-11T21:53:42.107Z', '2026-11-11T21:55:44.965Z', NULL, 'claude:trial-claude-lane', 2, NULL, NULL, '');
INSERT INTO "tasks" (task_id, title, owner, agent, branch, worktree, expected_files, modules, domains, risk_tags, notes, status, created_at, updated_at, parent_task_id, agent_id, version, assigned_agent_id, lease_expires_at, project) VALUES ('claude-lane-review', 'trial-feature / claude-lane-review', NULL, 'codex', NULL, NULL, '[]', '[]', '[]', '[]', NULL, 'complete', '2026-11-11T21:55:40.389Z', '2026-11-11T21:55:48.605Z', NULL, 'codex:trial-reviewer', 2, NULL, NULL, '');
INSERT INTO "tasks" (task_id, title, owner, agent, branch, worktree, expected_files, modules, domains, risk_tags, notes, status, created_at, updated_at, parent_task_id, agent_id, version, assigned_agent_id, lease_expires_at, project) VALUES ('wsl-lane', 'trial-feature / wsl-lane', NULL, 'script', NULL, NULL, '["src/shared.ts","/home/user/Projects/hr-relay/src/wsl-abs.ts"]', '[]', '[]', '[]', 'Brief: protocol trial task created by rpc.py.', 'active', '2026-11-11T21:58:21.795Z', '2026-11-11T21:58:21.807Z', NULL, 'script:wsl-lane', 1, NULL, NULL, '');
INSERT INTO "tasks" (task_id, title, owner, agent, branch, worktree, expected_files, modules, domains, risk_tags, notes, status, created_at, updated_at, parent_task_id, agent_id, version, assigned_agent_id, lease_expires_at, project) VALUES ('queued-lane', 'trial-feature / queued-lane', 'operator', 'script', NULL, NULL, '["src/queued.ts"]', '["skill:tdd","skill:debug"]', '["dashboard"]', '[]', 'BRIEF: Build the queued lane. Acceptance: X, Y. Skills: tdd, debug.', 'proposed', '2026-11-11T21:59:17.010Z', '2026-11-11T21:59:17.173Z', NULL, NULL, 2, NULL, NULL, '');
INSERT INTO "tasks" (task_id, title, owner, agent, branch, worktree, expected_files, modules, domains, risk_tags, notes, status, created_at, updated_at, parent_task_id, agent_id, version, assigned_agent_id, lease_expires_at, project) VALUES ('probe-overlap', 'probe / overlap', NULL, 'script', NULL, NULL, '["src/shared.ts","src/wsl-abs.ts"]', '[]', '[]', '[]', NULL, 'closed', '2026-11-11T21:59:17.642Z', '2026-11-11T21:59:17.793Z', NULL, 'script:mac-probe', 2, NULL, NULL, '');
CREATE TABLE "task_updates" (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    kind       TEXT NOT NULL,
    content    TEXT NOT NULL,
    agent      TEXT,
    created_at TEXT NOT NULL,
    project    TEXT NOT NULL DEFAULT '', superseded_by INTEGER REFERENCES task_updates(id), supersede_reason TEXT, superseded_at TEXT,
    FOREIGN KEY (project, task_id) REFERENCES tasks(project, task_id)
  );
INSERT INTO "task_updates" (id, task_id, kind, content, agent, created_at, project, superseded_by, supersede_reason, superseded_at) VALUES (1, 'trial-lane', 'progress', 'Scripted trial context: claimed and recorded by rpc.py.', 'script:mac-trial-lane', '2026-11-11T21:52:49.054Z', '', NULL, NULL, NULL);
INSERT INTO "task_updates" (id, task_id, kind, content, agent, created_at, project, superseded_by, supersede_reason, superseded_at) VALUES (2, 'claude-lane', 'blocker', 'Waiting on: Codex reviewer verdict for claude-lane.', 'claude:trial-claude-lane', '2026-11-11T21:53:43.586Z', '', NULL, NULL, NULL);
INSERT INTO "task_updates" (id, task_id, kind, content, agent, created_at, project, superseded_by, supersede_reason, superseded_at) VALUES (3, 'claude-lane', 'finding', 'REVIEW VERDICT: PUSH', 'codex:trial-reviewer', '2026-11-11T21:55:44.965Z', '', NULL, NULL, NULL);
INSERT INTO "task_updates" (id, task_id, kind, content, agent, created_at, project, superseded_by, supersede_reason, superseded_at) VALUES (4, 'wsl-lane', 'progress', 'Scripted trial context: claimed and recorded by rpc.py.', 'script:wsl-lane', '2026-11-11T21:58:21.807Z', '', NULL, NULL, NULL);
