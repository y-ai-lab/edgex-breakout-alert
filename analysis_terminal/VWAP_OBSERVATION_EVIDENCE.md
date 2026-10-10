# VWAPの仮約定前観測を保存する監査

凍結済みVWAP collectorのcommit後、現在の初回cycleだけから実観測済みpending状態を
独立したSQLite表へ保存する。記録時刻が次の実行足開始より前の場合だけ証拠とする。
元の戦略・capture・期限・評価器・損益・口座・実注文・通知には接続しない。

導入後の次15M bucketから開始し、再起動で監査起点・元capture起点を変えない。
過去状態をAPI足から再構成せず、旧欠損はUNAVAILABLE_BEFORE_AUDITと表示する。
同一bucket再試行で証拠を加算・上書きしない。stateのnull/falseも保持する。
元stateの12本GC後も証拠だけは保存する。候補identity・state SHA・観測/記録時刻を検証。

`GET /api/vwap-entry-evidence`は読取専用。既存`/api/vwap-entry-shadow`には監査概要を追加。
表示limitは全台帳の証拠件数を減らさない。保存・読取失敗は監査のエラーとして分離し、
元Shadowを停止・修復しない。COMPLETEは実約定・優位性・昇格資格を証明しない。

旧5候補の不足証拠や過去の失敗を解消したとは扱わず、同じcohortの更新を独立サンプルに
加算しない。未使用期間を先に開けず、サンプル不足の間は条件を固定する。
本番観測と監査レスポンスは作業環境内だけに保存し、GitHubへ複製しない。
