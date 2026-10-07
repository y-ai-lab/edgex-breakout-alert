# ENTRY and near-candidate history

The dashboard's **ENTRY・あと一歩の履歴** drawer shows production READY and the same qualified NEAR candidates used by WATCH NEXT. NEAR means RETEST_WAIT or CONFIRMATION_WAIT with finite RR >= the unchanged production minimum. Shadow-only candidates never appear as production ENTRY.

The background collector records fresh, closed-candle observations independently of notification transition baselining. `entry_candidate_history` is keyed by the canonical setup ID. Its first NEAR and first READY snapshots preserve observed time, direction, entry/SL/TP/RR and confirmation flags; later scans update only the latest observation. Setup lifecycle records supply expiry/invalidation/supersession. Archive writes are isolated so a failure does not stop notifications or outcome tracking. The archive has no automatic pruning.

Initialization adds the table and index and imports available identified candidate events and current paper signals once, transactionally. Existing tables, subscribers, signal timestamps and results stay intact. Legacy records without setup identity are not inferred by ticker; previously deleted records cannot be reconstructed. A repeat initialization does not replace archived observations.

`GET /api/entry-history` supports `kind=ALL|READY|NEAR`, `days=7|30|0` (0 = all, any value 0–365 accepted), ticker substring, limit 1–100 and paired `before_ms` / `before_setup` cursors. Totals describe the entire filtered cohort, independently of page limits; READY and NEAR counts overlap when a setup reached both. The endpoint is read-only. History date is the most recent **first** NEAR or READY observation, not a last-seen date that moves every scan.

Reference TP/SL/AMBIGUOUS comes from the first current paper signal for the exact same setup ID and is explicitly hypothetical. Unverified or incomplete outcomes show data checking instead of a confirmed result. No new outcome calculations or real execution are introduced. Existing signal-close +1ms exclusion and ambiguous-candle rules remain unchanged.

The drawer loads only when opened, preserves the four navigation tabs and never updates ENTRY NOW or global ENTRY status. “現在の分析を見る” requests fresh analysis instead of using historical prices for an entry. History is stored on the existing `/data` SQLite volume. READY-only Push and notification-service watch patterns are unchanged.
