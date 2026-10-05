import asyncio
import copy
import json
import math
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient
from analysis_terminal import btc_wave as wave, btc_wave_store as store
from analysis_terminal.test_replay import c
from analysis_terminal import test_storage as _storage

server=_storage.server
NOW=2_000_000*wave.STEP
BTC=server.scanner.Contract("btc","BTCUSDC","USDC",True,True)


def candle(t,interval="MINUTE_5",**values):
    return replace(c(t,interval,**values),contract_id="btc",contract_name="BTCUSDC")


def dataset(now=NOW):
    data={}
    for interval in wave.INTERVALS:
        step=server.scanner.INTERVAL_MS[interval];end=now//step*step
        data[interval]=[candle(end-(100-i)*step,interval,open=100+math.sin(i*.7),
                        close=100+math.sin(i*.7)+.2,high=102+math.sin(i*.7),low=98+math.sin(i*.7)) for i in range(100)]
    return data


def plan(mode="BREAKOUT",side="LONG",known=True,now=NOW+30000):
    high=dict(kind="HIGH",price=100,time_ms=NOW-20*wave.STEP,confirmed_ms=NOW-14*wave.STEP)
    low=dict(kind="LOW",price=99,time_ms=NOW-10*wave.STEP,confirmed_ms=NOW-4*wave.STEP)
    if not known:low["confirmed_ms"]=NOW
    frames={"MINUTE_15":dict(resistance=high,support=low,atr=.4),
            "HOUR_1":dict(trend="MIXED" if mode=="RANGE" else "UP" if side=="LONG" else "DOWN",
                          pivots=[dict(kind="HIGH",price=103),dict(kind="LOW",price=96)]),
            "HOUR_4":dict(trend="DOWN" if side=="LONG" else "UP",pivots=[])}
    if mode=="BREAKOUT":
        bars=[candle(NOW-2*wave.STEP,open=99.9,close=99.9,high=100,low=99.8),
              candle(NOW-wave.STEP,open=99.9,close=100.1,high=100.2,low=99.8)] if side=="LONG" else [
              candle(NOW-2*wave.STEP,open=99.1,close=99.1,high=99.2,low=99),
              candle(NOW-wave.STEP,open=99.1,close=98.9,high=99.2,low=98.8)]
    else:
        bars=[candle(NOW-2*wave.STEP,open=99.05,close=99.02,high=99.1,low=98.95),
              candle(NOW-wave.STEP,open=99,close=99.02,high=99.1,low=98.95)] if side=="LONG" else [
              candle(NOW-2*wave.STEP,open=99.95,close=99.98,high=100.05,low=99.9),
              candle(NOW-wave.STEP,open=100,close=99.98,high=100.05,low=99.9)]
    return wave.watch_plan(mode,side,frames,bars,now)


def report_for(p,now=NOW+30000):
    return dict(model=wave.MODEL,ticker="BTCUSDC",observed_ms=now,status="OBSERVING",mode="SHADOW_ONLY",
                real_orders_enabled=False,automatic_promotion=False,changes_live_rules=False,eligible_for_live_promotion=False,
                frames={"MINUTE_5":dict(series=[],pivots=[])},plans=[p])


class BTCWaveTests(unittest.TestCase):
    def test_only_btc_and_four_closed_timeframes_are_used(self):
        r=wave.analyze(BTC,dataset(),now_ms=NOW+1000)
        self.assertEqual(r["status"],"OBSERVING")
        self.assertEqual(len(r["plans"]),4)
        self.assertEqual(set(r["frames"]),set(wave.INTERVALS))
        self.assertFalse(r["real_orders_enabled"])
        self.assertFalse(r["eligible_for_live_promotion"])
        with self.assertRaises(ValueError):wave.analyze(replace(BTC,contract_name="ETHUSDC"),dataset(),now_ms=NOW)

    def test_forming_and_future_extremes_cannot_change_waves_or_plans(self):
        data=dataset();before=wave.analyze(BTC,data,now_ms=NOW)
        for interval in wave.INTERVALS:
            step=server.scanner.INTERVAL_MS[interval]
            data[interval].append(candle(NOW//step*step,interval,high=100000,low=.01,close=5000))
        self.assertEqual(before,wave.analyze(BTC,data,now_ms=NOW))

    def test_gap_latest_missing_identity_conflicts_nan_and_empty_data_block_all_plans(self):
        base=dataset()
        bad=[]
        for change in (lambda xs:xs[:80]+xs[81:],lambda xs:xs[:-1],lambda xs:[replace(xs[0],contract_id="other")]+xs[1:],
                       lambda xs:[replace(xs[0],high=math.nan)]+xs[1:],lambda xs:xs+[replace(xs[0],close=99)],lambda xs:[]):
            data=copy.deepcopy(base);data["MINUTE_5"]=change(data["MINUTE_5"]);bad.append(data)
        for data in bad:
            r=wave.analyze(BTC,data,now_ms=NOW)
            self.assertEqual((r["status"],r["plans"],r["signal_count"]),("DATA_WAIT",[],0))

    def test_pivots_are_known_only_after_two_following_closed_bars_and_do_not_repaint(self):
        bars=[candle((i+1)*wave.STEP,high=h,low=90,close=100) for i,h in enumerate([101,102,110,103,104,105])]
        self.assertEqual(wave.pivots(bars[:4],"MINUTE_5"),[])
        first=wave.pivots(bars[:5],"MINUTE_5")
        self.assertEqual(first,[dict(kind="HIGH",time_ms=3*wave.STEP,price=110,confirmed_ms=6*wave.STEP)])
        self.assertEqual(first,wave.pivots(bars,"MINUTE_5"))

    def test_same_candle_high_and_low_pivot_does_not_infer_wave_order(self):
        bars=[candle((i+1)*wave.STEP,high=120 if i==2 else 105,low=80 if i==2 else 95) for i in range(5)]
        self.assertEqual(wave.pivots(bars,"MINUTE_5"),[])

    def test_both_directions_and_both_hypotheses_use_signal_close_sentinel(self):
        for mode in ("BREAKOUT","RANGE"):
            for side in ("LONG","SHORT"):
                p=plan(mode,side);s=p["signal"]
                self.assertEqual(p["state"],"TRIGGERED_SHADOW")
                self.assertGreaterEqual(p["rr"],2)
                self.assertEqual(s["created_ms"],s["signal_candle_ms"]+wave.STEP+1)
                self.assertFalse(s["real_orders_enabled"])

    def test_a_level_confirmed_on_signal_close_is_not_used_for_an_earlier_touch(self):
        p=plan(known=False)
        self.assertIsNone(p["signal"])
        self.assertIn("LEVEL_JUST_CONFIRMED",p["reasons"])

    def test_detection_lag_120_seconds_is_late_shadow_not_live_highlight(self):
        self.assertEqual(plan(now=NOW+119999)["state"],"TRIGGERED_SHADOW")
        self.assertEqual(plan(now=NOW+120000)["state"],"LATE_SHADOW")
        self.assertEqual(plan(now=NOW+120000)["signal"]["detection_lag_ms"],120000)

    def test_structure_target_is_not_replaced_with_an_invented_fixed_two_r_target(self):
        p=plan();self.assertEqual(p["target"],103)
        self.assertNotEqual(p["target"],p["entry"]+2*(p["entry"]-p["stop"]))


class BTCWaveStorageTests(unittest.IsolatedAsyncioTestCase):
    setUp=_storage.StorageTests.setUp

    def save_signal(self,**changes):
        p=plan();p["signal"].update(changes)
        with server._db_connect() as conn:store.save(conn,report_for(p))
        return p["signal"]

    def test_first_entry_dedup_compact_storage_migration_and_main_data_preservation(self):
        p=plan();source=report_for(p);before=copy.deepcopy(source)
        source["frames"]["MINUTE_5"]["series"]=[dict(close=1)]*120
        with server._db_connect() as conn:
            store.save(conn,source);later=copy.deepcopy(source);later["plans"][0]["signal"]["entry"]=100.5
            store.save(conn,later)
            self.assertEqual(store.records(conn)[0]["entry"],p["signal"]["entry"])
            saved=json.loads(conn.execute('SELECT payload FROM btc_wave_observations').fetchone()[0])
            self.assertNotIn('series',saved['frames']['MINUTE_5'])
            self.assertEqual(len(store.records(conn)),1)
        self.assertEqual(source['plans'],before['plans'])
        server._init_db();server._init_db()
        self.assertEqual(server._subscription_count(),1)
        self.assertEqual(server._load_paper_signals(),[])
        self.assertEqual(server._load_shadow_v2_signals(),[])
        self.assertEqual(server._load_push_events(),[])

    def test_candidate_and_earlier_candles_cannot_resolve_outcome(self):
        signal=self.save_signal(entry=100,stop=90,target=120)
        bars=[candle(NOW-2*wave.STEP,high=150,low=50),candle(NOW-wave.STEP,high=150,low=50),candle(NOW)]
        with server._db_connect() as conn:
            store.update(conn,signal,bars,BTC,now_ms=NOW+wave.STEP)
            result=store.records(conn)[0]['result']
        self.assertEqual(result['status'],'OPEN');self.assertEqual(result['candles_checked'],1)

    def test_same_candle_tp_sl_is_ambiguous_and_excluded_from_net_metrics(self):
        signal=self.save_signal(entry=100,stop=90,target=120)
        with server._db_connect() as conn:
            store.update(conn,signal,[candle(NOW,high=125,low=85)],BTC,now_ms=NOW+wave.STEP)
            r=store.report(conn,now_ms=NOW+wave.STEP)
        self.assertEqual(r['metrics']['ambiguous'],1)
        self.assertEqual(r['metrics']['resolved'],0)
        self.assertIsNone(r['metrics']['avg_net_r'])

    def test_missing_foot_never_skips_to_future_hit_and_recovery_resolves_once(self):
        signal=self.save_signal(entry=100,stop=90,target=120)
        later=candle(NOW+wave.STEP,high=125,low=95)
        with server._db_connect() as conn:
            store.update(conn,signal,[later],BTC,now_ms=NOW+2*wave.STEP)
            pending=store.records(conn)[0]
            self.assertEqual(pending['result']['status'],'OPEN')
            self.assertEqual(pending['result']['quality'],'HISTORY_GAP')
            self.assertEqual(store.metrics([pending])['history_gaps'],1)
            store.update(conn,pending,[candle(NOW),later],BTC,now_ms=NOW+2*wave.STEP)
            done=store.records(conn)[0]
        self.assertEqual(done['result']['status'],'TP')
        self.assertAlmostEqual(done['result']['net_r'],2-(100+120)*.0007/10)

    def test_missing_foot_after_terminal_hit_is_not_an_unneeded_history_gap(self):
        signal=self.save_signal(entry=100,stop=90,target=120)
        with server._db_connect() as conn:
            store.update(conn,signal,[candle(NOW,high=125),candle(NOW+2*wave.STEP)],BTC,now_ms=NOW+3*wave.STEP)
            r=store.records(conn)[0]['result']
        self.assertEqual((r['status'],r['quality'],r['next_missing_ms']),('TP','COMPLETE',None))

    def test_net_loss_can_occur_even_when_tp_touch_win_rate_is_one_hundred(self):
        signal=self.save_signal(entry=100,stop=99.99,target=100.02)
        with server._db_connect() as conn:
            store.update(conn,signal,[candle(NOW,open=100,close=100.01,low=100,high=100.03)],BTC,now_ms=NOW+wave.STEP)
            m=store.metrics(store.records(conn))
        self.assertEqual(m['win_rate'],100)
        self.assertLess(m['avg_net_r'],0)
        self.assertEqual(m['profit_factor'],0)
        self.assertEqual(m['sample_status'],'INSUFFICIENT SAMPLE')

    async def test_read_only_btc_api_has_separate_modes_and_does_not_fetch_or_modify_db(self):
        self.save_signal()
        with server._db_connect() as conn:before=list(conn.iterdump())
        with patch.object(server,'_btc_wave_latest',None),patch.object(server,'fetch_snapshots',AsyncMock(side_effect=AssertionError('GET must not fetch'))):
            async with AsyncClient(transport=ASGITransport(app=server.app),base_url='http://test') as client:
                j=(await client.get('/api/btc-wave')).json()
                self.assertEqual(j['mode'],'SHADOW_ONLY')
                self.assertFalse(j['real_orders_enabled']);self.assertFalse(j['changes_live_rules'])
                page=await client.get('/btc');self.assertEqual(page.status_code,200)
                self.assertIn(server.app.version,page.text);self.assertIn('no-store',page.headers['cache-control'])
        with server._db_connect() as conn:self.assertEqual(before,list(conn.iterdump()))

    async def test_collector_failure_isolated_from_main_signals_notifications_and_private_api(self):
        data=dataset(now=self.now_ms//wave.STEP*wave.STEP)
        raw={('btc',k):v for k,v in data.items()}
        with patch.object(server.CLIENT,'get_contracts',AsyncMock(return_value={'btc':BTC})), \
             patch.object(server,'fetch_snapshots',AsyncMock(return_value=raw)) as fetch, \
             patch.object(server,'_btc_wave_latest',None):
            await server._btc_wave_cycle()
            self.assertEqual(fetch.call_args.kwargs['intervals'],wave.INTERVALS)
            self.assertIsNotNone(server._btc_wave_latest)
            with server._db_connect() as conn:self.assertEqual(conn.execute('SELECT count(*) FROM btc_wave_observations').fetchone()[0],1)
        self.assertEqual(server._load_paper_signals(),[])
        self.assertEqual(server._load_shadow_v2_signals(),[])
        self.assertEqual(server._load_push_events(),[])
        self.assertEqual(server._subscription_count(),1)

    async def test_unavailable_recovery_keeps_gap_visible_without_assuming_a_later_tp(self):
        now=self.now_ms//wave.STEP*wave.STEP
        self.save_signal(entry=100,stop=90,target=120,created_ms=now-2*wave.STEP+1,signal_candle_ms=now-3*wave.STEP)
        raw={('btc','MINUTE_5'):[candle(now-wave.STEP,high=125,low=95)]}
        report=report_for(dict(plan(),signal=None),now=now)
        with patch.object(server.CLIENT,'get_contracts',AsyncMock(return_value={'btc':BTC})), \
             patch.object(server,'fetch_snapshots',AsyncMock(return_value=raw)), \
             patch.object(server.btc_wave,'analyze',return_value=report), \
             patch.object(server,'fetch_history',AsyncMock(side_effect=RuntimeError('unavailable'))) as recover, \
             patch.object(server,'_btc_wave_latest',None):
            await server._btc_wave_cycle()
        self.assertEqual(recover.await_count,1)
        with server._db_connect() as conn:
            signal=store.records(conn)[0]
            self.assertEqual((signal['result']['status'],signal['result']['quality']),('OPEN','HISTORY_GAP'))
            self.assertEqual(store.metrics([signal])['resolved'],0)

    async def test_conflicting_rest_history_marks_data_error_without_advancing_cursor(self):
        now=self.now_ms//wave.STEP*wave.STEP
        self.save_signal(entry=100,stop=90,target=120,created_ms=now-2*wave.STEP+1,signal_candle_ms=now-3*wave.STEP)
        cached=candle(now-wave.STEP,high=125,low=95)
        raw={('btc','MINUTE_5'):[cached]};report=report_for(dict(plan(),signal=None),now=now)
        with patch.object(server.CLIENT,'get_contracts',AsyncMock(return_value={'btc':BTC})), \
             patch.object(server,'fetch_snapshots',AsyncMock(return_value=raw)), \
             patch.object(server.btc_wave,'analyze',return_value=report), \
             patch.object(server,'fetch_history',AsyncMock(return_value=[candle(now-2*wave.STEP),replace(cached,high=126)])), \
             patch.object(server,'_btc_wave_latest',None):
            await server._btc_wave_cycle()
        with server._db_connect() as conn:
            signal=store.records(conn)[0]
            self.assertEqual(signal['result']['quality'],'DATA_ERROR')
            self.assertNotIn('history_end_ms',signal['result'])
            self.assertEqual(store.metrics([signal])['data_errors'],1)

    async def test_new_background_task_is_cancelled_with_original_collector_on_shutdown(self):
        async def wait():await asyncio.Event().wait()
        with patch.object(server,'_background_collector',wait),patch.object(server,'_background_btc_wave',wait):
            async with server.lifespan(server.app):
                main,btc=server._background_task,server._btc_wave_task
                self.assertIsNot(main,btc)
                await asyncio.sleep(0)
            self.assertTrue(main.cancelled());self.assertTrue(btc.cancelled())
            self.assertIsNone(server._background_task);self.assertIsNone(server._btc_wave_task)

    def test_stale_and_future_snapshot_times_are_not_fresh(self):
        with server._db_connect() as conn:
            for age,stale in ((-1,True),(119999,False),(120000,True)):
                r=store.report(conn,now_ms=NOW+age,latest=report_for(plan(),now=NOW))
                self.assertEqual(r['stale'],stale)


class BTCWaveUITests(unittest.TestCase):
    def test_actual_shipped_page_full_boot_stale_failure_race_and_shadow_separation(self):
        import subprocess
        subprocess.run(['node',str(Path(__file__).with_name('test_btc_ui.js')),str(Path(__file__).with_name('btc.html'))],check=True,capture_output=True,text=True)
