# devdash — Agent Rules

devdash keeps two layers strictly separate:

- **Facts** (`metrics.json`, `graph.json`) are produced by `collect.py`. They are
  deterministic and machine-generated. **Never edit them by hand.**
- **Semantics** (`components.json`, `decisions.md`, and the optional
  `roadmap.md`) are yours to maintain. These are the **only files you may
  write.**

The whole point of devdash is trust. A dashboard that lies is worse than no
dashboard. Honesty beats completeness.

## components.json

A JSON array of component objects. One entry per meaningful module, folder, or
subsystem — not one per file. Each object:

- `id` — short, stable handle, e.g. `"api"`, `"auth"`, `"ingest"`. *(required)*
- `path` — folder or file it maps to, e.g. `"src/api/"`. *(required)*
- `description` — what it does **and why it exists as its own unit**. 1–2 plain
  sentences. *(required)*
- `status` — `"active"`, `"wip"`, or `"deprecated"`.
- `confidence` — `"high"`, `"medium"`, or `"low"`: your honest certainty.
  *(required)*
- `updated_at` — ISO-8601 timestamp; set to *now* whenever you touch the entry.
  *(required)*

Rules:

- Describe only what is actually in the code. No invented behaviour.
- When unsure, say so in the description and set `confidence: "low"`. A
  low-confidence honest note beats a confident wrong one.
- Keep it current. When code changes a component, update its `description` and
  `updated_at`. The dashboard flags entries older than 14 days.

## decisions.md

An append-only log of architectural decisions, newest on top. One block each:

```
## YYYY-MM-DD — short title
One to three sentences: what was decided and why. Name the affected components.
```

- Append; do not rewrite history.
- Log real decisions (a split, a library choice, a tradeoff) — not routine edits.

## roadmap.md (optional)

A small, living Markdown file for where the project is headed. Unlike
`decisions.md` it may be rewritten freely. Simple Markdown only — `## Heading`
and `- bullet`. Group by status (Done / In progress / Planned) if useful. It is
semantics, not facts: keep it honest and short, or leave it empty. The dashboard
renders it as-is in the Roadmap panel.

## Never

- Never edit `metrics.json`, `graph.json`, or `devdash-data.js`. They are
  regenerated and overwritten on every run.
- Never inflate `confidence` or invent components to make the dashboard look full.
