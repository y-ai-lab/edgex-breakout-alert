# EMA20 pullback Shadow v1 — KILL

目的は、既存のブレイク戦略のRR条件を緩めず、独立した押し目・戻りsetupでEntry候補を増やせるか検証すること。

結果を見る前に[条件と期間を固定](https://github.com/y-ai-lab/edgex-breakout-alert/commit/58230518c2ac26d351fb7ed01746b286de08830c)した。実装は `pullback_replay.py`、固定条件は `pullback_protocol.json`。本番の分析関数・collector・DB・Pushから呼び出さない。

| 期間（UTC、各7日） | 現行候補 | 既存Shadow候補 | 押し目候補 | 押し目 resolved | TP / SL / OPEN / AMBIGUOUS | Avg R | PF | 最大連敗 |
| --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: |
| 2026-09-26 03:00 → 10-03 03:00（既存履歴） | 2 | 235 | 243 | 234 | 61 / 173 / 9 / 0 | -0.2179 | 0.7052 | 17 |
| 2026-09-12 03:00 → 09-19 03:00（新規評価履歴） | 3 | 314 | 162 | 153 | 37 / 116 / 8 / 1 | -0.2745 | 0.6379 | 36 |

事前固定した基準は、いずれかの期間で resolved >=20 かつ Avg R <=0 または PF <=1 ならKILL。両期間が該当し、候補数が増えても採用しない。結果を見て条件を調整せず、ライブShadowにも追加しない。現行の良否は確定1件・2件しかなく INSUFFICIENT SAMPLE。過去結果から現行が優れているとは結論しない。

各期間とも固定182銘柄を取得、失敗0。全銘柄を分母とした180本連続指標窓の観測点カバー率は88.71%・82.86%。足とmanifestのSHA256を検証し、現行・既存Shadowの全signal/outcomeを再現してから独立モデルを比較した。新規setupは `pullback-v1` として分離し、warmupも含め最初のEntryのみ。signal close+1ms以降の連続した15M足で結果を評価し、欠測を飛び越えず、同一足TP/SLはAMBIGUOUSとした。

[研究実行・生足artifact](https://github.com/y-ai-lab/edgex-breakout-alert/actions/runs/37122021541)。artifactの保持期限は14日。静的結果と各source manifest hash、研究commit、rule fingerprintを `pullback_latest.json` に保持する。既存期間のbaseline source fingerprintも一致する。再実行は各source directoryに対して以下を使う。

```sh
python -m analysis_terminal.pullback_replay --source SOURCE --output RESULT --role REUSED_DEVELOPMENT_HISTORY
python -m analysis_terminal.pullback_replay --source SOURCE --output RESULT --role NEW_HISTORICAL_VALIDATION
```

公開元足は後から修正され得る。現在の銘柄集合による上場・生存銘柄の偏りがあり、手数料・slippage・funding・同時保有制約は未反映。候補数は実際の取引回数ではない。RETROSPECTIVEの結果はライブ昇格サンプルへ加算せず、ENTRY NOW・既存READY・RR>=2・通知READY_ONLYを維持する。
