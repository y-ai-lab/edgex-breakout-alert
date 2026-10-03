"""Read-only, paginated LAST_PRICE candles from the documented public API."""
import asyncio
import math
from typing import Callable

import app as scanner

PATH = "/api/v2/public/quote/getKline"


async def fetch_history(get_json: Callable, contract: scanner.Contract, interval: str,
                        begin_ms: int, end_ms: int, *, size: int = 1000) -> list[scanner.Candle]:
    step = scanner.INTERVAL_MS[interval]
    if begin_ms <= 0 or end_ms <= begin_ms or not 1 <= size <= 1000:
        raise ValueError("Invalid public history bounds")
    params = dict(contractId=contract.contract_id, klineType=interval, priceType="LAST_PRICE", size=str(size),
                  filterBeginKlineTimeInclusive=str(begin_ms), filterEndKlineTimeExclusive=str(end_ms))
    found = {}
    seen_tokens = set()
    for _ in range(100):
        payload = await asyncio.to_thread(get_json, PATH, dict(params))
        if payload.get("code") != "SUCCESS" or not isinstance(payload.get("data"), dict):
            raise ValueError("Invalid public history response")
        page = payload["data"]
        items = page.get("dataList")
        if not isinstance(items, list):
            raise ValueError("Missing public history records")
        for raw in items:
            c = scanner.Candle.from_payload(raw, fallback_interval=interval) if isinstance(raw, dict) else None
            if c is None or c.contract_id != contract.contract_id or c.interval != interval or raw.get("priceType") != "LAST_PRICE":
                raise ValueError("Invalid candle identity or price type")
            values = (c.open, c.high, c.low, c.close, c.volume, c.value)
            if not all(math.isfinite(v) for v in values) or c.low > min(c.open, c.close) or c.high < max(c.open, c.close):
                raise ValueError("Invalid candle OHLC")
            if c.time_ms % step or not begin_ms <= c.time_ms < end_ms:
                raise ValueError("Candle outside requested time grid")
            if c.time_ms in found and found[c.time_ms] != c:
                raise ValueError("Conflicting historical candle revisions")
            found[c.time_ms] = c
        token = page.get("nextPageOffsetData")
        if not token:
            return sorted(found.values(), key=lambda c: c.time_ms)
        if not isinstance(token, str) or token in seen_tokens or not items:
            raise ValueError("Invalid history pagination")
        seen_tokens.add(token)
        params["offsetData"] = token
    raise ValueError("History pagination exceeded safety bound")
