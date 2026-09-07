"""Read-only exchange calendar. UTC internally; exchange-local session dates.

No brokerage account is used. Missing/out-of-range calendar data fails closed.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

UTC = timezone.utc
ET = ZoneInfo('America/New_York')


def utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('Timestamp must include a timezone, e.g. 2026-09-08T10:00:00-04:00')
    return value.astimezone(UTC)


def session_day(value: datetime | str) -> date:
    return utc(value).astimezone(ET).date()


@dataclass(frozen=True)
class Session:
    trading_date: date
    open_at: datetime
    close_at: datetime

    def __post_init__(self):
        object.__setattr__(self, 'open_at', utc(self.open_at))
        object.__setattr__(self, 'close_at', utc(self.close_at))
        if self.close_at <= self.open_at + timedelta(minutes=45):
            raise ValueError('Invalid trading session')

    @property
    def opening_range_end(self): return self.open_at + timedelta(minutes=15)
    @property
    def entry_cutoff(self): return self.close_at - timedelta(minutes=30)
    @property
    def force_flatten_at(self): return self.close_at - timedelta(minutes=15)


class Calendar:
    """XNYS calendar suitable for US-equity regular-hours signals.

    This is a session calendar, NOT a broker's margin/settlement/eligibility check.
    """
    @lru_cache(maxsize=12)
    def _year(self, year: int):
        import exchange_calendars as xcals
        return xcals.get_calendar('XNYS', start=f'{year-1}-12-01', end=f'{year+1}-01-31')

    @lru_cache(maxsize=2048)
    def session_for(self, day: date) -> Session | None:
        import pandas as pd
        cal = self._year(day.year)
        label = pd.Timestamp(day)
        if not cal.is_session(label):
            return None
        return Session(day, cal.session_open(label).to_pydatetime(),
                       cal.session_close(label).to_pydatetime())


def normalize_session(session) -> Session:
    """Compatibility for legacy injected sessions, whose naive times are ET."""
    def aware(v): return v.replace(tzinfo=ET) if v.tzinfo is None else v
    return Session(session.trading_date, aware(session.open_at), aware(session.close_at))
