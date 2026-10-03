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
python -m unittest analysis_terminal.test_outcomes analysis_terminal.test_storage analysis_terminal.test_setups analysis_terminal.test_entry_ui analysis_terminal.test_lifecycle analysis_terminal.test_comparison analysis_terminal.test_tracking analysis_terminal.test_replay analysis_terminal.test_replay_review analysis_terminal.test_entry_diagnostics -v
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

## setup lifecycle（v19.0.4）

`setup_lifecycles` / `setup_lifecycle_events`に監視状態と遷移を追記します。
GET `/api/setups?limit=100&ticker=BTCUSDC` は状態・理由・観測時刻と、同setupの現行／Shadowの結果を返します。
`OPEN`は監視中、`READY`は現行READYの成立履歴、`EXPIRED` / `INVALIDATED`は監視終了です。
現在のエントリー可否は従来のscreener・ENTRY NOWで判断します。

終了根拠は既存コードと同じです。breakoutが直近roll_max_age本の4H検索窓から外れたらEXPIRED。
trend条件の崩壊、別setupへの交代、ブレイクが選択対象から外れる、stop_valid=falseはINVALIDATED。
structural targetが近すぎるSTRUCTURE_WAITだけでは終了させません。
同時に複数の根拠がある場合は検索窓→trend→setup交代→stopの順で理由を表示します。
終了時刻は市場データで初めて確認した観測時刻であり、欠測中の終了時刻や足内順序を推測しません。

最新の確定4H足と15M足が揃わない銘柄・欠損銘柄・DATA_WAITは更新を保留します。
同setupが既存条件で復活した場合はREACTIVATEDを履歴に残し、同じIDを再利用します。
初回の現行／Shadow成立時刻は保持し、追加エントリーは生成しません。
既存のID付きpaper/Shadow記録は、その本来の成立時刻を保持して読み込みます。
ブレイク情報のない旧記録は推測で紐付けません。読み込みだけでは市場確認済みとは扱いません。

監視の終了はpaper取引の決済ではありません。既存のOPEN/TP/SL/AMBIGUOUS結果、価格、
signal close+1ms、評価器、昇格集計を変更せず、終了したsetupのTP/SL追跡も継続します。
collectorでmarket/paper/Shadow保存後に更新し、GET APIは記録を変更しません。
旧テーブルの削除・再構築は行わず、既存snapshot・購読・シグナルを保持します。
遷移は15Mごとの観測であり、観測間の短い状態変化をすべて捕捉するものではありません。

## 隔離した過去検証（v19.0.7）

GET `/api/replay-review` と検証画面内のREPLAY欄は、静的な `replay_latest.json` を読みます。
RETROSPECTIVEと明示し、ライブDB・Shadow昇格集計・ENTRY表示・通知には混ぜません。
本番collectorは過去検証を実行しません。再実行は別の作業ディレクトリで行います。

```bash
python -m analysis_terminal.run_replay --end-ms 1790996400000 --days 7 --output /tmp/edgex-replay
```

公開getKlineのLAST_PRICE履歴をページ取得し、銘柄・時刻・OHLC・重複・改訂競合を検証します。
4H/15Mの確定180本を各時点で切り出し、同じanalyze_contractにその時点の時計を渡します。
候補判断に未来足を渡さず、結果判定はsignal close + 1ms以後の足だけです。
setupごとに各戦略の初回Entryを保持し、期間前の成立と欠測中の不明な初回Entryを除外します。
同時TP/SLはAMBIGUOUS、欠測後の結果は未検証として正式指標から除外します。
取得時の公開データとSHA256、条件・取得失敗・観測点除外を成果物に保存します。

最初の固定期間は2026-09-26 03:00〜2026-10-03 03:00 UTC、取得対象182銘柄、取得失敗0。
有効観測点108,492 / 122,304（88.7%）。現行2 setup / 1確定でINSUFFICIENT SAMPLE。
Shadow235 setup / 179確定（TP33、SL146）、平均R -0.4469、PF 0.4521、最大連敗29。
LONG/SHORTとも平均Rは負。両戦略で確定した同一setupは0件で、TP変更の因果効果は判定できません。
候補数の増加は確認できましたが、fixed 2Rの利益改善を支持しません。本番昇格は見送り、
ライブShadowは条件を維持して収集を継続します。戦略パラメータの再最適化は行っていません。

現在取引可能な銘柄のみの選択、改訂済み履歴、180本の固定指標窓、欠測の除外、
手数料・Funding・スリッページ未反映に偏りがあります。235件の独立性も保証しません。
過去検証の179確定はライブのresolved>=20条件へ加算しません。
元コード・公開データ成果物: [research run 37093101900](https://github.com/y-ai-lab/edgex-breakout-alert/actions/runs/37093101900)。
取得元SHAとrun/artifact IDは配布結果のprovenanceにも残しています。

## Entry時点の損失要因（v19.0.8）

`entry_diagnostics.py` は保存済みの公開元足とresearch reportから、全235 Shadow Entryを再構成します。
全182元ファイルのSHA256とanalyze_contractのfingerprintを検証し、各EntryのID・価格・SL・TP・
Measured Move余地が再現できなければ中断します。未来足をEntry判断へ渡さず、DBや通知にはアクセスしません。

```bash
python -m analysis_terminal.entry_diagnostics --source /path/to/research-artifact --output /tmp/edgex-entry-diagnostics
```

確認足実体・roll超過・EMA間隔・ストップ距離・Measured Move余地・出来高比を固定区分で比較します。
方向、ブレイク確定後の経過、Entry日、retest時系列も表示します。最適化した閾値ではありません。
API `/api/entry-diagnostics` は静的な `entry_diagnostics_latest.json` を読み、REPLAY内に表示します。
OPENも分母に残し、平均R/PFは検証済み確定結果のみ。少数群はINSUFFICIENT SAMPLEと表示します。

直近4本のretest判定には、4Hブレイク足が確定する前の15M足が含まれ得ます。
235件中87件は、その4H足の確定後に始まるretest足がありませんでした（63確定、TP6/SL57、平均R -0.7143）。
確定後・確認足のみは23件（17確定、平均R -0.2941）、確定後・確認足より前にretestがある群は125件
（99確定、平均R -0.3030）。確定後のretestがある群も利益改善を示していません。
固定した数値区分でも、20件以上確定した群はすべて平均Rが負でした。

これは既に結果を見た同じ7日間の記述分析です。OHLC内の順序は不明で、4Hブレイク足内部の押し戻しと
確定後のretestを区別する分類です。後者だけを採用する新戦略のバックテストではありません。
最初のEntryを除外すると後のEntryが成立する可能性があるため、単なる部分集合の成績は新ルールの成績になりません。
本番条件とライブShadowを変更せず、次は4Hブレイク確定後にretestが始まる条件を別Shadowとして
時系列から再検証するのが優先です。手数料未反映・銘柄選択・相関・OPENの観測期間差にも注意が必要です。
