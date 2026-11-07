"""Mobile-friendly image upload server for testing the onboarding pipeline."""

from __future__ import annotations

import asyncio
import html
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse


ROOT = Path(__file__).resolve().parent
UPLOAD_DIR = ROOT / "uploads"
CONFIG_DIR = ROOT / "configs"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}

app = FastAPI(title="MCA Edge Agent Upload Test")


def _render_page(
    *,
    machine_id: str = "",
    result: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    safe_machine_id = html.escape(machine_id, quote=True)
    output_section = ""
    if result is not None:
        output_section = f"""
        <section class="result success" aria-live="polite">
          <h2>Validated configuration</h2>
          <pre>{html.escape(result)}</pre>
          <button type="button" class="approve" title="Approval is not connected yet">
            Confirm &amp; Approve Config
          </button>
          <p class="placeholder">Approval is a placeholder and does not deploy this config.</p>
        </section>"""
    elif error is not None:
        output_section = f"""
        <section class="result failure" aria-live="polite">
          <h2>Onboarding failed</h2>
          <pre>{html.escape(error)}</pre>
        </section>"""

    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MCA Edge Agent - Photo Upload</title>
  <style>
    :root {{ color-scheme: light; font-family: system-ui, sans-serif; color: #17212b; background: #f2f5f8; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; padding: 24px 16px; }}
    main {{ max-width: 720px; margin: 0 auto; }}
    .card {{ background: white; border-radius: 16px; padding: clamp(20px, 5vw, 36px); box-shadow: 0 8px 30px #17212b12; }}
    h1 {{ margin-top: 0; font-size: clamp(1.5rem, 5vw, 2rem); }}
    label {{ display: block; font-weight: 650; margin: 22px 0 8px; }}
    input[type=file] {{ display: block; width: 100%; min-height: 100px; padding: 16px; border: 2px dashed #8292a3; border-radius: 12px; font-size: 1rem; }}
    input[type=text] {{ width: 100%; min-height: 48px; padding: 10px 12px; border: 1px solid #aeb9c4; border-radius: 8px; font: inherit; }}
    button {{ width: 100%; min-height: 54px; margin-top: 24px; border: 0; border-radius: 10px; background: #0759a5; color: white; font: inherit; font-size: 1.05rem; font-weight: 700; cursor: pointer; }}
    button:disabled {{ opacity: .7; cursor: wait; }}
    .hint, .placeholder {{ color: #526170; font-size: .92rem; }}
    .result {{ margin-top: 24px; padding: 18px; border-radius: 12px; }}
    .success {{ background: #edf8f1; border: 1px solid #91c9a2; }}
    .failure {{ background: #fff1f0; border: 1px solid #e6a19b; }}
    pre {{ overflow-wrap: anywhere; white-space: pre-wrap; background: #101923; color: #f2f6fa; padding: 16px; border-radius: 8px; overflow-x: auto; }}
    .approve {{ background: #277446; }}
    @media (max-width: 480px) {{ body {{ padding: 12px; }} }}
  </style>
</head>
<body>
  <main>
    <section class="card">
      <h1>Machine Photo Onboarding</h1>
      <p>Upload a nameplate or machine photo to run the validated onboarding pipeline.</p>
      <form id="upload-form" action="/upload" method="post" enctype="multipart/form-data">
        <label for="photo">Machine photo</label>
        <input id="photo" type="file" name="photo" accept="image/*" capture="environment" required>
        <p class="hint">For this single-image test, the uploaded image is supplied as both nameplate and panel input.</p>
        <label for="machine_id">Machine ID (optional)</label>
        <input id="machine_id" type="text" name="machine_id" value="{safe_machine_id}" placeholder="machine_20261107_073618" maxlength="64" pattern="[A-Za-z0-9][A-Za-z0-9_-]{{0,63}}">
        <button id="submit-button" type="submit">Run Onboarding Pipeline</button>
        <p id="loading" class="hint" hidden>Processing photo and validating configuration…</p>
      </form>
      {output_section}
    </section>
  </main>
  <script>
    document.getElementById("upload-form").addEventListener("submit", function () {{
      const button = document.getElementById("submit-button");
      button.disabled = true;
      button.textContent = "Processing…";
      document.getElementById("loading").hidden = false;
    }});
  </script>
</body>
</html>"""
    )


@app.get("/", response_class=HTMLResponse)
async def upload_form() -> HTMLResponse:
    default_id = f"machine_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}"
    return _render_page(machine_id=default_id)


@app.post("/upload", response_class=HTMLResponse)
async def upload_photo(
    photo: UploadFile = File(...),
    machine_id: str = Form(""),
) -> HTMLResponse:
    content_type = (photo.content_type or "").lower()
    extension = IMAGE_EXTENSIONS.get(content_type)
    if extension is None:
        return _render_page(machine_id=machine_id, error="Upload a JPEG, PNG, WebP, GIF, or BMP image.")

    requested_id = machine_id.strip() or f"machine_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", requested_id):
        return _render_page(
            machine_id=machine_id,
            error="Machine ID must start with a letter or digit and contain only letters, digits, underscores, or hyphens (up to 64 characters).",
        )
    normalized_id = requested_id.lower()

    image = await photo.read(MAX_UPLOAD_BYTES + 1)
    await photo.close()
    if not image:
        return _render_page(machine_id=normalized_id, error="The uploaded image is empty.")
    if len(image) > MAX_UPLOAD_BYTES:
        return _render_page(machine_id=normalized_id, error="The image exceeds the 20 MiB upload limit.")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    image_path = UPLOAD_DIR / f"{uuid.uuid4().hex}{extension}"
    try:
        image_path.write_bytes(image)
    except OSError as exc:
        return _render_page(machine_id=normalized_id, error=f"Unable to save uploaded image: {exc}")

    command = [
        sys.executable,
        str(ROOT / "agent.py"),
        "--nameplate",
        str(image_path),
        "--panel",
        str(image_path),
        "--machine-id",
        normalized_id,
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
        return _render_page(machine_id=normalized_id, error="Onboarding timed out after 5 minutes.")
    except OSError as exc:
        return _render_page(machine_id=normalized_id, error=f"Unable to start agent.py: {exc}")

    if process.returncode != 0:
        details = (stderr or stdout).decode("utf-8", errors="replace").strip()
        if not details:
            details = f"agent.py exited with status {process.returncode}."
        return _render_page(machine_id=normalized_id, error=details)

    config_path = CONFIG_DIR / f"{normalized_id}.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _render_page(
            machine_id=normalized_id,
            error=f"agent.py succeeded but its validated config could not be read from {config_path}: {exc}",
        )

    return _render_page(
        machine_id=normalized_id,
        result=json.dumps(config, indent=2, ensure_ascii=False),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
