# Work summary

"What did I get done" for a time window, or a weekly recap, from the user's own commits.

1. Resolve the window into concrete dates (default for a weekly recap: the last 7 days).
2. Read the git user's email from repo config; if missing, ask the user to set it.
3. Collect commits authored by that email in the window on the primary branch context, excluding
   merge commits and uncommitted changes: `git log --author=<email> --since=<start> --until=<end>
   --no-merges --stat`. Read diffs where a subject is unclear.
4. Keep substantial behavior or architecture changes; drop cosmetic-only ones (formatting, imports,
   minor renames). Describe changes functionally; do not infer intent.

Output: one short status-update summary with the real date range, then 2–5 bullets for major changes
only. A weekly recap adds a short paragraph classifying the work as likely bug fixes, tech debt or
net-new functionality, based only on commit history and diffs.

Adapted from cursor-team-kit's what-did-i-get-done and weekly-review (MIT, `../LICENSE-cursor-team-kit`).
