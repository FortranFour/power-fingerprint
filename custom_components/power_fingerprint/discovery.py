"""Candidate discovery and classification helpers for future setup flows.

0.4.4 introduced these helpers without changing an existing user's configured
tracked list. New installs can be seeded with unmetered candidates for explicit
include/exclude review; future setup/options UI can add per-device reclassification.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, SUPPORTED_TRACKED_DOMAINS
from .models import DeviceProfile
from .naming import source_display_name

_HEAVY_TYPES = {"hvac", "ev_charger", "oven_stove", "water_heater", "dryer"}


def classify_device_type(entity_id: str, friendly_name: str | None = None) -> tuple[str, str]:
    """Return (device_type, classification_source) using conservative hints."""
    domain = entity_id.split(".", 1)[0]
    text = f"{entity_id} {friendly_name or ''}".lower().replace("_", " ")

    # Name hints are intentionally conservative. They are suggestions only; a
    # future setup/reclassification step remains authoritative.
    hints = (
        (("ev charger", "car charger", "vehicle charger", "wall connector", "evse"), "ev_charger"),
        (("heat pump", "furnace", "thermostat", "air conditioner", "hvac", "mini split"), "hvac"),
        (("microwave",), "microwave"),
        (("oven", "range", "stove", "cooktop"), "oven_stove"),
        (("toaster",), "toaster"),
        (("water heater",), "water_heater"),
        (("dryer",), "dryer"),
        (("washer", "washing machine"), "washer"),
        (("dishwasher",), "dishwasher"),
        (("refrigerator", "fridge", "freezer"), "refrigerator"),
    )
    for terms, kind in hints:
        if any(term in text for term in terms):
            return kind, "name_hint"

    defaults = {
        "climate": "hvac",
        "light": "lighting",
        "fan": "fan",
        "media_player": "electronics",
        "switch": "generic_load",
    }
    return defaults.get(domain, "generic_load"), "domain_default"


def default_device_profile(hass: HomeAssistant, entity_id: str) -> DeviceProfile:
    """Build non-destructive metadata for an already-configured tracked entity."""
    kind, source = classify_device_type(entity_id, source_display_name(hass, entity_id))
    return DeviceProfile(
        entity_id=entity_id,
        device_type=kind,
        classification_source=source,
        selection_source="configured",
        user_confirmed=False,
        heavy_load=kind in _HEAVY_TYPES,
        source_kind="ha_entity",
        source_ref=entity_id,
    )


def discover_unmetered_candidates(hass: HomeAssistant) -> list[DeviceProfile]:
    """Suggest controllable entities whose HA device lacks power/energy sensors.

    New-install setup may preselect these suggestions, but the user still reviews
    the tracked-entity selector before creating the entry. Existing installations
    retain their configured list and are never replaced by discovery results.
    """
    registry = er.async_get(hass)
    metered_device_ids: set[str] = set()

    for entry in registry.entities.values():
        if not entry.device_id or entry.entity_id.split(".", 1)[0] != "sensor":
            continue
        state = hass.states.get(entry.entity_id)
        device_class = str(state.attributes.get("device_class", "")) if state else ""
        if device_class in {"power", "energy"}:
            metered_device_ids.add(entry.device_id)

    candidates: list[DeviceProfile] = []
    for state in sorted(hass.states.async_all(), key=lambda item: item.entity_id):
        entity_id = state.entity_id
        domain = entity_id.split(".", 1)[0]
        if domain not in SUPPORTED_TRACKED_DOMAINS or entity_id.startswith(f"{DOMAIN}."):
            continue
        entry = registry.async_get(entity_id)
        if entry and entry.disabled_by is not None:
            continue
        if entry and entry.device_id and entry.device_id in metered_device_ids:
            continue
        kind, source = classify_device_type(entity_id, source_display_name(hass, entity_id))
        candidates.append(DeviceProfile(
            entity_id=entity_id,
            device_type=kind,
            classification_source=source,
            selection_source="auto_discovered_candidate",
            user_confirmed=False,
            heavy_load=kind in _HEAVY_TYPES,
            source_kind="ha_entity",
            source_ref=entity_id,
        ))

    # Put likely high-energy targets first, but never silently include them.
    candidates.sort(key=lambda p: (not p.heavy_load, p.device_type, p.entity_id))
    return candidates
