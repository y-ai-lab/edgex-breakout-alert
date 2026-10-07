"""Frozen cohort identity, chronology, uncertainty and artifact regressions."""
import copy
from dataclasses import asdict,replace
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,AsyncMock,Mock
import zipfile

from analysis_terminal import pending_followup as follow
from analysis_terminal import pending_entry_replay as study
from analysis_terminal.test_pending_entry_replay import record,candle,evaluate,START,STEP,CONTRACT


def fixture(status="OPEN",side="LONG"):
    r=evaluate(record(side),[candle(START,low=95)] if side=="LONG" else [candle(START,open=99,low=98,high=105,close=99)])
    if status=="PENDING":r=record()
    elif status=="SL":r=evaluate(record(),[candle(START,low=95),candle(START+STEP,open=100,low=89)])
    policy=json.loads(follow.POLICY.read_text())
    base=dict(dataset="RETROSPECTIVE",eligible_for_live_promotion=False,automatic_promotion=False,real_orders_enabled=False,
              protocol_sha256=policy["original_entry_protocol_sha256"],engine_sha256=policy["known_original_engine_sha256"],
              production_rule_fingerprint=policy["entry_rule_fingerprint"],source_rule_fingerprint=policy["entry_rule_fingerprint"],
              start_ms=START,end_ms=START+2*STEP,period_complete=False,records=[r])
    base["metrics"]={m:study.metrics([r] if r["model"]==m else []) for m in study.MODELS}
    source=dict(dataset="RETROSPECTIVE",eligible_for_live_promotion=False,start_ms=base["start_ms"],end_ms=base["end_ms"],
                manifest=dict(rule_fingerprint=policy["entry_rule_fingerprint"],parameters=json.loads(study.PROTOCOL.read_text())["production_parameters"],
                              universe=[asdict(CONTRACT)],sources=[dict(ticker=CONTRACT.contract_name,file="candles/1.json")],failures=[]))
    return base,source


def anchored_fixture():
    base,source=fixture()
    origin=json.loads(study.PROTOCOL.read_text())["prospective_start_ms"]
    shift=origin-START
    r=base["records"][0]
    for field in ("created_ms","filled_ms","signal_candle_ms","expires_ms"):
        r[field]+=shift
    r["setup_id"]=f"setup-v1:TESTUSDC:LONG:{origin-16*STEP}:100"
    r["key"]=r["model"]+":"+r["setup_id"]
    base.update(start_ms=origin,end_ms=origin+7*follow.DAY,period_complete=True)
    source.update(start_ms=base["start_ms"],end_ms=base["end_ms"])
    return base,source


class FollowupTests(unittest.TestCase):
    def test_later_tp_extends_same_fill_without_charging_entry_again(self):
        base,source=fixture();before=copy.deepcopy(base)
        end=base["end_ms"]+STEP
        result=follow.extend(base,source,{"TESTUSDC":[candle(base["end_ms"],open=110,low=109,high=121,close=120)]},end_ms=end)
        new=result["records"][0];old=base["records"][0]
        self.assertEqual((new["status"],result["newly_resolved"]),("TP",1))
        for key in ("key","setup_id","created_ms","filled_ms","entry","stop","target","net_risk"):
            self.assertEqual(new[key],old[key])
        self.assertAlmostEqual(new["final_net_r"],2)
        self.assertEqual(result["metrics"][study.MODEL]["filled"],base["metrics"][study.MODEL]["filled"])
        self.assertAlmostEqual(result["portfolios"][study.MODEL]["closed_portfolio_roi_pct"],
                               result["portfolios"][study.MODEL]["realized_net_pnl_usdc"]/100)
        self.assertEqual(base,before)

    def test_short_followup_retains_same_costs_and_ordered_direction(self):
        base,source=fixture(side="SHORT");start=base["end_ms"]
        r=follow.extend(base,source,{"TESTUSDC":[candle(start,open=90,high=91,low=79,close=80)]},end_ms=start+STEP)
        self.assertEqual(r["records"][0]["status"],"TP")
        self.assertAlmostEqual(r["records"][0]["final_net_r"],2)

    def test_old_and_forming_candles_never_resolve_followup(self):
        base,source=fixture();start=base["end_ms"]
        cs=[candle(START-STEP,high=130,low=80),candle(start),candle(start+STEP,high=130)]
        r=follow.extend(base,source,{"TESTUSDC":cs},end_ms=start+STEP)
        self.assertEqual(r["records"][0]["status"],"OPEN")
        self.assertEqual(r["newly_resolved"],0)

    def test_missing_first_bar_blocks_later_profit_and_keeps_capital_unknown(self):
        base,source=fixture();start=base["end_ms"]
        r=follow.extend(base,source,{"TESTUSDC":[candle(start+STEP,high=130)]},end_ms=start+2*STEP)
        self.assertEqual(r["records"][0]["status"],"DATA_GAP")
        self.assertIsNone(r["records"][0]["final_net_r"])
        self.assertIsNone(r["portfolios"][study.MODEL]["equity_usdc"])

    def test_both_exit_touches_are_ambiguous_and_never_count_as_resolved(self):
        base,source=fixture();start=base["end_ms"]
        r=follow.extend(base,source,{"TESTUSDC":[candle(start,high=130,low=80)]},end_ms=start+STEP)
        self.assertEqual(r["records"][0]["status"],"AMBIGUOUS")
        self.assertEqual(r["metrics"][study.MODEL]["resolved"],0)
        self.assertIsNone(r["portfolios"][study.MODEL]["closed_portfolio_roi_pct"])

    def test_stop_gap_is_charged_beyond_one_r_without_widening_stop(self):
        base,source=fixture();start=base["end_ms"]
        r=follow.extend(base,source,{"TESTUSDC":[candle(start,open=85,low=84,high=86,close=85)]},end_ms=start+STEP)
        self.assertEqual(r["records"][0]["status"],"SL")
        self.assertLess(r["records"][0]["final_net_r"],-1)
        self.assertEqual(r["records"][0]["stop"],base["records"][0]["stop"])

    def test_terminal_and_unfilled_pending_records_are_never_reopened(self):
        for status in ("SL","PENDING"):
            base,source=fixture(status);start=base["end_ms"]
            r=follow.extend(base,source,{"TESTUSDC":[candle(start,high=130,low=95)]},end_ms=start+STEP)
            self.assertEqual(r["records"],base["records"])
            self.assertEqual(r["newly_resolved"],0)
            if status=="PENDING":
                self.assertEqual(r["censored_unfilled_pending"],1)
                self.assertIsNone(r["portfolios"][study.MODEL]["closed_portfolio_roi_pct"])

    def test_same_ticker_does_not_merge_different_setup_records(self):
        base,source=fixture();other=copy.deepcopy(base["records"][0])
        other["setup_id"]=other["setup_id"].rsplit(":",1)[0]+":101"
        other["key"]=other["model"]+":"+other["setup_id"]
        base["records"].append(other)
        base["metrics"][study.MODEL]=study.metrics(base["records"])
        start=base["end_ms"]
        r=follow.extend(base,source,{"TESTUSDC":[candle(start,open=110,high=121,low=109,close=120)]},end_ms=start+STEP)
        self.assertEqual(len(r["records"]),2)
        self.assertNotEqual(r["records"][0]["setup_id"],r["records"][1]["setup_id"])

    def test_duplicate_wrong_identity_or_frozen_price_change_fails_closed(self):
        for mode in ("duplicate","price","identity"):
            base,source=fixture()
            if mode=="duplicate":base["records"].append(copy.deepcopy(base["records"][0]))
            if mode=="price":base["records"][0]["entry"]+=1
            if mode=="identity":base["records"][0]["ticker"]="OTHERUSDC"
            with self.assertRaises(ValueError):follow.validate_base(base,source)

    def test_protocol_fingerprint_parameters_and_horizon_are_fixed(self):
        for field in ("protocol_sha256","engine_sha256","production_rule_fingerprint"):
            base,source=fixture();base[field]="unknown"
            with self.assertRaises(ValueError):follow.validate_base(base,source)
        base,source=fixture();source["manifest"]["parameters"]["min_rr"]=1.5
        with self.assertRaises(ValueError):follow.validate_base(base,source)
        base,source=fixture()
        with self.assertRaises(ValueError):follow.extend(base,source,{},end_ms=base["end_ms"]+8*follow.DAY)

    def test_wrong_candle_identity_nan_duplicate_or_bad_ohlc_is_rejected(self):
        for change in (dict(contract_id="other"),dict(open=float('nan')),dict(interval="MINUTE_5"),dict(low=105)):
            base,source=fixture();start=base["end_ms"]
            with self.assertRaises(ValueError):follow.extend(base,source,{"TESTUSDC":[candle(start,**change)]},end_ms=start+STEP)
        base,source=fixture();start=base["end_ms"]
        with self.assertRaises(ValueError):follow.extend(base,source,{"TESTUSDC":[candle(start),candle(start)]},end_ms=start+STEP)

    def test_resume_cannot_start_on_signal_or_fill_candle(self):
        base,_=fixture();r=base["records"][0]
        for start in (r["filled_ms"],r["signal_candle_ms"],r["filled_ms"]+1):
            with self.assertRaises(ValueError):study.evaluate(r,[],{},end_ms=base["end_ms"],start_ms=start)

    def test_waiting_before_first_sealed_week_does_not_access_network(self):
        origin=json.loads(study.PROTOCOL.read_text())["prospective_start_ms"]
        with tempfile.TemporaryDirectory() as d,patch.object(follow,"gh_json",side_effect=AssertionError("network")):
            self.assertEqual(follow.scheduled(Path(d),now_ms=origin+7*follow.DAY)["status"],"WAITING_FOR_FIRST_FROZEN_COHORT")

    def test_empty_missing_and_expired_artifacts_never_reconstruct_new_cohort(self):
        origin=json.loads(study.PROTOCOL.read_text())["prospective_start_ms"]
        for artifacts,status in (([],"MISSING_FROZEN_COHORT"),([dict(name=f'edgex-frozen-cohort-{origin}-{origin+7*follow.DAY}',expired=True,
                         workflow_run=dict(head_branch="main"),created_at="2026-10-14",id=1)],"FROZEN_COHORT_EXPIRED")):
            with tempfile.TemporaryDirectory() as d,patch.object(follow,"gh_json",return_value=dict(total_count=len(artifacts),artifacts=artifacts)):
                self.assertEqual(follow.scheduled(Path(d),now_ms=origin+8*follow.DAY)["status"],status)

    def test_seal_requires_complete_week_and_successful_full_universe(self):
        base,source=anchored_fixture()
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/"review").mkdir();(root/"source").mkdir()
            def write():
                (root/"review/pending-entry-report.json").write_text(json.dumps(base))
                (root/"source/replay-report.json").write_text(json.dumps(source))
            write();follow.seal(root,root/"frozen")
            stamp=json.loads((root/"frozen/seal.json").read_text())
            self.assertEqual(stamp["report_sha256"],hashlib.sha256((root/"frozen/report.json").read_bytes()).hexdigest())
            base["period_complete"]=False;write()
            with self.assertRaises(ValueError):follow.seal(root,root/"bad")
            base["period_complete"]=True;source["manifest"]["failures"]=[dict(ticker="TESTUSDC")];write()
            with self.assertRaises(ValueError):follow.seal(root,root/"bad")

    def test_scheduled_uses_earliest_main_archive_checks_digest_and_preserves_cohort(self):
        base,source=anchored_fixture();begin,end=base["start_ms"],base["end_ms"]
        report_raw,source_raw=json.dumps(base).encode(),json.dumps(source).encode()
        stamp=dict(report_sha256=follow.sha(report_raw),source_sha256=follow.sha(source_raw),start_ms=begin,end_ms=end)
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as z:
            for name,raw in (("report.json",report_raw),("source.json",source_raw),("seal.json",json.dumps(stamp))):z.writestr(name,raw)
        archive=stream.getvalue()
        def artifact(id,date,branch="main"):
            return dict(id=id,created_at=date,name=f'edgex-frozen-cohort-{begin}-{end}',expired=False,
                        digest="sha256:"+follow.sha(archive),workflow_run=dict(head_branch=branch))
        artifacts=[artifact(3,"2026-10-15"),artifact(1,"2026-10-14"),artifact(0,"2026-10-13","audit/other")]
        with tempfile.TemporaryDirectory() as d,patch.object(follow,"gh_json",return_value=dict(total_count=3,artifacts=artifacts)),\
             patch.object(follow.subprocess,"check_output",return_value=archive) as download,\
             patch.object(follow,"public_candles",AsyncMock(return_value={"TESTUSDC":[candle(end,open=110,low=109,high=121,close=120)]})):
            state=follow.scheduled(Path(d),now_ms=end+follow.DAY)
            result=json.loads((Path(d)/"followup-report.json").read_text())
            self.assertEqual(state["status"],"FOLLOWUP_RECORDED")
            self.assertEqual(result["base_artifact_id"],1)
            self.assertEqual(result["newly_resolved"],1)
            self.assertIn("/1/zip",download.call_args.args[0][-1])
            self.assertEqual(len(result["records"]),len(base["records"]))
            artifacts[1]["digest"]="sha256:modified"
            with self.assertRaises(ValueError):follow.scheduled(Path(d),now_ms=end+follow.DAY)


class RecoveryTests(unittest.TestCase):
    def archive(self):
        base,source=anchored_fixture();begin,end=base["start_ms"],base["end_ms"]
        report_raw,source_raw=json.dumps(base).encode(),json.dumps(source).encode()
        stamp=dict(report_sha256=follow.sha(report_raw),source_sha256=follow.sha(source_raw),start_ms=begin,end_ms=end)
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as z:
            for name,raw in (("report.json",report_raw),("source.json",source_raw),("seal.json",json.dumps(stamp))):
                z.writestr(name,raw)
        raw=stream.getvalue()
        artifact=dict(id=1,created_at="2026-10-14",name=f'edgex-frozen-cohort-{begin}-{end}',expired=False,
                      digest="sha256:"+follow.sha(raw),workflow_run=dict(head_branch="main"))
        return base,artifact,raw

    def test_recovery_waits_until_normal_followup_window_has_advanced(self):
        origin=json.loads(study.PROTOCOL.read_text())["prospective_start_ms"]
        with tempfile.TemporaryDirectory() as d,patch.object(follow,"gh_json",side_effect=AssertionError("network")):
            for days in (0,7,13,14):
                state=follow.scheduled(Path(d),now_ms=origin+days*follow.DAY,finalize_previous=True)
                self.assertEqual(state["status"],"WAITING_FOR_COMPLETED_FOLLOWUP")

    def test_missed_boundary_recovers_same_cohort_and_cutoff_through_next_week(self):
        base,artifact,raw=self.archive();begin,end=base["start_ms"],base["end_ms"]
        listing=dict(total_count=1,artifacts=[artifact])
        with tempfile.TemporaryDirectory() as d,patch.object(follow,"gh_json",return_value=listing) as query,\
             patch.object(follow.subprocess,"check_output",return_value=raw),\
             patch.object(follow,"public_candles",AsyncMock(return_value={"TESTUSDC":[candle(end,low=89)]})) as public:
            # On day 15 the normal path has moved to the next cohort. Recovery
            # still evaluates the original cohort through day 14, including day 21.
            for days in (15,16,20,21):
                state=follow.scheduled(Path(d),now_ms=begin+days*follow.DAY,finalize_previous=True)
                result=json.loads((Path(d)/"followup-report.json").read_text())
                self.assertEqual((state["cohort_start_ms"],state["cohort_end_ms"],state["end_ms"]),
                                 (begin,end,end+7*follow.DAY))
                self.assertTrue(result["followup_complete"])
                self.assertEqual(result["newly_resolved"],1)
                self.assertEqual(result["records"][0]["status"],"SL")
                self.assertEqual(public.call_args.kwargs["end_ms"],end+7*follow.DAY)
                self.assertIn(artifact["name"],query.call_args.args[0])
                for field in ("key","setup_id","created_ms","filled_ms","entry","stop","target"):
                    self.assertEqual(result["records"][0][field],base["records"][0][field])

    def test_recovery_excludes_profit_after_original_cutoff(self):
        base,artifact,raw=self.archive();end=base["end_ms"];cutoff=end+7*follow.DAY
        candles=[candle(t) for t in range(end,cutoff,STEP)]+[candle(cutoff,high=130)]
        with tempfile.TemporaryDirectory() as d,patch.object(follow,"gh_json",return_value=dict(total_count=1,artifacts=[artifact])),\
             patch.object(follow.subprocess,"check_output",return_value=raw),\
             patch.object(follow,"public_candles",AsyncMock(return_value={"TESTUSDC":candles})):
            follow.scheduled(Path(d),now_ms=cutoff+follow.DAY,finalize_previous=True)
            result=json.loads((Path(d)/"followup-report.json").read_text())
            self.assertEqual(result["records"][0]["status"],"OPEN")
            self.assertEqual(result["newly_resolved"],0)
            self.assertIsNone(result["portfolios"][study.MODEL]["closed_portfolio_roi_pct"])

    def test_recovery_missing_or_expired_cohort_never_reconstructs_entries(self):
        base,artifact,_=self.archive();artifact["expired"]=True
        with tempfile.TemporaryDirectory() as d,patch.object(follow.subprocess,"check_output",side_effect=AssertionError("download")):
            for artifacts,status in (([],"MISSING_FROZEN_COHORT"),([artifact],"FROZEN_COHORT_EXPIRED")):
                with patch.object(follow,"gh_json",return_value=dict(total_count=len(artifacts),artifacts=artifacts)):
                    state=follow.scheduled(Path(d),now_ms=base["end_ms"]+8*follow.DAY,finalize_previous=True)
                    self.assertEqual(state["status"],status)

    def test_recovery_missing_bar_does_not_resolve_using_later_profit(self):
        base,artifact,raw=self.archive();end=base["end_ms"]
        with tempfile.TemporaryDirectory() as d,patch.object(follow,"gh_json",return_value=dict(total_count=1,artifacts=[artifact])),\
             patch.object(follow.subprocess,"check_output",return_value=raw),\
             patch.object(follow,"public_candles",AsyncMock(return_value={"TESTUSDC":[candle(end+STEP,high=130)]})):
            state=follow.scheduled(Path(d),now_ms=end+8*follow.DAY,finalize_previous=True)
            result=json.loads((Path(d)/"followup-report.json").read_text())
            self.assertEqual(state["status"],"FOLLOWUP_DATA_GAPS")
            self.assertEqual(result["records"][0]["status"],"DATA_GAP")
            self.assertEqual(result["newly_resolved"],0)
            self.assertIsNone(result["portfolios"][study.MODEL]["equity_usdc"])


class PublicFollowupTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetches_only_public_15m_quotes_after_cohort_boundary(self):
        base,source=fixture();start=base["end_ms"]
        get=Mock(return_value={"code":"SUCCESS","data":{"dataList":[]}})
        with tempfile.TemporaryDirectory() as d:
            found=await follow.public_candles(base,source,end_ms=start+STEP,output=Path(d),get_json=get)
            self.assertEqual(found,{"TESTUSDC":[]})
            self.assertEqual(get.call_count,1)
            path,params=get.call_args.args
            self.assertEqual(path,"/api/v2/public/quote/getKline")
            self.assertEqual(params["klineType"],"MINUTE_15")
            self.assertEqual(params["priceType"],"LAST_PRICE")
            self.assertEqual(int(params["filterBeginKlineTimeInclusive"]),start)
            self.assertEqual(int(params["filterEndKlineTimeExclusive"]),start+STEP)
            manifest=json.loads((Path(d)/"manifest.json").read_text())
            raw=(Path(d)/manifest["sources"][0]["file"]).read_bytes()
            self.assertEqual(follow.sha(raw),manifest["sources"][0]["sha256"])


if __name__=="__main__":unittest.main()
