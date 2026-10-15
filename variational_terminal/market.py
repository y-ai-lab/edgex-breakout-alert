"""Public Omni stats. These are indicative quotes, never fills or OHLC."""
from __future__ import annotations
import asyncio
import math
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import requests

STATS_URL = 'https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats'
PROVENANCE = dict(venue='VARIATIONAL_OMNI', analysis_scope='EXTERNAL_REFERENCE_ONLY',
                  candle_source='EDGEX_PUBLIC_LAST_PRICE', native_candles=False,
                  native_entry_ready=False, real_orders_enabled=False,
                  performance_scope='REFERENCE_HYPOTHETICAL_ONLY')


def numeric(value, *, positive=False):
    if value is None or isinstance(value, bool):
        return None
    try:
        n = Decimal(str(value))
        if not n.is_finite() or (positive and n <= 0) or not math.isfinite(float(n)):
            return None
        return str(value)
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return None


def timestamp(value):
    try:
        if not isinstance(value, str) or not value.endswith('Z'):
            return None
        return int(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()*1000)
    except (ValueError, OverflowError):
        return None


def parse_stats(raw, *, observed_ms):
    if not isinstance(raw, dict) or not isinstance(raw.get('listings'), list) or not raw['listings']:
        raise ValueError('INVALID_STATS')
    markets = {}
    for item in raw['listings']:
        if not isinstance(item, dict):
            raise ValueError('INVALID_LISTING')
        ticker = item.get('ticker')
        if not isinstance(ticker, str) or not re.fullmatch(r'[A-Z0-9][A-Z0-9._-]{0,39}', ticker):
            raise ValueError('INVALID_TICKER')
        if ticker in markets:
            raise ValueError('DUPLICATE_TICKER')
        q = item.get('quotes') if isinstance(item.get('quotes'), dict) else {}
        base = q.get('base') if isinstance(q.get('base'), dict) else {}
        oi = item.get('open_interest') if isinstance(item.get('open_interest'), dict) else {}
        markets[ticker] = dict(ticker=ticker, name=str(item.get('name') or ticker)[:150],
            mark_price=numeric(item.get('mark_price'), positive=True),
            volume_24h=numeric(item.get('volume_24h')), funding_rate=numeric(item.get('funding_rate')),
            funding_interval_s=numeric(item.get('funding_interval_s'), positive=True),
            long_open_interest=numeric(oi.get('long_open_interest')),
            short_open_interest=numeric(oi.get('short_open_interest')),
            base_spread_bps=numeric(item.get('base_spread_bps')),
            indicative_bid=numeric(base.get('bid'), positive=True),
            indicative_ask=numeric(base.get('ask'), positive=True),
            quote_updated_ms=timestamp(q.get('updated_at')),
            stats_observed_ms=observed_ms, mark_updated_ms=None,
            quote_executable=False)
    return markets


def view(market, *, now_ms):
    m = dict(market)
    updated = m['quote_updated_ms']
    age = (now_ms-updated)/1000 if updated is not None else None
    m['quote_age_seconds'] = age
    m['quote_timestamp_status'] = ('UNKNOWN' if age is None else 'FUTURE_TIMESTAMP' if age < 0
                                    else 'STALE' if age >= 120 else 'RECENT_INDICATIVE')
    m['stats_age_seconds'] = max(0, (now_ms-m['stats_observed_ms'])/1000)
    return m


class StatsClient:
    def __init__(self, get=None):
        self.get = get or requests.get
        self.markets = {}
        self.observed_ms = None
        self.lock = asyncio.Lock()

    def _fetch(self):
        response = self.get(STATS_URL, timeout=20, allow_redirects=False)
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError('INVALID_STATS_STATUS')
        return response.json()

    async def refresh(self):
        # All callers share the cache, including forced browser scans.
        async with self.lock:
            if self.observed_ms is not None and time.time()*1000-self.observed_ms < 60_000:
                return self.markets
            raw = await asyncio.to_thread(self._fetch)
            observed = int(time.time()*1000)
            markets = parse_stats(raw, observed_ms=observed)
            self.markets, self.observed_ms = markets, observed
            return markets
