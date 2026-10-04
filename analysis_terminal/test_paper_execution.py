import copy
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient
from analysis_terminal import paper_execution as paper
from analysis_terminal import test_storage as _storage
from analysis_terminal.setups import setup_identity

server = _storage.server
STEP = paper.STEP
BASE = 2000000*STEP
NOW = BASE+1000


def row(ticker="TESTUSDC", side="LONG", breakout=BASE-16*STEP, **values):
    r=dict(ticker=ticker,direction=side,stage="READY",contract_id="1",score=90,
           breakout_time_ms=breakout,breakout_level=99,
           latest_15m_time_ms=BASE-STEP,entry_reference=100,
           stop_loss=90 if side=="LONG" else 110,take_profit=130 if side=="LONG" else 70,
           step_size=0.01,min_order_size=0.01,max_order_size=None)
    r.update(values)
    r['setup_id']=setup_identity(r)
    return r


def candle(t=BASE+STEP, open=100,high=105,low=95,close=100,cid="1",interval="MINUTE_15"):
    return server.scanner.Candle(contract_id=cid,contract_name="TESTUSDC",interval=interval,
                                 time_ms=t,open=open,high=high,low=low,close=close,
                                 volume=1,value=100,trades=None)


class PaperExecutionTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        paper.initialize(self.conn,now_ms=NOW-100,min_rr=2,previous_signals=[])
        self.conn.commit()

    def tick(self,rows=(),candles=None,now=NOW,age=0):
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            paper.cycle(self.conn,rows=list(rows),candles_by_ticker=candles or {},now_ms=now,snapshot_age_ms=age)
        return paper.report(self.conn,now_ms=now)

    def latest(self):return json.loads(self.conn.execute('SELECT payload FROM simulated_orders ORDER BY rowid DESC LIMIT 1').fetchone()[0])

    def policy(self,**values):
        s=paper.account(self.conn);s['policy'].update(values);paper._save_account(self.conn,s);self.conn.commit()

    def open(self,side="LONG"):
        self.tick([row(side=side)])
        self.tick(candles={'TESTUSDC':[candle()]},now=BASE+STEP+1000)
        self.assertEqual(self.latest()['status'],'OPEN')

    def test_future_open_committed_once_and_no_signal_or_forming_bar_outcome(self):
        self.tick([row()]);q=self.latest()
        self.assertEqual(q['status'],'PENDING')
        self.assertGreater(q['execute_ms'],q['created_ms'])
        self.tick([row()],{'TESTUSDC':[candle(BASE,high=140,low=80)]},now=NOW+1000)
        self.assertEqual(self.latest()['status'],'PENDING')
        self.assertEqual(len(paper.orders(self.conn)),1)
        self.tick(candles={'TESTUSDC':[candle(high=140,low=80)]},now=BASE+STEP+1000)
        self.assertEqual(self.latest()['status'],'OPEN')
        self.assertEqual(self.latest()['filled_ms'],BASE+STEP)
        self.tick(candles={'TESTUSDC':[candle(high=140,low=80)]},now=BASE+2*STEP)
        self.assertEqual(self.latest()['status'],'AMBIGUOUS')

    def test_signal_candle_and_future_foot_never_influence_outcome(self):
        self.open()
        self.tick(candles={'TESTUSDC':[candle(BASE,high=140,low=80),candle(BASE+3*STEP,high=140,low=80),candle()]},now=BASE+2*STEP)
        self.assertEqual(self.latest()['status'],'OPEN')
        self.assertEqual(self.latest()['last_closed_candle_ms'],BASE+STEP)

    def test_duplicate_setup_restart_and_different_setup_same_ticker(self):
        self.tick([row()])
        paper.initialize(self.conn,now_ms=NOW+10,min_rr=2,previous_signals=[])
        self.conn.commit()
        self.tick([row(),row(breakout=BASE-8*STEP)])
        items=paper.orders(self.conn)
        self.assertEqual(len(items),2)
        self.assertEqual(items[1]['reason'],'TICKER_ALREADY_OPEN')
        self.assertEqual(sum(o['status']=='PENDING' for o in items),1)

    def test_only_current_ready_and_valid_identity_can_enqueue(self):
        self.tick([row(stage='RR_WAIT',shadow_v2_ready=True),dict(row(),setup_id='wrong')])
        self.assertEqual(paper.orders(self.conn),[])
        for bad in (dict(row(),stop_loss=101),dict(row(),take_profit=115)):
            self.tick([bad]);self.assertEqual(self.latest()['reason'],'INVALID_READY_LEVELS')

    def test_stale_source_snapshot_unknown_and_future_are_rejected(self):
        for i,(source,age) in enumerate(((BASE-STEP,120000),(BASE-STEP,None),(BASE-2*STEP,0),(BASE,0))):
            self.tick([row(ticker=str(i)+'USDC',latest_15m_time_ms=source)],age=age)
            self.assertEqual(self.latest()['reason'],'STALE_OR_UNKNOWN_DATA')
        self.tick([row(ticker='BOUNDARYUSDC')],now=BASE+120000)
        self.assertEqual(self.latest()['reason'],'STALE_OR_UNKNOWN_DATA')

    def test_contract_limits_and_quantity_round_down_cost_risk_budget(self):
        self.tick([row(step_size=3,min_order_size=3,max_order_size=8)])
        q=self.latest()
        self.assertEqual(q['quantity'],6)
        self.assertLessEqual(q['reserved_risk_usdc'],100)
        self.assertGreater(paper.stop_cost_per_unit(100,90,1,paper.POLICY),10)
        self.tick([row(ticker='SMALLUSDC',step_size=100,min_order_size=100)])
        self.assertEqual(self.latest()['reason'],'SIZE_TOO_SMALL')
        self.tick([row(ticker='INVALIDUSDC',step_size=None)])
        self.assertEqual(self.latest()['reason'],'INVALID_CONTRACT_SIZE_RULES')

    def test_position_total_risk_notional_caps_and_no_paper_leverage(self):
        result=self.tick([row(ticker=f'T{i}USDC',score=90-i) for i in range(4)])
        self.assertEqual(result['account']['active_positions'],3)
        self.assertLessEqual(result['account']['reserved_risk_usdc'],300)
        self.assertEqual(self.latest()['reason'],'MAX_POSITIONS')
        self.setUp_notional_only()

    def setUp_notional_only(self):
        with self.conn:self.conn.execute('DELETE FROM simulated_orders')
        self.tick([row(stop_loss=99.99,take_profit=100.1)])
        self.assertLessEqual(self.latest()['reserved_notional_usdc'],10000)
        self.assertLessEqual(self.latest()['reserved_risk_usdc'],100)

    def test_total_risk_cannot_be_exceeded_when_position_cap_is_not_binding(self):
        self.policy(max_positions=10,max_total_risk_pct=1)
        self.tick([row(ticker='FIRSTUSDC'),row(ticker='SECONDUSDC',score=89)])
        self.assertEqual(self.latest()['reason'],'RISK_OR_NOTIONAL_LIMIT')

    def test_future_gap_fill_revalidates_rr_and_size_without_changing_sl_tp(self):
        self.tick([row()]);before=self.latest()
        self.tick(candles={'TESTUSDC':[candle(open=112,high=115,low=110,close=112)]},now=BASE+STEP+1000)
        q=self.latest();self.assertEqual(q['reason'],'LEVELS_OR_RR_INVALID_AT_FILL')
        self.assertEqual((q['stop'],q['target']),(before['stop'],before['target']))

    def test_costs_slippage_and_exactly_once_settlement_long_and_short(self):
        for side in ('LONG','SHORT'):
            with self.subTest(side=side):
                with self.conn:self.conn.execute('DELETE FROM simulated_orders')
                s=paper.account(self.conn);s.update(paused=False,pause_reason=None,pause_ms=None);paper._save_account(self.conn,s);self.conn.commit()
                self.open(side)
                c=candle(high=132,low=95,close=130) if side=='LONG' else candle(high=105,low=68,close=70)
                r=self.tick(candles={'TESTUSDC':[c]},now=BASE+2*STEP)
                q=self.latest();self.assertEqual(q['status'],'TP')
                direction=1 if side=='LONG' else -1
                self.assertAlmostEqual(q['fill_price'],100*(1+direction*.0002))
                self.assertAlmostEqual(q['exit_price'],q['target']*(1-direction*.0002))
                self.assertLess(q['net_pnl_usdc'],q['gross_pnl_usdc'])
                self.assertAlmostEqual(r['account']['cash_usdc'],10000+q['net_pnl_usdc'])
                self.assertEqual(self.tick(candles={'TESTUSDC':[c]},now=BASE+2*STEP)['account']['cash_usdc'],r['account']['cash_usdc'])

    def test_same_foot_tp_sl_is_ambiguous_even_when_open_is_past_a_level(self):
        for side in ('LONG','SHORT'):
            with self.conn:self.conn.execute('DELETE FROM simulated_orders')
            s=paper.account(self.conn);s.update(paused=False,pause_ms=None);paper._save_account(self.conn,s);self.conn.commit()
            self.open(side)
            self.tick(candles={'TESTUSDC':[candle(),candle(BASE+2*STEP,open=85,high=140,low=60,close=100)]},now=BASE+3*STEP)
            q=self.latest();r=paper.report(self.conn,now_ms=BASE+3*STEP)
            self.assertEqual(q['status'],'AMBIGUOUS')
            self.assertNotIn('net_pnl_usdc',q)
            self.assertIsNone(r['account']['cash_usdc'])
            self.assertTrue(r['account']['paused'])
            self.assertEqual(r['metrics']['resolved'],0)
            with self.assertRaises(ValueError):paper.set_pause(self.conn,paused=False,now_ms=BASE+3*STEP)

    def test_stop_gap_uses_worse_open_and_loss_can_exceed_planned_risk(self):
        self.open()
        self.tick(candles={'TESTUSDC':[candle(),candle(BASE+2*STEP,open=80,high=85,low=75,close=80)]},now=BASE+3*STEP)
        q=self.latest();self.assertEqual(q['status'],'SL')
        self.assertLess(q['exit_price'],90)
        self.assertLess(q['net_r'],-1)

    def test_missing_closed_foot_holds_cursor_then_backfill_resolves(self):
        self.open()
        r=self.tick(candles={'TESTUSDC':[candle(BASE+2*STEP,high=132,low=95,close=130)]},now=BASE+3*STEP)
        q=self.latest();self.assertEqual((q['status'],q['quality'],q['next_candle_ms']),('OPEN','HISTORY_GAP',BASE+STEP))
        self.tick([row(ticker='NEWUSDC',latest_15m_time_ms=BASE+2*STEP)],now=BASE+3*STEP+1000)
        self.assertEqual(self.latest()['reason'],'ACTIVE_POSITION_DATA_INCOMPLETE')
        self.tick(candles={'TESTUSDC':[candle(),candle(BASE+2*STEP,high=132,low=95,close=130)]},now=BASE+3*STEP+1000)
        self.assertEqual(paper.orders(self.conn)[0]['status'],'TP')

    def test_conflicting_or_wrong_contract_candle_cannot_resolve(self):
        self.open()
        for candles in ([candle(cid='wrong')],[candle(),candle(high=132,low=95,close=130)]):
            self.tick(candles={'TESTUSDC':candles},now=BASE+2*STEP)
            self.assertEqual(self.latest()['quality'],'DATA_ERROR')
            self.assertEqual(self.latest()['status'],'OPEN')

    def test_expiry_cannot_fill_retroactively_after_outage(self):
        self.tick([row()])
        self.tick(candles={'TESTUSDC':[candle()]},now=NOW+2*STEP+1)
        self.assertEqual(self.latest()['status'],'EXPIRED')
        self.assertNotIn('filled_ms',self.latest())

    def test_emergency_stop_cancels_future_orders_but_keeps_open_stop_tracking(self):
        self.tick([row()])
        paper.set_pause(self.conn,paused=True,now_ms=NOW+1);self.conn.commit()
        self.assertEqual(self.latest()['status'],'CANCELLED')
        paper.set_pause(self.conn,paused=False,now_ms=NOW+2);self.conn.commit()
        self.open(side='SHORT')
        paper.set_pause(self.conn,paused=True,now_ms=BASE+STEP+2000);self.conn.commit()
        self.tick(candles={'TESTUSDC':[candle(high=112,low=95,close=110)]},now=BASE+2*STEP)
        self.assertEqual(self.latest()['status'],'SL')
        self.assertTrue(paper.account(self.conn)['paused'])

    def test_daily_loss_stop_persists_restart_until_operator_resumes_next_day(self):
        self.policy(daily_loss_pct=.5)
        self.open()
        r=self.tick(candles={'TESTUSDC':[candle(high=105,low=85,close=90)]},now=BASE+2*STEP)
        self.assertTrue(r['account']['paused']);self.assertEqual(r['account']['pause_reason'],'DAILY_LOSS_LIMIT')
        with self.assertRaises(ValueError):paper.set_pause(self.conn,paused=False,now_ms=BASE+2*STEP)
        paper.initialize(self.conn,now_ms=BASE+3*STEP,min_rr=2,previous_signals=[]);self.conn.commit()
        self.assertTrue(paper.account(self.conn)['paused'])
        paper.set_pause(self.conn,paused=False,now_ms=NOW+96*STEP);self.conn.commit()
        self.assertFalse(paper.account(self.conn)['paused'])

    def test_engine_failure_is_a_persistent_stop(self):
        paper.record_error(self.conn,now_ms=NOW,error_type='ValueError');self.conn.commit()
        r=self.tick([row()]);self.assertTrue(r['account']['paused'])
        self.assertEqual(self.latest()['reason'],'ACCOUNT_PAUSED')
        self.assertEqual(r['account']['last_error']['type'],'ValueError')

    def test_read_only_report_sample_guard_and_no_transport_imports(self):
        r=self.tick([row()]);before=list(self.conn.iterdump())
        for limit in (1,200):self.assertEqual(paper.report(self.conn,now_ms=NOW,limit=limit)['metrics'],r['metrics'])
        self.assertEqual(before,list(self.conn.iterdump()))
        self.assertFalse(r['real_orders_enabled']);self.assertFalse(r['eligible_for_live_promotion'])
        self.assertFalse(r['automatic_promotion']);self.assertEqual(r['metrics']['sample_status'],'INSUFFICIENT SAMPLE')
        import ast
        t=ast.parse(Path(paper.__file__).read_text())
        modules=[n.module for n in ast.walk(t) if isinstance(n,ast.ImportFrom)]
        self.assertEqual([m for m in modules if m and m.startswith('analysis_terminal.')],['analysis_terminal.setups'])


class PaperExecutionStorageTests(unittest.IsolatedAsyncioTestCase):
    setUp = _storage.StorageTests.setUp

    async def test_api_is_read_only_and_limits_never_change_metrics(self):
        with server._db_connect() as conn:before=list(conn.iterdump())
        with patch.object(server,'_scan_market_rows',AsyncMock(side_effect=AssertionError('unexpected scan'))):
            async with AsyncClient(transport=ASGITransport(app=server.app),base_url='http://test') as client:
                a=(await client.get('/api/paper-execution?limit=1')).json()
                b=(await client.get('/api/paper-execution?limit=200')).json()
                self.assertEqual(a['metrics'],b['metrics'])
                self.assertEqual(a['mode'],'PAPER_ONLY');self.assertFalse(a['real_orders_enabled'])
                self.assertEqual((await client.post('/api/paper-execution')).status_code,405)
        with server._db_connect() as conn:self.assertEqual(before,list(conn.iterdump()))
        self.assertEqual(server._subscription_count(),1)

    def test_migration_only_baselines_existing_current_preserves_strategy_and_push(self):
        source=row()
        current=dict(key='current:'+source['setup_id'],setup_id=source['setup_id'],ticker='TESTUSDC',side='LONG',
                     created_ms=BASE+1,breakout_time_ms=source['breakout_time_ms'],breakout_level=99)
        server._insert_paper_signal(current)
        with server._db_connect() as conn:
            conn.execute('DELETE FROM simulated_orders');conn.execute('DELETE FROM simulated_account')
        server._init_db();server._init_db()
        with server._db_connect() as conn:
            r=paper.report(conn,now_ms=NOW)
        self.assertEqual(r['metrics']['status_counts'],{'BASELINED':1})
        self.assertEqual(server._load_paper_signals(),[current])
        self.assertEqual(server._subscription_count(),1);self.assertEqual(server._load_push_events(),[])

    async def test_worker_uses_only_public_cache_history_and_no_private_transport(self):
        stamp=self.now_ms//STEP*STEP
        source=row(latest_15m_time_ms=stamp-STEP,breakout_time_ms=stamp-16*STEP)
        contract=server.scanner.Contract(contract_id='1',contract_name='TESTUSDC',quote_coin='USDC',enable_trade=True,enable_display=True)
        with patch.object(server,'_snapshot_cache',(self.now_ms/1000,{})),patch.object(server.time,'time',return_value=(stamp+1000)/1000):
            server._simulation_cycle({'1':contract},[source])
        with server._db_connect() as conn:order=paper.orders(conn)[0]
        self.assertEqual(order['status'],'PENDING')
        with patch.object(server,'_snapshot_cache',((stamp+STEP+1000)/1000,{('1','MINUTE_15'):[candle(stamp+STEP)]})),patch.object(server.time,'time',return_value=(stamp+STEP+1000)/1000):
            server._simulation_cycle({'1':contract},[])
        with patch.object(server,'_snapshot_cache',((stamp+3*STEP)/1000,{})),patch.object(server.time,'time',return_value=(stamp+3*STEP)/1000):
            server._simulation_cycle({'1':contract},[])
        history=AsyncMock(return_value=[candle(stamp+STEP),candle(stamp+2*STEP,high=132,low=95,close=130)])
        with patch.object(server,'fetch_history',history),patch.object(server,'_snapshot_cache',((stamp+3*STEP)/1000,{})),patch.object(server.time,'time',return_value=(stamp+3*STEP)/1000):
            await server._recover_simulation_history({'1':contract})
        history.assert_awaited_once()
        with server._db_connect() as conn:self.assertEqual(paper.orders(conn)[0]['status'],'TP')
        self.assertEqual(server._load_paper_signals(),[]);self.assertEqual(server._load_shadow_v2_signals(),[])
        self.assertEqual(server._load_push_events(),[])

    def test_worker_failure_does_not_crash_scan_and_rolls_back_partial_orders(self):
        with patch.object(paper,'cycle',side_effect=ValueError('controlled fixture')):
            server._simulation_cycle({},[])
        with server._db_connect() as conn:
            self.assertTrue(paper.account(conn)['paused']);self.assertEqual(paper.orders(conn),[])

    def test_cli_status_pause_resume_uses_existing_db_only(self):
        for action in ('status','pause','resume'):
            r=subprocess.run(['python','-m','analysis_terminal.paper_execution_control',action,'--db',str(server.DB_PATH)],capture_output=True,text=True)
            self.assertEqual(r.returncode,0,r.stderr)
            self.assertEqual(json.loads(r.stdout)['paused'],action=='pause')
        missing=Path(self.directory.name)/'missing.db'
        r=subprocess.run(['python','-m','analysis_terminal.paper_execution_control','pause','--db',str(missing)],capture_output=True,text=True)
        self.assertNotEqual(r.returncode,0);self.assertFalse(missing.exists())


class PaperExecutionUITests(unittest.TestCase):
    def test_actual_script_render_guards_ambiguous_cash_and_async_failures(self):
        conn=sqlite3.connect(':memory:')
        paper.initialize(conn,now_ms=NOW-100,min_rr=2,previous_signals=[])
        paper.cycle(conn,rows=[row()],candles_by_ticker={},now_ms=NOW,snapshot_age_ms=0)
        paper.cycle(conn,rows=[],candles_by_ticker={'TESTUSDC':[candle(high=140,low=80)]},now_ms=BASE+2*STEP,snapshot_age_ms=0)
        r=paper.report(conn,now_ms=BASE+2*STEP);conn.close()
        html=Path(__file__).with_name('index.html').read_text()
        code=r"""
const fs=require('fs'),assert=require('assert'),x=JSON.parse(fs.readFileSync(0,'utf8')),html=x.html;
for(const s of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g))new Function(s[1]);
const nodes={};['Status','Summary','Body','Policy'].forEach(k=>nodes['paperExecution'+k]={innerHTML:'',textContent:''});
const document={getElementById:id=>nodes[id]},fmt=(v,d)=>Number(v).toFixed(d),card=(k,v)=>k+':'+v,directionJa=x=>x;
eval(html.slice(html.indexOf('function lifecycleEscape('),html.indexOf('function lifecycleTime(')));
let paperExecutionRequest=0;
eval(html.slice(html.indexOf('function renderPaperExecution('),html.indexOf('var readinessHistoryRequest=')));
renderPaperExecution(x.report);
assert(nodes.paperExecutionStatus.textContent.includes('PAPER ONLY — 実注文なし'));
assert(nodes.paperExecutionStatus.textContent.includes('停止'));
assert(nodes.paperExecutionStatus.textContent.includes('INSUFFICIENT SAMPLE'));
assert(nodes.paperExecutionBody.innerHTML.includes('AMBIGUOUS'));
assert(nodes.paperExecutionSummary.innerHTML.includes('仮想現金 USDC:—'));
assert(nodes.paperExecutionPolicy.textContent.includes('0.05%'));
assert(nodes.paperExecutionPolicy.textContent.includes('0.02%'));
const evil={...x.report,latest:[{...x.report.latest[0],ticker:'<img onerror=evil()>',reason:'<script>evil</script>'}]};
renderPaperExecution(evil);assert(!nodes.paperExecutionBody.innerHTML.includes('<img'));
for(const v of [{real_orders_enabled:true},{eligible_for_live_promotion:true},{automatic_promotion:true},{mode:'LIVE'},{source:'SHADOW'}]){
 renderPaperExecution({...x.report,...v});assert.equal(nodes.paperExecutionBody.innerHTML,'');assert.equal(nodes.paperExecutionSummary.innerHTML,'');
}
let pending=[],jf=()=>new Promise((resolve,reject)=>pending.push({resolve,reject}));
(async()=>{
 const first=loadPaperExecution(),second=loadPaperExecution();
 pending[1].resolve(x.report);await second;pending[0].reject(new Error('old'));await first;
 assert(nodes.paperExecutionStatus.textContent.includes('PAPER ONLY'));
 const failed=loadPaperExecution();pending[2].reject(new Error('offline'));await failed;
 assert(nodes.paperExecutionStatus.textContent.includes('取得失敗'));assert.equal(nodes.paperExecutionBody.innerHTML,'');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
        p=subprocess.run(['node','-e',code],input=json.dumps(dict(html=html,report=r)),capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stderr)
