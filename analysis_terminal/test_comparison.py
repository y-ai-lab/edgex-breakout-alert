import json
import math
import subprocess
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient
from analysis_terminal.comparison import metrics, strategy_comparison
from analysis_terminal.setups import setup_identity
from analysis_terminal import test_storage as _storage

server = _storage.server


def signal(index=1, status="OPEN", r=None, **values):
    item = dict(key=f"record-{index}", ticker="TESTUSDC", side="LONG", breakout_time_ms=index*14_400_000,
                breakout_level=100, created_ms=index*14_400_000+900_001, entry=101, stop=91, target=121,
                result=dict(status=status, final_r=r, evaluation_version=2, coverage_complete=True,
                            outcome_time_ms=index*14_400_000+1_800_000, mfe_r=2, mae_r=.5))
    item.update(values)
    item["setup_id"] = setup_identity(dict(item, direction=item["side"]))
    return item


class ComparisonTests(unittest.TestCase):
    def test_groups_match_identity_not_ticker_or_entry_time(self):
        c = [signal(1), signal(2)]
        v = [dict(signal(2), created_ms=999_000_001, entry=110), signal(3)]
        result = strategy_comparison(c, v)
        self.assertEqual([result["groups"][g]["setups"] for g in ("current_only", "shadow_only", "both")], [1, 1, 1])
        self.assertEqual(result["current"]["signals"], 2)
        self.assertEqual(result["shadow"]["signals"], 2)
        self.assertFalse(result["automatic_promotion"])

    def test_legacy_and_invalid_identity_are_not_inferred(self):
        rows = [dict(signal(1), setup_id=None), dict(signal(2), setup_id="wrong"), signal(3)]
        result = strategy_comparison([], rows)
        self.assertEqual(result["shadow"]["signals"], 1)
        self.assertEqual(result["exclusions"]["shadow"], dict(records=3, legacy_unidentified=1, invalid_identity=1, duplicates=0))

    def test_first_open_entry_wins_over_profitable_duplicate(self):
        first = signal(1)
        later = dict(signal(1, "TP", 2), key="later", created_ms=first["created_ms"]+900_000)
        result = strategy_comparison([], [later, first])
        self.assertEqual(result["shadow"]["open"], 1)
        self.assertEqual(result["shadow"]["resolved"], 0)
        self.assertEqual(result["exclusions"]["shadow"]["duplicates"], 1)

    def test_tied_duplicate_is_deterministic_across_input_order(self):
        a, z = dict(signal(1), key="a"), dict(signal(1, "TP", 2), key="z")
        self.assertEqual(strategy_comparison([], [a, z]), strategy_comparison([], [z, a]))

    def test_metrics_profit_factor_expectancy_excursions(self):
        m = metrics([signal(1, "TP", 2), signal(2, "SL", -1), signal(3, "TP", 3)])
        self.assertEqual((m["signals"], m["resolved"], m["tp"], m["sl"]), (3, 3, 2, 1))
        self.assertEqual((m["profit_factor"], m["win_rate"], m["avg_r"], m["expectancy_r"]), (5, 66.67, 1.3333, 1.3333))
        self.assertEqual((m["avg_mfe_r"], m["avg_mae_r"]), (2, .5))

    def test_unverified_ambiguous_and_nonfinite_or_wrong_sign_r_excluded(self):
        rows = [signal(1, "AMBIGUOUS"), signal(2, "TP", None), signal(3, "TP", math.inf),
                signal(4, "SL", 2), signal(5, "TP", -1), signal(6, "SL", -1)]
        rows[-1]["result"]["coverage_complete"] = False
        old = signal(7, "TP", 2);old["result"]["evaluation_version"] = 1;rows.append(old)
        m = metrics(rows)
        self.assertEqual((m["resolved"], m["ambiguous"], m["unverified_results"], m["invalid_resolved_results"]), (0, 1, 2, 4))
        self.assertIsNone(m["profit_factor"])
        self.assertIsNone(m["avg_r"])
        self.assertIsNone(m["avg_mfe_r"])

    def test_sample_gate_counts_only_verified_unique_resolved(self):
        rows = [signal(i, "TP", 2) for i in range(1, 21)]
        self.assertEqual(strategy_comparison([], rows[:19])["shadow"]["sample_status"], "INSUFFICIENT SAMPLE")
        full = strategy_comparison([], rows)
        self.assertEqual(full["shadow"]["sample_status"], "SUFFICIENT SAMPLE")
        self.assertEqual(full["shadow"]["profit_factor"], "INF")
        self.assertFalse(full["automatic_promotion"])
        self.assertEqual(strategy_comparison([], rows[:19]+[rows[0]])["shadow"]["resolved"], 19)

    def test_losses_ordered_by_resolution_not_entry(self):
        rows = [signal(1, "SL", -1), signal(2, "TP", 2), signal(3, "SL", -1)]
        rows[0]["result"]["outcome_time_ms"] = rows[2]["result"]["outcome_time_ms"]+900_000
        self.assertEqual(metrics(rows)["max_consecutive_losses"], 2)
        self.assertEqual(metrics(rows[::-1])["max_consecutive_losses"], 2)
        rows[0]["result"]["outcome_time_ms"] = None
        self.assertEqual(metrics(rows)["streak_samples"], 2)

    def test_excursions_have_own_missing_value_denominators(self):
        rows = [signal(1, "TP", 2), signal(2, "SL", -1), signal(3)]
        rows[1]["result"].update(mfe_r=None, mae_r=math.nan)
        m = metrics(rows)
        self.assertEqual((m["resolved"], m["mfe_r_samples"], m["mae_r_samples"]), (2, 1, 1))
        self.assertEqual(m["avg_mfe_r"], 2)

    def test_paired_delta_uses_same_both_resolved_setups(self):
        result = strategy_comparison([signal(1, "SL", -1), signal(2, "TP", 3)],
                                     [signal(1, "TP", 2), signal(2), signal(3, "TP", 2)])
        self.assertEqual(result["groups"]["both"]["setups"], 2)
        self.assertEqual(result["paired"]["resolved"], 1)
        self.assertEqual(result["paired"]["avg_delta_r"], 3)
        self.assertEqual(result["paired"]["current"]["signals"], 1)
        self.assertEqual(result["paired"]["sample_status"], "INSUFFICIENT SAMPLE")


class ComparisonAPITests(unittest.IsolatedAsyncioTestCase):
    setUp = _storage.StorageTests.setUp

    async def test_full_history_despite_display_limit_and_old_loader_caps(self):
        rows = [signal(i) for i in range(1, 2102)]
        # Duplicate of oldest entry beyond the existing latest-2000 loader cap.
        rows.append(dict(signal(1, "TP", 2), key="late-duplicate", created_ms=rows[-1]["created_ms"]+900_000))
        with server._db_connect() as conn:
            conn.executemany("INSERT INTO shadow_v2_signals VALUES (?,?,?,?)",
                             [(s["key"], json.dumps(s), s["created_ms"], s["created_ms"]) for s in rows])
        with patch.object(server, "_scan_market_rows", AsyncMock(side_effect=AssertionError("comparison must be DB-only"))):
            async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://test") as client:
                response = await client.get("/api/strategy-comparison?limit=1")
                self.assertEqual(response.status_code, 200)
                j = response.json()
                self.assertEqual((j["shadow"]["signals"], j["shadow"]["resolved"], len(j["latest"])), (2101, 0, 1))
                self.assertEqual(j["exclusions"]["shadow"]["duplicates"], 1)
                self.assertEqual((await client.get("/api/strategy-comparison?limit=0")).status_code, 422)

    async def test_api_preserves_all_existing_tables(self):
        server._insert_paper_signal(signal(1, "SL", -1))
        server._insert_shadow_v2_signal(signal(1, "TP", 2))
        server._save_market_snapshot(dict(time_ms=self.now_ms, ready=0))
        with server._db_connect() as conn:
            before = list(conn.iterdump())
        async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://test") as client:
            j = (await client.get("/api/strategy-comparison")).json()
        self.assertEqual(j["paired"]["avg_delta_r"], 3)
        with server._db_connect() as conn:
            self.assertEqual(before, list(conn.iterdump()))


class ComparisonUITests(unittest.TestCase):
    def test_actual_script_syntax_rendering_and_escaping(self):
        html = Path(__file__).with_name("index.html").read_text()
        j = strategy_comparison([], [signal(1, ticker="<script>bad</script>")])
        script = r'''
const fs=require('fs'), assert=require('assert');
const input=JSON.parse(fs.readFileSync(0,'utf8')),html=input.html;
for(const m of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g))new Function(m[1]);
const elements={};['Summary','Status','Metrics','Notes','Body'].forEach(x=>elements['strategyComparison'+x]={innerHTML:'',textContent:''});
const document={getElementById:id=>elements[id]};
const fmt=(v,d)=>Number(v).toFixed(d),card=(k,v)=>k+':'+v,directionJa=x=>x,resultJa=x=>x;
eval(html.slice(html.indexOf('function renderStrategyComparison('),html.indexOf('function lifecycleReason(')));
renderStrategyComparison(input.j);
assert(elements.strategyComparisonStatus.textContent.includes('INSUFFICIENT SAMPLE'));
assert(!elements.strategyComparisonMetrics.innerHTML.includes('—%'));
assert(elements.strategyComparisonMetrics.innerHTML.includes('INSUFFICIENT SAMPLE'));
assert(elements.strategyComparisonBody.innerHTML.includes('&lt;script&gt;bad&lt;/script&gt;'));
assert(!elements.strategyComparisonBody.innerHTML.includes('<script>'));
assert(elements.strategyComparisonNotes.textContent.includes('TP設計だけの効果とは断定'));
assert(!html.includes('loadShadowV2();loadStrategyComparison();loadSetupLifecycles();'));
assert(!html.includes('data-tab="compare"'));
assert(!html.includes('data-tab="journal"')); // Research renderer remains available without a user-facing tab.
console.log('comparison UI syntax/render/escaping passed');
'''
        p = subprocess.run(["node", "-e", script], input=json.dumps(dict(html=html, j=j)),
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout+p.stderr)


if __name__ == "__main__":
    unittest.main()
