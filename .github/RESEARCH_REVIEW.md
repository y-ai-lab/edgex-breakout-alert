# 固定した週次研究の実行サマリー

2026-10-07。エントリー回数を増やす既存の `confirmed_pullback_cost_2r_v1` 研究について、取得後に自動レビューを作る。戦略、評価器、protocol、通知、DB、Web UIを変更しない。

- GitHub Actionsの「EdgeX frozen strategy research」実行画面に、現行・Shadow・押し目指値案の仮想約定数、確定数、TP/SL、勝率、net平均R、PF、MFE/MAE、最大連敗を表示する。
- 資金制限後の約定数・実現損益・最終ROIと、現行に対する仮想約定純増も表示する。保有・不確定が残る場合は、元の評価器が返したROI不明を維持する。
- 確定足のみの登録済みUTC週を確認し、台帳からmetrics、資金計算、候補差を再検証する。週の完了を偽った場合や、条件・台帳・費用・集計の変更は失敗させる。
- 日次の途中結果は `PROVISIONAL_…`。同じ週の更新に同じcohort keyを付け、候補・確定数を過去の日次報告へ足さない。過去の開発・検証期間や別週の件数も合算しない。
- 取得失敗市場がある場合は `DATA_QUALITY_BLOCKED` とし、観測した数値と取得範囲を残す。指標が存在しない場合は「不明」、20確定未満は `INSUFFICIENT SAMPLE`。
- 新しい検証日が終わる前は `WAITING_FOR_FIRST_COMPLETED_DAY`。完了後も待機報告しかない場合は `COLLECTION_NOT_CURRENT`、取得が未完了なら `NO_VERIFIED_RESULTS`。未収集を0件の結果として扱わない。

判定は既存評価器の `decision` による単独週の固定条件検査。本番昇格の承認や、複数期間の優位性を証明するものではない。正の平均Rだけで最終ROIを推測しない。実注文・Push・Telegramを呼ばず、GitHubの実行サマリーとartifactにだけ保存する。

既存の期間選択・収集・7日cohort凍結・OPEN追跡・欠損補完・最終cutoff回復・90日保持を維持する。初回対象は2026-10-07 00:00–10-08 00:00 UTC、初回収集予定は10-08 00:30 UTC（日本時間09:30）。GitHubのscheduleは遅延する場合がある。初回7日終了は10-14 00:00 UTCで、未確定OPENは元のcohortとして7日間追跡する。

出力は `strategy-output/review/decision-summary.json` と `.md`。元台帳のSHA、公開市場manifestのSHA、登録protocolと評価器のSHAも残す。入力の上書きを禁止する。

```bash
python -m unittest discover -s .github/scripts -p 'test_*.py' -q
python .github/scripts/research_summary.py --research-dir strategy-output \
  --output strategy-output/review/decision-summary.json \
  --markdown strategy-output/review/decision-summary.md
```

これは公開OHLCからの仮想検証であり、板・価格tick・queue・funding・実約定は未検証。コスト込みRの単位はnet stop riskで、本番Shadowのoriginal stop distanceを使うRと直接合算しない。MFE/MAEの足内順序を推測しない。

## 資金制約の約定監査（2026-10-08）

`decision-summary.json` の `capital_admission_audit` は、検証済みの公開研究台帳に対する
元の固定portfolio関数の判断を観察する。除外理由別に、元台帳で仮約定した候補と
未約定候補を分ける。未約定候補の除外数を失われた約定数と扱わない。
予約の有無、3枠上限、同ticker、最小数量と残り建玉金額/リスクの関係も記録するが、
これらは重複する診断であり足し合わせない。時点で分かる資金だけを観察し、
予約解除、将来利益による配分、待機期限や元のパラメータの変更を行わない。

元のエンジンhash・全portfolio・全除外Counter・入力不変・候補と約定の保存を突合し、
不一致は停止する。元のmetrics/portfolio/判断と `capped_shared_setup_count:null` を保持する。
新サンプル・実約定・ROIの改善証拠ではない。固定研究口座1万USDC/1%/最大3件/合計3%/
1倍建玉金額/JST日次3%は、実運用口座3%/最大1件/日次制限なしと異なる。
GitHubには登録済み公開市場研究artifactの診断だけを出し、本番台帳をコピーしない。

### 資金制約・平均R・途中ROIの読み方

レポートは、失われた仮約定について、除外時の最小数量の建玉金額不足、
リスク予算不足、未約定予約の存在を並べる。同一取引に重複する診断であり、
合算や、予約を解除すれば取れたという因果解釈はしない。未約定候補の除外は
失われた約定に数えず、元の資金配分・予約・口座政策は変更しない。

net平均Rは決着済み仮約定1件当たりの費用込み平均損益（net stop-risk基準）。
候補・未決済を分母に加えない。口座ROIは資金制限後の既存取引が全て終了した
場合の値で、途中週の数値を最終週ROIと呼ばない。不明は不明のまま表示する。
これは表示の修正であり、元のJSON・metrics・portfolio・判定は変更しない。

## 待ち価格と未約定ファネルの再照合（2026-10-09）

日次サマリーに候補・仮約定・未約定状態・約定率・資金制限後の約定を分けて表示する。
既存の `review/pending-fill-diagnostics.json` があれば `entry_price_audit.py` が元の
公開市場ファイルと固定評価器から全診断を再計算し、保存された全候補・診断・SHA・
metrics・portfolioとの完全一致を要求する。不一致は停止し、値を合わせて修復しない。
診断未記録は `NOT_RECORDED` / summary null として表示し、価格未到達0件と扱わない。

元の候補台帳を変更せず、期限内未到達・ロール水準の反対側にある待ち価格・確認時点で
既に過ぎた構造TP・期限切れ後の固定観測を集計する。遅い価格接触は約定・利益ではなく、
4本期限を延長しない。距離Rは価格stop-distanceでありnet stop-riskの損益Rとは別。
追加期間取得・後付けパラメータ選択・ticker選別・本番台帳の輸出・本番条件変更を行わない。
既存の単独週判定、サンプル不足、元のmetrics/portfolio、最終ROI nullを維持する。
この再照合は同じ公開研究artifactの監査であり、新たな独立サンプルではない。
研究サマリーの変更も `Analysis terminal safety checks` の対象に含め、研究feature branchと
mainで全テストを実行する。週次収集workflowのbytes・固定期間は変更しない。

## VWAP研究CIの固定範囲（2026-10-08）

初回開発時の「本番全ファイルが同じ」というチェックは、後の正当なUI・認証・
ON/OFF実装まで研究条件変更として拒否していた。研究用チェックは登録済み
analyze_contractとパラメータのfingerprint、既知のscanner hash、元の評価器・
capture・protocol・週次workflowのbytes、事前登録したVWAP条件・期間、既存KILLを検証する。
Webサーバー全体・UI・注文認証コードの旧版との一致は要求しない。
それらの安全性は通常の全回帰テストで確認する。条件変更を許可するものではない。

実際のworkflow内チェックを実行する回帰テストで、UI・認証更新の受入れと、
評価器・RR・分析関数・期間の変更、棄却モデルの再稼働の拒否を確認する。
公開市場archiveから全台帳を再現する後続チェック、未使用期間、費用、
最初のartifact保持、サンプル基準を維持する。市場データ取得や成績は変更しない。

## 予約資金の時間監査（2026-10-10）

`reservation_time_audit.py` は登録済み公開研究台帳の元portfolioを実行し、
そのイベント後の予約金額・risk・枠数をPENDING/FILLED/UNCERTAINに分けて観測する。
元の関数・入力・出力一致、全イベント数、起点・締切・engine SHAを照合し、
不一致は停止する。候補登録後の未約定予約、元の足終値時刻での約定計上、
出口/期限切れによる解除、曖昧/欠損の拘束を同じ時系列で追跡する。

`decision-summary.json` の `reservation_time_audit` に金額×時間、枠×時間、
観測期間全体の時間加重平均、未約定の予約金額時間比率を追加する。
指標は予約の記述分析であり、資金を解除すれば取引・利益が増えるという因果効果、
板の実約定、口座ROI、代替配分の成績ではない。旧summaryはそのまま保持・表示する。
同じ週の更新を独立サンプルとして合算せず、本番記録をGitHubへ複製しない。
元のmetrics/portfolio/decision、期限、条件、通知、注文、本番口座policyを変更しない。

## 未計上費用に対する決着済み利益余地（2026-10-10）

`cost_headroom_audit.py` は元の費用込み損益を変更せず、決着済み取引だけに
同じ追加総費用をentry建玉金額のbpsで課した場合のゼロ損益点を記述する。
各取引の元net stop-riskで正規化したR集計と、元portfolioが採用した数量による
現金集計を区別する。元engine hash、metrics、portfolio、入力不変を照合して不一致で停止。
集計が赤字/ゼロ・決着なしなら正の費用余地はnullとし、費用0や利益保証と解釈しない。

これは実際のFunding履歴・金利・板約定・mark価格・保有中の費用の検証ではない。
OPEN/AMBIGUOUS/DATA_GAP、元口座ROI null、固定した費用・SL/TP・net stop-riskと
判定を保持し、不確かな資金を解放しない。追加コストで数量を再計算した代替口座も作らない。
同じcohortの監査であり新サンプル・将来の費用耐性・昇格根拠ではない。
GitHubへ出すのは登録済み公開市場研究の集計のみ。本番レスポンスは複製しない。

## 決着の完了範囲（2026-10-10）

`decision-summary.json` の `outcome_completion` は、再検証済みの同じ台帳から
候補・仮約定・TP/SL決着・OPEN・未約定PENDING・品質保留を区別する。
AMBIGUOUS / DATA_GAP / INVALIDATED_GAPは、約定前の保留も含めて明示する。
決着率の分母は仮約定で、期限切れや未約定候補を決着済み取引に加えない。
候補なしの率はnull、全件完了と20決着のサンプル充足は別に表示する。

途中週・未決済・待機・品質保留・市場取得失敗が残る場合はcohort全件完了としない。
資金制限後の口座の終了と、制約前の全候補の決着も分ける。元のROI null、
予約資金・不確定資金・metrics・portfolio・固定decisionは変更しない。
資金制限後の純約定差が0以下なら、約定増の確認ができていない旨も表示する。
これは同じ公開研究artifactの説明であり、新サンプル・新判定基準・本番昇格ではない。
旧サマリーは再生成・上書きせず、追加欄がなくても読み取れる。
