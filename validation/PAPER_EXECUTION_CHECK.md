# 実READYを用いた隔離DBの模擬執行チェック

記録済みの現行READY VELVETUSDCを、取得済みの公開LAST_PRICE 15M足17本で再現する。本番と同じpaper_execution.cycleを使用し、各時点で確定したOHLCと形成中足のopenだけを入力する。Shadowを現行READYに見立てない。

```bash
python -m validation.run_paper_execution_check \
  --source validation/fixtures/velvet_ready_execution.json \
  --output /tmp/paper-execution-check
```

出力先は新規ディレクトリが必要。既存出力の上書きと/dataへの出力は禁止。DBは出力配下に新規作成し、ANALYSIS_DB_PATHや本番口座を利用しない。ネットワーク取得、実注文、通知送信は行わない。

analysis_terminal/test-requirements.txtを導入した環境では`--server-hook`を付けると、実サーバーの_init_db・_simulation_cycle・HTTP GET /api/paper-executionもASGI経由で検証する。DB_PATHと公開キャッシュ・時計を隔離入力へ差し替え、公開・非公開RESTを禁止する。サーバーlifespanとcollectorタスクは開始しない。

検証対象はPENDING→OPEN→SL、独立したDecimal計算による約定価格・数量刻み・数量上限・手数料・損益・現金残高の一致、signal candleの除外、形成中high/lowの除外、欠測時の保留と復旧、同じsetupの再入力と再初期化による二重決済防止、読取APIの無変更、検知120秒の鮮度境界、初回有効化時の過去シグナルBASELINED。

v19.0.18では状態変化のイベント保存も照合する。PENDING・OPEN・SL、欠測時のHISTORY_GAP、market_msとobserved_msの区別、再起動／重複入力によるイベント重複防止、GET /api/paper-execution/eventsを確認する。各ケースのevent_historyは隔離DBの過去再現であり、本番の新規約定履歴へ投入しない。

データは保存済み実シグナル、2026-10-04に再取得したEdgeX公開履歴と数量ルール。15M足17本は既存の結果照合用保存データとも全件一致した。取得元と入力はfixtureに保持し、実行報告に入力・エンジンのSHA256を保存する。

検知時刻はsignal close+1msを仮定する。実際の初回観測時刻と当時の数量ルールは保存されていない。30秒・119,999msの検知遅延も確認し、120秒は拒否される。費用は本番模擬モデルと同じ仮定（片道5bps、滑り2bps）であり、実際のEdgeX手数料ではない。

標本は実READY1件のみ。各制御ケースは同じシグナルを再利用し、独立した取引や昇格標本には数えない。結果はRETROSPECTIVE_EXECUTION_CHECK / eligible_for_live_promotion=falseで、本番の注文・損益・実績に追加しない。実TP・SHORTの通し標本や本番の新規約定は未確認であり、このチェックは収益性や実注文の安全性を証明しない。
