"""Financial interpretation and read-only cohort/API regressions."""
import copy
import json
import math
import unittest
from unittest.mock import AsyncMock,patch

from httpx import ASGITransport,AsyncClient
from analysis_terminal import net_costs as costs
from analysis_terminal.comparison import cost_cohort_metrics,strategy_comparison
from analysis_terminal.test_comparison import signal
from analysis_terminal import test_storage as storage

server=storage.server


class NetCostTests(unittest.TestCase):
    def test_long_and_short_charge_entry_and_exit_on_both_paths(self):
        for side,stop,target in (("LONG",90,120),("SHORT",110,80)):
            p=costs.projection(dict(side=side,entry=100,stop=stop,target=target))
            self.assertAlmostEqual(p["projected_tp_net_r"],2-(100+target)*.0007/10)
            self.assertAlmostEqual(p["projected_sl_net_r"],-1-(100+stop)*.0007/10)
            self.assertLess(p["projected_net_rr"],2)
            self.assertFalse(p["changes_live_rules"])

    def test_tp_hit_can_be_net_loss_and_must_not_count_as_net_win(self):
        s=signal(1,"TP",2,entry=100,stop=99.99,target=100.02)
        r=costs.metrics([s])
        self.assertEqual((r["resolved"],r["tp"],r["tp_hit_rate"]),(1,1,100))
        self.assertEqual((r["net_wins"],r["net_losses"],r["net_win_rate"]),(0,1,0))
        self.assertLess(r["avg_net_r"],0)
        self.assertEqual(r["projected_tp_nonpositive"],1)

    def test_short_tp_with_small_risk_is_also_unprofitable(self):
        s=signal(1,"TP",2,side="SHORT",entry=100,stop=100.01,target=99.98)
        r=costs.metrics([s])
        self.assertEqual(r["tp_hit_rate"],100)
        self.assertEqual(r["net_win_rate"],0)
        self.assertEqual(r["profit_factor"],0)

    def test_ambiguous_incomplete_and_legacy_results_never_enter_resolved_metrics(self):
        rows=[signal(1,"AMBIGUOUS"),signal(2,"TP",2),signal(3,"SL",-1),signal(4)]
        rows[1]["result"]["coverage_complete"]=False
        rows[2]["result"]["evaluation_version"]=1
        m=costs.metrics(rows)
        self.assertEqual(m["resolved"],0)
        self.assertEqual(m["excluded_resolved"]["unverified"],2)
        self.assertIsNone(m["avg_net_r"])
        self.assertIsNone(m["net_win_rate"])

    def test_invalid_prices_and_inconsistent_recorded_r_are_excluded(self):
        rows=[signal(1,"TP",3),signal(2,"TP",2,stop=None),signal(3,"SL",1)]
        m=costs.metrics(rows)
        self.assertEqual(m["resolved"],0)
        self.assertEqual(m["excluded_resolved"],dict(unverified=0,invalid_prices=1,inconsistent_r=2))

    def test_four_decimal_recorded_r_rounding_is_allowed(self):
        s=signal(1,"TP",2.3457,entry=100,stop=90,target=123.45676)
        self.assertEqual(costs.metrics([s])["resolved"],1)

    def test_original_price_risk_basis_and_no_roi_conversion(self):
        r=costs.metrics([signal(1,"SL",-1)])
        self.assertLess(r["avg_net_r"],-1)
        self.assertEqual(r["r_basis"],"ORIGINAL_ENTRY_TO_STOP_PRICE_DISTANCE")
        self.assertIsNone(r["portfolio_roi_pct"])
        self.assertFalse(r["eligible_for_live_promotion"])

    def test_plans_with_unknown_direction_nan_boolean_zero_and_wrong_levels_are_unpriced(self):
        for values in (dict(side="up"),dict(entry=math.nan),dict(entry=True),dict(stop=0),dict(target=90),dict(stop=110)):
            s=signal(**values)
            self.assertIsNone(costs.projection(s))
            self.assertEqual(costs.metrics([s])["unpriced_signals"],1)
        json.dumps(costs.metrics([signal(entry=math.inf)]),allow_nan=False)

    def test_first_entry_cohort_excludes_duplicates_and_unidentified_records(self):
        first=signal(1)
        later=signal(1,"TP",2,key="later",created_ms=first["created_ms"]+900000)
        legacy=dict(signal(2,"TP",2),setup_id=None)
        r=cost_cohort_metrics([later,legacy,first])
        self.assertEqual((r["signals"],r["resolved"]),(1,0))
        self.assertEqual((r["exclusions"]["legacy_unidentified"],r["exclusions"]["duplicates"]),(1,1))

    def test_twenty_tp_hits_do_not_imply_profit_or_automatic_promotion(self):
        r=cost_cohort_metrics([signal(i,"TP",2,entry=100,stop=99.99,target=100.02) for i in range(1,21)])
        self.assertEqual(r["sample_status"],"SUFFICIENT SAMPLE")
        self.assertEqual(r["tp_hit_rate"],100)
        self.assertEqual(r["net_win_rate"],0)
        self.assertEqual(r["profit_factor"],0)
        self.assertFalse(r["automatic_promotion"])
        self.assertFalse(r["real_execution_results"])

    def test_loss_streak_includes_losing_tp_and_uses_resolution_order(self):
        rows=[signal(1,"TP",2,entry=100,stop=99.99,target=100.02),signal(2,"TP",2),signal(3,"SL",-1)]
        rows[0]["result"]["outcome_time_ms"]=rows[2]["result"]["outcome_time_ms"]+900000
        self.assertEqual(costs.metrics(rows)["max_consecutive_net_losses"],2)
        self.assertEqual(costs.metrics(rows[::-1])["max_consecutive_net_losses"],2)

    def test_new_fields_preserve_existing_gross_results_and_input_records(self):
        rows=[signal(1,"TP",2),signal(2,"SL",-1)]
        before=copy.deepcopy(rows)
        r=strategy_comparison([],rows,limit=1)
        self.assertEqual(r["shadow"]["avg_r"],.5)
        self.assertLess(r["cost_adjusted"]["shadow"]["avg_net_r"],.5)
        self.assertEqual(rows,before)
        self.assertEqual(r["cost_adjusted"]["shadow"]["resolved"],2)
        self.assertIsNotNone(r["latest"][0]["shadow"]["cost_projection"])


class NetCostAPITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        storage.StorageTests.setUp(self)

    async def test_all_price_and_outcome_records_preserved_across_read_endpoints(self):
        server._insert_paper_signal(signal(1,"SL",-1))
        server._insert_shadow_v2_signal(signal(1,"TP",2))
        btc=signal(2,"SL",-1,ticker="BTCUSDC",mode="RANGE")
        with server._db_connect() as conn:
            conn.execute("INSERT INTO btc_wave_signals VALUES(?,?,?)",(btc["key"],json.dumps(btc),btc["created_ms"]))
        with server._db_connect() as conn:
            before=list(conn.iterdump())
        with patch.object(server,"_scan_market_rows",AsyncMock(side_effect=RuntimeError("No external market access"))):
            async with AsyncClient(transport=ASGITransport(app=server.app),base_url="http://test") as client:
                a=(await client.get("/api/strategy-comparison?limit=1")).json()
                b=(await client.get("/api/shadow-v2?limit=1")).json()
                c=(await client.get("/api/btc-wave")).json()
                self.assertEqual(a["cost_adjusted"]["shadow"]["resolved"],1)
                self.assertEqual(b["cost_adjusted"]["resolved"],1)
                self.assertEqual(c["cost_adjusted"]["resolved"],1)
                self.assertFalse(c["real_orders_enabled"])
        with server._db_connect() as conn:
            self.assertEqual(before,list(conn.iterdump()))

    async def test_display_limit_never_reduces_net_metric_sample(self):
        rows=[signal(i,"TP",2) for i in range(1,21)]
        for r in rows:server._insert_shadow_v2_signal(r)
        async with AsyncClient(transport=ASGITransport(app=server.app),base_url="http://test") as client:
            small=(await client.get("/api/strategy-comparison?limit=1")).json()
            large=(await client.get("/api/strategy-comparison?limit=50")).json()
        self.assertEqual(small["cost_adjusted"],large["cost_adjusted"])
        self.assertEqual(small["cost_adjusted"]["shadow"]["resolved"],20)
