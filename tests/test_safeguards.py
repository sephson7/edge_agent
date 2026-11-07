from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from oee import calculate_oee
from offline_buffer import OfflineBuffer
from signal_filter import AnalogCTFilter, DiscreteDebouncer, SignalFilter
from validator import MachineProfile
from watchdog import GatewayWatchdog, StaleSensorMonitor
from agent import NameplateExtraction, main, merge_extraction_with_template


class FakePublishResult:
    rc = 0

    def wait_for_publish(self, timeout: float | None = None) -> None:
        return None

    def is_published(self) -> bool:
        return True


class FakeMQTTClient:
    def __init__(self) -> None:
        self.connected = False
        self.published: list[tuple[str, str, int, bool]] = []

    def is_connected(self) -> bool:
        return self.connected

    def publish(
        self,
        topic: str,
        *,
        payload: str,
        qos: int,
        retain: bool,
    ) -> FakePublishResult:
        self.published.append((topic, payload, qos, retain))
        return FakePublishResult()


def test_five_millisecond_counter_bounces_only_count_one_stable_part() -> None:
    counter = DiscreteDebouncer(debounce_ms=50)
    counted_parts = 0

    for timestamp in range(0, 55, 5):
        accepted = counter.update(False, timestamp)
        if accepted is True:
            counted_parts += 1

    bounce_samples = [
        (55, True),
        (60, False),
        (65, True),
        (70, False),
        (75, True),
        (80, False),
        (85, True),
    ]
    for timestamp, value in bounce_samples:
        accepted = counter.update(value, timestamp)
        if accepted is True:
            counted_parts += 1

    for timestamp in range(90, 146, 5):
        accepted = counter.update(True, timestamp)
        if accepted is True:
            counted_parts += 1

    assert counted_parts == 1
    assert counter.stable_value is True


def test_profile_channel_debounce_defaults_and_overrides() -> None:
    filters = SignalFilter.from_channels(
        [
            {"channel_id": "DI1", "channel_type": "part_counter"},
            {"channel_id": "AI1", "channel_type": "motor_current", "debounce_ms": 400},
        ]
    )
    counter_result = filters.update_discrete("DI1", True, 0)
    assert counter_result.value is None
    assert filters.update_discrete("DI1", True, 49).value is None
    assert filters.update_discrete("DI1", True, 50).value is True

    motor_result = filters.update_discrete("AI1", True, 0)
    assert motor_result.value is None
    assert filters.update_discrete("AI1", True, 399).value is None
    assert filters.update_discrete("AI1", True, 400).value is True


def test_signal_filter_reads_debounce_configuration_from_machine_profile() -> None:
    profile = MachineProfile.model_validate(
        {
            "channels": [
                {"channel_id": "DI1", "name": "main_motor_ct", "debounce_ms": 400, "read_only": True},
            ],
            "downtime_codes": ["Maintenance"],
        }
    )
    filters = SignalFilter.from_channels(profile.channels)
    assert filters.update_discrete("DI1", True, 0).value is None
    assert filters.update_discrete("DI1", True, 399).value is None
    assert filters.update_discrete("DI1", True, 400).value is True


def test_ct_filter_flags_out_of_range_and_suppresses_spikes() -> None:
    conditioner = AnalogCTFilter(debounce_ms=250)
    assert conditioner.update(10.0, 0).value == 10.0
    assert conditioner.update(10.2, 100).value == 10.1
    spike = conditioner.update(100.0, 200)
    assert spike.value == 10.1
    assert conditioner.update(-0.1, 300).fault == "SENSOR_FAULT"
    assert conditioner.update(500.1, 400).fault == "SENSOR_FAULT"


def test_mqtt_disconnect_queues_then_replays_messages_fifo(tmp_path: Path) -> None:
    database = tmp_path / "edge_buffer.db"
    client = FakeMQTTClient()
    timestamp = "2026-01-02T03:04:05-05:00"
    with OfflineBuffer(database, batch_size=50) as buffer:
        first_id = buffer.publish_or_queue(
            client,
            "mca/plant_01/press_01/status",
            "RUNNING",
            retain=True,
            timestamp_utc=timestamp,
        )
        second_id = buffer.publish_or_queue(
            client,
            "mca/plant_01/press_01/telemetry",
            {"cycle_count": 12},
            timestamp_utc="2026-01-02T08:04:06Z",
        )
        assert first_id < second_id
        assert buffer.pending_count() == 2
        assert client.published == []

        with sqlite3.connect(database) as connection:
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            row_timestamp = connection.execute(
                "SELECT timestamp_utc FROM outbound_messages WHERE id = ?", (first_id,)
            ).fetchone()[0]
        assert journal_mode.lower() == "wal"
        assert row_timestamp == "2026-01-02T08:04:05+00:00"

        client.connected = True
        assert buffer.drain_once(client) == 2
        assert buffer.pending_count() == 0
        assert [message[0] for message in client.published] == [
            "mca/plant_01/press_01/status",
            "mca/plant_01/press_01/telemetry",
        ]
        assert all(message[2] == 1 for message in client.published)


@pytest.mark.parametrize(
    "values",
    [
        {
            "planned_shift_seconds": 0,
            "running_seconds": 0,
            "cycle_count": 0,
            "ideal_cycle_time_seconds": None,
            "good_count": 0,
        },
        {
            "planned_shift_seconds": float("nan"),
            "running_seconds": float("inf"),
            "cycle_count": float("nan"),
            "ideal_cycle_time_seconds": 0,
            "good_count": float("inf"),
        },
        {
            "planned_shift_seconds": 60,
            "running_seconds": 120,
            "cycle_count": 1000,
            "ideal_cycle_time_seconds": 0.1,
            "good_count": 1200,
        },
    ],
)
def test_oee_is_finite_and_clamped(values: dict[str, Any]) -> None:
    metrics = calculate_oee(**values)
    for value in (metrics.availability, metrics.performance, metrics.quality, metrics.oee):
        assert value == value
        assert 0.0 <= value <= 1.0


def test_oee_missing_ideal_cycle_time_never_divides_by_zero() -> None:
    metrics = calculate_oee(
        planned_shift_seconds=3600,
        running_seconds=1800,
        cycle_count=20,
        ideal_cycle_time_seconds=None,
        good_count=18,
    )
    assert metrics.availability == 0.5
    assert metrics.performance == 0.0
    assert metrics.quality == 0.9
    assert metrics.oee == 0.0


def test_stale_sensor_transitions_offline_then_recovers() -> None:
    now = [100.0]
    transitions: list[tuple[str, str]] = []
    monitor = StaleSensorMonitor(
        ["plant_01/press_01"],
        stale_timeout_sec=60,
        on_transition=lambda machine_id, status: transitions.append((machine_id, status)),
        clock=lambda: now[0],
    )
    now[0] = 160.0
    assert monitor.check() == []
    now[0] = 160.1
    assert monitor.check() == ["plant_01/press_01"]
    assert monitor.is_offline("plant_01/press_01")
    monitor.record_report("plant_01/press_01")
    assert not monitor.is_offline("plant_01/press_01")
    assert transitions == [
        ("plant_01/press_01", "OFFLINE_SENSOR"),
        ("plant_01/press_01", "RECOVERED"),
    ]


def test_gateway_health_contains_required_health_metrics(tmp_path: Path) -> None:
    client = FakeMQTTClient()
    with OfflineBuffer(tmp_path / "health.db") as buffer:
        watchdog = GatewayWatchdog(
            site_id="plant_01",
            client=client,  # type: ignore[arg-type]
            buffer=buffer,
            active_machines=lambda: 4,
            disk_path=tmp_path,
        )
        payload = watchdog.payload()
        assert payload["uptime_sec"] >= 0
        assert payload["disk_free_mb"] > 0
        assert payload["buffered_messages_count"] == 0
        assert payload["active_machines"] == 4
        assert payload["read_only_verified"] is True
        watchdog.publish_once()
        assert buffer.pending_count() == 1


@pytest.mark.parametrize(
    "archetype",
    [
        "stamping_press",
        "cnc_mill",
        "injection_molder",
        "hydraulic_press",
        "conveyor_line",
        "bottling_filler_line",
        "food_packaging_wrapper",
        "industrial_oven_mixer",
    ],
)
def test_golden_archetype_merge_is_valid_and_read_only(archetype: str) -> None:
    expected_channels = {
        "bottling_filler_line": ["main_drive_ct", "bottle_optical_counter", "fault_stack_light"],
        "food_packaging_wrapper": ["main_drive_ct", "finished_pack_photoeye", "fault_stack_light"],
        "industrial_oven_mixer": ["mixer_drive_ct", "batch_cycle_pulse", "high_temp_burner_fault"],
    }
    expected_debounce = {
        "bottling_filler_line": [250, 20, 100],
        "food_packaging_wrapper": [250, 30, 100],
        "industrial_oven_mixer": [250, 100, 100],
    }
    extraction = NameplateExtraction(
        archetype=archetype,
        oem="Example OEM",
        model="Model 100",
        serial_number="SN-123",
        voltage="480V",
        plc_family="Allen-Bradley SLC 500",
        confidence_notes="Voltage was legible.",
    )
    profile = merge_extraction_with_template(
        extraction,
        machine_id="test_machine",
        site_id="plant_01",
        sensor_transport="wireless_900mhz",
    )

    assert profile.archetype == archetype
    assert profile.machine_id == "test_machine"
    assert profile.site_id == "plant_01"
    assert profile.sensor_transport == "wireless_900mhz"
    assert profile.oem == "Example OEM"
    assert profile.model == "Model 100"
    assert profile.serial_number == "SN-123"
    assert profile.voltage == "480V"
    assert profile.plc_family == "Allen-Bradley SLC 500"
    assert profile.template_locked is True
    assert [channel.channel_id for channel in profile.channels] == ["DI1", "DI2", "DI3"]
    assert all(channel.read_only is True for channel in profile.channels)
    if archetype in expected_channels:
        assert [channel.name for channel in profile.channels] == expected_channels[archetype]
        assert [channel.debounce_ms for channel in profile.channels] == expected_debounce[archetype]
    else:
        assert [channel.name for channel in profile.channels] == [
            "main_motor_ct",
            "part_counter",
            "fault_stack_light",
        ]
        assert [channel.debounce_ms for channel in profile.channels] == [250, 50, 250]
    assert len(profile.downtime_codes) == 6


def test_nameplate_extraction_forbids_generated_configuration() -> None:
    extraction = NameplateExtraction(
        archetype="stamping_press",
        oem="Minster",
        model="P2-100",
        voltage="480V",
        confidence_notes="Serial number illegible.",
    )
    assert extraction.serial_number is None
    assert extraction.plc_family is None
    with pytest.raises(ValueError):
        NameplateExtraction.model_validate(
            {**extraction.model_dump(), "channels": [{"read_only": True}]}
        )


def test_nameplate_schema_omits_gemini_unsupported_additional_properties() -> None:
    def contains_additional_properties(value: Any) -> bool:
        if isinstance(value, dict):
            return any(
                key in {"additionalProperties", "additional_properties"}
                or contains_additional_properties(child)
                for key, child in value.items()
            )
        if isinstance(value, list):
            return any(contains_additional_properties(item) for item in value)
        return False

    assert not contains_additional_properties(NameplateExtraction.model_json_schema())


def test_dry_run_generates_profile_without_api_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["agent.py", "--dry-run", "--machine-id", "dry_run_press"])

    assert main() == 0
    output_path = tmp_path / "configs" / "dry_run_press.json"
    assert output_path.exists()
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["archetype"] == "stamping_press"
    assert payload["oem"] == "Minster"
    assert payload["machine_id"] == "dry_run_press"
    assert payload["sensor_transport"] == "wired"


def test_machine_profile_rejects_duplicate_channel_ids_and_invalid_transports() -> None:
    with pytest.raises(ValueError):
        MachineProfile.model_validate(
            {
                "machine_id": "press_01",
                "channels": [
                    {"channel_id": "DI1", "read_only": True},
                    {"channel_id": "DI1", "read_only": True},
                ],
                "downtime_codes": ["Die Jam"],
            }
        )

    with pytest.raises(ValueError):
        MachineProfile.model_validate(
            {
                "machine_id": "press_01",
                "sensor_transport": "wireless_5g",
                "channels": [{"channel_id": "DI1", "read_only": True}],
                "downtime_codes": ["Die Jam"],
            }
        )

    with pytest.raises(ValueError):
        MachineProfile.model_validate(
            {
                "machine_id": "press_01",
                "channels": [{"channel_id": "DI1"}],
                "downtime_codes": ["Die Jam"],
            }
        )
