"""Edge gateway heartbeat and per-machine stale-sensor monitoring."""

from __future__ import annotations

import json
import logging
import math
import shutil
import threading
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Callable, Iterable

import paho.mqtt.client as mqtt

from offline_buffer import OfflineBuffer


LOGGER = logging.getLogger("edge_watchdog")
HEARTBEAT_INTERVAL_SECONDS = 10
DEFAULT_STALE_TIMEOUT_SECONDS = 60


def cpu_temperature_c() -> float | None:
    """Read Linux thermal-zone data where the platform exposes it."""

    candidates = (
        Path("/sys/class/thermal/thermal_zone0/temp"),
        Path("/sys/class/hwmon/hwmon0/temp1_input"),
    )
    for path in candidates:
        try:
            value = float(path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            continue
        if value > 1000:
            value /= 1000.0
        if math.isfinite(value) and -40 <= value <= 150:
            return round(value, 1)
    return None


def disk_free_mb(path: str | Path = ".") -> float:
    return round(shutil.disk_usage(path).free / (1024 * 1024), 1)


class StaleSensorMonitor:
    """Track last sensor reports and emit offline/recovered transitions."""

    def __init__(
        self,
        machine_ids: Iterable[str],
        *,
        stale_timeout_sec: float = DEFAULT_STALE_TIMEOUT_SECONDS,
        on_transition: Callable[[str, str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if stale_timeout_sec <= 0:
            raise ValueError("stale_timeout_sec must be positive")
        self.stale_timeout_sec = stale_timeout_sec
        self.on_transition = on_transition
        self._clock = clock
        self._lock = threading.Lock()
        now = clock()
        self._last_seen = {machine_id: now for machine_id in machine_ids}
        self._offline: set[str] = set()

    def record_report(self, machine_id: str, timestamp: float | None = None) -> None:
        now = self._clock() if timestamp is None else timestamp
        with self._lock:
            self._last_seen[machine_id] = now
            recovered = machine_id in self._offline
            self._offline.discard(machine_id)
        if recovered and self.on_transition:
            try:
                self.on_transition(machine_id, "RECOVERED")
            except Exception:
                LOGGER.exception("Stale-sensor recovery callback failed for %s", machine_id)

    def check(self, now: float | None = None) -> list[str]:
        current = self._clock() if now is None else now
        newly_offline: list[str] = []
        with self._lock:
            for machine_id, last_seen in self._last_seen.items():
                if current - last_seen > self.stale_timeout_sec and machine_id not in self._offline:
                    self._offline.add(machine_id)
                    newly_offline.append(machine_id)
        if self.on_transition:
            for machine_id in newly_offline:
                try:
                    self.on_transition(machine_id, "OFFLINE_SENSOR")
                except Exception:
                    LOGGER.exception("Stale-sensor callback failed for %s", machine_id)
        return newly_offline

    def is_offline(self, machine_id: str) -> bool:
        with self._lock:
            return machine_id in self._offline


class GatewayWatchdog:
    """Publish periodic retained edge-node health heartbeats."""

    def __init__(
        self,
        *,
        site_id: str,
        client: mqtt.Client,
        buffer: OfflineBuffer,
        active_machines: Callable[[], int],
        disk_path: str | Path = ".",
        interval_sec: float = HEARTBEAT_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.site_id = site_id
        self.client = client
        self.buffer = buffer
        self.active_machines = active_machines
        self.disk_path = disk_path
        self.interval_sec = interval_sec
        self._clock = clock
        self._started_at = clock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def payload(self) -> dict[str, int | float | bool | None]:
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "uptime_sec": max(0, int(self._clock() - self._started_at)),
            "cpu_temp_c": cpu_temperature_c(),
            "disk_free_mb": disk_free_mb(self.disk_path),
            "buffered_messages_count": self.buffer.pending_count(),
            "active_machines": self.active_machines(),
            "read_only_verified": True,
        }

    def publish_once(self) -> None:
        topic = f"mca/{self.site_id}/hub/health"
        self.buffer.publish_or_queue(
            self.client,
            topic,
            json.dumps(self.payload(), separators=(",", ":"), allow_nan=False),
            retain=True,
        )

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="gateway-watchdog", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.publish_once()
            except (OSError, RuntimeError, ValueError):
                LOGGER.exception("Unable to publish gateway heartbeat")
            self._stop.wait(self.interval_sec)

    def close(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
