# Explicit owner ON/OFF controls — v19.0.50

## Quarantined-position completion review — v19.0.56

With new entries OFF, the original owner may explicitly press **終了した建玉を照合**.
The existing short-lived control capability queues `reconcile_flat`; a read-only
view token cannot perform this ledger operation. No private proof is used by
production smoke, and deploying the feature never invokes the operation.

Only quarantined isolated-contract records with known positive entry fills are
eligible. The serialized worker reads complete active-order pages and fresh
account positions twice, before and after querying every attempted bot order.
Any remaining target-contract position/order, missing/stale data, nonterminal or
mismatched bot order, changed fill, policy/account change, expired/revoked owner
or OFF during the wait refuses the review. Other contracts are not attributed
to this bot. SDK create/cancel/signing methods are never called by the review.

Successful review saves `EXTERNAL_FLAT_VERIFIED` and its timestamp/original
status; all original fills, reserved-risk fields, order IDs, quarantine flag and
events remain. It does not invent exit prices, fees, TP/SL, realized PnL or ROI.
Health reports it separately from CLOSED. Research/paper portfolios and their
uncertain capital are unchanged. It confirms no current contract exposure, not
the lot ownership or financial outcome of its earlier manual/external activity.
Exchange reads are repeated observations, not an atomic exchange lock.

The engine stays paused; review does not automatically arm or retry an entry.
The owner must separately press ON, which runs the unchanged fresh-account
preflight and excludes occupied contracts. With a bot position still open,
restart remains blocked. Queued/interrupted review does not resume after a
restart. No manual-protection adoption, force-clear or close-all is introduced.

The API tab adds **自動取引 ON** and **新規エントリー OFF**, within the existing
four-tab interface. The original owner explicitly presses **登録端末で操作を有効にする**
first. ON enables future real orders from the unchanged CURRENT READY strategy.
Opening a page, reading balances, authenticating, refreshing status, redeploying
or viewing Shadow does not arm the engine. No live toggle/order is used as a test.

## Authority

`POST /api/execution/session` proves the same originally sealed owner Push device
as the account view. Newly registered Push devices cannot enroll or replace it.
This retains the existing single-owner bootstrap trust assumption; it is not MFA
or a general user-login system. A separate random token namespace grants only
`NEW_ENTRY_CONTROL`, for **120 seconds**, bound to the account and risk-policy
fingerprint. Read-only account sessions and Push management tokens cannot control
execution, and control tokens cannot read balances. Every request rechecks the
owner's presence, binding, expiry and policy. ON checks authority again inside the
final SQLite transaction after awaited exchange preflight. Logout, deletion,
expiry, policy/mode changes or OFF during that wait prevent activation.

Bearer tokens stay in memory/Authorization headers, never URLs, browser storage,
SQLite, source or logs. Page hiding and operation/account lock revoke the operation
session. Lock/expiry **does not stop an already activated engine**; press OFF to
stop new entries. Pending ON loses authorization and cannot finish after lock.
Cross-site writes, body/time limits, rate limits, CSP and API no-store apply. No
public owner reset/re-enrollment, wallet keys, withdrawal/transfer or ad-hoc order
route is added. A compromised original browser/device/server remains a risk.

## Runtime and race handling

v19.0.54: A disabled ON button labels the locked, unknown, pending or blocked state.
The authenticated status includes fixed `on_blockers` codes explaining the original
worker/mode/configuration/armed/unresolved-ledger gates, without credentials or
order identifiers. Ownership quarantine and unresolved records show the required
position/SL/TP/history checks next to the button. A closed exchange position alone
does not reconcile a quarantined bot record. Reasons never authorize or override ON;
the existing status, submit and final preflight conditions remain unchanged.

Authenticated `GET /api/execution/status` distinguishes the stored armed setting
from current runtime eligibility, worker availability and an ON request's phase.
An acknowledgement of QUEUED/PROCESSING is not proof of ON. The UI uses actual
status, guards late responses after OFF/lock, expires stale status at 10 seconds,
and keeps authenticated OFF available even when current status cannot be read.
It never automatically resends an uncertain action with a new operation ID.

`POST /api/execution/control` accepts only arm/pause, a canonical UUIDv4 operation
ID and a state epoch. Duplicate IDs do not rearm or repeat a pause; changed content
and collisions with environment/CLI IDs are refused. ON while already ON does
not reset the signal baseline. ON requires LIVE configuration, the existing worker,
no unresolved bot record, a matching epoch and no pending ON. It enqueues an
operation for the sole file-locked execution worker. Existing Engine.arm preflight,
account policy, baseline of past READY setups, post-arm candle time, unchanged
risk/fee budgets, grid/quote checks and same-contract ownership safeguards apply.
No risk, stop, RR, confirmation, daily limits or research conditions are changed.

OFF commits `armed=false` and increments the epoch immediately, independent of
remote SDK calls or worker availability. It cancels pending ON and interrupts an
ON preflight. Engine.arm checks the expected epoch before entering preflight and
again uses its existing epoch check at final commit, so an older ON cannot undo OFF.
It leaves EDGEX_EXEC_MODE=LIVE and existing reconciliation/SL/TP management intact.
**Already sent/in-flight IOC orders are not cancelled by OFF and can still fill**;
known fills continue through protection/exit reconciliation. OFF is not close-all,
mode OFF, cancellation of manual orders or a guaranteed immediate exit.

The additive `execution_web_controls` table stores operation IDs, phases, epochs,
policy fingerprints and fixed error codes, preserving all original ledgers and
subscriptions. Authorizations are memory only. At worker startup, queued/interrupted
ON operations become REFUSED, never replayed; interrupted activation also pauses
new entries. Existing terminal decisions, consumed UUIDs and already armed state
without an interrupted operation remain intact. Single replica/volume and the
existing worker file lock remain required; no distributed multi-account controller
or automatic exception recovery is introduced.

Tests use a synthetic exchange and device. They cover capability separation,
new-device/forged-proof denial, cross-site/body/input checks, duplicates, epoch
races, expiry/revocation at final commit, restart refusal, OFF during preflight and
IOC sending, and preserved exits after OFF. Real browser tests use an isolated
mock exchange. Production smoke uses public status and rejected unauthenticated
requests only, and checks the existing arm control and persistent data are preserved.
