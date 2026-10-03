"""Storage/API smoke tests; market and external push transport are mocked."""
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from httpx import ASGITransport, AsyncClient
from analysis_terminal.setups import setup_identity

# No network delivery is allowed from these tests, even with VAPID configured.
transport = types.ModuleType("pywebpush")
transport.WebPushException = Exception
transport.webpush = Mock(side_effect=AssertionError("unexpected push delivery"))
with patch.dict(sys.modules, {"pywebpush": transport}):
    from analysis_terminal import server


class StorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        db = Path(self.directory.name) / "analysis_terminal.db"
        for name, value in (
            ("DB_PATH", db), ("VAPID_PRIVATE_KEY", ""), ("VAPID_PUBLIC_KEY", ""),
        ):
            context = patch.object(server, name, value)
            context.start()
            self.addCleanup(context.stop)
        self.now_ms = int(server.time.time() * 1000)
        history = patch.object(server, "fetch_history", AsyncMock(side_effect=AssertionError("unexpected history request")))
        history.start()
        self.addCleanup(history.stop)
        # A legacy table predating the additive preference migrations.
        with sqlite3.connect(db) as conn:
            conn.execute("""CREATE TABLE push_subscriptions (
                endpoint TEXT PRIMARY KEY, payload TEXT NOT NULL,
                candidate_alerts INTEGER NOT NULL DEFAULT 1,
                daily_summary INTEGER NOT NULL DEFAULT 1,
                timezone TEXT NOT NULL DEFAULT 'Asia/Tokyo',
                created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
                last_success_ms INTEGER)""")
            conn.execute(
                "INSERT INTO push_subscriptions(endpoint,payload,created_ms,updated_ms) VALUES (?,?,?,?)",
                ("https://push.example.invalid/subscriber", "{}", self.now_ms, self.now_ms),
            )
        server._init_db()

    async def test_page_version_tracks_health_after_release(self):
        for version in (server.app.version, "19.0.999"):
            with patch.object(server.app, "version", version):
                async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://test") as client:
                    page = await client.get("/")
                    health = (await client.get("/health")).json()
                self.assertEqual(health["version"], version)
                self.assertIn(f"<title>EdgeX 分析ターミナル v{version}</title>", page.text)
                self.assertIn(f'class="versionBadge">v{version}</span>', page.text)
                self.assertNotIn("__APP_VERSION__", page.text)
                self.assertIn("no-store", page.headers["cache-control"])

    def test_repeated_migration_preserves_snapshots_subscribers_and_shadow(self):
        snapshot = dict(time_ms=self.now_ms, ready=0)
        server._save_market_snapshot(snapshot)
        legacy = dict(key="legacy", created_ms=self.now_ms, result=dict(status="TP", final_r=2))
        server._insert_shadow_v2_signal(legacy)
        server._init_db()
        server._init_db()
        self.assertEqual(server._subscription_count(), 1)
        self.assertEqual(server._load_market_history(), [snapshot])
        self.assertEqual(server._load_shadow_v2_signals(), [legacy])
        with server._db_connect() as conn:
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(push_subscriptions)")}
        self.assertTrue({"snooze_until_ms", "quiet_start", "quiet_end"} <= cols)

    def test_shadow_persistence_keeps_sentinel_and_deduplicates(self):
        source = self.now_ms // 900_000 * 900_000 - 900_000
        row = dict(
            ticker="TESTUSDC", direction="LONG", shadow_v2_ready=True,
            breakout_time_ms=source - 14400000, breakout_level=99,
            entry_reference=100, shadow_stop_loss=90, shadow_v2_target=120,
            latest_15m_time_ms=source, shadow_v2_extension_target=140,
            shadow_v2_room_rr=4,
        )
        self.assertEqual(server._persist_shadow_v2_signals([row]), 1)
        self.assertEqual(server._persist_shadow_v2_signals([row]), 0)
        self.assertEqual(server._persist_shadow_v2_signals([dict(row, latest_15m_time_ms=source+900_000, entry_reference=101)]), 0)
        saved = server._load_shadow_v2_signals()[0]
        self.assertEqual(saved["created_ms"], source + 900_000 + 1)
        self.assertEqual(saved["model"], "measured_room_fixed_2r")
        self.assertEqual(saved["extension_target"], 140)
        self.assertEqual(saved["setup_id"], setup_identity(row))
        self.assertEqual(server._persist_shadow_v2_signals([dict(row, breakout_time_ms=source)]), 1)
        self.assertEqual(server._load_paper_signals(), [])
        self.assertEqual(server._load_push_events(), [])

    def test_current_ready_is_saved_once_per_setup_with_first_entry(self):
        row = dict(ticker="TESTUSDC", direction="LONG", stage="READY", score=90,
                   latest_15m_time_ms=self.now_ms // 900000 * 900000 - 900000,
                   breakout_time_ms=self.now_ms - 14400000, breakout_level=99,
                   entry_reference=100, stop_loss=90, take_profit=125, rr=2.5)
        server._persist_scan_result({}, [row])
        server._persist_scan_result({}, [dict(row, latest_15m_time_ms=row["latest_15m_time_ms"]+900000, entry_reference=101)])
        signals = server._load_paper_signals()
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["entry"], 100)
        self.assertEqual(signals[0]["created_ms"], row["latest_15m_time_ms"]+900000+1)
        self.assertEqual(signals[0]["setup_id"], setup_identity(row))

    def test_migration_retains_legacy_events_results_and_leaves_identity_unknown(self):
        row = dict(ticker="TESTUSDC", direction="LONG", entry_reference=100, stop_loss=90, take_profit=120)
        server._log_candidate_event(row, kind="NEAR", label="legacy")
        event = server._load_candidate_events()[0]
        server._upsert_candidate_event_result(event["id"], dict(status="TP", final_r=2))
        with server._db_connect() as conn:
            conn.execute("ALTER TABLE candidate_events DROP COLUMN setup_id")
            conn.execute("ALTER TABLE approach_events DROP COLUMN setup_id")
        server._init_db()
        server._init_db()
        saved = server._load_candidate_events_with_results()[0]
        self.assertIsNone(saved["setup_id"])
        self.assertEqual(saved["result"]["final_r"], 2)
        self.assertEqual(server._subscription_count(), 1)

    async def test_push_identity_migration_baselines_and_new_setup_is_new_event(self):
        near = dict(ticker="TESTUSDC", direction="LONG", stage="CONFIRMATION_WAIT", rr=3, score=85, setup_id="first")
        server._state_set("server_candidate_state", '{"near":["TESTUSDC"],"ready":[]}')
        with patch.object(server, "_broadcast_push", AsyncMock()) as broadcast:
            await server._maybe_push_candidate_changes([near])
            broadcast.assert_not_called()
            await server._maybe_push_candidate_changes([dict(near, stage="READY")])
            self.assertEqual(broadcast.await_count, 1)
            self.assertEqual(server._load_candidate_events()[0]["setup_id"], "first")
            self.assertIn("昇格", server._load_candidate_events()[0]["label"])
            await server._maybe_push_candidate_changes([dict(near, stage="READY")])
            self.assertEqual(broadcast.await_count, 1)
            await server._maybe_push_candidate_changes([dict(near, setup_id="second", stage="READY")])
            self.assertEqual(broadcast.await_count, 2)
            self.assertEqual(server._load_candidate_events()[0]["label"], "新しくエントリー可能")
        self.assertEqual(server._subscription_count(), 1)

    async def test_only_current_ready_delivers_but_near_history_is_kept(self):
        server._state_set("server_candidate_state", '{"identity_version":1,"near":[],"ready":[]}')
        near = dict(ticker="TESTUSDC", direction="LONG", stage="CONFIRMATION_WAIT",
                    rr=3, score=85, setup_id="first", shadow_v2_ready=True)
        async def inline(func, *args):
            return func(*args)
        with patch.object(server, "_send_push_sync", return_value=True) as send, patch.object(server.asyncio, "to_thread", inline):
            await server._maybe_push_candidate_changes([near])
            send.assert_not_called()
            self.assertEqual(server._load_candidate_events()[0]["kind"], "NEAR")
            await server._maybe_push_candidate_changes([dict(near, stage="RR_WAIT")])
            send.assert_not_called()  # Shadow READY must never notify.
            await server._maybe_push_candidate_changes([dict(near, stage="READY")])
            send.assert_called_once()
            payload = send.call_args.args[1]
            self.assertEqual(payload["notification_kind"], "READY")
            self.assertEqual(payload["title"], "EdgeX エントリー可能")
            await server._maybe_push_candidate_changes([dict(near, stage="READY")])
            send.assert_called_once()
        self.assertEqual(server._load_push_events()[0]["kind"], "ready")

    async def test_non_entry_dispatch_is_blocked_even_for_legacy_preferences(self):
        before = dict(server._get_push_subscription("https://push.example.invalid/subscriber"))
        server._create_custom_alert(server.CustomAlertCreateRequest(
            endpoint=before["endpoint"], ticker="TESTUSDC", condition="PRICE_ABOVE", threshold=100))
        rule = server._load_custom_alerts(endpoint=before["endpoint"])[0]
        with patch.object(server, "_send_push_sync", Mock()) as send:
            for kind in ("candidate", "daily", "custom", "ready"):
                self.assertEqual(await server._broadcast_push({"title": "legacy"}, kind=kind), (0, 0))
            await server._maybe_push_daily_summary([])
            await server._evaluate_custom_alerts([dict(ticker="TESTUSDC", current_price=110)])
            send.assert_not_called()
        with patch.object(server, "webpush", Mock()) as push, patch.object(server, "_push_enabled", return_value=True):
            self.assertFalse(server._send_push_sync(before, {"title": "legacy custom"}))
            push.assert_not_called()
        self.assertEqual(dict(server._get_push_subscription(before["endpoint"])), before)
        self.assertEqual(server._load_push_subscriptions("daily"), [])
        self.assertEqual(server._load_custom_alerts(endpoint=before["endpoint"])[0], rule)

    async def test_ranking_events_are_kept_without_push(self):
        row = dict(ticker="TESTUSDC", stage="READY", setup_id="first", priority_score=90, rr=3)
        with (
            patch.object(server, "_priority_ranking", return_value=[row]),
            patch.object(server, "_load_previous_priority_snapshot", return_value=[dict(row, rank=8)]),
            patch.object(server, "_approach_score", return_value=dict(approach_score=85)),
            patch.object(server, "_broadcast_push", AsyncMock()) as broadcast,
        ):
            await server._process_priority_changes([row], self.now_ms)
            broadcast.assert_not_called()
        with server._db_connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM priority_events").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM priority_snapshots").fetchone()[0], 1)

    async def test_explicit_test_request_still_works_without_real_transport(self):
        async def inline(func, *args):
            return func(*args)
        with patch.object(server, "_send_push_sync", return_value=True) as send, patch.object(server.asyncio, "to_thread", inline):
            response = await server.push_test_api(server.PushTestRequest(endpoint="https://push.example.invalid/subscriber"))
            self.assertTrue(response["delivered"])
            self.assertEqual(send.call_args.args[1]["notification_kind"], "MANUAL")

    async def test_ready_honors_opt_out_snooze_and_quiet_hours(self):
        endpoint = "https://push.example.invalid/subscriber"
        for preferences in (
            dict(candidate_alerts=False),
            dict(snooze_until_ms=self.now_ms + 3600000),
            dict(quiet_start="00:00", quiet_end="23:59"),
        ):
            req = server.PushPreferenceRequest(endpoint=endpoint, **preferences)
            server._update_push_preferences(req)
            # Force noon for a deterministic quiet-hours check, independent of test clock.
            with patch.object(server, "datetime", wraps=server.datetime) as clock:
                clock.now.return_value = server.datetime(2026, 10, 3, 12, 0, tzinfo=server.JST)
                with patch.object(server, "_send_push_sync", Mock()) as send:
                    self.assertEqual(await server._broadcast_push({"notification_kind": "READY"}, kind="ready"), (0, 0))
                    send.assert_not_called()

    async def test_entry_policy_apis_keep_subscription_and_reject_custom_creation(self):
        endpoint = "https://push.example.invalid/subscriber"
        original = dict(server._get_push_subscription(endpoint))
        async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://test") as client:
            cfg = (await client.get("/api/push/config")).json()
            self.assertEqual(cfg["notification_policy"], "READY_ONLY")
            self.assertFalse(cfg["daily_summary_enabled"])
            prefs = (await client.get("/api/push/preferences", params={"endpoint": endpoint})).json()
            self.assertTrue(prefs["candidate_alerts"])
            self.assertFalse(prefs["daily_summary"])
            response = await client.post("/api/push/preferences", json=dict(endpoint=endpoint, candidate_alerts=True, daily_summary=True))
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["daily_summary"])
            custom = await client.post("/api/custom-alerts", json=dict(endpoint=endpoint, ticker="TESTUSDC", condition="PRICE_ABOVE", threshold=100))
            self.assertEqual(custom.status_code, 409)
        saved = server._get_push_subscription(endpoint)
        for field in ("endpoint", "payload", "created_ms", "last_success_ms", "timezone"):
            self.assertEqual(saved[field], original[field])
        self.assertEqual(server._subscription_count(), 1)

    def test_opportunity_excludes_unknown_identity_from_no_ready_claim(self):
        base = dict(id=1, ticker="TESTUSDC", kind="NEAR", label="test", stage="CONFIRMATION_WAIT", direction="LONG", score=80, rr=2,
                    entry=100, stop=90, target=120, created_ms=self.now_ms-60000,
                    result=dict(status="TP", final_r=2, outcome_time_ms=self.now_ms))
        events = [dict(base, setup_id="first"), dict(base, id=2), dict(base, id=3, kind="READY", setup_id="second", created_ms=self.now_ms-30000)]
        with patch.object(server, "_load_candidate_events_with_results", return_value=events):
            result = server._opportunity_analysis()
        self.assertEqual(result["confirmation_effect"]["near_became_ready"], 0)
        self.assertEqual(result["confirmation_effect"]["near_tp_without_ready"], 1)
        self.assertEqual(result["confirmation_effect"]["identity_unavailable"], 1)
        self.assertIsNone(next(item for item in result["latest"] if item["id"] == 2)["became_ready"])

    def test_approach_ready_association_uses_setup_not_ticker(self):
        row = dict(ticker="TESTUSDC", direction="LONG", setup_id="second")
        server._log_candidate_event(row, kind="READY", label="test")
        base = dict(ticker="TESTUSDC", current_score=75, created_ms=self.now_ms-60000, entry=100, stop=90, target=120, result=None)
        with patch.object(server, "_load_approach_events_with_results", return_value=[dict(base, setup_id="first"), dict(base, setup_id="second"), base]):
            result = server._approach_validation()
        self.assertEqual(result["overall"]["became_ready"], 1)
        self.assertEqual(result["overall"]["identity_unavailable"], 1)

    def test_approach_identity_round_trips_through_sql_and_result_join(self):
        server._log_approach_event(bucket_ms=self.now_ms, ticker="TESTUSDC", previous_score=60, current_score=75, current_rank=4,
                                   stage="CONFIRMATION_WAIT", direction="LONG", rr=2, priority_score=70, entry=100, stop=90, target=120, setup_id="first")
        event = server._load_approach_events()[0]
        self.assertEqual(event["setup_id"], "first")
        server._upsert_approach_event_result(event["id"], dict(status="OPEN"))
        self.assertEqual(server._load_approach_events_with_results()[0]["setup_id"], "first")

    async def test_local_api_smoke_and_push_read_routes(self):
        contract = server.scanner.Contract("1", "TESTUSDC", "USDC", True, True)
        row = dict(
            ticker="TESTUSDC", contract_id="1", stage="RR_WAIT", direction="LONG",
            current_price=100, score=75, rr=0.5, entry_reference=100, stop_loss=90,
            take_profit=105, retest_touched=True, confirmed=True,
            shadow_measured_ready=True, shadow_fixed_2r_ready=True,
            shadow_v2_ready=True, shadow_v2_target=120,
            shadow_v2_extension_target=140, shadow_v2_room_rr=4,
        )
        server._save_market_snapshot(dict(time_ms=self.now_ms, ready=0))
        with (
            patch.object(server, "_background_collector", AsyncMock()),
            patch.object(server, "_scan_market_rows", AsyncMock(return_value=({"1": contract}, [row]))),
            patch.object(server.CLIENT, "get_contracts", AsyncMock(return_value={"1": contract})),
            patch.object(server, "fetch_snapshots", AsyncMock(return_value={})),
            patch.object(server, "_snapshot_cache", (server.time.time(), {})),
        ):
            async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://test") as client:
                for path in (
                    "/", "/health", "/api/screener?limit=3", "/api/analyze?ticker=TESTUSDC",
                    "/api/readiness-review", "/api/shadow-v2", "/api/push/config", "/api/push/events",
                ):
                    with self.subTest(path=path):
                        self.assertEqual((await client.get(path)).status_code, 200)
                health = (await client.get("/health")).json()
                self.assertTrue(health["storage"]["db_exists"])
                self.assertEqual(health["push"]["subscribers"], 1)
                review = (await client.get("/api/readiness-review")).json()["review"]
                self.assertEqual(review["confirmed_after_retest"], 1)
                self.assertEqual(review["current"]["ready_count"], 0)
                self.assertEqual(review["proposed_v2"]["ready_count"], 1)
                self.assertEqual((await client.get("/api/shadow-v2")).json()["metrics"]["sample_status"], "INSUFFICIENT SAMPLE")
                self.assertEqual((await client.get("/api/push/preferences", params={"endpoint": "https://push.example.invalid/subscriber"})).status_code, 200)
                self.assertEqual((await client.post("/api/push/subscribe", json={"subscription": {"endpoint": "https://push.example.invalid/test"}})).status_code, 503)
        self.assertEqual(server._subscription_count(), 1)
        self.assertEqual(transport.webpush.call_count, 0)


if __name__ == "__main__":
    unittest.main()
