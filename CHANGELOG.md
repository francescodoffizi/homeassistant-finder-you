# Changelog - homeassistant-finder-you

Tutte le modifiche degne di nota a questa integrazione personalizzata per Home Assistant sono documentate in questo file.

## [Unreleased]

### Aggiunto
- **Serializzazione e Staggering Radio Multi-Proxy (300 ms)**:
  - In `topology.py`, implementato lo scaglionamento sequenziale a 300 ms tra i proxy ESPHome (`bluetooth_proxy_rack`, `bluetooth_proxy_edge`, `bluetooth_proxy_sara`, `esp32_bluetooth_proxy_18264c`) per serializzare le raffiche radio nell'aria ed eliminare le collisioni di pacchetti RF sui canali primari 37, 38 e 39.
- **Finestra di Tolleranza e Polling Dinamico Telemetria Mesh (6.0s)**:
  - In `coordinator.py`, `_async_verify_switch_state` monitora per 6 secondi a intervalli di 1.5 secondi l'aggiornamento dello stato inviato via BLE-mesh al gateway Finder. Risolve i falsi positivi di disallineamento e azzera gli interventi di Cloud Fallback non necessari.
- **Attributo `syncing` e Protezione UI**:
  - Aggiunto l'attributo `syncing: true/false` su entità `light` e `switch` per fornire feedback immediato all'utente durante l'elaborazione dei comandi e prevenire rimbalzi o doppi toggle a cascata.

### Modificato
- **Risoluzione Fibrillazione Rele (Rimozione Multi-Proxy Broadcast su Toggle)**:
  - In topology.py, rimosso il broadcast multi-proxy sequenziale per async_dispatch_click che causava toggle ripetuti (sfarfallio rapido on/off a 300ms) sui rele fisici (13.22/13.72).
  - La selezione del nodo (target_node) ora utilizza sempre select_best_node(device) per instradare il click verso il singolo proxy ottimale, con retry su singolo nodo alternativo in caso di eccezione del servizio.
- Rimosso l'early-return con controllo statico su `switch.py` che impediva re-invii espliciti del comando se l'entità era già considerata nel medesimo stato.
- Corretto l'import asincrono mancante (`import asyncio`) in `topology.py`.

---

## [1.0.0] - 2026-10-10
- Rilascio iniziale dell'integrazione personalizzata `finder_you`.
- Client gRPC over HTTP/2 con bypass live-client e gestione automatica dei token OAuth2 IdentityServer4.
- Supporto a tapparelle (`cover`), luci/dimmer (`light`) e prese/relè smart (`switch`).
- Canale BLE Diretto Locale (Local-First Mesh Routing) tramite ESPHome Bluetooth Proxy.
