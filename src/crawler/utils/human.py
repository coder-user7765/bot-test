"""Human-like timing and session mimicry utilities.

Provides helpers to make automated requests resemble organic user behaviour:
variable think times, micro-pauses between related actions, and session-level
cadence adjustments that avoid mechanical regularity.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field


@dataclass
class HumanCadence:
    """Adaptive pacing that drifts like a real user's attention span."""

    base_delay: float = 3.0
    jitter: float = 2.0
    burst_probability: float = 0.15
    pause_probability: float = 0.08
    burst_factor: float = 0.4
    pause_factor: float = 3.5
    fatigue_after: int = 25
    fatigue_scale: float = 1.4
    _requests: int = field(default=0, init=False)
    _last_ts: float = field(default_factory=time.monotonic, init=False)

    async def wait(self, sleep: Callable[[float], Awaitable[None]] | None = None) -> None:
        """Sleep for a humanised interval before the next action.

        `sleep` lets the caller supply an interruptible sleep (so Ctrl+C is honoured)."""
        self._requests += 1
        # Determine behaviour mode first to avoid double-rolling probability
        roll = random.random()
        if roll < self.burst_probability:
            factor = self.burst_factor
        elif roll < self.burst_probability + self.pause_probability:
            factor = self.pause_factor
        else:
            factor = 1.0

        span = self.base_delay + self.jitter
        if span <= 0:
            delay = 0.0
        else:
            # Log-normal "reading time": most gaps cluster near the middle of the configured
            # range with an occasional long one, like a person - not a flat random.uniform.
            median = self.base_delay + self.jitter / 2
            delay = random.lognormvariate(math.log(median), 0.35)
            delay = min(max(delay, self.base_delay * 0.6), span * 2.5) * factor

            if self.fatigue_after and self._requests > self.fatigue_after:
                fatigue = 1 + (self._requests - self.fatigue_after) / self.fatigue_after
                delay *= min(self.fatigue_scale, fatigue)

            # Enforce minimum floor so bursts don't collapse to zero/near-zero
            delay = max(0.25, delay)

        elapsed = time.monotonic() - self._last_ts
        remaining = max(0.0, delay - elapsed)
        if remaining > 0:
            await (sleep or asyncio.sleep)(remaining)
        self._last_ts = time.monotonic()

    def reset(self) -> None:
        self._requests = 0
        self._last_ts = time.monotonic()


def think_time(min_s: float = 0.8, max_s: float = 4.5) -> float:
    """Return a single 'thinking' duration drawn from a log-normal distribution."""
    mu = (math.log(min_s) + math.log(max_s)) / 2
    sigma = (math.log(max_s) - math.log(min_s)) / 4
    return max(min_s, min(max_s, random.lognormvariate(mu, sigma)))


async def human_pause(min_s: float = 0.6, max_s: float = 3.2) -> None:
    """Async sleep for a naturally variable pause."""
    await asyncio.sleep(think_time(min_s, max_s))


def micro_jitter(base: float = 0.05, spread: float = 0.15) -> float:
    """Tiny per-action noise so sequential operations are never perfectly spaced."""
    return max(0.0, base + random.uniform(-spread, spread))