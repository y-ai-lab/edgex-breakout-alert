# 模擬執行 v1 — PAPER ONLY

この機能は仮想口座の検証。実注文、送金、APIキー作成、ステーキング、実資金の利用を行わない。実注文へ切り替える設定や注文transportは存在しない。

入力は現行READYのライブ観測だけ。Shadow、旧シグナル、リプレイ結果は入力にしない。戦略・通知・既存結果判定は変更しない。結果は本番昇格標本へ加算しない。

## 試験条件

- 仮想初期現金10,000 USDC。未実現損益は現金に加えない。
- 1回の予定損失予算は発注時の仮想現金の1%。SL価格での仮定滑りと往復手数料を含む。窓開けの追加損失は保証できない。
- 最大3つの注文・建玉。銘柄単位の重複建玉なし。合計の残余損失予算3%、総想定元本は現金の1倍以下。
- JST日次の仮想現金減少が日初現金の3%に達したら新規注文停止。日付変更だけでは自動再開しない。
- 片道手数料5bps、滑り2bpsを試験用に仮定する。EdgeXの実際の手数料ではない。Funding、板厚、部分約定は未対応。
- 検知した15M確認足の確定後から120秒未満、キャッシュ取得後120秒未満が必要。

## 時系列と約定

1. 検知時刻でsetup初回のPENDINGを保存し、数量・損失予算・元本を予約。signal candle以前を価格判定に使わない。
2. 検知後の次の15M足の始値で仮想約定。最大15分の意図的な待機を含む粗いモデルで、即時市場注文の性能は再現しない。始値だけが必要なため、形成中の足からはopenのみを使える。
3. 当該足の時刻は注文作成より必ず後。約定でSL/TPの価格順序とRR>=現行min_rrを再確認し、未達ならREJECTED。SL/TPは変更しない。数量は予約時より増やさない。
4. 約定後は連続した確定足だけで結果判定。形成中のhigh/low、抜けた足の結果は使わない。カーソルの抜けはHISTORY_GAPとして保持し、新規注文を止める。
5. 足内でTP/SL両触れは、始値によらずAMBIGUOUS。損益を推測せず口座を停止し、現金総額をnullにする。
6. SLを飛び越える始値は、SL価格より悪い始値＋滑りで決済を模擬。想定損失予算を超え得る。
7. 約定データを注文作成から30分以内に取得できなければEXPIRED。後日の復帰で過去約定を追加しない。

## 保存と運用

既存SQLiteへsimulated_ordersとsimulated_accountをCREATE IF NOT EXISTSで追加する。setupごとの主キーとBEGIN IMMEDIATEで重複・並行更新を防止。初回有効化時点の保存済み現行シグナルはBASELINED（対象外）。再起動・再migrationで状態を保持する。

既存collectorの公開WSキャッシュを利用する。履歴回復は公開RESTのfetch_historyだけを1周期最大2要求・各256足に制限。復旧取得は未処理カーソル以降で、実注文APIを呼ばない。エラーは模擬口座を停止し、既存collector・通知の処理を継続する。

v19.0.19では、模擬建玉の履歴回復に渡す公開HTTP callableをCLIENT._get_json_syncへ修正した。クライアント本体を渡すとTypeErrorになり、HISTORY_GAPを公開履歴で回復できなかった。実履歴アダプターを通す回帰テストを追加し、記録済みREADYと公開足の欠測からSLまで復旧すること、不正価格種別・取得失敗ではカーソルと現金を保持することを確認する。上限・期限・足の範囲・戦略条件は変更していない。

v19.0.20では、PENDINGの約定対象足が欠測・不正だった場合も、足の確定後かつ元のexpires_ms以内だけ公開履歴で復旧する。形成中の足・期限超過の注文には取得しない。復旧開始は元のexecute_msで、signal candleや検知前の足を追加しない。約定時の価格・数量・RR再検査と停止条件は既存の模擬処理を通す。fill_observed_msには復旧を実際に観測した時刻を保存し、filled_msへ遡及させない。公開取得失敗・空の応答・不正価格種別ではPENDINGと予約・現金を保持し、期限切れ後の通常周期でEXPIREDにする。建玉の復旧と合算して1周期最大2要求・256足の上限を維持する。期限・戦略・本番通知・DB schemaは変更していない。

GET /api/paper-execution?limit=50 は読取専用。表示件数が集計母集団を変えない。画面はAPIタブ。PAPER ONLY、コスト仮定、試験モデルの制約を常時表示。

v19.0.21では、同APIのtrackingを全注文から読取集計し、WAITING_READY / WAITING_FILL / TRACKING / DATA_INCOMPLETE / PAUSED / AMBIGUOUS / NOT_STARTED / COLLECTOR_STALEを区別する。active_status_countsはPENDING・OPEN・AMBIGUOUS、data_issue_countsは有効な注文のHISTORY_GAP・DATA_ERROR・MISSING_FILL_CANDLEだけを数える。limit=1でも、一覧外の古い建玉の履歴不足を見落とさない。API画面は履歴不足・停止・更新不明を「要確認」とし、模擬約定待ち・建玉・履歴不足の件数を表示する。tracking未提供・不正応答も正常扱いしない。これは最後の処理時点の読取診断であり、READY判定・新規注文受付の許可・本番取引への昇格判定ではない。更新が未来時刻の場合も鮮度不明として扱う。DB・注文・戦略・収集周期は変更しない。

## 観測イベント履歴（v19.0.18）

simulated_order_eventsへ、setup IDに紐づく状態・quality・reasonの変化を追記する。通常周期の無変化ではイベントを増やさない。注文・費用・口座・イベントは同じトランザクションで保存し、失敗時はまとめてロールバックする。同じ周期でPENDING→OPEN→TP/SLになってもOPENを残す。

observed_msは処理が観測した時刻。market_msはOPENでは約定足の開始、TP/SL/AMBIGUOUSでは判定足の確定時刻。過去足の時刻を観測時刻へ置き換えず、足内の決済時刻を推測しない。各イベントに当時の注文スナップショットを保持する。

originのLIVE_CYCLEは模擬処理による変化、OPERATOR_CONTROLは管理CLIの取消、ACTIVATION_BASELINEは初回有効化時の対象外登録。移行前の注文は現在状態のみをMIGRATION_SNAPSHOTとして移行時刻に1度だけ保存し、過去の待機・約定履歴を生成しない。口座の元のactivated_ms・policy・残高・停止状態は保持し、event_tracking_started_msを別に記録する。

```
GET /api/paper-execution/events?after_id=0&limit=50
GET /api/paper-execution/events?order_id=current%3Asetup-v1%3A...&after_id=0&limit=50
```

GETのみ。idの昇順で最大200件、has_more・next_after_idで続きへ進む。order_idでsetupを絞る。total_countとorigin_countsはページ件数やカーソルで変えず、移行スナップショットを新規約定と区別する。読取によるスキャン・通知・DB更新は行わない。イベント件数を取引数・戦略昇格標本へ加算しない。新しいUIタブは追加しない。

停止・再開は認証されたサーバー管理環境からのみ実行する。公開書込APIは追加しない。

```
python -m analysis_terminal.paper_execution_control status --db /data/analysis_terminal.db
python -m analysis_terminal.paper_execution_control pause --db /data/analysis_terminal.db
python -m analysis_terminal.paper_execution_control resume --db /data/analysis_terminal.db
```

停止は未来の未約定注文を取消し、既存仮想建玉の結果追跡を継続。AMBIGUOUSや当日損失上限のままでは再開できない。DB自体のリセット、履歴削除は行わない。

日次現金変化は仮想約定足開始のentry feeと、出口判定足確定時のgross PnL−exit feeをJST日付へ割り当てる。15M足内の正確な出口時刻は推測しない。MFE/MAEは終端足のOHLC範囲を含む境界値で、約定直前・直後の足内順序を再現した値ではない。

検証: python -m unittest analysis_terminal.test_paper_execution analysis_terminal.test_paper_execution_events analysis_terminal.test_paper_history_recovery analysis_terminal.test_paper_status analysis_terminal.test_entry_ui -v。模擬執行を通して実注文への安全性や収益性が証明されたことにはならない。
