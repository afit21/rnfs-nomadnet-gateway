"""Fetch limits per visitor and overall (specification section 7). Not thread-safe: the gateway calls it
with its own lock held."""
import time
from collections import deque

HOUR = 3600.0


class Limits:
    """Fetches running (overall and per visitor) and fetches started per visitor in the last hour."""

    def __init__(self, max_fetches: int, max_per_visitor: int, per_hour: int, clock=time.monotonic):
        self.max_fetches = max_fetches
        self.max_per_visitor = max_per_visitor
        self.per_hour = per_hour
        self.clock = clock
        self.running: dict[str, int] = {}
        self.started: dict[str, deque] = {}  # visitor → start times within the last hour, oldest first

    def _recent(self, visitor: str) -> deque:
        """visitor's starts within the last hour (older ones dropped)."""
        starts = self.started.get(visitor)
        if starts is None:
            return deque()
        cutoff = self.clock() - HOUR
        while starts and starts[0] <= cutoff:
            starts.popleft()
        if not starts:
            del self.started[visitor]
        return starts

    def refusal(self, visitor: str) -> str | None:
        """Why visitor can't start a fetch now, or None if they can."""
        if self.running.get(visitor, 0) >= self.max_per_visitor:
            return ("Wait for your other fetch to finish." if self.max_per_visitor == 1
                    else "Wait for one of your fetches to finish.")
        starts = self._recent(visitor)
        if len(starts) >= self.per_hour:
            wait = max(1, int(starts[0] + HOUR - self.clock()) // 60 + 1)
            return f"No fetches left this hour. Try again in {wait} min."
        if sum(self.running.values()) >= self.max_fetches:
            return "The gateway is full right now. Try again later."
        return None

    def start(self, visitor: str):
        self.running[visitor] = self.running.get(visitor, 0) + 1
        self.started.setdefault(visitor, deque()).append(self.clock())

    def finish(self, visitor: str):
        count = self.running.get(visitor, 0) - 1
        if count > 0:
            self.running[visitor] = count
        else:
            self.running.pop(visitor, None)

    def remaining(self, visitor: str) -> int:
        """Fetches visitor may still start this hour."""
        return max(0, self.per_hour - len(self._recent(visitor)))

    def prune(self):
        """Forget visitors with no starts in the last hour."""
        for visitor in list(self.started):
            self._recent(visitor)
