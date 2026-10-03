from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from analysis_terminal.history import fetch_history
from analysis_terminal.replay import replay_contract, replay_report, strategy_parameters
from analysis_terminal import test_storage as _storage

server = _storage.server
STEP = 900_000
MONITOR = 16*STEP
START = 100*MONITOR
CONTRACT = server.scanner.Contract("1", "TESTUSDC", "USDC", True, True)


def c(time_ms, interval="MINUTE_15", high=105, low=95, **values):
    item = dict(contract_id="1", contract_name="TESTUSDC", interval=interval, time_ms=time_ms,
                open=100, high=high, low=low, close=100, volume=1, value=100, trades=None)
    item.update(values)
    return server.scanner.Candle(**item)


def dataset(end=START+4*STEP):
    monitor = [c(t, "HOUR_4") for t in range(START-12*MONITOR, end+3*MONITOR, MONITOR)]
    entries = [c(t) for t in range(START-8*MONITOR, end+3*STEP, STEP)]
    return monitor, entries


def analyzer(ready_from=START, **overrides):
    def analyze(contract, monitor, entries, *, as_of_ms):
        assert all(c.time_ms+MONITOR <= as_of_ms for c in monitor)
        assert all(c.time_ms+STEP <= as_of_ms for c in entries)
        ready = as_of_ms >= ready_from
        row = dict(ticker=contract.contract_name, direction="LONG", breakout_time_ms=START-2*MONITOR,
                   breakout_level=99, stage="READY" if ready else "RETEST_WAIT", shadow_v2_ready=ready,
                   entry_reference=100, stop_loss=90, take_profit=125, tp1_2r=120,
                   shadow_stop_loss=90, shadow_v2_target=120, shadow_v2_extension_target=140, shadow_v2_room_rr=4)
        row.update(overrides)
        return row
    return analyze


def replay(monitor, entries, analyze=None, end=START+4*STEP):
    return replay_contract(CONTRACT, monitor, entries, analyze or analyzer(), start_ms=START, end_ms=end,
                           monitor_window=2, entry_window=4)


class ReplayTests(unittest.TestCase):
    def test_first_post_signal_bar_and_one_entry_per_setup(self):
        m,e=dataset();e=[replace(x,high=125) if x.time_ms==START else x for x in e]
        result=replay(m,e)
        self.assertEqual((len(result['current']),len(result['shadow'])),(1,1))
        for model in ('current','shadow'):
            s=result[model][0]
            self.assertEqual(s['created_ms'],s['signal_candle_ms']+STEP+1)
            self.assertEqual((s['result']['status'],s['result']['outcome_time_ms']),('TP',START))
            self.assertTrue(s['result']['coverage_complete'])
        self.assertEqual(result['current'][0]['result']['final_r'],2.5)
        self.assertEqual(result['shadow'][0]['result']['final_r'],2)

    def test_future_candles_cannot_change_decisions_or_outcomes(self):
        m,e=dataset();base=replay(m,e)
        future4=[replace(x,high=1000,close=500) if x.time_ms+MONITOR>START+4*STEP else x for x in m]
        future15=[replace(x,high=1000,low=1) if x.time_ms+STEP>START+4*STEP else x for x in e]
        self.assertEqual(base,replay(future4,future15))

    def test_warmup_first_entry_does_not_reenter_reporting_period(self):
        m,e=dataset();r=replay(m,e,analyzer(ready_from=START-STEP))
        self.assertEqual((r['current'],r['shadow']),([],[]))
        self.assertEqual(r['warmup_first_entries'],dict(current=1,shadow=1))

    def test_same_ticker_new_breakout_is_a_different_setup(self):
        end=START+20*STEP;m,e=dataset(end)
        original=analyzer()
        def analyze(contract,monitor,entries,*,as_of_ms):
            row=original(contract,monitor,entries,as_of_ms=as_of_ms)
            if as_of_ms>=START+MONITOR:
                row['breakout_time_ms']=START
            return row
        r=replay(m,e,analyze,end=end)
        self.assertEqual((len(r['current']),len(r['shadow'])),(2,2))
        self.assertNotEqual(r['current'][0]['setup_id'],r['current'][1]['setup_id'])

    def test_missing_indicator_bar_is_excluded_and_outcome_gap_is_unverified(self):
        m,e=dataset();e=[x for x in e if x.time_ms!=START+STEP]
        e=[replace(x,high=125) if x.time_ms==START+2*STEP else x for x in e]
        r=replay(m,e)
        self.assertEqual(r['excluded_points']['incomplete_indicator_window'],2)
        self.assertEqual(r['shadow'][0]['result']['status'],'TP')
        self.assertFalse(r['shadow'][0]['result']['coverage_complete'])
        summary=replay_report([r],start_ms=START,end_ms=START+4*STEP,manifest={})
        self.assertEqual(summary['comparison']['shadow']['resolved'],0)
        self.assertFalse(summary['eligible_for_live_promotion'])

    def test_missing_warmup_does_not_invent_a_first_entry(self):
        m,e=dataset();e=[x for x in e if x.time_ms!=START-3*STEP]
        r=replay(m,e)
        self.assertEqual((r['current'],r['shadow']),([],[]))
        self.assertEqual(r['excluded_points']['unknown_first_entry_current'],1)

    def test_signal_wick_cannot_resolve_but_same_future_bar_tp_sl_is_ambiguous(self):
        m,e=dataset();e=[replace(x,high=150,low=50) if x.time_ms==START-STEP else x for x in e]
        self.assertEqual(replay(m,e)['shadow'][0]['result']['status'],'OPEN')
        e=[replace(x,high=125,low=85) if x.time_ms==START else x for x in e]
        self.assertEqual(replay(m,e)['shadow'][0]['result']['status'],'AMBIGUOUS')

    def test_current_and_shadow_can_first_enter_at_different_times(self):
        m,e=dataset();original=analyzer()
        def analyze(contract,monitor,entries,*,as_of_ms):
            row=original(contract,monitor,entries,as_of_ms=as_of_ms)
            if START<=as_of_ms<START+STEP:row['stage']='RR_WAIT'
            return row
        r=replay(m,e,analyze)
        self.assertEqual(r['current'][0]['created_ms']-r['shadow'][0]['created_ms'],STEP)
        summary=replay_report([r],start_ms=START,end_ms=START+4*STEP,manifest={})
        self.assertEqual(summary['comparison']['groups']['both']['setups'],1)

    def test_invalid_grid_order_or_identity_is_rejected(self):
        m,e=dataset()
        for bad in (e[::-1], e+[e[-1]], [replace(x,contract_id='2') for x in e]):
            with self.assertRaises(ValueError):replay(m,bad)
        with self.assertRaises(ValueError):
            replay_contract(CONTRACT,m,e,analyzer(),start_ms=START+1,end_ms=START+4*STEP)

    def test_default_analyzer_is_identical_and_explicit_clock_excludes_future(self):
        monitor=[c(START-(181-i)*MONITOR,'HOUR_4') for i in range(180)]
        entries=[c(START-(180-i)*STEP) for i in range(180)]
        with patch.object(server.time,'time',return_value=(START+1)/1000):
            default=server.analyze_contract(CONTRACT,monitor,entries)
        explicit=server.analyze_contract(CONTRACT,monitor+[c(START,'HOUR_4',high=1000)],entries+[c(START,high=1000)],as_of_ms=START+1)
        self.assertEqual(default,explicit)
        self.assertEqual(explicit['latest_15m_time_ms'],START-STEP)

    def test_parameter_export_is_an_explicit_whitelist(self):
        params=strategy_parameters(server.SETTINGS)
        self.assertEqual(params['min_rr'],2)
        self.assertFalse(any('key' in k or 'secret' in k or 'token' in k for k in params))


def payload(candle, **overrides):
    row=dict(contractId=candle.contract_id,contractName=candle.contract_name,klineType=candle.interval,
             priceType='LAST_PRICE',klineTime=str(candle.time_ms),open=str(candle.open),high=str(candle.high),
             low=str(candle.low),close=str(candle.close),size=str(candle.volume),value=str(candle.value))
    row.update(overrides);return row


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # The transport is already a synchronous fixture; no executor is needed.
        # Real threaded public requests are checked in the research Actions job.
        async def fixture_transport(call, *args, **kwargs):
            return call(*args, **kwargs)
        context=patch('analysis_terminal.history.asyncio.to_thread',fixture_transport)
        context.start();self.addCleanup(context.stop)

    async def test_documented_pagination_bounds_and_sorting(self):
        get=Mock(side_effect=[dict(code='SUCCESS',data=dict(dataList=[payload(c(START+STEP)),payload(c(START))],nextPageOffsetData='next')),
                             dict(code='SUCCESS',data=dict(dataList=[payload(c(START)),payload(c(START-STEP))],nextPageOffsetData=''))])
        rows=await fetch_history(get,CONTRACT,'MINUTE_15',START-STEP,START+2*STEP,size=2)
        self.assertEqual([x.time_ms for x in rows],[START-STEP,START,START+STEP])
        self.assertEqual(get.call_args_list[0].args[0],'/api/v2/public/quote/getKline')
        params=get.call_args_list[1].args[1]
        self.assertEqual((params['offsetData'],params['filterBeginKlineTimeInclusive'],params['priceType']),('next',str(START-STEP),'LAST_PRICE'))

    async def test_bad_identity_price_type_grid_ohlc_and_nonfinite_are_rejected(self):
        for override in (dict(contractId='2'),dict(priceType='MARK_PRICE'),dict(klineTime=str(START+1)),
                         dict(high='99'),dict(open='nan'),dict(klineTime=str(START+2*STEP))):
            get=Mock(return_value=dict(code='SUCCESS',data=dict(dataList=[payload(c(START),**override)])))
            with self.subTest(override=override),self.assertRaises(ValueError):
                await fetch_history(get,CONTRACT,'MINUTE_15',START-STEP,START+2*STEP)

    async def test_cyclic_token_empty_page_and_conflicting_revisions_fail(self):
        row=payload(c(START))
        for pages in ([dict(code='SUCCESS',data=dict(dataList=[row],nextPageOffsetData='same'))]*2,
                      [dict(code='SUCCESS',data=dict(dataList=[],nextPageOffsetData='same'))],
                      [dict(code='SUCCESS',data=dict(dataList=[row],nextPageOffsetData='next')),
                       dict(code='SUCCESS',data=dict(dataList=[dict(row,close='101')]))]):
            with self.assertRaises(ValueError):
                await fetch_history(Mock(side_effect=pages),CONTRACT,'MINUTE_15',START-STEP,START+STEP)

    async def test_empty_history_and_api_error_are_distinct(self):
        get=Mock(return_value=dict(code='SUCCESS',data=dict(dataList=[],nextPageOffsetData='')))
        self.assertEqual(await fetch_history(get,CONTRACT,'MINUTE_15',START-STEP,START+STEP),[])
        with self.assertRaises(ValueError):
            await fetch_history(Mock(return_value=dict(code='ERROR')),CONTRACT,'MINUTE_15',START-STEP,START+STEP)


class ReplayRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_artifact_isolation_and_failed_market_denominator(self):
        from analysis_terminal import run_replay
        other=replace(CONTRACT,contract_id='2',contract_name='OTHERUSDC')
        end=1000*MONITOR;start=end-7*86_400_000
        async def history(get,contract,interval,begin,finish):
            if contract.contract_id=='2':raise RuntimeError('fixture public API failure')
            step=MONITOR if interval=='HOUR_4' else STEP
            return [c(t,interval) for t in range(((begin+step-1)//step)*step,finish,step)]
        with tempfile.TemporaryDirectory() as directory,patch.object(server.CLIENT,'get_contracts',AsyncMock(return_value={'1':CONTRACT,'2':other})),patch.object(run_replay,'fetch_history',history),patch.object(server,'_db_connect',side_effect=AssertionError('No live DB access')),patch.object(server,'_broadcast_push',AsyncMock(side_effect=AssertionError('No push delivery'))),patch('builtins.print'):
            report=await run_replay.run(end,7,Path(directory))
            self.assertEqual((report['coverage']['requested_markets'],report['coverage']['failed_markets']),(2,1))
            self.assertEqual(report['coverage']['expected_points_all_markets'],2*7*96)
            self.assertFalse(report['eligible_for_live_promotion'])
            summary=json.loads((Path(directory)/'replay-summary.json').read_text())
            self.assertNotIn('signals',summary)
            self.assertEqual(len(summary['manifest']['sources']),1)
            self.assertEqual(summary['comparison']['current']['signals'],0)
