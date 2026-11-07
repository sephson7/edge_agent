import type { Machine, MachineProfile, MachineStatus, SensorTransport } from "./types";

export const SITE_ID = "plant_01";
export const SITE_NAME = "Highland Park Plant 01";

interface DemoDefinition {
  id: string;
  name: string;
  make: string;
  model: string;
  machine_type: string;
  archetype: Machine["archetype"];
  transport: SensorTransport;
  status: MachineStatus;
  amps: number;
  cycleRate: number;
  target: number;
  channels: string[];
}

const demoDefinitions: DemoDefinition[] = [
  {
    id: "press_01",
    name: "Minster P2-100 Stamping Press",
    make: "Minster",
    model: "P2-100",
    machine_type: "Stamping Press",
    archetype: "stamping",
    transport: "wired",
    status: "RUNNING",
    amps: 18.4,
    cycleRate: 42,
    target: 1260,
    channels: ["main_motor_ct", "part_chute_prox", "fault_stack_light"],
  },
  {
    id: "press_02",
    name: "Bliss 60T Press",
    make: "Bliss",
    model: "60T",
    machine_type: "Stamping Press",
    archetype: "stamping",
    transport: "wireless_900mhz",
    status: "MICRO_STOP",
    amps: 2.1,
    cycleRate: 36,
    target: 1080,
    channels: ["main_motor_ct", "part_counter", "fault_stack_light"],
  },
  {
    id: "haas_03",
    name: "Haas VF-2 CNC Mill",
    make: "Haas",
    model: "VF-2",
    machine_type: "CNC Mill",
    archetype: "cnc",
    transport: "wired",
    status: "RUNNING",
    amps: 11.7,
    cycleRate: 8,
    target: 240,
    channels: ["spindle_current_ct", "cycle_complete", "machine_alarm"],
  },
  {
    id: "mold_04",
    name: "Cincinnati Milacron Injection Molder",
    make: "Cincinnati Milacron",
    model: "Roboshot 150",
    machine_type: "Injection Molder",
    archetype: "injection",
    transport: "wireless_900mhz",
    status: "FAULT",
    amps: 3.4,
    cycleRate: 12,
    target: 360,
    channels: ["hydraulic_motor_ct", "mold_cycle_prox", "fault_stack_light"],
  },
];

const initialParts = [284, 193, 81, 142];
const initialOee = [0.86, 0.72, 0.91, 0.64];
const initialAvailability = [0.94, 0.78, 0.96, 0.69];
const initialPerformance = [0.93, 0.91, 0.97, 0.95];
const initialQuality = [0.98, 0.99, 0.98, 0.98];

function makeProfile(definition: DemoDefinition): MachineProfile {
  return {
    machine_id: definition.id,
    site_id: SITE_ID,
    sensor_transport: definition.transport,
    make: definition.make,
    model: definition.model,
    name: definition.name,
    machine_type: definition.machine_type,
    ideal_cycles_per_minute: definition.cycleRate,
    motor_full_load_amps: definition.amps,
    channels: definition.channels.map((name, index) => ({
      channel_id: `DI${index + 1}`,
      name,
      read_only: true,
    })),
  };
}

export function createDemoMachines(): Machine[] {
  return demoDefinitions.map((definition, index) => ({
    profile: makeProfile(definition),
    name: definition.name,
    archetype: definition.archetype,
    status: definition.status,
    targetPerShift: definition.target,
    downtimeSeconds: definition.status === "MICRO_STOP" ? 23 : definition.status === "FAULT" ? 96 : 0,
    telemetry: {
      timestamp: new Date().toISOString(),
      motor_amps: definition.amps,
      cycle_count: initialParts[index],
      oee: initialOee[index],
      availability: initialAvailability[index],
      performance: initialPerformance[index],
      quality: initialQuality[index],
      sensor_transport: definition.transport,
    },
    history: Array.from({ length: 28 }, (_, sample) =>
      Math.max(0.35, initialOee[index] + Math.sin(sample * 0.61 + index) * 0.055 - sample * 0.001),
    ),
    events: [
      {
        timestamp: new Date(Date.now() - 1000 * (index * 7 + 4)).toISOString(),
        message:
          definition.status === "FAULT"
            ? "Hydraulic pressure alarm — operator notified"
            : definition.status === "MICRO_STOP"
              ? "Short-cycle stop detected"
              : "Read-only polling active",
        kind: definition.status === "RUNNING" ? "system" : "status",
      },
    ],
  }));
}

export const reasonCodes: Record<Machine["archetype"], string[]> = {
  stamping: ["Die Jam", "Coil Change", "Scrap Clear", "Air Pressure Low", "Setup", "Maintenance"],
  cnc: ["Tool Breakage", "Coolant Low", "Part Inspection", "Program Hold", "Chip Clear", "Maintenance"],
  injection: ["Mold Jam", "Material Refill", "Part Inspection", "Temperature Recovery", "Setup", "Maintenance"],
};

export const statusLabels: Record<Machine["status"], string> = {
  RUNNING: "Running",
  MICRO_STOP: "Micro-stop",
  FAULT: "Fault",
  IDLE: "Idle",
};

export const statusCycle: Machine["status"][] = ["RUNNING", "MICRO_STOP", "RUNNING", "IDLE", "RUNNING", "FAULT"];
