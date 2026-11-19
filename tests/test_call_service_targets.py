"""Target tests for the actuation allowlist.

The allowlist only means something if the entities it checks are the entities Home
Assistant acts on. Each test drives the tool against a fake HA client (no network) and
asserts either a refusal with nothing sent, or exactly what HA was sent.
"""

from __future__ import annotations

import importlib

import pytest

SERVICES = (
    "switch.turn_on,switch.turn_off,light.*,fan.*,scene.turn_on,"
    "button.press,homeassistant.turn_off"
)
ENTITIES = (
    "switch.allowed_*,light.allowed_*,fan.allowed_*,scene.allowed_*,"
    "button.allowed_*,group.allowed_*"
)
INDIRECT_KEYS = ["area_id", "device_id", "floor_id", "label_id"]


def state(entity_id: str, /, **attributes) -> dict:
    return {"entity_id": entity_id, "state": "on", "attributes": attributes}


class FakeHA:
    """Stands in for HAClient. Every entity exists as a plain one unless overridden."""

    def __init__(self, states=None, missing=(), fail_reads=False) -> None:
        self.states = dict(states or {})
        self.missing = set(missing)
        self.fail_reads = fail_reads
        self.calls: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def get_state(self, entity_id):
        if self.fail_reads:
            from ha_mcp_bridge.ha_client import HAError

            raise HAError("Cannot reach HA")
        if entity_id in self.missing:
            return None
        return self.states.get(entity_id, state(entity_id))

    async def call_service(self, domain, service, data):
        self.calls.append((domain, service, data))
        return []


@pytest.fixture
def bridge(monkeypatch):
    """Build a server module with the given allowlists, wired to a FakeHA."""
    import ha_mcp_bridge.server as mod

    def make(services=SERVICES, entities=ENTITIES, ha=None):
        monkeypatch.setenv("HA_MCP_ALLOWED_SERVICES", services)
        monkeypatch.setenv("HA_MCP_ALLOWED_ENTITY_PATTERNS", entities)
        monkeypatch.setenv("HA_URL", "http://127.0.0.1:9")
        monkeypatch.setenv("HA_TOKEN", "fake-for-import")
        importlib.reload(mod)
        fake = ha or FakeHA()
        monkeypatch.setattr(mod, "_client", lambda: fake)
        return mod, fake

    return make


# A comma-joined entity_id string: HA splits it, so every piece is checked.


async def test_comma_joined_string_checks_every_id(bridge) -> None:
    mod, ha = bridge()
    out = await mod.ha_call_service(
        "switch", "turn_off", {"entity_id": "switch.allowed_lamp,switch.forbidden_relay"}
    )
    assert out["error"] == "entity_not_allowed"
    assert out["entity_id"] == "switch.forbidden_relay"
    assert ha.calls == []


async def test_comma_joined_list_item_checks_every_id(bridge) -> None:
    mod, ha = bridge()
    out = await mod.ha_call_service(
        "switch",
        "turn_off",
        {"entity_id": ["switch.allowed_fan", "switch.allowed_lamp, switch.forbidden_relay"]},
    )
    assert out["error"] == "entity_not_allowed"
    assert out["entity_id"] == "switch.forbidden_relay"
    assert ha.calls == []


async def test_comma_joined_allowed_ids_are_sent_as_checked(bridge) -> None:
    mod, ha = bridge()
    out = await mod.ha_call_service(
        "switch", "turn_on", {"entity_id": " switch.allowed_lamp , switch.allowed_fan"}
    )
    assert out["ok"] is True
    assert out["entity_ids"] == ["switch.allowed_lamp", "switch.allowed_fan"]
    assert ha.calls == [
        ("switch", "turn_on", {"entity_id": ["switch.allowed_lamp", "switch.allowed_fan"]})
    ]


@pytest.mark.parametrize(
    "special,entities",
    [
        # A substring pattern can match the literal "all", which HA reads as every
        # entity of the domain.
        ("all", "*all*"),
        ("ALL", "*"),
        ("none", "*"),
    ],
)
async def test_all_and_none_are_refused(bridge, special, entities) -> None:
    mod, ha = bridge(entities=entities)
    out = await mod.ha_call_service("switch", "turn_off", {"entity_id": special})
    assert out["error"] == "invalid_entity_id"
    assert ha.calls == []


@pytest.mark.parametrize(
    "raw",
    [
        "switch.Allowed_lamp",
        "{{ 'switch.allowed_lamp' }}",
        "switch.allowed_*",
        "switch.allowed_lamp,",
        5,
        {"switch.allowed_lamp": True},
        ["switch.allowed_lamp", 5],
    ],
)
async def test_malformed_entity_ids_are_refused(bridge, raw) -> None:
    mod, ha = bridge(entities="*")
    out = await mod.ha_call_service("switch", "turn_on", {"entity_id": raw})
    assert out["error"] == "invalid_entity_id"
    assert ha.calls == []


# Area, device, floor and label selectors: HA unions them with entity_id.


@pytest.mark.parametrize("key", INDIRECT_KEYS)
async def test_selector_beside_allowed_entity_is_refused(bridge, key) -> None:
    mod, ha = bridge()
    out = await mod.ha_call_service(
        "switch", "turn_off", {"entity_id": "switch.allowed_lamp", key: "area_a"}
    )
    assert out["error"] == "target_not_allowed"
    assert out["keys"] == [key]
    assert ha.calls == []


@pytest.mark.parametrize("key", INDIRECT_KEYS)
async def test_selector_with_empty_entity_list_is_refused(bridge, key) -> None:
    mod, ha = bridge()
    out = await mod.ha_call_service("switch", "turn_off", {"entity_id": [], key: ["area_a"]})
    assert out["error"] == "target_not_allowed"
    assert ha.calls == []


async def test_target_wrapper_is_refused(bridge) -> None:
    mod, ha = bridge()
    out = await mod.ha_call_service(
        "switch",
        "turn_off",
        {"entity_id": "switch.allowed_lamp", "target": {"entity_id": "switch.forbidden_relay"}},
    )
    assert out["error"] == "target_not_allowed"
    assert out["keys"] == ["target"]
    assert ha.calls == []


@pytest.mark.parametrize("raw", [[], "", " , "])
async def test_empty_entity_id_is_refused(bridge, raw) -> None:
    mod, ha = bridge()
    out = await mod.ha_call_service("switch", "turn_off", {"entity_id": raw})
    assert out["error"] == "entity_required"
    assert ha.calls == []


async def test_data_must_be_an_object(bridge) -> None:
    mod, ha = bridge()
    out = await mod.ha_call_service("switch", "turn_off", [("entity_id", "switch.allowed_lamp")])
    assert out["error"] == "invalid_data"
    assert ha.calls == []


# Domain and service go into the request path, so they must be plain names.


@pytest.mark.parametrize(
    "domain,service",
    [
        ("fan", "x/../../switch/turn_off"),
        ("fan", "turn_on?x=1"),
        ("light", "turn_on/"),
        ("Switch", "turn_off"),
    ],
)
async def test_service_names_must_be_plain(bridge, domain, service) -> None:
    mod, ha = bridge()
    out = await mod.ha_call_service(domain, service, {"entity_id": "switch.allowed_lamp"})
    assert out["error"] == "invalid_service"
    assert ha.calls == []


# Groups and scenes act on their members, so the members are held to the allowlist.


async def test_group_with_forbidden_member_is_refused(bridge) -> None:
    ha = FakeHA(
        {
            "group.allowed_set": state(
                "group.allowed_set", entity_id=["switch.allowed_lamp", "switch.forbidden_relay"]
            )
        }
    )
    mod, ha = bridge(ha=ha)
    out = await mod.ha_call_service("homeassistant", "turn_off", {"entity_id": "group.allowed_set"})
    assert out["error"] == "entity_not_allowed"
    assert out["entity_id"] == "switch.forbidden_relay"
    assert out["via"] == "group.allowed_set"
    assert ha.calls == []


async def test_group_entity_with_forbidden_member_is_refused(bridge) -> None:
    # A group entity (e.g. a switch group) lists its members under group_entities.
    ha = FakeHA(
        {
            "switch.allowed_bank": state(
                "switch.allowed_bank",
                group_entities=["switch.allowed_lamp", "switch.forbidden_relay"],
            )
        }
    )
    mod, ha = bridge(ha=ha)
    out = await mod.ha_call_service("switch", "turn_off", {"entity_id": "switch.allowed_bank"})
    assert out["error"] == "entity_not_allowed"
    assert out["entity_id"] == "switch.forbidden_relay"
    assert ha.calls == []


async def test_nested_group_member_is_refused(bridge) -> None:
    ha = FakeHA(
        {
            "group.allowed_outer": state("group.allowed_outer", entity_id=["group.allowed_inner"]),
            "group.allowed_inner": state("group.allowed_inner", entity_id=["switch.forbidden_relay"]),
        }
    )
    mod, ha = bridge(ha=ha)
    out = await mod.ha_call_service(
        "homeassistant", "turn_off", {"entity_id": "group.allowed_outer"}
    )
    assert out["error"] == "entity_not_allowed"
    assert out["entity_id"] == "switch.forbidden_relay"
    assert out["via"] == "group.allowed_inner"
    assert ha.calls == []


async def test_scene_setting_forbidden_entity_is_refused(bridge) -> None:
    ha = FakeHA(
        {
            "scene.allowed_evening": state(
                "scene.allowed_evening", entity_id=["light.allowed_desk", "switch.forbidden_relay"]
            )
        }
    )
    mod, ha = bridge(ha=ha)
    out = await mod.ha_call_service("scene", "turn_on", {"entity_id": "scene.allowed_evening"})
    assert out["error"] == "entity_not_allowed"
    assert out["entity_id"] == "switch.forbidden_relay"
    assert ha.calls == []


async def test_group_of_allowed_members_goes_through(bridge) -> None:
    # Members that loop back (a group listing itself, two groups listing each other)
    # are walked once.
    ha = FakeHA(
        {
            "group.allowed_a": state(
                "group.allowed_a", entity_id=["group.allowed_a", "group.allowed_b"]
            ),
            "group.allowed_b": state(
                "group.allowed_b", entity_id=["group.allowed_a", "switch.allowed_lamp"]
            ),
        }
    )
    mod, ha = bridge(ha=ha)
    out = await mod.ha_call_service("homeassistant", "turn_off", {"entity_id": "group.allowed_a"})
    assert out["ok"] is True
    assert ha.calls == [("homeassistant", "turn_off", {"entity_id": "group.allowed_a"})]


async def test_unknown_entity_is_refused(bridge) -> None:
    mod, ha = bridge(ha=FakeHA(missing={"switch.allowed_ghost"}))
    out = await mod.ha_call_service("switch", "turn_on", {"entity_id": "switch.allowed_ghost"})
    assert out["error"] == "entity_not_found"
    assert ha.calls == []


async def test_state_read_failure_refuses(bridge) -> None:
    mod, ha = bridge(ha=FakeHA(fail_reads=True))
    out = await mod.ha_call_service("switch", "turn_on", {"entity_id": "switch.allowed_lamp"})
    assert out["error"] == "entity_check_failed"
    assert ha.calls == []
