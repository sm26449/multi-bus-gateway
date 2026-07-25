# Device Builder — roadmap interfețe (RS485 · CAN · BLE · I/O)

> Document de planificare, salvat ca să nu pierdem nimic din vedere.
> Starea „livrat" = în `main`, testat automat (556 teste), deployat.
> Nimic de aici nu e început — ne apucăm după validarea planului.

Ultima actualizare: 2026-07-25.

---

## 0. Ce e deja livrat (contextul)

- **Generator RS485/Modbus RTU** — template Modbus + registre → YAML complet
  (`uart` + `modbus` + `modbus_controller`: value types, byte order, scale în
  filtre, poll groups → `update_interval`/`skip_updates`), topicuri MQTT
  explicite per registru, `improv_serial`, LWT. Validat de compilatorul
  ESPHome real; **nevalidat încă pe hardware fizic** (§1).
- **Adopt** — perechea gateway (template MQTT + device mqtt-in) generată din
  aceeași sursă; topicuri byte-identice.
- Build/OTA/loguri live prin containerul ESPHome (zero-config din compose),
  flash USB din browser (esp-web-tools vendored), profile hardware, Update
  all, discovery ESPHome pe LAN (unicast 6053) + import mDNS-adoptable.
- **Gateway direct**: Modbus TCP ✅, **Modbus RTU serial ✅** (`/dev/ttyUSB0`
  passthrough — interfața directă RS485 EXISTĂ deja în MBG), HTTP/JSON ✅,
  MQTT-in ✅. **CAN direct: nu există** (§3B).

---

## 1. Validare hardware RS485 RTU (primul pas, blochează restul)

Scop: fluxul complet pe fier, cap-coadă, o singură dată — apoi știm că
temelia e solidă pentru BLE/CAN/I-O.

**Necesar:** ESP32 devkit (esp32dev), transceiver RS485 (MAX485 cu DE/RE pe
GPIO4, sau auto-direction), contor Modbus real (ex. Eastron SDM120/630 sau
UMG-ul existent pe un port liber), sursă 3.3/5V, două fire A/B.

**Checklist:**
1. Devices → Deploy new device → template-ul contorului, subset mic
   (3–5 registre), profil „ESP32 generic — RS485 on UART2", unit ID real.
2. Preview → verificat topicurile → **Save + Adopt**.
3. `secrets.yaml` (editorul acceptă direct): `wifi_ssid`, `wifi_password`,
   `ota_password`; `mqtt_password` e pus automat dacă broker-ul are parolă.
4. **Build** — prima compilare descarcă toolchain-ul (5–15 min); următoarele
   ~1 min.
5. **Flash USB din browser** — atenție: WebSerial cere Chrome/Edge și
   **HTTPS sau http://localhost** (fallback documentat: download binar +
   orice esptool). Improv configurează Wi-Fi pe același cablu.
6. Nod pornit → verificăm: LWT `esphome/<nod>/status` = online; valorile pe
   topicuri; device-ul pereche în Devices prinde datele (staleness OK);
   Dashboard/History/Influx.
7. **Flash OTA** după o modificare minoră — confirmăm bucla de update.
8. Negative: contor deconectat de pe RS485 → sensor `unavailable`/NaN?
   Ce publică ESPHome și cum îl mapează mqtt-in (de verificat: NaN e
   respins de driver — valoarea rămâne stale, corect). Documentăm.
9. Rezultatele + orice surpriză → secțiune nouă în MANUAL §11b.

**De îmbunătățit dacă testul o cere:** `flow_control_pin` implicit în
profilul generic? `rx_buffer_size`? retry/timeout modbus_controller expuse
în wizard (acum default-uri ESPHome).

---

## 2. Profiluri BLE în generator (efort: ~1 zi)

Scop: nod „BLE gateway" generat din UI — citește senzori BLE din jur și
publică MQTT, pereche mqtt-in creată la Adopt.

- **Design**: în wizard, tip de nod nou „BLE scanner" (nu pornește de la un
  template Modbus, ci de la o listă de senzori): `esp32_ble_tracker` +
  platforme per senzor: `xiaomi_*` (LYWSD03MMC etc.), `ruuvitag`,
  `bthome` (senzori BTHome v2), `b_parasite`… fiecare cu MAC + chei
  (bindkey Xiaomi) unde e cazul.
- Topicuri: același contract `<prefix>/<nume>/state` → perechea mqtt-in
  identică cu cea de la Modbus (reutilizăm tot mecanismul Adopt).
- Aliniere cu `ble_theengs_sensor` (calea existentă prin Theengs rămâne
  alternativa fără ESP32 dedicat).
- Opțional (checkbox): `bluetooth_proxy` pentru Home Assistant — nodul e
  și proxy BT pe lângă scanner.
- **Testare**: unit pe YAML-ul emis; E2E validate cu ESPHome real; hardware
  cu 1–2 senzori (avem senzori Xiaomi/BTHome în casă? de inventariat).

---

## 3. CAN — două straturi, O SINGURĂ schemă de semnale

CAN nu are „registre": are **frame-uri** (ID 11/29-bit) purtând **semnale**
pe biți (stil DBC). Cheia de design: extindem schema de template o singură
dată, și o folosesc AMBELE straturi.

### 3A. Extensia de schemă (design-first, de discutat înainte de cod)

Registru de tip nou în `device_template` (schema v2 sau câmpuri opționale):

```json
{ "name": "SOC", "label": "State of charge", "unit": "%",
  "can_id": 849, "can_id_extended": false,
  "start_bit": 0, "bit_length": 16, "byte_order": "little",
  "signed": false, "scale": 10, "offset": 0,
  "poll_group": "realtime" }
```

- Validare: poziții de biți în [0,64), suprapuneri semnalate, id-uri
  duplicate OK (mai multe semnale per frame).
- Template-uri exemplu de livrat: **Pylontech CAN** (SMA/Victron profile,
  0x351/0x355/0x356/0x359), poate Victron VE.Can meter — surse publice,
  verificate pe documente.
- `transports: ["can"]` → `template_transport()` învață „can".

### 3B. Gateway direct: driver CAN nativ (efort: mediu-mare)

- **python-can + SocketCAN** (`can0`): host-ul face `ip link set can0 up
  type can bitrate 500000`, containerul primește interfața prin
  `network_mode: host`?? NU — SocketCAN merge în container cu
  `--cap-add NET_ADMIN` sau interfața mutată în netns; **de investigat
  concret pe Docker** (variantă sigură: `network: host` doar dacă am
  accepta-o; alternativ cgw/candump relay). Adaptoare: gs_usb/candleLight
  (nativ kernel), slcan (CANable serial).
- `CanClient` nou lângă ModbusClient/HttpClient: ascultă (bus pasiv, nu
  poll!) → decodează semnalele per schema §3A → value store. Poll groups
  devin irelevante (event-driven) — `stale_after_s` rămâne singura noțiune.
- Config device: `protocol: can`, `connection: {channel: can0, bitrate:…}`.
- Restul vine gratis: MQTT/Influx/HA/metere virtuale/UI (un device ca
  oricare altul).

### 3C. Nod ESP32 cu CAN (efort: mediu, după 3A)

- ESPHome `canbus`: **TWAI intern ESP32** (transceiver extern SN65HVD230)
  sau **MCP2515** pe SPI (esp8266/placi fără TWAI).
- Generatorul emite `canbus:` + `on_frame` per can_id cu lambda care
  extrage semnalele (biți → valoare → `mqtt.publish` pe topicul standard).
  Atenție la mărimea lambdas-urilor — generăm cod C++ curat, testat cu
  `esphome compile` real în CI-ul nostru de teste E2E.
- Adopt identic (mqtt-in pereche) — nimic nou pe partea de gateway.

---

## 4. Noduri cu intrări/ieșiri (GPIO, S0/pulse, relee)

Scop: „placa cu intrari/iesiri" — nodul nu doar citește un bus, ci și I/O:

- **Intrări digitale** → `binary_sensor` (contacte, alarme) → topic state.
- **Contor de impulsuri S0** → `pulse_counter`/`pulse_meter` (contoare de
  energie ieftine cu ieșire S0!) → kWh cumulat + putere instant. Foarte
  valoros, efort mic — candidat de inclus devreme.
- **Ieșiri (relee/switch)** → `switch` pe GPIO + topic de comandă.
  ⚠️ **Design de securitate necesar**: MBG nu are azi cale de SCRIERE prin
  MQTT către device-uri (write gate-ul există doar pe Modbus). De decis:
  extindem write gate-ul (allow_writes + roluri + audit + lease) la un
  „mqtt command out" per registru writable, sau lăsăm comanda pe HA/Node-RED
  în faza 1 și MBG rămâne read-only pe noduri. **Recomandare: faza 1
  read-only** (input+S0), scrierea = fază separată cu design.
- Wizard: secțiune „I/O" în generator (pin, tip, invert, debounce,
  multiplicator impulsuri).

## 5. Profiluri pentru plăci integrate (LilyGO & co)

Plăci cu RS485/CAN la bord — profilurile trebuie **verificate pe schematic**
înainte de livrare (nu inventăm pini):

- **LilyGO T-CAN485** (ESP32, RS485 + CAN pe aceeași placă — ținta ideală
  pentru §3C + §1): pini RS485 (UART, enable), CAN (TWAI), de confirmat din
  schematicul oficial + test fizic.
- LilyGO T-Connect / T-ETH-Lite (variante cu Ethernet — bonus: `ethernet:`
  în loc de Wi-Fi în generator = opțiune nouă „network: wifi|ethernet").
- M5Stack Atomic RS485 kit, Waveshare ESP32-S3 RS485/CAN…
- Profilurile devin shareable JSON (mecanismul există) + PR-friendly listă
  în `docs/device-catalog.md`.

## 6. Plan de testare (pe fiecare etapă, în ordinea asta)

1. Unit + route tests (pattern-ul existent: FakeDashboard, socket-uri reale
   unde e protocol).
2. `esphome validate`/`compile` REAL pe fiecare YAML generat nou (BLE/CAN/
   I-O) — deja avem calea prin WS.
3. Playwright pe instanță de test izolată (niciodată pe live).
4. Hardware: checklist per interfață (§1 e șablonul).
5. Suita completă + deploy live doar la final de etapă.

## 7. Necesar hardware (de procurat/inventariat înainte de start)

| Piesă | Pentru | Note |
|---|---|---|
| ESP32 devkit (esp32dev) | §1 | probabil există deja |
| Transceiver RS485 (MAX485/auto-dir) | §1 | 2 buc (unul de rezervă) |
| LilyGO **T-CAN485** | §3C, §5 | RS485+CAN pe o placă — cea mai eficientă achiziție |
| Transceiver CAN SN65HVD230 | §3C alternativ | dacă folosim devkit + TWAI |
| USB-CAN (CANable/candleLight) | §3B | pentru driverul direct SocketCAN |
| Sursă CAN de test | §3 | ideal: BMS Pylontech-profil sau un al doilea nod care emite |
| Contor cu ieșire S0 | §4 | opțional, ieftin |
| Senzor BLE (Xiaomi LYWSD03MMC / BTHome) | §2 | de inventariat ce există în casă |

## 8. Ordinea propusă & estimări

| # | Etapă | Depinde de | Estimare |
|---|---|---|---|
| 1 | Validare hardware RS485 (§1) | piese | ~½ zi cu piesele în mână |
| 2 | S0/pulse + intrări digitale în generator (§4, read-only) | 1 | ~1 zi |
| 3 | Profiluri BLE (§2) | 1 | ~1 zi |
| 4 | Schema semnale CAN (§3A, design → review → implementare) | — | 1–2 zile |
| 5 | Nod ESP32 CAN (§3C) | 4, T-CAN485 | 1–2 zile |
| 6 | Driver CAN direct SocketCAN (§3B) | 4, USB-CAN, investigație Docker | 2–3 zile |
| 7 | Profil T-CAN485 verificat + ethernet option (§5) | 5 | ~½ zi |
| 8 | (separat, cu design) scriere spre noduri — relee/comenzi | 2 | de dimensionat |

> Regulile de proces rămân cele de până acum: baseline verde înainte de
> orice etapă, test complet după fiecare, live-ul nu se atinge decât la
> puncte de deploy controlate, totul commitat granular.
