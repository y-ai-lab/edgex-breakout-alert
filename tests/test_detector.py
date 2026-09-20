import unittest

from app import BreakoutDetector, Candle, Contract, JsonStateStore, Settings


def make_settings(**overrides):
    values = dict(
        api_base_url="https://example.com",
        ws_url="wss://example.com/ws",
        intervals=("MINUTE_5",),
        breakout_lookback=3,
        volume_lookback=3,
        volume_multiplier=1.5,
        min_breakout_pct=0.1,
        min_volume_value=0,
        alert_cooldown_minutes=30,
        metadata_refresh_seconds=3600,
        reconnect_initial_seconds=2,
        reconnect_max_seconds=60,
        include_hidden=True,
        history_size=20,
        timezone_name="Asia/Tokyo",
        database_path=__import__("pathlib").Path("/tmp/test-edgex.sqlite3"),
        state_backend="sqlite",
        state_file=__import__("pathlib").Path("/tmp/test-edgex-state.json"),
        telegram_token=None,
        telegram_chat_id=None,
        dry_run=True,
        account_id=None,
        api_key=None,
        api_passphrase=None,
        api_secret=None,
        collateral_coin_id="1000",
        manual_equity_usdc=24.0,
        manual_available_balance_usdc=None,
        manual_leverage=None,
        risk_per_trade=0.05,
        stop_method="signal_candle",
        tp_r_multiple=2.0,
    )
    values.update(overrides)
    return Settings(**values)


def candle(index, high, low, close, value, open_price=None):
    open_price = close if open_price is None else open_price
    return Candle(
        contract_id="1",
        contract_name="TESTUSDC",
        interval="MINUTE_5",
        time_ms=index * 300_000,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=value,
        value=value,
        trades=10,
    )


class BreakoutDetectorTests(unittest.TestCase):
    def setUp(self):
        self.contract = Contract("1", "TESTUSDC", "USDC", True, True)
        self.detector = BreakoutDetector(make_settings())

    def test_up_breakout_requires_volume(self):
        history = [candle(1, 100, 90, 98, 100), candle(2, 101, 91, 99, 100), candle(3, 102, 92, 100, 100)]
        candidate = candle(4, 105, 99, 103, 200, open_price=100)
        signal = self.detector.detect(self.contract, "MINUTE_5", history + [candidate], candidate)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "up")
        self.assertAlmostEqual(signal.volume_ratio, 2.0)

    def test_down_breakout(self):
        history = [candle(1, 110, 100, 102, 100), candle(2, 109, 99, 101, 100), candle(3, 108, 98, 100, 100)]
        candidate = candle(4, 101, 94, 96, 200, open_price=100)
        signal = self.detector.detect(self.contract, "MINUTE_5", history + [candidate], candidate)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "down")
        self.assertLess(signal.breakout_pct, 0)

    def test_wick_without_close_breakout_is_ignored(self):
        history = [candle(1, 100, 90, 98, 100), candle(2, 101, 91, 99, 100), candle(3, 102, 92, 100, 100)]
        candidate = candle(4, 110, 99, 102, 200, open_price=100)
        signal = self.detector.detect(self.contract, "MINUTE_5", history + [candidate], candidate)
        self.assertIsNone(signal)

    def test_low_volume_is_ignored(self):
        history = [candle(1, 100, 90, 98, 100), candle(2, 101, 91, 99, 100), candle(3, 102, 92, 100, 100)]
        candidate = candle(4, 105, 99, 103, 120, open_price=100)
        signal = self.detector.detect(self.contract, "MINUTE_5", history + [candidate], candidate)
        self.assertIsNone(signal)

    def test_not_enough_history_is_ignored(self):
        history = [candle(1, 100, 90, 98, 100), candle(2, 101, 91, 99, 100)]
        candidate = candle(3, 105, 99, 103, 200, open_price=100)
        signal = self.detector.detect(self.contract, "MINUTE_5", history + [candidate], candidate)
        self.assertIsNone(signal)


class JsonStateStoreTests(unittest.TestCase):
    def test_alert_state_round_trips(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = JsonStateStore(path)
            contract = Contract("1", "TESTUSDC", "USDC", True, True)
            detector = BreakoutDetector(make_settings())
            history = [
                candle(1, 100, 90, 98, 100),
                candle(2, 101, 91, 99, 100),
                candle(3, 102, 92, 100, 100),
            ]
            candidate = candle(4, 105, 99, 103, 200, open_price=100)
            signal = detector.detect(contract, "MINUTE_5", history + [candidate], candidate)
            self.assertIsNotNone(signal)
            assert signal is not None
            store.save_alert(signal, 123456)
            store.close()

            reopened = JsonStateStore(path)
            self.assertTrue(reopened.alert_exists(signal.key))
            self.assertEqual(reopened.latest_alert_time("1", "MINUTE_5", "up"), 123456)
            reopened.close()


if __name__ == "__main__":
    unittest.main()
