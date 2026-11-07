# MCA Edge Agent (`/edge_agent`)

The **MCA Edge Agent** is the automated onboarding and configuration engine for the **Motorcity Automated Systems (MCA) Legacy Machine Health Kit**. 

Operating strictly as a **read-only diagnostic tool** (similar to an automotive OBD-II scanner), this agent takes field photos of legacy industrial machinery and control cabinets, extracts hardware and protocol metadata using a multimodal LLM, and generates a validated JSON configuration bundle. That bundle automatically provisions the edge gateway's polling drivers, Unified Namespace (UNS) MQTT topics, deterministic OEE formulas, and operator downtime buttons in the MCA Progressive Web App (PWA).

---

## Core Architecture & Safety Guardrails

1. **Zero-Write / Read-Only Isolation:** 
   The agent **never** generates PLC write commands or modifies existing machine ladder logic. All generated driver configurations are strictly locked to passive read-only polling (DF1, Modbus RTU/TCP, EtherNet/IP) or physical non-invasive I/O taps (split-core CT clamps, optical part counters, 24VDC stack-light relays).
2. **Deterministic Math Over LLM Guessing:** 
   The AI agent is used strictly for visual extraction, machine archetype classification, and structured configuration generation. Live OEE calculations (Availability × Performance × Quality) and state-machine timers are executed by a deterministic edge engine—never by an LLM.
3. **Human-in-the-Loop Gate:** 
   Every generated configuration stages in a `PENDING_APPROVAL` state for a 60-second technician review before being pushed to the edge gateway.

---

## Directory Structure

```text
/edge_agent
├── README.md               # Architecture and usage guide
├── requirements.txt        # Python dependencies
├── agent.py                # Main Vision-to-JSON extraction & classification pipeline
├── validator.py            # Pydantic schema enforcement & read-only safety checks
├── templates/              # Base archetypes (stamping_press.json, cnc_mill.json, injection_molder.json)
└── sample_inputs/          # Test nameplate and control panel photos for local validation
```

---

## How the Onboarding Pipeline Works

1. **Capture Field Inputs:** Technician uploads photos of the machine nameplate, interior control cabinet, and wired gateway channels via the MCA Field PWA.
2. **Vision Extraction (`agent.py`):** Multimodal model extracts OEM make, model, serial number, voltage, and PLC family (e.g., *Minster P2-100 Stamping Press, Allen-Bradley SLC 5/04*).
3. **Archetype & Tag Mapping:** Matches the machine against `/templates` to assign default ideal cycle rates, debounce timers, MQTT topic paths (`site/line/machine/metric`), and 6–8 machine-specific operator downtime reason codes.
4. **Schema Validation (`validator.py`):** Verifies strict adherence to `/schemas/machine_profile.schema.json` and strips any non-read-only register flags.
5. **Provisioning Output:** Exports `machine_config.json` ready for deployment to the edge gateway and PWA frontend.

---

## Quickstart (Local Development)

### 1. Install Dependencies
```bash
cd edge_agent
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure Environment Variables
Create a `.env` file in `/edge_agent`:
```env
LLM_API_KEY=your_api_key_here
MQTT_BROKER_HOST=localhost
MQTT_BROKER_PORT=1883
```

### 3. Run the Agent Against Sample Images
```bash
python agent.py \
  --nameplate ./sample_inputs/minster_nameplate.jpg \
  --panel ./sample_inputs/slc500_cabinet.jpg \
  --channels "DI1:main_motor_ct,DI2:part_chute_prox,DI3:fault_stack_light" \
  --output ./output/press_04_config.json
```

---

## Roadmap
- [ ] Add automated register-map lookup for Allen-Bradley SLC 500 / MicroLogix DF1 serial tables.
- [ ] Add passive network discovery helper (`nmap` / EtherNet/IP CIP identity scanner) to assist photo extraction.
- [ ] Build one-click config push endpoint to sync validated JSON directly to the edge gateway over WireGuard/HTTPS.
