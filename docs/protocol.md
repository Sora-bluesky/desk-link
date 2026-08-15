[English](protocol.md) | [日本語](protocol.ja.md) | [README](../README.md)

# desk-link protocol

JSONL is a text format in which each line is one JSON object. The outbox contains requests, the ack stream contains delivery state, and the inbox contains successful replies.

## Phase 0 seat-tail events

Phase 0 reads supported local conversation records and emits a summary event. Its required base keys are `id`, `ts`, `seat`, `dir`, `kind`, `text`, and `src`.

`dir` is always `in`, and `kind` is `utterance` or `meta`. `text` is a redacted summary capped at 200 characters.

Routing keys are omitted unless they are explicitly set. A seat-tail event does not copy routing fields from the local conversation record.

```json
{"id":"<event-id>","ts":"<timestamp>","seat":"cursor","dir":"in","kind":"utterance","text":"[assistant] <summary>","src":"<source>"}
```

## Request fields

Every request must include the following non-empty string fields.

| Field | Meaning |
| --- | --- |
| `id` | Unique request ID |
| `ts` | Request timestamp |
| `seat` | `bot` |
| `dir` | `out` |
| `kind` | `utterance`, `design`, `adversarial`, `independent`, or `implement` |
| `text` | Prompt sent to the selected CLI |
| `src` | `grok-bot` |
| `to` | Required routing target: `cursor`, `claude`, `codex`, or `grok-build` |

`cwd` is optional and selects an existing workspace. If it is omitted, the desk-link directory is the workspace.

```json
{"id":"<request-id>","ts":"<timestamp>","seat":"bot","dir":"out","kind":"utterance","text":"<prompt>","src":"grok-bot","to":"cursor"}
```

## Routing metadata and validation

For a bridge request, `to` is required and is validated against its four target values. Phase 0 omits `to`, `model`, and `effort` unless they are explicitly set.

When explicitly set through the routing-field validator, the allowed values are listed below.

| Field | Allowed values |
| --- | --- |
| `to` | `cursor`, `claude`, `codex`, `grok-build` |
| `model` | `grok-4.6`, `gpt-5.6-sol` |
| `effort` | `xhigh`, `ultra` |

The dispatcher does not require `model` or `effort` to select a target. A bridge request's required routing choice is its `to` field.

## Acknowledgements and replies

An acknowledgement includes `reply_to`, `request_id`, `target`, `status`, and `kind` in addition to its event fields. `reply_to` and `request_id` both carry the original request ID.

```json
{"id":"<ack-id>","ts":"<timestamp>","seat":"bot","dir":"ack","kind":"started","status":"started","text":"","src":"bridge","reply_to":"<request-id>","request_id":"<request-id>","target":"cursor"}
```

A terminal acknowledgement uses `kind` `terminal` and status `ok` or `error`. A successful inbox reply uses `reply_to` to carry the original request ID.

```json
{"id":"<reply-id>","ts":"<timestamp>","seat":"cursor","dir":"in","kind":"utterance","text":"<CLI reply>","src":"cursor-agent","reply_to":"<request-id>"}
```

Persisted bridge acknowledgement and inbox text are redacted and capped at 200 characters. Synchronous `ask` stdout can return the full redacted successful reply instead of the persisted 200-character form.

## Delivery and failure behavior

External CLI execution is at-most-once, not exactly-once. A terminal acknowledgement is the deduplication record, so a request ID with a terminal result is not dispatched again.

Only one delivery is active per bus. Independent outbox enqueueing can continue while that delivery is active.

Each external execution has a guardian that owns the bus execution lease until the CLI exits. If the dispatcher exits unexpectedly, the guardian keeps the lease, so a later delivery waits instead of overlapping the surviving CLI.

If processing is interrupted after the request is claimed, recovery records a terminal error rather than re-running the CLI. Send a new request ID to try again.

If a successful terminal acknowledgement exists but writing the inbox reply was interrupted, recovery writes the missing reply without re-running the CLI. Invalid requests receive a terminal error acknowledgement.

Both public CLI entry points convert ordinary storage and runtime failures into categorical JSON errors. They do not print raw exception tracebacks or private paths.

## Current limits

There is no daemon or automatic startup. `ask` enqueues and synchronously processes its own request, while `run --once` processes one request already waiting in the outbox.

Every adapter creates a new headless, non-interactive CLI execution. desk-link does not deliver messages into an existing UI chat or native product session.

See the [architecture reference](architecture.md) for the flow and the [README](../README.md) for commands.
