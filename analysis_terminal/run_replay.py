"""Fetch public candles and write an isolated replay artifact, never the live DB.

python -m analysis_terminal.run_replay --end-ms 1790996400000 --days 7 --output replay-output
"""
import argparse
import asyncio
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import threading
import time

from analysis_terminal import server
from analysis_terminal.history import fetch_history
from analysis_terminal.replay import replay_contract, replay_report, rule_fingerprint, strategy_parameters


async def run(end_ms: int, days: int, output: Path, *, universe: dict | None = None) -> dict:
    settings = server.SETTINGS
    if settings.monitor_interval != "HOUR_4" or settings.entry_interval != "MINUTE_15":
        raise ValueError("This research protocol requires the production 4H/15M intervals")
    if end_ms % 900_000 or end_ms > int(time.time()*1000) or not 1 <= days <= 30:
        raise ValueError("Invalid replay period")
    start_ms = end_ms-days*86_400_000
    window = 180  # Observed BTC public WebSocket seed: 181 raw / 180 closed.
    scan_start = start_ms-settings.roll_max_age*14_400_000-settings.retest_lookback*900_000
    begin4 = ((scan_start-window*14_400_000-14_400_000)//14_400_000)*14_400_000
    begin15 = scan_start-window*900_000
    output.mkdir(parents=True, exist_ok=True)
    data_dir = output/"candles"
    data_dir.mkdir(exist_ok=True)
    contracts = await server.CLIENT.get_contracts() if universe is None else universe
    manifest = dict(protocol="public_last_price_7d_all_current_contracts_v1", fetched_ms=int(time.time()*1000),
                    source_endpoint="/api/v2/public/quote/getKline", parameters=strategy_parameters(settings),
                    rule_fingerprint=rule_fingerprint(server.analyze_contract, settings),
                    indicator_windows=dict(HOUR_4=window, MINUTE_15=window),
                    universe=[asdict(c) for c in sorted(contracts.values(), key=lambda c:c.contract_id)],
                    sources=[], failures=[])
    # Bound aggregate request cadence; use only the SDK's public GET transport.
    lock = threading.Lock()
    next_request = [0.0]

    def get_public(path, params):
        for attempt in range(3):
            with lock:
                delay = max(0, next_request[0]-time.monotonic())
                if delay:
                    time.sleep(delay)
                next_request[0] = time.monotonic()+.2
            try:
                return server.CLIENT._get_json_sync(path, params)
            except RuntimeError:
                if attempt == 2:
                    raise
                time.sleep(attempt+1)

    semaphore = asyncio.Semaphore(4)

    async def collect(contract):
        async with semaphore:
            try:
                monitor = await fetch_history(get_public, contract, "HOUR_4", begin4, end_ms)
                entries = await fetch_history(get_public, contract, "MINUTE_15", begin15, end_ms)
                payload = dict(contract=asdict(contract), HOUR_4=[asdict(c) for c in monitor], MINUTE_15=[asdict(c) for c in entries])
                raw = json.dumps(payload, separators=(",", ":"), allow_nan=False)
                name = f"{int(contract.contract_id)}.json"
                (data_dir/name).write_text(raw)
                manifest["sources"].append(dict(ticker=contract.contract_name, file="candles/"+name,
                                                 sha256=hashlib.sha256(raw.encode()).hexdigest(),
                                                 monitor_candles=len(monitor), entry_candles=len(entries)))
                return replay_contract(contract, monitor, entries, server.analyze_contract, start_ms=start_ms, end_ms=end_ms,
                                       monitor_window=window, entry_window=window, setup_max_age=settings.roll_max_age,
                                       retest_lookback=settings.retest_lookback)
            except Exception as exc:
                manifest["failures"].append(dict(ticker=contract.contract_name, error_type=type(exc).__name__,
                                                 reason=str(exc)[:200]))
                return None

    results = await asyncio.gather(*(collect(c) for c in sorted(contracts.values(), key=lambda c:c.contract_id)))
    manifest["sources"].sort(key=lambda s:s["ticker"])
    manifest["failures"].sort(key=lambda s:s["ticker"])
    report = replay_report([r for r in results if r is not None], start_ms=start_ms, end_ms=end_ms, manifest=manifest)
    report["coverage"]["failed_markets"] = len(manifest["failures"])
    report["coverage"]["requested_markets"] = len(contracts)
    report["coverage"]["fetched_markets"] = len(manifest["sources"])
    report["coverage"]["expected_points_all_markets"] = days*96*len(contracts)
    (output/"replay-report.json").write_text(json.dumps(report, ensure_ascii=False, allow_nan=False))
    summary = {k:v for k,v in report.items() if k != "signals"}
    (output/"replay-summary.json").write_text(json.dumps(summary, ensure_ascii=False, allow_nan=False))
    print("REPLAY_METRICS="+json.dumps({k:report[k] for k in ("dataset", "eligible_for_live_promotion", "coverage")} |
                                     {"current":report["comparison"]["current"], "shadow":report["comparison"]["shadow"]}))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--end-ms", type=int, required=True)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--universe-manifest", type=Path, help="Reuse an archived research universe for a period comparison")
    args = parser.parse_args()
    universe = None
    if args.universe_manifest:
        report = json.loads(args.universe_manifest.read_text())
        if report.get("dataset") != "RETROSPECTIVE" or report.get("eligible_for_live_promotion") is not False:
            raise ValueError("Invalid archived research universe")
        contracts = [server.scanner.Contract(**c) for c in report["manifest"]["universe"]]
        universe = {c.contract_id: c for c in contracts}
        if not universe or len(universe) != len(contracts):
            raise ValueError("Invalid or duplicate archived contracts")
    asyncio.run(run(args.end_ms, args.days, args.output, universe=universe))


if __name__ == "__main__":
    main()
