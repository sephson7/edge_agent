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
from typing import Any

from dotenv import load_dotenv
from pydantic import ValidationError

from validator import MachineProfile


LOGGER = logging.getLogger("agent")
DEFAULT_OPENAI_MODEL = "gpt-4o"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

EXTRACTION_PROMPT = """\
Inspect both attached machine images and create a practical machine profile.
Use the nameplate to identify OEM, model, serial number, voltage, and other
legible specifications. Use the control-panel image to identify the PLC family,
visible protocols, and observable diagnostic channels. Include useful machine
metadata and a channel entry for each confidently identified diagnostic input.
Do not invent unreadable values; omit unknown optional values.

This is a strictly read-only diagnostic system. Every channel must have
read_only set to true. Never include a register configuration that grants write
permissions. Include only passive polling or non-invasive measurement details.
For unrecognized values, follow the supplied MachineProfile JSON schema and
return a single JSON object only.

MachineProfile JSON schema:
{schema}
"""


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
    schema = MachineProfile.model_json_schema(mode="validation")
    return EXTRACTION_PROMPT.format(schema=json.dumps(schema, indent=2))


def _extract_with_openai(
    api_key: str,
    model: str,
    nameplate: tuple[str, bytes],
    panel: tuple[str, bytes],
) -> dict[str, Any]:
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
                    "name": "machine_profile",
                    "schema": MachineProfile.model_json_schema(mode="validation"),
                    "strict": False,
                },
            },
        )
    except APIError as exc:
        raise RuntimeError(f"OpenAI API request failed: {exc}") from exc

    output = response.choices[0].message.content
    if not output:
        raise RuntimeError("OpenAI returned an empty machine profile")
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"OpenAI returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("OpenAI response must be a JSON object")
    return parsed


def _extract_with_gemini(
    api_key: str,
    model: str,
    nameplate: tuple[str, bytes],
    panel: tuple[str, bytes],
) -> dict[str, Any]:
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
                response_schema=MachineProfile,
            ),
        )
    except errors.APIError as exc:
        raise RuntimeError(f"Gemini API request failed: {exc}") from exc

    output = response.text
    if not output:
        raise RuntimeError("Gemini returned an empty machine profile")
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Gemini returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Gemini response must be a JSON object")
    return parsed


def extract_profile(
    provider: str,
    api_key: str,
    model: str,
    nameplate_path: Path,
    panel_path: Path,
) -> MachineProfile:
    """Send images to the selected multimodal provider and validate its profile."""

    nameplate = _read_image(nameplate_path)
    panel = _read_image(panel_path)
    if provider == "openai":
        data = _extract_with_openai(api_key, model, nameplate, panel)
    elif provider == "gemini":
        data = _extract_with_gemini(api_key, model, nameplate, panel)
    else:
        raise ValueError(f"Unsupported provider: {provider}")
    return MachineProfile.model_validate(data)


def _machine_slug(*values: Any) -> str:
    source = "-".join(str(value).strip() for value in values if value)
    slug = re.sub(r"[^a-z0-9]+", "_", source.lower()).strip("_")
    slug = re.sub(r"_+", "_", slug)
    if not slug:
        raise ValueError("Could not derive a machine ID; provide --machine-id")
    return slug[:64].rstrip("_-")


def _derived_machine_id(profile: MachineProfile) -> str:
    data = profile.model_dump(mode="python")
    make = data.get("oem") or data.get("manufacturer") or data.get("make")
    model = data.get("model") or data.get("model_number")
    machine = data.get("machine")
    if isinstance(machine, dict):
        make = make or machine.get("oem") or machine.get("manufacturer") or machine.get("make")
        model = model or machine.get("model")
    return _machine_slug(make, model)


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
        profile = extract_profile(args.provider, api_key, model, args.nameplate, args.panel)
        machine_id = _machine_slug(args.machine_id) if args.machine_id else _derived_machine_id(profile)
        profile_data = profile.model_dump(mode="python")
        profile_data["machine_id"] = machine_id
        profile_data["site_id"] = args.site_id
        profile_data["sensor_transport"] = args.sensor_transport
        profile = MachineProfile.model_validate(profile_data)
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
