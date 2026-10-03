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
python -m unittest analysis_terminal.test_outcomes analysis_terminal.test_storage analysis_terminal.test_setups analysis_terminal.test_entry_ui -v
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

## setup identity（v19.0.2）

`setup-v1:ticker:direction:breakout_time_ms:breakout_level` を4Hブレイクから生成します。
価格表記は正規化し、15M確認足やEntryの変化ではIDを変えません。
現行READYとShadowは、それぞれ同一setupの最初の観測エントリーを1件だけ記録します。
両者の結果を同じsetupで比較でき、NEAR/急接近→READYも同じIDだけで関連付けます。
再エントリー戦略やEXPIRED/INVALIDATEDによる終了は、この変更には含みません。

candidate_events / approach_eventsにはnullableなsetup_idを追加します。
過去レコードはブレイク情報が不足するため、tickerからIDを推測しません。
IDなしの記録・結果・購読・snapshotは保持し、昇格率の分母から除外します。
通知とブラウザの旧ticker状態は一度だけ基準状態へ移行し、既存候補を再通知しません。

Shadow APIのtrackedは保存された全件、setup_tracked/open/resolved/TP/SL/Avg R/PFは
識別できるsetupの最初のエントリーだけを集計します。legacy_unidentified_signalsは
保持した旧記録件数です。昇格判定はこのsetupサンプルでresolved >= 20、Avg R > 0、PF > 1
を満たす必要があり、満たしても自動で本番へ切り替えません。
このIDは同一setupの重複を防ぎますが、銘柄間の相関や統計的独立性を保証しません。

## ENTRY表示の鮮度（v19.0.3）

ENTRY NOWとglobal status barは共通のentryFreshnessを使用します。
APIのsnapshot_age_secondsに受信後の経過時間を一度だけ加え、120秒以上なら
READY/WAIT判定を「データ鮮度を確認」に切り替えます。取得時刻・鮮度が不明な場合も同様です。
120秒までの残り時間でタイマーを予約し、定期更新・通信失敗・タブ復帰でも再評価します。
非表示タブではブラウザがタイマーを遅延するため、復帰時は通信完了前に古い表示を取り消します。

資金計算の返答後に鮮度と表示の世代を確認し、古い返答によるREADYの復活を防ぎます。
カードと共通バーのクリック時にも再確認します。戦略条件・server側のREADY・Push条件は変更しません。
テストは実際のHTML内の関数を、時刻とDOMを制御して実行します。
本番HTMLにも同じ回帰ケースを実行できます。

```bash
node analysis_terminal/test_entry_freshness.js /path/to/production-index.html
```
