"""Display-name helpers for source entities."""
from __future__ import annotations

from homeassistant.const import ATTR_FRIENDLY_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import SOURCE_DISPLAY_NAME_OVERRIDES

_ACRONYMS = {
    "a19": "A19",
    "ev": "EV",
    "hdmi": "HDMI",
    "hvac": "HVAC",
    "ip": "IP",
    "led": "LED",
    "rgb": "RGB",
    "tv": "TV",
    "ups": "UPS",
    "usb": "USB",
}


def _usable_name(value: object, entity_id: str) -> str | None:
    """Return a non-empty name unless it merely repeats the raw entity id."""
    if not isinstance(value, str):
        return None
    name = " ".join(value.split())
    if not name:
        return None
    object_id = entity_id.split(".", 1)[-1]
    if name.casefold() in {entity_id.casefold(), object_id.casefold()}:
        return None
    return name


def humanize_entity_id(entity_id: str) -> str:
    """Turn a raw entity id into a readable last-resort display name."""
    object_id = entity_id.split(".", 1)[-1]
    words = object_id.replace("-", "_").split("_")
    return " ".join(
        _ACRONYMS.get(word.casefold(), word[:1].upper() + word[1:])
        for word in words
        if word
    ) or entity_id


def source_display_name(hass: HomeAssistant, entity_id: str) -> str:
    """Return the best current display name for a tracked source entity.

    User-assigned Home Assistant names take precedence. Registry names cover
    sources that are temporarily unavailable during integration setup. The
    raw entity id is never exposed unless it cannot be parsed at all.
    """
    override = _usable_name(SOURCE_DISPLAY_NAME_OVERRIDES.get(entity_id), entity_id)
    if override:
        return override

    state = hass.states.get(entity_id)
    if state:
        friendly = _usable_name(state.attributes.get(ATTR_FRIENDLY_NAME), entity_id)
        if friendly:
            return friendly

    registry_entry = er.async_get(hass).async_get(entity_id)
    if registry_entry:
        for candidate in (registry_entry.name, registry_entry.original_name):
            registry_name = _usable_name(candidate, entity_id)
            if registry_name:
                return registry_name

    if state:
        state_name = _usable_name(state.name, entity_id)
        if state_name:
            return state_name

    return humanize_entity_id(entity_id)
