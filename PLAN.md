# desk-link 計画

公開リポジトリ: https://github.com/Sora-bluesky/desk-link

---
agent: grok-bot
dropped: 2026-08-15
id: 2026-08-15-0852-desk-link-plan
---

## Summary

- sora 2026-08-15 08:47 JST。母艦で Grok Bot が Cursor / Claude Code / Codex / Grok Build と双方向に見える机上リンクを作れ、という依頼。
- 採用アーキテクチャは **1つだけ**: 母艦ローカルのメッセージバス（ファイルシステム JSONL + 任意の localhost HTTP/MCP）。座席ごとに薄いアダプタ。
- 2026-08-15 時点、Cursor に Bot↔IDE ライブチャット API は無い。Composer は tail できるファイルではない。
- この製品は専用エージェント `desk-link` が持つ。winsmux / TASK-661 / 0.36.32 には手を出さない。
- 公開リポジトリを `Sora-bluesky/desk-link` に作る（private にしない）。

## Source

- 母艦ディレクトリ一覧: `C:\Users\sorab\Documents\Projects`（`desk-link` は未使用だった）
- Cursor: `%USERPROFILE%\.cursor`（`mcp.json`, `hooks.json`, `projects\`）
- Claude Code: `%USERPROFILE%\.claude`（`sessions\`, `projects\`, `hooks\`, `history.jsonl`）
- Codex: `%USERPROFILE%\.codex`（`sessions\YYYY\MM\DD\rollout-*.jsonl`, `hooks.json`, `session_index.jsonl`）
- Grok Build: `%USERPROFILE%\.grok`（`sessions\`, `logs\unified.jsonl`, `README.md`）
- Grok Bot: `%USERPROFILE%\.grok-bot\rules\`
- Hermes drop プロトコル: Vault の grok-bot DROP.md（Vault 本文は公開ツリーに入れない）
- 公式ドキュメント（ログイン不要で到達）:
  - https://cursor.com/docs/mcp
  - https://code.claude.com/docs/en/hooks
  - https://developers.openai.com/codex/cli/slash-commands
  - 母艦 `claude --help` / `codex --help` / `grok --help` / `%USERPROFILE%\.grok\README.md`

## Facts

1. C:/Users/sorab/Documents/Projects に desk-link / desk-bus は無かった。スラッグは desk-link。
2. GitHub Sora-bluesky/desk-link は未作成だった（gh repo view が resolve 失敗）。
3. Cursor エージェント転写は実在する: %USERPROFILE%/.cursor/projects/<slug>/agent-transcripts/<uuid>/<uuid>.jsonl。1行目キーは role, message。winsmux 作業コピーで確認した（winsmux コードは読んでいない・触っていない）。
4. Cursor Composer / IDE チャットは %APPDATA%/Cursor/User/workspaceStorage/*/state.vscdb（SQLite）と globalStorage/state.vscdb。tail できるチャットファイルではない。
5. Cursor MCP は公式。設定は %USERPROFILE%/.cursor/mcp.json（キー mcpServers。中身の秘密は読まない）。Cloud Agents も MCP を使える（公式）。Bot↔Composer のライブ duplex API は公式に無い。

6. Claude Code の会話本体は %USERPROFILE%/.claude/projects/<encoded-cwd>/*.jsonl（キー type, operation, timestamp, sessionId, content）。sessions/*.json はメタ。history.jsonl あり。
7. Claude 公式 write: claude -p（非対話）、claude -c/--continue、claude -r/--resume。hooks は公式。UserPromptSubmit の additionalContext は文脈注入であり、吹き出し挿入ではない。

8. %USERPROFILE%/.claude/hooks/inbox.jsonl は母艦に実在（キー id, at, from, to, subject, body, expires_at）。Claude 公式 API ではない。ローカル発明のメール箱。
9. Codex 公式 read: %USERPROFILE%/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl（キー timestamp, type, payload）。session_index.jsonl あり。公式 write: codex exec / codex resume / codex exec resume（codex --help で確認）。

10. Grok Build 公式 read: %USERPROFILE%/.grok/sessions/<encoded-cwd>/<uuid>/chat_history.jsonl ほか events.jsonl / updates.jsonl。
prompt_history.jsonl キーは timestamp, session_id, prompt, is_bash。公式 write: grok 対話、grok -p（headless、README）、grok --continue。
--resume は --fork-session 説明に出てくる。grok agent stdio（ACP）も README にある。
11. Grok Bot は %USERPROFILE%/.grok-bot/rules/（anti-stall-lifecycle.md, model-dispatch.md）と Hermes inbox が既にある。公式ライブ座席 API は無い。Bot が付ける先がこのバス。

12. 非目標は winsmux 第二GO、画素取得、wiki 置換、0.36.32 着手。

## Architecture

YAGNI。ブローカー1本、アダプタは座席ごと。母艦ローカルの JSONL バスが正。任意で localhost MCP/HTTP。

置き場は本リポジトリの作業コピー（Documents/Projects/desk-link）。公開ツリーに会話本文を置かない。

| file | role |
|---|---|
| bus/inbox.jsonl | seats to Bot. one event per line. append only |
| bus/outbox.jsonl | Bot to seats. Phase 1 adapters consume |
| bus/ack.jsonl | delivery result |

Event min shape (local invention, not an official API):

{"id":"...","ts":"2026-08-15T08:52:00+09:00","seat":"cursor|claude|codex|grok_build|bot","dir":"in|out","kind":"utterance|meta","text":"...","src":"path-or-cli"}

Bus holds summaries. Originals stay in each seat file. Do not commit transcripts to public git.

localhost MCP は Cursor / Claude / Codex が公式に接続できる面として出す（stdio）。相手がツールを呼ぶ経路であり、Composer 吹き出しへのライブ挿入ではない。

### 座席ごとの分類

Cursor: Read = agent-transcripts jsonl watch。Write = ライブ Composer 挿入は不可。Phase 1 = 小さな MCP を Cursor エージェントが add。任意で Cloud Agent launch/reply（非同期、IDE チャットではない）。read は製品が書くローカルファイル（非公式 API）。MCP / Cloud Agent は公式。Composer duplex は存在しない。Phase 0 = watch。Phase 1 = MCP + 任意 Cloud Agent。

Claude Code: Read = projects jsonl watch。補助で hooks の Stop / UserPromptSubmit。Write = claude -p / --resume / --continue。hook additionalContext は文脈注入。hooks/inbox.jsonl はローカル発明。セッション jsonl / CLI / hooks は公式。Phase 0 = watch。Phase 1 = CLI。

Codex: Read = sessions/YYYY/MM/DD/rollout-*.jsonl + session_index.jsonl。Write = codex exec / resume / exec resume。両方公式。Phase 0 = watch。Phase 1 = CLI。
Grok Build: Read = chat_history.jsonl（補助 logs/unified.jsonl）。Write = grok -p / --continue（--resume は help 交差参照、未完全検証）。セッションと CLI は公式。Phase 0 = watch。Phase 1 = CLI。
Grok Bot: Read/Write = 既存 SendToAgent / Hermes inbox。本バスに attach。バス自体は発明。Phase 0 から attach。

### Phase 0（今やる）

1. bus/ を母艦ローカルに作る（git には空 .gitkeep だけ。本文は gitignore）。
2. Cursor / Claude / Codex / Grok の jsonl を読み取り専用で tail し、要約イベントを bus/inbox.jsonl に書く。
3. Bot は bus/inbox.jsonl を読む。相手のチャットに書き込まない。
4. Composer state.vscdb は触らない（SQLite、ライブチャットではない、壊すリスク）。

### Phase 1（Bot が返せる）

1. Claude: claude -p または --resume で新しいターンを公式 CLI から足す。
2. Codex: codex exec resume で公式に足す。
3. Grok Build: grok -p / --continue で公式に足す。
4. Cursor: 小さな desk-link MCP（inbox_read / outbox_write）を mcp.json に足し、エージェントがツール経由で読む/書く。Cloud Agent reply は非同期バックアップ。Composer に吹き出しを挿す約束はしない。
5. どの write も人間承認または各 CLI の許可モデルを通す。自動なりすましはしない。

### 非目標

- winsmux の第二 GO / 第二オペレーター
- IDE の画素取得
- 認証情報の流用
- Hermes wiki の置換（inbox drop は続ける。バスは机上の短距離）
- 0.36.32 / TASK-661 の開始
- 公式ライブ duplex の捏造

### 推奨エージェント

- 名前: desk-link
- 一人称ペルソナ（一行）: 母艦デスクの座席間バス係。見て、渡して、記録する。winsmux には手を出さない。
- 親が左ペインに作る。winsmux 残務の Sora 席に混ぜない（GO が二つになる）。

## Open・Uncertain

- Cursor Composer を SQLite から読むことは技術的には可能だが、非公式・壊れやすい。Phase 0 ではやらない。
- grok --resume の正確なフラグ文面は、--fork-session 説明での言及まで。完全な help 行は再取得できなかった。
- Claude hooks/inbox.jsonl を誰が消費するかは未検証。Phase 1 の正は claude CLI。
- Codex hooks.json の中身は読んでいない。セッション resume は CLI 公式面で足りる。
- ライブで今この吹き出しに割り込む公式 API は、どの座席にも無い。Phase 1 は次ターンを CLI / MCP で足す。
- バスを localhost HTTP にするかは Phase 0 では不要。ファイル watch で足りる。

## Links

- https://github.com/Sora-bluesky/desk-link
- https://cursor.com/docs/mcp
- https://code.claude.com/docs/en/hooks
- https://developers.openai.com/codex/cli/slash-commands
- [[grok-bot]]
- [[desk-link]]

## Next Action

1. 親が左ペインに desk-link エージェントを作る（winsmux Sora 席に混ぜない）。
2. そのエージェントが Phase 0: bus/ + 4座席の jsonl watch（読み取り専用）。
3. Cursor にはライブ duplex を約束しない。MCP は Phase 1。
4. winsmux / 0.36.32 / TASK-661 は触らない。

