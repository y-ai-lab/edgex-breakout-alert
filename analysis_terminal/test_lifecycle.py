import asyncio
import subprocess
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from httpx import ASGITransport, AsyncClient
from analysis_terminal.lifecycle import current_observation, end_reason, new_setup, observe_setup
from analysis_terminal.setups import setup_identity
from analysis_terminal import test_storage as _storage

server = _storage.server

MONITOR = 14_400_000
ENTRY = 900_000
NOW = 100 * MONITOR + 2 * ENTRY + 30_000


def row(now=NOW, **values):
    latest4 = now // MONITOR * MONITOR - MONITOR
    item = dict(ticker="TESTUSDC", direction="LONG", trend="UP", stage="RETEST_WAIT",
                latest_4h_time_ms=latest4, latest_15m_time_ms=now//ENTRY*ENTRY-ENTRY,
                breakout_time_ms=latest4-MONITOR, breakout_level=99,
                breakout_window_start_ms=latest4-5*MONITOR, stop_valid=True,
                entry_reference=100, stop_loss=90, take_profit=120, rr=2,
                shadow_stop_loss=90, shadow_v2_target=120, shadow_v2_ready=False)
    item.update(values)
    item["setup_id"] = setup_identity(item)
    return item


class LifecycleTests(unittest.TestCase):
    def test_window_boundary_matches_detector_not_wall_clock_timeout(self):
        initial = row();setup = new_setup(initial, NOW)
        boundary = dict(initial, breakout_window_start_ms=setup["breakout_time_ms"])
        self.assertIsNone(end_reason(setup, boundary))
        boundary["breakout_window_start_ms"] += MONITOR
        self.assertEqual(end_reason(setup, boundary), ("EXPIRED", "BREAKOUT_WINDOW_EXPIRED"))

    def test_partial_stale_unfinished_or_bad_identity_is_not_evidence(self):
        self.assertTrue(current_observation(row(), NOW, MONITOR, ENTRY))
        for delta in (dict(stage="DATA_WAIT"), dict(latest_15m_time_ms=NOW//ENTRY*ENTRY),
                      dict(latest_15m_time_ms=0), dict(latest_4h_time_ms=0), dict(trend=None),
                      dict(setup_id="wrong"), dict(breakout_window_start_ms=None)):
            self.assertFalse(current_observation(dict(row(), **delta), NOW, MONITOR, ENTRY))

    def test_neutral_and_opposite_trend_invalidate_only_observed_setup(self):
        setup = new_setup(row(), NOW)
        for direction in (None, "SHORT"):
            updated, reason = observe_setup(setup, row(direction=direction), NOW+1)
            self.assertEqual(updated["status"], "INVALIDATED")
            self.assertEqual(reason, "TREND_INVALIDATED")
            self.assertEqual(updated["ended_ms"], NOW+1)

    def test_new_setup_supersedes_old_while_structural_target_wait_does_not(self):
        setup = new_setup(row(), NOW)
        updated, reason = observe_setup(setup, row(breakout_time_ms=setup["breakout_time_ms"]+MONITOR), NOW+1)
        self.assertEqual(reason, "SUPERSEDED")
        self.assertEqual(updated["status"], "INVALIDATED")
        updated, reason = observe_setup(setup, row(stage="STRUCTURE_WAIT", shadow_v2_ready=True), NOW+1)
        self.assertEqual(updated["status"], "OPEN")
        self.assertIsNone(updated["ended_ms"])

    def test_invalid_stop_has_specific_reason(self):
        updated, reason = observe_setup(new_setup(row(), NOW), row(stop_valid=False), NOW+1)
        self.assertEqual(reason, "STRUCTURAL_STOP_INVALID")
        self.assertEqual(updated["ended_ms"], NOW+1)

    def test_ready_history_is_sticky_even_when_confirmation_or_rr_is_lost(self):
        setup, _ = observe_setup(new_setup(row(), NOW), row(stage="READY"), NOW+1)
        ready_ms = setup["first_ready_ms"]
        setup, _ = observe_setup(setup, row(stage="RR_WAIT"), NOW+2)
        self.assertEqual(setup["status"], "READY")
        self.assertEqual(setup["first_ready_ms"], ready_ms)
        ended, _ = observe_setup(setup, row(direction=None), NOW+3)
        self.assertEqual(ended["first_ready_ms"], ready_ms)

    def test_end_time_is_first_observed_not_guessed_and_reactivation_is_explicit(self):
        setup, _ = observe_setup(new_setup(row(), NOW), row(direction=None), NOW+1)
        again, reason = observe_setup(setup, row(direction=None), NOW+2)
        self.assertIsNone(reason)
        self.assertEqual(again["ended_ms"], NOW+1)
        recovered, reason = observe_setup(again, row(), NOW+3)
        self.assertEqual(reason, "REACTIVATED")
        self.assertIsNone(recovered["ended_ms"])
        self.assertEqual(recovered["setup_id"], setup["setup_id"])
        self.assertEqual(recovered["first_seen_ms"], setup["first_seen_ms"])

    def test_out_of_order_observation_cannot_reopen_terminated_setup(self):
        ended, _ = observe_setup(new_setup(row(), NOW), row(direction=None), NOW+10)
        updated, reason = observe_setup(ended, row(), NOW)
        self.assertEqual(updated, ended)
        self.assertIsNone(reason)


class LifecycleStorageTests(unittest.IsolatedAsyncioTestCase):
    setUp = _storage.StorageTests.setUp

    def persist(self, items, now=NOW):
        server._persist_setup_lifecycles(items, observed_ms=now)

    def test_repeated_scan_and_migration_keep_history_without_duplicate_events(self):
        self.persist([row()]);first = server._setup_lifecycle_review()
        self.persist([row()]);server._init_db();server._init_db()
        self.assertEqual(server._setup_lifecycle_review()["tracked"], 1)
        self.assertEqual(server._setup_lifecycle_review()["events"], first["events"])
        self.assertEqual(server._subscription_count(), 1)

    def test_missing_or_delayed_symbol_does_not_end_active_setup(self):
        self.persist([row()]);self.persist([]);self.persist([row(latest_15m_time_ms=0)])
        item = server._setup_lifecycle_review()["items"][0]
        self.assertEqual(item["status"], "OPEN")
        self.assertIsNone(item["ended_ms"])

    def test_superseding_and_reopening_preserve_same_identity_and_event_history(self):
        initial=row();self.persist([initial])
        replacement=row(breakout_time_ms=initial["breakout_time_ms"]+MONITOR)
        self.persist([replacement], NOW+1)
        items={s["setup_id"]:s for s in server._setup_lifecycle_review()["items"]}
        self.assertEqual(items[initial["setup_id"]]["end_reason"], "SUPERSEDED")
        self.assertEqual(items[replacement["setup_id"]]["status"], "OPEN")
        self.persist([initial], NOW+2)
        review=server._setup_lifecycle_review()
        self.assertEqual(review["tracked"], 2)
        self.assertIn("REACTIVATED", [e["reason"] for e in review["events"]])

    def test_existing_identified_entries_import_without_fabricating_legacy_identity(self):
        signal=dict(row(), key="identified", side="LONG", created_ms=NOW-ENTRY, result=None)
        server._insert_shadow_v2_signal(signal)
        legacy=dict(key="legacy", ticker="TESTUSDC", side="LONG", created_ms=NOW-ENTRY, result=dict(status="TP", final_r=2))
        server._insert_shadow_v2_signal(legacy)
        before=server._load_shadow_v2_signals()
        self.persist([])
        review=server._setup_lifecycle_review()
        self.assertEqual(review["tracked"], 1)
        self.assertEqual(review["items"][0]["first_shadow_ready_ms"], NOW-ENTRY)
        self.assertIsNone(review["items"][0]["market_observed_ms"])
        self.assertEqual(server._load_shadow_v2_signals(), before)

    def test_expiration_cannot_change_existing_trade_result_or_sample_count(self):
        initial=row(stage="READY", shadow_v2_ready=True)
        server._persist_shadow_v2_signals([initial])
        signal=server._load_shadow_v2_signals()[0]
        signal["result"]=dict(status="TP", final_r=2, evaluation_version=2, coverage_complete=True)
        server._update_shadow_v2_signal(signal)
        before=server._load_shadow_v2_signals();metrics=server._shadow_v2_metrics(before)
        self.persist([initial])
        self.persist([row(breakout_window_start_ms=initial["breakout_time_ms"]+MONITOR)], NOW+1)
        self.assertEqual(server._load_shadow_v2_signals(), before)
        self.assertEqual(server._shadow_v2_metrics(before), metrics)
        item=server._setup_lifecycle_review()["items"][0]
        self.assertEqual(item["status"], "EXPIRED")
        self.assertEqual(item["shadow_trade"]["result"]["status"], "TP")

    async def test_tp_sl_evaluation_continues_after_setup_termination(self):
        initial=row(shadow_v2_ready=True, latest_15m_time_ms=NOW//ENTRY*ENTRY-2*ENTRY)
        server._persist_shadow_v2_signals([initial]);self.persist([])
        self.persist([row(direction=None)], NOW+1)
        self.assertEqual(server._setup_lifecycle_review()["items"][0]["status"], "INVALIDATED")
        source=initial["latest_15m_time_ms"]
        candle=server.scanner.Candle(contract_id="1",contract_name="TESTUSDC",interval="MINUTE_15",
                                    time_ms=source+ENTRY,open=100,high=120,low=95,close=110,volume=1,value=100,trades=None)
        contract=server.scanner.Contract("1", "TESTUSDC", "USDC", True, True)
        with patch.object(server, "fetch_snapshots", AsyncMock(return_value={("1","MINUTE_15"):[candle]})), patch.object(server.time,"time",return_value=NOW/1000):
            await server._refresh_shadow_v2_results({"1":contract})
        self.assertEqual(server._load_shadow_v2_signals()[0]["result"]["status"], "TP")
        self.assertEqual(server._setup_lifecycle_review()["items"][0]["status"], "INVALIDATED")

    async def test_setups_api_filter_and_validation_are_read_only(self):
        self.persist([row(),row(ticker="OTHERUSDC")])
        before=server._setup_lifecycle_review()
        async with AsyncClient(transport=ASGITransport(app=server.app),base_url="http://test") as client:
            response=await client.get('/api/setups?limit=10&ticker=testusdc')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()["tracked"],1)
            self.assertEqual(response.json()["items"][0]["ticker"],"TESTUSDC")
            self.assertEqual((await client.get('/api/setups?limit=501')).status_code,422)
        self.assertEqual(server._setup_lifecycle_review(),before)

    async def test_lifecycle_storage_failure_does_not_block_existing_collector(self):
        async def sleep(delay):
            if delay == 30:
                raise asyncio.CancelledError
        replacements = {
            "_scan_market_rows": AsyncMock(return_value=({}, [])),
            "_persist_scan_result": Mock(), "_persist_shadow_v2_signals": Mock(),
            "_persist_setup_lifecycles": Mock(side_effect=RuntimeError("test storage failure")),
            "_maybe_generate_daily_report": Mock(),
        }
        for name in ("_process_priority_changes", "_maybe_push_candidate_changes", "_evaluate_custom_alerts",
                     "_refresh_paper_signal_results", "_refresh_shadow_v2_results", "_refresh_candidate_event_results",
                     "_refresh_approach_event_results", "_maybe_push_daily_summary"):
            replacements[name] = AsyncMock()
        with patch.multiple(server, **replacements), patch.object(server.asyncio,"sleep",sleep), patch('builtins.print'):
            with self.assertRaises(asyncio.CancelledError):
                await server._background_collector()
        replacements["_refresh_paper_signal_results"].assert_awaited_once()
        replacements["_refresh_shadow_v2_results"].assert_awaited_once()
        replacements["_maybe_push_candidate_changes"].assert_awaited_once()


class LifecycleBrowserTests(unittest.TestCase):
    def test_ended_setup_and_open_trade_are_rendered_separately_and_escaped(self):
        code=r"""
const fs=require('fs'),assert=require('assert');
const script=fs.readFileSync(process.argv[1],'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];new Function(script);
const nodes=Object.fromEntries(['setupLifecycleSummary','setupLifecycleStatus','setupLifecycleBody','setupLifecycleEvents'].map(k=>[k,{innerHTML:'',textContent:'',rows:[],appendChild(x){this.rows.push(x)}}]));
global.document={getElementById:id=>nodes[id],createElement:()=>({innerHTML:''})};
global.card=(k,v)=>k+':'+v;global.directionJa=x=>x;global.resultJa=x=>x;
(0,eval)(script.slice(script.indexOf('function lifecycleEscape('),script.indexOf('function renderConfirmationDiagnostics(')));
renderSetupLifecycles({tracked:1,status_counts:{EXPIRED:1},items:[{ticker:'<img>',direction:'LONG',status:'EXPIRED',market_observed_ms:1,end_reason:'BREAKOUT_WINDOW_EXPIRED',ended_ms:2,shadow_trade:{result:{status:'OPEN'}}}],events:[{reason:'REACTIVATED',observed_ms:3,setup:{ticker:'<img>'}}]});
const html=nodes.setupLifecycleBody.rows[0].innerHTML;
assert(html.includes('期限切れ'));assert(html.includes('OPEN'));assert(html.includes('&lt;img&gt;'));assert(!html.includes('<img>'));
assert(nodes.setupLifecycleEvents.innerHTML.includes('同setupの条件が再成立'));
"""
        result=subprocess.run(["node","-e",code,str(Path(__file__).with_name('index.html'))],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
