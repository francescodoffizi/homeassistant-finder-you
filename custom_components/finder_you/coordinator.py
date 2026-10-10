"""Update coordinator: keeps a long-lived h2 connection alive and polls the
plant for shutter state at a fixed interval. Commands flow through the same
connection so they share the gateway claim established by the boot
sequence."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    FinderApiError,
    FinderHomeClient,
    GatewayOfflineError,
    Light,
    OAuthError,
    Scenario,
    Shutter,
    Switch,
    extract_channel_states,
    extract_shutter_motion,
    extract_shutter_positions,
    extract_shutter_states,
    fetch_token,
    parse_lights_and_switches,
    parse_plant,
    parse_scenarios,
    refresh_token,
)

from .const import DEFAULT_SCAN_INTERVAL_SECONDS, DOMAIN

# Minimum gap between consecutive command sends. 250 ms was enough to stop
# the WiFi-side burst, but during 6-shutter scenes the gateway's BLE-mesh
# fan-out to the actual shutters still dropped the farthest hops (Cucina,
# Salotto). 2 s gives BLE-mesh time to land each command before the next
# one fires. Single user-initiated taps still feel instant because there's
# nothing queued ahead of them.
COMMAND_SEND_GAP = 2.0
# How long after a command to wait for the gateway's plant-state cache to
# reflect the change. Empirically the cache lags 30-60 s for solo taps but
# can stretch past 90 s during scene bursts. 180 s outwaits the worst case
# we've observed; the alternative (declaring failure when the shutter
# actually moved) is worse than making the user wait.
VERIFY_TIMEOUT = 180.0
# How many times to retry a command if verify fails. Each retry tears
# down the cloud client and re-runs the OpenNotificationChannel handshake,
# which resolves any cloud-side subscription staleness. Three attempts
# total gives the gateway multiple shots at landing the BLE-mesh hop —
# the dominant remaining failure for the shutter that's farthest from
# the puck. Total worst-case time per shutter: 3 × VERIFY_TIMEOUT plus
# a few seconds of reconnect overhead.
MAX_SEND_ATTEMPTS = 3
# How often to re-fetch the plant during verification. Cheap call; tight
# polling makes successful commands feel as fast as possible.
VERIFY_POLL_INTERVAL = 2.0
# If the gateway hasn't pushed any telemetry for this many seconds when
# a user command arrives, force a fresh OpenNotificationChannel handshake
# before the first send attempt. The reactive self-heal already does
# this — but only after a 180 s verify timeout. Doing it preemptively
# when staleness is plausible turns "first scene of the morning waits
# 3 minutes" into "first scene completes in seconds." Threshold picked
# to be well below any normal idle window the cloud-side claim can
# survive (we've seen healthy claims survive >>10 min idle in logs) so
# we don't burn handshakes on innocent quiet stretches.
PREEMPTIVE_HANDSHAKE_TELEMETRY_AGE = 600.0  # 10 min
# How many consecutive fast-path "success" sends (baseline unknown,
# telemetry fresh, no motor evidence ever observed) we tolerate before
# concluding another app has taken the live-client slot. The Finder
# YOU app silently demotes us when it's opened: subsequent SetOpenPercent
# RPCs are accepted by the cloud but never reach the puck. Our fast-path
# can't tell the difference between "shutter already at target" and
# "command went into a void" — both look identical. Threshold of 1
# means we reclaim immediately on the *current* user command rather
# than making the first post-takeover tap silently fail and recovering
# on the second.
PREEMPTIVE_HANDSHAKE_UNVERIFIED_SENDS = 1

_LOGGER = logging.getLogger(__name__)

# Refresh the JWT a few minutes before it actually expires.
TOKEN_REFRESH_MARGIN = 300  # seconds


class FinderYouCoordinator(DataUpdateCoordinator[dict]):
    """Owns the FinderHomeClient lifecycle and produces shutter state for HA."""

    def __init__(self, hass: HomeAssistant, email: str, password: str) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}/{email}",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL_SECONDS),
        )
        self._email = email
        self._password = password
        self._client: FinderHomeClient | None = None
        self._client_lock = asyncio.Lock()
        # Serializes the SEND step across all command paths. Verification
        # polls can still overlap because we now diff each shutter's own
        # state slice rather than the whole payload.
        self._send_lock = asyncio.Lock()
        self._last_send_ts: float = 0.0
        self._token: dict | None = None
        self._token_expiry: float = 0
        self._plant_id: bytes | None = None
        self._plant_name: str = ""
        self._shutters: list[Shutter] = []
        self._scenarios: list[Scenario] = []
        self._lights: list[Light] = []
        self._switches: list[Switch] = []
        self._channel_states: dict[tuple[str, int], dict[str, Any]] = {}
        # Wedge characterization: track when the gateway last produced
        # fresh telemetry for *any* shutter, so we can tell whether the
        # cloud-to-puck link is alive without needing a user command.
        # ``_previous_slices`` holds the per-shutter state slice from
        # the last successful poll; any byte-level diff (motion, RSSI,
        # position, anything) counts as the gateway being alive.
        self._previous_slices: dict[str, bytes] = {}
        self._last_telemetry_change_ts: float | None = None
        self._last_successful_command_ts: float | None = None
        # Set whenever ``_ensure_client`` completes a fresh handshake.
        # The watchdog uses this to schedule preemptive rehandshakes.
        self._last_handshake_ts: float | None = None
        # Consecutive fast-path successes (baseline unknown, telemetry
        # fresh) with no motor evidence ever observed. Increments on
        # each fast-path, resets on any real verify that sees motor
        # activity. When it hits PREEMPTIVE_HANDSHAKE_UNVERIFIED_SENDS
        # we assume another app stole the live-client slot and force a
        # rehandshake to reclaim it.
        self._unverified_send_count: int = 0

    @property
    def plant_id(self) -> bytes | None:
        return self._plant_id

    @property
    def plant_name(self) -> str:
        return self._plant_name

    @property
    def shutters(self) -> list[Shutter]:
        return self._shutters

    @property
    def scenarios(self) -> list[Scenario]:
        return self._scenarios

    @property
    def lights(self) -> list[Light]:
        return self._lights

    @property
    def switches(self) -> list[Switch]:
        return self._switches

    @property
    def channel_states(self) -> dict[tuple[str, int], dict[str, Any]]:
        return self._channel_states


    @property
    def last_telemetry_change_ts(self) -> float | None:
        """Unix timestamp of the last poll that observed any slice diff."""
        return self._last_telemetry_change_ts

    @property
    def last_successful_command_ts(self) -> float | None:
        """Unix timestamp of the last verified command."""
        return self._last_successful_command_ts

    @property
    def last_handshake_ts(self) -> float | None:
        """Unix timestamp of the last successful OpenNotificationChannel handshake."""
        return self._last_handshake_ts

    async def _ensure_token(self) -> None:
        now = time.time()
        if self._token and now < self._token_expiry - TOKEN_REFRESH_MARGIN:
            return
        if self._token and self._token.get("refresh_token"):
            try:
                self._token = await refresh_token(self._token["refresh_token"])
            except OAuthError:
                _LOGGER.info("refresh failed, doing fresh login")
                self._token = None
        if not self._token:
            self._token = await fetch_token(self._email, self._password)
        self._token_expiry = time.time() + int(self._token.get("expires_in", 3600))

    async def _ensure_client(self) -> FinderHomeClient:
        if self._client is not None:
            return self._client
        await self._ensure_token()
        assert self._token is not None
        try:
            client = await FinderHomeClient.connect(self._token["access_token"])
            plants_msg = await client.handshake()
            # First GetUserPlants response carries the plant UUID in field 1
            # of the inner Plant message (field 3 of the wrapper).
            if 3 in plants_msg:
                from .api.proto import parse_fields  # local import to avoid cycle

                inner = parse_fields(plants_msg[3][0])
                self._plant_id = inner.get(1, [b""])[0]
        except Exception:
            # If anything during handshake fails, drop the client so we'll
            # reconnect on the next update.
            try:
                await client.close()  # type: ignore[name-defined]
            except Exception:
                pass
            raise
        self._client = client
        self._last_handshake_ts = time.time()
        return client

    async def _drop_client(self) -> None:
        """Close + forget the current client so the next call reconnects."""
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None

    async def _run_or_reconnect(self, fn):
        """Run ``fn(client)`` once. If the connection looks dead (any error or
        timeout), drop the client and retry once with a fresh one.

        Without this, a silent GOAWAY or a coordinator update timeout leaves
        ``_client`` half-broken: HA service calls would write into a closed
        socket and hang forever, with no log line, because the underlying
        cloud protocol has no exception path that surfaces above the
        per-call read.
        """
        try:
            client = await self._ensure_client()
            return await asyncio.wait_for(fn(client), timeout=15)
        except Exception as err:
            _LOGGER.info("client call failed (%s); reconnecting", err)
            await self._drop_client()
            client = await self._ensure_client()
            return await asyncio.wait_for(fn(client), timeout=15)

    async def _async_update_data(self) -> dict[str, int | None]:
        """Fetch plant + return ``{shutter_uuid: position}`` for HA cover."""
        try:
            plant_payload = await self._run_or_reconnect(
                lambda c: c.get_plant(self._plant_id) if self._plant_id else c.handshake()
            )
        except (TimeoutError, FinderApiError, ConnectionError, OAuthError, OSError) as err:
            await self._drop_client()
            raise UpdateFailed(str(err)) from err

        # If we fell back to handshake (no plant_id yet), the payload is the
        # plants_msg dict, not the raw plant bytes.
        if isinstance(plant_payload, dict):
            return {s.uuid: None for s in self._shutters}

        plant_name, shutters = parse_plant(plant_payload)
        self._plant_name = plant_name or self._plant_name
        if shutters:
            self._shutters = shutters
        scenarios = parse_scenarios(plant_payload)
        if scenarios:
            self._scenarios = scenarios
        lights, switches = parse_lights_and_switches(plant_payload)
        if lights:
            self._lights = lights
        if switches:
            self._switches = switches
        self._channel_states = extract_channel_states(plant_payload)
        positions = extract_shutter_positions(plant_payload)
        self._track_telemetry_freshness(plant_payload)
        return {s.uuid: positions.get(s.uuid) for s in self._shutters}


    def _track_telemetry_freshness(self, plant_payload: bytes) -> None:
        """Update freshness markers and log slice transitions.

        We diff each shutter's per-shutter state slice against the prior
        poll. Any byte-level difference (position, motion, RSSI, etc.)
        is proof the gateway is still pushing data to the cloud — i.e.
        the puck-to-Finder pipe is alive. Stretches of zero diffs while
        commands are being issued, or stretches of any length on a quiet
        plant, are what we want to characterize so we can find the
        threshold that precedes a wedge.

        Logs at INFO level so the user can `journalctl | grep
        finder_you.coordinator` and see the timeline without flipping
        DEBUG on the noisy lower layers.
        """
        new_slices = extract_shutter_states(plant_payload)
        first_poll = not self._previous_slices
        changed: list[str] = []
        for uuid, slc in new_slices.items():
            if self._previous_slices.get(uuid) != slc:
                changed.append(uuid)
        self._previous_slices = new_slices
        # Skip the freshness stamp + log on the very first poll — every
        # shutter looks "changed" because we had no baseline, and that
        # would falsely register as gateway activity at startup.
        if not changed or first_poll:
            return
        self._last_telemetry_change_ts = time.time()
        _LOGGER.info(
            "telemetry: %d shutter(s) updated: %s",
            len(changed),
            ", ".join(u[:8] for u in changed),
        )

    def _telemetry_recent(self) -> bool:
        """Has the gateway pushed any per-shutter slice diff in the
        ``PREEMPTIVE_HANDSHAKE_TELEMETRY_AGE`` window? When true, the
        puck-to-cloud pipe is demonstrably alive even if a particular
        shutter's cached position has expired — which is what lets us
        treat a `baseline_position is None` send as a safe no-op
        without falling for a wedge."""
        if self._last_telemetry_change_ts is None:
            return False
        return time.time() - self._last_telemetry_change_ts < PREEMPTIVE_HANDSHAKE_TELEMETRY_AGE

    def _should_preemptive_rehandshake(self) -> bool:
        """Decide whether to drop the cloud client before the first send.

        Two independent triggers:

          * **Stale telemetry**: the puck hasn't produced any per-shutter
            slice diff for longer than
            ``PREEMPTIVE_HANDSHAKE_TELEMETRY_AGE``. Strong empirical
            signal of cloud-subscription drift (morning of 2026-06-18:
            8 h of zero telemetry before the first scene sat through
            three 180 s verify timeouts).

          * **Live-client takeover**: ``_unverified_send_count`` has
            accumulated past ``PREEMPTIVE_HANDSHAKE_UNVERIFIED_SENDS``.
            When the Finder YOU mobile app is opened it silently grabs
            the live-client slot from us — our SetOpenPercent RPCs are
            accepted by the cloud but never reach the puck. We can't
            distinguish a true no-op (shutter at target) from a silent
            takeover within one command, but consecutive no-evidence
            commands across different shutters/targets are diagnostic.

        Skipped while another command is already in flight (``_send_lock``
        held) — ripping out the client underneath it would just cascade.
        """
        if self._send_lock.locked():
            return False
        if (
            self._last_telemetry_change_ts is not None
            and time.time() - self._last_telemetry_change_ts >= PREEMPTIVE_HANDSHAKE_TELEMETRY_AGE
        ):
            return True
        if self._unverified_send_count >= PREEMPTIVE_HANDSHAKE_UNVERIFIED_SENDS:
            return True
        return False

    async def _send_command(self, shutter_uuid: str, target: int, do_call) -> None:
        """Send a command, retrying with fresh handshake on verify failure.

        Each verify timeout means one of:
          * The gateway's cloud-side subscription has gone stale (server
            restart, claim expiry) and is dropping our RPC silently.
            ``_drop_client`` fixes this on the next call.
          * The gateway's BLE-mesh hop to this specific shutter dropped
            on the floor (congestion, transient link). A fresh re-send
            gets a new shot.

        Both paths benefit from "drop, reconnect, send again". We try up
        to ``MAX_SEND_ATTEMPTS`` times; if all of them fail to elicit
        motor evidence the gateway is truly wedged for this shutter and
        we raise so HA can surface the failure.

        Before the first attempt we may also do a *preemptive*
        rehandshake — see ``_should_preemptive_rehandshake`` — which
        skips an otherwise-guaranteed 180 s verify timeout when the
        gateway's telemetry has been silent long enough to suggest
        subscription drift.
        """
        if self._should_preemptive_rehandshake():
            tel = (
                f"telemetry {time.time() - self._last_telemetry_change_ts:.0f}s old"
                if self._last_telemetry_change_ts is not None
                else "no telemetry baseline"
            )
            _LOGGER.info(
                "preemptive rehandshake before send (%s, %d unverified sends)",
                tel,
                self._unverified_send_count,
            )
            await self._drop_client()
            # Give the freshly reclaimed claim a clean slate before we
            # start re-incrementing the suspicion counter.
            self._unverified_send_count = 0
        # We allow one in-line reclaim+retry per command: if the first
        # attempt returns via the unknown-baseline fast-path (no motor
        # evidence ever observed) we can't tell whether it was a true
        # no-op or a silent failure due to live-client takeover. Rather
        # than guess, drop the client to force a fresh handshake and
        # try once more. A second fast-path success after a fresh claim
        # is accepted as a true no-op — if even the reclaim didn't
        # surface motor evidence, the shutter is almost certainly at
        # target. Bounded to one reclaim-retry to keep scene UX from
        # paying a handshake cost per shutter when nothing's broken.
        inline_reclaim_used = False
        for attempt in range(MAX_SEND_ATTEMPTS):
            pre_count = self._unverified_send_count
            try:
                await self._send_and_verify(shutter_uuid, target, do_call)
            except GatewayOfflineError:
                if attempt == MAX_SEND_ATTEMPTS - 1:
                    raise
                _LOGGER.info(
                    "gateway didn't reflect %s on attempt %d/%d; "
                    "dropping client and retrying",
                    shutter_uuid,
                    attempt + 1,
                    MAX_SEND_ATTEMPTS,
                )
                await self._drop_client()
                continue
            if (
                self._unverified_send_count > pre_count
                and not inline_reclaim_used
                and attempt + 1 < MAX_SEND_ATTEMPTS
            ):
                _LOGGER.info(
                    "%s succeeded only via fast-path; reclaiming live-client "
                    "and retrying once",
                    shutter_uuid,
                )
                await self._drop_client()
                # Roll the suspicion counter back so the retry starts
                # fresh — if it also goes via fast-path we'll re-increment.
                self._unverified_send_count = pre_count
                inline_reclaim_used = True
                continue
            return

    async def _send_and_verify(self, shutter_uuid: str, target: int, do_call) -> None:
        """One send-then-verify cycle.

        The YESLY gateway's WiFi/MQTT link silently drops commands when
        more than one or two arrive within ~100 ms, and its BLE-mesh
        fan-out to the actual shutters drops the farthest hops when six
        shutters fire concurrently. Defense:

          * Serialize sends through ``_send_lock`` with ``COMMAND_SEND_GAP``
            between consecutive calls so neither the WiFi link nor the
            BLE-mesh sees a burst.
          * After the cloud accepts the send, capture the shutter's
            current position and motion flag and poll the plant until we
            see *evidence the motor actually ran* (see
            ``_wait_for_motor_evidence``) or ``VERIFY_TIMEOUT`` elapses.

        Why two signals (position + motion) instead of just position:
        the position field changes when the shutter reports its new
        physical reading back to the gateway via BLE-mesh telemetry —
        usually within seconds, but the cache lag can stretch past 90 s
        during scene bursts, and on a shutter whose pre-send position
        was already empty (Camera Alex in our setup) we have no baseline
        to detect a change against. The motion flag (#12) reads 3 while
        the gateway is actively driving the motor, so observing it even
        briefly is positive proof that the BLE-mesh hop reached the
        shutter — independent of whether telemetry has come back yet.

        Short-circuit: if the shutter's position already matches
        ``target`` we don't expect any motor movement, so we send the
        command for safety but skip the verify wait.

        On verify timeout we raise ``GatewayOfflineError`` so the caller
        can drop+retry or surface the failure to HA.
        """
        async with self._send_lock:
            loop = asyncio.get_event_loop()
            wait = COMMAND_SEND_GAP - (loop.time() - self._last_send_ts)
            if wait > 0:
                await asyncio.sleep(wait)
            baseline_payload = await self._run_or_reconnect(lambda c: c.get_plant(self._plant_id))
            await self._run_or_reconnect(do_call)
            self._last_send_ts = asyncio.get_event_loop().time()

        baseline_position = extract_shutter_positions(baseline_payload).get(shutter_uuid)
        if baseline_position == target:
            # Already at target — no motor movement to detect. This is
            # a definite no-op (we saw the exact target value), so we
            # also reset the suspicion counter: the cloud showed us a
            # current value, which means the puck-to-cloud pipe is
            # alive on our subscription.
            self._unverified_send_count = 0
            self._last_successful_command_ts = time.time()
            await self.async_request_refresh()
            return
        if baseline_position is None:
            # Shutters without telemetry in plant cache (or whose telemetry
            # has expired) don't report position or baseline.
            # Check briefly for motor movement (motion flag 3 or position appearance).
            # If not detected within a short window, accept optimistically.
            if await self._wait_for_motor_evidence(shutter_uuid, baseline_position, timeout=6.0):
                self._unverified_send_count = 0
                _LOGGER.debug("%s baseline unknown but motor movement observed", shutter_uuid)
            else:
                self._unverified_send_count += 1
                _LOGGER.debug(
                    "%s baseline unknown and no motor movement within 6s — treating as accepted/no-op (suspicion %d)",
                    shutter_uuid,
                    self._unverified_send_count,
                )
            self._last_successful_command_ts = time.time()
            await self.async_request_refresh()
            return
        if not await self._wait_for_motor_evidence(shutter_uuid, baseline_position):
            raise GatewayOfflineError(
                f"gateway didn't reflect {shutter_uuid} within "
                f"{VERIFY_TIMEOUT:.0f}s — likely offline or congested"
            )
        # Real motor evidence — the puck heard us and drove the motor.
        # That's positive proof we still hold the live-client claim.
        self._unverified_send_count = 0
        # Verify succeeded: kick a coordinator refresh so HA's state
        # picks up the new position the next time HomeKit reads it.
        self._last_successful_command_ts = time.time()
        await self.async_request_refresh()

    async def _wait_for_motor_evidence(
        self,
        shutter_uuid: str,
        baseline_position: int | None,
        timeout: float = VERIFY_TIMEOUT,
    ) -> bool:
        """Poll the plant for proof the shutter motor actually ran.

        Either signal counts as evidence:
          * Position field changes from ``baseline_position`` — the
            shutter has reported a new physical reading back to the
            gateway. Most reliable, but needs a known baseline and is
            subject to telemetry cache lag.
          * Motion flag (#12) observed at 3 ("moving") — the gateway is
            actively driving the motor over BLE-mesh. Faster than
            position telemetry and doesn't need a baseline; the only way
            we'd observe 3 is if the BLE-mesh hop reached the shutter.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(VERIFY_POLL_INTERVAL)
            try:
                payload = await self._run_or_reconnect(lambda c: c.get_plant(self._plant_id))
            except Exception:
                _LOGGER.debug("verify poll failed", exc_info=True)
                continue
            if extract_shutter_positions(payload).get(shutter_uuid) != baseline_position:
                return True
            if extract_shutter_motion(payload).get(shutter_uuid) == 3:
                return True
        return False

    async def async_set_position(self, shutter_uuid: str, percent: int) -> None:
        assert self._plant_id is not None
        await self._send_command(
            shutter_uuid,
            percent,
            lambda c: c.set_open_percent(self._plant_id, shutter_uuid.encode(), percent),
        )

    async def async_open(self, shutter_uuid: str) -> None:
        assert self._plant_id is not None
        await self._send_command(
            shutter_uuid,
            100,
            lambda c: c.open_full(self._plant_id, shutter_uuid.encode()),
        )

    async def async_close_shutter(self, shutter_uuid: str) -> None:
        assert self._plant_id is not None
        await self._send_command(
            shutter_uuid,
            0,
            lambda c: c.close_full(self._plant_id, shutter_uuid.encode()),
        )

    async def async_activate_scenario(self, scenario_uuid: str) -> None:
        """Activate a cloud scenario."""
        assert self._plant_id is not None
        await self._run_or_reconnect(
            lambda c: c.activate_scenario(self._plant_id, scenario_uuid.encode())
        )
        _LOGGER.info("activated scenario %s", scenario_uuid)

    async def async_switch_on(self, device_uuid: str, channel: int = 1) -> None:
        """Switch on a light or plug channel."""
        assert self._plant_id is not None
        curr = self._channel_states.get((device_uuid, channel), {}).get("is_on")
        if curr is True:
            _LOGGER.debug("device %s channel %d already on, skipping toggle", device_uuid, channel)
            return
        await self._run_or_reconnect(
            lambda c: c.switch_on(self._plant_id, device_uuid.encode(), channel)
        )
        self._channel_states.setdefault((device_uuid, channel), {})["is_on"] = True
        _LOGGER.info("switched on device %s channel %d", device_uuid, channel)

    async def async_switch_off(self, device_uuid: str, channel: int = 1) -> None:
        """Switch off a light or plug channel."""
        assert self._plant_id is not None
        curr = self._channel_states.get((device_uuid, channel), {}).get("is_on")
        if curr is False:
            _LOGGER.debug("device %s channel %d already off, skipping toggle", device_uuid, channel)
            return
        await self._run_or_reconnect(
            lambda c: c.switch_off(self._plant_id, device_uuid.encode(), channel)
        )
        self._channel_states.setdefault((device_uuid, channel), {})["is_on"] = False
        _LOGGER.info("switched off device %s channel %d", device_uuid, channel)

    async def async_set_brightness(self, device_uuid: str, percent: int, channel: int = 1) -> None:
        """Set brightness level (0-100%) on a dimmer channel."""
        assert self._plant_id is not None
        await self._run_or_reconnect(
            lambda c: c.set_brightness(self._plant_id, device_uuid.encode(), percent, channel)
        )
        ch_state = self._channel_states.setdefault((device_uuid, channel), {})
        ch_state["is_on"] = (percent > 0)
        ch_state["brightness"] = round((percent / 100) * 255)
        _LOGGER.info("set brightness on device %s channel %d to %d%%", device_uuid, channel, percent)

    async def async_shutdown(self) -> None:

        if self._client:
            await self._client.close()
            self._client = None
