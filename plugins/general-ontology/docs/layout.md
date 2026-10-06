# The layout of a topic repo

Read it before you look for a file in a topic. Use the kit to read and change them; never parse or edit the
files yourself (`AGENTS.md`, sections 4 and 6).

| Path | Contents |
|---|---|
| `ontology.json` | Manifest: name, namespace (`ns`), title, packs, policy |
| `packs/local.*` | The topic's own kinds, relations and questions (on top of the built-in `core` and `discovery` packs, and `assessment` when it is on) |
| `graph/` | `nodes.jsonl` and `edges.jsonl`, sorted by id |
| `sources/` | Ingested texts, sanitized, named `src-<hash>`, with `index.jsonl` |
| `proposals/` | `pending/` and `done/` proposals with their reviews |
| `interview/log.jsonl` | Every question asked and how it was answered |
| `ledger/` | `decisions/` (the user's choices) and `changes.jsonl` (every write) |
| `metrics/history.jsonl` | Richness over time |
| `imports/` | `lock.json` and the vendored exports of imported topics |
| `build/`, `MANIFEST.json`, `VERSIONS.md` | Release outputs |
| `.claude/settings.json` | The plugin wiring `onto setup` writes for Claude Code |
| `plugins/general-ontology/` | The kit: `bin/onto` (CLI), `bin/onto-mcp` (MCP server), `skills/`, `docs/`, `ontokit/` |
| `examples/` | The synthetic demo (`bash examples/demo.sh`) |
