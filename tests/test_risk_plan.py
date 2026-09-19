import base64
import hashlib
import hmac
import os
import unittest
from unittest.mock import patch

from app import (
    Candle,
    Contract,
    Settings,
    Signal,
    _edgex_hmac_signature,
    _manual_account_asset,
    build_risk_plan,
    format_signal,
)


def make_signal(*, close=100.0, low=95.0, high=102.0, direction="up"):
    contract = Contract(
        contract_id="10000001",
        contract_name="BTCUSDC",
        quote_coin="USDC",
        enable_trade=True,
        enable_display=True,
        step_size=0.1,
        min_order_size=0.1,
        max_order_size=1000.0,
        max_long_leverage=50.0,
        max_short_leverage=50.0,
    )
    candle = Candle(
        contract_id=contract.contract_id,
        contract_name=contract.contract_name,
        interval="MINUTE_5",
        time_ms=1_700_000_000_000,
        open=98.0,
        high=high,
        low=low,
        close=close,
        volume=100.0,
        value=100000.0,
        trades=100.0,
    )
    return Signal(
        contract=contract,
        interval="MINUTE_5",
        direction=direction,
        candle=candle,
        breakout_level=99.0 if direction == "up" else 101.0,
        breakout_pct=1.0 if direction == "up" else -1.0,
        volume_average=50000.0,
        volume_ratio=2.0,
        volume_lookback=20,
    )


def settings():
    env = {
        "EDGE_X_RISK_PER_TRADE": "0.05",
        "EDGE_X_STOP_METHOD": "signal_candle",
        "EDGE_X_TP_R_MULTIPLE": "2.0",
        "EDGE_X_COLLATERAL_COIN_ID": "1000",
    }
    with patch.dict(os.environ, env, clear=True):
        return Settings.from_env(dry_run_override=True)


def manual_settings():
    env = {
        "EDGEX_EQUITY_USDC": "1000",
        "EDGE_X_RISK_PER_TRADE": "0.05",
        "EDGE_X_STOP_METHOD": "signal_candle",
        "EDGE_X_TP_R_MULTIPLE": "2.0",
        "EDGE_X_COLLATERAL_COIN_ID": "1000",
    }
    with patch.dict(os.environ, env, clear=True):
        return Settings.from_env(dry_run_override=True)


def account_asset(*, equity="1000", available="500", leverage="3"):
    return {
        "code": "SUCCESS",
        "data": {
            "account": {
                "id": "123",
                "contractIdToTradeSetting": {
                    "10000001": {"leverage": leverage}
                },
            },
            "collateralAssetModelList": [
                {
                    "coinId": "1000",
                    "totalEquity": equity,
                    "availableBalance": available,
                }
            ],
        },
    }


class RiskPlanTests(unittest.TestCase):
    def test_hmac_matches_official_sdk_flow(self):
        secret = "secret-value"
        timestamp = "1773302047172"
        method = "GET"
        path = "/api/v2/private/account/getAccountAsset"
        body = "accountId=123"
        expected = hmac.new(
            base64.b64encode(secret.encode()),
            f"{timestamp}{method}{path}{body}".encode(),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(
            _edgex_hmac_signature(secret, timestamp, method, path, body),
            expected,
        )

    def test_five_percent_risk_size(self):
        plan = build_risk_plan(make_signal(), account_asset(), settings())
        self.assertAlmostEqual(plan.equity, 1000.0)
        self.assertAlmostEqual(plan.risk_budget, 50.0)
        self.assertAlmostEqual(plan.entry_price, 100.0)
        self.assertAlmostEqual(plan.stop_loss, 95.0)
        self.assertAlmostEqual(plan.size, 10.0)
        self.assertAlmostEqual(plan.max_loss, 50.0)
        self.assertAlmostEqual(plan.actual_risk_fraction, 0.05)
        self.assertLessEqual(plan.max_loss, plan.risk_budget)
        self.assertAlmostEqual(plan.tp_1r, 105.0)
        self.assertAlmostEqual(plan.tp_target, 110.0)
        self.assertFalse(plan.margin_capped)

    def test_manual_equity_works_without_api_credentials(self):
        cfg = manual_settings()
        self.assertTrue(cfg.manual_risk_enabled)
        self.assertFalse(cfg.account_risk_enabled)
        plan = build_risk_plan(make_signal(), _manual_account_asset(cfg), cfg)
        self.assertAlmostEqual(plan.equity, 1000.0)
        self.assertAlmostEqual(plan.risk_budget, 50.0)
        self.assertAlmostEqual(plan.size, 10.0)
        self.assertAlmostEqual(plan.max_loss, 50.0)

    def test_margin_cap_never_exceeds_five_percent_loss(self):
        signal = make_signal(close=100.0, low=99.5)
        plan = build_risk_plan(signal, account_asset(), settings())
        # 5% risk alone requests 100 units, but 500 available x 3 leverage
        # caps notional to 1500, i.e. 15 units.
        self.assertAlmostEqual(plan.theoretical_size, 100.0)
        self.assertAlmostEqual(plan.size, 15.0)
        self.assertTrue(plan.margin_capped)
        self.assertAlmostEqual(plan.max_loss, 7.5)
        self.assertLess(plan.actual_risk_fraction, 0.05)

    def test_notification_contains_risk_instruction(self):
        signal = make_signal()
        plan = build_risk_plan(signal, account_asset(), settings())
        text = format_signal(signal, "Asia/Tokyo", risk_plan=plan)
        self.assertIn("5%リスク エントリー指示", text)
        self.assertIn("リスク予算: $50", text)
        self.assertIn("枚数: 10", text)
        self.assertIn("SL損失: -$50", text)
        self.assertIn("2R:", text)


if __name__ == "__main__":
    unittest.main()
