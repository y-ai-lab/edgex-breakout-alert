"""Storage/API smoke tests; market and external push transport are mocked."""
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from httpx import ASGITransport, AsyncClient

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
            entry_reference=100, shadow_stop_loss=90, shadow_v2_target=120,
            latest_15m_time_ms=source, shadow_v2_extension_target=140,
            shadow_v2_room_rr=4,
        )
        self.assertEqual(server._persist_shadow_v2_signals([row]), 1)
        self.assertEqual(server._persist_shadow_v2_signals([row]), 0)
        saved = server._load_shadow_v2_signals()[0]
        self.assertEqual(saved["created_ms"], source + 900_000 + 1)
        self.assertEqual(saved["model"], "measured_room_fixed_2r")
        self.assertEqual(saved["extension_target"], 140)
        self.assertEqual(server._load_paper_signals(), [])
        self.assertEqual(server._load_push_events(), [])

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
