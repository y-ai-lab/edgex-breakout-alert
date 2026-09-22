"""EdgeX multi-timeframe roll-reversal alert service.

The service monitors the 4-hour trend and recent role-reversal level, then
uses 15-minute closed candles for pullback / rally entry confirmation. It
sizes positions so the stop-loss risk stays at or below the configured
fraction of current equity and sends Telegram instructions only. It never
places orders.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import math
import os
import signal
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - .env loading is optional
    pass

import websockets
from websockets.exceptions import ConnectionClosed


LOGGER = logging.getLogger("edgex-breakout")


INTERVAL_MS: dict[str, int] = {
    "MINUTE_1": 60_000,
    "MINUTE_5": 5 * 60_000,
    "MINUTE_15": 15 * 60_000,
    "MINUTE_30": 30 * 60_000,
    "HOUR_1": 60 * 60_000,
    "HOUR_2": 2 * 60 * 60_000,
    "HOUR_4": 4 * 60 * 60_000,
    "HOUR_6": 6 * 60 * 60_000,
    "HOUR_8": 8 * 60 * 60_000,
    "HOUR_12": 12 * 60 * 60_000,
    "DAY_1": 24 * 60 * 60_000,
    "WEEK_1": 7 * 24 * 60 * 60_000,
    "MONTH_1": 30 * 24 * 60 * 60_000,
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return float(raw)


def _boolish(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return default


def _number(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        parsed = float(value)
        if parsed != parsed or parsed in {float("inf"), float("-inf")}:
            return None
        return parsed
    except (TypeError, ValueError):
        return None


def _format_number(value: float | None, digits: int = 8) -> str:
    if value is None:
        return "-"
    if value == 0:
        return "0"
    absolute = abs(value)
    if absolute >= 1000:
        text = f"{value:,.2f}"
    elif absolute >= 1:
        text = f"{value:,.6f}"
    else:
        text = f"{value:.{digits}f}"
    return text.rstrip("0").rstrip(".")


def _format_usd(value: float | None) -> str:
    if value is None:
        return "-"
    if abs(value) >= 1_000_000:
        return f"${value:,.0f}"
    if abs(value) >= 1_000:
        return f"${value:,.2f}"
    return f"${value:,.4f}"


def _floor_to_step(value: float, step: float | None) -> float:
    if value <= 0:
        return 0.0
    if step is None or step <= 0:
        return value
    return math.floor((value + step * 1e-9) / step) * step


def _edgex_hmac_signature(
    api_secret: str,
    timestamp: str,
    method: str,
    request_uri: str,
    body: str,
) -> str:
    """Mirror the official EdgeX V2 SDK HMAC signing flow."""
    message = f"{timestamp}{method.upper()}{request_uri}{body}"
    secret_bytes = base64.b64encode(api_secret.encode("utf-8"))
    return hmac.new(secret_bytes, message.encode("utf-8"), hashlib.sha256).hexdigest()


def _interval_label(interval: str) -> str:
    labels = {
        "MINUTE_1": "1分",
        "MINUTE_5": "5分",
        "MINUTE_15": "15分",
        "MINUTE_30": "30分",
        "HOUR_1": "1時間",
        "HOUR_2": "2時間",
        "HOUR_4": "4時間",
        "HOUR_6": "6時間",
        "HOUR_8": "8時間",
        "HOUR_12": "12時間",
        "DAY_1": "日足",
        "WEEK_1": "週足",
        "MONTH_1": "月足",
    }
    return labels.get(interval, interval)


def _ema(values: Iterable[float], period: int) -> float | None:
    data = [float(value) for value in values]
    if period <= 0 or len(data) < period:
        return None
    seed = sum(data[:period]) / period
    multiplier = 2.0 / (period + 1.0)
    ema = seed
    for value in data[period:]:
        ema = (value - ema) * multiplier + ema
    return ema


def _atr(candles: Iterable["Candle"], period: int) -> float | None:
    ordered = sorted(candles, key=lambda item: item.time_ms)
    if period <= 0 or len(ordered) < period + 1:
        return None
    true_ranges: list[float] = []
    previous_close = ordered[0].close
    for candle in ordered[1:]:
        true_range = max(
            candle.high - candle.low,
            abs(candle.high - previous_close),
            abs(candle.low - previous_close),
        )
        true_ranges.append(true_range)
        previous_close = candle.close
    if len(true_ranges) < period:
        return None
    atr = sum(true_ranges[:period]) / period
    for true_range in true_ranges[period:]:
        atr = ((atr * (period - 1)) + true_range) / period
    return atr


@dataclass(frozen=True)
class Settings:
    api_base_url: str
    ws_url: str
    intervals: tuple[str, ...]
    breakout_lookback: int
    volume_lookback: int
    volume_multiplier: float
    min_breakout_pct: float
    min_volume_value: float
    alert_cooldown_minutes: int
    metadata_refresh_seconds: int
    reconnect_initial_seconds: float
    reconnect_max_seconds: float
    include_hidden: bool
    history_size: int
    timezone_name: str
    database_path: Path
    state_backend: str
    state_file: Path
    telegram_token: str | None
    telegram_chat_id: str | None
    dry_run: bool
    account_id: str | None
    api_key: str | None
    api_passphrase: str | None
    api_secret: str | None
    collateral_coin_id: str
    manual_equity_usdc: float | None
    manual_available_balance_usdc: float | None
    manual_leverage: float | None
    risk_per_trade: float
    stop_method: str
    tp_r_multiple: float
    monitor_interval: str
    entry_interval: str
    trend_fast_ema: int
    trend_slow_ema: int
    roll_lookback: int
    roll_max_age: int
    retest_lookback: int
    atr_period: int
    atr_stop_buffer: float
    atr_target_buffer: float
    retest_atr_tolerance: float
    min_rr: float
    split_rr: float
    pullback_swing_lookback: int

    @property
    def account_risk_enabled(self) -> bool:
        return all((self.account_id, self.api_key, self.api_passphrase, self.api_secret))

    @property
    def manual_risk_enabled(self) -> bool:
        return self.manual_equity_usdc is not None and self.manual_equity_usdc > 0

    @classmethod
    def from_env(cls, *, dry_run_override: bool | None = None) -> "Settings":
        monitor_interval = os.getenv("EDGE_X_MONITOR_INTERVAL", "HOUR_4").strip().upper()
        entry_interval = os.getenv("EDGE_X_ENTRY_INTERVAL", "MINUTE_15").strip().upper()
        unknown_strategy = sorted({monitor_interval, entry_interval} - set(INTERVAL_MS))
        if unknown_strategy:
            raise ValueError(f"Unsupported strategy interval(s): {', '.join(unknown_strategy)}")
        if monitor_interval == entry_interval:
            raise ValueError("Monitor and entry intervals must be different")

        raw_intervals = os.getenv(
            "EDGE_X_INTERVALS",
            os.getenv("EDGE_X_INTERVAL", f"{monitor_interval},{entry_interval}"),
        )
        requested = [x.strip().upper() for x in raw_intervals.split(",") if x.strip()]
        intervals = tuple(dict.fromkeys([*requested, monitor_interval, entry_interval]))
        unknown = sorted(set(intervals) - set(INTERVAL_MS))
        if unknown:
            raise ValueError(f"Unsupported EdgeX interval(s): {', '.join(unknown)}")

        breakout_lookback = _env_int("EDGE_X_BREAKOUT_LOOKBACK", 20)
        volume_lookback = _env_int("EDGE_X_VOLUME_LOOKBACK", 20)
        trend_fast_ema = _env_int("EDGE_X_TREND_FAST_EMA", 20)
        trend_slow_ema = _env_int("EDGE_X_TREND_SLOW_EMA", 50)
        roll_lookback = _env_int("EDGE_X_ROLL_LOOKBACK", 20)
        roll_max_age = _env_int("EDGE_X_ROLL_MAX_AGE", 6)
        retest_lookback = _env_int("EDGE_X_RETEST_LOOKBACK", 4)
        atr_period = _env_int("EDGE_X_ATR_PERIOD", 14)
        pullback_swing_lookback = _env_int("EDGE_X_PULLBACK_SWING_LOOKBACK", 5)
        if min(breakout_lookback, volume_lookback, trend_fast_ema, roll_lookback, atr_period) < 2:
            raise ValueError("Strategy lookback values must be at least 2")
        if trend_slow_ema <= trend_fast_ema:
            raise ValueError("EDGE_X_TREND_SLOW_EMA must be greater than EDGE_X_TREND_FAST_EMA")
        if min(roll_max_age, retest_lookback, pullback_swing_lookback) < 1:
            raise ValueError("Strategy age/lookback values must be at least 1")

        history_size = _env_int(
            "EDGE_X_HISTORY_SIZE",
            max(96, trend_slow_ema + roll_lookback + roll_max_age + 5, atr_period + 10),
        )
        minimum_history = max(trend_slow_ema + 2, roll_lookback + roll_max_age + 2, atr_period + 2)
        if history_size < minimum_history:
            raise ValueError("EDGE_X_HISTORY_SIZE is too small for the multi-timeframe strategy")

        dry_run = _env_bool("DRY_RUN", False) if dry_run_override is None else dry_run_override
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not dry_run and (not token or not chat_id):
            raise ValueError(
                "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required; "
                "set DRY_RUN=true when testing without Telegram"
            )

        database_path = Path(os.getenv("DATABASE_PATH", "data/edgex_breakout.sqlite3"))
        state_backend = os.getenv("STATE_BACKEND", "sqlite").strip().lower()
        if state_backend not in {"sqlite", "json"}:
            raise ValueError("STATE_BACKEND must be sqlite or json")
        state_file = Path(os.getenv("STATE_FILE", "data/edgex_alert_state.json"))

        risk_per_trade = _env_float("EDGE_X_RISK_PER_TRADE", 0.05)
        if not 0 < risk_per_trade < 1:
            raise ValueError("EDGE_X_RISK_PER_TRADE must be greater than 0 and less than 1")
        stop_method = os.getenv("EDGE_X_STOP_METHOD", "signal_candle").strip().lower()
        if stop_method not in {"signal_candle", "breakout_level"}:
            raise ValueError("EDGE_X_STOP_METHOD must be signal_candle or breakout_level")
        tp_r_multiple = _env_float("EDGE_X_TP_R_MULTIPLE", 2.0)
        if tp_r_multiple <= 0:
            raise ValueError("EDGE_X_TP_R_MULTIPLE must be greater than 0")

        atr_stop_buffer = _env_float("EDGE_X_ATR_STOP_BUFFER", 0.5)
        atr_target_buffer = _env_float("EDGE_X_ATR_TARGET_BUFFER", 0.25)
        retest_atr_tolerance = _env_float("EDGE_X_RETEST_ATR_TOLERANCE", 0.35)
        min_rr = _env_float("EDGE_X_MIN_RR", 2.0)
        split_rr = _env_float("EDGE_X_SPLIT_RR", 3.0)
        if min(atr_stop_buffer, atr_target_buffer, retest_atr_tolerance) < 0:
            raise ValueError("ATR buffers/tolerance cannot be negative")
        if min_rr < 2.0:
            raise ValueError("EDGE_X_MIN_RR must be at least 2.0")
        if split_rr < min_rr:
            raise ValueError("EDGE_X_SPLIT_RR must be greater than or equal to EDGE_X_MIN_RR")

        manual_equity_usdc = _number(os.getenv("EDGEX_EQUITY_USDC"))
        manual_available_balance_usdc = _number(os.getenv("EDGEX_AVAILABLE_BALANCE_USDC"))
        manual_leverage = _number(os.getenv("EDGEX_LEVERAGE"))
        if manual_equity_usdc is not None and manual_equity_usdc <= 0:
            raise ValueError("EDGEX_EQUITY_USDC must be greater than 0")
        if manual_available_balance_usdc is not None and manual_available_balance_usdc <= 0:
            raise ValueError("EDGEX_AVAILABLE_BALANCE_USDC must be greater than 0")
        if manual_leverage is not None and manual_leverage <= 0:
            raise ValueError("EDGEX_LEVERAGE must be greater than 0")

        def optional_env(name: str) -> str | None:
            value = os.getenv(name)
            return value.strip() if value and value.strip() else None

        return cls(
            api_base_url=os.getenv("EDGE_X_API_BASE_URL", "https://edgex-prod-v2.edgex.exchange").rstrip("/"),
            ws_url=os.getenv("EDGE_X_WS_URL", "wss://edgex-quote-prod-v2.edgex.exchange/api/v1/public/ws"),
            intervals=intervals,
            breakout_lookback=breakout_lookback,
            volume_lookback=volume_lookback,
            volume_multiplier=max(0.0, _env_float("EDGE_X_VOLUME_MULTIPLIER", 1.5)),
            min_breakout_pct=max(0.0, _env_float("EDGE_X_MIN_BREAKOUT_PCT", 0.1)),
            min_volume_value=max(0.0, _env_float("EDGE_X_MIN_VOLUME_VALUE", 0.0)),
            alert_cooldown_minutes=max(0, _env_int("EDGE_X_ALERT_COOLDOWN_MINUTES", 0)),
            metadata_refresh_seconds=max(300, _env_int("EDGE_X_METADATA_REFRESH_SECONDS", 3600)),
            reconnect_initial_seconds=max(1.0, _env_float("EDGE_X_RECONNECT_INITIAL_SECONDS", 2.0)),
            reconnect_max_seconds=max(5.0, _env_float("EDGE_X_RECONNECT_MAX_SECONDS", 60.0)),
            include_hidden=_env_bool("EDGE_X_INCLUDE_HIDDEN", True),
            history_size=history_size,
            timezone_name=os.getenv("TIMEZONE", "Asia/Tokyo"),
            database_path=database_path,
            state_backend=state_backend,
            state_file=state_file,
            telegram_token=token,
            telegram_chat_id=chat_id,
            dry_run=dry_run,
            account_id=optional_env("EDGEX_ACCOUNT_ID"),
            api_key=optional_env("EDGEX_API_KEY"),
            api_passphrase=optional_env("EDGEX_API_PASSPHRASE"),
            api_secret=optional_env("EDGEX_API_SECRET"),
            collateral_coin_id=os.getenv("EDGE_X_COLLATERAL_COIN_ID", "1000").strip() or "1000",
            manual_equity_usdc=manual_equity_usdc,
            manual_available_balance_usdc=manual_available_balance_usdc,
            manual_leverage=manual_leverage,
            risk_per_trade=risk_per_trade,
            stop_method=stop_method,
            tp_r_multiple=tp_r_multiple,
            monitor_interval=monitor_interval,
            entry_interval=entry_interval,
            trend_fast_ema=trend_fast_ema,
            trend_slow_ema=trend_slow_ema,
            roll_lookback=roll_lookback,
            roll_max_age=roll_max_age,
            retest_lookback=retest_lookback,
            atr_period=atr_period,
            atr_stop_buffer=atr_stop_buffer,
            atr_target_buffer=atr_target_buffer,
            retest_atr_tolerance=retest_atr_tolerance,
            min_rr=min_rr,
            split_rr=split_rr,
            pullback_swing_lookback=pullback_swing_lookback,
        )


@dataclass(frozen=True)
class Contract:
    contract_id: str
    contract_name: str
    quote_coin: str
    enable_trade: bool
    enable_display: bool
    step_size: float | None = None
    min_order_size: float | None = None
    max_order_size: float | None = None
    max_long_leverage: float | None = None
    max_short_leverage: float | None = None


@dataclass(frozen=True)
class Candle:
    contract_id: str
    contract_name: str
    interval: str
    time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    value: float
    trades: float | None

    @classmethod
    def from_payload(cls, payload: dict[str, Any], fallback_interval: str | None = None) -> "Candle | None":
        contract_id = str(payload.get("contractId", "")).strip()
        contract_name = str(payload.get("contractName", contract_id)).strip() or contract_id
        interval = str(payload.get("klineType") or payload.get("interval") or fallback_interval or "").upper()
        try:
            time_ms = int(payload.get("klineTime") or payload.get("startTime"))
        except (TypeError, ValueError):
            return None
        open_price = _number(payload.get("open") or payload.get("openPrice"))
        high = _number(payload.get("high") or payload.get("highPrice"))
        low = _number(payload.get("low") or payload.get("lowPrice"))
        close = _number(payload.get("close") or payload.get("closePrice"))
        volume = _number(payload.get("size") or payload.get("volume"))
        value = _number(payload.get("value") or payload.get("turnover"))
        trades = _number(payload.get("trades"))
        if (
            not contract_id
            or not interval
            or time_ms < 0
            or open_price is None
            or high is None
            or low is None
            or close is None
            or volume is None
            or value is None
            or high < low
            or min(open_price, high, low, close, volume, value) < 0
        ):
            return None
        return cls(
            contract_id=contract_id,
            contract_name=contract_name,
            interval=interval,
            time_ms=time_ms,
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=volume,
            value=value,
            trades=trades,
        )


@dataclass(frozen=True)
class RiskPlan:
    equity: float
    available_balance: float | None
    risk_fraction: float
    risk_budget: float
    entry_price: float
    stop_loss: float
    size: float
    theoretical_size: float
    notional: float
    max_loss: float
    actual_risk_fraction: float
    tp_1r: float
    tp_target: float
    profit_1r: float
    profit_target: float
    tp_r_multiple: float
    leverage: float | None
    margin_capped: bool
    size_capped: bool


@dataclass(frozen=True)
class Signal:
    contract: Contract
    interval: str
    direction: str
    candle: Candle
    breakout_level: float
    breakout_pct: float
    volume_average: float
    volume_ratio: float
    volume_lookback: int
    strategy_name: str | None = None
    monitor_interval: str | None = None
    ema_fast: float | None = None
    ema_slow: float | None = None
    atr_entry: float | None = None
    atr_monitor: float | None = None
    raw_target: float | None = None
    stop_loss_override: float | None = None
    take_profit_override: float | None = None
    rr: float | None = None
    breakout_time_ms: int | None = None
    split_targets: tuple[tuple[float, float], ...] = ()

    @property
    def key(self) -> str:
        return f"{self.contract.contract_id}:{self.interval}:{self.candle.time_ms}:{self.direction}"


class EdgeXClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _get_json_sync(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        url = f"{self.settings.api_base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "edgex-breakout-alert/1.0"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
        except urllib.error.URLError as exc:
            raise RuntimeError(f"EdgeX HTTP request failed: {exc}") from exc
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("EdgeX returned invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("code") != "SUCCESS":
            raise RuntimeError(f"EdgeX API error: {payload.get('msg') if isinstance(payload, dict) else payload}")
        return payload

    async def get_contracts(self) -> dict[str, Contract]:
        payload = await asyncio.to_thread(
            self._get_json_sync, "/api/v2/public/meta/getMetaData", None
        )
        data = payload.get("data") or {}
        contracts_payload = data.get("contractList") or []
        coins = {
            str(item.get("coinId")): str(item.get("coinName") or item.get("coinId"))
            for item in (data.get("coinList") or [])
            if isinstance(item, dict)
        }
        contracts: dict[str, Contract] = {}
        for item in contracts_payload:
            if not isinstance(item, dict):
                continue
            contract_id = str(item.get("contractId", "")).strip()
            if not contract_id or not _boolish(item.get("enableTrade")):
                continue
            if not self.settings.include_hidden and not _boolish(item.get("enableDisplay"), True):
                continue
            quote_coin_id = str(item.get("quoteCoinId", "")).strip()
            contracts[contract_id] = Contract(
                contract_id=contract_id,
                contract_name=str(item.get("contractName") or contract_id),
                quote_coin=coins.get(quote_coin_id, quote_coin_id or "USDC"),
                enable_trade=True,
                enable_display=_boolish(item.get("enableDisplay"), True),
                step_size=_number(item.get("stepSize")),
                min_order_size=_number(item.get("minOrderSize")),
                max_order_size=_number(item.get("maxOrderSize")),
                max_long_leverage=_number(item.get("maxLongLeverage")),
                max_short_leverage=_number(item.get("maxShortLeverage")),
            )
        if not contracts:
            raise RuntimeError("EdgeX metadata returned no tradable contracts")
        return contracts

    def _get_private_json_sync(
        self, path: str, params: dict[str, str]
    ) -> dict[str, Any]:
        if not self.settings.account_risk_enabled:
            raise RuntimeError("EdgeX private API credentials are not configured")

        sorted_pairs = sorted(params.items())
        body_str = "&".join(f"{key}={value}" for key, value in sorted_pairs)
        query = urllib.parse.urlencode(sorted_pairs)
        timestamp = str(int(time.time() * 1000))
        signature = _edgex_hmac_signature(
            self.settings.api_secret or "",
            timestamp,
            "GET",
            path,
            body_str,
        )
        request = urllib.request.Request(
            f"{self.settings.api_base_url}{path}?{query}",
            headers={
                "Accept": "application/json",
                "User-Agent": "edgex-breakout-alert/1.0",
                "X-edgeX-Api-Key": self.settings.api_key or "",
                "X-edgeX-Passphrase": self.settings.api_passphrase or "",
                "X-edgeX-Signature": signature,
                "X-edgeX-Timestamp": timestamp,
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
        except urllib.error.URLError as exc:
            raise RuntimeError(f"EdgeX private API request failed: {exc}") from exc
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("EdgeX private API returned invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("code") != "SUCCESS":
            message = payload.get("msg") if isinstance(payload, dict) else payload
            raise RuntimeError(f"EdgeX private API error: {message}")
        return payload

    async def get_account_asset(self) -> dict[str, Any]:
        if not self.settings.account_id:
            raise RuntimeError("EDGEX_ACCOUNT_ID is not configured")
        return await asyncio.to_thread(
            self._get_private_json_sync,
            "/api/v2/private/account/getAccountAsset",
            {"accountId": self.settings.account_id},
        )


def _manual_account_asset(
    settings: Settings,
    equity_usdc: float | None = None,
) -> dict[str, Any]:
    equity = equity_usdc if equity_usdc is not None else settings.manual_equity_usdc
    if equity is None or equity <= 0:
        raise RuntimeError("EdgeX equity is not configured")
    model: dict[str, Any] = {
        "coinId": settings.collateral_coin_id,
        "totalEquity": str(equity),
    }
    if settings.manual_available_balance_usdc is not None:
        model["availableBalance"] = str(settings.manual_available_balance_usdc)

    account: dict[str, Any] = {}
    if settings.manual_leverage is not None:
        account["defaultTradeSetting"] = {"leverage": str(settings.manual_leverage)}

    return {
        "code": "SUCCESS",
        "data": {
            "account": account,
            "collateralAssetModelList": [model],
        },
    }


def _account_asset_metrics(
    payload: dict[str, Any],
    collateral_coin_id: str,
) -> tuple[float, float | None, dict[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("EdgeX account asset response has no data object")

    models = data.get("collateralAssetModelList") or []
    if not isinstance(models, list):
        models = []
    chosen: dict[str, Any] | None = None
    for item in models:
        if isinstance(item, dict) and str(item.get("coinId", "")) == collateral_coin_id:
            chosen = item
            break
    if chosen is None:
        chosen = next(
            (
                item
                for item in models
                if isinstance(item, dict) and _number(item.get("totalEquity")) is not None
            ),
            None,
        )
    if chosen is None:
        raise RuntimeError("EdgeX account asset response has no collateral equity model")

    equity = _number(chosen.get("totalEquity"))
    available = _number(chosen.get("availableBalance"))
    if equity is None or equity <= 0:
        raise RuntimeError("EdgeX totalEquity is missing or not positive")
    account = data.get("account") if isinstance(data.get("account"), dict) else {}
    return equity, available, account


def _account_leverage(
    account: dict[str, Any],
    contract: Contract,
    direction: str,
) -> float | None:
    per_contract = account.get("contractIdToTradeSetting")
    setting: dict[str, Any] | None = None
    if isinstance(per_contract, dict):
        candidate = per_contract.get(contract.contract_id)
        if isinstance(candidate, dict):
            setting = candidate
    if setting is None and isinstance(account.get("defaultTradeSetting"), dict):
        setting = account["defaultTradeSetting"]

    leverage = _number(setting.get("leverage")) if setting else None
    market_max = (
        contract.max_long_leverage if direction == "up" else contract.max_short_leverage
    )
    if leverage is not None and leverage <= 0:
        leverage = None
    if leverage is not None and market_max is not None and market_max > 0:
        leverage = min(leverage, market_max)
    return leverage


def build_risk_plan(
    signal: Signal,
    account_asset: dict[str, Any],
    settings: Settings,
) -> RiskPlan:
    equity, available_balance, account = _account_asset_metrics(
        account_asset, settings.collateral_coin_id
    )
    entry = signal.candle.close
    if signal.stop_loss_override is not None:
        stop = signal.stop_loss_override
    elif settings.stop_method == "breakout_level":
        stop = signal.breakout_level
    else:
        stop = signal.candle.low if signal.direction == "up" else signal.candle.high

    if signal.direction == "up" and stop >= entry:
        raise RuntimeError("Long stop-loss must be below entry")
    if signal.direction == "down" and stop <= entry:
        raise RuntimeError("Short stop-loss must be above entry")

    stop_distance = abs(entry - stop)
    if entry <= 0 or stop_distance <= 0:
        raise RuntimeError("Signal has no usable stop distance for risk sizing")

    risk_budget = equity * settings.risk_per_trade
    theoretical_size = risk_budget / stop_distance
    size = theoretical_size
    margin_capped = False
    size_capped = False

    leverage = _account_leverage(account, signal.contract, signal.direction)
    if available_balance is not None and available_balance > 0 and leverage is not None:
        margin_size = available_balance * leverage / entry
        if margin_size < size:
            size = margin_size
            margin_capped = True

    if signal.contract.max_order_size is not None and signal.contract.max_order_size > 0:
        if signal.contract.max_order_size < size:
            size = signal.contract.max_order_size
            size_capped = True

    size = _floor_to_step(size, signal.contract.step_size)
    if size <= 0:
        raise RuntimeError("Calculated EdgeX order size rounded to zero")
    if (
        signal.contract.min_order_size is not None
        and signal.contract.min_order_size > 0
        and size < signal.contract.min_order_size
    ):
        raise RuntimeError(
            f"Calculated size {size} is below EdgeX minimum {signal.contract.min_order_size}"
        )

    notional = size * entry
    max_loss = size * stop_distance
    actual_risk_fraction = max_loss / equity
    direction_sign = 1.0 if signal.direction == "up" else -1.0
    tp_1r = entry + direction_sign * stop_distance
    if signal.take_profit_override is not None:
        tp_target = signal.take_profit_override
        if signal.direction == "up" and tp_target <= entry:
            raise RuntimeError("Long take-profit must be above entry")
        if signal.direction == "down" and tp_target >= entry:
            raise RuntimeError("Short take-profit must be below entry")
        target_r_multiple = abs(tp_target - entry) / stop_distance
    else:
        target_r_multiple = settings.tp_r_multiple
        tp_target = entry + direction_sign * stop_distance * target_r_multiple

    return RiskPlan(
        equity=equity,
        available_balance=available_balance,
        risk_fraction=settings.risk_per_trade,
        risk_budget=risk_budget,
        entry_price=entry,
        stop_loss=stop,
        size=size,
        theoretical_size=theoretical_size,
        notional=notional,
        max_loss=max_loss,
        actual_risk_fraction=actual_risk_fraction,
        tp_1r=tp_1r,
        tp_target=tp_target,
        profit_1r=max_loss,
        profit_target=max_loss * target_r_multiple,
        tp_r_multiple=target_r_multiple,
        leverage=leverage,
        margin_capped=margin_capped,
        size_capped=size_capped,
    )


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS processed_candles (
                contract_id TEXT NOT NULL,
                interval TEXT NOT NULL,
                candle_time_ms INTEGER NOT NULL,
                PRIMARY KEY (contract_id, interval)
            );
            CREATE TABLE IF NOT EXISTS alerts (
                signal_key TEXT PRIMARY KEY,
                contract_id TEXT NOT NULL,
                contract_name TEXT NOT NULL,
                interval TEXT NOT NULL,
                direction TEXT NOT NULL,
                candle_time_ms INTEGER NOT NULL,
                sent_at_ms INTEGER NOT NULL,
                breakout_level REAL NOT NULL,
                close_price REAL NOT NULL,
                volume_value REAL NOT NULL,
                volume_ratio REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runtime_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def last_processed(self, contract_id: str, interval: str) -> int | None:
        row = self.connection.execute(
            "SELECT candle_time_ms FROM processed_candles WHERE contract_id=? AND interval=?",
            (contract_id, interval),
        ).fetchone()
        return int(row[0]) if row else None

    def mark_processed(self, contract_id: str, interval: str, candle_time_ms: int) -> None:
        self.connection.execute(
            """
            INSERT INTO processed_candles(contract_id, interval, candle_time_ms)
            VALUES (?, ?, ?)
            ON CONFLICT(contract_id, interval) DO UPDATE SET candle_time_ms=excluded.candle_time_ms
            """,
            (contract_id, interval, candle_time_ms),
        )
        self.connection.commit()

    def alert_exists(self, signal_key: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM alerts WHERE signal_key=? LIMIT 1", (signal_key,)
        ).fetchone()
        return row is not None

    def latest_alert_time(self, contract_id: str, interval: str, direction: str) -> int | None:
        row = self.connection.execute(
            """
            SELECT sent_at_ms FROM alerts
            WHERE contract_id=? AND interval=? AND direction=?
            ORDER BY sent_at_ms DESC LIMIT 1
            """,
            (contract_id, interval, direction),
        ).fetchone()
        return int(row[0]) if row else None

    def save_alert(self, signal: Signal, sent_at_ms: int) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO alerts(
                signal_key, contract_id, contract_name, interval, direction,
                candle_time_ms, sent_at_ms, breakout_level, close_price,
                volume_value, volume_ratio
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal.key,
                signal.contract.contract_id,
                signal.contract.contract_name,
                signal.interval,
                signal.direction,
                signal.candle.time_ms,
                sent_at_ms,
                signal.breakout_level,
                signal.candle.close,
                signal.candle.value,
                signal.volume_ratio,
            ),
        )
        self.connection.commit()

    def get_runtime_equity(self) -> float | None:
        row = self.connection.execute(
            "SELECT value FROM runtime_state WHERE key='equity_usdc'"
        ).fetchone()
        return _number(row[0]) if row else None

    def set_runtime_equity(self, equity: float) -> None:
        self.connection.execute(
            """
            INSERT INTO runtime_state(key, value) VALUES ('equity_usdc', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (str(equity),),
        )
        self.connection.commit()

    def get_telegram_update_offset(self) -> int | None:
        row = self.connection.execute(
            "SELECT value FROM runtime_state WHERE key='telegram_update_offset'"
        ).fetchone()
        try:
            return int(row[0]) if row else None
        except (TypeError, ValueError):
            return None

    def set_telegram_update_offset(self, offset: int) -> None:
        self.connection.execute(
            """
            INSERT INTO runtime_state(key, value) VALUES ('telegram_update_offset', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (str(int(offset)),),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


class JsonStateStore:
    """Small portable state store for ephemeral scheduled runners.

    GitHub-hosted runners are recreated for every scheduled job, so a JSON
    file can be committed back to the repository after a run. The file stores
    only deduplication state and never contains Telegram credentials.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.state: dict[str, Any] = {
            "processed_candles": {},
            "alerts": {},
            "runtime": {},
        }
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Invalid state file: {self.path}") from exc
        if not isinstance(loaded, dict):
            raise RuntimeError(f"Invalid state file: {self.path}")
        for key in ("processed_candles", "alerts", "runtime"):
            if isinstance(loaded.get(key), dict):
                self.state[key] = loaded[key]

    @staticmethod
    def _processed_key(contract_id: str, interval: str) -> str:
        return f"{contract_id}:{interval}"

    def _write(self) -> None:
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def last_processed(self, contract_id: str, interval: str) -> int | None:
        value = self.state["processed_candles"].get(self._processed_key(contract_id, interval))
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def mark_processed(self, contract_id: str, interval: str, candle_time_ms: int) -> None:
        self.state["processed_candles"][self._processed_key(contract_id, interval)] = int(candle_time_ms)
        self._write()

    def alert_exists(self, signal_key: str) -> bool:
        return signal_key in self.state["alerts"]

    def latest_alert_time(self, contract_id: str, interval: str, direction: str) -> int | None:
        latest: int | None = None
        for record in self.state["alerts"].values():
            if not isinstance(record, dict):
                continue
            if (
                str(record.get("contract_id")) != contract_id
                or str(record.get("interval")) != interval
                or str(record.get("direction")) != direction
            ):
                continue
            try:
                sent_at_ms = int(record["sent_at_ms"])
            except (KeyError, TypeError, ValueError):
                continue
            latest = sent_at_ms if latest is None else max(latest, sent_at_ms)
        return latest

    def save_alert(self, signal: Signal, sent_at_ms: int) -> None:
        if self.alert_exists(signal.key):
            return
        self.state["alerts"][signal.key] = {
            "contract_id": signal.contract.contract_id,
            "contract_name": signal.contract.contract_name,
            "interval": signal.interval,
            "direction": signal.direction,
            "candle_time_ms": signal.candle.time_ms,
            "sent_at_ms": int(sent_at_ms),
        }
        self._write()

    def get_runtime_equity(self) -> float | None:
        return _number(self.state["runtime"].get("equity_usdc"))

    def set_runtime_equity(self, equity: float) -> None:
        self.state["runtime"]["equity_usdc"] = float(equity)
        self._write()

    def get_telegram_update_offset(self) -> int | None:
        value = self.state["runtime"].get("telegram_update_offset")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def set_telegram_update_offset(self, offset: int) -> None:
        self.state["runtime"]["telegram_update_offset"] = int(offset)
        self._write()

    def close(self) -> None:
        return


class TelegramNotifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool(self.settings.telegram_token and self.settings.telegram_chat_id)

    @property
    def enabled(self) -> bool:
        return bool(self.configured and not self.settings.dry_run)

    def _api_sync(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.telegram_token:
            raise RuntimeError("Telegram bot token is missing")
        url = f"https://api.telegram.org/bot{self.settings.telegram_token}/{method}"
        body = urllib.parse.urlencode(params).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Telegram request failed: {exc}") from exc
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise RuntimeError(
                f"Telegram API error: {payload.get('description') if isinstance(payload, dict) else payload}"
            )
        return payload

    def _send_sync(self, text: str) -> None:
        if not self.settings.telegram_chat_id:
            raise RuntimeError("Telegram chat ID is missing")
        self._api_sync(
            "sendMessage",
            {"chat_id": self.settings.telegram_chat_id, "text": text},
        )

    def _get_updates_sync(self, offset: int | None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeout": 0,
            "limit": 100,
            "allowed_updates": json.dumps(["message", "channel_post"]),
        }
        if offset is not None:
            params["offset"] = offset
        payload = self._api_sync("getUpdates", params)
        result = payload.get("result") or []
        return [item for item in result if isinstance(item, dict)]

    async def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        return await asyncio.to_thread(self._get_updates_sync, offset)

    async def send(self, text: str) -> None:
        if not self.enabled:
            LOGGER.info("DRY RUN notification:\n%s", text)
            return
        await asyncio.to_thread(self._send_sync, text)


def format_signal(
    signal: Signal,
    timezone_name: str,
    risk_plan: RiskPlan | None = None,
    risk_note: str | None = None,
) -> str:
    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        tz = timezone.utc
    timestamp = datetime.fromtimestamp(signal.candle.time_ms / 1000, tz=timezone.utc).astimezone(tz)

    if signal.strategy_name:
        long_side = signal.direction == "up"
        setup_label = "押し目買い / LONG" if long_side else "戻り売り / SHORT"
        trend_label = "上昇トレンド" if long_side else "下降トレンド"
        lines = [
            "🎯 EdgeX 4H→15M ロールリバーサル条件成立",
            f"銘柄: {signal.contract.contract_name}",
            f"方向: {setup_label}",
            f"監視足: {_interval_label(signal.monitor_interval or 'HOUR_4')} / エントリー足: {_interval_label(signal.interval)}",
            f"確定時刻: {timestamp:%Y-%m-%d %H:%M:%S} {timezone_name}",
            f"4Hトレンド: {trend_label}",
            f"EMA: {_format_number(signal.ema_fast)} / {_format_number(signal.ema_slow)}",
            f"ロールリバーサル水準: {_format_number(signal.breakout_level)}",
            f"15M ATR: {_format_number(signal.atr_entry)} / 4H ATR: {_format_number(signal.atr_monitor)}",
        ]
        if risk_plan is not None:
            lines.extend(
                [
                    "",
                    "📐 エントリー計画",
                    f"EdgeX Equity: {_format_usd(risk_plan.equity)}",
                    f"最大リスク: {_format_usd(risk_plan.risk_budget)} ({risk_plan.risk_fraction * 100:.1f}%)",
                    f"Entry目安: {_format_number(risk_plan.entry_price)}",
                    f"SL: {_format_number(risk_plan.stop_loss)}",
                    f"TP: {_format_number(risk_plan.tp_target)}",
                    f"RR: 1:{risk_plan.tp_r_multiple:.2f}",
                    f"枚数: {_format_number(risk_plan.size)}",
                    f"想定Notional: {_format_usd(risk_plan.notional)}",
                    f"SL損失: -{_format_usd(risk_plan.max_loss)} ({risk_plan.actual_risk_fraction * 100:.2f}%)",
                ]
            )
            if len(signal.split_targets) >= 2:
                lines.extend(
                    [
                        "利確方式: 2分割（50% / 50%）",
                        f"TP1 50%: {_format_number(signal.split_targets[0][1])} (2R)",
                        f"TP2 50%: {_format_number(signal.split_targets[-1][1])} (4Hターゲット)",
                    ]
                )
            else:
                lines.append("利確方式: 分割なし（全量を4Hターゲットで利確）")
            if risk_plan.margin_capped:
                leverage_text = f"{risk_plan.leverage:g}x" if risk_plan.leverage is not None else "現在設定"
                lines.append(f"証拠金上限で枚数縮小: {leverage_text}")
            if risk_plan.size_capped:
                lines.append("EdgeX最大注文枚数で枚数縮小")
        elif risk_note:
            lines.extend(["", f"📐 リスク計算: {risk_note}"])
        lines.extend(["通知のみ（自動発注なし）", "https://pro.edgex.exchange/"])
        return "\n".join(lines)

    direction_label = "上抜け" if signal.direction == "up" else "下抜け"
    sign = "+" if signal.breakout_pct >= 0 else ""
    lines = [
        "🚨 EdgeX 出来高ブレイクアウト",
        f"銘柄: {signal.contract.contract_name}",
        f"方向: {direction_label}",
        f"時間足: {_interval_label(signal.interval)}足",
        f"確定時刻: {timestamp:%Y-%m-%d %H:%M:%S} {timezone_name}",
        f"終値: {_format_number(signal.candle.close)} {signal.contract.quote_coin}",
        f"突破基準: {_format_number(signal.breakout_level)}",
        f"突破幅: {sign}{signal.breakout_pct:.2f}%",
        f"出来高: {_format_usd(signal.candle.value)}",
        f"平均比: {signal.volume_ratio:.2f}倍（過去{signal.volume_lookback}本平均）",
    ]
    if risk_plan is not None:
        lines.extend(
            [
                "",
                "📐 5%リスク エントリー指示",
                f"EdgeX Equity: {_format_usd(risk_plan.equity)}",
                f"リスク予算: {_format_usd(risk_plan.risk_budget)} ({risk_plan.risk_fraction * 100:.1f}%)",
                f"Entry目安: {_format_number(risk_plan.entry_price)}",
                f"SL: {_format_number(risk_plan.stop_loss)}",
                f"枚数: {_format_number(risk_plan.size)}",
                f"想定Notional: {_format_usd(risk_plan.notional)}",
                f"SL損失: -{_format_usd(risk_plan.max_loss)} ({risk_plan.actual_risk_fraction * 100:.2f}%)",
                f"1R: {_format_number(risk_plan.tp_1r)} / +{_format_usd(risk_plan.profit_1r)}",
                f"{risk_plan.tp_r_multiple:g}R: {_format_number(risk_plan.tp_target)} / +{_format_usd(risk_plan.profit_target)}",
            ]
        )
    elif risk_note:
        lines.extend(["", f"📐 5%リスク指示: {risk_note}"])
    lines.extend(["通知のみ（自動発注なし）", "https://pro.edgex.exchange/"])
    return "\n".join(lines)


def parse_equity_command(text: str) -> tuple[str, float | None] | None:
    raw = text.strip()
    if not raw:
        return None
    parts = raw.split()
    command = parts[0].lower().split("@", 1)[0]
    if command not in {"/equity", "/balance", "残高"}:
        return None
    if len(parts) == 1:
        return ("show", None)
    if len(parts) != 2:
        return ("invalid", None)
    value = _number(parts[1].replace(",", ""))
    if value is None or value <= 0 or value > 100_000_000:
        return ("invalid", None)
    return ("set", value)


class BreakoutDetector:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def detect(
        self,
        contract: Contract,
        interval: str,
        candles: Iterable[Candle],
        candidate: Candle,
    ) -> Signal | None:
        ordered = sorted((c for c in candles if c.time_ms <= candidate.time_ms), key=lambda c: c.time_ms)
        candidate_index = next((i for i, c in enumerate(ordered) if c.time_ms == candidate.time_ms), None)
        if candidate_index is None:
            return None
        previous = ordered[:candidate_index]
        required = max(self.settings.breakout_lookback, self.settings.volume_lookback)
        if len(previous) < required:
            return None

        breakout_window = previous[-self.settings.breakout_lookback :]
        volume_window = previous[-self.settings.volume_lookback :]
        upper = max(c.high for c in breakout_window)
        lower = min(c.low for c in breakout_window)
        average_value = mean(c.value for c in volume_window)
        if average_value <= 0 or candidate.value < self.settings.min_volume_value:
            return None
        volume_ratio = candidate.value / average_value
        if volume_ratio < self.settings.volume_multiplier:
            return None

        up_pct = (candidate.close / upper - 1) * 100 if upper else 0
        down_pct = (1 - candidate.close / lower) * 100 if lower else 0
        if up_pct >= self.settings.min_breakout_pct:
            return Signal(
                contract=contract,
                interval=interval,
                direction="up",
                candle=candidate,
                breakout_level=upper,
                breakout_pct=up_pct,
                volume_average=average_value,
                volume_ratio=volume_ratio,
                volume_lookback=self.settings.volume_lookback,
            )
        if down_pct >= self.settings.min_breakout_pct:
            return Signal(
                contract=contract,
                interval=interval,
                direction="down",
                candle=candidate,
                breakout_level=lower,
                breakout_pct=-down_pct,
                volume_average=average_value,
                volume_ratio=volume_ratio,
                volume_lookback=self.settings.volume_lookback,
            )
        return None


class RollReversalDetector:
    """4H trend + role reversal, confirmed on the 15M entry candle."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _recent_breakout(
        self,
        candles: list[Candle],
        direction: str,
    ) -> tuple[int, float] | None:
        lookback = self.settings.roll_lookback
        if len(candles) < lookback + 2:
            return None
        start = max(lookback, len(candles) - self.settings.roll_max_age)
        for index in range(len(candles) - 1, start - 1, -1):
            previous = candles[index - lookback : index]
            if len(previous) < lookback:
                continue
            if direction == "up":
                level = max(candle.high for candle in previous)
                if candles[index].close > level:
                    return index, level
            else:
                level = min(candle.low for candle in previous)
                if candles[index].close < level:
                    return index, level
        return None

    def detect(
        self,
        contract: Contract,
        monitor_candles: Iterable[Candle],
        entry_candles: Iterable[Candle],
        candidate: Candle,
    ) -> Signal | None:
        monitor = sorted(monitor_candles, key=lambda item: item.time_ms)
        entries = sorted((c for c in entry_candles if c.time_ms <= candidate.time_ms), key=lambda item: item.time_ms)
        if not monitor or not entries or entries[-1].time_ms != candidate.time_ms:
            return None
        required_monitor = max(
            self.settings.trend_slow_ema,
            self.settings.roll_lookback + self.settings.roll_max_age,
            self.settings.atr_period + 1,
        )
        required_entry = max(
            self.settings.atr_period + 1,
            self.settings.retest_lookback,
            self.settings.pullback_swing_lookback,
        )
        if len(monitor) < required_monitor or len(entries) < required_entry:
            return None

        closes = [candle.close for candle in monitor]
        ema_fast = _ema(closes, self.settings.trend_fast_ema)
        ema_slow = _ema(closes, self.settings.trend_slow_ema)
        atr_monitor = _atr(monitor, self.settings.atr_period)
        atr_entry = _atr(entries, self.settings.atr_period)
        if None in {ema_fast, ema_slow, atr_monitor, atr_entry}:
            return None
        assert ema_fast is not None and ema_slow is not None
        assert atr_monitor is not None and atr_entry is not None
        if atr_monitor <= 0 or atr_entry <= 0:
            return None

        latest_monitor = monitor[-1]
        if ema_fast > ema_slow and latest_monitor.close > ema_fast:
            direction = "up"
        elif ema_fast < ema_slow and latest_monitor.close < ema_fast:
            direction = "down"
        else:
            return None

        breakout = self._recent_breakout(monitor, direction)
        if breakout is None:
            return None
        breakout_index, roll_level = breakout
        tolerance = max(atr_entry * self.settings.retest_atr_tolerance, roll_level * 0.001)
        retest_window = entries[-self.settings.retest_lookback :]
        if direction == "up":
            touched = any(candle.low <= roll_level + tolerance for candle in retest_window)
            confirmed = candidate.close > candidate.open and candidate.close > roll_level
        else:
            touched = any(candle.high >= roll_level - tolerance for candle in retest_window)
            confirmed = candidate.close < candidate.open and candidate.close < roll_level
        if not touched or not confirmed:
            return None

        swing_window = entries[-self.settings.pullback_swing_lookback :]
        if direction == "up":
            structural_stop = min(min(candle.low for candle in swing_window), roll_level)
            stop_loss = structural_stop - atr_entry * self.settings.atr_stop_buffer
            raw_target = max(candle.high for candle in monitor[breakout_index:])
            take_profit = raw_target - atr_monitor * self.settings.atr_target_buffer
            if stop_loss >= candidate.close or take_profit <= candidate.close:
                return None
            rr = (take_profit - candidate.close) / (candidate.close - stop_loss)
        else:
            structural_stop = max(max(candle.high for candle in swing_window), roll_level)
            stop_loss = structural_stop + atr_entry * self.settings.atr_stop_buffer
            raw_target = min(candle.low for candle in monitor[breakout_index:])
            take_profit = raw_target + atr_monitor * self.settings.atr_target_buffer
            if stop_loss <= candidate.close or take_profit >= candidate.close:
                return None
            rr = (candidate.close - take_profit) / (stop_loss - candidate.close)
        if rr < self.settings.min_rr:
            return None

        stop_distance = abs(candidate.close - stop_loss)
        if rr >= self.settings.split_rr:
            direction_sign = 1.0 if direction == "up" else -1.0
            split_targets = (
                (0.5, candidate.close + direction_sign * stop_distance * 2.0),
                (0.5, take_profit),
            )
        else:
            split_targets = ((1.0, take_profit),)

        volume_window = entries[-min(len(entries), self.settings.volume_lookback) :]
        average_value = mean(candle.value for candle in volume_window) if volume_window else candidate.value
        volume_ratio = candidate.value / average_value if average_value > 0 else 1.0
        breakout_pct = (
            (candidate.close / roll_level - 1.0) * 100.0
            if direction == "up"
            else -(1.0 - candidate.close / roll_level) * 100.0
        )
        return Signal(
            contract=contract,
            interval=self.settings.entry_interval,
            direction=direction,
            candle=candidate,
            breakout_level=roll_level,
            breakout_pct=breakout_pct,
            volume_average=average_value,
            volume_ratio=volume_ratio,
            volume_lookback=len(volume_window),
            strategy_name="4H trend + roll reversal",
            monitor_interval=self.settings.monitor_interval,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            atr_entry=atr_entry,
            atr_monitor=atr_monitor,
            raw_target=raw_target,
            stop_loss_override=stop_loss,
            take_profit_override=take_profit,
            rr=rr,
            breakout_time_ms=monitor[breakout_index].time_ms,
            split_targets=split_targets,
        )


class BreakoutService:
    def __init__(
        self,
        settings: Settings,
        *,
        run_once: bool = False,
        once_timeout_seconds: float = 75.0,
    ) -> None:
        self.settings = settings
        self.client = EdgeXClient(settings)
        self.store = (
            JsonStateStore(settings.state_file)
            if settings.state_backend == "json"
            else StateStore(settings.database_path)
        )
        self.notifier = TelegramNotifier(settings)
        self.detector = RollReversalDetector(settings)
        self.histories: dict[tuple[str, str], dict[int, Candle]] = {}
        self.initialized: set[tuple[str, str]] = set()
        self.contracts: dict[str, Contract] = {}
        self.stop_event = asyncio.Event()
        self.run_once = run_once
        self.once_timeout_seconds = max(15.0, once_timeout_seconds)
        self.expected_snapshot_keys: set[tuple[str, str]] = set()
        self.snapshot_seen: set[tuple[str, str]] = set()
        self._account_asset_loaded = False
        self._account_asset: dict[str, Any] | None = None
        self._account_asset_error: str | None = None

    async def close(self) -> None:
        self.store.close()

    def request_stop(self) -> None:
        self.stop_event.set()

    def _add_candles(self, key: tuple[str, str], candles: Iterable[Candle]) -> None:
        history = self.histories.setdefault(key, {})
        for candle in candles:
            history[candle.time_ms] = candle
        if len(history) > self.settings.history_size:
            for timestamp in sorted(history)[: len(history) - self.settings.history_size]:
                del history[timestamp]

    def _closed_candles(self, key: tuple[str, str], interval: str) -> list[Candle]:
        cutoff = int(time.time() * 1000)
        interval_ms = INTERVAL_MS[interval]
        return [
            candle
            for candle in sorted(self.histories.get(key, {}).values(), key=lambda item: item.time_ms)
            if candle.time_ms + interval_ms <= cutoff
        ]

    async def _bootstrap(self, key: tuple[str, str], interval: str) -> None:
        if key in self.initialized:
            return
        self.initialized.add(key)
        closed = self._closed_candles(key, interval)
        contract_id = key[0]
        latest = closed[-1] if closed else None
        if latest is not None and self.store.last_processed(contract_id, interval) is None:
            self.store.mark_processed(contract_id, interval, latest.time_ms)
        LOGGER.debug(
            "Seeded %s %s with %d candles%s",
            contract_id,
            interval,
            len(self.histories.get(key, {})),
            " (no initial alert)" if latest else "",
        )

    def _detect_strategy(self, contract_id: str, candidate: Candle | None = None) -> Signal | None:
        contract = self.contracts.get(contract_id)
        if contract is None:
            return None
        monitor_key = (contract_id, self.settings.monitor_interval)
        entry_key = (contract_id, self.settings.entry_interval)
        monitor = self._closed_candles(monitor_key, self.settings.monitor_interval)
        entries = self._closed_candles(entry_key, self.settings.entry_interval)
        if not monitor or not entries:
            return None
        selected = candidate or entries[-1]
        return self.detector.detect(contract, monitor, entries, selected)

    async def _process_updates(self, key: tuple[str, str], interval: str) -> None:
        if key not in self.initialized:
            await self._bootstrap(key, interval)
            return
        closed = self._closed_candles(key, interval)
        if not closed:
            return
        contract_id = key[0]
        last_processed = self.store.last_processed(contract_id, interval)
        if last_processed is None:
            self.store.mark_processed(contract_id, interval, closed[-1].time_ms)
            return
        new_closed = [candle for candle in closed if candle.time_ms > last_processed]
        if not new_closed:
            return

        # Entry decisions are made only when a new 15-minute candle closes.
        candidate = new_closed[-1]
        if interval == self.settings.entry_interval:
            signal = self._detect_strategy(contract_id, candidate)
            if signal is not None:
                await self._send_signal(signal)
        self.store.mark_processed(contract_id, interval, candidate.time_ms)

    async def _process_snapshot_once(self, key: tuple[str, str], interval: str) -> None:
        """Evaluate the latest 15M entry after both 4H and 15M snapshots exist."""
        if interval not in {self.settings.monitor_interval, self.settings.entry_interval}:
            return
        signal = self._detect_strategy(key[0])
        if signal is not None:
            await self._send_signal(signal)

    def _current_equity(self) -> float | None:
        runtime_equity = self.store.get_runtime_equity()
        if runtime_equity is not None and runtime_equity > 0:
            return runtime_equity
        if self.settings.manual_risk_enabled:
            return self.settings.manual_equity_usdc
        return None

    async def _sync_telegram_equity_commands(self) -> None:
        if not self.notifier.enabled or not self.settings.telegram_chat_id:
            return
        try:
            updates = await self.notifier.get_updates(self.store.get_telegram_update_offset())
        except Exception as exc:
            LOGGER.warning("Telegram equity command sync failed: %s", exc)
            return
        if not updates:
            LOGGER.info("Telegram command sync: 0 update(s)")
            return

        LOGGER.info("Telegram command sync: %d update(s)", len(updates))
        next_offset = self.store.get_telegram_update_offset()
        authorized_chat_id = str(self.settings.telegram_chat_id)
        for update in updates:
            try:
                update_id = int(update.get("update_id"))
            except (TypeError, ValueError):
                continue
            next_offset = max(next_offset or 0, update_id + 1)
            message = update.get("message")
            if not isinstance(message, dict):
                message = update.get("channel_post")
            if not isinstance(message, dict):
                continue
            chat = message.get("chat")
            if not isinstance(chat, dict) or str(chat.get("id")) != authorized_chat_id:
                continue
            text = message.get("text")
            if not isinstance(text, str):
                continue
            command = parse_equity_command(text)
            if command is None:
                continue

            action, value = command
            if action == "set" and value is not None:
                self.store.set_runtime_equity(value)
                self._account_asset_loaded = False
                self._account_asset = None
                self._account_asset_error = None
                risk_amount = value * self.settings.risk_per_trade
                await self.notifier.send(
                    "✅ EdgeX残高を更新しました\n"
                    f"Equity: {_format_usd(value)}\n"
                    f"1トレード最大リスク: {_format_usd(risk_amount)} "
                    f"({self.settings.risk_per_trade * 100:.1f}%)"
                )
            elif action == "show":
                current = self._current_equity()
                if current is None:
                    reply = "現在のEdgeX残高は未設定です。 /equity 24 の形式で送ってください。"
                else:
                    reply = (
                        f"現在のEdgeX Equity: {_format_usd(current)}\n"
                        f"5%リスク: {_format_usd(current * self.settings.risk_per_trade)}"
                    )
                await self.notifier.send(reply)
            else:
                await self.notifier.send(
                    "形式: /equity 24.50\n"
                    "確認だけなら /equity"
                )

        if next_offset is not None:
            self.store.set_telegram_update_offset(next_offset)

    async def _get_account_asset_for_risk(self) -> dict[str, Any] | None:
        if self._account_asset_loaded:
            return self._account_asset
        self._account_asset_loaded = True

        current_equity = self._current_equity()
        if current_equity is not None:
            self._account_asset = _manual_account_asset(
                self.settings,
                equity_usdc=current_equity,
            )
            return self._account_asset

        if not self.settings.account_risk_enabled:
            self._account_asset_error = "EdgeX口座資産未設定（EDGEX_EQUITY_USDC）"
            return None
        try:
            self._account_asset = await self.client.get_account_asset()
        except Exception as exc:
            self._account_asset_error = "EdgeX口座資産を取得できませんでした"
            LOGGER.warning("EdgeX account asset lookup failed: %s", exc)
        return self._account_asset

    async def _send_signal(self, signal: Signal) -> None:
        if self.store.alert_exists(signal.key):
            return
        if self.settings.alert_cooldown_minutes:
            last_alert = self.store.latest_alert_time(
                signal.contract.contract_id, signal.interval, signal.direction
            )
            if last_alert is not None:
                cooldown_ms = self.settings.alert_cooldown_minutes * 60_000
                if signal.candle.time_ms - last_alert < cooldown_ms:
                    LOGGER.info("Cooldown skipped: %s", signal.key)
                    return
        account_asset = await self._get_account_asset_for_risk()
        risk_plan: RiskPlan | None = None
        risk_note = self._account_asset_error
        if account_asset is not None:
            try:
                risk_plan = build_risk_plan(signal, account_asset, self.settings)
            except Exception as exc:
                risk_note = "5%リスク枚数を計算できませんでした"
                LOGGER.warning("Risk plan calculation failed for %s: %s", signal.key, exc)

        message = format_signal(
            signal,
            self.settings.timezone_name,
            risk_plan=risk_plan,
            risk_note=risk_note,
        )
        await self.notifier.send(message)
        self.store.save_alert(signal, int(time.time() * 1000))
        LOGGER.info(
            "Strategy alert sent: %s %s direction=%s rr=%s",
            signal.contract.contract_name,
            signal.interval,
            signal.direction,
            f"{signal.rr:.2f}" if signal.rr is not None else "-",
        )

    async def _handle_message(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            LOGGER.warning("Ignoring non-JSON WebSocket message")
            return
        message_type = str(message.get("type", "")).lower()
        if message_type == "ping":
            # EdgeX uses an application-level heartbeat in addition to the
            # WebSocket protocol ping/pong.
            return
        if message_type == "error":
            LOGGER.warning("EdgeX WebSocket error: %s", message)
            return
        if message_type != "quote-event":
            return
        content = message.get("content") or {}
        interval = str(content.get("channel") or message.get("channel") or "").split(".")[-1].upper()
        if interval not in INTERVAL_MS:
            return
        payload_items = content.get("data") or []
        if not isinstance(payload_items, list):
            return
        parsed: list[Candle] = []
        for item in payload_items:
            if isinstance(item, dict):
                candle = Candle.from_payload(item, fallback_interval=interval)
                if candle is not None and candle.interval == interval:
                    parsed.append(candle)
        if not parsed:
            return
        key = (parsed[0].contract_id, interval)
        self._add_candles(key, parsed)
        data_type = str(content.get("dataType", "")).lower()
        if data_type == "snapshot" and self.run_once:
            self.snapshot_seen.add(key)
            await self._process_snapshot_once(key, interval)
            if self.expected_snapshot_keys and self.expected_snapshot_keys.issubset(self.snapshot_seen):
                LOGGER.info(
                    "Scheduled scan completed: %d/%d snapshots",
                    len(self.snapshot_seen),
                    len(self.expected_snapshot_keys),
                )
                self.request_stop()
        elif self.run_once:
            # A live update can arrive interleaved with the initial snapshots.
            # A scheduled run evaluates snapshots only and must not fall back
            # to the continuous-mode bootstrap path.
            return
        elif data_type == "snapshot":
            await self._bootstrap(key, interval)
        else:
            await self._process_updates(key, interval)

    async def _read_connection(self, websocket: Any) -> None:
        async for raw in websocket:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                await self._handle_message(raw)
                continue
            if str(message.get("type", "")).lower() == "ping":
                await websocket.send(json.dumps({"type": "pong", "time": message.get("time")}))
                continue
            await self._handle_message(json.dumps(message))

    async def _refresh_timer(self, refresh_event: asyncio.Event) -> None:
        try:
            await asyncio.sleep(self.settings.metadata_refresh_seconds)
            refresh_event.set()
        except asyncio.CancelledError:
            return

    async def _once_timeout(self) -> None:
        await asyncio.sleep(self.once_timeout_seconds)

    async def _run_connection(self, contracts: dict[str, Contract]) -> None:
        LOGGER.info(
            "Connecting to EdgeX WebSocket: %d contracts x %d interval(s)",
            len(contracts),
            len(self.settings.intervals),
        )
        refresh_event = asyncio.Event()
        self.expected_snapshot_keys = {
            (contract_id, interval)
            for interval in self.settings.intervals
            for contract_id in contracts
        }
        self.snapshot_seen.clear()
        async with websockets.connect(
            self.settings.ws_url,
            open_timeout=30,
            close_timeout=10,
            ping_interval=20,
            ping_timeout=20,
            max_size=8 * 1024 * 1024,
        ) as websocket:
            hello = await asyncio.wait_for(websocket.recv(), timeout=30)
            LOGGER.debug("EdgeX WebSocket hello: %s", hello)
            for interval in self.settings.intervals:
                for contract_id in contracts:
                    channel = f"kline.LAST_PRICE.{contract_id}.{interval}"
                    await websocket.send(json.dumps({"type": "subscribe", "channel": channel}))
            LOGGER.info("Subscribed to %d kline streams", len(contracts) * len(self.settings.intervals))
            reader = asyncio.create_task(self._read_connection(websocket))
            stopper = asyncio.create_task(self.stop_event.wait())
            if self.run_once:
                timeout_task = asyncio.create_task(self._once_timeout())
                done, pending = await asyncio.wait(
                    {reader, timeout_task, stopper},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            else:
                refresher = asyncio.create_task(self._refresh_timer(refresh_event))
                done, pending = await asyncio.wait(
                    {reader, refresher, stopper},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if stopper in done or self.stop_event.is_set():
                return
            if self.run_once and any(task is timeout_task for task in done):
                missing = len(self.expected_snapshot_keys - self.snapshot_seen)
                LOGGER.warning("Scheduled scan timed out with %d snapshot(s) missing", missing)
                self.request_stop()
                return
            if self.run_once and reader in done:
                LOGGER.warning(
                    "Scheduled scan connection ended: %d/%d snapshots",
                    len(self.snapshot_seen),
                    len(self.expected_snapshot_keys),
                )
            if refresh_event.is_set():
                LOGGER.info("Refreshing EdgeX metadata and subscriptions")
            for task in done:
                if task is reader:
                    exception = task.exception()
                    if exception:
                        raise exception

    async def run(self) -> None:
        try:
            await self._sync_telegram_equity_commands()
            if self.run_once:
                self.contracts = await self.client.get_contracts()
                LOGGER.info(
                    "Loaded %d tradable EdgeX contracts (include_hidden=%s)",
                    len(self.contracts),
                    self.settings.include_hidden,
                )
                await self._run_connection(self.contracts)
                return

            backoff = self.settings.reconnect_initial_seconds
            while not self.stop_event.is_set():
                try:
                    self.contracts = await self.client.get_contracts()
                    LOGGER.info(
                        "Loaded %d tradable EdgeX contracts (include_hidden=%s)",
                        len(self.contracts),
                        self.settings.include_hidden,
                    )
                    await self._run_connection(self.contracts)
                    backoff = self.settings.reconnect_initial_seconds
                except asyncio.CancelledError:
                    raise
                except ConnectionClosed as exc:
                    LOGGER.warning("EdgeX WebSocket closed: %s", exc)
                except Exception:
                    LOGGER.exception("Scanner connection failed")
                if not self.stop_event.is_set():
                    LOGGER.info("Reconnecting in %.1f seconds", backoff)
                    try:
                        await asyncio.wait_for(self.stop_event.wait(), timeout=backoff)
                    except asyncio.TimeoutError:
                        pass
                    backoff = min(backoff * 2, self.settings.reconnect_max_seconds)
        finally:
            await self.close()


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


async def _async_main(args: argparse.Namespace) -> None:
    settings = Settings.from_env(dry_run_override=True if args.dry_run else None)
    service = BreakoutService(
        settings,
        run_once=args.once,
        once_timeout_seconds=args.once_timeout,
    )
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, service.request_stop)
        except (NotImplementedError, RuntimeError):
            pass
    LOGGER.info(
        "Starting EdgeX roll-reversal scanner: mode=%s intervals=%s monitor=%s entry=%s EMA=%d/%d ATR=%d minRR=%.2f risk=%.2f%% manual_risk=%s account_risk=%s dry_run=%s state=%s",
        "scheduled-once" if args.once else "continuous",
        ",".join(settings.intervals),
        settings.monitor_interval,
        settings.entry_interval,
        settings.trend_fast_ema,
        settings.trend_slow_ema,
        settings.atr_period,
        settings.min_rr,
        settings.risk_per_trade * 100,
        settings.manual_risk_enabled,
        settings.account_risk_enabled,
        settings.dry_run,
        settings.state_backend,
    )
    await service.run()


def main() -> None:
    parser = argparse.ArgumentParser(description="EdgeX 4H trend / 15M roll-reversal Telegram notifier")
    parser.add_argument("--dry-run", action="store_true", help="log notifications without sending Telegram messages")
    parser.add_argument("--once", action="store_true", help="run one scheduled snapshot scan and exit")
    parser.add_argument(
        "--once-timeout",
        type=float,
        default=75.0,
        help="maximum seconds to wait for scheduled WebSocket snapshots",
    )
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    args = parser.parse_args()
    _configure_logging(args.log_level)
    try:
        asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        LOGGER.info("Stopped")


if __name__ == "__main__":
    main()
