"""Describe frozen research ledgers; never create signals, fills or outcomes."""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path

from analysis_terminal import pending_entry_replay as study
from analysis_terminal.setups import setup_identity

STATUSES={"PENDING","OPEN","TP","SL","AMBIGUOUS","EXPIRED","INVALIDATED",
          "INVALIDATED_GAP","REJECTED_AT_FILL","DATA_GAP"}


def percentage(numerator,denominator):
    return round(100*numerator/denominator,6) if denominator else None


def summarize(report):
    dataset=report.get("dataset")
    if (dataset not in {"RETROSPECTIVE","RETROSPECTIVE_OUTCOME_FOLLOWUP"} or
            any(report.get(k) is not False for k in ("eligible_for_live_promotion","automatic_promotion","real_orders_enabled"))):
        raise ValueError("Only isolated research ledgers are supported")
    if dataset=="RETROSPECTIVE":
        if report.get("protocol_sha256")!=hashlib.sha256(study.PROTOCOL.read_bytes()).hexdigest():
            raise ValueError("Entry protocol differs from frozen research")
        start,end,entry_end=report["start_ms"],report["end_ms"],report["end_ms"]
    else:
        if report.get("no_new_entries") is not True or report.get("changes_live_rules") is not False:
            raise ValueError("Follow-up must preserve original entries")
        start,entry_end,end=report["cohort_start_ms"],report["cohort_end_ms"],report["followup_end_ms"]
    if any(not isinstance(v,int) or isinstance(v,bool) or v%study.STEP for v in (start,entry_end,end)) or not start<entry_end<=end:
        raise ValueError("Invalid closed-bar cohort boundaries")
    records=report["records"]
    if len({r["key"] for r in records})!=len(records):
        raise ValueError("Duplicate frozen record")
    for r in records:
        _,breakout,level=r["setup_id"].rsplit(":",2)
        identity=setup_identity(dict(ticker=r["ticker"],direction=r["side"],breakout_time_ms=breakout,breakout_level=level))
        if (r["model"] not in study.MODELS or identity!=r["setup_id"] or r["key"]!=r["model"]+":"+identity or
                r["status"] not in STATUSES or r["created_ms"]!=r["signal_candle_ms"]+study.STEP+1 or
                r["signal_candle_ms"]%study.STEP or not start<=r["created_ms"]-1<entry_end):
            raise ValueError("Invalid setup identity or signal chronology")
        for field in ("trigger","stop","target","mfe_r","mae_r"):
            if (isinstance(r[field],bool) or not math.isfinite(r[field]) or r[field]<0 or
                    field in {"trigger","stop","target"} and r[field]==0):
                raise ValueError("Invalid frozen numeric data")
        fill=r["filled_ms"]
        if fill is not None and (not isinstance(fill,int) or isinstance(fill,bool) or fill%study.STEP or
                                 not r["created_ms"]-1<=fill<min(r["expires_ms"],entry_end)):
            raise ValueError("Fill outside original entry window")
        if r["status"] in {"TP","SL","OPEN"} and fill is None:
            raise ValueError("Filled status without a fill")
        if r["status"] in {"TP","SL"}:
            if isinstance(r["final_net_r"],bool) or not math.isfinite(r["final_net_r"]):
                raise ValueError("Resolved result missing net R")
        elif r["final_net_r"] is not None:
            raise ValueError("Unresolved result cannot have realized R")
        outcome=r["outcome_ms"]
        if r["status"] not in {"OPEN","PENDING"} and outcome is None:
            raise ValueError("Terminal status without outcome time")
        if outcome is not None and (not isinstance(outcome,int) or isinstance(outcome,bool) or outcome%study.STEP or
                not r["created_ms"]-1<=outcome<=end or fill is not None and outcome<fill+study.STEP):
            raise ValueError("Outcome before fill or outside observation")
    models={}
    filled_sets={}
    for model in study.MODELS:
        rows=[r for r in records if r["model"]==model]
        observed=study.metrics(rows)
        capped=study.portfolio(rows)
        if observed!=report["metrics"][model] or capped!=report["portfolios"][model]:
            raise ValueError("Saved metrics or capital model differ from ledger")
        filled=[r for r in rows if r["filled_ms"] is not None]
        unfilled=[r for r in rows if r["filled_ms"] is None]
        filled_sets[model]={r["setup_id"] for r in filled}
        waits=Counter((r["filled_ms"]-(r["created_ms"]-1))//study.STEP+1 for r in filled)
        models[model]=dict(uncapped=dict(candidates=len(rows),filled=len(filled),unfilled=len(unfilled),
                                        fill_rate_pct=percentage(len(filled),len(rows)),
                                        unfilled_status_counts=dict(Counter(r["status"] for r in unfilled)),
                                        filled_status_counts=dict(Counter(r["status"] for r in filled)),
                                        expired_pct=percentage(sum(r["status"]=="EXPIRED" for r in unfilled),len(rows)),
                                        fill_wait_bars={str(k):v for k,v in sorted(waits.items())},
                                        observed_metrics=observed),
                           capped=capped,
                           filled_omitted_by_capital_model=len(filled)-capped["filled"],
                           fill_retention_after_capital_model_pct=percentage(capped["filled"],len(filled)))
    current=filled_sets["current_next_open"];proposal=filled_sets[study.MODEL]
    comparison=dict(current_filled=len(current),proposal_filled=len(proposal),
                    shared_filled_setups=len(current&proposal),
                    proposal_only_filled_setups=len(proposal-current),
                    current_filled_missing_in_proposal=len(current-proposal),
                    net_filled_count_difference=len(proposal)-len(current),
                    capped_filled_count_difference=models[study.MODEL]["capped"]["filled"]-models["current_next_open"]["capped"]["filled"],
                    capped_shared_setup_count=None)
    return dict(dataset="FROZEN_RESEARCH_EXECUTION_FUNNEL",source_dataset=dataset,
                source_role=report.get("role"),cohort_start_ms=start,cohort_end_ms=entry_end,observed_end_ms=end,
                period_complete=report.get("period_complete"),followup_complete=report.get("followup_complete"),
                same_cohort_update=dataset=="RETROSPECTIVE_OUTCOME_FOLLOWUP",
                r_basis="NET_STOP_RISK_WITH_5BPS_FEE_2BPS_ADVERSE_SLIPPAGE_EACH_SIDE",
                models=models,proposal_vs_current=comparison,
                changes_live_rules=False,real_orders_enabled=False,automatic_promotion=False,eligible_for_live_promotion=False,
                limitations=["Descriptive accounting of the same ledger; not new signals, causal effects or live fills.",
                             "Proposal-only setups are not the net increase; current-only filled setups are also shown.",
                             "Capital exclusions include unfilled candidates; never attribute every omitted fill to one exclusion count.",
                             "Aggregate capped counts do not identify shared admitted setups; that count remains unknown.",
                             "Do not tune waiting time or rank subgroups from this report; insufficient samples remain insufficient.",
                             "R and portfolio return are distinct; OPEN/uncertain exposure retains the source ROI limitations."])


def write_report(source,output,expected_sha256=None):
    raw=source.read_bytes();digest=hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and digest!=expected_sha256:
        raise ValueError("Source ledger checksum mismatch")
    result=summarize(json.loads(raw))
    result.update(source_report_sha256=digest,diagnostic_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n")
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--expected-sha256")
    args=parser.parse_args()
    result=write_report(args.report,args.output,args.expected_sha256)
    print(json.dumps(result["proposal_vs_current"]))


if __name__=="__main__":
    main()
