# Read-only execution health (v19.0.46)

The `health` field of `GET /api/live-execution` audits the entire bot order ledger in the same
SQLite read transaction as the connection report. It adds no controls, SDK
requests, order retries, auto-resume, notification or strategy changes.

Connection success can clear `last_error` while an ownership-quarantined order
remains unresolved. Health therefore checks persistent ledger state independently
of that field. Ownership uncertainty, disabled management, unresolved closes,
protection expiry/cancellation, stale observations and malformed records require
exchange review. This diagnostic is not an additional execution gate and does not
itself halt the engine.

Counts distinguish intentions/acknowledgments, recorded fills, protection records
and verified closed records. SKIPPED includes historical baseline entries; it is
not a count of attempted or filled trades. No account-wide flatness is inferred.
Only the bot ledger is counted; manual or other positions are excluded. No setup,
order or account IDs, tickers, prices, keys or free-form error details are added.

The last eight state transitions contain fixed captions and observation times.
A send or acknowledgment is not a fill or closure. A PROTECTED record and a fresh
account connection do not establish current per-order protection: no per-order
freshness timestamp or exchange query is added. CLOSED requires a recorded fill
and a valid outcome observation time; this is not account ROI or Funding-adjusted
performance.

The four-tab UI shows review guidance on the dashboard and API page. Client
snapshots expire after 30 seconds. Failed status requests retain the last known
warning with an explicit stale label. Missing/malformed responses require review,
never imply zero positions. Existing ENTRY NOW freshness remains 120 seconds.
The API connection check also warns when ledger health requires attention.

Recovery guidance asks the operator to inspect exchange positions, orders and
fills before re-sending or re-enabling an uncertain order. OFF/READ_ONLY stops
management; a LIVE manual pause stops new entries while retaining reconciliation.
Ownership quarantine can cancel the bot's protective orders without closing a
position whose ownership is unknown. The diagnostic cannot guarantee protection,
recover ambiguous ownership, resolve missing acknowledgments, or cap actual loss.

Tests use a simulated exchange to reproduce successful reconnection with
`last_error=null` and persistent ownership quarantine. API tests verify GET leaves
all DB rows unchanged. UI tests cover stale/failed responses, malformed health,
text-only event rendering and consistency of the API check. No real orders or
notifications are sent by validation.
