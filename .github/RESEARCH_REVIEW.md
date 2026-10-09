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
