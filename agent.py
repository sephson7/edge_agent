"""Extract a read-only machine profile from nameplate and panel images."""

from __future__ import annotations

import argparse
import base64
import json
import logging
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, ValidationError

from validator import MachineProfile


LOGGER = logging.getLogger("agent")
DEFAULT_OPENAI_MODEL = "gpt-4o"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

EXTRACTION_PROMPT = """\
Inspect the attached nameplate and control panel images. Classify the machine
using exactly one supplied archetype: stamping_press, cnc_mill,
injection_molder, hydraulic_press, or conveyor_line. Extract only the visible
OEM, model, serial number, voltage, and PLC family. Use null for illegible or
unavailable fields and briefly explain uncertainty in confidence_notes. Do not
output channels, debounce settings, downtime codes, OEE settings, or any
machine configuration. Return only the requested NameplateExtraction JSON.
"""

class NameplateExtraction(BaseModel):
    """Only OCR and archetype classification fields permitted from the LLM."""

    model_config = ConfigDict(extra="forbid")

    archetype: Literal[
        "stamping_press",
        "cnc_mill",
        "injection_molder",
        "hydraulic_press",
        "conveyor_line",
    ]
    oem: str | None = None
    model: str | None = None
    serial_number: str | None = None
    voltage: str | None = None
    plc_family: str | None = None
    confidence_notes: str = ""


def _read_image(path: Path) -> tuple[str, bytes]:
    if not path.is_file():
        raise ValueError(f"Image file does not exist or is not a file: {path}")
    mime_type, _encoding = mimetypes.guess_type(path.name)
    if not mime_type or not mime_type.startswith("image/"):
        raise ValueError(f"Unsupported image type for '{path}'; use a recognized image extension")
    try:
        image_bytes = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"Unable to read image '{path}': {exc}") from exc
    if not image_bytes:
        raise ValueError(f"Image file is empty: {path}")
    return mime_type, image_bytes


def _prompt() -> str:
    return EXTRACTION_PROMPT


def _extract_with_openai(
    api_key: str,
    model: str,
    nameplate: tuple[str, bytes],
    panel: tuple[str, bytes],
) -> NameplateExtraction:
    try:
        from openai import APIError, OpenAI
    except ImportError as exc:
        raise RuntimeError("OpenAI provider selected; install dependencies with `pip install -r requirements.txt`") from exc

    client = OpenAI(api_key=api_key)
    content: list[dict[str, Any]] = [{"type": "text", "text": _prompt()}]
    for label, (mime_type, image_bytes) in (("Nameplate", nameplate), ("Control panel", panel)):
        data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
        content.extend(
            [
                {"type": "text", "text": label},
                {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}},
            ]
        )

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": content}],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "nameplate_extraction",
                    "schema": NameplateExtraction.model_json_schema(mode="validation"),
                    "strict": False,
                },
            },
        )
    except APIError as exc:
        raise RuntimeError(f"OpenAI API request failed: {exc}") from exc

    output = response.choices[0].message.content
    if not output:
        raise RuntimeError("OpenAI returned an empty nameplate extraction")
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"OpenAI returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("OpenAI response must be a JSON object")
    try:
        return NameplateExtraction.model_validate(parsed)
    except ValidationError as exc:
        raise RuntimeError(f"OpenAI response did not match NameplateExtraction: {exc}") from exc


def _extract_with_gemini(
    api_key: str,
    model: str,
    nameplate: tuple[str, bytes],
    panel: tuple[str, bytes],
) -> NameplateExtraction:
    try:
        from google import genai
        from google.genai import errors, types
    except ImportError as exc:
        raise RuntimeError("Gemini provider selected; install dependencies with `pip install -r requirements.txt`") from exc

    client = genai.Client(api_key=api_key)
    contents: list[Any] = [_prompt()]
    for label, (mime_type, image_bytes) in (("Nameplate", nameplate), ("Control panel", panel)):
        contents.extend([label, types.Part.from_bytes(data=image_bytes, mime_type=mime_type)])

    try:
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=NameplateExtraction,
            ),
        )
    except errors.APIError as exc:
        raise RuntimeError(f"Gemini API request failed: {exc}") from exc

    output = response.text
    if not output:
        raise RuntimeError("Gemini returned an empty nameplate extraction")
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Gemini returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Gemini response must be a JSON object")
    try:
        return NameplateExtraction.model_validate(parsed)
    except ValidationError as exc:
        raise RuntimeError(f"Gemini response did not match NameplateExtraction: {exc}") from exc


def extract_nameplate(
    provider: str,
    api_key: str,
    model: str,
    nameplate_path: Path,
    panel_path: Path,
) -> NameplateExtraction:
    """Send images to the selected multimodal provider for metadata extraction."""

    nameplate = _read_image(nameplate_path)
    panel = _read_image(panel_path)
    if provider == "openai":
        extraction = _extract_with_openai(api_key, model, nameplate, panel)
    elif provider == "gemini":
        extraction = _extract_with_gemini(api_key, model, nameplate, panel)
    else:
        raise ValueError(f"Unsupported provider: {provider}")
    return extraction


def merge_extraction_with_template(
    extraction: NameplateExtraction,
    *,
    machine_id: str,
    site_id: str,
    sensor_transport: str,
    templates_dir: Path | None = None,
) -> MachineProfile:
    """Merge extracted metadata into a deterministic, read-only archetype."""

    template_root = templates_dir or Path(__file__).resolve().parent / "templates"
    template_path = template_root / f"{extraction.archetype}.json"
    try:
        template_data = json.loads(template_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Unable to read archetype template '{template_path}': {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Archetype template '{template_path}' is not valid JSON: {exc}") from exc
    if not isinstance(template_data, dict):
        raise ValueError(f"Archetype template '{template_path}' must contain a JSON object")
    if template_data.get("archetype") != extraction.archetype:
        raise ValueError(
            f"Archetype template '{template_path}' does not match '{extraction.archetype}'"
        )

    merged = dict(template_data)
    merged.update(
        {
            "machine_id": machine_id,
            "site_id": site_id,
            "sensor_transport": sensor_transport,
        }
    )
    extracted_metadata = extraction.model_dump(
        mode="json",
        exclude={"archetype", "confidence_notes"},
        exclude_none=True,
    )
    merged.update(extracted_metadata)
    return MachineProfile.model_validate(merged)


def _machine_slug(*values: str | None) -> str:
    source = "-".join(str(value).strip() for value in values if value)
    slug = re.sub(r"[^a-z0-9]+", "_", source.lower()).strip("_")
    slug = re.sub(r"_+", "_", slug)
    if not slug:
        raise ValueError("Could not derive a machine ID; provide --machine-id")
    return slug[:64].rstrip("_-")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nameplate", required=True, type=Path, help="Machine nameplate image path")
    parser.add_argument("--panel", required=True, type=Path, help="Control-panel image path")
    parser.add_argument(
        "--provider",
        choices=("gemini", "openai"),
        default=os.getenv("LLM_PROVIDER", "gemini").lower(),
        help="Multimodal API provider (default: LLM_PROVIDER or gemini)",
    )
    parser.add_argument("--machine-id", help="Machine ID; defaults to a slug from extracted OEM and model")
    parser.add_argument("--site-id", default="default", help="Site ID for UNS topic routing (default: default)")
    parser.add_argument(
        "--sensor-transport",
        choices=("wired", "wireless_900mhz"),
        default="wired",
        help="Machine sensor transport (default: wired)",
    )
    parser.add_argument("--model", help="Override the provider's default model")
    parser.add_argument("--verbose", action="store_true", help="Log extraction progress")
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    provider_env = "OPENAI_API_KEY" if args.provider == "openai" else "GEMINI_API_KEY"
    api_key = os.getenv(provider_env) or os.getenv("LLM_API_KEY")
    if not api_key:
        LOGGER.error("Set %s or LLM_API_KEY to use the %s provider", provider_env, args.provider)
        return 2
    default_model = DEFAULT_OPENAI_MODEL if args.provider == "openai" else DEFAULT_GEMINI_MODEL
    model = args.model or os.getenv(
        "OPENAI_MODEL" if args.provider == "openai" else "GEMINI_MODEL",
        default_model,
    )

    output_path: Path | None = None
    try:
        extraction = extract_nameplate(args.provider, api_key, model, args.nameplate, args.panel)
        machine_id = _machine_slug(args.machine_id) if args.machine_id else _machine_slug(extraction.oem, extraction.model)
        profile = merge_extraction_with_template(
            extraction,
            machine_id=machine_id,
            site_id=args.site_id,
            sensor_transport=args.sensor_transport,
        )
        output_path = Path("configs") / f"{machine_id}.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(profile.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except (OSError, ValueError, ValidationError, RuntimeError) as exc:
        LOGGER.error("Machine profile extraction failed: %s", exc)
        return 1

    LOGGER.info("Saved validated machine profile to %s", output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
