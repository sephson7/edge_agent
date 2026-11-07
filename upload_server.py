"""Self-contained, read-only multi-tenant onboarding and machine-health HMI."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import math
import os
import re
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field, ValidationError

from simulator.multi_machine_sim import ConfiguredMachine, MachineSimulation
from validator import MachineProfile


ROOT = Path(__file__).resolve().parent
DEFAULT_SITE_ID = "highland_park_plant_01"
DEFAULT_SITES = {
    DEFAULT_SITE_ID: {
        "name": "Highland Park Plant 01",
        "location": "Highland Park, MI",
    }
}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}
SITE_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
LOGGER = logging.getLogger("upload_server")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_site_id(site_id: str | None) -> str:
    value = (site_id or "").strip().lower()
    if not value or value == "default":
        return DEFAULT_SITE_ID
    if not SITE_ID_PATTERN.fullmatch(value):
        raise ValueError("site_id must be a lowercase MQTT-safe identifier")
    return value


class SiteCreate(BaseModel):
    site_id: str
    name: str = Field(min_length=1, max_length=120)
    location: str = Field(default="", max_length=160)


class ApprovalRequest(BaseModel):
    config: dict[str, Any]
    ideal_cycle_time_seconds: float | None = None


class SimulatorRequest(BaseModel):
    enabled: bool


class MachineWorker:
    """Keep one site's simulation and latest read-only snapshot isolated."""

    def __init__(
        self,
        *,
        site_id: str,
        machine_id: str,
        profile: MachineProfile,
        publish: Any,
    ) -> None:
        self.site_id = site_id
        self.machine_id = machine_id
        self.profile = profile
        self.publish = publish
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name=f"telemetry-{site_id}-{machine_id}",
            daemon=True,
        )
        self._snapshot_lock = threading.Lock()
        self.snapshot: dict[str, Any] = {
            "site_id": site_id,
            "machine_id": machine_id,
            "status": "STARTING",
            "telemetry": {},
            "downtime_seconds": 0,
            "updated_at": _utc_now(),
        }

    def start(self) -> None:
        if not self.thread.is_alive():
            self.thread.start()

    def close(self) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=2)

    def current(self) -> dict[str, Any]:
        with self._snapshot_lock:
            return dict(self.snapshot)

    def _run(self) -> None:
        configured = ConfiguredMachine(
            profile=self.profile,
            metadata={
                key: value
                for key, value in self.profile.model_dump(mode="json").items()
                if key in {"oem", "model", "serial_number", "machine_type", "plc_family"}
                and value is not None
            },
        )
        simulator = MachineSimulation(configured)
        downtime_started: float | None = None
        last_status: str | None = None
        base_topic = f"mca/{self.site_id}/{self.machine_id}"
        while not self.stop_event.is_set():
            status, telemetry = simulator.sample()
            now = time.monotonic()
            if status == "RUNNING":
                downtime_started = None
                downtime_seconds = 0
            else:
                if last_status == "RUNNING" or downtime_started is None:
                    downtime_started = now
                downtime_seconds = max(0, int(now - downtime_started))
            last_status = status
            snapshot = {
                "site_id": self.site_id,
                "machine_id": self.machine_id,
                "status": status,
                "telemetry": telemetry,
                "downtime_seconds": downtime_seconds,
                "updated_at": _utc_now(),
            }
            with self._snapshot_lock:
                self.snapshot = snapshot
            self.publish(f"{base_topic}/status", status, retain=True)
            self.publish(
                f"{base_topic}/telemetry",
                json.dumps(telemetry, separators=(",", ":"), allow_nan=False),
                retain=False,
            )
            self.stop_event.wait(1)


class RegistryRuntime:
    """Own site persistence, per-site telemetry workers, and optional MQTT output."""

    def __init__(self, root: Path = ROOT) -> None:
        self.root = root
        self._lock = threading.RLock()
        self.sites: dict[str, dict[str, str]] = {}
        self.workers: dict[tuple[str, str], MachineWorker] = {}
        self.simulators_enabled: dict[str, bool] = {}
        self.mqtt_client: mqtt.Client | None = None
        self.started = False
        self.initialize_registry()

    @property
    def deployed_dir(self) -> Path:
        return self.root / "deployed_configs"

    @property
    def sites_path(self) -> Path:
        return self.deployed_dir / "sites.json"

    def initialize_registry(self) -> None:
        self.deployed_dir.mkdir(parents=True, exist_ok=True)
        if self.sites_path.exists():
            data = json.loads(self.sites_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError(f"Site registry must be a JSON object: {self.sites_path}")
            self.sites = {}
            for site_id, record in data.items():
                canonical = _canonical_site_id(site_id)
                if not isinstance(record, dict) or not isinstance(record.get("name"), str):
                    raise ValueError(f"Invalid site registry record for '{site_id}'")
                self.sites[canonical] = {
                    "name": record["name"],
                    "location": str(record.get("location", "")),
                }
        else:
            self.sites = dict(DEFAULT_SITES)
            self._write_sites()
        if DEFAULT_SITE_ID not in self.sites:
            self.sites[DEFAULT_SITE_ID] = dict(DEFAULT_SITES[DEFAULT_SITE_ID])
            self._write_sites()

    def _write_sites(self) -> None:
        self.deployed_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.sites_path.with_name(f".{self.sites_path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(self.sites, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.sites_path)

    def add_site(self, site_id: str, name: str, location: str) -> dict[str, str]:
        try:
            canonical = _canonical_site_id(site_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        with self._lock:
            if canonical in self.sites:
                existing = self.sites[canonical]
                if existing["name"] != name or existing["location"] != location:
                    raise HTTPException(status_code=409, detail=f"Site '{canonical}' already exists.")
                return {"site_id": canonical, **existing}
            record = {"name": name.strip(), "location": location.strip()}
            if not record["name"]:
                raise HTTPException(status_code=422, detail="Site name must not be empty.")
            self.sites[canonical] = record
            self.simulators_enabled.setdefault(canonical, True)
            self._write_sites()
            return {"site_id": canonical, **record}

    def _ensure_mqtt(self) -> None:
        if self.mqtt_client is not None:
            return
        host = os.getenv("MQTT_BROKER_HOST", "localhost")
        try:
            client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=f"mca-hmi-{uuid.uuid4().hex[:12]}",
            )
            client.reconnect_delay_set(min_delay=1, max_delay=30)
            client.connect_async(host, int(os.getenv("MQTT_BROKER_PORT", "1883")), keepalive=60)
            client.loop_start()
            self.mqtt_client = client
        except (OSError, ValueError, mqtt.MQTTException):
            LOGGER.exception("MQTT is unavailable; HMI telemetry remains available locally.")

    def publish(self, topic: str, payload: str, *, retain: bool) -> None:
        client = self.mqtt_client
        if client is None:
            return
        try:
            result = client.publish(topic, payload=payload, qos=1, retain=retain)
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                LOGGER.debug("MQTT publish deferred for %s (result %s)", topic, result.rc)
        except (OSError, RuntimeError, ValueError, mqtt.MQTTException):
            LOGGER.exception("MQTT publish failed for %s", topic)

    def start_worker(self, site_id: str, profile: MachineProfile) -> None:
        machine_id = profile.machine_id
        key = (site_id, machine_id)
        with self._lock:
            existing = self.workers.get(key)
            if existing is not None:
                existing.close()
            self._ensure_mqtt()
            worker = MachineWorker(
                site_id=site_id,
                machine_id=machine_id,
                profile=profile,
                publish=self.publish,
            )
            self.workers[key] = worker
            worker.start()

    def set_simulator(self, site_id: str, enabled: bool) -> None:
        with self._lock:
            self.simulators_enabled[site_id] = enabled
            matching = [
                (key, worker)
                for key, worker in self.workers.items()
                if key[0] == site_id
            ]
            if enabled:
                for (_site_id, _machine_id), worker in matching:
                    if not worker.thread.is_alive():
                        replacement = MachineWorker(
                            site_id=site_id,
                            machine_id=worker.machine_id,
                            profile=worker.profile,
                            publish=self.publish,
                        )
                        self.workers[(site_id, worker.machine_id)] = replacement
                        replacement.start()
            else:
                for _key, worker in matching:
                    worker.close()

    def telemetry_for_site(self, site_id: str) -> dict[str, Any]:
        with self._lock:
            site_workers = [
                worker.current()
                for (worker_site, _machine_id), worker in self.workers.items()
                if worker_site == site_id
            ]
            enabled = self.simulators_enabled.get(site_id, True)
        metrics = []
        for snapshot in site_workers:
            profile_path = self.deployed_dir / site_id / f"{snapshot['machine_id']}.json"
            try:
                profile = json.loads(profile_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                LOGGER.exception("Unable to read deployed profile %s", profile_path)
                continue
            telemetry = snapshot["telemetry"]
            metrics.append(
                {
                    **snapshot,
                    "oem": profile.get("oem") or profile.get("manufacturer") or profile.get("make") or "Unknown OEM",
                    "model": profile.get("model") or profile.get("model_number") or "Unknown Model",
                    "archetype": profile.get("archetype", "unknown"),
                    "cycle_count": telemetry.get("cycle_count", 0),
                    "motor_amps": telemetry.get("motor_amps", 0),
                    "oee": telemetry.get("oee", 0),
                    "availability": telemetry.get("availability", 0),
                    "performance": telemetry.get("performance", 0),
                    "quality": telemetry.get("quality", 0),
                }
            )
        active = sum(item["status"] == "RUNNING" for item in metrics)
        fleet_oee = (
            sum(float(item["oee"]) for item in metrics) / len(metrics)
            if metrics
            else 0.0
        )
        return {
            "site_id": site_id,
            "machines": metrics,
            "summary": {
                "connected_assets": len(metrics),
                "running": active,
                "fleet_oee": round(fleet_oee, 4),
                "shift_output": sum(int(item["cycle_count"]) for item in metrics),
            },
            "hub": {
                "status": "ONLINE",
                "active_machines": len(metrics),
                "read_only_verified": True,
            },
            "simulator_enabled": enabled,
            "updated_at": _utc_now(),
        }

    def startup(self) -> None:
        if self.started:
            return
        self.initialize_registry()
        for site_id in self.sites:
            self.simulators_enabled.setdefault(site_id, True)
            site_dir = self.deployed_dir / site_id
            if not site_dir.is_dir():
                continue
            for config_path in sorted(site_dir.glob("*.json")):
                try:
                    config = json.loads(config_path.read_text(encoding="utf-8"))
                    if not isinstance(config, dict):
                        raise ValueError("configuration root must be an object")
                    profile = MachineProfile.model_validate(config)
                    if profile.machine_id != config_path.stem or profile.site_id != site_id:
                        raise ValueError("deployed config identity does not match its site/path")
                    self._assert_approved_config(config)
                    self.start_worker(site_id, profile)
                except (OSError, json.JSONDecodeError, ValidationError, ValueError):
                    LOGGER.exception("Skipping invalid deployed configuration %s", config_path)
        self.started = True

    def shutdown(self) -> None:
        with self._lock:
            workers = list(self.workers.values())
            self.workers.clear()
        for worker in workers:
            worker.close()
        if self.mqtt_client is not None:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
            self.mqtt_client = None
        self.started = False

    @staticmethod
    def _assert_approved_config(config: dict[str, Any]) -> None:
        if config.get("template_locked") is not True:
            raise ValueError("Configuration must set template_locked to true.")
        channels = config.get("channels")
        if isinstance(channels, dict):
            channel_values = list(channels.values())
        elif isinstance(channels, list):
            channel_values = channels
        else:
            raise ValueError("Configuration must contain read-only channels.")
        if not channel_values:
            raise ValueError("Configuration must contain at least one read-only channel.")
        for channel in channel_values:
            if not isinstance(channel, dict) or channel.get("read_only") is not True:
                raise ValueError("Every channel must set read_only to true.")


runtime = RegistryRuntime()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    runtime.startup()
    try:
        yield
    finally:
        runtime.shutdown()


app = FastAPI(title="MCA Machine Health", lifespan=lifespan)


def _site_id_or_404(site_id: str) -> str:
    try:
        canonical = _canonical_site_id(site_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if canonical not in runtime.sites:
        raise HTTPException(status_code=404, detail=f"Unknown site '{canonical}'.")
    return canonical


def _site_options(selected: str) -> str:
    options = []
    for site_id, record in sorted(runtime.sites.items()):
        mark = " selected" if site_id == selected else ""
        options.append(
            f'<option value="{html.escape(site_id, quote=True)}"{mark}>'
            f'{html.escape(record["name"])} — {html.escape(site_id)}</option>'
        )
    return "\n".join(options)


def _onboarding_page(
    *,
    machine_id: str = "",
    selected_site: str = DEFAULT_SITE_ID,
    config: dict[str, Any] | None = None,
    error: str | None = None,
) -> HTMLResponse:
    timestamp_id = f"machine_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}"
    machine_value = html.escape(machine_id or timestamp_id, quote=True)
    options = _site_options(selected_site)
    result = ""
    if config is not None:
        rendered_config = html.escape(json.dumps(config, indent=2, ensure_ascii=False))
        cycle_time = config.get("ideal_cycle_time_seconds", "")
        result = f"""
        <section class="panel result">
          <h2>Validated Machine Configuration</h2>
          <p>Review the generated JSON and optionally adjust ideal cycle time before human approval.</p>
          <label for="ideal-cycle">Ideal cycle time (seconds, optional override)</label>
          <input id="ideal-cycle" type="number" min="0.001" max="86400" step="any" value="{html.escape(str(cycle_time), quote=True)}">
          <textarea id="validated-config" spellcheck="false">{rendered_config}</textarea>
          <input type="hidden" id="approved-site-id" value="{html.escape(str(config["site_id"]), quote=True)}">
          <button id="approve-button" class="primary" type="button">Confirm &amp; Deploy Machine</button>
          <p id="approval-message" role="status"></p>
        </section>"""
    elif error:
        result = f'<section class="panel error"><h2>Onboarding failed</h2><pre>{html.escape(error)}</pre></section>'
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Technician Onboarding | MCA Machine Health</title>
<style>
:root{{--bg:#0b0f14;--panel:#131922;--line:#253140;--text:#e7edf4;--muted:#9aa8b8;--green:#21c77a;--blue:#2875d2}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,sans-serif;padding:24px}}
main{{max-width:900px;margin:auto}}.panel{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:24px;margin:18px 0}}
h1{{font-size:clamp(1.7rem,5vw,2.4rem);margin:.1em 0}}h2{{margin:0 0 12px}}p,.muted{{color:var(--muted)}}
label{{display:block;font-weight:650;margin:18px 0 7px}}input,select,textarea{{display:block;width:100%;border:1px solid #354354;background:#0d131b;color:var(--text);padding:13px;border-radius:8px;font:inherit}}
input[type=file]{{min-height:80px;border:2px dashed #516174}}textarea{{min-height:300px;font:13px ui-monospace,monospace;white-space:pre;overflow:auto}}
button{{border:0;border-radius:8px;padding:14px 18px;font:inherit;font-weight:700;cursor:pointer}}.primary{{background:var(--green);color:#06140d;margin-top:18px;width:100%}}
.nav{{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:22px}}a{{color:#8cbcff}}.error{{border-color:#a84444;color:#ffc6c6}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}
[hidden]{{display:none!important}}@media(max-width:520px){{body{{padding:12px}}.panel{{padding:18px}}}}
</style></head>
<body><main><div class="nav"><a href="/dashboard?site_id={html.escape(selected_site, quote=True)}">← Floor Overview</a><span class="muted">100% READ-ONLY DIAGNOSTIC</span></div>
<section class="panel"><h1>Technician Onboarding</h1><p>Capture a machine nameplate photo and validate a template-backed read-only profile.</p>
<form id="upload-form" action="/upload" method="post" enctype="multipart/form-data">
<label for="site_id">Plant / Site</label><select name="site_id" id="site_id">{options}<option value="__new__">＋ Create a new plant…</option></select>
<div id="new-site-fields" hidden>
  <label for="new-site-id">New site ID</label><input id="new-site-id" name="new_site_id" pattern="[a-z0-9][a-z0-9_-]{{0,63}}" maxlength="64" placeholder="plant_02">
  <label for="new-site-name">Plant name</label><input id="new-site-name" name="new_site_name" maxlength="120" placeholder="Plant 02">
  <label for="new-site-location">Location</label><input id="new-site-location" name="new_site_location" maxlength="160" placeholder="Detroit, MI">
</div>
<label for="photo">Machine / nameplate photo</label><input id="photo" type="file" name="photo" accept="image/*" capture="environment" required>
<p class="muted">The uploaded image is supplied as both nameplate and panel input for this single-photo test.</p>
<label for="machine_id">Machine ID (optional)</label><input id="machine_id" type="text" name="machine_id" value="{machine_value}" maxlength="64" pattern="[A-Za-z0-9][A-Za-z0-9_-]{{0,63}}">
<button id="submit-button" class="primary" type="submit">Run Onboarding Pipeline</button><p id="loading" hidden>Processing photo and validating configuration…</p>
</form></section>{result}
</main><script>
const siteSelect=document.getElementById("site_id");
siteSelect.addEventListener("change",()=>document.getElementById("new-site-fields").hidden=siteSelect.value!=="__new__");
document.getElementById("upload-form").addEventListener("submit",()=>{{const b=document.getElementById("submit-button");b.disabled=true;b.textContent="Processing…";document.getElementById("loading").hidden=false;}});
const approve=document.getElementById("approve-button");
if(approve) approve.addEventListener("click",async()=>{{
  const message=document.getElementById("approval-message");approve.disabled=true;message.textContent="Deploying approved configuration…";
  try {{
    const config=JSON.parse(document.getElementById("validated-config").value);
    const cycle=document.getElementById("ideal-cycle").value;
    const payload={{config}};
    if(cycle!=="") payload.ideal_cycle_time_seconds=Number(cycle);
    const site=document.getElementById("approved-site-id").value;
    const response=await fetch(`/api/sites/${{encodeURIComponent(site)}}/approve`,{{method:"POST",headers:{{"Content-Type":"application/json"}},body:JSON.stringify(payload)}});
    if(response.redirected) {{window.location.href=response.url;return;}}
    const data=await response.json();
    if(!response.ok) throw new Error(data.detail||"Deployment failed");
    window.location.href=`/dashboard?site_id=${{encodeURIComponent(site)}}`;
  }} catch(error) {{message.textContent=error.message;approve.disabled=false;}}
}});
</script></body></html>"""
    )


def _dashboard_page(site_id: str) -> HTMLResponse:
    js_site = json.dumps(site_id)
    options = _site_options(site_id)
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>MCA Machine Health | Floor Overview</title>
<style>
:root{{--bg:#0B0F14;--card:#131922;--border:#232e3b;--text:#e5edf5;--muted:#96a4b4;--green:#20c77a;--amber:#e6a23c;--red:#ed6b6b;--blue:#3e8ee8}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 Inter,ui-sans-serif,system-ui,sans-serif}}
header{{min-height:76px;padding:14px clamp(16px,3vw,42px);display:flex;align-items:center;gap:24px;justify-content:space-between;border-bottom:1px solid var(--border);background:#0e141c;position:sticky;top:0;z-index:3}}
.brand{{min-width:210px}}.brand strong{{display:block;font-size:17px;letter-spacing:.03em}}.brand small,.eyebrow,.muted{{color:var(--muted)}}.brand small{{font-size:10px;letter-spacing:.13em}}
.header-tools,.nav,.toggle-wrap{{display:flex;align-items:center;gap:16px;flex-wrap:wrap}}select{{background:#151e29;border:1px solid #354354;color:var(--text);padding:10px;border-radius:7px;max-width:260px}}
a{{color:#b4c8dd;text-decoration:none}}a:hover{{color:white}}.badge{{border:1px solid #168a59;color:#65e8a7;border-radius:99px;padding:7px 10px;font-size:10px;font-weight:800;letter-spacing:.08em;white-space:nowrap}}
.toggle-wrap{{color:var(--muted);gap:8px}}.switch{{position:relative;width:42px;height:24px;display:inline-block}}.switch input{{opacity:0;width:0;height:0}}.slider{{position:absolute;inset:0;background:#384351;border-radius:30px;cursor:pointer}}.slider:before{{content:"";position:absolute;width:18px;height:18px;left:3px;top:3px;background:white;border-radius:50%;transition:.15s}}.switch input:checked+.slider{{background:#138b58}}.switch input:checked+.slider:before{{transform:translateX(18px)}}
main{{max-width:1500px;margin:auto;padding:28px clamp(16px,3vw,42px) 50px}}.section-heading{{display:flex;align-items:end;justify-content:space-between;gap:16px;margin:10px 0 18px}}h1{{font-size:24px;margin:0}}h2{{font-size:16px;margin:0}}.kpis{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:18px 0 28px}}.card{{background:var(--card);border:1px solid var(--border);border-radius:11px;padding:18px}}
.kpi-label{{font-size:10px;color:var(--muted);letter-spacing:.12em;font-weight:800}}.kpi-value{{font-size:30px;font-weight:750;margin-top:7px}}.kpi-unit{{font-size:14px;color:var(--muted)}}
.fleet-layout{{display:grid;grid-template-columns:minmax(0,1fr) 260px;gap:16px;align-items:start}}.machines{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}}.machine-card{{padding:18px}}.machine-title{{display:flex;justify-content:space-between;gap:10px}}.machine-title h2{{font-size:16px}}.machine-title p{{margin:4px 0;color:var(--muted);font-size:12px}}.status{{border-radius:99px;padding:5px 9px;font-size:10px;font-weight:850;height:max-content;white-space:nowrap;background:#25323e;color:#b8c5d2}}.status.running{{background:#123a2b;color:#69e8a8}}.status.micro{{background:#493719;color:#ffd082}}.status.fault{{background:#4b2224;color:#ff9b9b}}
.oee-line{{display:flex;align-items:center;justify-content:space-between;margin:20px 0 12px}}.oee-number{{font-size:32px;font-weight:800}}.progress-row{{display:grid;grid-template-columns:95px 1fr 42px;gap:9px;align-items:center;margin:10px 0;color:var(--muted);font-size:11px}}.bar{{height:7px;border-radius:9px;background:#26313e;overflow:hidden}}.bar i{{display:block;height:100%;background:var(--green);border-radius:9px}}
.machine-stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:18px}}.stat{{background:#0e141c;border-radius:7px;padding:10px}}.stat small{{display:block;color:var(--muted);font-size:9px;letter-spacing:.08em}}.stat b{{display:block;margin-top:4px;font-size:15px}}.hub{{position:sticky;top:98px}}.hub-status{{color:#66e8a4;font-weight:800;margin:14px 0}}.hub-metric{{border-top:1px solid var(--border);padding:12px 0;display:flex;justify-content:space-between}}.empty,.error{{padding:25px;border:1px dashed #3a4858;color:var(--muted);border-radius:10px;grid-column:1/-1}}footer{{border-top:1px solid var(--border);padding:18px clamp(16px,3vw,42px);color:var(--muted);font-size:11px;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}}code{{color:#a7d8ff}}.read-only{{color:#58d69a;font-weight:800;letter-spacing:.08em}}
@media(max-width:1050px){{header{{align-items:flex-start}}.header-tools{{justify-content:flex-end}}.fleet-layout{{grid-template-columns:1fr}}.hub{{position:static}}}}
@media(max-width:760px){{header{{position:static;display:block}}.header-tools{{justify-content:flex-start;margin-top:14px}}.kpis{{grid-template-columns:repeat(2,minmax(0,1fr))}}.brand{{margin-bottom:10px}}}}
@media(max-width:380px){{.kpis{{grid-template-columns:1fr}}}}
</style></head>
<body><header>
<div class="brand"><strong>MCA Machine Health</strong><small>MOTORCITY AUTOMATED SYSTEMS, LLC</small></div>
<div class="header-tools"><label class="eyebrow" for="site-picker">PLANT / SITE</label><select id="site-picker">{options}</select>
<span class="badge">100% READ-ONLY DIAGNOSTIC</span><nav class="nav"><a href="/dashboard?site_id={html.escape(site_id, quote=True)}">Floor Overview</a><a href="/?site_id={html.escape(site_id, quote=True)}">Technician Onboarding</a></nav>
<label class="toggle-wrap" for="demo-toggle">Live Demo Simulator <span id="toggle-state">ON</span><span class="switch"><input id="demo-toggle" type="checkbox" checked><span class="slider"></span></span></label>
</div></header>
<main><div class="section-heading"><div><p class="eyebrow">FLEET OPERATIONS / <span id="site-name">{html.escape(runtime.sites.get(site_id, {}).get("name", site_id))}</span></p><h1>Floor Overview</h1></div><span class="muted" id="last-updated">Connecting to site telemetry…</span></div>
<section class="kpis">
<div class="card"><div class="kpi-label">CONNECTED ASSETS</div><div class="kpi-value" id="assets">—</div></div>
<div class="card"><div class="kpi-label">RUNNING</div><div class="kpi-value" id="running">—</div></div>
<div class="card"><div class="kpi-label">FLEET OEE</div><div class="kpi-value"><span id="fleet-oee">—</span><span class="kpi-unit"> %</span></div></div>
<div class="card"><div class="kpi-label">SHIFT OUTPUT</div><div class="kpi-value" id="shift-output">—</div></div>
</section>
<div class="section-heading"><div><p class="eyebrow">REAL-TIME EQUIPMENT</p><h2>Machine Fleet</h2></div><span class="read-only">PASSIVE MONITORING ONLY</span></div>
<section class="fleet-layout"><div id="machines" class="machines"><div class="empty">Waiting for approved machines in this plant.</div></div>
<aside class="card hub"><p class="eyebrow">EDGE GATEWAY</p><h2>Central Hub</h2><div class="hub-status" id="hub-status">● ONLINE</div><div class="hub-metric"><span class="muted">Active machines</span><b id="hub-machines">0</b></div><div class="hub-metric"><span class="muted">Write access</span><b>DISABLED</b></div><div class="hub-metric"><span class="muted">Data mode</span><b>Read-only</b></div><p class="muted">Gateway health is scoped to the selected site.</p></aside></section>
</main><footer><span>UNIFIED NAMESPACE · <code id="topic-pattern">mca/{html.escape(site_id, quote=True)}/&lt;machine_id&gt;/status • telemetry</code></span><span class="read-only">ZERO-WRITE READ-ONLY ARCHITECTURE</span></footer>
<script>
const currentSite={js_site};
const picker=document.getElementById("site-picker");
picker.addEventListener("change",()=>window.location.href=`/dashboard?site_id=${{encodeURIComponent(picker.value)}}`);
const demoToggle=document.getElementById("demo-toggle");
const esc=value=>String(value??"").replace(/[&<>"']/g,char=>({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[char]));
const pct=value=>Math.max(0,Math.min(100,Number(value||0)*100));
function machineCard(machine){{
  const status=machine.status||"UNKNOWN";
  const kind=status==="RUNNING"?"running":status==="MICRO_STOP"?"micro":status==="FAULT"||status==="OFFLINE_SENSOR"?"fault":"";
  const progress=(label,value)=>`<div class="progress-row"><span>${{label}}</span><span class="bar"><i style="width:${{pct(value)}}%"></i></span><b>${{pct(value).toFixed(0)}}%</b></div>`;
  const downtime=Number(machine.downtime_seconds||0);
  const minutes=Math.floor(downtime/60),seconds=downtime%60;
  return `<article class="card machine-card"><div class="machine-title"><div><h2>${{esc(machine.oem)}} · ${{esc(machine.model)}}</h2><p>${{esc(machine.machine_id)}} · ${{esc(machine.archetype)}}</p></div><span class="status ${{kind}}">${{esc(status.replace("_"," "))}}</span></div>
  <div class="oee-line"><span class="eyebrow">OVERALL EQUIPMENT EFFECTIVENESS</span><span class="oee-number">${{pct(machine.oee).toFixed(1)}}%</span></div>
  ${{progress("Availability",machine.availability)}}${{progress("Performance",machine.performance)}}${{progress("Quality",machine.quality)}}
  <div class="machine-stats"><div class="stat"><small>SHIFT PARTS</small><b>${{Number(machine.cycle_count||0).toLocaleString()}}</b></div><div class="stat"><small>MOTOR CURRENT</small><b>${{Number(machine.motor_amps||0).toFixed(1)}} A</b></div><div class="stat"><small>DOWNTIME</small><b>${{minutes}}:${{String(seconds).padStart(2,"0")}}</b></div></div></article>`;
}}
async function poll(){{
  try {{
    const response=await fetch(`/api/sites/${{encodeURIComponent(currentSite)}}/telemetry`,{{cache:"no-store"}});
    if(!response.ok) throw new Error((await response.json()).detail||"Telemetry unavailable");
    const data=await response.json();
    document.getElementById("assets").textContent=data.summary.connected_assets;
    document.getElementById("running").textContent=data.summary.running;
    document.getElementById("fleet-oee").textContent=(Number(data.summary.fleet_oee)*100).toFixed(1);
    document.getElementById("shift-output").textContent=Number(data.summary.shift_output).toLocaleString();
    document.getElementById("hub-machines").textContent=data.hub.active_machines;
    document.getElementById("hub-status").textContent=`● ${{data.hub.status}}`;
    document.getElementById("last-updated").textContent=`Updated ${{new Date(data.updated_at).toLocaleTimeString()}}`;
    document.getElementById("machines").innerHTML=data.machines.length?data.machines.map(machineCard).join(""):'<div class="empty">No approved machines are registered at this site. Use Technician Onboarding to add an asset.</div>';
    demoToggle.checked=data.simulator_enabled;
    document.getElementById("toggle-state").textContent=data.simulator_enabled?"ON":"OFF";
  }} catch(error) {{
    document.getElementById("last-updated").textContent=error.message;
    document.getElementById("last-updated").style.color="#ed6b6b";
  }}
}}
demoToggle.addEventListener("change",async()=>{{
  demoToggle.disabled=true;
  try {{
    const response=await fetch(`/api/sites/${{encodeURIComponent(currentSite)}}/simulator`,{{method:"POST",headers:{{"Content-Type":"application/json"}},body:JSON.stringify({{enabled:demoToggle.checked}})}});
    if(!response.ok) throw new Error("Unable to change simulator state");
    document.getElementById("toggle-state").textContent=demoToggle.checked?"ON":"OFF";
  }} catch(error) {{demoToggle.checked=!demoToggle.checked;document.getElementById("last-updated").textContent=error.message;}}
  finally {{demoToggle.disabled=false;}}
}});
poll();setInterval(poll,1000);
</script></body></html>"""
    )


@app.get("/", response_class=HTMLResponse)
async def upload_form(site_id: str | None = None) -> HTMLResponse:
    try:
        selected = _canonical_site_id(site_id)
    except ValueError:
        selected = DEFAULT_SITE_ID
    if selected not in runtime.sites:
        selected = DEFAULT_SITE_ID
    return _onboarding_page(selected_site=selected)


@app.post("/upload", response_class=HTMLResponse)
async def upload_photo(
    photo: UploadFile = File(...),
    machine_id: str = Form(""),
    site_id: str = Form(DEFAULT_SITE_ID),
    new_site_id: str = Form(""),
    new_site_name: str = Form(""),
    new_site_location: str = Form(""),
) -> HTMLResponse:
    extension = IMAGE_EXTENSIONS.get((photo.content_type or "").lower())
    if extension is None:
        return _onboarding_page(machine_id=machine_id, error="Upload a JPEG, PNG, WebP, GIF, or BMP image.")
    if site_id == "__new__":
        try:
            site = runtime.add_site(new_site_id, new_site_name, new_site_location)
            site_id = site["site_id"]
        except HTTPException as exc:
            return _onboarding_page(machine_id=machine_id, error=str(exc.detail))
    try:
        canonical_site = _canonical_site_id(site_id)
    except ValueError as exc:
        return _onboarding_page(machine_id=machine_id, error=str(exc))
    if canonical_site not in runtime.sites:
        return _onboarding_page(machine_id=machine_id, error=f"Unknown site '{canonical_site}'.")

    requested_id = machine_id.strip() or f"machine_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", requested_id):
        return _onboarding_page(
            machine_id=machine_id,
            selected_site=canonical_site,
            error="Machine ID must start with a letter or digit and use only letters, digits, underscores, or hyphens.",
        )
    normalized_id = requested_id.lower()
    image = await photo.read(MAX_UPLOAD_BYTES + 1)
    await photo.close()
    if not image:
        return _onboarding_page(machine_id=normalized_id, selected_site=canonical_site, error="The uploaded image is empty.")
    if len(image) > MAX_UPLOAD_BYTES:
        return _onboarding_page(machine_id=normalized_id, selected_site=canonical_site, error="Image exceeds the 20 MiB limit.")

    upload_dir = ROOT / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    image_path = upload_dir / f"{uuid.uuid4().hex}{extension}"
    try:
        image_path.write_bytes(image)
    except OSError as exc:
        return _onboarding_page(machine_id=normalized_id, selected_site=canonical_site, error=f"Unable to save photo: {exc}")

    command = [
        sys.executable,
        str(ROOT / "agent.py"),
        "--nameplate",
        str(image_path),
        "--panel",
        str(image_path),
        "--machine-id",
        normalized_id,
        "--site-id",
        canonical_site,
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=ROOT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return _onboarding_page(machine_id=normalized_id, selected_site=canonical_site, error="Onboarding timed out after 5 minutes.")
    except OSError as exc:
        return _onboarding_page(machine_id=normalized_id, selected_site=canonical_site, error=f"Unable to start agent.py: {exc}")
    if process.returncode != 0:
        details = (stderr or stdout).decode("utf-8", errors="replace").strip()
        return _onboarding_page(
            machine_id=normalized_id,
            selected_site=canonical_site,
            error=details or f"agent.py exited with status {process.returncode}.",
        )
    try:
        config_path = ROOT / "configs" / f"{normalized_id}.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("agent.py output must be a JSON object")
        config["site_id"] = canonical_site
        return _onboarding_page(machine_id=normalized_id, selected_site=canonical_site, config=config)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return _onboarding_page(
            machine_id=normalized_id,
            selected_site=canonical_site,
            error=f"Unable to read validated agent output: {exc}",
        )


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(site_id: str = DEFAULT_SITE_ID) -> HTMLResponse:
    selected = _site_id_or_404(site_id)
    return _dashboard_page(selected)


@app.get("/api/sites")
async def list_sites() -> dict[str, Any]:
    with runtime._lock:
        sites = [
            {"site_id": site_id, **record}
            for site_id, record in sorted(runtime.sites.items())
        ]
    return {"default_site_id": DEFAULT_SITE_ID, "sites": sites}


@app.post("/api/sites", status_code=201)
async def create_site(request: SiteCreate) -> dict[str, str]:
    return runtime.add_site(request.site_id, request.name, request.location)


@app.get("/api/sites/{site_id}/telemetry")
async def get_site_telemetry(site_id: str) -> dict[str, Any]:
    canonical = _site_id_or_404(site_id)
    return runtime.telemetry_for_site(canonical)


@app.post("/api/sites/{site_id}/simulator")
async def set_site_simulator(site_id: str, request: SimulatorRequest) -> dict[str, Any]:
    canonical = _site_id_or_404(site_id)
    runtime.set_simulator(canonical, request.enabled)
    return {"site_id": canonical, "enabled": request.enabled}


@app.post("/api/sites/{site_id}/approve")
async def approve_config(site_id: str, request: ApprovalRequest) -> RedirectResponse:
    canonical = _site_id_or_404(site_id)
    config = dict(request.config)
    machine_id = config.get("machine_id")
    if not isinstance(machine_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", machine_id):
        raise HTTPException(status_code=422, detail="Configuration must contain a valid machine_id.")
    config_site_id = config.get("site_id", "default")
    if not isinstance(config_site_id, str) or config_site_id not in {"default", canonical}:
        raise HTTPException(status_code=422, detail="Configuration site_id does not match the approval route.")
    config["site_id"] = canonical

    try:
        runtime._assert_approved_config(config)
        profile = MachineProfile.model_validate(config)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if profile.machine_id != machine_id:
        raise HTTPException(status_code=422, detail="machine_id changed during profile validation.")
    if request.ideal_cycle_time_seconds is not None:
        cycle_time = request.ideal_cycle_time_seconds
        if not math.isfinite(cycle_time) or not 0.001 <= cycle_time <= 86400:
            raise HTTPException(status_code=422, detail="ideal_cycle_time_seconds must be finite and between 0.001 and 86400.")
        config["ideal_cycle_time_seconds"] = cycle_time
    config["approved_by"] = "human"
    config["approved_at"] = _utc_now()

    site_dir = runtime.deployed_dir / canonical
    site_dir.mkdir(parents=True, exist_ok=True)
    destination = site_dir / f"{machine_id}.json"
    temporary = site_dir / f".{machine_id}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, destination)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Unable to persist approved config: {exc}") from exc

    profile = MachineProfile.model_validate(config)
    runtime.start_worker(canonical, profile)
    return RedirectResponse(url=f"/dashboard?site_id={canonical}", status_code=303)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
