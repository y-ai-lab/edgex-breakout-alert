# EdgeX 出来高ブレイクアウト通知システム

EdgeXの公開WebSocketから、現在取引可能な全コントラクトを自動取得して監視します。指定した時間足の確定足が、直近レンジを終値で突破し、同時にEdgeX上のUSDC建て（将来変更された場合はメタデータ上の建値）出来高が平均を上回ったときだけTelegramへ通知します。

このプログラムは通知専用です。EdgeXへの注文・自動売買は行いません。

## 独立した定期実行方式

このリポジトリの `.github/workflows/edgex-breakout-scan.yml` を有効にすると、Public GitHubの標準GitHub-hosted runnerが5分ごとに起動し、EdgeXの全取引可能コントラクトを確認して終了します。AI VALUE RADARやGPT、PCを起動したままにする必要はありません。

これはWebSocketへ24時間接続し続ける方式ではなく、各回のスナップショットを確認する方式です。GitHub Actionsの起動遅延が発生する場合があるため、厳密なティック単位のリアルタイム監視ではありません。

## 判定ルール（初期値）

- 対象銘柄：EdgeXメタデータの `enableTrade=true`。`EDGE_X_INCLUDE_HIDDEN=true` の場合は非表示設定の取引可能銘柄も含む
- 時間足：5分足
- ブレイク：確定足の終値が、直前20本の最高値を0.1%以上上回る（上抜け）、または最低値を0.1%以上下回る（下抜け）
- 出来高確認：確定足の `value`（建値通貨建て売買代金）が、直前20本の平均の1.5倍以上
- 初回起動時：過去のブレイクを遡って通知せず、最新の確定足を基準に待機
- 重複防止：SQLiteへ処理済み足・通知済みシグナルを保存

`value` を使う理由は、銘柄ごとにトークン数量の単位が違うためです。EdgeX内の取引活況を判定する目的では、数量 (`size`) より建値通貨建ての売買代金が比較しやすくなります。

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

EdgeXのPrivate REST APIを読み取り専用で使える場合、ブレイク通知へ口座資産ベースのエントリー指示を追加します。自動発注は行いません。

計算は次の順です。

```text
リスク予算 = EdgeX TotalEquity × 5%
理論枚数 = リスク予算 ÷ |Entry - SL|
最終枚数 = min(理論枚数, 利用可能証拠金×現在レバレッジ÷Entry, EdgeX最大注文枚数)
```

初期設定では、Entryはシグナル確定足の終値、SLは上抜けならシグナル足の安値、下抜けならシグナル足の高値です。SL到達時の損失が口座Equityの5%以下になるよう枚数を算出し、1Rと2Rの価格・損益もTelegramへ表示します。証拠金や最大注文枚数で縮小された場合は、実際のリスク率も併記します。

APIキーがない場合は、EdgeX画面に表示される現在の口座資産をGitHub ActionsのRepository Secret `EDGEX_EQUITY_USDC` に入れます。これだけで「Equity × 5%」のSLリスク基準から枚数を計算できます。

任意で `EDGEX_AVAILABLE_BALANCE_USDC` と `EDGEX_LEVERAGE` も設定すると、証拠金上限を考慮して枚数を縮小できます。未設定の場合は証拠金上限の自動判定はせず、5%リスク基準の理論枚数を通知します。

Private REST APIの認証情報を持っている場合のみ、`EDGEX_ACCOUNT_ID` / `EDGEX_API_KEY` / `EDGEX_API_PASSPHRASE` / `EDGEX_API_SECRET` を使った自動取得も利用できます。手入力の `EDGEX_EQUITY_USDC` がある場合はそちらを優先します。

いずれの方式でも自動発注は行いません。

調整値は `.env.example` の `EDGE_X_RISK_PER_TRADE`、`EDGE_X_STOP_METHOD`、`EDGE_X_TP_R_MULTIPLE` で変更できます。

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
4. 問題がなければ、以後は5分ごとの自動実行に任せる

Telegram接続だけを確認したい場合は、手動実行フォームの `test_telegram` を `true` にします。この場合だけテスト通知を1通送信し、その後に通常のスキャンを実行します。

ワークフローはTelegram認証情報をSecretsから読み込みます。認証情報をソースコード、`.env`、ログへ書き込みません。重複通知防止用の `data/edgex_alert_state.json` だけを必要時にリポジトリへ保存します。

Publicリポジトリの標準GitHub-hosted runnerだけを使うため、VPS・有料API・有料runnerは不要です。GitHub Actionsの実行時刻は混雑状況によって遅れることがあります。

## 調整例

```dotenv
# 1分足に変更
EDGE_X_INTERVALS=MINUTE_1

# 5分足と1時間足を同時監視
EDGE_X_INTERVALS=MINUTE_5,HOUR_1

# 直近30本の高値・安値、出来高2倍、ブレイク幅0.2%
EDGE_X_BREAKOUT_LOOKBACK=30
EDGE_X_VOLUME_LOOKBACK=20
EDGE_X_VOLUME_MULTIPLIER=2.0
EDGE_X_MIN_BREAKOUT_PCT=0.2
```

銘柄数はEdgeXのメタデータから毎回取得し、接続更新時にも再読み込みします。新規銘柄や停止銘柄を固定リストへ手入力する必要はありません。

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
