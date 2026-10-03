import json
import sqlite3
import subprocess
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient
from analysis_terminal.outcomes import verified_result
from analysis_terminal.tracking import tracking_state, tracking_summary
from analysis_terminal import test_storage as _storage

server = _storage.server
STEP = 900_000
NOW = 10*STEP


def signal(key="oldest", **values):
    item = dict(key=key, ticker="TESTUSDC", side="LONG", entry=100, stop=90, target=120,
                created_ms=2*STEP+1, signal_candle_ms=STEP)
    item.update(values)
    return item


def candle(index=2, high=105, low=95, cid="1"):
    return server.scanner.Candle(contract_id=cid, contract_name="TESTUSDC", interval="MINUTE_15",
                                time_ms=index*STEP, open=100, high=high, low=low, close=100,
                                volume=1, value=100, trades=None)


def state(s, now=NOW):
    return tracking_state(s, interval_ms=STEP, now_ms=now)


class TrackingTests(unittest.TestCase):
    def test_sentinel_does_not_skip_first_post_signal_bar(self):
        self.assertEqual(state(signal(), 3*STEP-1)["quality"], "WAITING_FIRST_CLOSED_CANDLE")
        first = state(signal(), 3*STEP)
        self.assertEqual((first["next_expected_ms"], first["bars_due"], first["overdue"]), (2*STEP, 1, False))
        self.assertTrue(state(signal(), 4*STEP)["overdue"])

    def test_mid_bar_and_exact_boundary_follow_evaluator_observation_window(self):
        for s, expected in ((signal(created_ms=2*STEP), 2*STEP),
                            (signal(created_ms=2*STEP+1000), 3*STEP),
                            (signal(created_ms=2*STEP+1000, signal_candle_ms=None), 3*STEP)):
            self.assertEqual(state(s)["next_expected_ms"], expected)
            r=server.evaluate_paper_signal(s, [], interval_ms=STEP, now_ms=NOW)
            self.assertEqual(state(s)["next_expected_ms"], r["observation_start_ms"])

    def test_incremental_cursor_ignores_legacy_and_oldest_age_is_not_processing_lag(self):
        r=dict(status="OPEN", evaluation_version=2, coverage_complete=True, history_end_ms=9*STEP)
        tracked=state(signal(result=r))
        self.assertEqual((tracked["quality"], tracked["bars_due"], tracked["overdue"]), ("TRACKING", 0, False))
        r["evaluation_version"]=1
        legacy=state(signal(result=r))
        self.assertEqual((legacy["quality"], legacy["next_expected_ms"]), ("LEGACY_PENDING", 2*STEP))

    def test_fresh_but_incomplete_history_is_not_reclassified_as_verified(self):
        r=dict(status="OPEN", evaluation_version=2, coverage_complete=False, history_end_ms=9*STEP)
        item=state(signal(result=r))
        self.assertEqual((item["quality"], item["bars_due"], item["overdue"]), ("INCOMPLETE_HISTORY", 0, False))
        summary=tracking_summary([signal(result=r)], interval_ms=STEP, now_ms=NOW)
        self.assertEqual((summary["incomplete_history"], summary["overdue"]), (1, 0))

    def test_terminal_results_need_no_further_bars_and_legacy_remains_unverified(self):
        for status in ("TP", "SL", "AMBIGUOUS"):
            r=dict(status=status, evaluation_version=2, coverage_complete=True)
            self.assertEqual(state(signal(result=r))["quality"], "VERIFIED_TERMINAL")
            self.assertFalse(state(signal(result=r))["overdue"])
            r.pop("evaluation_version")
            self.assertEqual(state(signal(result=r))["quality"], "UNVERIFIED_TERMINAL")

    def test_bad_cursor_is_a_data_error_not_a_healthy_record(self):
        for end in (STEP, 3*STEP+1, 10*STEP):
            self.assertEqual(state(signal(result=dict(status="OPEN", evaluation_version=2,
                coverage_complete=True, history_end_ms=end)))["quality"], "DATA_ERROR")
        self.assertEqual(state(signal(result=dict(status="ERROR", evaluation_version=2)))["quality"], "DATA_ERROR")


class TrackingStorageTests(unittest.IsolatedAsyncioTestCase):
    setUp = _storage.StorageTests.setUp

    def insert(self, model, rows):
        with server._db_connect() as conn:
            table=server.SIGNAL_TABLES[model]
            conn.executemany(f"INSERT INTO {table} VALUES (?,?,?,?)",
                [(s["key"], json.dumps(s), s["created_ms"], s["created_ms"]) for s in rows])

    def load(self, model):
        with server._db_connect() as conn:
            return {r["signal_key"]:json.loads(r["payload"]) for r in conn.execute(f"SELECT * FROM {server.SIGNAL_TABLES[model]}")}

    async def refresh(self, model, contracts=None):
        if contracts is None:
            contracts={"1":server.scanner.Contract("1", "TESTUSDC", "USDC", True, True)}
        with patch.object(server.time, "time", return_value=NOW/1000):
            fn=server._refresh_paper_signal_results if model=="current" else server._refresh_shadow_v2_results
            await fn(contracts)
        raw=server._state_get("outcome_refresh_"+model)
        return json.loads(raw) if raw else None

    async def test_oldest_open_survives_newer_terminal_history_beyond_both_caps(self):
        for model, count in (("current",1001),("shadow",2001)):
            with self.subTest(model=model):
                oldest=signal()
                terminal=[signal(f"new-{i}", created_ms=3*STEP+i,
                            result=dict(status="TP", final_r=2)) for i in range(count)]
                self.insert(model,[oldest]+terminal)
                self.assertEqual([s["key"] for s in server._load_pending_signals(model)], ["oldest"])
                with patch.object(server,"fetch_snapshots",AsyncMock(return_value={("1","MINUTE_15"):[candle(high=120)]})):
                    report=await self.refresh(model)
                result=self.load(model)
                self.assertEqual(report["selected"],1)
                self.assertEqual((report["evaluated"],report["advanced"],report["status"]),(1,1,"OK"))
                self.assertTrue(verified_result(result["oldest"]["result"]))
                self.assertEqual(result["oldest"]["result"]["status"],"TP")
                for s in terminal:
                    self.assertEqual(result[s["key"]],s)
                for field in ("created_ms","signal_candle_ms","entry","stop","target"):
                    self.assertEqual(oldest[field], result["oldest"][field])
        self.assertEqual(server._subscription_count(),1)

    async def test_pending_sql_selects_null_tp1_error_but_never_terminal(self):
        rows=[signal(str(i), result=None if status is None else dict(status=status))
              for i,status in enumerate((None,"OPEN","TP1","ERROR","TP","SL","AMBIGUOUS"))]
        self.insert("shadow",rows)
        self.assertEqual([s["key"] for s in server._load_pending_signals("shadow")],["0","1","2","3"])
        with self.assertRaises(KeyError):
            server._load_pending_signals("shadow_v2_signals; DROP TABLE paper_signals")

    async def test_missing_contract_and_snapshot_preserve_prior_results(self):
        prior=dict(status="OPEN", evaluation_version=2, coverage_complete=True, history_end_ms=2*STEP)
        rows=[signal(result=prior), signal("missing",ticker="MISSINGUSDC",result=prior)]
        self.insert("shadow",rows);before=self.load("shadow")
        with patch.object(server,"fetch_snapshots",AsyncMock(return_value={})):
            report=await self.refresh("shadow")
        self.assertEqual(self.load("shadow"),before)
        self.assertEqual((report["missing_contract"], report["missing_snapshot"],report["status"]),(1,1,"ERROR"))

    async def test_one_bad_record_does_not_stop_other_outcomes(self):
        self.insert("shadow",[signal("bad", entry="invalid"),signal("good")])
        with patch.object(server,"fetch_snapshots",AsyncMock(return_value={("1","MINUTE_15"):[candle(high=120)]})), patch('builtins.print'):
            report=await self.refresh("shadow")
        rows=self.load("shadow")
        self.assertNotIn("result",rows["bad"])
        self.assertEqual(rows["good"]["result"]["status"],"TP")
        self.assertEqual((report["signal_errors"],report["evaluated"],report["status"]),(1,1,"PARTIAL"))

    async def test_evaluator_error_result_is_not_reported_as_success(self):
        self.insert("current",[signal(stop=100)])
        with patch.object(server,"fetch_snapshots",AsyncMock(return_value={("1","MINUTE_15"):[candle()]})):
            report=await self.refresh("current")
        self.assertEqual(self.load("current")["oldest"]["result"]["status"],"ERROR")
        self.assertEqual((report["signal_errors"],report["status"]),(1,"PARTIAL"))

    async def test_chunk_failure_is_visible_and_other_chunk_and_retry_advance(self):
        contracts={f"{i:03}":server.scanner.Contract(f"{i:03}",f"TEST{i}USDC","USDC",True,True) for i in range(1,52)}
        self.insert("shadow",[signal(str(i),ticker=f"TEST{i}USDC") for i in range(1,52)])
        with patch.object(server,"fetch_snapshots",AsyncMock(side_effect=[RuntimeError("private-test-marker"),
                {("051","MINUTE_15"):[candle(cid="051")]}])), patch('builtins.print') as log:
            report=await self.refresh("shadow",contracts)
            self.assertNotIn("private-test-marker",str(log.call_args_list))
        self.assertEqual((report["selected"],report["chunk_errors"],report["missing_snapshot"],report["advanced"],report["status"]),(51,1,50,1,"PARTIAL"))
        with patch.object(server,"fetch_snapshots",AsyncMock(side_effect=lambda ids,**kw:{(cid,"MINUTE_15"):[candle(cid=cid)] for cid in ids})):
            report=await self.refresh("shadow",contracts)
        self.assertEqual((report["advanced"],report["evaluated"],report["status"]),(50,51,"OK"))
        self.assertTrue(all(verified_result(s["result"]) for s in self.load("shadow").values()))

    async def test_real_evaluator_retains_signal_exclusion_and_ambiguous_rule(self):
        self.insert("shadow",[signal()])
        with patch.object(server,"fetch_snapshots",AsyncMock(return_value={("1","MINUTE_15"):[candle(1,150,50),candle(2,125,75)]})):
            await self.refresh("shadow")
        r=self.load("shadow")["oldest"]["result"]
        self.assertEqual((r["status"],r["history_start_ms"],r["candles_checked"],r["final_r"]),("AMBIGUOUS",2*STEP,1,None))

    async def test_gap_cannot_become_verified_or_overwrite_terminal_history(self):
        self.insert("shadow",[signal()])
        with patch.object(server,"fetch_snapshots",AsyncMock(return_value={("1","MINUTE_15"):[candle(3,120,95)]})):
            await self.refresh("shadow")
        before=self.load("shadow")["oldest"]
        self.assertFalse(verified_result(before["result"]))
        self.assertEqual(before["result"]["status"],"TP")
        with patch.object(server,"fetch_snapshots",AsyncMock()) as fetch:
            report=await self.refresh("shadow")
            fetch.assert_not_awaited()
        self.assertEqual(self.load("shadow")["oldest"],before)
        self.assertEqual((report["selected"],report["status"]),(0,"OK"))

    async def test_diagnostics_failure_does_not_block_evaluation(self):
        self.insert("current",[signal()])
        with patch.object(server,"_state_set",side_effect=sqlite3.OperationalError("busy")), patch('builtins.print'), patch.object(server,"fetch_snapshots",AsyncMock(return_value={("1","MINUTE_15"):[candle(high=120)]})):
            await self.refresh("current")
        self.assertEqual(self.load("current")["oldest"]["result"]["status"],"TP")

    async def test_api_cohort_and_all_history_are_separate_and_read_only(self):
        row=dict(ticker="TESTUSDC", direction="LONG", shadow_v2_ready=True, breakout_time_ms=STEP,
                 breakout_level=99, entry_reference=100, shadow_stop_loss=90, shadow_v2_target=120,
                 latest_15m_time_ms=STEP)
        server._persist_shadow_v2_signals([row])
        self.insert("shadow",[signal("legacy",result=dict(status="TP",final_r=2))])
        with server._db_connect() as conn:before=list(conn.iterdump())
        with patch.object(server,"_scan_market_rows",AsyncMock(side_effect=AssertionError("DB-only API"))):
            async with AsyncClient(transport=ASGITransport(app=server.app),base_url="http://test") as client:
                response=await client.get('/api/outcome-tracking?limit=1')
                self.assertEqual(response.status_code,200)
                model=response.json()["models"]["shadow"]
                self.assertEqual((model["all_records"]["records"],model["setup_cohort"]["records"]),(2,1))
                self.assertEqual(model["all_records"]["quality_counts"]["UNVERIFIED_TERMINAL"],1)
                self.assertEqual(model["exclusions"]["legacy_unidentified"],1)
                self.assertEqual(len(model["setup_cohort"]["pending_items"]),1)
                self.assertIsNone(model["last_refresh"])
                self.assertEqual((await client.get('/api/outcome-tracking?limit=0')).status_code,422)
        with server._db_connect() as conn:self.assertEqual(before,list(conn.iterdump()))


class TrackingUITests(unittest.TestCase):
    def test_actual_script_renders_lag_gaps_and_failed_fetch_without_entry_changes(self):
        script=r'''
const fs=require('fs'),assert=require('assert');
const html=fs.readFileSync(process.argv[1],'utf8');
for(const m of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g))new Function(m[1]);
const sum={innerHTML:''},st={textContent:'',className:'',style:{}},document={getElementById:id=>id==='outcomeTrackingSummary'?sum:st},card=(k,v)=>k+':'+v;
eval(html.slice(html.indexOf('function renderOutcomeTracking('),html.indexOf('function lifecycleEscape(')));
const report={started_ms:1,finished_ms:2,status:'OK',selected:8,evaluated:8,advanced:8};
const j={time_ms:3,interval_ms:900000,models:{current:{setup_cohort:{},all_records:{},last_refresh:report,refresh_age_seconds:1},shadow:{setup_cohort:{pending:8,overdue:0,incomplete_history:0},all_records:{pending:13,incomplete_history:5,quality_counts:{UNVERIFIED_TERMINAL:2}},last_refresh:report,refresh_age_seconds:1}}};
renderOutcomeTracking(j);assert(!st.className.includes('warn'));assert(sum.innerHTML.includes('Shadow 未確定 / 遅延:8 / 0'));assert(st.textContent.includes('進行 8'));assert(st.textContent.includes('全記録の未確定 13'));
j.models.shadow.last_refresh={...report,backfill_recovered:2,gap_deferred:1,backfill_errors:1,unverified_pending:1,status:'PARTIAL'};renderOutcomeTracking(j);assert(st.textContent.includes('履歴補完 2件'));assert(st.textContent.includes('欠損保留 1件'));assert(st.textContent.includes('補完エラー 1'));assert(st.className.includes('warn'));j.models.shadow.last_refresh=report;
j.models.shadow.setup_cohort.overdue=1;renderOutcomeTracking(j);assert(st.className.includes('warn'));
j.models.shadow.setup_cohort.overdue=0;j.models.shadow.setup_cohort.incomplete_history=1;renderOutcomeTracking(j);assert(st.className.includes('warn'));
j.models.shadow.setup_cohort.incomplete_history=0;j.models.shadow.refresh_age_seconds=1800;renderOutcomeTracking(j);assert(st.className.includes('warn'));
j.models.shadow.refresh_age_seconds=1;j.models.shadow.last_refresh=null;renderOutcomeTracking(j);assert(st.className.includes('warn'));
const jf=async()=>{throw new Error('offline')};
(async()=>{await loadOutcomeTracking();assert(st.textContent.includes('追跡状況を確認できません'));assert(st.className.includes('warn'));console.log('Outcome tracking UI: OK')})().catch(e=>{console.error(e);process.exit(1)});
'''
        p=subprocess.run(["node","-e",script,str(Path(__file__).with_name('index.html'))],capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
