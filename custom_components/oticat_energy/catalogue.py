"""The entities this Home Assistant can offer, for the settings pickers on dom.oti.cat.

dom cannot see into a private Home Assistant, so the list travels the other way: the settings
page asks for it, the plan response carries `want_catalogue`, and this sends the catalogue with
the next request. The filters mirror GET /api/energy/connect's candidate list, so one page
component renders both a site on one of our instances and a site on the user's own HA.
"""

from __future__ import annotations

import re
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant

# A big installation can have hundreds of energy sensors, and the list rides on a plan request.
MAX_PER_GROUP = 300
MAX_INVERTERS = 20

SOC_DOMAINS = ("sensor", "number", "input_number")
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

    # A phone's battery level is also a percentage, so battery devices come first.
    soc = [s for s in states
           if _domain(s.entity_id) in SOC_DOMAINS
           and s.attributes.get("unit_of_measurement") == "%"]
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

    # Entities whose state is not a reading: where a meter that gives unsigned power says which
    # way the energy is going.
    flow = [s for s in states
            if _domain(s.entity_id) in ("sensor", "binary_sensor")
            and s.attributes.get("unit_of_measurement") is None]

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
