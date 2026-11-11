"""Daily spend caps for paid APIs. Check before calling; record after."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engine.db.models import Spend, utcnow


class SpendCapReached(Exception):
    def __init__(self, service: str, spent: float, cap: float) -> None:
        super().__init__(f"{service} daily cap reached (${spent:.2f} of ${cap:.2f})")
        self.service, self.spent, self.cap = service, spent, cap


class SpendLedger:
    def __init__(self, session_factory: sessionmaker[Session], now=utcnow) -> None:
        self._session_factory = session_factory
        self._now = now

    def _day_start(self) -> datetime:
        return self._now().astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    def spent_today(self, service: str) -> float:
        with self._session_factory() as db:
            total = db.scalar(
                select(func.coalesce(func.sum(Spend.usd), 0.0)).where(
                    Spend.service == service, Spend.ts >= self._day_start()
                )
            )
        return float(total or 0.0)

    def check(self, service: str, cap_usd: float, about_to_spend: float = 0.0) -> None:
        """Raise if today's spend, plus what the next call will cost, would pass the cap."""
        spent = self.spent_today(service)
        if spent + about_to_spend > cap_usd:
            raise SpendCapReached(service, spent, cap_usd)

    def record(self, service: str, usd: float, details: dict | None = None) -> None:
        with self._session_factory.begin() as db:
            db.add(Spend(service=service, usd=max(0.0, usd), details=details, ts=self._now()))
