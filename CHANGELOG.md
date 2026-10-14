# Changelog - homeassistant-finder-you

Tutte le modifiche degne di nota a questa integrazione personalizzata per Home Assistant sono documentate in questo file.

## [Unreleased]

### Aggiunto
- **Fallback Cloud Deterministico a 3.0s con Conferma Telemetrica Reale**:
  - In `coordinator.py`, implementato `_state_change_events` con `BLE_STATE_CONFIRMATION_TIMEOUT = 3.0s`: `async_switch_on` e `async_switch_off` inviano il comando BLE locale (max 2 nodi esphome con gap 300ms) e attendono fino a 3.0s per l'arrivo della telemetria fisica reale dalla rete mesh.
  - Se entro 3.0s il relè fisico non conferma la commutazione, l'integrazione attiva il comando discreto su Finder Cloud (`c.switch_on` / `c.switch_off`), evitando doppi toggle ravvicinati o spegnimenti accidentali.
- **Protezione Anti-Rimbalzo Stato (Lock Temporale Canale & Guardia BLE)**:
  - In `coordinator.py`, introdotto `CHANNEL_STATE_LOCK_WINDOW` (15s) e il tracciamento `_channel_lock_until` su ogni operazione di spegnimento/accensione/dimmerazione.
  - In `_async_update_data`, il polling periodico cloud Finder non sovrascrive più lo stato locale se il canale si trova nella finestra di lock, eliminando il rimbalzo su `ON` causato dalla latenza del gateway cloud.
  - In `topology.py`, estesa la finestra di guardia `_tx_guard_until` (`BLE_TX_GUARD_DURATION` = 6.0s) per ignorare telemetrie mesh BLE ritardate post-comando.
- **Risoluzione Automatica Nodi ESPHome via Registry (`device_id`) & Affinità Stanza**:
  - In `topology.py`, aggiunto resolver intelligente `_resolve_esphome_node` che mappa i `device_id` degli eventi `esphome.finder_ble_adv` direttamente al nome del nodo ESPHome e all'area/stanza (sfruttando `dr.async_get(hass)` e keyword alias).
  - Implementata affinità per stanza/nome in `select_best_node` (es. priorità automatica a `bluetooth_proxy_sara` per dispositivi in Camera di Sara / Cameretta).
- **Supporto Telemetria Live per Dimmer Finder YESLY 15.21**:
  - In `coordinator.py` (`handle_ble_device_state_update`), aggiunto supporto dedicato per i dimmer YESLY 15.21: l'aggiornamento della percentuale non viene più confuso con le tapparelle (`Shutter`), ma aggiorna lo stato relè (`is_on`) e il livello `brightness` nel canale luce.

### Modificato
- **Fix Conferma Telemetrica BLE (`_pending_ble_target`) & Filtro Frame Mesh Relay (`0x4x`)**:
  - Risolto bug in `coordinator.py` (`handle_ble_device_state_update`) per cui l'aggiornamento ottimistico preventivo di `self._channel_states[key]["is_on"]` impediva l'attivazione di `change_evt.set()` alla ricezione del pacchetto BLE di conferma (`curr == is_on`), causando falsi timeout a 3.0s e doppi impulsi via Cloud Fallback.
  - Esclusi in `topology.py` i frame ripetuti di mesh (`0x4x`, `is_mesh_relay=True`, 13 byte cifrati) dalla decodifica dello stato relè e dal calcolo RSSI diretto, eliminando le false oscillazioni `ON/OFF` a 4-15ms sul relè ripetitore (`luce_corridoio`).
- **Rimozione Blocco Cieco della Telemetria in `topology.py`**:
  - Eliminata la soppressione automatica dei pacchetti durante `_tx_guard_until`: ora qualsiasi advertisement BLE valido inviato dal relè fisico (con payload >= 11 byte) viene analizzato ed emesso immediatamente, garantendo la ricezione istantanea del feedback senza perdite.
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
