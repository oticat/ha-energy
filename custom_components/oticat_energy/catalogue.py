"""The entities this Home Assistant allows the site on dom.oti.cat to use.

Home Assistant owns the permission: the entities the service may read and drive are chosen here,
in the integration's flow, and stored in the entry's options. The catalogue that travels to dom is
exactly the projection of that set — never an inventory of the house, and never something the
service can widen by itself. Anything a site names that is not in it is refused before it is read
or switched.

`candidate_entities()` is the proposal, made with the same narrow filters as before: entity
classes that unambiguously say what they are. `build_catalogue()` groups only what was approved,
and classifies it by what it *can* fill rather than by those filters, so a hand-built state of
charge or a Zigbee direction sensor is offered once the user has chosen it.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant

# A big installation can have hundreds of energy sensors, and the list rides on a plan request.
MAX_PER_GROUP = 300
MAX_INVERTERS = 20

# The SOC may be a sensor marked as a battery, or a number helper with the unit.
SOC_HELPER_DOMAINS = ("number", "input_number")
ENERGY_STATES = ("total", "total_increasing")
SWITCH_DOMAINS = ("switch", "input_boolean", "climate")
PROGRAM = re.compile(r"^(?:number|input_number)\.(.+)_program_1_soc$")
INVERTER = re.compile(r"^[a-z0-9_]{1,40}$")

GROUPS = ("soc", "energy", "power", "switches", "flow", "zones", "inverters")

# What the options flow lets the user pick from: every domain a site can point at. The proposal is
# narrower, but an approved entity of these domains is classified by what it can fill.
APPROVABLE_DOMAINS = (
    "sensor", "binary_sensor", "number", "input_number",
    "switch", "input_boolean", "climate", "zone",
)

ENERGY_UNITS = ("Wh", "kWh", "MWh")
POWER_UNITS = ("W", "kW", "MW")


def _domain(entity_id: str) -> str:
    return entity_id.split(".")[0]


def _label(state: Any) -> dict[str, str]:
    return {"entity_id": state.entity_id, "name": state.attributes.get("friendly_name") or state.entity_id}


def candidate_entities(hass: HomeAssistant) -> dict[str, list[Any]]:
    """What this Home Assistant would propose at install, by picker group.

    The filters say what an entity *is*, so that nothing ambiguous is proposed without the user
    choosing it by name: a percentage of a battery for the SOC, energy meters, power meters, the
    switches and climates a device is driven through, direction sensors marked as enums, and zones
    with coordinates.
    """
    states = [s for s in hass.states.async_all() if s.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)]

    soc = [
        s for s in states
        if s.attributes.get("unit_of_measurement") == "%"
        and (
            _domain(s.entity_id) in SOC_HELPER_DOMAINS
            or s.attributes.get("device_class") == "battery"
        )
    ]
    # A phone's battery level is also a percentage, so battery devices come first.
    soc.sort(key=lambda s: 0 if s.attributes.get("device_class") == "battery" else 1)

    energy = [s for s in states
              if _domain(s.entity_id) == "sensor"
              and s.attributes.get("device_class") == "energy"
              and s.attributes.get("state_class") in ENERGY_STATES]

    switches = [s for s in states if _domain(s.entity_id) in SWITCH_DOMAINS]

    # Grid power in W, for the water heaters' controller.
    power = [s for s in states
             if _domain(s.entity_id) == "sensor"
             and s.attributes.get("device_class") == "power"]

    # Where a meter that gives unsigned power says which way the energy is going. A direction is
    # an enum: without that device class there is no telling one from a door, a motion sensor or a
    # presence tracker, and all of those would ride along.
    flow = [s for s in states
            if _domain(s.entity_id) in ("sensor", "binary_sensor")
            and s.attributes.get("device_class") == "enum"]

    # Zones with coordinates, home first: the plan takes its solar forecast and temperature from
    # the chosen one.
    zones = [s for s in states
             if _domain(s.entity_id) == "zone"
             and s.attributes.get("latitude") is not None
             and s.attributes.get("longitude") is not None]
    zones.sort(key=lambda s: 0 if s.entity_id == "zone.home" else 1)

    inverters = sorted({m.group(1) for s in states if (m := PROGRAM.match(s.entity_id)) is not None})

    return {
        "soc": soc[:MAX_PER_GROUP],
        "energy": energy[:MAX_PER_GROUP],
        "power": power[:MAX_PER_GROUP],
        "switches": switches[:MAX_PER_GROUP],
        "flow": flow[:MAX_PER_GROUP],
        "zones": zones[:MAX_PER_GROUP],
        "inverters": [p for p in inverters if INVERTER.match(p)][:MAX_INVERTERS],
    }


def candidate_ids(hass: HomeAssistant) -> tuple[list[str], list[str]]:
    """Every id the install flow proposes, and the inverter prefixes it found."""
    found = candidate_entities(hass)
    ids = sorted({s.entity_id for group in GROUPS if group != "inverters" for s in found[group]})
    return ids, list(found["inverters"])


def group_of(state: Any) -> set[str]:
    """Which pickers an entity could fill, from what it is rather than what it declared.

    Used only for entities the user approved, so it can be generous: a sensor with a percentage
    is a candidate state of charge whether or not its integration set the battery device class,
    and a sensor without a unit is a candidate direction sensor.
    """
    domain = _domain(state.entity_id)
    attrs = state.attributes
    unit = attrs.get("unit_of_measurement")
    device_class = attrs.get("device_class")
    groups: set[str] = set()
    if domain in ("sensor", "number", "input_number") and unit == "%":
        groups.add("soc")
    if domain == "sensor" and (device_class == "energy" or unit in ENERGY_UNITS):
        groups.add("energy")
    if domain == "sensor" and (device_class == "power" or unit in POWER_UNITS):
        groups.add("power")
    if domain in SWITCH_DOMAINS:
        groups.add("switches")
    if domain in ("sensor", "binary_sensor") and (device_class == "enum" or unit is None):
        groups.add("flow")
    if domain == "zone":
        groups.add("zones")
    return groups


def build_catalogue(
    hass: HomeAssistant, approved_entities: Iterable[str], approved_inverters: Iterable[str]
) -> dict[str, Any]:
    """The payload for dom: only approved entities, grouped by what each can fill."""
    approved = set(approved_entities)
    groups: dict[str, list[Any]] = {name: [] for name in GROUPS if name != "inverters"}
    for entity_id in sorted(approved):
        state = hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            continue
        for name in group_of(state):
            if len(groups[name]) < MAX_PER_GROUP:
                groups[name].append(state)
    groups["soc"].sort(key=lambda s: 0 if s.attributes.get("device_class") == "battery" else 1)
    groups["zones"].sort(key=lambda s: 0 if s.entity_id == "zone.home" else 1)
    return {
        "soc": [_label(s) for s in groups["soc"]],
        "energy": [_label(s) for s in groups["energy"]],
        "power": [_label(s) for s in groups["power"]],
        "switches": [_label(s) for s in groups["switches"]],
        "flow": [_label(s) for s in groups["flow"]],
        "zones": [_label(s) for s in groups["zones"]],
        "inverters": sorted(p for p in set(approved_inverters) if INVERTER.match(p))[:MAX_INVERTERS],
    }
