# Variational Omni analysis terminal

Independent analysis-only service using the existing four-tab terminal and frozen
4H / 15M analysis rules. This is a reference-data application, not native Omni
execution or a port of EdgeX performance results.

- Omni `/metadata/stats`: market universe, mark, indicative quotes, Funding and OI.
- EdgeX public LAST_PRICE candles: charts, setup detection, Entry / SL / TP / RR,
  hypothetical outcome tracking and paper account. Exact symbol matches only;
  no aliases, scaling or synthetic bars. Same ticker does not certify identical
  underlying, market basis, liquidity or instrument specification; check the asset
  on Omni before use. Unmapped markets remain in the full Omni market listing.
- All pages prominently disclose reference prices. Quotes can be cached for up to
  600s; fetch time is not mark/quote update time. Funding is displayed as the
  API raw value with its calculation window: the API field rate basis is not
  verified, so no per-period percentage, APR or funding payment is inferred. `native_entry_ready` is always
  false. These indications do not certify a tradable RFQ.
- Own database, VAPID key and subscriptions. No imported EdgeX histories,
  subscriptions, account funds, secrets or orders. Own persistent volume required.
- Reuses shared server functions in a separate process, with a public-only facade,
  explicit route allowlist and a separate lifespan. No execution/account collectors
  or private trading routes. No Telegram or prospective research collectors.
- Manual risk estimates have no Omni instrument limits or precision and exclude
  Omni-specific execution costs and Funding. Paper costs remain the original
  research assumptions, not Omni realized performance or account ROI.

Run from repository root: `python -m variational_terminal.server`. Dependencies are
root plus analysis requirements. Use the Dockerfile here, root context `/`, port
8080. Configure `VARIATIONAL_PUBLIC_ORIGIN` to the service HTTPS origin and
`VARIATIONAL_DB_PATH=/data/variational_terminal.db`. Do not supply EdgeX credentials
or WEBPUSH keys. VAPID is generated independently and retained on the new volume.

Watch paths: `/variational_terminal/**`, `/analysis_terminal/**`, `/app.py`,
`/requirements.txt`. Changes confined here must not redeploy EdgeX services.

Tests: `python -m unittest discover -s variational_terminal -p 'test_*.py' -v`.
The original analysis/research tests must also pass unchanged. Browser testing
covers all four tabs, mobile layout, analysis charts and JavaScript console errors.
