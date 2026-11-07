from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import upload_server


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    runtime = upload_server.runtime
    runtime.shutdown()
    monkeypatch.setattr(runtime, "root", tmp_path)
    monkeypatch.setattr(runtime, "_ensure_mqtt", lambda: None)
    monkeypatch.setattr(runtime, "publish", lambda *_args, **_kwargs: None)
    runtime.sites = {}
    runtime.simulators_enabled = {}
    with TestClient(upload_server.app, follow_redirects=False) as test_client:
        yield test_client
    runtime.shutdown()


def _template_config(machine_id: str, site_id: str) -> dict[str, Any]:
    template = Path(upload_server.__file__).resolve().parent / "templates" / "stamping_press.json"
    config = json.loads(template.read_text(encoding="utf-8"))
    config.update({"machine_id": machine_id, "site_id": site_id})
    return config


def test_site_registry_and_dashboard_are_preseeded(client: TestClient) -> None:
    sites = client.get("/api/sites").json()
    assert sites["default_site_id"] == "highland_park_plant_01"
    assert sites["sites"][0] == {
        "site_id": "highland_park_plant_01",
        "name": "Highland Park Plant 01",
        "location": "Highland Park, MI",
    }

    dashboard = client.get("/dashboard?site_id=highland_park_plant_01")
    assert dashboard.status_code == 200
    assert "MCA Machine Health" in dashboard.text
    assert "100% READ-ONLY DIAGNOSTIC" in dashboard.text
    assert "Central Hub" in dashboard.text


def test_approval_persists_config_and_starts_site_scoped_telemetry(client: TestClient) -> None:
    site = client.post(
        "/api/sites",
        json={"site_id": "plant_alpha", "name": "Plant Alpha", "location": "Detroit, MI"},
    )
    assert site.status_code == 201

    response = client.post(
        "/api/sites/plant_alpha/approve",
        json={
            "config": _template_config("press_01", "plant_alpha"),
            "ideal_cycle_time_seconds": 2.0,
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard?site_id=plant_alpha"

    deployed_path = upload_server.runtime.deployed_dir / "plant_alpha" / "press_01.json"
    deployed = json.loads(deployed_path.read_text(encoding="utf-8"))
    assert deployed["approved_by"] == "human"
    assert deployed["approved_at"]
    assert deployed["ideal_cycle_time_seconds"] == 2.0
    assert deployed["site_id"] == "plant_alpha"

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        telemetry = client.get("/api/sites/plant_alpha/telemetry").json()
        if telemetry["machines"] and telemetry["machines"][0]["telemetry"]:
            break
        time.sleep(0.02)
    assert [machine["machine_id"] for machine in telemetry["machines"]] == ["press_01"]
    assert client.get("/api/sites/highland_park_plant_01/telemetry").json()["machines"] == []


@pytest.mark.parametrize(
    ("config_update", "message"),
    [
        ({"template_locked": False}, "template_locked"),
        ({"channels": [{"channel_id": "DI1", "read_only": False}]}, "read_only"),
    ],
)
def test_approval_rejects_unsafe_profiles(
    client: TestClient,
    config_update: dict[str, Any],
    message: str,
) -> None:
    config = _template_config("unsafe_press", "highland_park_plant_01")
    config.update(config_update)
    response = client.post(
        "/api/sites/highland_park_plant_01/approve",
        json={"config": config},
    )
    assert response.status_code == 422
    assert message in response.json()["detail"]
