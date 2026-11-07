export type SensorTransport = "wired" | "wireless_900mhz";
export type MachineStatus = "RUNNING" | "MICRO_STOP" | "FAULT" | "IDLE";
export type Screen = "overview" | "detail" | "onboarding";

export interface ChannelConfig {
  read_only: true;
  [key: string]: unknown;
}

export interface MachineProfile {
  machine_id: string;
  site_id: string;
  sensor_transport: SensorTransport;
  channels: ChannelConfig[] | Record<string, ChannelConfig>;
  [key: string]: unknown;
}

export interface Telemetry {
  timestamp: string;
  motor_amps: number;
  cycle_count: number;
  oee: number;
  availability: number;
  performance: number;
  quality: number;
  sensor_transport: SensorTransport;
}

export interface Machine {
  profile: MachineProfile;
  name: string;
  archetype: "stamping" | "cnc" | "injection";
  status: MachineStatus;
  telemetry: Telemetry;
  targetPerShift: number;
  downtimeSeconds: number;
  activeReason?: string;
  history: number[];
  events: MachineEvent[];
}

export interface MachineEvent {
  timestamp: string;
  message: string;
  kind: "status" | "operator" | "system";
}

export function profileChannels(profile: MachineProfile): ChannelConfig[] {
  return Array.isArray(profile.channels) ? profile.channels : Object.values(profile.channels);
}
