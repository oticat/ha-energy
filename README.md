# Oticat Energy for Home Assistant

Charges a home battery from the grid in the cheapest hours of the Spanish PVPC tariff, and only
when the solar panels won't fill it first.

Every 15 minutes the integration asks the Oticat Energy service for a plan covering every hour
with a known price, which runs to the end of tomorrow once REE publishes tomorrow's prices around
20:15. Each hour gets one of three actions:

| Action | What the battery does |
|--------|-----------------------|
| Self-consumption | Solar surplus charges it, the house draws from it down to the minimum SOC |
| Hold | It stops discharging at a floor, so the house buys cheap grid energy now and keeps the stored energy for a more expensive hour |
| Charge from grid | The grid tops it up to a target, on top of what the panels give |

The plan uses the hourly PVPC price, a solar forecast for your panels and your home's own
consumption pattern. For a Deye hybrid inverter, the integration writes the plan straight into its
time-of-use programs.

## Requirements

- An account on [dom.oti.cat](https://dom.oti.cat) with Inverter automation enabled.
- A site created there for each inverter. The site holds the battery, solar panel and grid
  settings, and gives you a token.
- Home Assistant 2025.4 or newer.
- For inverter control: a Deye hybrid inverter connected through the
  [ha-solarman](https://github.com/davidrapan/ha-solarman) integration. Without it, the plan is
  only exposed as sensors.

## Installation

### HACS

1. In HACS, open the menu and choose **Custom repositories**.
2. Add `https://github.com/oticat/ha-energy` with type **Integration**.
3. Install **Oticat Energy** and restart Home Assistant.

### Manual

Copy `custom_components/oticat_energy` into your Home Assistant `custom_components` folder and
restart.

## Setup

1. **Settings > Devices & services > Add integration > Oticat Energy.** Keep the default service
   URL.
2. On the Oticat Energy entry, choose **Add inverter** and fill in:
   - **Site token**: shown when you create the site on dom.oti.cat.
   - **Battery SOC entity**: the state of charge in percent.
   - **House consumption energy sensor**: total kWh used by the house, not counting battery
     charging. The plan learns your hourly pattern from its last 14 days. Until there is a full
     week of history it uses REE's standard household profile.
   - **Grid energy sensor** (optional): total kWh drawn from the grid, used by the day chart on
     dom.oti.cat to show the real grid cost. A signed meter that goes negative while exporting
     is fine. Without it the chart falls back to an estimate.
   - **Grid export energy sensor** (optional): total kWh fed to the grid, if the meter keeps a
     separate export register. Its hours are priced at the surplus price.
   - **Solar production energy sensor** (optional): total kWh produced by the panels. The day
     chart shows the measured production for the hours already past and the model ahead.
   - **Location**: the zone where the panels are, for the solar forecast and temperature. It
     defaults to Home. For another place, add a zone under Settings > Areas, labels & zones >
     Zones first.
   - **Inverter control**: `Deye` to write the programs, or `None` for sensors only.
   - **Inverter entity prefix**: for `number.deye_program_1_soc` it is `deye`.
   - **Program power**: the battery power limit written to every program.

Add one inverter per site. Each becomes its own device.

## Entities

For each inverter:

| Entity | Meaning |
|--------|---------|
| Battery action | `charge`, `hold` or `self_consume` for the current hour. Attributes hold the full plan, the inverter programs and the plan status |
| Target SOC | The SOC the battery charges to or holds at this hour |
| Grid charging | On while the plan charges from the grid |
| Expected savings | Euros saved until the end of the plan compared with plain self-consumption, counting the energy left in the battery |
| Planned grid cost | Euros the plan spends on grid energy until its end |

The `status` attribute of Battery action is `ok`, `stale` (the last request failed and an earlier
plan still applies) or `fallback` (no plan covers the current hour, so the battery runs plain
self-consumption).

## Deye inverter control

The integration writes the 6 time-of-use programs of the inverter: start time, SOC, power and
whether each one charges from the grid. It also sets time of use to every day. It only writes
values that differ from what the inverter already has, so a run with nothing to change sends no
Modbus writes. A program changed by hand is put back within 15 minutes.

Entities are found by name: `<prefix>_program_<n>_time`, `_soc`, `_power`, `_charging` and
`<prefix>_time_of_use`. Single-phase (`deye_hybrid`) and three-phase (`deye_p3`) ha-solarman
profiles use the same names.

## What is sent to the service

Each request sends the chosen zone's coordinates and name, your time zone, the current battery SOC and the
hourly consumption of the last 14 days. Nothing else leaves your home, and the service never
connects to your inverter.

## License

MIT
