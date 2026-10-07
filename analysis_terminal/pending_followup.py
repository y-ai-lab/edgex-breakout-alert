"""Read-only outcome follow-up of sealed research cohorts; no server hooks."""
import argparse
import asyncio
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import subprocess
import time
import zipfile
import io

import app as scanner
from analysis_terminal import pending_entry_replay as study
from analysis_terminal.history import fetch_history
from analysis_terminal.setups import setup_identity

POLICY=Path(__file__).with_name("pending_followup_protocol.json")
DAY=86_400_000


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def validate_base(base, source):
    policy=json.loads(POLICY.read_text())
    protocol=json.loads(study.PROTOCOL.read_text())
    if (base.get("dataset")!="RETROSPECTIVE" or source.get("dataset")!="RETROSPECTIVE" or
            source.get("eligible_for_live_promotion") is not False or base.get("eligible_for_live_promotion") is not False or
            base.get("real_orders_enabled") is not False or base.get("automatic_promotion") is not False or
            base.get("protocol_sha256")!=policy["original_entry_protocol_sha256"] or
            sha(study.PROTOCOL.read_bytes())!=policy["original_entry_protocol_sha256"] or
            base.get("engine_sha256") not in {policy["known_original_engine_sha256"],sha(Path(study.__file__).read_bytes())} or
            base.get("production_rule_fingerprint")!=policy["entry_rule_fingerprint"] or
            source["manifest"]["parameters"]!=protocol["production_parameters"] or
            base.get("source_rule_fingerprint")!=source["manifest"]["rule_fingerprint"] or
            (base["start_ms"],base["end_ms"])!=(source["start_ms"],source["end_ms"])):
        raise ValueError("Unrecognized or changed frozen research cohort")
    records=base["records"]
    if len({r["key"] for r in records})!=len(records):
        raise ValueError("Duplicate frozen record identity")
    contracts={c["contract_name"]:scanner.Contract(**c) for c in source["manifest"]["universe"]}
    if (len(contracts)!=len(source["manifest"]["universe"]) or
            len({c.contract_id for c in contracts.values()})!=len(contracts) or
            any(not str(c.contract_id).isdigit() or int(c.contract_id)<=0 for c in contracts.values())):
        raise ValueError("Ambiguous frozen contract universe")
    for r in records:
        _,breakout_ms,level=r["setup_id"].rsplit(":",2)
        identity=setup_identity(dict(ticker=r["ticker"],direction=r["side"],breakout_time_ms=breakout_ms,breakout_level=level))
        if (r["model"] not in study.MODELS or r["key"]!=r["model"]+":"+r["setup_id"] or
                identity!=r["setup_id"] or r["ticker"] not in contracts or
                not base["start_ms"]<=r["created_ms"]-1<base["end_ms"]):
            raise ValueError("Invalid frozen record")
        if r["status"]=="OPEN":
            if (r["filled_ms"] is None or r["filled_ms"]%study.STEP or r.get("final_net_r") is not None or
                    not r["created_ms"]-1<=r["filled_ms"]<base["end_ms"]):
                raise ValueError("OPEN without a historical fill")
            levels=study.cost_levels(r["nominal_fill"],r["stop"],r["target"],r["side"])
            for field in ("entry","net_risk","stop_exit","target_exit"):
                if isinstance(r[field],bool) or not math.isfinite(r[field]) or not math.isclose(r[field],levels[field],rel_tol=1e-10,abs_tol=1e-10):
                    raise ValueError("Changed frozen prices or costs")
    for model in study.MODELS:
        if study.metrics([r for r in records if r["model"]==model])!=base["metrics"][model]:
            raise ValueError("Frozen metrics differ from ledger")
    return contracts


def seal(research_dir, destination):
    """Keep a small immutable weekly ledger longer than the raw-data artifact."""
    report=(research_dir/"review/pending-entry-report.json").read_bytes()
    source=(research_dir/"source/replay-report.json").read_bytes()
    base,manifest=json.loads(report),json.loads(source)
    validate_base(base,manifest)
    origin=json.loads(study.PROTOCOL.read_text())["prospective_start_ms"]
    if (base.get("period_complete") is not True or base["end_ms"]-base["start_ms"]!=7*DAY or
            base["start_ms"]<origin or (base["start_ms"]-origin)%(7*DAY) or
            manifest["manifest"].get("failures") or
            len(manifest["manifest"]["sources"])!=len(manifest["manifest"]["universe"])):
        raise ValueError("Seal only complete anchored prospective weeks")
    destination.mkdir(parents=True,exist_ok=True)
    (destination/"report.json").write_bytes(report)
    (destination/"source.json").write_bytes(source)
    (destination/"seal.json").write_text(json.dumps(dict(report_sha256=sha(report),source_sha256=sha(source),
                                                       start_ms=base["start_ms"],end_ms=base["end_ms"]),indent=2)+"\n")


def extend(base, source, candles_by_ticker, *, end_ms):
    contracts=validate_base(base,source)
    start=base["end_ms"]
    if end_ms%study.STEP or not start<=end_ms<=start+7*DAY:
        raise ValueError("Follow-up exceeds fixed horizon or closed-bar grid")
    records=[]
    for old in base["records"]:
        new=dict(old)
        if old["status"]=="OPEN":
            contract=contracts[old["ticker"]]
            candles=candles_by_ticker.get(old["ticker"],[])
            if any(a.time_ms>=b.time_ms for a,b in zip(candles,candles[1:])):
                raise ValueError("Follow-up candles must be unique and ordered")
            for c in candles:
                if (c.contract_id!=contract.contract_id or c.interval!="MINUTE_15" or c.time_ms%study.STEP or
                        not all(math.isfinite(v) and v>0 for v in (c.open,c.high,c.low,c.close)) or
                        not c.low<=min(c.open,c.close)<=max(c.open,c.close)<=c.high):
                    raise ValueError("Invalid follow-up candle identity/grid/OHLC")
            new=study.evaluate(old,[c for c in candles if c.time_ms>=start],{},end_ms=end_ms,start_ms=start)
        records.append(new)
    groups={m:[r for r in records if r["model"]==m] for m in study.MODELS}
    return dict(dataset="RETROSPECTIVE_OUTCOME_FOLLOWUP",cohort_start_ms=base["start_ms"],cohort_end_ms=start,
                followup_end_ms=end_ms,followup_complete=end_ms==start+7*DAY,
                no_new_entries=True,changes_live_rules=False,real_orders_enabled=False,
                automatic_promotion=False,eligible_for_live_promotion=False,
                original_metrics=base["metrics"],metrics={m:study.metrics(rs) for m,rs in groups.items()},
                portfolios={m:study.portfolio(rs) for m,rs in groups.items()},
                censored_unfilled_pending=sum(r["status"]=="PENDING" for r in records),
                newly_resolved=sum(a["status"]=="OPEN" and b["status"] in {"TP","SL"} for a,b in zip(base["records"],records)),
                base_engine_sha256=base["engine_sha256"],followup_engine_sha256=sha(Path(study.__file__).read_bytes()),
                followup_policy_sha256=sha(POLICY.read_bytes()),records=records,
                limitations=["Same cohort follow-up, never additional independent signals.",
                             "Historical public candles, not captured-live or actual fills.",
                             "Unfilled pending entries at the boundary are censored; no new fills are guessed.",
                             "Seven-day follow-up cutoff can still leave OPEN; never force expiry or release uncertain capital."])


async def public_candles(base, source, *, end_ms, output, get_json=None):
    if get_json is None:
        from analysis_terminal import server
        get_json=server.CLIENT._get_json_sync
    contracts=validate_base(base,source)
    output.mkdir(parents=True,exist_ok=True)
    found,sources={},[]
    last=[0.0]
    def get_public(path,params):
        time.sleep(max(0,.2-(time.monotonic()-last[0])))
        last[0]=time.monotonic()
        return get_json(path,params)
    for ticker in sorted({r["ticker"] for r in base["records"] if r["status"]=="OPEN"}):
        contract=contracts[ticker]
        candles=await fetch_history(get_public,contract,"MINUTE_15",base["end_ms"],end_ms)
        found[ticker]=candles
        raw=json.dumps(dict(contract=asdict(contract),MINUTE_15=[asdict(c) for c in candles]),separators=(",",":"),allow_nan=False).encode()
        file=str(int(contract.contract_id))+".json"
        (output/file).write_bytes(raw)
        sources.append(dict(ticker=ticker,file=file,sha256=sha(raw),candles=len(candles)))
    (output/"manifest.json").write_text(json.dumps(dict(start_ms=base["end_ms"],end_ms=end_ms,sources=sources),indent=2)+"\n")
    return found


def gh_json(path):
    return json.loads(subprocess.check_output(["gh","api",path]))


def scheduled(output, *, now_ms):
    """Read only this repository's main artifacts and public quote endpoints."""
    output.mkdir(parents=True,exist_ok=True)
    origin=json.loads(study.PROTOCOL.read_text())["prospective_start_ms"]
    window=study.research_window(now_ms,origin)
    begin=window["start_ms"]-7*DAY
    if begin<origin:
        return dict(status="WAITING_FOR_FIRST_FROZEN_COHORT",automatic_promotion=False)
    repo="y-ai-lab/edgex-breakout-alert"
    name=f'edgex-frozen-cohort-{begin}-{window["start_ms"]}'
    listing=gh_json(f"repos/{repo}/actions/artifacts?name={name}&per_page=100")
    if listing["total_count"]>100:
        raise ValueError("Ambiguous truncated frozen archive listing")
    candidates=[a for a in listing["artifacts"] if a["name"]==name and a.get("workflow_run",{}).get("head_branch")=="main"]
    if not candidates:
        return dict(status="MISSING_FROZEN_COHORT",cohort_start_ms=begin,cohort_end_ms=window["start_ms"],automatic_promotion=False)
    artifact=min(candidates,key=lambda a:(a["created_at"],a["id"]))
    if artifact["expired"]:
        return dict(status="FROZEN_COHORT_EXPIRED",base_artifact_id=artifact["id"],automatic_promotion=False)
    raw=subprocess.check_output(["gh","api",f'repos/{repo}/actions/artifacts/{artifact["id"]}/zip'])
    if artifact.get("digest")!="sha256:"+sha(raw):
        raise ValueError("Frozen archive checksum mismatch")
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        if set(z.namelist())!={"report.json","source.json","seal.json"}:
            raise ValueError("Unexpected frozen archive layout")
        report_bytes,source_bytes=z.read("report.json"),z.read("source.json")
        stamp=json.loads(z.read("seal.json"))
    if stamp["report_sha256"]!=sha(report_bytes) or stamp["source_sha256"]!=sha(source_bytes):
        raise ValueError("Frozen ledger checksum mismatch")
    base,source=json.loads(report_bytes),json.loads(source_bytes)
    if (base["start_ms"],base["end_ms"])!=(begin,window["start_ms"]):
        raise ValueError("Frozen cohort differs from requested week")
    if (stamp["start_ms"],stamp["end_ms"])!=(begin,window["start_ms"]):
        raise ValueError("Seal differs from requested week")
    candles=asyncio.run(public_candles(base,source,end_ms=window["end_ms"],output=output/"candles"))
    report=extend(base,source,candles,end_ms=window["end_ms"])
    report.update(base_artifact_id=artifact["id"],base_artifact_digest=artifact["digest"],base_report_sha256=sha(report_bytes))
    (output/"followup-report.json").write_text(json.dumps(report,allow_nan=False)+"\n")
    summary={k:v for k,v in report.items() if k!="records"}
    (output/"followup-summary.json").write_text(json.dumps(summary,indent=2,allow_nan=False)+"\n")
    gaps=sum(a["status"]=="OPEN" and b["status"]=="DATA_GAP" for a,b in zip(base["records"],report["records"]))
    return dict(status="FOLLOWUP_DATA_GAPS" if gaps else "FOLLOWUP_RECORDED",data_gap_records=gaps,
                cohort_start_ms=begin,cohort_end_ms=base["end_ms"],
                end_ms=window["end_ms"],newly_resolved=report["newly_resolved"],automatic_promotion=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seal-research",type=Path)
    parser.add_argument("--scheduled",action="store_true")
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    if bool(args.seal_research)==args.scheduled:
        parser.error("Choose exactly one mode")
    if args.seal_research:
        seal(args.seal_research,args.output)
    else:
        try:state=scheduled(args.output,now_ms=int(time.time()*1000))
        except Exception as exc:
            args.output.mkdir(parents=True,exist_ok=True)
            (args.output/"run-status.json").write_text(json.dumps(dict(status="FOLLOWUP_FAILED",error_type=type(exc).__name__))+"\n")
            raise
        (args.output/"run-status.json").write_text(json.dumps(state,indent=2)+"\n")
        print(json.dumps(state))


if __name__=="__main__":
    main()
