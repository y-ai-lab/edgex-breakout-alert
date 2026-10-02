# EdgeX Analysis Terminal

通知Botとは別Railwayサービスで動く分析ツールです。注文API・Telegram認証情報は使いません。

- Market Screener: EdgeX全取引可能銘柄を4H→15Mでスキャン
- Ticker Analysis: EMA20/50、ATR、breakout、retest、confirmation、SL/TP/RR
- Risk Calculator: Equity / Risk% / Entry / SLから最大損失基準の枚数を計算

Screenerの全市場スナップショットは4分キャッシュします。

Stages:
- READY
- CONFIRMATION_WAIT
- RETEST_WAIT
- RR_WAIT
- BREAKOUT_WAIT
- TREND_WAIT
- STRUCTURE_WAIT

このサービスは通知Botの状態DBやTelegramには接続しません。

## 結果判定の検証

Python 3.12とNode.jsが必要です。

```bash
python -m pip install -r analysis_terminal/test-requirements.txt
python -m unittest analysis_terminal.test_outcomes analysis_terminal.test_storage -v
python -m unittest discover -s tests -v
```

APIテストは一時SQLite、市場データのfixture、Push送信のmockを使います。
実際のPush配送と本番API確認は別途行います。

v19.0.1の結果評価では、signal close + 1msを維持してsignal足を除外し、
最初の次足から判定します。足途中の候補イベントは次の完全な足から判定し、
除外した区間があるため履歴不足として扱います。足の欠損も履歴不足です。
TP/SLが同じ足で到達した場合はAMBIGUOUSとし、決着後の足をMFE/MAEに含めません。
決着足のMFE/MAEはOHLCからの範囲であり、足内の決着前後の順序は推測しません。

旧方式の確定結果は上書きせず保持します。未確定の旧結果はlegacy_resultに保持して
再評価します。Shadowの昇格集計とブラウザの正式シグナル集計には、
evaluation_version=2かつcoverage_complete=trueの確定結果のみを使用します。
旧方式の確定結果の再検証には、発生時から決着までの完全な履歴が必要です。
READY戦略・min_rr・本番通知条件は変更していません。
