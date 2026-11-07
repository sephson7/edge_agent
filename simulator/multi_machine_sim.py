"""Publish simulated telemetry for every valid profile in a config directory."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from offline_buffer import OfflineBuffer
from oee import calculate_oee
from validator import MachineProfile
from watchdog import GatewayWatchdog, StaleSensorMonitor


LOGGER = logging.getLogger("multi_machine_sim")
SAMPLE_INTERVAL_SECONDS = 1
MICRO_STOP_SECONDS = 45


@dataclass(frozen=True)
class ConfiguredMachine:
    profile: MachineProfile
    metadata: dict[str, Any]

    @property
    def machine_id(self) -> str:
        return self.profile.machine_id

    @property
    def site_id(self) -> str:
        return self.profile.site_id


def _validate_topic_segment(value: str, field: str) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value):
        raise ValueError(f"{field} must be a non-empty MQTT topic segment")


def load_configured_machines(config_dir: Path) -> list[ConfiguredMachine]:
    """Load valid profiles, logging and skipping individual invalid files."""

    if not config_dir.is_dir():
        raise ValueError(f"Configuration directory does not exist: {config_dir}")

    machines: list[ConfiguredMachine] = []
    seen: set[tuple[str, str]] = set()
    for path in sorted(config_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("configuration root must be a JSON object")
            profile = MachineProfile.model_validate(data)
            machine_id = profile.machine_id or path.stem
            _validate_topic_segment(machine_id, "machine_id")
            _validate_topic_segment(profile.site_id, "site_id")
            if profile.machine_id and machine_id != path.stem:
                raise ValueError(
                    f"machine_id '{machine_id}' does not match configuration filename '{path.stem}.json'"
                )
            identity = (profile.site_id, machine_id)
            if identity in seen:
                raise ValueError(f"duplicate machine_id '{machine_id}' for site '{profile.site_id}'")
            profile = profile.model_copy(update={"machine_id": machine_id})
            metadata = _profile_metadata(profile)
            machines.append(ConfiguredMachine(profile=profile, metadata=metadata))
            seen.add(identity)
        except (OSError, UnicodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            LOGGER.error("Skipping invalid machine config %s: %s", path, exc)
    return machines


def _profile_metadata(profile: MachineProfile) -> dict[str, Any]:
    values = profile.model_dump(mode="json")
    keys = (
        "oem",
        "manufacturer",
        "make",
        "model",
        "model_number",
        "serial_number",
        "machine_type",
        "plc_family",
        "line",
    )
    metadata = {key: values[key] for key in keys if values.get(key) is not None}
    metadata["sensor_transport"] = profile.sensor_transport
    return metadata


def _cycles_per_minute(profile: MachineProfile) -> float:
    values = profile.model_dump(mode="python")
    for key in ("cycles_per_minute", "ideal_cycles_per_minute"):
        value = values.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value)
    cycle_time = values.get("ideal_cycle_time_seconds")
    if isinstance(cycle_time, (int, float)) and not isinstance(cycle_time, bool) and cycle_time > 0:
        return 60.0 / float(cycle_time)
    return 45.0


def _nominal_motor_amps(profile: MachineProfile) -> float:
    values = profile.model_dump(mode="python")
    for key in ("motor_full_load_amps", "motor_amperage", "motor_amps"):
        value = values.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value)
    return 12.0


class MachineSimulation:
    """Generate machine telemetry and deterministic OEE components."""

    def __init__(
        self,
        machine: ConfiguredMachine,
        seed: int | None = None,
    ) -> None:
        self.machine = machine
        self.cycles_per_minute = _cycles_per_minute(machine.profile)
        self.nominal_motor_amps = _nominal_motor_amps(machine.profile)
        self.random = random.Random(seed)
        self.elapsed_seconds = 0
        self.running_seconds = 0
        self.cycle_count = 0
        self.good_count = 0
        self._cycle_fraction = 0.0
        self._current_stop: str | None = "IDLE"
        self._stop_until = 5
        self._next_event_at = self._next_event_gap()

    def _next_event_gap(self) -> int:
        return self.random.randint(10 * 60, 30 * 60)

    def sample(self) -> tuple[str, dict[str, Any]]:
        state = "RUNNING"
        if self._current_stop is not None:
            if self.elapsed_seconds < self._stop_until:
                state = self._current_stop
            else:
                self._current_stop = None
                self._next_event_at = self.elapsed_seconds + self._next_event_gap()

        if self._current_stop is None and self.elapsed_seconds >= self._next_event_at:
            if self.random.random() < 0.08:
                self._current_stop = "FAULT"
                self._stop_until = self.elapsed_seconds + self.random.randint(30, 120)
            else:
                self._current_stop = "MICRO_STOP"
                self._stop_until = self.elapsed_seconds + MICRO_STOP_SECONDS
            state = self._current_stop

        if state == "RUNNING":
            self.running_seconds += SAMPLE_INTERVAL_SECONDS
            self._cycle_fraction += self.cycles_per_minute / 60.0
            completed = int(self._cycle_fraction)
            self._cycle_fraction -= completed
            self.cycle_count += completed
            for _ in range(completed):
                if self.random.random() >= 0.01:
                    self.good_count += 1
            motor_amps = self.nominal_motor_amps * (
                1.0 + math.sin(self.elapsed_seconds * (2 * math.pi / 8)) * 0.06
            ) + self.random.uniform(-self.nominal_motor_amps * 0.025, self.nominal_motor_amps * 0.025)
        else:
            motor_amps = self.random.uniform(0.08, 0.14) * self.nominal_motor_amps

        elapsed = max(self.elapsed_seconds + SAMPLE_INTERVAL_SECONDS, 1)
        oee_metrics = calculate_oee(
            planned_shift_seconds=elapsed,
            running_seconds=self.running_seconds,
            cycle_count=self.cycle_count,
            ideal_cycle_time_seconds=60.0 / self.cycles_per_minute if self.cycles_per_minute > 0 else None,
            good_count=self.good_count,
        )
        telemetry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "motor_amps": round(max(motor_amps, 0.0), 2),
            "cycle_count": self.cycle_count,
            "oee": round(oee_metrics.oee, 4),
            "availability": round(oee_metrics.availability, 4),
            "performance": round(oee_metrics.performance, 4),
            "quality": round(oee_metrics.quality, 4),
            "sensor_transport": self.machine.profile.sensor_transport,
        }
        self.elapsed_seconds += SAMPLE_INTERVAL_SECONDS
        return state, telemetry


def _publish_manifest(
    client: mqtt.Client,
    machines: list[ConfiguredMachine],
    buffer: OfflineBuffer | None = None,
) -> None:
    by_site: dict[str, list[dict[str, Any]]] = {}
    for machine in machines:
        by_site.setdefault(machine.site_id, []).append(
            {"machine_id": machine.machine_id, "metadata": machine.metadata}
        )
    for site_id, site_machines in by_site.items():
        payload = json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "machines": site_machines,
            },
            separators=(",", ":"),
        )
        topic = f"mca/{site_id}/fleet/manifest"
        if buffer:
            buffer.publish_or_queue(client, topic, payload, retain=True)
        else:
            result = client.publish(topic, payload=payload, qos=1, retain=True)
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError(f"MQTT publish failed for {topic}: error {result.rc}")


def _machine_loop(
    client: mqtt.Client,
    machine: ConfiguredMachine,
    seed: int | None,
    buffer: OfflineBuffer | None = None,
    sensor_monitor: StaleSensorMonitor | None = None,
    stop: threading.Event | None = None,
) -> None:
    seed_value = seed
    if seed_value is not None:
        seed_value += sum(ord(char) for char in f"{machine.site_id}/{machine.machine_id}")
    simulation = MachineSimulation(machine, seed=seed_value)
    base_topic = f"mca/{machine.site_id}/{machine.machine_id}"

    while stop is None or not stop.is_set():
        state, telemetry = simulation.sample()
        if sensor_monitor:
            sensor_monitor.record_report(f"{machine.site_id}/{machine.machine_id}")
        try:
            status_topic = f"{base_topic}/status"
            telemetry_topic = f"{base_topic}/telemetry"
            telemetry_payload = json.dumps(telemetry, separators=(",", ":"), allow_nan=False)
            if buffer:
                buffer.publish_or_queue(client, status_topic, state, retain=True)
                buffer.publish_or_queue(client, telemetry_topic, telemetry_payload, retain=False)
            else:
                for topic, payload, retain in (
                    (status_topic, state, True),
                    (telemetry_topic, telemetry_payload, False),
                ):
                    result = client.publish(topic, payload=payload, qos=1, retain=retain)
                    if result.rc != mqtt.MQTT_ERR_SUCCESS:
                        raise RuntimeError(f"MQTT publish failed for {topic}: error {result.rc}")
        except (OSError, RuntimeError, ValueError, mqtt.MQTTException):
            LOGGER.exception("Publish failed for machine %s; retrying next interval", machine.machine_id)
        time.sleep(SAMPLE_INTERVAL_SECONDS)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, default=Path("configs"))
    parser.add_argument(
        "--host",
        default=os.getenv("MQTT_BROKER_HOST", "localhost"),
        help="MQTT broker host (default: MQTT_BROKER_HOST or localhost)",
    )
    parser.add_argument(
        "--port",
        default=int(os.getenv("MQTT_BROKER_PORT", "1883")),
        type=int,
        help="MQTT broker port (default: MQTT_BROKER_PORT or 1883)",
    )
    parser.add_argument("--client-id", default="mca-edge-hub")
    parser.add_argument("--buffer-db", type=Path, default=Path("edge_buffer.db"))
    parser.add_argument("--stale-timeout-sec", type=float, default=60.0)
    parser.add_argument("--seed", type=int, help="Base seed for repeatable simulation")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if not 1 <= args.port <= 65535:
        LOGGER.error("MQTT port must be between 1 and 65535")
        return 2
    try:
        machines = load_configured_machines(args.config_dir)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 2
    if not machines:
        LOGGER.error("No valid machine profiles found in %s", args.config_dir)
        return 2

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=args.client_id,
    )

    def on_connect(
        _client: mqtt.Client,
        _userdata: Any,
        _flags: Any,
        reason_code: Any,
        _properties: Any,
    ) -> None:
        if reason_code == 0:
            LOGGER.info("Connected to MQTT broker at %s:%d", args.host, args.port)
        else:
            LOGGER.error("MQTT connection failed: %s", reason_code)

    client.on_connect = on_connect
    threads: list[threading.Thread] = []
    stop = threading.Event()
    buffer: OfflineBuffer | None = None
    watchdogs: list[GatewayWatchdog] = []
    sensor_monitor: StaleSensorMonitor | None = None
    stale_thread: threading.Thread | None = None
    try:
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.connect_async(args.host, args.port, keepalive=60)
        client.loop_start()
        buffer = OfflineBuffer(args.buffer_db)
        buffer.start(client)
        _publish_manifest(client, machines, buffer)
        machines_by_site: dict[str, list[ConfiguredMachine]] = {}
        for machine in machines:
            machines_by_site.setdefault(machine.site_id, []).append(machine)

        def sensor_transition(machine_id: str, status: str) -> None:
            site_id, _, local_machine_id = machine_id.partition("/")
            machine = next(
                (
                    item
                    for item in machines
                    if item.site_id == site_id and item.machine_id == local_machine_id
                ),
                None,
            )
            if not machine or not buffer:
                return
            topic = f"mca/{machine.site_id}/{machine.machine_id}/status"
            payload = "IDLE" if status == "RECOVERED" else "OFFLINE_SENSOR"
            try:
                buffer.publish_or_queue(client, topic, payload, retain=True)
            except (OSError, RuntimeError, ValueError):
                LOGGER.exception("Unable to publish stale-sensor transition for %s", machine_id)

        sensor_monitor = StaleSensorMonitor(
            (f"{machine.site_id}/{machine.machine_id}" for machine in machines),
            stale_timeout_sec=args.stale_timeout_sec,
            on_transition=sensor_transition,
        )

        def monitor_staleness() -> None:
            while not stop.wait(1):
                sensor_monitor.check()

        stale_thread = threading.Thread(target=monitor_staleness, name="sensor-stale-monitor", daemon=True)
        stale_thread.start()
        for site_id, site_machines in machines_by_site.items():
            gateway_watchdog = GatewayWatchdog(
                site_id=site_id,
                client=client,
                buffer=buffer,
                active_machines=lambda count=len(site_machines): count,
            )
            gateway_watchdog.start()
            watchdogs.append(gateway_watchdog)
        for machine in machines:
            thread = threading.Thread(
                target=_machine_loop,
                args=(client, machine, args.seed, buffer, sensor_monitor, stop),
                name=f"machine-{machine.machine_id}",
                daemon=True,
            )
            thread.start()
            threads.append(thread)
        LOGGER.info("Publishing telemetry for %d machines", len(threads))
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        LOGGER.info("Stopping multi-machine simulator")
    except (OSError, RuntimeError, ValueError, mqtt.MQTTException):
        LOGGER.exception("Unable to start multi-machine simulator")
        return 1
    finally:
        stop.set()
        if stale_thread:
            stale_thread.join(timeout=2)
        for gateway_watchdog in watchdogs:
            gateway_watchdog.close()
        for thread in threads:
            thread.join(timeout=2)
        if buffer:
            buffer.close()
        if client.is_connected():
            client.disconnect()
        client.loop_stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
