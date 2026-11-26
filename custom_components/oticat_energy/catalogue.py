"""The entities this Home Assistant can offer, for the settings pickers on dom.oti.cat.

dom cannot see into a private Home Assistant, so the list travels the other way: the settings
page asks for it, the plan response carries `want_catalogue`, and this sends the catalogue with
the next request. The filters mirror GET /api/energy/connect's candidate list, so one page
component renders both a site on one of our instances and a site on the user's own HA.

This is an inventory of the house, so it only leaves when the user has turned on "Share the entity
list" in the integration's options (off by default), and each group holds only what its picker can
actually use: a percentage of a battery for the SOC, energy meters, power meters, the switches and
climates a device may be driven through, direction sensors marked as enums and zones with
coordinates. Nothing looks for "any sensor", which would take doors, motion and presence with it.
"""

from __future__ import annotations

import re
from typing import Any

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


def _domain(entity_id: str) -> str:
    return entity_id.split(".")[0]


def _label(state: Any) -> dict[str, str]:
    return {"entity_id": state.entity_id, "name": state.attributes.get("friendly_name") or state.entity_id}


def build_catalogue(hass: HomeAssistant) -> dict[str, Any]:
    """Every entity a site can be pointed at, grouped the way the settings page offers them."""
    states = [s for s in hass.states.async_all() if s.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)]

    # A percentage on its own is not a state of charge: humidity and power factor are percentages
    # too. A sensor has to be marked as a battery; a number helper is a number helper, and the
    # sites that store their SOC in one mark it with the unit.
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

    inverters = {m.group(1) for s in states if (m := PROGRAM.match(s.entity_id)) is not None}

    return {
        "soc": [_label(s) for s in soc[:MAX_PER_GROUP]],
        "energy": [_label(s) for s in energy[:MAX_PER_GROUP]],
        "power": [_label(s) for s in power[:MAX_PER_GROUP]],
        "switches": [_label(s) for s in switches[:MAX_PER_GROUP]],
        "flow": [_label(s) for s in flow[:MAX_PER_GROUP]],
        "zones": [_label(s) for s in zones[:MAX_PER_GROUP]],
        "inverters": sorted(p for p in inverters if INVERTER.match(p))[:MAX_INVERTERS],
    }
