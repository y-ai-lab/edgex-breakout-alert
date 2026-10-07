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
