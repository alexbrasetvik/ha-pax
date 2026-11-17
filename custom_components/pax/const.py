"""Constants for the PAX integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "pax"
MANUFACTURER = "PAX"
MODEL = "Momento"

CONF_PIN = "pin"

SCAN_INTERVAL = timedelta(seconds=15)
# The device clock drives the schedule slots. Set it on every connect and then
# hourly, which also picks up DST changes.
CLOCK_SYNC_INTERVAL = timedelta(hours=1)

# Slots the heater switch owns. The device has 28 (0..27); 0..25 are left for a
# future sync with Home Assistant schedules. Two, because a slot can't cross
# midnight: a late boost uses one for today and one for tomorrow.
BOOST_SLOTS: tuple[int, ...] = (27, 26)

# The heater has taken up to about a minute to act on a schedule slot. Until it
# reports the new state, the switch shows what was asked for.
PENDING_STATE_TIMEOUT = timedelta(minutes=2)

SERVICE_START_OR_EXTEND = "start_or_extend"
ATTR_DURATION = "duration"
# Two slots cover up to a day; keep boosts well within that.
MAX_BOOST = timedelta(hours=12)
