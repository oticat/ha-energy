"""Constants for Oticat Energy."""

DOMAIN = "oticat_energy"
DEFAULT_URL = "https://dom.oti.cat"
PLAN_PATH = "/api/energy/plan"

CONF_TOKEN = "token"
CONF_URL = "url"
CONF_SOC_ENTITY = "soc_entity"
CONF_LOAD_ENTITY = "load_entity"
CONF_GRID_ENTITY = "grid_entity"
CONF_GRID_EXPORT_ENTITY = "grid_export_entity"
CONF_SOLAR_ENTITY = "solar_entity"
CONF_INVERTER = "inverter"
CONF_INVERTER_PREFIX = "inverter_prefix"
CONF_PROGRAM_POWER = "program_power"
CONF_SITE_ID = "site_id"
CONF_ZONE = "zone"

DEFAULT_ZONE = "zone.home"

SUBENTRY_INVERTER = "inverter"

INVERTER_NONE = "none"
INVERTER_DEYE = "deye"

HISTORY_DAYS = 14

# --- Water heaters -------------------------------------------------------------------------
# The integration can drive water heaters (or any dump load) straight from the plan: solar
# surplus first, and the cheap-grid hours the service marks with `preheat`. This replaces the
# solar_surplus_heater blueprint, so the whole site is configured in one place.

MAX_HEATERS = 4


def heater_key(index: int, part: str) -> str:
    """Subentry data keys per heater slot: heater_1_entity, heater_1_power, heater_1_hvac_mode."""
    return f"heater_{index}_{part}"


CONF_HEATER_GRID_ENTITY = "heater_grid_entity"      # grid power in W, signed or with a flow sensor
CONF_HEATER_FLOW_ENTITY = "heater_flow_entity"      # optional direction sensor
CONF_HEATER_CONSUMING_STATE = "heater_consuming_state"
CONF_HEATER_SURPLUS_W = "heater_surplus_w"          # surplus threshold: keep the grid near this
CONF_HEATER_MAX_IMPORT_W = "heater_max_import_w"    # ceiling on grid import for cheap-grid heating
CONF_HEATER_MARGIN = "heater_margin"                # anti-flapping hysteresis, W
CONF_HEATER_MIN_ELEVATION = "heater_min_elevation"
CONF_HEATER_MIN_AZIMUTH = "heater_min_azimuth"
CONF_HEATER_MAX_AZIMUTH = "heater_max_azimuth"

DEFAULT_HEATER_CONSUMING_STATE = "consuming"
DEFAULT_HEATER_SURPLUS_W = 100
DEFAULT_HEATER_MAX_IMPORT_W = 3000
DEFAULT_HEATER_MARGIN = 50
DEFAULT_HEATER_MIN_ELEVATION = 10
DEFAULT_HEATER_MIN_AZIMUTH = 90
DEFAULT_HEATER_MAX_AZIMUTH = 270
DEFAULT_HEATER_HVAC_MODE = "heat"
