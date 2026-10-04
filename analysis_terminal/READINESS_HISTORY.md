# 日別READYファネルと成立頻度

`GET /api/readiness-history?days=7`（1〜30日、JST）。保存済み市場スナップショットと、現行／Shadowの保存済み初回シグナルを読むだけのAPI。市場取得、結果判定、通知、昇格は実行しない。

- ファネルは現行判定順序に沿う排他的なstageの集計。`DATA_WAIT → TREND_WAIT → BREAKOUT_WAIT → RETEST_WAIT → CONFIRMATION_WAIT → STRUCTURE_WAIT → RR_WAIT → READY`。件数は銘柄×観測枠で、独立取引数ではない。
- 15M確認通過率は `（STRUCTURE_WAIT + RR_WAIT + READY）/ リテスト通過`。確認後脱落率は `（STRUCTURE_WAIT + RR_WAIT）/ 確認通過`。分母ゼロはnull。
- 成立頻度は既存のcomparison.cohortを使用し、全履歴でsetup初回を選んでから期間を絞る。同tickerの別setupは別件。同setupの後続観測、旧ticker単位の記録、不正なidentityは除外する。
- シグナルはcreated_msのJST日付へ所属。signal close + 1msを変更しない。日別resolved等は、その日に成立した群の現在までの結果であり、その日に決済した件数ではない。
- 15分bucketを一意な観測として数える。当日は途中集計。欠測枠と不正なstage内訳を表示し、未観測を市場での不成立と解釈しない。
- 既存スナップショットはそのまま利用。新規保存にだけreadiness_shadowを追加し、Shadow成立時に現行STRUCTURE_WAIT／RR_WAITだった件数も記録する。旧観測にこの値を推定補完しない。DB schemaの変更はない。
- サンプル状態と指標は既存comparison.metricsを使う。日別集計は昇格判定に使わず、条件変更や自動昇格は行わない。

画面: 「検証と改善」冒頭の「READYの日別ファネルと成立頻度」。7／14／30日を選択できる。

検証: `python -m unittest analysis_terminal.test_readiness_history -v`。JST境界、欠測、反復setup、全履歴での重複排除、排他的stage、Shadowの未保存と実測ゼロ、API無書込、UI表示と非同期競合を確認。
