[English](protocol.md) | [日本語](protocol.ja.md) | [README](../README.ja.md)

# desk-linkプロトコル

JSONLは、1行に1つのJSONオブジェクトを記録するテキスト形式です。outboxには依頼、ackストリームには配信状態、inboxには成功した返答が入ります。

## Phase 0のseat-tailイベント

Phase 0は対応するローカル会話記録を読み取り、要約イベントを生成します。必須の基本項目は`id`、`ts`、`seat`、`dir`、`kind`、`text`、`src`です。

`dir`は常に`in`で、`kind`は`utterance`または`meta`です。`text`は秘匿情報を伏せた要約で、200文字以下になります。

経路項目は明示的に設定したときだけ追加されます。seat-tailイベントは、ローカル会話記録から経路項目をコピーしません。

```json
{"id":"<event-id>","ts":"<timestamp>","seat":"cursor","dir":"in","kind":"utterance","text":"[assistant] <summary>","src":"<source>"}
```

## 依頼の項目

すべての依頼には、次の空ではない文字列項目が必要です。

| 項目 | 意味 |
| --- | --- |
| `id` | 一意のrequest ID |
| `ts` | 依頼の時刻 |
| `seat` | `bot` |
| `dir` | `out` |
| `kind` | `utterance`、`design`、`adversarial`、`independent`、`implement`のいずれか |
| `text` | 選択したCLIへ送るプロンプト |
| `src` | `grok-bot` |
| `to` | 必須の経路先。`cursor`、`claude`、`codex`、`grok-build`のいずれか |

`cwd`は任意で、既存のワークスペースを指定します。省略時はdesk-linkディレクトリがワークスペースになります。

```json
{"id":"<request-id>","ts":"<timestamp>","seat":"bot","dir":"out","kind":"utterance","text":"<prompt>","src":"grok-bot","to":"cursor"}
```

## 経路メタデータと検証

bridge依頼では`to`が必須で、4つの対象値に対して検証されます。Phase 0では、`to`、`model`、`effort`を明示的に設定しない限り省略します。

経路項目の検証で明示的に設定する場合に使える値は、次のとおりです。

| 項目 | 利用できる値 |
| --- | --- |
| `to` | `cursor`、`claude`、`codex`、`grok-build` |
| `model` | `grok-4.6`、`gpt-5.6-sol` |
| `effort` | `xhigh`、`ultra` |

ディスパッチャーは、対象を選ぶために`model`と`effort`を必要としません。bridge依頼で必須の経路指定は`to`項目です。

## 確認結果と返答

確認結果には、イベントの項目に加えて`reply_to`、`request_id`、`target`、`status`、`kind`が入ります。`reply_to`と`request_id`は、どちらも元のrequest IDを記録します。

```json
{"id":"<ack-id>","ts":"<timestamp>","seat":"bot","dir":"ack","kind":"started","status":"started","text":"","src":"bridge","reply_to":"<request-id>","request_id":"<request-id>","target":"cursor"}
```

完了確認では`kind`が`terminal`で、`status`は`ok`または`error`です。成功したinbox返答は、`reply_to`に元のrequest IDを記録します。

```json
{"id":"<reply-id>","ts":"<timestamp>","seat":"cursor","dir":"in","kind":"utterance","text":"<CLI reply>","src":"cursor-agent","reply_to":"<request-id>"}
```

保存されるbridgeの確認結果とinbox本文は、秘匿情報を伏せて200文字以下にします。同期`ask`の標準出力は、保存時の200文字形式ではなく、成功した返答の全文を伏せ字処理して返せます。

## 配信と失敗時の動作

外部CLIの実行は、exactly-onceではなくat-most-onceです。完了確認は重複排除の記録であるため、完了結果を持つrequest IDは再実行されません。

バスごとに同時に有効な配信は1件だけです。その配信中でも、独立したoutboxへの追加は続けられます。

各外部CLI実行には、CLIが終了するまでバスの実行ロックを保持する実行監視があります。ディスパッチャーが異常終了しても実行監視がロックを保持するため、後続の配信は残ったCLIと重ならずに待機します。

依頼の取得後に処理が中断した場合、復旧処理はCLIを再実行せず、完了エラーを記録します。再試行には新しいrequest IDを送ります。

成功の完了確認後にinbox返答の記録が中断した場合、復旧処理はCLIを再実行せずに不足した返答を記録します。不正な依頼には完了エラーの確認結果が記録されます。

2つの公開CLI入口は、通常の保存・実行エラーを分類済みJSONへ変換します。未加工の例外トレースバックや個人パスは出力しません。

## 現在の制限

デーモンや自動起動はありません。`ask`は自分の依頼を追加して同期処理し、`run --once`はoutboxで待機中の依頼を1件処理します。

各アダプターは、新しいヘッドレス・非対話のCLI実行を開始します。desk-linkは、既存のUIチャットや製品標準セッションへメッセージを配信しません。

[アーキテクチャリファレンス](architecture.ja.md)でフローを確認し、コマンドは[README](../README.ja.md)を参照してください。
