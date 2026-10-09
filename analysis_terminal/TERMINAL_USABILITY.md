# Terminal recovery display — v19.0.58

The existing four tabs, BTC link, ENTRY NOW and 120-second market freshness
guard remain. No strategy, execution engine, signing, transport, risk policy,
owner capability, notification, Shadow collection or ledger behavior changes.

## Browser storage failures

Malformed JSON, wrong container types and unusable candidate/visit state no
longer stop the entire shipped script before scan and navigation handlers load.
Denied storage access, quota failures and failed removal leave the UI usable
with in-memory preferences and a visible warning. Unreadable keys are preserved:
automatic refresh does not overwrite or delete their original stored bytes.
Other valid preferences and legacy browser history are retained.

Private account values, Push proofs and control tokens still stay in memory;
this preference fallback does not grant authority or enable notifications or
trading. It does not repair unknown history, export private data or erase DB rows.

## Current state and historical operation results

An older successful `reconcile_flat` no longer replaces the current armed/runtime
state with an OFF message. The current state is shown first; completed/refused
requests are explicitly labeled as the last operation's result. Pending completion
review is not described as ON preflight. Fixed refusal reasons distinguish ON,
OFF and completion review, without rendering arbitrary server error strings.

Next-step guidance is next to the controls, with an exchange portfolio link and
the existing OFF/lock/review distinctions in an expandable help block. Ownership
uncertainty only suggests the review button when the existing server says that
the record is eligible; all original disabled-button and final owner/server gates
remain. Authentication, refresh and page opening never submit trading controls.

## Validation scope

The actual full script is exercised with corrupt JSON, wrong shapes, malformed
nested data, denied reads, quota failures and denied removal. Tests retain the
original data, scan/analyze/risk/API interactions, four tabs, 120-second stale
rejection and absence of trading/session requests. Existing synthetic exchange,
auth, OFF-priority, duplicate-send, protection and outcome tests still apply.
Mobile/desktop Chromium checks cover layout, visible guidance and page errors.
Production smoke is GET-only and verifies persistence and existing operation state.

This fixes the reproduced defects; it does not prove all possible bugs are absent
or supply the missing historical cause of a legacy emergency close. SL/TP placement
remains non-atomic and same-contract manual activity still requires ownership review.
