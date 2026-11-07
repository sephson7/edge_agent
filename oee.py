"""Finite, bounded OEE calculations for edge-side telemetry."""

from __future__ import annotations

import math
from dataclasses import dataclass


def _finite_nonnegative(value: float | int | None) -> float:
    if value is None:
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return number if math.isfinite(number) and number >= 0 else 0.0


def _clamp_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    ratio = numerator / denominator
    if not math.isfinite(ratio):
        return 0.0
    return min(1.0, max(0.0, ratio))


@dataclass(frozen=True)
class OEEMetrics:
    availability: float
    performance: float
    quality: float
    oee: float


def calculate_oee(
    *,
    planned_shift_seconds: float | int | None,
    running_seconds: float | int | None,
    cycle_count: float | int | None,
    ideal_cycle_time_seconds: float | int | None,
    good_count: float | int | None,
) -> OEEMetrics:
    """Calculate OEE safely; missing or invalid denominators contribute zero."""

    planned = _finite_nonnegative(planned_shift_seconds)
    running = _finite_nonnegative(running_seconds)
    cycles = _finite_nonnegative(cycle_count)
    ideal_cycle = _finite_nonnegative(ideal_cycle_time_seconds)
    good = _finite_nonnegative(good_count)

    availability = _clamp_ratio(running, planned)
    ideal_production = running / ideal_cycle if running > 0 and ideal_cycle > 0 else 0.0
    performance = _clamp_ratio(cycles, ideal_production)
    quality = _clamp_ratio(good, cycles)
    oee = availability * performance * quality
    if not math.isfinite(oee):
        oee = 0.0
    return OEEMetrics(
        availability=availability,
        performance=performance,
        quality=quality,
        oee=min(1.0, max(0.0, oee)),
    )
