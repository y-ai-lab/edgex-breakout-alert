# 捕捉Shadow台帳の全件監査（v19.0.76）

`GET /api/captured-shadow-ledger?source=pending&limit=500`
（`source=vwap`も対応）は、既存の実時間捕捉研究台帳を読み取る。
実注文・実約定・新しい検証サンプルではない。UIのタブや本番READYを増やさない。
元のcollector/evaluator/protocol/条件/口座/通知/DB schemaを変更しない。
既存`pending-entry-shadow`/`vwap-entry-shadow`の表示上限・レスポンスは維持する。

## 同じ読み取り状態の全件だけを取得する

最初のGETは元の全件reviewと全recordsを、単一のSQLite read transactionで取得する。
接続は`mode=ro`と`query_only`。DB欠落時は503とし、空のDBや台帳を作らない。
通常reviewの全件指標・cohorts・continuous_portfolios・観測証跡概要をそのまま返す。
`latest`だけをcreated_ms昇順、同時刻はkey昇順で分割する。

`ledger_page`にはsource、snapshot_ms、expires_ms、snapshot_sha256、offset、
page_size、returned_records、total_records、next_cursor、completeを返す。
snapshot_msは最初のサーバー読取時刻で、元のsignal/observed/capture起点ではない。
続きは同じsource/limitとnext_cursorで取得する。空台帳や最後のページは
complete=true/next_cursor=null。取得失敗を空台帳や全件完了へ読み替えない。

cursorはsource・最初の読取時計・全review/全recordsのSHA・page size・offsetに
署名し、120秒で期限切れとなる。署名鍵はprocess内で自動生成し、コード・ログ・DBへ
保存しない。cursorは取引の所有証明やAPI鍵ではない。再起動/デプロイ/期限切れは
409 `LEDGER_CURSOR_RESTART_REQUIRED`、内容更新は409 `LEDGER_CHANGED_RESTART_REQUIRED`。
sourceやlimitの混在・不正形式は400。元時計が未来になった場合も再取得する。
複数replicaをまたぐcursorは使用できないため、processが変わった場合は最初から取り直す。

ページ間で同じ読取時計を用いてreviewを再計算する。内容の変更（決着・新候補・
元価格・meta・coverage・観測証跡概要等）を検出したら、途中の取得を破棄して最初から
取り直す。最初のDB transactionをHTTP要求の間保持せず、元の収集やOPEN追跡は止めない。
heartbeat/metaの更新だけでも再取得となり得る。長時間の固定snapshot保持は保証しない。
同じcursorの再試行は内容と期限が有効な間、同じページを返す。

## 検算する側の確認

1. 各ページでsource/snapshot_ms/SHA/total_records/page_sizeが一致すること。
2. offsetとreturned_recordsが連続し、created_ms/key順で重複・欠落がないこと。
3. complete=trueとnext_cursor=nullまで取得し、全件数とユニークkey数がtotal_recordsと一致すること。
4. `latest=[]`に戻した元reviewと並べた全recordsからSHAを再計算すること。
5. 全件で元metrics/stress/週口座/連続口座を再計算すること。途中ページで全体の成績を作らない。

SHAは`{"source":source,"review":元review（latest=[]、ledger_pageなし）,
"records":全件のcreated_ms/key昇順}`を、Python jsonのsort_keys=True、
separators=(',',':')、allow_nan=False、ensure_ascii=FalseでUTF-8化して計算する。
別の言語でfloatの表現が異なる場合、SHA不一致を良い値へ修復せず明示する。

既存の観測証跡APIの詳細表示上限500は変更していない。
証跡概要が全件であることと、詳細の全件照合は別。詳細が不足すれば不足を明示する。
旧UNAVAILABLE_BEFORE_AUDITや欠損を再構成せず、AMBIGUOUS/OPEN/DATA_GAP・
保留資金・ROI nullを維持する。旧API、元週、replay、後追い、日次更新を別サンプルとして足さない。

本番の台帳・価格・口座policy・運用meta・cursor・raw/manifestは排他的な作業環境内にだけ
保存する。GitHubへのエクスポート・本文印字・通知配信には使用しない。
