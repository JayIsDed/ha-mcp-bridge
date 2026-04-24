"""Unit tests for the shelf entity registry.

Validates structural invariants that consumers (tools, flags, orchestrator) depend
on — no live HA required.
"""

from __future__ import annotations

import pytest

from ha_mcp_bridge.shelf_registry import (
    CATEGORIES,
    CONTAINERS,
    SHELF_ENTITIES,
    active_entity_ids,
    active_keys,
    all_entity_ids,
    by_category,
    by_container,
    by_role,
    find_key,
    spec,
)


def test_every_entry_has_required_fields() -> None:
    required = {"entity_id", "category", "container", "role"}
    for key, entry in SHELF_ENTITIES.items():
        missing = required - set(entry)
        assert not missing, f"registry[{key}] missing: {missing}"


def test_active_field_defaults_or_bool() -> None:
    for key, entry in SHELF_ENTITIES.items():
        if "active" not in entry:
            continue
        assert isinstance(entry["active"], bool), f"registry[{key}].active must be bool"


def test_categories_are_valid() -> None:
    for key, entry in SHELF_ENTITIES.items():
        assert entry["category"] in CATEGORIES, (
            f"registry[{key}].category={entry['category']} not in CATEGORIES"
        )


def test_containers_are_valid() -> None:
    for key, entry in SHELF_ENTITIES.items():
        assert entry["container"] in CONTAINERS, (
            f"registry[{key}].container={entry['container']} not in CONTAINERS"
        )


def test_entity_ids_are_unique() -> None:
    ids = [v["entity_id"] for v in SHELF_ENTITIES.values()]
    assert len(ids) == len(set(ids)), "duplicate entity_id in registry"


def test_range_tuples_are_ordered() -> None:
    for key, entry in SHELF_ENTITIES.items():
        rng = entry.get("range")
        if rng is None:
            continue
        lo, hi = rng
        assert lo < hi, f"registry[{key}].range=({lo},{hi}) must have lo<hi"


def test_target_and_band_types() -> None:
    for key, entry in SHELF_ENTITIES.items():
        for field in ("target", "band", "flag_band"):
            v = entry.get(field)
            if v is None:
                continue
            assert isinstance(v, (int, float)), (
                f"registry[{key}].{field} must be numeric, got {type(v).__name__}"
            )


def test_active_keys_returns_only_active() -> None:
    ak = active_keys()
    for k in ak:
        assert SHELF_ENTITIES[k].get("active") is True


def test_active_entity_ids_matches_active_keys() -> None:
    expected = [SHELF_ENTITIES[k]["entity_id"] for k in active_keys()]
    assert sorted(active_entity_ids()) == sorted(expected)


def test_all_entity_ids_includes_inactive() -> None:
    ae = set(active_entity_ids())
    all_ids = set(all_entity_ids())
    # inactive entries exist → all_ids strictly larger than active subset
    inactive = all_ids - ae
    assert len(inactive) > 0, "expected at least one inactive registry entry"


def test_by_category_filters_correctly() -> None:
    thermal = by_category("thermal")
    for key, entry in thermal.items():
        assert entry["category"] == "thermal"
        assert entry.get("active") is True


def test_by_category_active_only_flag() -> None:
    with_inactive = by_category("chemistry", active_only=False)
    without = by_category("chemistry", active_only=True)
    # ph_tank + soil probes are inactive by default → should show up when
    # active_only=False
    assert len(with_inactive) > len(without)


def test_by_container_returns_main_tank_bundle() -> None:
    mt = by_container("main_tank_10g")
    # tank_center + tank_substrate + tds_tank + heater_power + pump outlet must all be present
    keys = set(mt.keys())
    expected_subset = {
        "tank_center",
        "tank_substrate",
        "tds_tank",
        "heater_power",
        "climate_main_tank",
    }
    assert expected_subset <= keys


def test_by_role_water_temp() -> None:
    wt = by_role("water_temp")
    # tank_center, tank_substrate, bucket_rig_water → 3 active water_temp probes
    assert len(wt) >= 2
    for _, entry in wt.items():
        assert entry["role"] == "water_temp"


def test_find_key_roundtrip() -> None:
    for key, entry in SHELF_ENTITIES.items():
        found = find_key(entry["entity_id"])
        assert found == key, f"find_key({entry['entity_id']}) returned {found}, expected {key}"


def test_find_key_unknown_returns_none() -> None:
    assert find_key("sensor.does_not_exist") is None


def test_spec_returns_typed_view() -> None:
    s = spec("tank_center")
    assert s.key == "tank_center"
    assert s.entity_id == "sensor.plant_shelf_temperatures_tank_center"
    assert s.category == "thermal"
    assert s.target == 77.0
    assert s.active is True


def test_spec_raises_on_unknown_key() -> None:
    with pytest.raises(KeyError):
        spec("nonexistent_key")


def test_gated_chemistry_entries_reference_switch() -> None:
    """Gated TDS probes must name the switch that enables them so dashboard
    UIs can display the gate state next to the sensor.
    """
    for key, entry in SHELF_ENTITIES.items():
        if entry.get("role") == "tds" and "gated_by" in entry:
            assert entry["gated_by"].startswith("switch."), (
                f"{key}.gated_by must be a switch.* entity"
            )


def test_climate_entries_have_powered_by() -> None:
    """Climate helpers must name the switch they actuate so phantom-heat
    detection can correlate state.
    """
    for key, entry in SHELF_ENTITIES.items():
        if entry.get("category") == "climate":
            assert "powered_by" in entry, f"climate[{key}] must name powered_by"
