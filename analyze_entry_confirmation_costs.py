#!/usr/bin/env python3
import csv, json, math
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SRC=Path("backtest_results/entry_confirmation_trades.csv")
OUT=Path("backtest_results/entry_confirmation_costs.json")
START=200.0
RISKS=[0.01,0.02,0.03,0.05]
CAPS=[0.10,0.15,0.20]
# Round-trip execution stress scenarios. Funding is excluded because historical per-trade funding data is not in the source dataset.
COSTS={
 "fee_only_taker":{"entry_pct":0.00038,"exit_pct":0.00038},
 "fee_plus_slip_005":{"entry_pct":0.00038+0.0005,"exit_pct":0.00038+0.0005},
 "fee_plus_slip_010":{"entry_pct":0.00038+0.0010,"exit_pct":0.00038+0.0010},
 "fee_plus_slip_020":{"entry_pct":0.00038+0.0020,"exit_pct":0.00038+0.0020},
}

def ts(s):
    return datetime.fromisoformat(s).timestamp() if s else None

def load():
    rows=[]
    with SRC.open(encoding="utf-8",newline="") as f:
        for x in csv.DictReader(f):
            if not x["exit_time_utc"] or x["r_result"]=="":
                continue
            entry=float(x["entry"]); stop=float(x["stop"])
            stop_pct=abs(entry-stop)/entry
            if stop_pct<=0: continue
            rows.append({
              "variant":x["variant"],"symbol":x["symbol"],"entry_t":ts(x["entry_time_utc"]),"exit_t":ts(x["exit_time_utc"]),
              "r":float(x["r_result"]),"stop_pct":stop_pct,"rr":float(x["rr_planned"]),
            })
    rows.sort(key=lambda z:(z["entry_t"],z["symbol"]))
    return rows

def cost_r(t,c):
    # Approximate turnover cost in R. Split trades are conservatively charged on full notional once in and once out.
    return (c["entry_pct"]+c["exit_pct"])/t["stop_pct"]

def simulate(rows,risk,cap,cost,one_symbol=True):
    equity=START; peak=START; maxdd=0.0
    active=[]; accepted=skipped_cap=skipped_symbol=0
    wins=losses=0; net_r=gp=gl=0.0; max_open=0; max_open_risk=0.0
    loss_streak=max_loss_streak=0
    for t in rows:
        # Realize exits before this new entry.
        exiting=[p for p in active if p["exit_t"]<=t["entry_t"]]
        for p in sorted(exiting,key=lambda p:p["exit_t"]):
            rr=p["net_r"]
            equity *= max(0.0,1.0+risk*rr)
            net_r+=rr
            if rr>0: wins+=1; gp+=rr; loss_streak=0
            elif rr<0: losses+=1; gl+=-rr; loss_streak+=1; max_loss_streak=max(max_loss_streak,loss_streak)
            peak=max(peak,equity)
            if peak>0:maxdd=max(maxdd,(peak-equity)/peak)
        active=[p for p in active if p["exit_t"]>t["entry_t"]]
        if one_symbol and any(p["symbol"]==t["symbol"] for p in active):
            skipped_symbol+=1; continue
        open_risk=len(active)*risk
        if open_risk+risk>cap+1e-12:
            skipped_cap+=1; continue
        nr=t["r"]-cost_r(t,cost)
        active.append({**t,"net_r":nr})
        accepted+=1
        max_open=max(max_open,len(active))
        max_open_risk=max(max_open_risk,len(active)*risk)
    for p in sorted(active,key=lambda p:p["exit_t"]):
        rr=p["net_r"]; equity*=max(0.0,1.0+risk*rr); net_r+=rr
        if rr>0:wins+=1;gp+=rr;loss_streak=0
        elif rr<0:losses+=1;gl+=-rr;loss_streak+=1;max_loss_streak=max(max_loss_streak,loss_streak)
        peak=max(peak,equity)
        if peak>0:maxdd=max(maxdd,(peak-equity)/peak)
    closed=wins+losses
    return {
      "risk_pct":risk*100,"portfolio_cap_pct":cap*100,"accepted":accepted,
      "skipped_portfolio_cap":skipped_cap,"skipped_same_symbol":skipped_symbol,
      "closed":closed,"wins":wins,"losses":losses,
      "win_rate_pct":100*wins/closed if closed else None,
      "net_r_after_cost":net_r,"avg_r_after_cost":net_r/closed if closed else None,
      "profit_factor_after_cost":gp/gl if gl else None,
      "ending_equity_usdc":equity,"return_pct":100*(equity/START-1),
      "max_drawdown_pct":100*maxdd,"max_loss_streak":max_loss_streak,
      "max_simultaneous_positions":max_open,"max_open_risk_pct":100*max_open_risk,
    }

rows=load()
variants=sorted(set(x["variant"] for x in rows))
result={"starting_equity_usdc":START,"variants":{}}
for variant in variants:
    vrows=[x for x in rows if x["variant"]==variant]
    raw_net=sum(x["r"] for x in vrows)
    stop_pcts=sorted(x["stop_pct"] for x in vrows)
    def pct(v,p):
        if not v:return None
        k=(len(v)-1)*p; a=math.floor(k); b=math.ceil(k)
        return v[a] if a==b else v[a]*(b-k)+v[b]*(k-a)
    block={
      "source_trades":len(vrows),
      "raw_net_r_before_cost":raw_net,
      "stop_distance_pct":{"p10":100*pct(stop_pcts,.1),"median":100*pct(stop_pcts,.5),"p90":100*pct(stop_pcts,.9),"mean":100*sum(stop_pcts)/len(stop_pcts)},
      "cost_scenarios":{}
    }
    for name,cost in COSTS.items():
        costs=[cost_r(t,cost) for t in vrows]
        cb={
          "assumption":cost,
          "cost_r":{"mean":sum(costs)/len(costs),"median":pct(sorted(costs),.5),"p90":pct(sorted(costs),.9)},
          "no_portfolio_filter":{
            "net_r_after_cost":sum(t["r"]-cost_r(t,cost) for t in vrows),
            "avg_r_after_cost":sum(t["r"]-cost_r(t,cost) for t in vrows)/len(vrows)
          },
          "portfolio_grid":[]
        }
        for risk in RISKS:
            for cap in CAPS:
                if risk<=cap:
                    cb["portfolio_grid"].append(simulate(vrows,risk,cap,cost,True))
        block["cost_scenarios"][name]=cb
    result["variants"][variant]=block
OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
print(json.dumps(result,ensure_ascii=False))
