[English](README.md) | [日本語](README.ja.md)

# desk-link

`desk-link` は母艦（Windows PC）上の **ローカル座席バス** です。Grok Bot が Cursor / Claude Code / Codex / Grok Build の会話を見て、公式に存在する経路だけで返します。

winsmux の第二オペレーターではありません。画素スクレイピング、cookie / token の取得、Hermes wiki の置換もしません。

## なぜあるか

各座席はすでに自分のセッションファイルを書いています。全席チャットを一本化する公式 MCP はありません。2026-08-15 時点、Cursor に Bot ↔ Composer のライブ双方向 API はありません。近い公式面は MCP（エージェントがツールを呼ぶ）と Cloud Agent の launch/reply（非同期、IDE チャットではない）です。

`desk-link` はその薄いローカルアダプタです。ファイルを監視し、要約を1本のバスに載せ、本文は元の座席ファイルに残します。

## 何をするか

- 座席の JSONL を監視する（Phase 0 は読み取り専用）。
- 発話ごとに要約イベントをローカルの `bus/inbox.jsonl` へ append する。
- Grok Bot がそのバスを読む。Phase 1 では公式 CLI、または小さな Cursor MCP で返す。
- 実チャット本文は公開 git に入れない。

## 何をしないか

- Cursor Composer への吹き出し挿入。
- `state.vscdb` を開くこと。
- winsmux の実装や GO に触ること。
- transcript / token / cookie の commit。

## 座席

| 座席 | Phase 0（読み） | Phase 1（書き） |
| --- | --- | --- |
| Cursor | `agent-transcripts` JSONL | 小さな MCP、または非同期 Cloud Agent reply |
| Claude Code | `projects` JSONL | `claude -p` / `--resume` / `--continue` |
| Codex | `rollout-*.jsonl` | `codex exec` / `resume` |
| Grok Build | `chat_history.jsonl` | `grok -p` / `--continue` |
| Grok Bot | このバス | SendToAgent / Hermes inbox |

## ローカルバス

1行1イベント。載せるのは要約だけ。本文は各座席の元ファイルに残す。

```
bus/inbox.jsonl    座席 → Bot
bus/outbox.jsonl   Bot → 座席
bus/ack.jsonl      配送結果
```

イベント最小形（ローカル契約。公式 API ではない）:

```json
{"id":"...","ts":"2026-08-15T08:52:00+09:00","seat":"cursor|claude|codex|grok_build|bot","dir":"in|out","kind":"utterance|meta","text":"<要約>","src":"<path-or-cli>"}
```

## セキュリティ

- `bus/*.jsonl` の本文と座席 transcript は commit しない。
- `mcp.json` の秘密、cookie、token は読まない。
- 内部計画（`PLAN.md` / `STATUS.md` など）は母艦ローカルに置き、gitignore する。

## 現状

公開ツリーは製品面だけ。Phase 0 の watcher はまだ無い。
