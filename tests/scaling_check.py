"""Timing assertions for the analyzers' complexity tests.

Absolute time limits flake on slow or busy CI runners. These assertions
compare two timings taken in the same run on the same machine, so the
runner's speed cancels out: doubling linear work about doubles the time,
while doubling quadratic work quadruples it.

A ratio over the bound is measured once more before failing: a burst of load
on a shared runner can slow one side of one measurement, while a real
complexity regression is over the bound every time.
"""

from __future__ import annotations

import gc
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TypeVar

T = TypeVar("T")

# Calls shorter than this are repeated until one measurement reaches it, so
# timer resolution and scheduler jitter don't decide the ratio.
_MIN_MEASUREMENT_SECONDS = 0.02
# Hang guard only; the ratio carries the actual check.
HANG_GUARD_SECONDS = 60.0
# Measurements of one ratio before it fails the check.
ATTEMPTS = 2


@contextmanager
def gc_paused() -> Iterator[None]:
    """Collect, then keep the cyclic collector off while timing.

    A collection triggered inside one measurement but not the other adds its
    whole cost to one side of the ratio. The previous state is restored, so a
    caller that had the collector off keeps it off.
    """
    was_enabled = gc.isenabled()
    gc.collect()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()


def best_time[T](run: Callable[[T], object], arg: T, repeats: int = 3) -> float:
    """Best per-call time over `repeats` measurements, with the collector paused."""
    with gc_paused():
        return _best_time(run, arg, repeats)


def _best_time[T](run: Callable[[T], object], arg: T, repeats: int) -> float:
    started = time.perf_counter()
    run(arg)
    first = time.perf_counter() - started
    assert first < HANG_GUARD_SECONDS
    loops = max(1, int(_MIN_MEASUREMENT_SECONDS / max(first, 1e-9)) + 1)
    best = first if loops == 1 else float("inf")
    for _ in range(repeats if loops > 1 else repeats - 1):
        started = time.perf_counter()
        for _ in range(loops):
            run(arg)
        elapsed = time.perf_counter() - started
        assert elapsed < HANG_GUARD_SECONDS
        best = min(best, elapsed / loops)
    return best


def assert_scales_linearly[T](
    make: Callable[[int], T],
    run: Callable[[T], object],
    n: int,
    max_ratio: float = 3.0,
) -> None:
    """Time at size 2n over time at size n stays near 2, well below the 4 of
    quadratic work. Inputs are built outside the timed part."""
    small, large = make(n), make(2 * n)
    ratios = _ratios(lambda: best_time(run, large) / best_time(run, small), max_ratio)
    assert ratios[-1] < max_ratio, f"doubling the input took {', then '.join(f'{r:.2f}x' for r in ratios)} as long"


def assert_costs_like[T](hard: T, easy: T, run: Callable[[T], object], max_ratio: float) -> None:
    """Adversarial input costs at most `max_ratio` times a benign input of the
    same size. For regressions where a capped rescan made the work linear but
    with a large constant."""
    ratios = _ratios(lambda: best_time(run, hard) / best_time(run, easy), max_ratio)
    assert ratios[-1] < max_ratio, f"the adversarial input took {', then '.join(f'{r:.2f}x' for r in ratios)} as long"


def _ratios(measure: Callable[[], float], max_ratio: float) -> list[float]:
    """Each measured ratio, stopping at the first under ``max_ratio`` or after `ATTEMPTS`."""
    ratios = [measure()]
    while ratios[-1] >= max_ratio and len(ratios) < ATTEMPTS:
        ratios.append(measure())
    return ratios
