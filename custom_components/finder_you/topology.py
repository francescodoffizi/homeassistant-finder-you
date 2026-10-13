"""Topology and dynamic routing for Finder YESLY BLE mesh nodes.

Tracks link quality (RSSI) of all Finder YESLY devices seen by ESPHome nodes
distributed throughout the home, and routes local commands through the best hop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.core import Event, HomeAssistant, callback

if TYPE_CHECKING:
    from .api import BleDirectDevice

_LOGGER = logging.getLogger(__name__)

# RSSI considered strong enough for direct transmission
EXCELLENT_RSSI_THRESHOLD = -75
# Maximum age in seconds before an RSSI observation is considered stale
TOPOLOGY_STALE_TIMEOUT = 300.0


class FinderTopologyRouter:
    """Tracks ESPHome node visibility and selects the best transmitter for each device."""

    def __init__(self, hass: HomeAssistant, coordinator: Any = None) -> None:
        self.hass = hass
        self._coordinator = coordinator
        # device_mac -> {node_name: {"rssi": int, "last_seen": float, "mesh_hop": bool}}
        self._topology: dict[str, dict[str, dict[str, Any]]] = {}
        # Set of active ESPHome node names discovered on the network
        self._esphome_nodes: set[str] = set()
        self._unsub_listener = None

    def start(self) -> None:
        """Start listening for BLE advertisement events and register known nodes."""
        self._unsub_listener = self.hass.bus.async_listen(
            "esphome.finder_ble_adv", self._handle_ble_adv_event
        )
        self._discover_registered_nodes()
        _LOGGER.info("Finder BLE Topology Router started")

    def stop(self) -> None:
        """Stop listening to events."""
        if self._unsub_listener:
            self._unsub_listener()
            self._unsub_listener = None

    def _discover_registered_nodes(self) -> None:
        """Scan HA services to find ESPHome nodes that expose finder_mesh services."""
        services = self.hass.services.async_services()
        esphome_services = services.get("esphome", {})
        for s_name in esphome_services:
            if "finder_mesh" in s_name:
                parts = s_name.split("_finder_mesh_")
                if len(parts) == 2:
                    node_name = parts[0]
                    self._esphome_nodes.add(node_name)
                    _LOGGER.info("Discovered compatible ESPHome mesh node: %s", node_name)

    @callback
    def _handle_ble_adv_event(self, event: Event) -> None:
        """Process incoming BLE advertisement observation from an ESPHome node."""
        data = event.data
        node = data.get("node")
        mac = data.get("mac")
        rssi_str = data.get("rssi")
        mf_str = data.get("mf", "")

        if not mac or not rssi_str:
            return

        try:
            rssi = int(rssi_str)
        except (ValueError, TypeError):
            return

        if node:
            self._esphome_nodes.add(node)
        mac_clean = mac.upper().replace("-", ":")
        
        # Parse manufacturer hex bytes
        byte_list: list[int] = []
        parts = mf_str.strip().split()
        if parts:
            for p in parts:
                try:
                    byte_list.append(int(p, 16))
                except ValueError:
                    pass

        # Check if this frame is a mesh repeating frame (starts with 0x4x in MF)
        is_mesh_relay = False
        if byte_list:
            if (byte_list[0] & 0x40) != 0:
                is_mesh_relay = True

        if node:
            node_entry = self._topology.setdefault(mac_clean, {}).setdefault(node, {})
            node_entry["rssi"] = rssi
            node_entry["last_seen"] = time.time()
            node_entry["is_mesh_relay"] = is_mesh_relay

        _LOGGER.debug(
            "BLE Topology updated: MAC %s seen by node '%s' (RSSI: %d dBm, mesh=%s)",
            mac_clean,
            node or "unknown",
            rssi,
            is_mesh_relay,
        )

        # Telemetry decoding: match MAC with device inventory in coordinator
        if self._coordinator and hasattr(self._coordinator, "ble_inventory"):
            inv = self._coordinator.ble_inventory
            matched_dev = None

            # 1. Match direct frame by exact MAC
            for dev in inv.values():
                if dev.mac.upper().replace("-", ":") == mac_clean:
                    matched_dev = dev
                    break

            # 2. Match repeating mesh frame (0x4x): originating device MAC suffix is in byte_list[1:4]
            if not matched_dev and is_mesh_relay and len(byte_list) >= 4:
                suffix = f"{byte_list[1]:02X}:{byte_list[2]:02X}:{byte_list[3]:02X}"
                for dev in inv.values():
                    clean_dmac = dev.mac.upper().replace("-", ":")
                    if clean_dmac.endswith(suffix):
                        matched_dev = dev
                        break

            if matched_dev:
                channel_states: dict[int, bool] = {}
                percent_val: int | None = None
                fam = (matched_dev.family or "").upper()

                # Decodifica stato Finder YESLY:
                # In pacchetti diretti (non-relay), lo stato relè è tipicamente a byte 6.
                # In pacchetti repeating mesh (0x4x), il payload ha 4 byte di header mesh (cmd + 3 byte mac suffix),
                # con stato canali a byte 6 o byte 4 a seconda del tipo di frame.
                status_byte: int | None = None
                if is_mesh_relay:
                    if len(byte_list) >= 7:
                        status_byte = byte_list[6]
                    elif len(byte_list) >= 5:
                        status_byte = byte_list[4]
                else:
                    if len(byte_list) >= 7:
                        status_byte = byte_list[6]

                if status_byte is not None:
                    # Canale 1: bit 0; Canale 2: bit 4
                    channel_states[1] = bool(status_byte & 0x01)
                    channel_states[2] = bool(status_byte & 0x10)

                # Dimmer / Shutter percentage value:
                if any(k in fam for k in ["1521", "1571", "13S2", "SHUTTER"]):
                    pos_idx = 5 if is_mesh_relay and len(byte_list) >= 6 else 4
                    if len(byte_list) > pos_idx:
                        raw_pos = byte_list[pos_idx]
                        if 0 <= raw_pos <= 100:
                            percent_val = raw_pos

                if channel_states or percent_val is not None:
                    self._coordinator.handle_ble_device_state_update(
                        matched_dev.uuid,
                        channel_states=channel_states if channel_states else None,
                        percent=percent_val,
                    )

    def select_best_node(self, device: BleDirectDevice) -> str | None:
        """Select the best ESPHome node to transmit to the target device.

        Ranking strategy:
        1. Nodes that have seen this specific MAC directly within the last 5 minutes,
           sorted by highest RSSI (e.g. -60 dBm > -85 dBm).
        2. If no direct sightings, any node that has seen mesh relay frames recently.
        3. Fallback: Any available registered ESPHome node.
        """
        self._discover_registered_nodes()
        if not self._esphome_nodes:
            return None

        mac_upper = device.mac.upper().replace("-", ":")
        now = time.time()

        # 1. Direct sightings of this device
        candidates = self._topology.get(mac_upper, {})
        valid_candidates: list[tuple[str, int]] = []
        for node, info in candidates.items():
            if node in self._esphome_nodes and (now - info.get("last_seen", 0)) < TOPOLOGY_STALE_TIMEOUT:
                valid_candidates.append((node, info.get("rssi", -100)))

        if valid_candidates:
            # Sort by RSSI descending (strongest signal first)
            valid_candidates.sort(key=lambda x: x[1], reverse=True)
            best_node, best_rssi = valid_candidates[0]
            _LOGGER.debug(
                "Selected best node '%s' for MAC %s based on direct RSSI (%d dBm)",
                best_node,
                mac_upper,
                best_rssi,
            )
            return best_node

        # 2. Sighting via mesh repeaters (node that sees the most active mesh traffic)
        mesh_scores: dict[str, int] = {}
        for dev_mac, nodes in self._topology.items():
            for node, info in nodes.items():
                if node in self._esphome_nodes and (now - info.get("last_seen", 0)) < TOPOLOGY_STALE_TIMEOUT:
                    mesh_scores[node] = max(mesh_scores.get(node, -100), info.get("rssi", -100))

        if mesh_scores:
            best_mesh_node = max(mesh_scores.items(), key=lambda x: x[1])[0]
            _LOGGER.debug(
                "Selected best mesh node '%s' for MAC %s based on mesh activity",
                best_mesh_node,
                mac_upper,
            )
            return best_mesh_node

        # 3. Fallback to any discovered node
        fallback_node = next(iter(self._esphome_nodes))
        _LOGGER.debug(
            "Fallback to default node '%s' for MAC %s (no RSSI topology data yet)",
            fallback_node,
            mac_upper,
        )
        return fallback_node

    async def async_dispatch_click(
        self,
        device: BleDirectDevice,
        channel: int,
        preferred_node: str | None = None,
    ) -> bool:
        """Transmit a relay / button click via the best ESPHome node or all nodes."""
        self._discover_registered_nodes()
        if not self._esphome_nodes:
            _LOGGER.warning("No ESPHome mesh node available to send click to %s", device.mac)
            return False

        payload = {
            "mac": device.mac,
            "key": device.key,
            "channel": channel,
        }

        # If a preferred node is given or we have direct RSSI data, use that specific node
        mac_upper = device.mac.upper().replace("-", ":")
        candidates = self._topology.get(mac_upper, {})
        has_direct_sighting = any(
            n in self._esphome_nodes and (time.time() - info.get("last_seen", 0)) < TOPOLOGY_STALE_TIMEOUT
            for n, info in candidates.items()
        )

        target_node = preferred_node or self.select_best_node(device)

        if target_node:
            service_name = f"{target_node}_finder_mesh_click"
            try:
                _LOGGER.info(
                    "Local BLE Dispatch: Calling esphome.%s for MAC %s Ch %d",
                    service_name,
                    device.mac,
                    channel,
                )
                await self.hass.services.async_call("esphome", service_name, payload, blocking=True)
                return True
            except Exception as err:
                _LOGGER.warning(
                    "Failed to send local BLE click via node '%s': %s", target_node, err
                )

        alt_nodes = [n for n in self._esphome_nodes if n != target_node]
        if alt_nodes:
            alt_node = alt_nodes[0]
            alt_service = f"{alt_node}_finder_mesh_click"
            try:
                _LOGGER.info(
                    "Retrying local BLE click via alternate node '%s'", alt_node
                )
                await self.hass.services.async_call("esphome", alt_service, payload, blocking=True)
                return True
            except Exception as alt_err:
                _LOGGER.warning("Alternate node '%s' also failed: %s", alt_node, alt_err)
        return False

    async def async_dispatch_set_percent(
        self,
        device: BleDirectDevice,
        percent: int,
        preferred_node: str | None = None,
    ) -> bool:
        """Transmit a dimmer or shutter position via the best ESPHome node."""
        target_node = preferred_node or self.select_best_node(device)
        if not target_node:
            _LOGGER.warning("No ESPHome mesh node available to set percent on %s", device.mac)
            return False

        service_name = f"{target_node}_finder_mesh_set_percent"
        payload = {
            "mac": device.mac,
            "key": device.key,
            "percent": percent,
        }
        try:
            _LOGGER.info(
                "Local BLE Dispatch: Calling esphome.%s for MAC %s Percent %d%%",
                service_name,
                device.mac,
                percent,
            )
            await self.hass.services.async_call("esphome", service_name, payload, blocking=True)
            return True
        except Exception as err:
            _LOGGER.warning(
                "Failed to send local BLE percent via node '%s': %s", target_node, err
            )
            alt_nodes = [n for n in self._esphome_nodes if n != target_node]
            if alt_nodes:
                alt_node = alt_nodes[0]
                alt_service = f"{alt_node}_finder_mesh_set_percent"
                try:
                    _LOGGER.info(
                        "Retrying local BLE percent via alternate node '%s'", alt_node
                    )
                    await self.hass.services.async_call("esphome", alt_service, payload, blocking=True)
                    return True
                except Exception as alt_err:
                    _LOGGER.warning("Alternate node '%s' also failed: %s", alt_node, alt_err)
            return False

    async def async_dispatch_shutter_stop(
        self,
        device: BleDirectDevice,
        channel: int = 1,
        preferred_node: str | None = None,
    ) -> bool:
        """Transmit a shutter stop command via the best ESPHome node."""
        target_node = preferred_node or self.select_best_node(device)
        if not target_node:
            return False

        service_name = f"{target_node}_finder_mesh_shutter_stop"
        payload = {
            "mac": device.mac,
            "key": device.key,
            "channel": channel,
        }
        try:
            _LOGGER.info(
                "Local BLE Dispatch: Calling esphome.%s for MAC %s Ch %d",
                service_name,
                device.mac,
                channel,
            )
            await self.hass.services.async_call("esphome", service_name, payload, blocking=True)
            return True
        except Exception as err:
            _LOGGER.warning(
                "Failed to send local BLE stop via node '%s': %s", target_node, err
            )
            return False
