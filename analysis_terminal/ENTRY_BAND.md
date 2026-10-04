# 確認価格とRRの成立帯 — v19.0.22

診断・保存のみ。READY、min_rr、Shadow、通知、注文、outcome判定は変更しない。

4HのSL=S、TP=T、最低RR=rを固定すると、RR境界は `B=S+(T-S)/(1+r)`。
LONGのRR成立帯は `(S,B]`、確認終値はrollより大きい必要があり、両立帯は `(max(S,roll),B]`。
SHORTはRR成立帯 `[B,S)`、確認終値はroll未満、両立帯は `[B,min(S,roll))`。
roll=Bでは、終値条件が厳密な不等号なのでNO_OVERLAP。

analyze_contractがentry_bandを追加する。COMPATIBLE / NO_OVERLAP / INVALID_STRUCTURE / INVALID_INPUTを区別。現在価格でstopが無効でも、元の4H構造から診断する。COMPATIBLEは価格条件の両立可能性だけで、陽線・陰線、retest、trend、足の確定、データ鮮度、tick sizeを満たす保証ではない。注文価格の提案でもない。

GET /api/analyze、/api/chart、/api/screenerの分析行に診断を含む。/api/readiness-reviewのreview.entry_bandsに最新の全診断を追加。分析画面に説明を表示し、4タブ・ENTRY NOW・120秒の鮮度制限を維持する。

既存market_snapshots.payloadのentry_bandsに、取得時刻observed_ms、件数とsetup_idごとの構造・診断・確定足時刻を保存。テーブルやmigrationは変更しない。1つの15M枠に複数収集した場合、既存保存仕様と同じく最後の観測が残る。保存期間は既存の30日。日中すべての状態遷移を保存するものではない。

/api/server-historyで保存済みsetup別の構造更新を追える。/api/readiness-historyのsummary/daily.entry_bandsは観測件数・欠測件数・status別銘柄×観測数・NO_OVERLAP率を返す。同じsetupの繰り返し観測を独立トレードとして数えない。NO_OVERLAP率の分母はCOMPATIBLE+NO_OVERLAP、無効構造・入力を除く。旧snapshotに診断がない場合や内容不整合はmissing_observationsとし、ゼロや過去の推定値を埋めない。新形式でsetupゼロを記録した観測は有効なゼロとして区別する。

価格帯が将来の4H構造更新で変わることはある。本機能の導入を、エントリー回数・収益性・勝率が改善した証拠として扱わない。別戦略への変更には未使用期間の費用込み検証とShadow観測が必要。

検証: python -m unittest analysis_terminal.test_entry_band -v。
