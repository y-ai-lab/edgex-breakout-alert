# 保持済み原本による固定締切監査

GitHub artifactの取得不能を、別の結果への差し替えで埋めない。今回は**以前取得した原本ZIPが残る10/7–14の1cohortだけ**を監査した。最新6日研究artifactや最初のmain最終outcome artifactを復旧したとは扱わない。消失原因は未特定で、リポジトリ内にartifact削除処理は見つからなかった。

条件はコード実装・監査実行前のmain commit `3660473686ddb2d2d356e85b3398df439593dae4` と [protocol](retained_cohort_audit_protocol.json) へ固定した。既閲覧の原本を用いる継続性監査で、新仮説・未使用期間・独立サンプルではない。

## 実装した監査

`.github/scripts/retained_cohort_audit.py` は通信やGitHub書込みをせず、次を検証する。

1. 最初の封印artifact `11871366807`、全市場source `11872175802`、保持済み追跡artifact `12164327517` のZIP SHAを事前登録値へ固定。元のreport/source/sealと全候補を照合する。
2. 元の182市場・全台帳を固定評価器で再現してから、保持済み10/19 cutoffの全followupを再現する。hash、元ledger、口座、既存決着の不一致は停止する。
3. 元の既約定OPENが保持済み結果で既にOPEN以外となっていることを要求する。OPENが残れば後日の価格やTP/SLを推定せず停止する。未約定PENDINGとAMBIGUOUS/DATA_GAPは元の打切り・資金拘束を維持する。
4. 元の追加7日締切10/21 00:00 UTCに到達した後だけ、**同じ保存足だけ**で元評価器を固定締切まで実行する。変えてよいのは締切/完了メタデータだけで、全948行、MFE/MAE、metrics、portfolioの完全一致を要求する。
5. 新しい排他的な作業先へ `LOCAL_RETAINED_ORIGINAL_AUDIT_NOT_GITHUB_FINAL_ARTIFACT` として保存する。既存ファイルを上書きせず、原本を再生成・再アップロードしない。

元のOPEN11件は保持済み足の範囲で既に終了していたため、新しい価格なしでこの限定監査が成立した。**観測締切の監査完了と、全結果・資金の確定は別**。`followup_complete:true` は登録された観測期限についての値で、未確定を解消した意味ではない。`actual_evaluated_source_end_ms` は10/19 cutoffのまま明示する。

## 結果：成績は変わらず、実用性未達

元948行は保持済み結果と一致し、新規価格0・新規候補0・新規約定0・新規決着0。別実装でも費用・metrics・時系列口座計算が一致した。今回の監査を元週や日次追跡へ加算しない。

押し戻り案は37仮約定/32決着、14TP18SL、勝率43.75%、費用込みAvg R+0.3125/PF1.5556、最大9連敗のまま。AMBIGUOUS5、打切りPENDING1を保持する。制約後13仮約定/12決着、実現+726.5203USDCだがactive2/uncertain1、最終口座ROIはnull。共通/新案のみ/現行から欠落は7/30/9、制約前純差+21でも制約後純差0。採用根拠にはならない。Rは費用込みstop-risk、MFE/MAEは価格stop-distance、口座ROIは資金計算で別である。

このcohortの元条件・SL/TP・期限・約定・既存決着・資金・KILLは変更していない。最新6日artifact、現在GitHubから取得できない最初のmain最終outcome、過去限定VWAP原本の欠落も未解決として残す。後の有利な結果に差し替えず、これは本番捕捉Shadowや実約定の昇格サンプルではない。

## 実行と検証

```bash
EDGEX_EXEC_MODE=OFF python .github/scripts/retained_cohort_audit.py \
  --frozen /path/to/11871366807.zip \
  --source /path/to/11872175802.zip \
  --retained /path/to/12164327517.zip \
  --output /path/to/new-exclusive-audit
```

ローカル1,512 tests成功（分析1,118、研究369、tests25）。新規15テストはLONG/SHORT、未決済OPEN停止、PENDING打切り、AMBIGUOUS/欠損資金拘束、締切、hash・JSON・ZIP path不正、結果改変、上書き拒否、CLI起動を検証する。原本の全市場再生と保存足による出口計算・独立口座検算は、公開市場の実データでも成功した。テストや監査を追加取引サンプルへ数えない。

既存 `analysis-terminal.yml` の `.github/scripts/**` 安全性CI対象で、凍結した `strategy-research.yml` や既存評価器/capture/protocolのbytesは変更しない。本番ルール、UI、collector、DB、Push/Telegram、口座policy、注文/署名/arm/pause/ON/OFFも変更しない。元ZIPや全ledgerは作業環境内に保持し、Gitへ出すのは研究コード・テスト・事前条件・公開集計だけ。利益や実用化の完成は保証しない。
