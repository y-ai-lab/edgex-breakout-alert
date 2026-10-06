"""Causal fill, cost and capital checks for the isolated registered experiment."""
from dataclasses import replace
from types import SimpleNamespace
import unittest

import app
from analysis_terminal import pending_entry_replay as study
from analysis_terminal.setups import setup_identity

STEP, START = study.STEP, 100*16*study.STEP
CONTRACT = app.Contract("1","TESTUSDC","USDC",True,True,.01,.01,1000)
SETTINGS = SimpleNamespace(monitor_interval="HOUR_4",entry_interval="MINUTE_15",roll_max_age=6,retest_lookback=4,min_rr=2)


def row(side="LONG"):
    r=dict(ticker="TESTUSDC",direction=side,breakout_time_ms=START-16*STEP,
           breakout_level=100,retest_touched=True,confirmed=True,
           entry_reference=101 if side=="LONG" else 99,
           stage="RR_WAIT",entry_band=dict(structural_stop=90 if side=="LONG" else 110,
                                          structural_target=120 if side=="LONG" else 80))
    r["setup_id"]=setup_identity(r)
    return r


def record(side="LONG", model=study.MODEL):
    r=row(side)
    if model=="current_next_open":
        r.update(stage="READY",stop_loss=90,take_profit=125,entry_reference=100)
    return study.candidate(r,model,candle_ms=START-STEP) | dict(step_size=.01,min_order_size=.01,max_order_size=1000)


def candle(stamp,**kwargs):
    d=dict(contract_id="1",contract_name="TESTUSDC",interval="MINUTE_15",time_ms=stamp,
           open=101,high=102,low=100,close=101,volume=1,value=100,trades=None)
    d.update(kwargs)
    return app.Candle(**d)


def evaluate(r,cs,states=None,end=None):
    if states is None:
        states={c.time_ms:r["setup_id"] for c in cs}
    return study.evaluate(r,cs,states,end_ms=end or max(c.time_ms for c in cs)+STEP)


class PendingEntryTests(unittest.TestCase):
    def test_research_collection_excludes_today_and_keeps_fixed_week_boundaries(self):
        origin=1791331200000;day=86400000
        self.assertEqual(study.research_window(origin+day-1,origin)["days"],0)
        first=study.research_window(origin+day+12345,origin)
        self.assertEqual((first["start_ms"],first["end_ms"],first["days"]),(origin,origin+day,1))
        self.assertEqual(study.research_window(origin+7*day,origin)["days"],7)
        second=study.research_window(origin+8*day,origin)
        self.assertEqual((second["start_ms"],second["days"]),(origin+7*day,1))

    def test_cost_boundary_accounts_for_both_fees_and_both_slippages(self):
        for side,stop,target in (("LONG",90,120),("SHORT",110,80)):
            trigger=study.pullback_trigger(stop,target,side)
            self.assertAlmostEqual(study.cost_levels(trigger,stop,target,side)["net_rr"],2)
            worse=trigger*(1.0001 if side=="LONG" else .9999)
            self.assertLess(study.cost_levels(worse,stop,target,side)["net_rr"],2)

    def test_fee_dominated_structure_has_no_feasible_entry(self):
        self.assertIsNone(study.pullback_trigger(100,100.01,"LONG"))
        self.assertIsNone(study.pullback_trigger(100.01,100,"SHORT"))

    def test_confirmation_and_tp_beyond_roll_required(self):
        r=row();r["confirmed"]=False
        self.assertIsNone(study.candidate(r,study.MODEL,candle_ms=START-STEP))
        r=row();r["entry_band"]["structural_target"]=99
        self.assertIsNone(study.candidate(r,study.MODEL,candle_ms=START-STEP))

    def test_signal_candle_never_fills_or_resolves_trade(self):
        r=record()
        result=evaluate(r,[candle(START-STEP,high=130,low=80),candle(START)],end=START+STEP)
        self.assertEqual(result["status"],"PENDING")
        self.assertIsNone(result["filled_ms"])

    def test_later_touch_fills_then_later_tp_returns_cost_two_r(self):
        r=record()
        result=evaluate(r,[candle(START,low=r["trigger"]-.01),candle(START+STEP,open=110,high=121,low=109,close=120)])
        self.assertEqual((result["status"],result["filled_ms"],result["outcome_ms"]),("TP",START,START+2*STEP))
        self.assertAlmostEqual(result["final_net_r"],2)

    def test_short_is_symmetric_and_keeps_original_stop_target(self):
        r=record("SHORT")
        result=evaluate(r,[candle(START,open=99,low=98,high=r["trigger"]+.01,close=99),
                           candle(START+STEP,open=90,high=91,low=79,close=80)])
        self.assertEqual(result["status"],"TP")
        self.assertAlmostEqual(result["final_net_r"],2)
        self.assertEqual((result["stop"],result["target"]),(110,80))

    def test_lone_tp_or_sl_on_fill_bar_is_ambiguous(self):
        r=record()
        for cs in ([candle(START,low=r["trigger"]-.1,high=121)], [candle(START,low=89)]):
            result=evaluate(r,cs)
            self.assertEqual(result["status"],"AMBIGUOUS")
            self.assertIsNone(result["final_net_r"])
            self.assertEqual(result["mfe_r"],0)

    def test_gap_before_entry_cannot_fill_on_later_candle(self):
        r=record()
        result=evaluate(r,[candle(START+STEP,low=95),candle(START+2*STEP,high=130)],end=START+3*STEP)
        self.assertEqual(result["status"],"DATA_GAP")
        self.assertIsNone(result["filled_ms"])

    def test_gap_after_entry_blocks_later_tp(self):
        r=record()
        result=evaluate(r,[candle(START,low=95),candle(START+2*STEP,high=130)],end=START+3*STEP)
        self.assertEqual(result["status"],"DATA_GAP")
        self.assertIsNone(result["final_net_r"])

    def test_open_through_stop_cancels_pending(self):
        result=evaluate(record(),[candle(START,open=89,low=88,high=99,close=95)])
        self.assertEqual(result["status"],"INVALIDATED_GAP")
        self.assertIsNone(result["filled_ms"])

    def test_only_prior_known_setup_can_invalidate(self):
        r=record()
        result=evaluate(r,[candle(START),candle(START+STEP,low=95)],states={START:r["setup_id"],START+STEP:None})
        self.assertEqual(result["status"],"INVALIDATED")
        self.assertIsNone(result["filled_ms"])

    def test_four_bar_deadline_is_not_extended(self):
        r=record()
        cs=[candle(START+i*STEP) for i in range(5)]
        cs[-1]=candle(START+4*STEP,low=95)
        result=evaluate(r,cs)
        self.assertEqual(result["status"],"EXPIRED")
        self.assertIsNone(result["filled_ms"])

    def test_forming_future_candles_do_not_affect_outcome(self):
        r=record()
        cs=[candle(START,low=95),candle(START+STEP,high=130,low=80)]
        self.assertEqual(evaluate(r,cs,end=START+STEP),evaluate(r,cs[:1],end=START+STEP))

    def test_gap_stop_loss_can_exceed_planned_one_r(self):
        r=record()
        result=evaluate(r,[candle(START,low=95),candle(START+STEP,open=85,low=84,high=89,close=86)])
        self.assertEqual(result["status"],"SL")
        self.assertLess(result["final_net_r"],-1)

    def test_next_open_baseline_can_resolve_fill_bar_but_not_signal_bar(self):
        r=record(model="current_next_open")
        result=evaluate(r,[candle(START-STEP,high=140),candle(START,open=100,high=126,low=99,close=125)])
        self.assertEqual(result["status"],"TP")
        self.assertLess(result["final_net_r"],2.5)

    def test_portfolio_cash_uses_resolved_pnl_not_r_times_capital(self):
        r=record()
        result=evaluate(r,[candle(START,low=95),candle(START+STEP,open=110,low=109,high=121,close=120)])
        p=study.portfolio([result])
        self.assertEqual((p["resolved"],p["active"]),(1,0))
        self.assertAlmostEqual(p["closed_portfolio_roi_pct"],p["realized_net_pnl_usdc"]/100)
        self.assertLessEqual(p["realized_net_pnl_usdc"],200)

    def test_unknown_outcome_locks_risk_and_suppresses_roi(self):
        result=evaluate(record(),[candle(START,low=95,high=130)])
        p=study.portfolio([result])
        self.assertEqual(p["uncertain"],1)
        self.assertIsNone(p["closed_portfolio_roi_pct"])
        self.assertIsNone(p["realized_roi_pct"])

    def test_future_profit_never_enlarges_pending_quantity(self):
        r=record();cs=[candle(START,low=95),candle(START+STEP,open=110,low=109,high=121,close=120)]
        first=evaluate(r,cs)
        second=dict(first,key="second",ticker="OTHERUSDC")
        single=study.portfolio([first]);both=study.portfolio([first,second])
        self.assertAlmostEqual(both["realized_net_pnl_usdc"],2*single["realized_net_pnl_usdc"])

    def test_unknown_size_rules_never_assume_tradable_quantity(self):
        r=record();r["step_size"]=None
        p=study.portfolio([r])
        self.assertEqual(p["admitted"],0)
        self.assertEqual(p["exclusions"]["UNKNOWN_SIZE_RULES"],1)

    def test_rejected_first_bar_releases_reservation_after_creation(self):
        r=record(model="current_next_open")
        result=evaluate(r,[candle(START,open=124,low=123,high=125,close=124)])
        self.assertEqual(result["status"],"REJECTED_AT_FILL")
        p=study.portfolio([result])
        self.assertEqual((p["admitted"],p["filled"],p["active"]),(1,0,0))
        self.assertEqual(p["closed_portfolio_roi_pct"],0)

    def test_expiry_at_last_closed_boundary_releases_pending(self):
        r=record()
        result=evaluate(r,[candle(START+i*STEP) for i in range(4)])
        self.assertEqual(result["status"],"EXPIRED")
        self.assertEqual(study.portfolio([result])["active"],0)

    def test_protocol_kills_negative_sufficient_period_no_auto_tuning(self):
        def p(n,r,pf):
            return dict(metrics={study.MODEL:dict(resolved=n,avg_net_r=r,profit_factor=pf)},
                        opportunities=dict(additional_filled_setups_vs_current=1))
        self.assertEqual(study.decision([p(19,-1,0),p(1,2,"INF")]),"CONTINUE_INSUFFICIENT_SAMPLE")
        self.assertEqual(study.decision([p(20,-.1,.9),p(1,2,"INF")]),"KILL")
        self.assertEqual(study.decision([p(20,.1,1.1),p(20,.2,1.2)]),"CONTINUE_FORWARD_SHADOW_REQUIRED")

    def test_full_replay_never_rearms_or_uses_unknown_first_confirmation(self):
        end=START+6*STEP
        monitor=[replace(candle(t),interval="HOUR_4") for t in range(START-12*16*STEP,end+16*STEP,16*STEP)]
        entries=[candle(t) for t in range(START-9*16*STEP,end+STEP,STEP)]
        def analyze(contract,m,e,*,as_of_ms):
            self.assertTrue(all(c.time_ms+16*STEP<=as_of_ms for c in m))
            self.assertTrue(all(c.time_ms+STEP<=as_of_ms for c in e))
            return row() if as_of_ms>=START else dict(ticker="TESTUSDC",stage="RETEST_WAIT")
        settings=SimpleNamespace(**vars(SETTINGS))
        result=study.replay_market(CONTRACT,monitor,entries,analyze,settings,start_ms=START,end_ms=end,window=2)
        self.assertEqual(len(result["records"]),1)
        self.assertEqual(result["records"][0]["status"],"EXPIRED")
        entries=[c for c in entries if c.time_ms!=START-STEP]
        result=study.replay_market(CONTRACT,monitor,entries,analyze,settings,start_ms=START,end_ms=end,window=2)
        self.assertEqual(result["records"],[])


if __name__=="__main__":
    unittest.main()
