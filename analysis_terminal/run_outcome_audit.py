"""Fetch public post-signal bars and reconcile an immutable live API capture.

python -m analysis_terminal.run_outcome_audit --source capture.json --output audit-output
"""
import argparse
import asyncio
from dataclasses import asdict
import hashlib
import inspect
import json
from pathlib import Path
import time

from analysis_terminal import server
from analysis_terminal.comparison import cohort
from analysis_terminal.history import fetch_history
from analysis_terminal.outcome_audit import DATASET, audit_signal, audit_summary


async def run(source_path, output):
    raw = source_path.read_bytes()
    capture = json.loads(raw)
    contracts = await server.CLIENT.get_contracts()
    by_name = {c.contract_name: c for c in contracts.values()}
    output.mkdir(parents=True, exist_ok=True)
    (output / "source.json").write_bytes(raw)
    source_dir = output / "candles"
    source_dir.mkdir(exist_ok=True)
    models = {}; manifest = []
    for model in ("current", "shadow"):
        records, excluded = cohort(capture["records"][model])
        expected = capture["comparison"][model]["signals"]
        if len(records) != expected:
            raise ValueError("Incomplete live signal capture: " + model)
        identities = {s["setup_id"] for s in records}
        compared = {r["setup_id"] for r in capture["comparison"]["latest"] if r.get(model)}
        if identities != compared:
            raise ValueError("Live capture identity mismatch: " + model)
        rows = []
        for signal in records:
            contract = by_name.get(signal["ticker"])
            if contract is None:
                rows.append(dict(key=signal["key"], setup_id=signal["setup_id"], ticker=signal["ticker"], status="MISSING_CONTRACT"))
                continue
            saved = signal.get("result") or {}
            end = saved.get("history_end_ms")
            if end is None:
                rows.append(audit_signal(signal, [], contract))
                continue
            try:
                # Include the signal candle as an exclusion control. Closed bars
                # after the saved cursor are never used to compare an earlier state.
                candles = await fetch_history(server.CLIENT._get_json_sync, contract, "MINUTE_15",
                    int(signal.get("signal_candle_ms") if signal.get("signal_candle_ms") is not None
                        else signal["source_candle_ms"]), int(end) + 900_000, max_pages=10)
                payload = dict(contract=asdict(contract), candles=[asdict(c) for c in candles])
                encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
                name = model + "-" + hashlib.sha256(signal["key"].encode()).hexdigest()[:16] + ".json"
                (source_dir / name).write_bytes(encoded)
                manifest.append(dict(model=model, key=signal["key"], file="candles/"+name,
                                     sha256=hashlib.sha256(encoded).hexdigest(), bars=len(candles)))
                rows.append(audit_signal(signal, candles, contract))
            except Exception as exc:
                rows.append(dict(key=signal["key"], setup_id=signal["setup_id"], ticker=signal["ticker"],
                                 status="FETCH_ERROR", error_type=type(exc).__name__))
            await asyncio.sleep(.2)
        models[model] = dict(summary=audit_summary(rows), exclusions=excluded, items=rows)
    report = dict(dataset=DATASET, eligible_for_live_promotion=False, automatic_promotion=False,
                  changes_live_results=False, captured_ms=capture["captured_ms"], audited_ms=int(time.time()*1000),
                  source_sha256=hashlib.sha256(raw).hexdigest(),
                  rule_fingerprint=hashlib.sha256((inspect.getsource(audit_signal)+inspect.getsource(server.evaluate_paper_signal)
                                                    +inspect.getsource(server.consecutive_window)).encode()).hexdigest(),
                  models=models, manifest=manifest,
                  outcome="NO_RECORDS" if not sum(m["summary"]["records"] for m in models.values()) else "MATCH" if all(m["summary"]["matched"] == m["summary"]["records"] for m in models.values()) else "REVIEW_REQUIRED",
                  limitations=["Point-in-time reconciliation of recorded live entries against public REST history; not an entry-rule replay.",
                               "Public history can be revised. A mismatch is evidence for review, never permission to overwrite a saved result.",
                               "No additional signals or resolved promotion samples. Legacy unidentified records remain excluded.",
                               "Compare only through each saved cursor; OPEN is not a win/loss and terminal outcomes are not modified."])
    (output/"outcome-audit-latest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    print("OUTCOME_RECONCILIATION="+json.dumps({k:report[k] for k in ("dataset","outcome","captured_ms","audited_ms")} |
          {"models":{k:v["summary"] for k,v in models.items()}},ensure_ascii=False))
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source",type=Path,required=True);parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args();asyncio.run(run(args.source,args.output))


if __name__ == "__main__":
    main()
