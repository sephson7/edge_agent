import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  ArrowDownRight,
  ArrowLeft,
  ArrowUpRight,
  BadgeCheck,
  Check,
  ChevronRight,
  CircleHelp,
  Clock3,
  Cloud,
  Cpu,
  Factory,
  FileJson2,
  Gauge,
  HardDrive,
  ImagePlus,
  LockKeyhole,
  Radio,
  Signal,
  Sparkles,
  Upload,
  Wifi,
  Zap,
} from "lucide-react";
import { createDemoMachines, reasonCodes, SITE_ID, SITE_NAME, statusLabels } from "./data";
import type {
  Machine,
  MachineProfile,
  MachineStatus,
  Screen,
  SensorTransport,
  Telemetry,
} from "./types";
import { profileChannels } from "./types";

const statusClass: Record<MachineStatus, string> = {
  RUNNING: "running",
  MICRO_STOP: "micro-stop",
  FAULT: "fault",
  IDLE: "idle",
};

const archetypeName: Record<Machine["archetype"], string> = {
  stamping: "Stamping press",
  cnc: "CNC mill",
  injection: "Injection molder",
};

function formatTime(dateString: string) {
  return new Date(dateString).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function formatDuration(seconds: number) {
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

function Header({
  screen,
  setScreen,
  live,
  setLive,
}: {
  screen: Screen;
  setScreen: (screen: Screen) => void;
  live: boolean;
  setLive: (value: boolean) => void;
}) {
  const tabs: { id: Screen; label: string }[] = [
    { id: "overview", label: "Floor Overview" },
    { id: "detail", label: "Machine Detail" },
    { id: "onboarding", label: "Technician Onboarding" },
  ];
  return (
    <header className="topbar">
      <div className="brand-group">
        <div className="brand-mark"><Factory size={21} strokeWidth={2.2} /></div>
        <div className="brand-title-wrap">
          <div className="brand-title">MCA <span>Machine Health</span></div>
          <div className="brand-byline">MOTORCITY AUTOMATED SYSTEMS, LLC</div>
        </div>
        <div className="site-pill"><span className="site-dot" />{SITE_NAME}</div>
        <div className="readonly-pill"><LockKeyhole size={12} />100% READ-ONLY DIAGNOSTIC</div>
      </div>
      <div className="header-controls">
        <nav className="top-nav" aria-label="Main navigation">
          {tabs.map((tab) => (
            <button
              className={`nav-tab ${screen === tab.id ? "active" : ""}`}
              key={tab.id}
              onClick={() => setScreen(tab.id)}
            >
              {tab.label}
            </button>
          ))}
        </nav>
        <button className={`sim-toggle ${live ? "is-on" : ""}`} onClick={() => setLive(!live)} aria-pressed={live}>
          <span className="sim-toggle-copy"><span className="sim-kicker">BROWSER MODE</span><span>Live Demo Simulator</span></span>
          <span className="switch"><span /></span>
        </button>
      </div>
    </header>
  );
}

function TransportBadge({ transport }: { transport: SensorTransport }) {
  if (transport === "wireless_900mhz") {
    return (
      <span className="transport-badge wireless">
        <Signal size={13} /> Wireless 900MHz <span className="signal-bars"><i /><i /><i /></span>
      </span>
    );
  }
  return <span className="transport-badge"><Zap size={13} /> Wired Tap</span>;
}

function StatusBadge({ status, compact = false }: { status: MachineStatus; compact?: boolean }) {
  return (
    <span className={`status-label ${statusClass[status]} ${compact ? "compact" : ""}`}>
      <span className="status-dot" />{status}
    </span>
  );
}

function ProgressMetric({ label, value, color }: { label: string; value: number; color?: string }) {
  const percent = Math.max(0, Math.min(100, Math.round(value * 100)));
  return (
    <div className="progress-metric">
      <div className="progress-caption"><span>{label}</span><b>{percent}%</b></div>
      <div className="progress-track"><span style={{ width: `${percent}%`, background: color }} /></div>
    </div>
  );
}

function MachineCard({ machine, onClick }: { machine: Machine; onClick: () => void }) {
  const stateHint = machine.status === "RUNNING" ? "PRODUCTION ACTIVE" : machine.activeReason || statusLabels[machine.status].toUpperCase();
  return (
    <button className="machine-card" onClick={onClick}>
      <div className={`card-status-bar ${statusClass[machine.status]}`}>
        <span className="card-status-light" />
        <span>{stateHint}</span>
        <StatusBadge status={machine.status} compact />
      </div>
      <div className="machine-card-body">
        <div className="machine-card-head">
          <div className="machine-card-icon"><Cpu size={18} /></div>
          <div className="machine-title">
            <span className="machine-id">{machine.profile.machine_id}</span>
            <h3>{machine.name}</h3>
          </div>
          <ChevronRight className="card-chevron" size={18} />
        </div>
        <div className="transport-row"><TransportBadge transport={machine.profile.sensor_transport} /><span className="machine-type">{archetypeName[machine.archetype]}</span></div>
        <div className="card-oee-row">
          <div className="oee-number"><span>OEE</span><strong>{Math.round(machine.telemetry.oee * 100)}<small>%</small></strong></div>
          <div className="card-progress">
            <ProgressMetric label="Availability" value={machine.telemetry.availability} />
            <ProgressMetric label="Performance" value={machine.telemetry.performance} />
            <ProgressMetric label="Quality" value={machine.telemetry.quality} />
          </div>
        </div>
        <div className="card-stats">
          <div><span>SHIFT PARTS</span><strong>{machine.telemetry.cycle_count.toLocaleString()} <small>/ {machine.targetPerShift.toLocaleString()}</small></strong></div>
          <div><span>MOTOR CURRENT</span><strong>{machine.telemetry.motor_amps.toFixed(1)} <small>A</small></strong></div>
          {machine.status !== "RUNNING" && (
            <div className="downtime-stat"><span>DOWN TIME</span><strong><Clock3 size={13} />{formatDuration(machine.downtimeSeconds)}</strong></div>
          )}
        </div>
      </div>
      <div className="card-foot"><span><span className="online-dot" />EDGE NODE CONNECTED</span><span>{formatTime(machine.telemetry.timestamp)}</span></div>
    </button>
  );
}

function Overview({ machines, openMachine }: { machines: Machine[]; openMachine: (id: string) => void }) {
  const running = machines.filter((machine) => machine.status === "RUNNING").length;
  const averageOee = machines.length
    ? Math.round((machines.reduce((sum, machine) => sum + machine.telemetry.oee, 0) / machines.length) * 100)
    : 0;
  const parts = machines.reduce((sum, machine) => sum + machine.telemetry.cycle_count, 0);
  return (
    <main className="app-content">
      <div className="page-heading overview-heading">
        <div>
          <div className="eyebrow"><span className="eyebrow-line" />CENTRAL EDGE HUB <span className="hub-online"><span className="online-dot" />ONLINE</span></div>
          <h1>Plant Floor <span>Overview</span></h1>
          <p>Real-time visibility across your connected production assets.</p>
        </div>
        <div className="overview-summary">
          <div><span>CONNECTED ASSETS</span><strong>{String(machines.length).padStart(2, "0")}<small> MACHINES</small></strong></div>
          <div><span>RUNNING</span><strong className="summary-green">{String(running).padStart(2, "0")}<small> / {machines.length}</small></strong></div>
          <div><span>FLEET OEE</span><strong>{averageOee}<small>%</small></strong></div>
          <div><span>SHIFT OUTPUT</span><strong>{parts.toLocaleString()}<small> PARTS</small></strong></div>
        </div>
      </div>
      <div className="section-title-row">
        <div><h2>Connected Machines</h2><span className="section-subtitle">LIVE TELEMETRY · REFRESHING EVERY SECOND</span></div>
        <div className="connected-indicator"><span className="online-dot" />{machines.length} EDGE CHANNELS ACTIVE</div>
      </div>
      <div className="machine-grid">
        {machines.map((machine) => (
          <MachineCard key={`${machine.profile.site_id}/${machine.profile.machine_id}`} machine={machine} onClick={() => openMachine(machine.profile.machine_id)} />
        ))}
        <div className="edge-node-card">
          <div className="edge-node-icon"><HardDrive size={21} /></div>
          <div><span className="machine-id">MCA EDGE NODE</span><h3>Central Hub</h3></div>
          <span className="edge-node-live"><i />CONNECTED</span>
          <p>Read-only polling gateway · Unified Namespace bridge</p>
          <div className="edge-topics"><span>mca/{SITE_ID}/fleet/manifest</span><span>{machines.length} machine profiles synchronized</span></div>
        </div>
      </div>
      <div className="uns-footer"><span><Cloud size={14} /> UNIFIED NAMESPACE</span><code>mca/{SITE_ID}/&#123;machine_id&#125;/status &nbsp;·&nbsp; telemetry</code><span>LAST HUB SYNC &nbsp; NOW</span></div>
    </main>
  );
}

function GaugeRing({ label, value, color, size = 118 }: { label: string; value: number; color: string; size?: number }) {
  const radius = 44;
  const circumference = 2 * Math.PI * radius;
  const percent = Math.max(0, Math.min(1, value));
  return (
    <div className="gauge-ring" style={{ width: size, height: size }}>
      <svg viewBox="0 0 100 100" aria-label={`${label} ${Math.round(percent * 100)} percent`}>
        <circle className="gauge-track" cx="50" cy="50" r={radius} />
        <circle className="gauge-value" cx="50" cy="50" r={radius} stroke={color} strokeDasharray={circumference} strokeDashoffset={circumference * (1 - percent)} />
      </svg>
      <div className="gauge-center"><strong>{Math.round(percent * 100)}<small>%</small></strong><span>{label}</span></div>
    </div>
  );
}

function Sparkline({ values }: { values: number[] }) {
  const points = values.map((value, index) => `${(index / Math.max(1, values.length - 1)) * 100},${94 - value * 80}`).join(" ");
  const area = `0,100 ${points} 100,100`;
  return (
    <svg className="sparkline" viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label="Motor current trend">
      <defs><linearGradient id="spark-fill" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stopColor="#46d994" stopOpacity=".24" /><stop offset="100%" stopColor="#46d994" stopOpacity="0" /></linearGradient></defs>
      <polygon points={area} fill="url(#spark-fill)" />
      <polyline points={points} fill="none" stroke="#46d994" strokeWidth="1.8" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

function Detail({ machine, onBack, onReason }: { machine: Machine; onBack: () => void; onReason: (reason: string) => void }) {
  const reasons = reasonCodes[machine.archetype];
  const targetPercent = Math.min(100, Math.round((machine.telemetry.cycle_count / machine.targetPerShift) * 100));
  return (
    <main className="app-content">
      <div className="detail-title-row">
        <button className="back-button" onClick={onBack}><ArrowLeft size={16} /> FLOOR OVERVIEW</button>
        <div className="detail-machine-title"><span className="machine-id">{machine.profile.machine_id}</span><h1>{machine.name}</h1></div>
        <StatusBadge status={machine.status} />
        <TransportBadge transport={machine.profile.sensor_transport} />
        <span className="detail-live"><i /> LIVE</span>
      </div>
      <div className="detail-split">
        <section className="detail-left">
          <div className="detail-metrics-panel">
            <div className="detail-oee">
              <span className="eyebrow">OVERALL EQUIPMENT EFFECTIVENESS</span>
              <strong>{Math.round(machine.telemetry.oee * 100)}<small>%</small></strong>
              <span className="oee-trend"><ArrowUpRight size={14} /> +2.4% vs. previous shift</span>
              <div className="oee-comparison"><span>SHIFT TARGET</span><b>85%</b><div><span style={{ width: `${Math.min(100, (machine.telemetry.oee / 0.85) * 100)}%` }} /></div></div>
            </div>
            <div className="gauge-group">
              <GaugeRing label="Availability" value={machine.telemetry.availability} color="#39d98a" />
              <GaugeRing label="Performance" value={machine.telemetry.performance} color="#43a7ff" />
              <GaugeRing label="Quality" value={machine.telemetry.quality} color="#b18aff" />
            </div>
          </div>
          <div className="detail-lower-grid">
            <section className="panel current-panel">
              <div className="panel-heading"><div><span className="panel-icon green"><Activity size={15} /></span><h2>Motor Current</h2></div><span className="live-tag"><i />LIVE</span></div>
              <div className="current-reading">{machine.telemetry.motor_amps.toFixed(1)}<small> A</small><span className="current-state"><span className="online-dot" />NORMAL LOAD</span></div>
              <div className="chart-wrap"><div className="chart-guides"><span>20 A</span><span>10 A</span><span>0 A</span></div><Sparkline values={machine.history} /></div>
              <div className="chart-foot"><span>LAST 30 SECONDS</span><span>CT CLAMP · DI1</span></div>
            </section>
            <section className="panel production-panel">
              <div className="panel-heading"><div><span className="panel-icon blue"><Gauge size={15} /></span><h2>Shift Production</h2></div><span className="panel-badge">SHIFT A</span></div>
              <div className="production-count">{machine.telemetry.cycle_count.toLocaleString()}<span> / {machine.targetPerShift.toLocaleString()}</span></div>
              <div className="production-bar"><span style={{ width: `${targetPercent}%` }} /></div>
              <div className="production-foot"><span>{targetPercent}% OF SHIFT TARGET</span><span>GOAL {machine.targetPerShift.toLocaleString()}</span></div>
              <div className="production-stats"><div><span>IDEAL CYCLE</span><b>{(60 / Number(machine.profile.ideal_cycles_per_minute || 45)).toFixed(1)}<small> SEC</small></b></div><div><span>GOOD PARTS</span><b>{Math.round(machine.telemetry.cycle_count * machine.telemetry.quality).toLocaleString()}</b></div></div>
              <div className="target-note"><ArrowDownRight size={13} /> Target based on current shift plan</div>
            </section>
            <section className="panel event-panel">
              <div className="panel-heading"><div><span className="panel-icon amber"><Clock3 size={15} /></span><h2>Live Event Log</h2></div><button className="text-button">VIEW ALL <ChevronRight size={13} /></button></div>
              <div className="event-list">
                {machine.events.slice(0, 5).map((event, index) => (
                  <div className="event-row" key={`${event.timestamp}-${index}`}>
                    <span className={`event-marker ${event.kind}`} />
                    <time>{formatTime(event.timestamp)}</time>
                    <span>{event.message}</span>
                  </div>
                ))}
              </div>
            </section>
          </div>
        </section>
        <aside className="downtime-panel">
          <div className="downtime-heading"><div><span className="panel-icon amber"><AlertTriangle size={16} /></span><div><h2>Operator Downtime Pad</h2><span>SELECT A REASON CODE TO LOG A STOP</span></div></div><span className="touch-badge">TOUCH READY</span></div>
          <div className="reason-callout"><span className="callout-indicator" /><span>Current status: <b className={statusClass[machine.status]}>{machine.status.replace("_", " ")}</b></span></div>
          <div className="reason-grid">
            {reasons.map((reason, index) => (
              <button className="reason-button" key={reason} onClick={() => onReason(reason)}>
                <span className="reason-number">0{index + 1}</span><span>{reason}</span><ChevronRight size={16} />
              </button>
            ))}
          </div>
          <div className="downtime-bottom">
            <div className="downtime-duration"><Clock3 size={16} /><div><span>ACTIVE STOP DURATION</span><strong>{machine.status === "RUNNING" ? "— — : — —" : formatDuration(machine.downtimeSeconds)}</strong></div></div>
            {machine.activeReason && <div className="active-reason"><span>ACTIVE REASON</span><b>{machine.activeReason}</b></div>}
            <div className="read-only-note"><LockKeyhole size={13} /> Diagnostic only · No machine control commands</div>
          </div>
        </aside>
      </div>
    </main>
  );
}

const onboardingPresets = [
  { machine_id: "press_05", name: "Minster P2-100 Stamping Press", make: "Minster", model: "P2-100", kind: "stamping" as const, rate: 42, amps: 18.4, channels: ["main_motor_ct", "part_chute_prox", "fault_stack_light"] },
  { machine_id: "haas_04", name: "Haas VF-2 CNC Mill", make: "Haas", model: "VF-2", kind: "cnc" as const, rate: 8, amps: 11.7, channels: ["spindle_current_ct", "cycle_complete", "machine_alarm"] },
  { machine_id: "mold_05", name: "Cincinnati Milacron Injection Molder", make: "Cincinnati Milacron", model: "Roboshot 150", kind: "injection" as const, rate: 12, amps: 16.2, channels: ["hydraulic_motor_ct", "mold_cycle_prox", "fault_stack_light"] },
];

function Onboarding({
  onDeploy,
  machineIds,
}: {
  onDeploy: (machine: Machine) => void;
  machineIds: string[];
}) {
  const [presetId, setPresetId] = useState(onboardingPresets[0].machine_id);
  const [machineId, setMachineId] = useState(onboardingPresets[0].machine_id);
  const [transport, setTransport] = useState<SensorTransport>("wired");
  const [nameplateFile, setNameplateFile] = useState("");
  const [panelFile, setPanelFile] = useState("");
  const [nameplatePreview, setNameplatePreview] = useState("");
  const [panelPreview, setPanelPreview] = useState("");
  const [extracted, setExtracted] = useState(false);
  const [toast, setToast] = useState("");
  const preset = onboardingPresets.find((item) => item.machine_id === presetId) || onboardingPresets[0];

  const profile: MachineProfile = {
    machine_id: machineId.trim(),
    site_id: SITE_ID,
    sensor_transport: transport,
    make: preset.make,
    model: preset.model,
    name: preset.name,
    machine_type: preset.kind === "cnc" ? "CNC Mill" : preset.kind === "injection" ? "Injection Molder" : "Stamping Press",
    ideal_cycles_per_minute: preset.rate,
    motor_full_load_amps: preset.amps,
    channels: preset.channels.map((channel, index) => ({
      channel_id: `DI${index + 1}`,
      name: channel,
      read_only: true,
    })),
  };
  const jsonPreview = JSON.stringify(profile, null, 2);

  function choosePreset(value: string) {
    const selected = onboardingPresets.find((item) => item.machine_id === value);
    if (!selected) return;
    setPresetId(value);
    setMachineId(selected.machine_id);
    setExtracted(false);
  }

  function selectImage(file: File | undefined, target: "nameplate" | "panel") {
    if (!file) return;
    const url = URL.createObjectURL(file);
    if (target === "nameplate") {
      if (nameplatePreview) URL.revokeObjectURL(nameplatePreview);
      setNameplateFile(file.name);
      setNameplatePreview(url);
    } else {
      if (panelPreview) URL.revokeObjectURL(panelPreview);
      setPanelFile(file.name);
      setPanelPreview(url);
    }
    setExtracted(false);
  }

  function simulateExtraction() {
    setExtracted(true);
    setToast("Profile extracted and read-only checks passed");
    window.setTimeout(() => setToast(""), 3000);
  }

  function deploy() {
    if (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(profile.machine_id)) {
      setToast("Machine ID must be a lowercase MQTT-safe ID");
      window.setTimeout(() => setToast(""), 3500);
      return;
    }
    if (machineIds.includes(profile.machine_id)) {
      setToast("That machine ID is already connected");
      window.setTimeout(() => setToast(""), 3500);
      return;
    }
    const channels = profileChannels(profile);
    if (!channels.every((channel) => channel.read_only === true)) {
      setToast("Read-only validation failed");
      window.setTimeout(() => setToast(""), 3500);
      return;
    }
    const telemetry: Telemetry = {
      timestamp: new Date().toISOString(),
      motor_amps: 0,
      cycle_count: 0,
      oee: 0,
      availability: 1,
      performance: 1,
      quality: 1,
      sensor_transport: transport,
    };
    const machine: Machine = {
      profile,
      name: preset.name,
      archetype: preset.kind,
      status: "IDLE",
      telemetry,
      targetPerShift: preset.rate * 30,
      downtimeSeconds: 0,
      history: Array(28).fill(0.75),
      events: [{ timestamp: new Date().toISOString(), message: "Read-only profile approved & deployed", kind: "system" }],
    };
    onDeploy(machine);
    setToast("Read-only config approved and added to floor");
    window.setTimeout(() => setToast(""), 3500);
  }

  useEffect(() => () => {
    if (nameplatePreview) URL.revokeObjectURL(nameplatePreview);
    if (panelPreview) URL.revokeObjectURL(panelPreview);
  }, [nameplatePreview, panelPreview]);

  return (
    <main className="app-content onboarding-content">
      <div className="page-heading onboarding-heading">
        <div>
          <div className="eyebrow"><span className="eyebrow-line" />MCA EDGE AGENT <span className="approval-pill"><LockKeyhole size={12} /> HUMAN APPROVAL REQUIRED</span></div>
          <h1>Technician <span>Onboarding</span></h1>
          <p>Extract, validate, and stage a machine profile for read-only monitoring.</p>
        </div>
        <div className="step-track"><span className="step done"><Check size={12} />01</span><i /><span className={`step ${extracted ? "done" : "current"}`}>{extracted ? <Check size={12} /> : "02"} VALIDATE</span><i /><span className="step">03 DEPLOY</span></div>
      </div>
      <div className="onboarding-columns">
        <section className="panel onboarding-panel">
          <div className="panel-heading"><div><span className="panel-icon blue"><ImagePlus size={16} /></span><h2>Machine Identification</h2></div><span className="step-count">STEP 01 / 03</span></div>
          <label className="form-label">SELECT ARCHETYPE / SAMPLE PROFILE</label>
          <select className="industrial-select" value={presetId} onChange={(event) => choosePreset(event.target.value)}>
            {onboardingPresets.map((item) => <option value={item.machine_id} key={item.machine_id}>{item.name}</option>)}
          </select>
          <div className="form-grid">
            <label><span className="form-label">MACHINE ID</span><input className="industrial-input" value={machineId} onChange={(event) => { setMachineId(event.target.value.toLowerCase()); setExtracted(false); }} /></label>
            <label><span className="form-label">SITE ID</span><input className="industrial-input" value={SITE_ID} readOnly /></label>
          </div>
          <div className="upload-grid">
            <label className="upload-card">
              {nameplatePreview ? <img src={nameplatePreview} alt="Uploaded machine nameplate" /> : <span className="upload-icon"><Upload size={17} /></span>}
              <span><b>NAMEPLATE PHOTO</b><small>{nameplateFile || "Upload or use demo sample"}</small></span>
              <input type="file" accept="image/*" onChange={(event) => selectImage(event.target.files?.[0], "nameplate")} />
              {!nameplateFile && <button type="button" className="sample-link" onClick={(event) => { event.preventDefault(); setNameplateFile(`${preset.make.toLowerCase()}_nameplate_demo.jpg`); }}>USE SAMPLE</button>}
              {nameplateFile && <span className="file-check"><Check size={13} /></span>}
            </label>
            <label className="upload-card">
              {panelPreview ? <img src={panelPreview} alt="Uploaded control panel" /> : <span className="upload-icon"><Upload size={17} /></span>}
              <span><b>CONTROL PANEL</b><small>{panelFile || "Upload or use demo sample"}</small></span>
              <input type="file" accept="image/*" onChange={(event) => selectImage(event.target.files?.[0], "panel")} />
              {!panelFile && <button type="button" className="sample-link" onClick={(event) => { event.preventDefault(); setPanelFile(`${preset.machine_id}_panel_demo.jpg`); }}>USE SAMPLE</button>}
              {panelFile && <span className="file-check"><Check size={13} /></span>}
            </label>
          </div>
          <div className="transport-choice">
            <div><span className="form-label">SENSOR TRANSPORT</span><small>Passive monitoring connection type</small></div>
            <div className="transport-options">
              <button className={transport === "wired" ? "selected" : ""} onClick={() => { setTransport("wired"); setExtracted(false); }}><Zap size={14} /> Wired</button>
              <button className={transport === "wireless_900mhz" ? "selected" : ""} onClick={() => { setTransport("wireless_900mhz"); setExtracted(false); }}><Radio size={14} /> Wireless 900MHz</button>
            </div>
          </div>
          <button className="extract-button" onClick={simulateExtraction}><Sparkles size={16} />{extracted ? "PROFILE VALIDATED" : "SIMULATE AI EXTRACTION"}<ChevronRight size={15} /></button>
          <div className="extract-foot"><span><LockKeyhole size={12} />CHANNELS LOCKED READ-ONLY</span><span>{extracted ? "VALIDATION PASSED" : "LOCAL DEMO MODE"}</span></div>
        </section>
        <section className="panel config-panel">
          <div className="panel-heading"><div><span className="panel-icon green"><FileJson2 size={16} /></span><h2>Validated MachineProfile</h2></div><span className="json-valid"><BadgeCheck size={13} /> Pydantic v2</span></div>
          <div className="json-header"><span>CONFIG PREVIEW <small>configs/{profile.machine_id || "machine_id"}.json</small></span><span className="json-readonly"><LockKeyhole size={11} /> READ-ONLY</span></div>
          <pre className="json-preview"><code>{jsonPreview.split("\n").map((line, index) => <span className="json-line" key={index}><i>{String(index + 1).padStart(2, "0")}</i><span className={line.includes("read_only") || line.includes("sensor_transport") ? "json-key-safety" : line.includes(":") ? "json-normal" : ""}>{line}</span></span>)}</code></pre>
          <div className="mapped-channels">
            <div className="mapped-heading"><span>DIAGNOSTIC CHANNEL MAP</span><span>3 INPUTS · PASSIVE</span></div>
            {preset.channels.map((channel, index) => (
              <div className="mapped-channel" key={channel}><span className="channel-id">DI{index + 1}</span><span>{channel.replace(/_/g, " ")}</span><span className="channel-type">READ ONLY</span><Check size={13} /></div>
            ))}
          </div>
          <div className="mqtt-preview"><div><Cloud size={14} /><span>UNIFIED NAMESPACE TOPIC</span></div><code>mca/{SITE_ID}/{profile.machine_id || "{machine_id}"}/telemetry</code></div>
          <button className="deploy-button" onClick={deploy}><LockKeyhole size={15} /> APPROVE &amp; DEPLOY READ-ONLY CONFIG <ChevronRight size={16} /></button>
          <div className="human-gate"><CircleHelp size={13} /> Configuration staged locally · Supervisor approval gate active</div>
        </section>
      </div>
      {toast && <div className="toast"><Check size={16} />{toast}</div>}
    </main>
  );
}

function App() {
  const [machines, setMachines] = useState<Machine[]>(createDemoMachines);
  const [selectedId, setSelectedId] = useState("press_01");
  const [screen, setScreen] = useState<Screen>("overview");
  const [live, setLive] = useState(true);
  const [toast, setToast] = useState("");
  const tickRef = useRef(0);
  const cycleFractionsRef = useRef<Record<string, number>>({});
  const selectedMachine = useMemo(() => machines.find((machine) => machine.profile.machine_id === selectedId), [machines, selectedId]);

  const openMachine = useCallback((machineId: string) => {
    setSelectedId(machineId);
    setScreen("detail");
  }, []);

  useEffect(() => {
    if (!live) return;
    const timer = window.setInterval(() => {
      tickRef.current += 1;
      const tick = tickRef.current;
      setMachines((current) => current.map((machine, index) => {
        let status = machine.status;
        let downtimeSeconds = machine.downtimeSeconds;
        let activeReason = machine.activeReason;
        const phase = tick + index * 23;
        if (status === "MICRO_STOP") {
          downtimeSeconds += 1;
          if (downtimeSeconds >= 45) {
            status = "RUNNING";
            downtimeSeconds = 0;
            activeReason = undefined;
          }
        } else if (status === "FAULT") {
          downtimeSeconds += 1;
          if (downtimeSeconds >= 120) {
            status = "RUNNING";
            downtimeSeconds = 0;
            activeReason = undefined;
          }
        } else if (status === "IDLE") {
          downtimeSeconds += 1;
          if (downtimeSeconds >= 12) {
            status = "RUNNING";
            downtimeSeconds = 0;
          }
        } else if (phase > 0 && phase % 96 === 0) {
          status = phase % 288 === 0 ? "FAULT" : "MICRO_STOP";
          downtimeSeconds = 0;
          activeReason = status === "FAULT" ? "Sensor alarm" : "Brief line interruption";
        } else if (phase % 173 === 0) {
          status = "IDLE";
          downtimeSeconds = 0;
        }

        const isRunning = status === "RUNNING";
        const machineId = machine.profile.machine_id;
        let cycleFraction = cycleFractionsRef.current[machineId] || 0;
        if (isRunning) cycleFraction += Number(machine.profile.ideal_cycles_per_minute || 30) / 60;
        const cycleIncrement = isRunning ? Math.floor(cycleFraction) : 0;
        cycleFractionsRef.current[machineId] = cycleFraction - cycleIncrement;
        const cycleCount = machine.telemetry.cycle_count + cycleIncrement;
        const noise = Math.sin((tick + index * 13) / 8) * 0.012;
        const availability = Math.min(0.99, Math.max(0.55, machine.telemetry.availability + (isRunning ? 0.00006 : -0.0004)));
        const performance = Math.min(0.99, Math.max(0.65, machine.telemetry.performance + noise * 0.04));
        const quality = Math.min(0.999, Math.max(0.94, machine.telemetry.quality + noise * 0.01));
        const motorBase = Number(machine.profile.motor_full_load_amps || 12);
        const amps = isRunning ? motorBase * (1 + Math.sin(tick / 4 + index) * 0.045) : motorBase * (status === "FAULT" ? 0.24 : 0.12);
        const telemetry: Telemetry = {
          ...machine.telemetry,
          timestamp: new Date().toISOString(),
          cycle_count: cycleCount,
          motor_amps: Math.max(0, amps),
          availability,
          performance,
          quality,
          oee: availability * performance * quality,
        };
        const history = [...machine.history.slice(-27), telemetry.motor_amps / Math.max(1, motorBase * 1.35)];
        const events = status !== machine.status
          ? [{ timestamp: telemetry.timestamp, message: `Machine state changed to ${status.replace("_", " ")}`, kind: "status" as const }, ...machine.events].slice(0, 12)
          : machine.events;
        return { ...machine, status, downtimeSeconds, activeReason, telemetry, history, events };
      }));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [live]);

  const handleReason = useCallback((reason: string) => {
    if (!selectedMachine) return;
    const timestamp = new Date().toISOString();
    setMachines((current) => current.map((machine) => machine.profile.machine_id === selectedId
      ? {
          ...machine,
          status: "MICRO_STOP",
          downtimeSeconds: 0,
          activeReason: reason,
          events: [{ timestamp, message: `Operator logged downtime: ${reason}`, kind: "operator" as const }, ...machine.events].slice(0, 12),
        }
      : machine));
    setToast(`${reason} logged for ${selectedMachine.profile.machine_id}`);
    window.setTimeout(() => setToast(""), 3000);
  }, [selectedId, selectedMachine]);

  const deployMachine = useCallback((machine: Machine) => {
    setMachines((current) => [...current, machine]);
    setSelectedId(machine.profile.machine_id);
    setScreen("overview");
  }, []);

  return (
    <div className="app-shell">
      <Header screen={screen} setScreen={setScreen} live={live} setLive={setLive} />
      <div className="mode-strip"><span><span className="online-dot" />{live ? "SIMULATED LIVE TELEMETRY" : "SIMULATOR PAUSED"}</span><span><Wifi size={13} />EDGE NODE <b>CONNECTED</b></span><span><Cloud size={13} />UNS <b>mca/{SITE_ID}</b></span><span className="mode-spacer" /><span>SHIFT A &nbsp;·&nbsp; {new Date().toLocaleDateString([], { weekday: "short", month: "short", day: "2-digit" }).toUpperCase()}</span></div>
      {screen === "overview" && <Overview machines={machines} openMachine={openMachine} />}
      {screen === "detail" && (selectedMachine
        ? <Detail machine={selectedMachine} onBack={() => setScreen("overview")} onReason={handleReason} />
        : <main className="app-content"><p>Machine not found.</p><button className="back-button" onClick={() => setScreen("overview")}>Return to overview</button></main>)}
      {screen === "onboarding" && <Onboarding onDeploy={deployMachine} machineIds={machines.map((machine) => machine.profile.machine_id)} />}
      {toast && screen !== "onboarding" && <div className="toast"><Check size={16} />{toast}</div>}
      <footer className="app-footer"><span>© {new Date().getFullYear()} MOTORCITY AUTOMATED SYSTEMS, LLC</span><span><LockKeyhole size={11} /> ZERO-WRITE · READ-ONLY DIAGNOSTICS</span><span>EDGE HUB v1.0.4</span></footer>
    </div>
  );
}

export default App;
