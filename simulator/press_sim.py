"""Publish simulated stamping-press telemetry to an MQTT broker."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import sys
import threading
import time
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from validator import MachineProfile


LOGGER = logging.getLogger("press_sim")
SAMPLE_INTERVAL_SECONDS = 1
MICRO_STOP_SECONDS = 45
MIN_MICRO_STOP_GAP_SECONDS = 10 * 60
MAX_MICRO_STOP_GAP_SECONDS = 30 * 60


def _topic_prefix(profile: MachineProfile) -> str:
    data = profile.model_dump(mode="python")
    for key in ("topic_prefix", "mqtt_topic", "base_topic"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            prefix = value.strip().strip("/")
            break
    else:
        machine = data.get("machine")
        if isinstance(machine, dict):
            machine = machine.get("name") or machine.get("id")
        segments = [
            str(data.get("site") or "demo"),
            str(data.get("line") or "production"),
            str(
                data.get("machine_name")
                or data.get("machine_id")
                or machine
                or data.get("name")
                or "press"
            ),
        ]
        prefix = "/".join(segment.strip().strip("/") for segment in segments)

    _validate_topic_prefix(prefix)
    return prefix


def _validate_topic_prefix(prefix: str) -> None:
    if not prefix or any(char in prefix for char in ("+", "#", "\0")):
        raise ValueError("MQTT topic prefix must be non-empty and cannot contain wildcards")


def _cycles_per_minute(profile: MachineProfile) -> float:
    data = profile.model_dump(mode="python")
    for key in ("cycles_per_minute", "ideal_cycles_per_minute"):
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value)
    cycle_time = data.get("ideal_cycle_time_seconds")
    if isinstance(cycle_time, (int, float)) and not isinstance(cycle_time, bool) and cycle_time > 0:
        return 60.0 / float(cycle_time)
    return 45.0


class PressSimulation:
    """Generate one-second press telemetry samples with occasional micro-stops."""

    def __init__(self, cycles_per_minute: float, seed: int | None = None) -> None:
        self.cycles_per_minute = cycles_per_minute
        self._random = random.Random(seed)
        self._elapsed_seconds = 0
        self._part_fraction = 0.0
        self._part_count = 0
        self._stopped_until: int | None = None
        self._next_micro_stop = self._next_gap()

    def _next_gap(self) -> int:
        return self._random.randint(MIN_MICRO_STOP_GAP_SECONDS, MAX_MICRO_STOP_GAP_SECONDS)

    def sample(self) -> dict[str, Any]:
        stopped = False
        if self._stopped_until is not None:
            if self._elapsed_seconds < self._stopped_until:
                stopped = True
            else:
                self._stopped_until = None
                self._next_micro_stop = self._elapsed_seconds + self._next_gap()

        if self._stopped_until is None and self._elapsed_seconds >= self._next_micro_stop:
            stopped = True
            self._stopped_until = self._elapsed_seconds + MICRO_STOP_SECONDS

        if stopped:
            state = "IDLE"
            amperage = self._random.uniform(1.2, 1.8)
        else:
            state = "RUNNING"
            amperage = (
                12.0
                + math.sin(self._elapsed_seconds * (2 * math.pi / 8)) * 0.8
                + self._random.uniform(-0.4, 0.4)
            )
            self._part_fraction += self.cycles_per_minute / 60.0
            completed_parts = int(self._part_fraction)
            self._part_count += completed_parts
            self._part_fraction -= completed_parts

        sample = {
            "state": state,
            "motor_amperage": round(amperage, 2),
            "part_count": self._part_count,
        }
        self._elapsed_seconds += SAMPLE_INTERVAL_SECONDS
        return sample


def publish_forever(
    client: mqtt.Client,
    topic_prefix: str,
    simulation: PressSimulation,
    connected: threading.Event,
) -> None:
    if not connected.wait(timeout=10):
        raise RuntimeError("Timed out waiting for MQTT broker connection")

    next_sample_at = time.monotonic()
    while True:
        sample = simulation.sample()
        for metric, value in sample.items():
            result = client.publish(
                f"{topic_prefix}/{metric}",
                payload=str(value),
                qos=0,
                retain=False,
            )
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError(f"Failed to publish {metric}: MQTT error {result.rc}")
        LOGGER.info(
            "state=%s motor_amperage=%.2f A part_count=%d",
            sample["state"],
            sample["motor_amperage"],
            sample["part_count"],
        )
        next_sample_at += SAMPLE_INTERVAL_SECONDS
        time.sleep(max(0.0, next_sample_at - time.monotonic()))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path, help="MachineProfile JSON file")
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
    parser.add_argument("--topic-prefix", help="Override the profile's MQTT topic prefix")
    parser.add_argument("--client-id", default="press-simulator")
    parser.add_argument("--seed", type=int, help="Seed the simulator's random values")
    parser.add_argument("--verbose", action="store_true", help="Log each published sample")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        profile_data = json.loads(args.profile.read_text(encoding="utf-8"))
        profile = MachineProfile.model_validate(profile_data)
        topic_prefix = args.topic_prefix.strip().strip("/") if args.topic_prefix else _topic_prefix(profile)
        _validate_topic_prefix(topic_prefix)
        if not 1 <= args.port <= 65535:
            raise ValueError("MQTT port must be between 1 and 65535")
        simulation = PressSimulation(_cycles_per_minute(profile), seed=args.seed)
    except (OSError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        LOGGER.error("Invalid machine profile or simulator configuration: %s", exc)
        return 2

    connected = threading.Event()
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
            connected.set()
            LOGGER.info("Connected to MQTT broker at %s:%d", args.host, args.port)
        else:
            LOGGER.error("MQTT connection failed: %s", reason_code)

    client.on_connect = on_connect
    try:
        client.connect(args.host, args.port, keepalive=60)
        client.loop_start()
        publish_forever(client, topic_prefix, simulation, connected)
    except KeyboardInterrupt:
        LOGGER.info("Stopping press simulator")
    except (OSError, RuntimeError, ValueError, mqtt.MQTTException) as exc:
        LOGGER.error("%s", exc)
        return 1
    finally:
        if client.is_connected():
            client.disconnect()
        client.loop_stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
