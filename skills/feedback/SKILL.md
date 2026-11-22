---
name: feedback
description: Collect the operator's feedback as it arrives in short bursts and hold all action until the operator says to act, then apply it as one batch. Use only when the user explicitly invokes feedback by name.
disable-model-invocation: true
---

# Feedback

The operator sends feedback in several short messages. Acting on the first one wastes work the next
one changes.

1. On invocation, start nothing new. Let a command that is already running finish.
2. For each message, add its points to a numbered list and reply with one line per point:
   `Noted <n>: <the point in your words>`. Make no edits, send nothing to workers and ask nothing,
   unless a point cannot be understood; then ask in that same line.
3. A stop instruction ("stop", "pause", "don't push") applies at once.
4. When the operator says to act, restate the list merged and deduplicated; where two points
   conflict the later one wins, and say so. Then do all of it as one batch.
5. Feedback meant for a worker you lead goes to that worker as one message, after step 4.
