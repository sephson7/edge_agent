"""Pydantic models and safety checks for machine profiles."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_WRITE_PERMISSION_KEYS = {
    "write",
    "writable",
    "writeenabled",
    "writepermission",
    "writepermissions",
    "writeaccess",
    "canwrite",
    "allowwrite",
}
_PERMISSION_KEYS = {"permission", "permissions", "access", "accessmode", "operation", "operations", "mode"}
_WRITE_PERMISSION_VALUES = {"write", "writable", "rw", "readwrite", "writeonly"}


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return _normalized(value) not in {"", "false", "no", "none", "null", "disabled", "off", "0"}
    return bool(value)


def _permission_contains_write(value: Any) -> bool:
    if isinstance(value, str):
        return _normalized(value) in _WRITE_PERMISSION_VALUES
    if isinstance(value, (list, tuple, set)):
        return any(_permission_contains_write(item) for item in value)
    if isinstance(value, dict):
        return any(
            (_normalized(str(key)) in _WRITE_PERMISSION_VALUES and _enabled(item))
            or _permission_contains_write(item)
            for key, item in value.items()
        )
    return False


def _find_write_permission(value: Any, path: str) -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = _normalized(str(key))
            child_path = f"{path}.{key}"
            if normalized_key in _WRITE_PERMISSION_KEYS and _enabled(child):
                return child_path
            if normalized_key == "readonly" and child is False:
                return child_path
            if normalized_key in _PERMISSION_KEYS and _permission_contains_write(child):
                return child_path
            result = _find_write_permission(child, child_path)
            if result is not None:
                return result
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            result = _find_write_permission(child, f"{path}[{index}]")
            if result is not None:
                return result
    return None


def _find_register_write_permission(value: Any, path: str = "") -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if "register" in _normalized(str(key)):
                result = _find_write_permission(child, child_path)
                if result is not None:
                    return result
            result = _find_register_write_permission(child, child_path)
            if result is not None:
                return result
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            result = _find_register_write_permission(child, f"{path}[{index}]")
            if result is not None:
                return result
    return None


class ChannelConfig(BaseModel):
    """A machine input channel, locked to passive read-only polling."""

    model_config = ConfigDict(extra="allow")

    channel_id: str
    read_only: bool = False


class MachineProfile(BaseModel):
    """Extensible machine configuration with mandatory read-only safeguards."""

    model_config = ConfigDict(extra="allow")

    machine_id: str = ""
    site_id: str = Field(default="default", pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    sensor_transport: str = "wired"
    channels: list[ChannelConfig] | dict[str, ChannelConfig] = Field(default_factory=list)
    downtime_codes: list[str] = Field(default_factory=list)

    @field_validator("machine_id")
    @classmethod
    def validate_machine_id(cls, value: str) -> str:
        if value and not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value):
            raise ValueError("machine_id must be a lowercase MQTT-safe identifier")
        return value

    @model_validator(mode="after")
    def validate_read_only_profile(self) -> "MachineProfile":
        allowed_transports = {"wired", "wireless_900mhz", "wireless_lorawan"}
        if self.sensor_transport not in allowed_transports:
            raise ValueError(
                "sensor_transport must be one of: wired, wireless_900mhz, wireless_lorawan"
            )

        channel_entries: list[ChannelConfig] = []
        if isinstance(self.channels, dict):
            channel_entries.extend(self.channels.values())
        else:
            channel_entries.extend(self.channels)

        seen_ids: set[str] = set()
        for channel in channel_entries:
            if not isinstance(channel, ChannelConfig):
                continue
            if channel.channel_id in seen_ids:
                raise ValueError(f"Duplicate channel_id '{channel.channel_id}' is not allowed")
            seen_ids.add(channel.channel_id)
            if channel.read_only is not True:
                raise ValueError(f"Channel '{channel.channel_id}' must set read_only to true")

        if not self.downtime_codes:
            raise ValueError("Machine profile must define at least one downtime code")

        return self

    @model_validator(mode="before")
    @classmethod
    def reject_writable_registers(cls, value: Any) -> Any:
        if isinstance(value, dict):
            path = _find_register_write_permission(value)
            if path is not None:
                raise ValueError(f"register configuration grants write access at '{path}'")
        return value


def validate_machine_profile(data: dict[str, Any]) -> MachineProfile:
    """Validate a machine profile and enforce its read-only safety constraints."""

    return MachineProfile.model_validate(data)
