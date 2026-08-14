# desk-link

母艦（Windows PC）上の **ローカル座席バス**。Grok Bot が Cursor / Claude Code / Codex / Grok Build の会話を見て、公式に存在する経路だけで返す。

- 公開リポジトリ: https://github.com/Sora-bluesky/desk-link
- 計画: [PLAN.md](PLAN.md)
- 現状: [STATUS.md](STATUS.md)

## これは何か

各座席の会話を **1本のローカル JSONL バス** に集約する薄いアダプタ群。winsmux の第二オペレーターではない。IDE を画素スクレイピングしない。OAuth Cookie を盗まない。Hermes wiki の代替でもない。

## 公式に無いもの

2026-08-15 時点、Cursor に **Bot ↔ IDE Composer のライブ双方向 API は無い**。近い公式面は MCP（エージェントがツールを呼ぶ）と Cloud Agent の launch/reply（非同期）。Composer 本体は `state.vscdb` に入り、tail できるチャットファイルではない。

## 所有

専用の左ペイン Grok Bot エージェント `desk-link` がこの製品を持つ。winsmux 残務の Sora 席に混ぜない。
