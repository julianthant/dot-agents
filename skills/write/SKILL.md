---
name: write
description: Draft, edit, humanize or critique human-facing prose, including emails, memos, articles, proposals and website copy. Use for /write or requests to make prose sound natural and match the writer's voice. Code generation and repository/agent instruction maintenance use their own workflows. Also for a slop pass on any text and for restating something plainly (/bro).
---

# Write

Produce prose that sounds like a person with something specific to say. Humanizer's method is
built into the default drafting and editing pass below; no separate skill or installation is needed.

## Establish the brief

Infer audience, purpose, voice, format and length from the request and supplied material. Read an
available writing sample to learn its vocabulary, rhythm, formality and deliberate quirks. Ask only
for missing information that would materially change the result; otherwise proceed with a reasonable
choice. Draft or revise when requested; a critique alone returns feedback without changing the text.

Keep facts, names, numbers, dates, citations, quotations, negations, uncertainty and commitments faithful
to the source. A rewrite retains substantive meaning; a requested summary may omit detail while
preserving qualifications that change the conclusion. Flag contradictions instead of resolving them
by invention. Missing evidence calls for simpler wording, a clearly marked placeholder when useful,
or a focused question. Fiction may invent details within the requested premise.

## Draft and humanize

- Lead with what the reader needs. Let each paragraph develop a useful point; use lists when the
  material is actually parallel or sequential. Stop when the purpose is fulfilled.
- Use concrete verbs and familiar words. Remove empty praise, inflated significance, vague experts,
  filler, chatbot greetings/closings, and canned contrast or conclusion formulas.
- Vary sentence length and openings where it improves flow. Keep stable names for the same thing;
  forced synonyms, ornamental phrasing and mechanically balanced lists make prose harder to read.
- Match the writer's voice and the audience. Preserve personality, humor or uncertainty already
  supported by the brief. Do not invent anecdotes, opinions or lived experience to sound human.
  Formal and technical writing can be natural without becoming casual.
- Use punctuation and formatting for meaning. Avoid mechanical word blacklists, arbitrary dash bans,
  deliberate errors or forced informality. Never promise to bypass AI detectors.

For substantial rewrites or an explicit humanizing request, read
[humanizing and fidelity checks](references/humanizing.md). For persuasive or conversion copy, also
read [marketing](references/marketing.md); ordinary correspondence does not need that branch.
For a line-by-line slop pass on any text, including agent replies and docs, use the numbered
[slop patterns](references/slop-patterns.md).

**Say it plainly** (`/bro`, "say that simply"): restate the last message without jargon, simpler and
shorter, like one person talking to another.

Before returning prose, compare it with the source for factual drift, then read for rhythm, repetition
and unnecessary words. Preserve exact quoted text, commands and code unless their alteration is part
of the request. Humanizer-style editing must not erase a meaningful caveat or turn a target into a promise.

## Deliver

Return the requested copy directly, with only material unresolved issues or requested editorial notes.
Avoid unsolicited alternate versions and explanations of every edit. Prose intended for another reader
uses normal grammar even when conversation updates use Caveman compression, unless the user requests
that style for the artifact itself.

Draft in chat by default; edit a named file when requested. For Word/PDF/slides or another formatted
artifact, use the relevant available native artifact skill and its verification procedure. Copy-only
webpage work belongs here; building the page uses /ui. Repository or agent instructions use /docs.
Drafting does not authorize sending messages or publishing; require explicit authorization for that action.

Method sources and local adaptations are recorded in the linked references. Evaluation requests use
[scenarios](evals/evals.json); these are test inputs, not routine writing context.
