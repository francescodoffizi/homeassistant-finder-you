# Changelog - homeassistant-finder-you

Tutte le modifiche degne di nota a questa integrazione personalizzata per Home Assistant sono documentate in questo file.

## [Unreleased]

### Aggiunto
- **Risoluzione Automatica Nodi ESPHome via Registry (`device_id`) & Affinità Stanza**:
  - In `topology.py`, aggiunto resolver intelligente `_resolve_esphome_node` che mappa i `device_id` degli eventi `esphome.finder_ble_adv` direttamente al nome del nodo ESPHome e all'area/stanza (sfruttando `dr.async_get(hass)` e keyword alias).
  - Implementata affinità per stanza/nome in `select_best_node` (es. priorità automatica a `bluetooth_proxy_sara` per dispositivi in Camera di Sara / Cameretta).
- **Supporto Telemetria Live per Dimmer Finder YESLY 15.21**:
  - In `coordinator.py` (`handle_ble_device_state_update`), aggiunto supporto dedicato per i dimmer YESLY 15.21: l'aggiornamento della percentuale non viene più confuso con le tapparelle (`Shutter`), ma aggiorna lo stato relè (`is_on`) e il livello `brightness` nel canale luce.

### Modificato
- **Rimozione Early-Return Bloccante su `light.py`**:
  - Rimossi i controlli statici `if self.is_on: return` e `if not self.is_on: return` da `async_turn_on()` e `async_turn_off()`, permettendo all'utente di forzare sempre il comando verso il dispositivo fisico in caso di disallineamento iniziale.
- **Normalizzazione Offset Byte Dimmer & Shutter**:
  - In `topology.py`, corretto l'indice di estrazione della percentuale `percent_val` per eliminare i salti anomali tra frame relay e diretti.
- **Risoluzione Fibrillazione Rele (Rimozione Multi-Proxy Broadcast su Toggle)**:
  - In `topology.py`, rimosso il broadcast multi-proxy sequenziale per `async_dispatch_click` che causava toggle ripetuti (sfarfallio rapido on/off a 300ms) sui relè fisici (13.22/13.72).
  - La selezione del nodo (`target_node`) ora utilizza sempre `select_best_node(device)` per instradare il click verso il singolo proxy ottimale, con retry su singolo nodo alternativo in caso di eccezione del servizio.
- Rimosso l'early-return con controllo statico su `switch.py` che impediva re-invii espliciti del comando se l'entità era già considerata nel medesimo stato.
- Corretto l'import asincrono mancante (`import asyncio`) in `topology.py`.

---

## [1.0.0] - 2026-10-10
- Rilascio iniziale dell'integrazione personalizzata `finder_you`.
- Client gRPC over HTTP/2 con bypass live-client e gestione automatica dei token OAuth2 IdentityServer4.
- Supporto a tapparelle (`cover`), luci/dimmer (`light`) e prese/relè smart (`switch`).
- Canale BLE Diretto Locale (Local-First Mesh Routing) tramite ESPHome Bluetooth Proxy.
