# Private account view (v19.0.47)

The API tab shows USDC equity, available collateral, cash, unrealized PnL, margin,
all current positions (including manual positions), and separate transaction and
collateral-change histories for the last 30 days. It adds no tab, strategy change,
trading controls, notification, withdrawal or transfer. EdgeX trading-account
assets do not represent the un-deposited balance of an Arc wallet.

## Authority and privacy

This single-owner deployment already has a known owner's sole Push device. On
first upgrade, the additive `account_view_owner` table seals that pre-existing
subscription's domain-separated fingerprint and the configured account binding.
An empty, invalid or multiple-subscription bootstrap stays locked. This is a
migration trust assumption about the known existing device, not a claim that any
public Push subscriber is the account owner. Never reuse automatic enrollment for
an unknown installation or a multi-user service.

New Push registrations cannot enroll or replace the sealed owner. The original
device proves its existing subscription keys with `POST /api/account/session`.
Endpoint possession and Push management tokens do not authorize viewing. The
server issues random, memory-only, read-only sessions valid for 600 seconds.
Every private request checks account binding, session expiry and the continued
presence of that same device. Account changes or subscription deletion revoke
access. Redeployment invalidates sessions without changing the sealed binding.
There is no public owner reset/recovery route. A lost or renewed device requires
operator-side identity verification and an explicit migration; do not rebind to a
new public subscription automatically.

View tokens use Authorization headers, never URLs, cookies or browser storage.
API responses are no-store. UI values are erased on explicit lock or when the
page becomes hidden, and logout invalidates the session. Failed/stale account
reads hide balances/positions after a 30-second freshness boundary. Previously
loaded historical pages remain explicitly historical until lock. This does not
protect against a compromised original browser, extension, device or server.
Existing CSP, cross-site write checks, body/time limits and bounded rate limits
apply. No private SDK response, balance, position, account/order ID, subscription
keys or token is logged or exported as a research artifact.

## Read-only data path

A separate SDK client receives HMAC API credentials with empty trading and wallet
signing keys. Its adapter allows only GET account assets, position transactions
and collateral transactions; metadata reads resolve names. It never invokes the
order adapter or changes execution/ownership state. An independent collector
publishes validated snapshots atomically in memory, polling after 15 seconds.
Snapshot and ledger time windows are not mixed. The public `/api/account/config`
reports section availability/freshness only, not values or position counts.

Missing account/position lists, foreign accounts/coins, nonfinite numbers or
ambiguous identifiers invalidate the relevant section. Zero/negative equity may
be displayed; absent values are null, not zero. Collateral amount, legacy amount, equity and available amount
remain distinct; legacy and current amounts are not silently added. Missing position detail means unknown entry/PnL/liquidation
price, not an inferred price. Metadata failure means an unknown name.

Initial history pages contain at most 50 rows per section. Session-scoped opaque
cursors retain the original 30-day interval and server-side upstream cursor.
They are single-use and bounded to 20 pages; repeated cursors or record IDs block
completion. Revocation/expiry is checked again after awaited page requests.
Unfinished or failed pagination never represents all records in the interval.
Missing pages have `items:null`, not an empty valid result. These API histories
are fetched on demand/in memory, not persisted as performance samples.

Transaction realized PnL, opening/closing fees and funding changes are separate.
The API's fee inclusion is not inferred. Collateral movements include overlapping
trade/funding cash flows and are not added to transaction PnL. Partial history is
not total lifetime performance; no net PnL, win rate or account ROI is manufactured
from it. `portfolio_roi_pct` remains null.

Tests use synthetic accounts and a mock exchange. They cover sealed migration,
new-device denial, forged proof, expiry/revocation/logout, sanitized GET-only
reads, missing data, stale boundaries, duplicates, frozen pagination windows,
post-await revocation, UI escaping and late responses after lock. Production
smoke checks use public availability and rejected unauthenticated requests; the
owner's private browser proof is not extracted or impersonated for validation.
