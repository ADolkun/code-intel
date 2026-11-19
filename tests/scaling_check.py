"""Timing assertions for the analyzers' complexity tests.

Absolute time limits flake on slow or busy CI runners. These assertions
compare two timings taken in the same run on the same machine, so the
runner's speed cancels out: doubling linear work about doubles the time,
while doubling quadratic work quadruples it.

The two sides of a ratio are timed in alternation inside one collector-paused
block: a loop count is calibrated per side, then each round times the
numerator and the denominator back to back, and each side keeps its best
per-call time. Load that shifts over seconds, as when sibling test workers
start and stop, lands on both sides alike instead of on one whole block.

A ratio over the bound is measured once more before failing: a burst of load
can still slow one side of one measurement, while a real complexity
regression is over the bound every time.
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
        loops = _calibrated_loops(run, arg)
        return min(_per_call(run, arg, loops) for _ in range(repeats))


def best_ratio[T](run: Callable[[T], object], numerator: T, denominator: T, repeats: int = 3) -> float:
    """Best per-call time of `run(numerator)` over that of `run(denominator)`.

    The sides are timed alternately for `repeats` rounds, so a change in
    machine load between rounds affects both.
    """
    with gc_paused():
        loops_num = _calibrated_loops(run, numerator)
        loops_den = _calibrated_loops(run, denominator)
        best_num = best_den = float("inf")
        for _ in range(repeats):
            best_num = min(best_num, _per_call(run, numerator, loops_num))
            best_den = min(best_den, _per_call(run, denominator, loops_den))
        return best_num / best_den


def _calibrated_loops[T](run: Callable[[T], object], arg: T) -> int:
    """Calls per measurement so one reaches `_MIN_MEASUREMENT_SECONDS`. The
    calibration call also warms caches, and is not itself a sample."""
    started = time.perf_counter()
    run(arg)
    first = time.perf_counter() - started
    assert first < HANG_GUARD_SECONDS
    return int(_MIN_MEASUREMENT_SECONDS / max(first, 1e-9)) + 1


def _per_call[T](run: Callable[[T], object], arg: T, loops: int) -> float:
    started = time.perf_counter()
    for _ in range(loops):
        run(arg)
    elapsed = time.perf_counter() - started
    assert elapsed < HANG_GUARD_SECONDS
    return elapsed / loops


def assert_scales_linearly[T](
    make: Callable[[int], T],
    run: Callable[[T], object],
    n: int,
    max_ratio: float = 3.0,
) -> None:
    """Time at size 2n over time at size n stays near 2, well below the 4 of
    quadratic work. Inputs are built outside the timed part."""
    small, large = make(n), make(2 * n)
    ratios = _ratios(lambda: best_ratio(run, large, small), max_ratio)
    assert ratios[-1] < max_ratio, f"doubling the input took {', then '.join(f'{r:.2f}x' for r in ratios)} as long"


def assert_costs_like[T](hard: T, easy: T, run: Callable[[T], object], max_ratio: float) -> None:
    """Adversarial input costs at most `max_ratio` times a benign input of the
    same size. For regressions where a capped rescan made the work linear but
    with a large constant."""
    ratios = _ratios(lambda: best_ratio(run, hard, easy), max_ratio)
    assert ratios[-1] < max_ratio, f"the adversarial input took {', then '.join(f'{r:.2f}x' for r in ratios)} as long"


def _ratios(measure: Callable[[], float], max_ratio: float) -> list[float]:
    """Each measured ratio, stopping at the first under ``max_ratio`` or after `ATTEMPTS`."""
    ratios = [measure()]
    while ratios[-1] >= max_ratio and len(ratios) < ATTEMPTS:
        ratios.append(measure())
    return ratios
