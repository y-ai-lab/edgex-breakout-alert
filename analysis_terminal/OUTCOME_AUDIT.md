# 保存結果と公開実足の照合 — v19.0.13

`/api/outcome-audit` は、保存済みライブEntryの時点監査を表示する読み取り専用API。`outcome_audit_latest.json` の取得時刻・各保存結果の最終足までを照合し、現在の成績とは区別する。新しいsignalやresolved標本を作成せず、保存結果・DB・戦略・通知を書き換えない。

[実足の照合](https://github.com/y-ai-lab/edgex-breakout-alert/actions/runs/37161292971)では現行1件とShadow25件の全26件がMATCH。各signal closeの次の足から、保存結果の最終足までの公開LAST_PRICE履歴を取得し、保存resultを入力から外して結果を再計算した。TP/SL・R・MFE/MAE・判定時刻・観測範囲・足数を比較する。signal candle・形成中・保存範囲より後の足は判定に使わず、欠測を飛び越えず、同一足TP/SLはAMBIGUOUSとする。現行の `source_candle_ms` とShadowの `signal_candle_ms` に対応する。

公開元足と捕捉したsignalのSHA256、照合コードcommit、Actions runを記録する。全26件をローカルでも再現した。公開履歴は後から修正され得るため、差異が出た場合はREVIEW_REQUIREDとし自動上書きしない。旧記録のsetup_idは推測で付けない。

この取得時点のShadowは25setup、resolved4（TP0 / SL4）、OPEN21。Avg R -1、PF0、INSUFFICIENT SAMPLE。監査MATCHは利益やエントリー可能を意味せず、既存の昇格集計にも加算しない。本番昇格条件は20件以上の異なるsetupの検証済み確定、Avg R>0、PF>1を維持する。

`/api/shadow-v2` の集計は表示limitと独立して保存全履歴を使う。`latest` は従来どおり最大50件。既存の `unverified_results` は互換性のため全記録の数を保ち、`unverified_results_basis=all_records_including_legacy` を追加した。`setup_unverified_results` と `legacy_unverified_results` を分離する。実データの13未検証結果はIDなしの旧確定結果であり、現行setupのOPEN欠測ではない。画面もこの内訳を表示する。既存の利益指標・昇格条件は変更しない。

CLIは保存したAPI captureを使い、実行時のDBへ接続しない。

```sh
python -m analysis_terminal.run_outcome_audit --source CAPTURE.json --output RESULT
```

captureは既存の `/api/shadow-v2`、`/api/server-paper-signals`、`/api/strategy-comparison` から取得し、cohortの件数とIDがすべて一致することを確認する。表示上限から対象が欠ける場合は完全な監査として扱わない。Actions artifactの保持期限は14日。
