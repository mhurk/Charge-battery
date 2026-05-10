(this is very draft)

# Charge home battery with Home Assistant
The goal is to be able to charge the home battery with cheap energy during the night. But only if the expected solar production of the following day is not sufficient. Or if the battery is almost empty and some energy is needed to cover the (expensive) peak prices in the morning.

## Components
The following components are needed to setup this service. I mentioned the version numbers I have used during the development of this code.

- Home Assistant 2026.5.1
- [Alpha ESS HA integration](https://github.com/CharlesGillanders/homeassistant-alphaESS), version 0.8.4
- [Forecast.Solar](https://forecast.solar/), with personal account. This geves more frequent update then the free version.
- [Nordpool](https://github.com/custom-components/nordpool) for price info, based on Apex but I found this to be more stable than Entso-e which had crashed my HA install a couple of times)
- [Pyscript](https://github.com/custom-components/pyscript) version 2.0.1, needed to run the script
- Terminal & SSH for troubleshooting and testing

## Configuration

Add this to <code>configuration.yaml</code> to enable pyscript to import the required libraries
```
pyscript:
  allow_all_imports: true
  hass_is_global: true
```
And 
```
logger:
  default: warning
  logs:
    custom_components.pyscript: info
```
to enable the logger to log all activities. This is needed for debugging and to check what happened.


Make changes to <code>smart_battery_charg.py</code> such that it matches your system and wishes. If you have renamed your Alpha ESS you need to change that here as well. I just use the default name with the serial numbers.

```
BATTERY_CAPACITY_KWH = 9.3
CHARGE_RATE_KW       = 2.4    # Your alphaESS max charge rate (kW)
NIGHT_START_HOUR     = 23     # Start of cheap night window
NIGHT_END_HOUR       = 6      # End of cheap window (next morning)
MIN_CHARGE_KWH       = 0.5    # Skip if less than this is needed
MAX_PRICE_EUR        = 0.25   # Never charge above this price (€/kWh)
MIN_SOC_FLOOR_PCT    = 20     # Always charge to at least this SOC %


SOLAR_SENSOR    = "sensor.energy_production_tomorrow"
SOC_SENSOR      = "sensor.alpha_ess_energy_statistics_ald071026xxxxxx_ald071026xxxxxx_instantaneous_battery_soc"
NORDPOOL_SENSOR = "sensor.nordpool_kwh_nl_eur_3_095_021"

SERIAL = "ald071026xxxxxx"
BTN_BASE = f"button.alpha_ess_energy_statistics_{SERIAL}_{SERIAL}"
BTN_15   = f"{BTN_BASE}_15_minute_charge"
BTN_30   = f"{BTN_BASE}_30_minute_charge"
BTN_60   = f"{BTN_BASE}_60_minute_charge"
BTN_RST  = f"{BTN_BASE}_reset_charge_discharge"
```




