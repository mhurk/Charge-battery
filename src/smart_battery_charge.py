import math
from datetime import datetime, timedelta

# ── Configuration ─────────────────────────────────────────────────────────────
BATTERY_CAPACITY_KWH = 9.3
CHARGE_RATE_KW       = 4.8    # Your alphaESS max charge rate (kW)
NIGHT_START_HOUR     = 23     # Start of cheap night window
NIGHT_END_HOUR       = 6      # End of cheap window (next morning)
MIN_CHARGE_KWH       = 0.5    # Skip if less than this is needed
MAX_PRICE_EUR        = 0.30   # Never charge above this price (€/kWh)
MIN_SOC_FLOOR_PCT    = 30     # Always charge to at least this SOC %

# Seasonal base consumption
MONTH = datetime.now().month
if MONTH in [11, 12, 1, 2]:     # Winter months
    BASE_CONSUMPTION_KW = 0.80  # Higher due to heatpump
else:
    BASE_CONSUMPTION_KW = 0.35  # Summer base load


# ── Sensor entity IDs — adjust these to match YOUR setup ──────────────────────
SOLAR_SENSOR    = "sensor.energy_production_tomorrow"
SOC_SENSOR      = "sensor.alpha_ess_energy_statistics_ald071026xxxxxx_ald071026xxxxxx_instantaneous_battery_soc"
NORDPOOL_SENSOR = "sensor.nordpool_kwh_nl_eur_3_095_021"

SERIAL = "ald071026xxxxxx"
BTN_BASE = f"button.alpha_ess_energy_statistics_{SERIAL}_{SERIAL}"
BTN_15   = f"{BTN_BASE}_15_minute_charge"
BTN_30   = f"{BTN_BASE}_30_minute_charge"
BTN_60   = f"{BTN_BASE}_60_minute_charge"
BTN_RST  = f"{BTN_BASE}_reset_charge_discharge"


# ── Triggers ──────────────────────────────────────────────────────────────────
@service
@time_trigger("cron(30 13 * * *)")
def plan_night_charging_afternoon():
    """First run at 13:30 — prices are fresh but we don't execute yet."""
    log.info("[BatteryOpt] Afternoon check (13:30) — planning only")
    charge_minutes, start_time, avg_price = _plan_charging()
    if charge_minutes > 0:
        log.info(
            f"[BatteryOpt] Tentative plan: {charge_minutes} min starting "
            f"{start_time.strftime('%H:%M')} @ avg €{avg_price:.3f}/kWh"
        )

@service
@time_trigger("cron(0 22 * * *)")
def plan_and_execute_night_charging():
    """Second run at 22:00 — plan with latest forecast, then wait and execute."""
    log.info("[BatteryOpt] Evening check (22:00) — planning and scheduling execution")
    charge_minutes, start_time, avg_price = _plan_charging()

    if charge_minutes <= 0:
        log.info("[BatteryOpt] No charging needed — resetting any active charge")
        service.call("button", "press", entity_id=BTN_RST)
        return

    log.info(
        f"[BatteryOpt] Will charge {charge_minutes} min starting "
        f"{start_time.strftime('%H:%M')} @ avg €{avg_price:.3f}/kWh"
    )

    # Wait until optimal start time, then press buttons
    now = datetime.now()
    wait_seconds = (start_time - now).total_seconds()
    if wait_seconds > 0:
        log.info(f"[BatteryOpt] Sleeping {wait_seconds/60:.0f} min until {start_time.strftime('%H:%M')}")
        task.sleep(wait_seconds)

    _press_charge_buttons(charge_minutes)


# ── Core planning logic ────────────────────────────────────────────────────────
def _plan_charging():
    """Returns (charge_minutes, optimal_start_datetime, avg_price)."""
    solar_kwh   = float(state.get(SOLAR_SENSOR) or 0)
    soc_pct     = float(state.get(SOC_SENSOR) or 0)
    current_kwh = (soc_pct / 100.0) * BATTERY_CAPACITY_KWH
    floor_kwh   = (MIN_SOC_FLOOR_PCT / 100.0) * BATTERY_CAPACITY_KWH

    remaining_capacity = BATTERY_CAPACITY_KWH - current_kwh
    solar_fill         = min(solar_kwh, remaining_capacity)
    projected_kwh      = current_kwh + solar_fill

    charge_for_full  = max(0.0, BATTERY_CAPACITY_KWH - projected_kwh)
    charge_for_floor = max(0.0, floor_kwh - current_kwh)
    charge_needed    = max(charge_for_full, charge_for_floor)

    log.info(
        f"[BatteryOpt] Solar: {solar_kwh:.1f} kWh | SOC: {soc_pct:.0f}% "
        f"({current_kwh:.1f} kWh) | Floor: {floor_kwh:.1f} kWh | "
        f"Needed: {charge_needed:.1f} kWh"
    )

    if charge_needed < MIN_CHARGE_KWH:
        log.info("[BatteryOpt] Sufficient solar forecast and above floor — no charge needed")
        _update_dashboard(
            "✅ No charging needed",
            f"Solar expected tomorrow: {solar_kwh:.1f} kWh | SOC: {soc_pct:.0f}% | Needed: 0 kWh"
        )
        return 0, None, 0.0

    # Round up to nearest 15-min slot, convert to minutes
    slots_needed   = math.ceil((charge_needed / CHARGE_RATE_KW) * 4)
    charge_minutes = slots_needed * 15

    all_slots  = _get_night_price_slots()
    log.info(f"[BatteryOpt] Got {len(all_slots)} night slots from Nordpool")
    affordable = [s for s in all_slots if s["price"] <= MAX_PRICE_EUR]
    log.info(f"[BatteryOpt] Affordable slots: {len(affordable)}")

    if not affordable:
        log.warning("[BatteryOpt] No slots below MAX_PRICE_EUR tonight")
        _update_dashboard(
            "⚠️ No affordable slots found",
            f"Solar expected tomorrow: {solar_kwh:.1f} kWh | SOC: {soc_pct:.0f}% | Needed: {charge_needed:.1f} kWh"
        )
        return 0, None, 0.0

    # First pass — find cheapest window for initial charge estimate
    try:
        start_time, avg_price = _find_cheapest_window(affordable, slots_needed)
        log.info(f"[BatteryOpt] First pass window: {start_time} @ €{avg_price:.3f}")
    except Exception as e:
        log.error(f"[BatteryOpt] Error in _find_cheapest_window: {e}")
        return 0, None, 0.0

    # Second pass — adjust for base consumption drain between now and charge start
    if start_time is not None:
        now                    = datetime.now()
        hours_until_charge     = (start_time - now).total_seconds() / 3600
        extra_drain_kwh        = max(0.0, hours_until_charge * BASE_CONSUMPTION_KW)
        charge_needed_adjusted = charge_needed + extra_drain_kwh
        slots_needed_adjusted  = math.ceil((charge_needed_adjusted / CHARGE_RATE_KW) * 4)
        charge_minutes         = slots_needed_adjusted * 15
        log.info(
            f"[BatteryOpt] Extra drain: {extra_drain_kwh:.2f} kWh over {hours_until_charge:.1f} hrs | "
            f"Adjusted needed: {charge_needed_adjusted:.1f} kWh"
        )
        try:
            start_time, avg_price = _find_cheapest_window(affordable, slots_needed_adjusted)
            log.info(f"[BatteryOpt] Second pass window: {start_time} @ €{avg_price:.3f}")
        except Exception as e:
            log.error(f"[BatteryOpt] Error in second _find_cheapest_window: {e}")
            return 0, None, 0.0
    else:
        slots_needed_adjusted = slots_needed

    if start_time is None:
        # Not enough contiguous affordable slots — use cheapest individual slots
        cheapest   = sorted(affordable, key=lambda x: x["price"])[:slots_needed_adjusted]
        start_time = sorted(cheapest, key=lambda x: x["time"])[0]["time"]
        total_price = 0.0
        for s in cheapest:
            total_price += s["price"]
        avg_price  = total_price / len(cheapest)
        log.warning("[BatteryOpt] No contiguous window found — using cheapest individual slots")

    if start_time:
        try:
            _update_dashboard(
                f"🔋 Charging {charge_minutes} min from {start_time.strftime('%H:%M')}",
                f"Solar: {solar_kwh:.1f} kWh | SOC: {soc_pct:.0f}% | "
                f"Needed: {charge_needed:.1f} kWh | Avg: €{avg_price:.3f}/kWh"
            )
        except Exception as e:
            log.error(f"[BatteryOpt] Error updating dashboard: {e}")

    return charge_minutes, start_time, avg_price


# ── Update the Dashoard ───────────────────────────────────────────────────────
def _update_dashboard(status, detail):
    """Write plan summary to input_text helpers for dashboard display."""
    now = datetime.now().strftime("%d %b %H:%M")
    service.call("input_text", "set_value",
        entity_id="input_text.batteryopt_status",
        value=f"[{now}] {status}"
    )
    service.call("input_text", "set_value",
        entity_id="input_text.batteryopt_detail",
        value=detail
    )


# ── Find cheapest contiguous window ───────────────────────────────────────────
@service
def _find_cheapest_window(slots, slots_needed):
    """Sliding window over sorted slots — find start of cheapest contiguous block."""
    ordered = sorted(slots, key=lambda x: x["time"])

    best_start = None
    best_avg   = float("inf")

    for i in range(len(ordered) - slots_needed + 1):
        window = ordered[i : i + slots_needed]

        # Check all slots are strictly contiguous (no generator expressions)
        contiguous = True
        for j in range(len(window) - 1):
            if window[j + 1]["time"] != window[j]["time"] + timedelta(minutes=15):
                contiguous = False
                break

        if not contiguous:
            continue

        total = 0.0
        for s in window:
            total += s["price"]
        avg = total / slots_needed

        if avg < best_avg:
            best_avg   = avg
            best_start = window[0]["time"]

    return best_start, best_avg


# ── Price slot extraction ──────────────────────────────────────────────────────
def _get_night_price_slots():
    try:
        attrs = state.getattr(NORDPOOL_SENSOR)
        if not attrs:
            log.warning(f"[BatteryOpt] Nordpool sensor returned no attributes")
            return []
        log.info(f"[BatteryOpt] Nordpool keys: {list(attrs.keys())}")

        now         = datetime.now()
        night_start = now.replace(hour=NIGHT_START_HOUR, minute=0, second=0, microsecond=0)
        night_end   = (now + timedelta(days=1)).replace(
                          hour=NIGHT_END_HOUR, minute=0, second=0, microsecond=0)

        slots = []
        for day_key in ["raw_today", "raw_tomorrow"]:
            for entry in attrs.get(day_key, []):
                slot_start = entry["start"]
                if hasattr(slot_start, "tzinfo") and slot_start.tzinfo:
                    slot_start = slot_start.replace(tzinfo=None)
                if night_start <= slot_start < night_end:
                    slots.append({"time": slot_start, "price": entry["value"]})
        return slots
    except Exception as e:
        log.error(f"[BatteryOpt] Error reading Nordpool: {e}")
        return []

# ── Button press logic ─────────────────────────────────────────────────────────
def _press_charge_buttons(total_minutes):
    """
    Decompose total_minutes into 60/30/15 button presses.
    Waits for each press to complete before pressing the next.
    Assumes buttons do NOT stack — each press replaces the active timer.
    """
    remaining = total_minutes
    sequence  = []

    while remaining >= 60:
        sequence.append((BTN_60, 60))
        remaining -= 60
    if remaining >= 30:
        sequence.append((BTN_30, 30))
        remaining -= 30
    if remaining >= 15:
        sequence.append((BTN_15, 15))
        remaining -= 15

    log.info(f"[BatteryOpt] Pressing {len(sequence)} button(s): {[s[1] for s in sequence]} min")

    for i, (btn, duration_min) in enumerate(sequence):
        service.call("button", "press", entity_id=btn)
        log.info(f"[BatteryOpt] Pressed {duration_min}-min button")

        # Sleep between presses — not after the last one
        if i < len(sequence) - 1:
            task.sleep(duration_min * 60)