# 固定押し戻り研究レビュー（2026-10-18）

**採用できる改善ルールは確認できていない。** 最新の完了済みmain研究runは [39003658795](https://github.com/y-ai-lab/edgex-breakout-alert/actions/runs/39003658795)（10/17 UTC、SUCCESS）。10/14–17 UTCの途中3日分であり、10/14–21の完全週ではない。当日runの未完了をゼロ成績としない。

main `975effe66498c42f13263a6448229d986490a5e7` の固定コード・protocolを読み、最新artifact `12089689759` の元ledger、decision-summary、execution-funnel、followup、recoveryを照合した。取得時点の保存結果や前日の同じcohortを新しい独立サンプルに加えていない。

## 最新の途中3日：費用・資金制約で改善を確認できず

| モデル | 候補 / 仮約定 / 決着 | TP / SL | net勝率 | net Avg R / PF | 高費用Avg R / PF | MFE / MAE price R | 最大連敗 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 現行・次足始値 | 6 / 6 / 5 | 2 / 3 | 40.00% | +0.2946 / 1.4910 | +0.2065 / 1.3441 | 1.6037 / 0.8660 | 3 |
| Shadow v2・次足始値 | 241 / 0 / 0 | 0 / 0 | 不明 | 不明 | 不明 | 不明 | 不明 |
| 固定押し戻りnet2R | 239 / 13 / 9 | 2 / 7 | 22.22% | −0.3333 / 0.5714 | −0.4621 / 0.4059 | 0.8532 / 0.9361 | 7 |

基本費用は片道fee5bps＋slippage2bps、高費用は10bps＋5bps。Rは費用込みstop-risk基準、MFE/MAEは価格stop-distance基準で直接混ぜない。資本ROIへ読み替えない。Shadowの241候補は全てREJECTED_AT_FILLであり、候補241件の実約定ではない。

押し戻り案の状態はEXPIRED223、INVALIDATED2、SL7、TP2、OPEN3、AMBIGUOUS1、PENDING1。共通仮約定3、新案のみ10、現行から欠落3、制約前純差+7。新案のみ10を純増と呼ばない。

独立1万USDC研究口座の資金制約後は、現行5仮約定/4決着、実現+152.4126USDC、active1/uncertain0。新案2仮約定/2決着、実現−13.3338USDC、active1/uncertain0で、**純仮約定差は−3件**。両方とも最終ROIはnull（不明）。口座のactiveと約定済みOPENを混同しない。資金制約後の共通setup件数は元ledgerにないためnull。除外194候補を失った194約定と扱わない。

元の判定は `PROVISIONAL_CONTINUE_INSUFFICIENT_SAMPLE`、raw ruleは `CONTINUE_INSUFFICIENT_SAMPLE`。負の平均R/PFだが9決着で、元の最低20決着に届かない。最低件数や条件を変更してKILL/採用へ進めない。AMBIGUOUSにより全体成績も確定していない。追加の実用性評価は `NOT_QUALIFIED`。新仮説・本番条件変更は行わない。

## 元の封印週の後追いは同じサンプル

最初のmain run `38514910609`、SHA `3393db09c5ea9447455d6935f31cfc938b65c50a`、最初の封印artifact `11871366807` と元の全sourceを含むartifact `11872175802` を取得し、hashを検証した。封印report SHA256は `40d27d433b9a8c483d79efb5a258c77f82fab074aeca1f5c82369ea09954c617`。

10/7–14の封印週に対する10/17までの出口追跡は、現行16仮約定/16決着、5TP11SL、Avg R−0.0365/PF0.9469。押し戻り案37仮約定/32決着、14TP18SL、勝率43.75%、Avg R+0.3125/PF1.5556、高費用+0.2033/PF1.3613、MFE1.4483/MAE0.9487 price R、最大9連敗。前回の保存結果から新しい決着増加はなく、新しい好成績とは扱わない。

制約前純差+21でも資金制約後純差0。現行口座13仮約定/13決着、実現−10.3346USDC、最終ROI−0.10335%。新案口座13仮約定/12決着、実現+726.5203USDC、active2/uncertain1で最終ROI不明。新案のAMBIGUOUS5を解決済みにせず、不確定資金と未約定打切りの予約を解放しない。active2はOPEN2を意味しない。

元の候補948行のうち、非OPEN937行は完全一致。既約定OPEN11行の出口だけを追跡し、元のentry/SL/TP・約定・期限・既存決着を変えていない。追加7日締切は10/21 UTCのまま。今回は3日までで、最終outcome artifactの期限前。recoveryは `WAITING_FOR_COMPLETED_FOLLOWUP`、未完了を追加サンプルへ数えない。

## 再現と今回の監査限界

- 元の完全週182市場と最新途中週182市場を同じ固定コードで全ledger再現。metrics、資金計算、funnel、decision-summaryが一致。別実装の価格・費用・時系列口座計算も一致。
- followup元source9市場・2,592本を独立した出口計算で照合。公開RESTの再取得も保存済み10/17 cutoffまでの9市場・2,592本で一致し、改訂・欠損0。再照合は新しい取引サンプルではない。
- exact mainのローカルテスト1,497件成功（分析1,118、研究354、root25）。コード変更はなく、今回のGit差分は公開研究集計2ファイルだけ。
- 過去限定VWAPの以前の結論 `BLOCKED_DATA_QUALITY`／有限バックテスト終了／採用根拠なしは保持。**今回の作業環境には、以前ローカルだけに保存した過去限定期間の元source・台帳・初回sealの原本がないため、その再検算は `MISSING_LOCAL_ORIGINAL_ARCHIVE` として停止した。** 保存済み集計を今日の再現結果と表示せず、履歴を再取得して原本に差し替えていない。今回取得できた週次GitHub artifactの検証とは別の制約である。

研究口座は各期間独立の1万USDC・1%・同時3・合計3%・JST日次3%・1倍notional。実口座の設定に置き換えず、週別ROIから連続口座ROIを推定しない。Funding、OHLC内約定順序、板/queue、実約定品質、現在市場集合の生存者バイアス・相関は残る。バックテストや再照合を実時間Shadow/実注文の昇格根拠へ加算しない。

元のKILL、protocol、将来研究の封印、live collector、売買条件、注文/署名/ON/OFF、UI、口座policy、DB、Push、Telegramは変更しない。本番レスポンスと本番台帳の検証証跡は作業環境内にのみ保存し、GitHubへエクスポートしない。利益・勝率改善・最適解の完成を保証する判断はない。
