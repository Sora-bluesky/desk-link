# desk-link STATUS

公開: https://github.com/Sora-bluesky/desk-link

## いま

- スラッグ desk-link を母艦 Documents/Projects に確保した。
- 調査済み。採用はローカル JSONL バス + 座席アダプタ。
- Cursor ライブ Composer duplex は無い（2026-08-15）。
- コードはまだ無い。watch も未実装。
- winsmux / 0.36.32 は未着手のまま。

## つぎ

1. 専用エージェント desk-link を左ペインに作る。
2. Phase 0: bus/inbox.jsonl と4座席の読み取り専用 watch。
3. Phase 1 は Phase 0 が1座席でも実イベントを出してから。最初の write は Claude か Codex の公式 CLI。

## 座席メモ（短い）

| 座席 | Phase 0 | Phase 1 |
|---|---|---|
| Cursor | agent-transcripts jsonl | MCP（エージェント経由）。Composer 挿入なし |
| Claude | projects jsonl | claude -p / --resume |
| Codex | rollout jsonl | codex exec resume |
| Grok Build | chat_history.jsonl | grok -p / --continue |
