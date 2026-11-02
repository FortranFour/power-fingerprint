"""Keep generated per-source sensor areas aligned with their source devices."""
from __future__ import annotations

from collections.abc import Callable

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import DOMAIN

_SOURCE_SENSOR_SUFFIXES = (
    "_isolated_power",
    "_isolated_energy",
    "_estimated_power",
    "_estimated_energy",
)


@callback
def async_setup_area_inheritance(
    hass: HomeAssistant, entry_id: str
) -> Callable[[], None]:
    """Backfill registered sensors, then follow relevant registry changes.

    Read registry rows rather than live entities so disabled compatibility
    sensors and sensors retained from earlier tracked-device selections are
    included. Only update the area: entity IDs, device links, and statistics
    identifiers remain intact.
    """
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    prefix = f"{entry_id}_"
    source_entity_ids: set[str] = set()
    source_registry_ids: set[str] = set()
    device_ids: set[str] = set()

    def source_key(sensor: er.RegistryEntry) -> str | None:
        if (
            sensor.config_entry_id != entry_id
            or sensor.platform != DOMAIN
            or not sensor.entity_id.startswith("sensor.")
            or not sensor.unique_id.startswith(prefix)
        ):
            return None
        for suffix in _SOURCE_SENSOR_SUFFIXES:
            if sensor.unique_id.endswith(suffix):
                return sensor.unique_id[len(prefix):-len(suffix)] or None
        return None

    @callback
    def sync_areas() -> None:
        source_entity_ids.clear()
        source_registry_ids.clear()
        device_ids.clear()
        areas: dict[str, str | None] = {}
        for sensor in er.async_entries_for_config_entry(registry, entry_id):
            if (key := source_key(sensor)) is None:
                continue
            if key not in areas:
                source_entity_ids.add(key)
                source = registry.async_get(key)
                areas[key] = (
                    er.async_get_effective_area_id(hass, source) if source else None
                )
                if source:
                    source_entity_ids.add(source.entity_id)
                    source_registry_ids.add(source.id)
                    if source.device_id:
                        device_ids.add(source.device_id)
                        device = devices.async_get(source.device_id)
                        if parent_id := getattr(device, "parent_device_id", None):
                            device_ids.add(parent_id)
            if sensor.area_id != areas[key]:
                registry.async_update_entity(sensor.entity_id, area_id=areas[key])

    @callback
    def entity_changed(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        data = event.data
        sensor = registry.async_get(data["entity_id"])
        if sensor and source_key(sensor) is not None:
            # Apply inheritance to newly registered sensors. Ignore our own
            # area writes, which also emit registry events.
            if data["action"] == "create":
                sync_areas()
            return
        if (
            data["entity_id"] not in source_entity_ids
            and data.get("old_entity_id") not in source_entity_ids
            and (sensor is None or sensor.id not in source_registry_ids)
        ):
            return
        if data["action"] != "update" or {
            "area_id", "device_id", "entity_id"
        }.intersection(data.get("changes", {})):
            sync_areas()

    @callback
    def device_changed(event: Event[dr.EventDeviceRegistryUpdatedData]) -> None:
        data = event.data
        if data["device_id"] not in device_ids:
            return
        if data["action"] != "update" or {
            "area_id", "parent_device_id"
        }.intersection(data.get("changes", {})):
            sync_areas()

    unsubs = (
        hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, entity_changed),
        hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, device_changed),
    )
    sync_areas()

    @callback
    def unsubscribe() -> None:
        for unsub in unsubs:
            unsub()

    return unsubscribe
