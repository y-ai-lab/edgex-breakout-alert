# 固定戦略研究レビュー（2026-10-21）：最新成果物が取得不能

**最新Actionsの元台帳を取得できず、今回の研究判断は `BLOCKED_LATEST_ARTIFACT_UNAVAILABLE`。採用・KILLへの更新は行わない。** これは証拠の取得状態であり、登録した戦略の判定条件を変更するものではない。

main `4022dd52d16241d71df0d87b6aadbd4da3ade782` を確認。前回からの差分は公開研究集計2ファイルだけで、scanner・評価器・capture・protocol・workflowのbytesは不変。過去限定protocol、旧KILL、元の期間・費用・確認・RR・SL/TP・4本期限は保持する。

## 最新runはSUCCESSだが成果物は欠落

最新完了main run [39372817340](https://github.com/y-ai-lab/edgex-breakout-alert/actions/runs/39372817340)（10/20 UTC、headは上記main）のchecks/researchはSUCCESS。研究job `118464549774` のログには以下がある。

- 10/14–20 UTC、途中6日。10/14–21の完全週ではない。
- upload-artifactはSUCCESS、artifact ID `12242590310`、報告ZIP SHA256 `d8af9f3c8150ab21b282d6490f0796db487d8af3617764b0d3544e369ade78b7`。
- 現在のrun artifact一覧は、専用toolとRESTの両方で0件。ログのIDを指定したダウンロードもHTTP404。ZIP bytes/hashを検証できない。削除・期限切れ・可視性などの原因は未確認で、特定の操作が原因とは断定しない。
- `review/decision-summary.json`、元ledger/source、execution-funnel、高費用計算を取得できない。ジョブログの集計だけで全台帳の再現・データ品質・実行可能性を認定しない。最新の検証済みmetrics/portfolio/登録rule decisionはnull。

ログ記載値は次のとおり。**元台帳未検算の参照値**で、検証済み最新バックテスト成績や独立した将来サンプルではない。

| ログ記載モデル | 候補 / 仮約定 / 決着 | TP / SL | net勝率 | Avg net R / PF | MFE / MAE price R | 最大連敗 |
|---|---:|---:|---:|---:|---:|---:|
| 現行・次足始値 | 11 / 11 / 11 | 3 / 8 | 27.27% | −0.1364 / 0.8124 | 1.3466 / 1.0540 | 5 |
| Shadow v2・次足始値 | 332 / 0 / 0 | 0 / 0 | 不明 | 不明 | 不明 | 不明 |
| 固定押し戻りnet2R | 333 / 19 / 18 | 5 / 13 | 27.78% | −0.1667 / 0.7692 | 1.1111 / 1.0274 | 7 |

ログの新案状態はEXPIRED310、INVALIDATED4、SL13、TP5、AMBIGUOUS1。現行と共通6、新案のみ13、現行から欠落5、制約前純差+8、制約後純差−5、制約後の共通setup件数null。新案のみ13を純増と呼ばない。ログ上の資金制約後は、現行8仮約定/8決着、実現+181.6896USDC、active0/uncertain0、ROI+1.81690%。新案3仮約定/3決着、実現−107.0438USDC、active0/uncertain0、ROI−1.07044%。いずれも元口座台帳の再計算は未実施で、途中週の値を登録週全体や実口座の最終ROIへ読み替えない。高費用metricsは取得不能で不明。

ログの18決着は元の20決着基準未満で、仮に集計が一致してもそれだけでKILLとはしない。さらに今回は元sourceが欠けているため、ログ上の負成績から元のdecision-summaryを再生成したり、前日の正の平均Rへ差し替えたりしない。新仮説・本番条件変更・昇格は行わない。

## 封印週のCI追跡も原本を取得できていない

同じjobログのfollowupは `MISSING_FROZEN_COHORT`（元週10/7–14）、recoveryは `WAITING_FOR_COMPLETED_FOLLOWUP`。最初のmain run `38514910609` のartifact一覧も現在0件だった。以前確認した最初の封印artifact `11871366807` とSHA、最初のmain SHA `3393db09c5ea9447455d6935f31cfc938b65c50a` は保持する。後のartifactを最初の原本として扱わない。

追加7日締切は10/21 00:00 UTCで到達しているが、レビュー時の最新完了runは10/20分。当日のcron遅延・未完了を0成績や回収成功としない。最初のmain最終outcome artifactは未確認。回収を行う場合も元のcutoffまでに限り、実行日まで延長しない。原本を失ったCIは再生成で補完しない。保持期間設定だけで原本の実在・回収成功を保証しない。

## 保持済み原本の再検算は最新成果物の代替にしない

作業環境には最初の封印ZIP・全source ZIPと、前回取得したartifact `12164327517`（run `39202123721`、10/19 cutoff）が残っている。ZIP/hashと展開した全ファイルを、以前Gitへ記録した値と再照合した。これは原本の再生成ではない。現在GitHub上に存在するという証明でもない。

- 元の封印週182市場と**保持済みの10/14–19途中5日**を現mainの固定コードで再生し、全ledger・metrics・portfolio・funnel・元summaryを再現。別実装の価格・費用・時系列口座計算も一致した。
- 保持済み10/19までのfollowup9市場・4,320本を独立検算し、公開RESTもその保存cutoffまでで一致、改訂・欠損0。10/20以降の新しい価格を使って最新の欠落成果物や最終outcomeを作り直していない。
- 元948候補の非OPEN937行とentry/SL/TP・期限・約定・既存決着は不変。保存済み既約定OPEN11行の出口だけを検算し、未約定PENDINGを追加期間で埋めず、不確定資金を解放しない。
- これらは過去取得時点の再現であり、最新6日集計や10/21の最終出口結果とは別。元週・後追い・回収・日次更新を独立サンプルとして加算しない。
- exact mainのテスト1,497件成功（分析1,118、研究354、tests25）。テスト成功は欠落artifactの全ledger再現や収益性の証拠ではない。今回のGit差分は公開研究集計2ファイルだけ。

過去限定VWAPの2つの35日間の原source・台帳・初回sealは今回もローカル不在で、再検算を `MISSING_LOCAL_ORIGINAL_ARCHIVE` として停止。以前の有限バックテスト終了・`BLOCKED_DATA_QUALITY`・採用根拠なしを保持し、保存済み集計を今日の再現結果と表示しない。週次原本のローカル保持とは区別する。

研究口座は元の独立1万USDC・1%risk・同時3・合計3%・JST日次3%・1倍notional。Rは費用込みstop-risk、MFE/MAEは価格stop-distance、口座ROIは資金配分計算で別。Funding・板/queue・OHLC内順序・実約定品質・相関・生存者バイアスは残る。元のKILLや未決済・不確定資金・ROI nullを変更せず、勝率・利益・実用化完了を保証しない。

本番API・捕捉台帳・約定前proof・週別/連続研究口座の最新読取と独立検算は作業環境内にのみ保存し、GitHubへエクスポートしない。最新Actionsが取得不能でも、本番捕捉Shadowで穴埋めしたり、実注文の成績へ合算しない。本番の売買・collector起点・UI・注文/署名/arm/pause/ON/OFF・通知・口座policy・DB・Push購読は変更しない。
