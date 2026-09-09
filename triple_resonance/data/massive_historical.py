"""Read-only Massive minute aggregates with bounded pagination and rate limiting."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import os
import time
import urllib.parse
import urllib.request

from ..domain.models import Bar


class MassiveHistoricalProvider:
    def __init__(self, api_key=None, *, calls_per_minute=5, opener=None, clock=None,
                 sleeper=None):
        self.api_key = api_key or os.getenv("MASSIVE_API_KEY")
        if not self.api_key:
            raise ValueError("Set MASSIVE_API_KEY in the environment")
        if not 0 < calls_per_minute <= 5:
            raise ValueError("Free-plan calls_per_minute must be between 1 and 5")
        self.min_interval = 60 / calls_per_minute
        self.opener = opener or urllib.request.urlopen
        self.clock = clock or time.monotonic
        self.sleeper = sleeper or time.sleep
        self.last_request_at = None

    def _get(self, url):
        if self.last_request_at is not None:
            remaining = self.min_interval - (self.clock() - self.last_request_at)
            if remaining > 0:
                self.sleeper(remaining)
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self.api_key}",
                          "User-Agent": "triple-resonance-research/1"})
        try:
            with self.opener(request, timeout=30) as response:
                payload = json.load(response)
        finally:
            self.last_request_at = self.clock()
        if payload.get("status") not in ("OK", "DELAYED"):
            raise ValueError(f"Massive request failed: {payload.get('status', 'unknown')}")
        return payload

    def bars(self, symbol, start: date, end: date):
        if end <= start:
            raise ValueError("end must be after start")
        symbol = symbol.upper()
        inclusive_end = end - timedelta(days=1)
        base = (f"https://api.massive.com/v2/aggs/ticker/{urllib.parse.quote(symbol)}/"
                f"range/1/minute/{start}/{inclusive_end}")
        url = base + "?adjusted=false&sort=asc&limit=50000"
        rows = []
        seen_urls = set()
        while url:
            if url in seen_urls:
                raise ValueError("Massive pagination loop detected")
            seen_urls.add(url)
            payload = self._get(url)
            for item in payload.get("results", []):
                ts = datetime.fromtimestamp(item["t"] / 1000, timezone.utc)
                rows.append(Bar(symbol, ts, float(item["o"]), float(item["h"]),
                                float(item["l"]), float(item["c"]), float(item["v"]),
                                float(item["vw"]) if item.get("vw") is not None else None))
            url = payload.get("next_url")
        return sorted({bar.ts: bar for bar in rows}.values(), key=lambda bar: bar.ts)
