# MCA Machine Health PWA

Responsive React + Vite + Tailwind dashboard for read-only monitoring demos.
The demo machines and onboarding JSON use the `MachineProfile` fields declared
in `../validator.py`: `machine_id`, `site_id`, `sensor_transport`, and
`channels`, with `read_only: true` on every channel. Additional profile values
are allowed, matching Pydantic's `extra="allow"` configuration.

```bash
npm install
npm run dev
npm run build
npm run preview
```

The browser simulator is on by default and creates one-second telemetry locally.
To connect a broker later, replace the simulator state source with a WebSocket
MQTT client subscribed to:

- `mca/{site_id}/{machine_id}/status`
- `mca/{site_id}/{machine_id}/telemetry`
- `mca/{site_id}/fleet/manifest`

`public/manifest.json` configures landscape standalone installation. The
service worker caches the app shell and same-origin assets for offline launch;
installability and service-worker behavior require HTTPS or localhost.
