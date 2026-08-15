[English](README.md) | [日本語](README.ja.md)

# desk-link

`desk-link` is a **local seat bus** on the mothership Windows PC. It lets Grok Bot see conversations from Cursor, Claude Code, Codex, and Grok Build, and reply only through official paths that already exist.

It is not a second winsmux operator. It does not scrape pixels, steal cookies or tokens, or replace Hermes wiki.

## Why it exists

Each seat already writes its own session files. There is no official MCP that unifies every seat chat into one stream. As of 2026-08-15, Cursor has no live Bot ↔ Composer duplex API. The closest official surfaces are MCP (an agent calls tools) and Cloud Agent launch/reply (async, not the IDE chat).

`desk-link` is the thin local adapter: watch those files, put summaries on one bus, and leave the originals where they are.

## What it does

- Watches seat JSONL transcripts (read-only in Phase 0).
- Appends one summary event per utterance to a local `bus/inbox.jsonl`.
- Lets Grok Bot read that bus and, in Phase 1, write back through official CLIs or a small Cursor MCP.
- Keeps real chat bodies off the public git tree.

## What it does not do

- Insert bubbles into Cursor Composer.
- Open `state.vscdb`.
- Touch winsmux implementation or GO.
- Commit transcripts, tokens, or cookies.

## Seats

| Seat | Phase 0 (read) | Phase 1 (write) |
| --- | --- | --- |
| Cursor | `agent-transcripts` JSONL | small MCP, or async Cloud Agent reply |
| Claude Code | `projects` JSONL | `claude -p` / `--resume` / `--continue` |
| Codex | `rollout-*.jsonl` | `codex exec` / `resume` |
| Grok Build | `chat_history.jsonl` | `grok -p` / `--continue` |
| Grok Bot | this bus | SendToAgent / Hermes inbox |

## Local bus

One event per line. Summaries only. Originals stay in each seat file.

```
bus/inbox.jsonl    seats → bot
bus/outbox.jsonl   bot → seats
bus/ack.jsonl      delivery result
```

Minimum event shape (local contract, not an official API):

```json
{"id":"...","ts":"2026-08-15T08:52:00+09:00","seat":"cursor|claude|codex|grok_build|bot","dir":"in|out","kind":"utterance|meta","text":"<summary>","src":"<path-or-cli>"}
```

## Security

- Do not commit `bus/*.jsonl` bodies or seat transcripts.
- Do not read `mcp.json` secrets, cookies, or tokens.
- Internal plans (`PLAN.md`, `STATUS.md`, and similar notes) stay on the local machine and are gitignored.

## Status

The public tree is the product surface only. The Phase 0 watcher is not written yet.
