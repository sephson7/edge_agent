"""Debounce discrete inputs and reject implausible current measurements."""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable


DEFAULT_COUNTER_DEBOUNCE_MS = 50
DEFAULT_MOTOR_DEBOUNCE_MS = 250
MAX_CT_AMPS = 500.0


@dataclass(frozen=True)
class SignalResult:
    value: Any
    changed: bool = False
    fault: str | None = None
    state: str | None = None


class DiscreteDebouncer:
    """Only accept a discrete transition after it has remained stable."""

    def __init__(self, debounce_ms: int = DEFAULT_COUNTER_DEBOUNCE_MS) -> None:
        if debounce_ms < 0:
            raise ValueError("debounce_ms must not be negative")
        self.debounce_ms = debounce_ms
        self._stable: bool | None = None
        self._candidate: bool | None = None
        self._candidate_since: float | None = None

    @property
    def stable_value(self) -> bool | None:
        return self._stable

    def update(self, value: bool, timestamp_ms: float) -> bool | None:
        value = bool(value)
        if not math.isfinite(timestamp_ms):
            raise ValueError("timestamp_ms must be finite")
        if self._stable is None:
            if self._candidate != value:
                self._candidate = value
                self._candidate_since = timestamp_ms
            elif self._candidate_since is not None and timestamp_ms - self._candidate_since >= self.debounce_ms:
                self._stable = value
                self._candidate = None
                self._candidate_since = None
                return value
            return None

        if value == self._stable:
            self._candidate = None
            self._candidate_since = None
            return None
        if self._candidate != value:
            self._candidate = value
            self._candidate_since = timestamp_ms
            return None
        if self._candidate_since is not None and timestamp_ms - self._candidate_since >= self.debounce_ms:
            self._stable = value
            self._candidate = None
            self._candidate_since = None
            return value
        return None


class AnalogCTFilter:
    """Median-filter CT spikes and debounce running/idle transitions."""

    def __init__(
        self,
        *,
        debounce_ms: int = DEFAULT_MOTOR_DEBOUNCE_MS,
        max_amps: float = MAX_CT_AMPS,
        running_threshold_amps: float = 1.0,
        window_size: int = 5,
        spike_ratio: float = 3.0,
    ) -> None:
        if not math.isfinite(max_amps) or max_amps <= 0:
            raise ValueError("max_amps must be a positive finite number")
        if not math.isfinite(running_threshold_amps) or running_threshold_amps < 0:
            raise ValueError("running_threshold_amps must be a non-negative finite number")
        if window_size < 1:
            raise ValueError("window_size must be positive")
        self.max_amps = max_amps
        self.running_threshold_amps = running_threshold_amps
        self.spike_ratio = spike_ratio
        self._samples: deque[float] = deque(maxlen=window_size)
        self._running = DiscreteDebouncer(debounce_ms)

    def update(self, amps: float, timestamp_ms: float) -> SignalResult:
        if not math.isfinite(amps) or amps < 0 or amps > self.max_amps:
            return SignalResult(value=None, fault="SENSOR_FAULT")

        median = statistics.median(self._samples) if self._samples else amps
        if self._samples and abs(amps - median) > max(1.0, abs(median) * self.spike_ratio):
            filtered = median
        else:
            self._samples.append(amps)
            filtered = statistics.median(self._samples)

        transition = self._running.update(filtered >= self.running_threshold_amps, timestamp_ms)
        changed = transition is not None
        return SignalResult(
            value=filtered,
            changed=changed,
            fault=None,
            state=("RUNNING" if transition else "IDLE") if changed else None,
        )


class SignalFilter:
    """Manage channel-specific discrete and analog signal conditioners."""

    def __init__(
        self,
        debounce_ms_by_channel: dict[str, int] | None = None,
        channel_types: dict[str, str] | None = None,
    ) -> None:
        self._debounce_ms = debounce_ms_by_channel or {}
        self._channel_types = channel_types or {}
        self._discrete: dict[str, DiscreteDebouncer] = {}
        self._analog: dict[str, AnalogCTFilter] = {}

    @classmethod
    def from_channels(cls, channels: Iterable[Any] | dict[str, Any]) -> SignalFilter:
        """Create per-channel debounce settings from profile channel metadata."""

        entries = channels.items() if isinstance(channels, dict) else enumerate(channels)
        debounce: dict[str, int] = {}
        channel_types: dict[str, str] = {}
        for key, channel in entries:
            if hasattr(channel, "model_dump"):
                channel = channel.model_dump(mode="python")
            if not isinstance(channel, dict):
                continue
            channel_id = str(channel.get("channel_id") or channel.get("name") or key)
            channel_type = str(
                channel.get("channel_type") or channel.get("type") or channel.get("name") or ""
            ).lower()
            if "debounce_ms" in channel:
                debounce[channel_id] = int(channel["debounce_ms"])
            channel_types[channel_id] = channel_type
        return cls(debounce, channel_types)

    def update_discrete(
        self,
        channel_id: str,
        value: bool,
        timestamp_ms: float,
        *,
        channel_type: str = "part_counter",
        debounce_ms: int | None = None,
    ) -> SignalResult:
        channel_type = self._channel_types.get(channel_id, channel_type)
        if debounce_ms is None:
            motor_transition = any(
                token in channel_type for token in ("motor", "current", "stack_light", "stacklight")
            )
            debounce_ms = self._debounce_ms.get(
                channel_id,
                DEFAULT_MOTOR_DEBOUNCE_MS if motor_transition else DEFAULT_COUNTER_DEBOUNCE_MS,
            )
        debouncer = self._discrete.setdefault(channel_id, DiscreteDebouncer(debounce_ms))
        accepted = debouncer.update(value, timestamp_ms)
        return SignalResult(value=accepted, changed=accepted is not None)

    def update_analog_ct(
        self,
        channel_id: str,
        amps: float,
        timestamp_ms: float,
        *,
        debounce_ms: int | None = None,
    ) -> SignalResult:
        if debounce_ms is None:
            debounce_ms = self._debounce_ms.get(channel_id, DEFAULT_MOTOR_DEBOUNCE_MS)
        conditioner = self._analog.setdefault(
            channel_id,
            AnalogCTFilter(debounce_ms=debounce_ms),
        )
        return conditioner.update(amps, timestamp_ms)
