<h3 align="center">🔥 PAX for Home Assistant</h3>

<p align="center">
Control a <b>PAX Momento</b> towel warmer from <a href="https://www.home-assistant.io/">Home Assistant</a>, locally over Bluetooth LE. Works with an <a href="https://esphome.io/components/bluetooth_proxy.html">ESPHome Bluetooth proxy</a>.
</p>

<p align="center">
<a href="https://hacs.xyz/"><img src="https://img.shields.io/badge/HACS-Custom-41BDF5.svg" alt="HACS Custom"></a>
<a href="https://github.com/alexbrasetvik/ha-pax/actions/workflows/validate.yml"><img src="https://github.com/alexbrasetvik/ha-pax/actions/workflows/validate.yml/badge.svg" alt="Validate"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License: MIT"></a>
</p>

> **Unofficial.** This is an independent community integration, not affiliated with or endorsed by PAX or Volution. The protocol was reverse-engineered from the PAX Connect iOS app (v1.7.0), not from a documented API, and checked against one real Momento (hardware 01.00, software 1.02). Use at your own risk.

## Features

- **Heater switch**: on heats for the timer length, off stops it. The switch also shows **on** when the heater was started by its button or by getting power.
- **Start or extend** (`pax.start_or_extend`): heats for at least a given time from now. It extends a run that would end sooner and never shortens one, so it's safe to call again and again from an automation.
- **Timer length** (2–6 hours): how long the button, and the switch, heat for.
- **Heating level**: 40, 60 or 100 %, the levels the app offers.
- **Rail temperature**, which follows the element as it heats up and cools down.
- **Heating until**: when Home Assistant's boost ends, following any extensions. Unknown when no boost is running, including when the button started the heater, since the heater doesn't report when its button timer runs out.
- **Heat source**: off, button, schedule (Home Assistant), or the warm-up it does after getting power.

The integration sets the heater's clock on every connection and then hourly, so it follows DST changes.

## How switching on works

The heater has no "heat now" command. It heats on its button, for a while after getting power, or during its weekly **schedule slots**. So switching it on writes a slot that covers the next *timer length* hours, and switching it off clears the slot.

- The heater switches itself off at the end of the slot, even if Home Assistant or the Bluetooth link goes away mid-way.
- A slot can't cross midnight. A late run uses two slots: one for today until 23:59, one for tomorrow from 00:00.
- The integration clears a finished boost's slots at the next poll, so the boost doesn't repeat the next week.
- Heating starts within about a minute. The switch shows the new state straight away and follows the heater once it reports back.
- **Slots 26 and 27 are Home Assistant's.** It leaves slots 0–25 alone, for a possible future sync with Home Assistant schedules.
- Switching off clears the slots. If the heater keeps heating because someone pressed its button, switching off raises an error saying so; press the button again instead.

## Heating after a shower

`pax.start_or_extend` takes an optional `duration` (up to 12 hours; defaults to the timer length). Each call makes sure the heater runs for at least that long from now. It starts the heater if it's off. If Home Assistant already has it on for longer, that's kept. If the button started it, a boost is added on top.

```yaml
triggers:
  - trigger: numeric_state
    entity_id: sensor.bathroom_humidity
    above: 70
actions:
  - action: pax.start_or_extend
    target:
      entity_id: switch.bathroom_towel_warmer
    data:
      duration:
        hours: 1
```

The switch works the same way: switching it on while it's on heats for the timer length from now, but never ends a longer run early.

## Installation

1. In HACS, open the menu → **Custom repositories**.
2. Add `https://github.com/alexbrasetvik/ha-pax` with category **Integration**.
3. Search for **PAX**, download it, and restart Home Assistant.

## Setup

1. A heater in range of an adapter or proxy shows up under **Settings → Devices & services** as discovered; press **Add**. Otherwise use **Add integration → PAX** and pick it from the Bluetooth devices your adapters and proxies can hear.
2. Put it in pairing mode: press its button about **7 times**, until the light turns **green**, then **blue**. Press **Submit** while it's blue. (The presses also switch it on and off.)

Home Assistant reads the heater's PIN and uses it from then on. The device is named after the description set in the PAX app, if any.

**The integration stays connected.** That uses one connection slot on the adapter or proxy, and the PAX app can't connect while Home Assistant is.

## Known unknowns

- **The exact casing of the advertised name.** Discovery matches `Pax Momento`, which is the name the PAX app looks for, but the app compares it case-insensitively and Home Assistant doesn't. A heater advertising another casing still shows up first in the manual list.
- **The midnight handover** between the two slots should be seamless (both ends inclusive), but it is untested. At worst it pauses for about a minute.
- **Stopping button heating remotely.** Untested. Clearing the slots probably doesn't stop it, and there may be no way to.
- **Unused parts of the protocol**: the `ioControl` and `mode` characteristics, which the app never touches. The link to PAX Calima fans isn't supported.

## Development

```sh
uv sync
mise run check      # every gate below, without modifying files
mise run format     # format in place
mise run lint       # ruff and strict mypy
mise run test       # tests
mise run hassfest   # checks out HA core at the installed version into .cache/
```
