# Cost-inclusive entry geometry

`.github/scripts/entry_feasibility_audit.py` reads an existing, hash-verified
`/api/readiness-review` snapshot locally. It checks whether the **same nominal
price** can satisfy strict roll-level confirmation, original structural SL/TP
validity and hypothetical fee-inclusive 2R. Fees are 5bps and adverse slippage
2bps per side, using the frozen pending-entry cost calculation.

This is a descriptive diagnostic on previously observed input, not a new strategy
or a test on an unused period. It neither changes READY nor creates candidates.
For pending-entry, confirmation happens first and a pullback fill happens later;
the two prices can differ. Do not use this intersection as that model's gate.

The report separates existing gross-price conflicts from gross-compatible bands
lost only after costs. It groups a single observation by stage and confirmation
flag, deduplicates setup identity through the original observation validator and
fails on inconsistent geometry, timestamps, hashes or counts. Missing structural
inputs remain `NOT_EVALUABLE`. It exports aggregate counts, without prices,
symbols, setup IDs or account policy. These are observations, not independent
trades, fills, win rates or ROI. Candle colour, liquidity and tick/size rounding
are outside this diagnostic.

Run with `EDGEX_EXEC_MODE=OFF` from the repository:

```sh
python .github/scripts/entry_feasibility_audit.py \
  --snapshot /local/exclusive-timestamped-snapshot \
  --output /local/new-entry-feasibility-report.json
```

The output must be a new file; earlier evidence is never overwritten. Production
snapshots and reports stay in the work environment and must not be committed or
uploaded as GitHub artifacts. Do not relax confirmation, SL, RR, wait bars or
volume from these counts, or infer counterfactual fills/profitability. Continue
the independently registered pending/VWAP cohorts until their frozen criteria
permit a decision; insufficient samples remain insufficient.
