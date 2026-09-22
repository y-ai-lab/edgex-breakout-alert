# EdgeX 4Hトレンド / 15Mロールリバーサル通知システム

EdgeXの公開WebSocketから現在取引可能な全コントラクトを取得し、4時間足でトレンドとロールリバーサル候補を監視、15分足で押し目買い・戻り売りのエントリー条件を判定します。条件が揃い、ATR調整後のリスクリワードが1:2以上の場合だけTelegramへ通知します。

このプログラムは通知専用です。EdgeXへの注文・自動売買は行いません。

## 独立した定期実行方式

有効な `.github/workflows/edgex-breakout-scan-v2.yml` は、Public GitHubの標準GitHub-hosted runnerで15分ごと（15分足確定後）に起動し、EdgeXの全取引可能コントラクトを確認して終了します。PCを起動したままにする必要はありません。

これはWebSocketへ24時間接続し続ける方式ではなく、各回のスナップショットを確認する方式です。GitHub Actionsの起動遅延が発生する場合があるため、厳密なティック単位のリアルタイム監視ではありません。

## 判定ルール

現在の機械判定は次の通りです。

- 監視足：4時間足
- エントリー足：15分足
- トレンド：4HのEMA20 > EMA50かつ終値 > EMA20なら上昇、EMA20 < EMA50かつ終値 < EMA20なら下降
- ロールリバーサル：4Hで直近20本高値/安値を終値で突破した水準を候補化。直近6本の4H足以内のブレイクのみ有効
- 押し目/戻り：15Mの直近4本がロールリバーサル水準へATR許容幅内で再接触し、最新15M足がトレンド方向へ確定
- ATR：ATR14を使用
- 損切り：15M直近5本のスイングとロールリバーサル水準の外側へ、15M ATR × 0.5のバッファ
- 利確：ブレイク後の4H直近高値（ショートは直近安値）を基準に、4H ATR × 0.25だけ内側へ調整
- RR：ATR調整後で1:2未満は通知しない
- RR 1:2以上〜1:3未満：分割利確なし。全量を4Hターゲットで利確
- RR 1:3以上：2分割。50%を2R、残り50%を4Hターゲット
- 1回の最大リスク：エントリー時点の総資産の5%
- 枚数：`総資産 × 5% ÷ |Entry - SL|` を上限に、EdgeXの注文刻みへ切り下げる
- 証拠金残高・レバレッジが設定されている場合は、証拠金上限でも枚数を縮小する

ATRバッファやEMA期間などは環境変数で変更できます。現時点では上記を固定の検証ルールとして運用します。

## 起動方法

Python 3.11以降を推奨します。

```bash
cd edgex-breakout-alert
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

`.env` に以下を設定します。

```dotenv
TELEGRAM_BOT_TOKEN=BotFatherで取得したトークン
TELEGRAM_CHAT_ID=通知先のchat_id
DRY_RUN=false
```

その後、起動します。

```bash
python app.py
```

定期実行と同じ1回スキャンを手元で確認する場合は、次のコマンドを使います。

```bash
python app.py --once --dry-run --log-level INFO
```

Telegram未設定で動作確認するときは、次のようにします。通知内容はログへ出ます。

```bash
python app.py --dry-run --log-level DEBUG
```

## 5%リスクのエントリー指示

Telegram通知時のEntryは条件成立した15分足の終値です。SLは上記のATR調整済みラインを使用し、次の式で枚数を計算します。

```text
リスク予算 = 現在のEdgeX Equity × 5%
理論枚数 = リスク予算 ÷ |Entry - ATR調整済みSL|
最終枚数 = min(理論枚数, 証拠金上限が分かる場合の上限, EdgeX最大注文枚数)
```

EdgeXの注文刻みへ切り下げるため、実際のSL損失は5%以下になります。手数料・スリッページ・資金調達料はこの5%計算には含まれません。

APIキーがない場合はTelegramの `/equity` で現在資産を更新できます。API認証情報がある場合はPrivate REST APIの口座資産取得も利用できます。いずれの場合も自動発注は行いません。

## Telegramから口座資産を更新

APIキーがなくても、通知に使っているTelegram botへ残高を送るだけで、次回以降の5%リスク計算へ反映できます。

```text
/equity 31.50
```

botは次回の定期スキャン時にコマンドを読み取り、次のように返信します。

```text
✅ EdgeX残高を更新しました
Equity: $31.5000
1トレード最大リスク: $1.5750 (5.0%)
```

現在値だけ確認する場合は `/equity` を送ります。`/balance 31.50` または `残高 31.50` でも更新できます。

更新値は `data/edgex_alert_state.json` に保存されるため、GitHub Actionsの実行環境が毎回作り直されても引き継がれます。コマンドは設定済みの `TELEGRAM_CHAT_ID` と一致するチャットからのみ受け付けます。反映は定期スキャン単位なので、通常は次の15分スキャン以降です。

## Telegramの準備

1. Telegramで `@BotFather` を開き、`/newbot` でBotを作成する
2. 発行されたBot tokenを `.env` の `TELEGRAM_BOT_TOKEN` に入れる
3. 通知先のチャットまたはグループへBotを追加する
4. 通知先からBotへ一度メッセージを送る
5. `https://api.telegram.org/bot<TOKEN>/getUpdates` を開き、返却JSON内の `message.chat.id` を `TELEGRAM_CHAT_ID` に入れる

Bot tokenをURLやログに貼り付けないでください。`.env` はGitへコミットしない設定です。

## GitHub Actionsを有効にする手順

1. このリポジトリの `Settings` → `Secrets and variables` → `Actions` → `New repository secret` を開く
2. 次の2つを登録する

```text
TELEGRAM_BOT_TOKEN = BotFatherのトークン
TELEGRAM_CHAT_ID = 通知先のchat_id
```

3. `Actions` タブで `EdgeX breakout scan` を選び、`Run workflow` で手動実行して確認する
4. 問題がなければ、以後は15分ごとの自動実行に任せる

Telegram接続だけを確認したい場合は、手動実行フォームの `test_telegram` を `true` にします。この場合だけテスト通知を1通送信し、その後に通常のスキャンを実行します。

ワークフローはTelegram認証情報をSecretsから読み込みます。認証情報をソースコード、`.env`、ログへ書き込みません。重複通知防止用の `data/edgex_alert_state.json` だけを必要時にリポジトリへ保存します。

Publicリポジトリの標準GitHub-hosted runnerだけを使うため、VPS・有料API・有料runnerは不要です。GitHub Actionsの実行時刻は混雑状況によって遅れることがあります。

## 主な調整値

```dotenv
EDGE_X_MONITOR_INTERVAL=HOUR_4
EDGE_X_ENTRY_INTERVAL=MINUTE_15
EDGE_X_TREND_FAST_EMA=20
EDGE_X_TREND_SLOW_EMA=50
EDGE_X_ROLL_LOOKBACK=20
EDGE_X_ROLL_MAX_AGE=6
EDGE_X_ATR_PERIOD=14
EDGE_X_ATR_STOP_BUFFER=0.5
EDGE_X_ATR_TARGET_BUFFER=0.25
EDGE_X_MIN_RR=2.0
EDGE_X_SPLIT_RR=3.0
EDGE_X_RISK_PER_TRADE=0.05
```

## Dockerで常駐させる場合

```bash
cp .env.example .env
# .envにTelegram設定を入力
docker compose up -d --build
docker compose logs -f
```

SQLiteは `./data` に保存されるため、再起動しても重複通知を抑制できます。

## 状態保存方式

- 通常の常駐起動：`STATE_BACKEND=sqlite`（SQLiteを使用）
- GitHub Actions：`STATE_BACKEND=json`（`STATE_FILE`をリポジトリへ保存）

GitHub Actionsでは毎回runnerが新しくなるため、ワークフローがJSON状態をコミットして次回へ引き継ぎます。

## 注意

EdgeXの公開API・WebSocketの仕様、接続制限、取引状況は変更される可能性があります。通知は売買推奨ではなく、ブレイクアウト条件に一致したという機械的な検出結果です。特に低流動性銘柄では、スリッページや急反転のリスクがあります。
