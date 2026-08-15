# Manual de utilizare — Multi-Bus Gateway

[🇬🇧 English](MANUAL.md) | 🇷🇴 **Română**

Ghid pas cu pas pentru tehnician/integrator: de la o instalare curată la un
gateway multi-dispozitiv cu metere virtuale, diagnostice și acces securizat.
Documente însoțitoare:

- **[architecture.md](architecture.md)** — cum funcționează motorul (diagrame, EN).
- **[API.md](API.md)** — referința completă a endpoint-urilor REST cu rolurile necesare (EN).
- **[virtual-meter-spec.md](virtual-meter-spec.md)** — contractul la nivel de
  fir al unui meter virtual (politici de staleness, blocul de calitate la 61440).
- **[device-catalog.md](device-catalog.md)** — fiecare hartă de registre
  inclusă și sursa față de care a fost verificată.
- **[csv-import.md](csv-import.md)** — importul unei hărți de registre din CSV.
- **[alerts-webhooks.md](alerts-webhooks.md)** — alertare de infrastructură +
  praguri pe valori.

## Cuprins
1. [De ce ai nevoie](#1-de-ce-ai-nevoie)
2. [Instalare (Docker)](#2-instalare-docker)
3. [Prima configurare](#3-prima-configurare)
4. [Interfața web, tab cu tab](#4-interfața-web-tab-cu-tab)
5. [Dispozitive & template-uri de dispozitiv (multi-device)](#5-dispozitive--template-uri-de-dispozitiv-multi-device)
6. [Registre, grupuri de poll & praguri](#6-registre-grupuri-de-poll--praguri)
7. [Registre calculate](#7-registre-calculate)
8. [MQTT & Home Assistant](#8-mqtt--home-assistant)
9. [InfluxDB & Grafana](#9-influxdb--grafana)
10. [REST push & feed-ul HTTP/JSON](#10-rest-push--feed-ul-httpjson)
11. [Metere virtuale — pas cu pas](#11-metere-virtuale--pas-cu-pas)
11b. [Device Builder — noduri ESP32 remote (ESPHome)](#11b-device-builder--noduri-esp32-remote-esphome)
12. [Alerte & webhook-uri](#12-alerte--webhook-uri)
13. [Diagnostice](#13-diagnostice)
14. [Scrieri Modbus & lease-uri dead-man](#14-scrieri-modbus--lease-uri-dead-man)
15. [Siguranța configurației: snapshot-uri, rollback, backup](#15-siguranța-configurației-snapshot-uri-rollback-backup)
16. [Securitate](#16-securitate)
17. [Observabilitate: Status, /metrics, evenimente](#17-observabilitate-status-metrics-evenimente)
18. [Limbi & fus orar](#18-limbi--fus-orar)
19. [Depanare](#19-depanare)

---

> Designul defensiv din spatele întregului manual — fiecare fail-safe,
> garanție de livrare și self-heal — e catalogat în
> [reliability.md](reliability.md).

## 1. De ce ai nevoie

- Cel puțin o sursă southbound: un dispozitiv **Modbus TCP** (de ex. un
  Janitza UMG 512-PRO — cu portul 502 activat), un dispozitiv **Modbus RTU**
  pe linie serială (`/dev/ttyUSB0` + adaptor RS-485), un endpoint
  **HTTP/JSON** (Fronius Solar API, Shelly, Tasmota…) sau un broker **MQTT**
  cu topicuri de telemetrie.
- Un host cu **Docker + Docker Compose** (amd64 sau arm64 — merge și pe
  Raspberry Pi).
- *(Opțional)* un broker MQTT pentru Home Assistant și/sau InfluxDB pentru
  Grafana/istoric.

---

## 2. Instalare (Docker)

```bash
# 1) Ia codul
git clone https://github.com/sm26449/multi-bus-gateway.git
cd multi-bus-gateway

# 2) Creează fișierul de environment (opțional — totul se poate configura din UI)
cp .env.example .env

# 3) Pornește stack-ul COMPLET — gateway + broker MQTT (mosquitto) +
#    MQTT Explorer + InfluxDB + Grafana + ESPHome. Tot ce are nevoie
#    produsul e în acest fișier; nimic extern de instalat.
docker compose up -d

# 4) Ia parola de admin generată (doar la primul boot), apoi deschide UI-ul
docker compose logs multi-bus-gateway | grep -A3 'FIRST RUN'
#    http://<host>:8080
```

Din prima pornire pipeline-ul e viu cap-coadă: gateway-ul publică în
broker-ul inclus (host-ul implicit al brokerului e `mosquitto`), **MQTT
Explorer** pe `:4000` arată fiecare topic curgând, iar InfluxDB se
auto-configurează la primul boot (org/bucket `multibus`; schimbă
parola/token-ul în `.env`, apoi activează sink-ul din Config → InfluxDB și
lipește token-ul — Grafana e pe `:3000`). Vrei mai puțin? Pornește doar ce
ai nevoie: `docker compose up -d multi-bus-gateway mosquitto`. Preferi
propriul broker sau InfluxDB mai târziu? Îndrepți gateway-ul spre ele din
UI — cele incluse sunt containere obișnuite pe care le poți opri.

Rulezi deja un stack pe rețeaua partajată? Fișierul de bază își *numește*
rețeaua `pv-stack-network` (suprascriibilă prin `PV_STACK_NETWORK` în
`.env`), deci o instalare proaspătă o creează, iar serviciile viitoare i se
pot alătura. Dacă rețeaua — și broker-ul/InfluxDB — **există deja**,
folosește overlay-ul și pornește doar serviciile gateway-ului:

```bash
docker compose -f docker-compose.yml -f docker-compose.pv-stack.yml \
  up -d multi-bus-gateway
```

Porturi publicate de compose-ul implicit: `8080` (UI/API), `1883` + `9001`
(MQTT inclus + WebSockets), `4000` (MQTT Explorer), `8086` (InfluxDB),
`3000` (Grafana), `1502–1512` (gama meterelor virtuale, extinsă via
`VMETER_PORT_START/END`) și `502` (Modbus standard, pentru consumatorii
care îl cer — scoate-l dacă hostul îl folosește deja). Containerele rulează **non-root** (gateway-ul cu uid
10001; bridge-ul serial cu uid 10002, cu `/dev` read-only și acces doar la
tty-uri), motiv pentru care compose-ul setează
`net.ipv4.ip_unprivileged_port_start=0` — limitat la namespace-ul de rețea
propriu al containerului, ca un proces neprivilegiat să poată face bind pe
`:502`; pe host nu se schimbă nimic. Pentru un dispozitiv RTU fie pornești
bridge-ul serial inclus — `docker compose --profile rtu-bridge up -d`
(recomandat; vezi [rtu-serial.md](rtu-serial.md)) — fie treci adaptorul
direct: `devices: ["/dev/ttyUSB0:/dev/ttyUSB0"]` într-un override de
compose.

Loguri: `docker compose logs -f`.

---

## 3. Prima configurare

> **Sfat:** după prima pornire poți seta totul din UI — conexiunile
> dispozitivelor pe pagina **Devices**, MQTT/InfluxDB sub **Config** — se
> persistă în `config/config.yaml` și se aplică fără restart. `.env` doar
> pre-populează un deploy nou; o valoare dată prin env are întâietate și
> **blochează** câmpul în UI (scoate-o din environment ca să-l poți edita).

Variabilele de mediu opționale:

| Variabilă | Ce este | Exemplu |
|-----------|---------|---------|
| `MODBUS_HOST` / `MODBUS_PORT` / `MODBUS_UNIT_ID` | conexiunea dispozitivului primar | `192.168.1.100` / `502` / `1` |
| `MQTT_ENABLED` / `MQTT_BROKER` / `MQTT_PORT` | sink-ul MQTT | `true` / `mosquitto` / `1883` |
| `MQTT_USERNAME` / `MQTT_PASSWORD` / `MQTT_PREFIX` / `MQTT_PUBLISH_MODE` | detalii MQTT | — |
| `INFLUXDB_ENABLED` / `INFLUXDB_URL` / `INFLUXDB_TOKEN` / `INFLUXDB_ORG` / `INFLUXDB_BUCKET` | sink-ul InfluxDB | — |
| `UI_PORT` | portul UI-ului web | `8080` |
| `UI_HOST` | adresa de bind — implicitul pe bare-metal e loopback (`127.0.0.1`); imaginea de container setează `0.0.0.0` (expunerea e guvernată de maparea de porturi din compose) | `0.0.0.0` |
| `API_KEY` | cere `X-API-Key` la cererile de modificare | — |
| `VMETER_PORT_START` / `VMETER_PORT_END` | gama de porturi a meterelor virtuale | `1502` / `1512` |
| `TZ` | fus orar pentru containerul ESPHome inclus și marcajele de timp | `Europe/Bucharest` |
| `ESPHOME_URL` | unde găsește gateway-ul dashboard-ul ESPHome (Device Builder); pe un deploy nou activează și secțiunea | `http://esphome:6052` |
| `ESPHOME_ENABLED` | forțează Builder-ul pornit/oprit la fiecare start (altfel decide UI-ul) | — |
| `ESPHOME_DASHBOARD_USERNAME` / `ESPHOME_DASHBOARD_PASSWORD` | login pe dashboard-ul ESPHome; aceleași valori configurează și clientul gateway-ului | — |

Punctele de status din bara de sus (Modbus / MQTT / InfluxDB) devin verzi pe
măsură ce fiecare pipeline se conectează — click pe unul pentru detalii.

---

## 4. Interfața web, tab cu tab

Deschide `http://<host>:8080`. Navigarea de sus:

- **Dashboard** — carduri KPI live globale + valorile fixate de tine de pe
  orice dispozitiv. *Customize* alege cardurile; comutare card/tabel;
  culorile implicite ale widgeturilor urmează convenția de faze setată în
  Config → General.
- **Devices** — fiecare sursă southbound ca un card cu sănătatea live;
  wizard-ul **Add Device** și **Discover devices** stau aici. Deschiderea
  unui dispozitiv oferă workspace-ul lui cu tab-uri: *Overview* (rezumat
  read-only + sănătatea datelor), *Edit* (conexiune, template, intervale de
  poll, comutatoare de ieșiri), *Registers*, *Calculated*, *Outputs* și
  *Monitor / History / Energy* per dispozitiv.
- **Virtual Meters** — servește valorile live ca metere Modbus standard
  (vezi §11).
- **Diagnostics** — trusa de punere în funcțiune: bus monitor, register
  probe, discovery, scanare SunSpec (vezi §13).
- **Status** — sănătatea pipeline-urilor, taxonomia erorilor, evenimente,
  alerte, amprenta de resurse (vezi §17).
- **Config** — setări globale: MQTT, InfluxDB, General (fus orar, culori),
  Security, Backup & Snapshots.

---

## 5. Dispozitive & template-uri de dispozitiv (multi-device)

O instalare citește **mai multe surse**. Fiecare dispozitiv împerechează o
**conexiune** cu un **template de dispozitiv** (harta de registre a acelui
tip de echipament) și **rutarea proprie a datelor** (prefix de topic MQTT,
bucket + tag InfluxDB, comutatoare de ieșiri).

### 5.1 Discovery — găsește întâi dispozitivul

Devices → *Discover devices*:

- **Scanare Modbus TCP** — parcurge un CIDR privat (maxim /24) pe un port
  (implicit 502) după dispozitive care răspund. Read-only; restricționat la
  LAN.
- **Sweep de unit-ID** — parcurge ID-urile de unit/slave (implicit 1–32) pe
  un host TCP sau pe o linie serială RTU.
- **Scanare SunSpec** — parcurge lanțul de modele SunSpec (marker-ul `SunS`
  la 40000/50000/0, apoi fiecare model declarat, inclusiv blocul de
  identitate). Primul pas natural pentru hardware Fronius/SolarEdge/Huawei.
- **Scanare ESPHome** — parcurge LAN-ul pe portul API-ului nativ (6053) și
  cere identitatea fiecărui nod (nume, versiune; nodurile cu API criptat sunt
  totuși detectate). Funcționează din Docker — scanarea e unicast, deci nu
  depinde de mDNS/multicast, care nu trece de bridge-ul Docker. Nodurile care
  își anunță pachetul de adopție pot fi **importate direct** în Device
  Builder (devin noduri administrate: build/OTA de aici).
- **Răsfoire topicuri MQTT** — se conectează la un broker și arată topicurile
  care vorbesc, cu preview de payload (topicurile retained apar instant; o
  fereastră scurtă de ascultare prinde publisher-ii live), ca să alegi un
  topic în loc să-l tastezi.
- **Fronius Solar API discover** — enumeră invertoarele/meterele din spatele
  unui DataManager, cu indicația că meterul stă la unitatea Modbus 240.

*Use* pe orice rezultat pre-completează wizard-ul Add Device.

### 5.2 Adaugă un dispozitiv (wizard)

1. **Conexiune** — alege protocolul:
   - **Modbus TCP**: host, port, unit ID, timeout.
   - **Modbus RTU**: două moduri (vezi [rtu-serial.md](rtu-serial.md)).
     **Prin rețea (recomandat)** — apasă **Scan** și alege un adaptor USB de
     pe serial bridge (îl bagi în priză → apare; îl scoți → dispare), MBG
     rămâne neprivilegiat. **Un singur master per linie bridged** este
     impus: un al doilea dispozitiv pe același endpoint de bridge e respins
     la validare (doi masteri s-ar evacua reciproc la nesfârșit), iar
     Test-connection refuză un endpoint pe care un dispozitiv pornit face
     deja poll. **Serial direct** — port serial (de ex.
     `/dev/ttyUSB0`), baud, paritate, biți de stop, cu adaptorul mapat în
     container.
   - **HTTP/JSON**: un URL care întoarce JSON; fiecare registru își extrage
     valoarea cu un `json_path` (de ex. `Body.Data.PowerReal_P_Sum`).
     URL-urile trebuie să indice un host din LAN-ul privat, cu excepția
     cazului în care setezi `security.allow_nonlan_http_devices` (gardă
     SSRF).
   - **MQTT**: broker, port, credențiale/TLS, un topic de subscribe; fiecare
     registru citește din payload-ul JSON prin `json_path` sau ia payload-ul
     brut ca număr, și poate avea propriul topic cu wildcard-uri `+`/`#`.

   Apasă **Test connection**: la Modbus, *orice* răspuns la nivel de
   protocol — chiar și o excepție — dovedește un dispozitiv viu; la HTTP cu
   un template ales, testul raportează câte `json_path`-uri s-au rezolvat;
   la MQTT se conectează și așteaptă scurt un mesaj de probă.
2. **Template** — alege din bibliotecă (11 hărți incluse, vezi
   [device-catalog.md](device-catalog.md)), **încarcă** un template `.json`
   (validat rând cu rând; conflictele de id întreabă înainte de suprascriere),
   **creează** unul în editor sau **importă un CSV sau YAML** cu harta de
   registre ([csv-import.md](csv-import.md),
   [yaml-import.md](yaml-import.md)). Built-in-urile sunt read-only —
   *Duplicate to edit*. Un template folosit de un dispozitiv nu poate fi
   șters.
3. **Rutarea datelor** — **id-ul** dispozitivului devine cheia de rutare:
   valorile se publică sub prefixul lui de topic MQTT (preview live) și
   ajung în bucket-ul lui InfluxDB (creat automat, retenție 90 de zile),
   etichetate cu tag-ul lui. **Capcană:** identitatea de rutare e fixă după
   creare — schimbarea ei ar orfaniza istoricul și entitățile Home
   Assistant.

După creare, dispozitivul începe să citească imediat: template-urile curate
își auto-selectează subsetul recomandat de registre (harta Janitza selectează
58 din 4.126); un template fără defaults și cu peste 300 de registre nu
selectează nimic, ca să nu inunde MQTT/InfluxDB cu mii de serii dintr-un
click.

**Instalările existente:** meterul original apare automat ca dispozitivul #1
cu identitatea de rutare blocată — topicurile, bucket-ul, tag-urile și
entitățile HA rămân byte-identice.

**Ștergerea unui dispozitiv** păstrează fișierul lui de registre pe disc
(siguranța datelor) și e blocată cât timp un meter virtual îl folosește ca
sursă.

---

## 6. Registre, grupuri de poll & praguri

Workspace-ul dispozitivului → **Registers**:

- **Available** — catalogul din template-ul dispozitivului (răsfoire/căutare;
  4.126 de intrări pentru Janitza). Poți adăuga manual un **registru
  custom** dacă lipsește din hartă.
- **Selected** — ce se citește efectiv. Per registru: grup de poll, tip de
  date, scale (valoare inginerească = raw ÷ scale), tip de registru
  (holding FC3 / input FC4 / coil FC1 / discrete FC2), topic MQTT,
  measurement + tags InfluxDB, widget de dashboard și **praguri**.

**Grupurile de poll** (`realtime` / `normal` / `slow` implicit) citesc
fiecare la intervalul propriu — editabil per dispozitiv între 0,05 s și
24 h, aplicat live. Registrele dintr-un grup se unesc în citiri batch care
sar peste goluri de până la `max_gap` (implicit 10); slave-urile stricte
care refuză blocurile unite cu *illegal data address* trebuie setate cu
`max_gap: 0`.

**Pragurile** colorează o valoare (warningLow/High, dangerLow/High) pe
dashboard și pe gauge-uri. Sunt per registru — iar aceleași limite pot și
declanșa **alerte** la traversare: activează motorul de praguri încorporat
(`alerts.signals.threshold`, vezi §12).

Salvarea selecției re-încarcă hot doar pollerele acelui dispozitiv.

### 6.1 Nume canonice de câmpuri

Ca automatizările, dashboard-urile și predicțiile să fie predictibile, **numele**
unui registru vine dintr-un dicționar comun — aceeași mărime fizică e denumită la
fel pe orice dispozitiv (`voltage_l1_n` peste tot, nu `ull_0` pe un meter și
`v_l1` pe altul). Numele devine leaf-ul topicului MQTT, field-ul InfluxDB și
tag-ul `name`; topicul MQTT e ierarhic (`voltage/l1_n`), iar field-ul InfluxDB e
numele canonic plat. Lista completă e în
[`docs/canonical-fields.md`](canonical-fields.md) (56 câmpuri). Convenție:
`<mărime>_<poziție>` — `voltage_l1_n`, `current_l2`, `power_active_total`,
`energy_active_import`, `frequency`, …

- **În editorul de registre**, câmpul *Name* autocompletează din dicționar,
  marchează un nume non-canonic cu „did you mean …?" și pre-completează topicul
  MQTT + măsurătoarea InfluxDB când numele e canonic.
- **În editorul de template** (Templates → *New map* / *Edit*), fiecare rând e
  verificat live, se afișează un sumar „**N/M canonice**", iar **Auto-canonicalize**
  deduce numele canonice pentru o hartă criptică importată din label/unit-ul
  fiecărui rând. E deliberat conservator — orice caz incert rămâne amber ca
  să-l setezi tu, niciodată redenumit greșit. Verifici grila, apoi Salvezi.
- Un template promite nume canonice cu `"canonical": true`; numele non-canonice
  sau duplicate apar apoi ca avertisment în managerul de template-uri.

**Unitatea face parte din contract.** Fiecare câmp canonic poartă o unitate
canonică (vezi coloana Unit din
[`canonical-fields.md`](canonical-fields.md)); energia e în familia de bază
**Wh** (Wh/varh/VAh). Un meter a cărui hartă nativă e în kWh convertește în
**scale**-ul selecției (`scale/1000`) — valoarea pe care o publică trebuie
să fie deja în unitatea canonică, altfel fiecare dashboard cross-device,
binding de vmeter și geamăn de failover moștenește o eroare ×1000 tăcută.
Două gărzi impun asta:

- **În editorul de template**, celula *Unit* arată unitatea canonică ca
  tooltip pentru numele canonice și devine **amber live** când unitatea
  tastată diferă, cu remedierea spusă explicit (ajustezi scale-ul sau
  redenumești rândul).
- **La salvare**, selectarea unui nume canonic cu o unitate nepotrivită
  loghează un warning (salvarea trece totuși — warning-ul e firul de
  alarmă).

Hărțile de referință vendor (ex. Janitza UMG512) preced această schemă și-și
păstrează numele native — schema se aplică template-urilor pe care le creezi/imporți.

### 6.2 Igienă de polling & decodare (opt-in)

Totul aici e **oprit implicit** — activezi global, per dispozitiv sau per
registru, când hardware-ul o cere.

- **Jitter la pornire** — `polling.startup_jitter_s` (implicit global) sau
  `startup_jitter_s` sub `connection:`-ul unui dispozitiv. Fiecare grup de
  poll așteaptă o întârziere aleatoare în `[0, min(interval, jitter)]`
  înaintea **primei** citiri, ca mai multe dispozitive/grupuri să nu tragă în
  pas de defilare și să lovească un transport partajat (mai ales un serial
  bridge RTU-prin-rețea) la boot. `0` = oprit.
- **Lista de registre ilegale** — `illegal_registers` sub `connection:`-ul
  unui dispozitiv: adrese la care slave-ul răspunde cu *illegal data address*
  (excepția 02). Citirile batch unite nu mai trec niciodată peste una, iar un
  registru selectat care stă pe una e sărit — soluția pentru o gaură *în
  interiorul* unei serii contigue, pe care `max_gap: 0` n-o poate rezolva.
  Zecimal sau `0x…`.
- **Poarta anti-cadru-tot-zero** — `drop_all_zero: true` pe un dispozitiv: un
  dispozitiv adormit (un invertor noaptea) poate răspunde cu un cadru COMPLET
  ZERO în loc de eroare; publicat ca atare, arată ca date reale de 0 V / 0 W.
  Cu poarta pornită, un grup de poll cu toate valorile numerice exact zero
  (și cel puțin 2 la număr) e aruncat, iar cache-ul păstrează ultimele valori
  bune până se trezește dispozitivul.

```yaml
polling:
  startup_jitter_s: 2          # implicit global pentru toate dispozitivele
devices:
  - id: inverter
    connection:
      host: 192.168.1.60
      startup_jitter_s: 5      # override per dispozitiv
      illegal_registers: [0x2100, 505]
      drop_all_zero: true      # doarme noaptea
```

- **Opțiuni de decodare per registru** (editorul de registre/template sau
  JSON-ul de template):

| Cheie | Efect |
|---|---|
| `offset` | valoare inginerească = `raw / scale + offset` — deplasări de punct zero / unitate (ex. Kelvin×10 → °C cu `scale: 10, offset: -273.15`); sărit la rândurile enum/bitfield |
| `nan` | santinelă not-available: `true` = valoarea SunSpec a tipului de date (`0x8000`/`0xFFFF`…), sau o valoare brută / listă explicită. O potrivire se citește ca *lipsă*, niciodată un număr-gunoi (−32768 °C). Float NaN/Inf e mereu aruncat |
| `monotonic: true` | contoare cumulative (Wh/kWh/varh): un pas neplauzibil în **oricare direcție** e aruncat (cache-ul ține ultima valoare bună) — un glitch descendent, dar și un salt ascendent cu >50% peste baseline (un high word corupt ar injecta altfel GWh fantomă). Un reset real, susținut, e adoptat doar după citiri de confirmare coerente (la sub 5% una de alta) și loghează un WARNING cu noul baseline |
| `enum` / `bits` (+ `mask`/`shift`) | decodează un cuvânt de stare brut în text — `enum: {7: "Fault"}` (nemapat → `unknown (n)`), `bits: {0: "overvoltage"}` unește numele biților setați; se construiește vizual din butonul **States** din editorul de template. Hărțile incluse îl folosesc și pentru registrele de identitate (ex. codul de detecție EM24 1651, model id-ul Fronius 65A 731) |

Aceste opțiuni se aplică **identic pe orice transport** — sursele Modbus,
HTTP/JSON și MQTT-in rulează toate același pipeline fir→valoare (santinelă
→ enum/bits → scale+offset → monotonic), deci un contor de energie
alimentat prin MQTT (Shelly, Zigbee2MQTT…) primește aceeași protecție la
rollover ca unul Modbus. Când o valoare e aruncată, motivul e una din trei
etape vizibile: `sentinel` (declarat not-available), `decode_failed` (stare
nemapabilă, logată o singură dată pe tranziție) sau `filter_drop`
(respingere monotonic, logată la debug).

---

## 7. Registre calculate

Workspace-ul dispozitivului → **Calculated**: derivă măsurători noi prin
formulă din valorile live. Exemple:

| Scop | Expresie |
|---|---|
| Factor de putere | `_G_P_SUM3 / _G_S_SUM3` |
| Sumă pe faze | `p_l1 + p_l2 + p_l3` |
| Conversie de unități | `energy_wh / 1000` |
| Dezechilibru de curent % | `(max(i1,i2,i3) - avg(i1,i2,i3)) / avg(i1,i2,i3) * 100` |
| Putere medie dintr-un contor de energie | `(E - prev(E)) / dt * 3600` |
| Între dispozitive | `grid.p_total + pv.p_total` |

- Builder-ul oferă chip-uri clicabile cu măsurătorile, o paletă de
  funcții/operatori (`min max avg abs round sqrt pow floor ceil clamp`,
  `pi e`, comparații, `a if cond else b`), **preview live** și preset-uri —
  îți poți salva propriile formule ca preset-uri reutilizabile.
- `prev(x)` și `dt` fac posibile calculele de rată; o formulă cu stare nu
  poate fi previzualizată one-shot (UI-ul o spune) — produce valori de la a
  doua evaluare încolo.
- Expresiile sunt validate pe o listă albă sigură înainte de salvare; la
  rulare, un input lipsă sare peste runda respectivă (fără valori parțiale
  sau fabricate), iar o eroare nu poate omorî pollerul.
- O valoare calculată curge către **toate** ieșirile (MQTT, InfluxDB,
  metere virtuale, feed-uri) și apare în Monitor/History ca orice
  măsurătoare. Grupul ei de poll decide cât de des se recalculează.

---

## 8. MQTT & Home Assistant

Config → **MQTT**: broker, port, credențiale, QoS, retain și TLS
(port 8883; certificat CA pentru verificarea brokerului, opțional certificat
client + cheie pentru TLS mutual — pune fișierele sub `config/` și
referențiază căile din container; „skip verification" e doar pentru teste).

- **Topicuri**: `<prefixul dispozitivului>/<topicul registrului sau numele
  derivat>`. Dispozitivul #1 își păstrează prefixul istoric; dispozitivele
  noi primesc implicit `mqtt.default_topic_pattern` (`meters/{device}`).
- **Mod de publicare**: `changed` (implicit — publică doar valorile care
  s-au schimbat; cache-ul de schimbări se confirmă doar după o publicare
  reușită, deci o pană de broker nu pierde nimic) sau `all` (fiecare
  citire).
- **Heartbeat** — `mqtt.heartbeat_interval` (secunde, `0` = oprit, implicit):
  în modul `changed`, forțează republicarea unei valori *neschimbate* după N
  secunde, ca o citire stabilă să-și păstreze un timestamp proaspăt și Home
  Assistant să nu îngrizeze entitatea în stările staționare lungi.
- **Disponibilitate**: un Last-Will marchează `<prefix>/status` = `offline`
  dacă gateway-ul moare; `online` e publicat retained la conectare.
  **Fiecare dispozitiv publică un topic de disponibilitate retained,
  inclusiv primarul**, iar cache-ul de disponibilitate e golit la
  reconectare, ca să fie mereu re-afirmat — un consumator nu trebuie să
  ghicească niciodată din tăcere. Se confirmă doar după o publicare
  reușită, deci un sughiț de broker nu poate lăsa în urmă un `online`
  stale.
- **Comenzile retained nu sunt niciodată executate** — un mesaj MQTT
  retained rămas pe un topic de *comandă* (un `mosquitto_pub -r` rătăcit,
  un `retain: true` din HA) ar re-acționa altfel hardware-ul la **fiecare**
  reconectare. Livrările retained pe topicurile de comandă sunt aruncate,
  iar copia retained e ștearsă la subscribe.
- **Home Assistant discovery**: activat implicit. Fiecare dispozitiv devine
  un device HA cu registrele selectate ca senzori (`unique_id`
  `mbg_dev_<device>_<addr>_<name>` pentru dispozitivele non-primare), cu
  device/state class deduse din unități, plus un **`binary_sensor` de
  conectivitate** per dispozitiv (disponibilitate). Registrele marcate
  writable în template devin entități HA **`number`/`select`** (limitele
  din template, dublu-păzite de `mqtt.allow_write_entities` +
  `security.allow_writes` — vezi §14); discovery-ul write-aware e
  înregistrat la boot, deci controlul supraviețuiește unui restart.
  Meterele virtuale publică propriile
  entități de diagnostic (stare de servire, rată de cereri, erori,
  prospețime…). Ștergerea unui dispozitiv curăță discovery-ul retained, deci
  HA renunță la entități.
- **Migrări de prefix de topic** — `mqtt.compat_aliases` publică dual
  numele vechi de topicuri alături de cele noi, ca consumatorii să poată fi
  mutați fără gol (vezi [config-reference.md](config-reference.md)); scoate
  alias-urile odată ce fiecare consumator s-a mutat.

**Capcană:** cu `retain: true` (implicit) un consumator care se abonează
târziu vede totuși ultima valoare — dar după un restart de broker fără
persistență, valorile reapar doar pe măsură ce se publică din nou;
gateway-ul își golește cache-ul de schimbări la fiecare reconectare și
republică starea completă exact din acest motiv.

---

## 9. InfluxDB & Grafana

Config → **InfluxDB**: URL, token, org, bucket. Bucket-urile per dispozitiv
se creează automat cu retenție de 90 de zile; measurement/tags per registru
se setează în tab-ul Registers. Îndreaptă Grafana spre același bucket.
Profilurile opționale de compose pornesc un InfluxDB + Grafana local
(`--profile influxdb --profile grafana`).

**Garanții asupra datelor.** Fiecare punct e ștampilat cu ora *citirii*, nu
ora scrierii. Dacă InfluxDB devine inaccesibil, punctele intră într-un
**buffer store-and-forward** (implicit **120 de minute / 200.000 de
puncte**, ~40 MB mărginit — reglabil prin `influxdb.buffer_minutes` /
`buffer_max_points`) și sunt replay-ate cu timestamp-urile originale la
reconectare, idempotent. Fereastra e **relativă la date** (măsurată de la
cel mai nou punct din buffer, nu de la ceasul de perete), deci un restart
întârziat nu aruncă niciodată un snapshot valid. Cu
`influxdb.buffer_persist: true` (implicit) buffer-ul supraviețuiește și unui
restart în timpul penei (`config/influx_buffer.jsonl`). Batch-urile la care
clientul renunță după cele ~5 min de retry intern sunt recuperate în același
buffer. **Eșecurile de autentificare sunt detectate explicit**: un
401/403/404 (token rotit, bucket șters) setează un flag
`influx_auth_failed`, ridică o alertă de operator și re-bufferizează în loc
să arunce — doar punctele cu adevărat malformate (400/422) sunt vreodată
aruncate, iar `writes_confirmed` / `last_confirm_age_s` dovedesc că datele
chiar aterizează (vechiul mod de eșec era `connected: true` cu 100 %
pierdere). Penele mai lungi decât fereastra pierd punctele cele mai vechi;
pentru tensiunile Janitza, înregistrarea internă a meterului le poate
recupera prin `python -m multibus.backfill` — backfill-ul își derivă schema
punctelor (tags, fields, measurement) **din selecția live de registre**,
deci punctele reparate aterizează în serii byte-identice, iar o adresă
deselectată e sărită în loc să fie scrisă cu o schemă ghicită. Urmărește
`buffer_points` / `replayed_total` / `dropped_total` în `/api/status` sau
`gateway_influx_buffer_points` în `/metrics`.

MQTT **nu** este replay-at, intenționat: e un bus live — la reconectare se
republică starea curentă.

**History** per dispozitiv (linii agregate cu bandă min/max) și **Energy**
(totaluri lunare din contoarele cumulative + defalcare zilnică, în fusul
orar din §18; selecția contoarelor din tab-ul Energy e per dispozitiv)
citesc aceste date înapoi.

---

## 10. REST push & feed-ul HTTP/JSON

Workspace-ul dispozitivului → **Outputs** — două sink-uri suplimentare per
dispozitiv, ambele opt-in:

- **HTTP/JSON output** — servește valorile live ale dispozitivului ca JSON
  read-only la `GET /api/meters/<id>` (stil Solar API, indexat pe numele
  registrelor, cu un flag `stale`). Gateway-ul nu împinge nimic; orice
  client HTTP trage datele. Notă: cu login activat, feed-ul cere o sesiune —
  ține cont în designul accesului.
- **REST push** — POST-ează valorile dispozitivului ca JSON către un URL
  extern la interval (minim 5 s): `{enabled, url, interval_s, headers,
  format, verify_tls, timeout}`. `format: native` trimite
  `{nume: {value, unit, ts}}`; `flat` trimite `{nume: valoare}`.
  Autentificarea merge în `headers` (stocate mascat, păstrate la salvare).
  Țintele *pot* fi externe (spre deosebire de inputurile HTTP de
  dispozitiv), dar redirecturile sunt refuzate ca să nu-ți fie rejucate
  credențialele în altă parte. Livrarea e fire-and-forget; cardul arată
  ultimul status, iar **Test** împinge o dată imediat.

---

## 11. Metere virtuale — pas cu pas

Scop: alt sistem (Victron ESS, un invertor Fronius, orice client SunSpec) să
citească acest gateway ca meterul pe care *el* îl așteaptă.

> ⚠️ Un meter virtual poate alimenta o buclă de control. Fă pașii 11.1→11.3
> (validare în paralel) înainte să-l faci vreodată singurul meter al unui
> consumator.

**11.0 — Publică porturile (o dată).** Compose-ul publică `1502–1512` (plus
`502`). Alege porturi de instanță din gamă; lărgește-o prin
`VMETER_PORT_START/END` + recreează containerul dacă ai nevoie de mai multe.

**11.1 — Alege sau creează un template.** Virtual Meters → **Templates**.
Incluse: `em24_av53` (Carlo Gavazzi EM24 → Victron), `fronius_ts_native`
(Fronius Smart Meter TS → DataManager), `fronius_sunspec_meter` (SunSpec 213
float). Un rând de template = adresă, tip, scale, ordine de cuvinte și o
**sursă**: un registru live (`nume`, sau `dispozitiv.registru` pentru alt
dispozitiv), o constantă sau un `sum` de mai multe surse. Import/export ca
YAML.

**11.2 — Adaugă o instanță.** Pe tab-ul **Meters**: template, un port liber,
unit id, dispozitivul sursă, politica de staleness → **Add instance**.
Pornește **dezactivată**.

**11.3 — Validează în paralel.** Activeaz-o și îndreaptă un consumator de
*test* — sau doar urmărește tab-ul **Logs** — spre `host:port`. Jurnalul de
query-uri arată exact ce citește consumatorul, când, și ce i s-a răspuns
(ultimele 1024 de cereri). Compară cu meterul real. **Stats** arată rata,
erorile, registrele cele mai citite; **Decode** interpretează orice interval
de adrese înapoi la variabilele-sursă.

**11.4 — Alege politica de staleness** (`on_stale`) — ce servește un rând
stale/lipsă:

| Politică | Comportament | Folosire |
|---|---|---|
| `legacy` (implicit) | comportamentul clasic single-source: un singur watchdog de prospețime per instanță; golurile păstrează ultimele cuvinte | meterele existente — neatinse |
| `fail` | orice citire care atinge un rând stale → **excepție Modbus** (fără adevăr parțial) | consumatori de control (Victron, PLC) |
| `sentinel` | N/A SunSpec: float→NaN, int16→0x8000, uint16→0xFFFF… | consumatori care înțeleg santinelele |
| `hold` | ultima valoare până la `max_hold_s` (implicit 30 s), apoi ca `fail` | display-uri tolerante |

Absența nu se servește **niciodată** ca 0/false. Sumele preiau calitatea
celui mai slab membru. În modurile cu politică, serverul rămâne pornit cât
timp cel puțin o sursă e proaspătă; toate moarte → nu mai răspunde, ca
fail-safe-ul de pierdere-de-meter al consumatorului să se activeze.

Două rafinări per rând:

- **Un rând a cărui sursă nu s-a rezolvat niciodată** (registru redenumit,
  deselectare, un typo în template) pică din start verdictul de prospețime
  și ridică un eveniment `unresolved` — distinct de *stale*. Înaintea
  acestei reguli, blocul inițializat cu zero ar fi servit un 0 W plauzibil
  în timp ce meterul părea sănătos; acum meterul refuză zgomotos să
  servească până când sursa se rezolvă o dată.
- **Limită de prospețime per rând** — un registru își poate purta propriul
  `stale_after_s`, care suprascrie limita instanței, ca un senzor BLE de
  60 de secunde să poată împărți un meter cu rânduri de grid la 250 ms fără
  staleness fals; limita efectivă derivă automat și din cadența
  poll-group-ului care produce valoarea.

**11.5 — Opțional: blocul de calitate in-band.** Dacă consumatorul
(PLC/SCADA) trebuie să știe calitatea datelor pe aceeași conexiune Modbus,
activează *In-band quality block* pe instanță. Servește un bloc read-only la
**61440** (identic pentru orice meter):

| Adresă | Tip | Semnificație |
|-------:|-----|--------------|
| 61440 | u16 | versiunea formatului (1) |
| 61441 | u16 | 0 legacy · 1 ok · 2 degradat · 3 stale |
| 61442–61444 | u16 | rânduri proaspete / stale / lipsă |
| 61445 | u16 | total rânduri de date |
| 61446 | u32 | vârsta celei mai noi valori proaspete (s); 0xFFFFFFFF = niciodată |

Garda tipică în consumator: *folosește datele doar cât timp 61441 == 1;
alarmă pe 3*. Specificația completă:
[virtual-meter-spec.md](virtual-meter-spec.md).

**11.6 — Cut-over.** Îndreaptă consumatorul real spre meterul virtual.
Watchdog-ul de prospețime e plasa de siguranță. Aceeași hartă e servită și
ca JSON la `/api/virtual-meters/<id>/values` sub convenția de agregator
(`value: null` + `quality` + `age_s`, `last_value` separat) — un SCADA
citește totul într-un singur poll.

**11.7 — Surse redundante (failover).** Un meter care alimentează o buclă de
control n-ar trebui să se oprească la căderea unei singure surse. Două
straturi, același motor:

- **Per registru** — în editorul de template, dropdown-ul de tip de sursă
  oferă **Failover (live…)**: o listă de candidați separați prin virgulă, în
  ordinea priorității (`dispozitiv.registru` pentru o sursă de pe alt
  dispozitiv). YAML:
  `source: { failover: ["_G_P_SUM3", "fronius.power_active_total"] }`
  (`combined` e un alias acceptat). Fiecare rebuild servește **primul
  candidat proaspăt**, sare peste cei lipsă/stale în ordine și revine la
  primar în clipa în care e din nou proaspăt.
- **Per instanță** — dropdown-ul *Secondary source device (failover)* din
  modalul Add/Edit instance (`device_fallback:` pe instanță) numește un
  **dispozitiv geamăn**. La pornire, fiecare registru live cu nume simplu e
  rescris în perechea `[nume, <geamăn>.nume]` — fără editări de template,
  pentru că numele canonice de câmpuri sunt identice între dispozitive.
  Rândurile const, sum, deja-failover și cele cu `dispozitiv.registru`
  explicit rămân cum au fost scrise. **Contoarele cumulative de energie
  sunt excluse deliberat**: geamănul e un alt meter fizic, deci totalul lui
  pe viață ar fi un salt non-monotonic care corupe statisticile kWh din
  aval (un Fronius DataManager tratează un contor care merge înapoi ca pe
  un fault). Rândurile de contor rămân pe primar și, la o pană, **îngheață
  la ultima valoare bună** în orice politică — un contor înghețat e o
  afirmație adevărată („energia livrată până acum") — în timp ce rândurile
  instantanee fac failover; când primarul revine, contorul își reia mersul
  cu un salt înainte legitim, ca după orice power-cycle de meter.
  Fallback-ul e validat la salvare
  (dispozitiv cunoscut, diferit de sursă) și re-verificat la pornire (o
  valoare greșită e ignorată cu un warning — nu blochează niciodată meterul).
  Un geamăn configurat-dar-offline se armează și intră în joc în clipa în
  care publică.

**Interacțiunea cu staleness:** failover-ul servește doar un candidat
*proaspăt*; dacă niciunul nu e proaspăt, rândul degradează prin politica
`on_stale` a instanței exact ca o singură sursă stale (sub `fail` citirea e
refuzată). **Evenimente de comutare:** fiecare schimbare a sursei servite
ajunge în jurnalul de evenimente și în logul de proces — `warn` la căderea pe
o sursă cu prioritate mai mică, `info` la recuperare — iar cardul meterului
arată `✓ on primary` / `⇢ n/m on fallback`.

**Starea publicată.** Starea MQTT retained a fiecărui meter
(`<prefix mqtt>/vmeter/<id>/state`) poartă politica de staleness și limitele
ei (`on_stale`, `stale_after_s`, `max_hold_s`), contoarele de calitate ale
ultimului rebuild (fresh/stale/missing) și rutarea failover live (per
registru: candidații, sursa activă, `on_primary`) — un monitor extern vede
*cum* degradează meterul și ce sursă îl alimentează, nu doar că a devenit
stale.

**Capcane:** meterul răspunde pe orice unit id (cel configurat e
informativ); citirile în afara hărții emulate răspund *illegal data
address* prin design (consumatorii identifică meterele sondând adresele
joase); ștergerea unui dispozitiv sursă e blocată cât timp un meter îl
folosește.

---

## 11b. Device Builder — noduri ESP32 remote (ESPHome)

Ai un contor pe RS485 într-o altă clădire sau la alt site, fără cablu de
rețea până la el? Secțiunea **Builder** îți construiește firmware pentru un
nod ESP32/ESP8266 care citește contorul prin Modbus RTU și publică valorile
prin MQTT înapoi în gateway — totul din interfață, fără toolchain instalat.

Compilarea o face un container **ESPHome** standard, pe hardware-ul tău;
gateway-ul îl comandă prin API, deci nu are nevoie de volume partajate sau
dependențe noi. Nimic nu iese din rețeaua ta.

**Pornire: zero configurare.** `docker compose up -d` pornește și serviciul
ESPHome inclus, iar gateway-ul se leagă singur de el (`ESPHOME_URL` e
pre-completat în compose). Cardul **Device Builder** te așteaptă în pagina
**Devices**, sub lista de dispozitive, cu bannerul verde și versiunea ESPHome
deja afișate; butonul **Deploy new device** din toolbar te duce direct în
wizard. Dashboard-ul ESPHome nu e expus pe LAN —
totul trece printr-o singură interfață, cu un singur login și un singur
audit trail. (Ai deja un ESPHome în altă parte? Schimbă URL-ul din
Devices → Device Builder → ⚙, sau setează `ESPHOME_URL` în `.env`. Vrei și login pe
dashboard-ul ESPHome? `ESPHOME_DASHBOARD_USERNAME/PASSWORD` în `.env` le
setează pe ambele capete deodată.)

**Fluxul complet, de la template la date live:**

1. **Generate from template** — alegi un template Modbus (ex. Eastron
   SDM630), subsetul de registre, profilul hardware (placă + pini UART/DE-RE)
   și adresa Modbus a contorului. Broker-ul MQTT se moștenește automat de la
   gateway.
2. **Preview** — primești YAML-ul complet (uart/modbus/modbus_controller cu
   tipurile de date, ordinea de octeți și scalarea corecte; valorile pleacă
   în unități inginerești pe topicuri explicite per registru).
3. **Save + Adopt as device** — un singur click salvează firmware-ul pe
   dashboard-ul ESPHome, completează cheile lipsă din `secrets.yaml`
   (placeholder `CHANGE_ME` pentru Wi-Fi/OTA) **și creează automat perechea
   din gateway**: un template MQTT + un dispozitiv mqtt-in cu exact aceleași
   topicuri. Zero configurare dublă.
4. Completezi `secrets.yaml` (butonul de editare acceptă și secrets.yaml),
   apoi **Build** — loguri live în consolă.
5. **Primul flash: prin USB, direct din browser** (butonul *USB*; necesită
   Chrome/Edge și HTTPS sau localhost — esp-web-tools e servit local, fără
   cloud). Același dialog configurează Wi-Fi prin cablu (Improv). Ulterior:
   **Flash OTA** din aceeași pagină.
6. Nodul pornește, publică, iar dispozitivul pereche din gateway prinde
   valorile automat — le vezi în Dashboard/Monitor, cu staleness LWT inclus.

**Ce acoperă generatorul azi (și ce nu, încă):**

| Interfață nod | Stare | Note |
|---|---|---|
| **RS485 / Modbus RTU** | ✅ complet | uart + modbus + modbus_controller din orice template Modbus; validat de ESPHome („Configuration is valid!") |
| **MQTT northbound** | ✅ complet | topicuri explicite per registru + LWT; perechea mqtt-in se creează la Adopt |
| **BLE (senzori)** | ⚠️ parțial | gateway-ul consumă BLE prin MQTT (template `ble_theengs_sensor`); generatorul nu emite încă profiluri `esp32_ble_tracker` |
| **CAN bus** | ⏳ planificat | ESPHome are `canbus` (TWAI intern ESP32 / MCP2515), dar CAN e orientat pe frame-uri+semnale, nu pe registre — cere o extensie de schemă de template (id frame, biți, scalare), în design |

Un YAML importat manual poate folosi ORICE componentă ESPHome (inclusiv
canbus/BLE) încă de azi — limitele de mai sus privesc doar **generatorul**
automat din template-uri. Extinderea generatorului (BLE, CAN pe ambele căi,
noduri cu I/O, plăci integrate gen LilyGO T-CAN485) este pe foaia de parcurs
a proiectului.

**De reținut:**

- Fără auth activat, totul e deschis (LAN de încredere, ca restul
  aplicației); cu auth, YAML-ul nodurilor, build-urile și flash-ul sunt
  **doar admin**, iar totul intră în audit log.
- **Update all** recompilează și actualizează OTA toate nodurile cu firmware
  vechi (după un upgrade de ESPHome, de exemplu).
- Profilurile hardware (pini/placă) se salvează și se refolosesc între
  noduri; două profile generice sunt incluse.
- Ștergerea unui nod îl **arhivează** pe dashboard-ul ESPHome — nimic nu se
  pierde definitiv.

## 12. Alerte & webhook-uri

Două familii de alerte pe o singură cale de livrare (MQTT + webhook):

- **Sănătatea infrastructurii** — un dispozitiv sau un sink pică, latența de
  citire rămâne mare, buffer-ul InfluxDB crește.
- **Praguri pe valori** — limitele warning/danger per registru din §6 devin
  evenimente de alertă printr-un motor cu histerezis încorporat (**oprit
  implicit**, `signals.threshold`): cinci benzi, declanșează doar la
  tranziții de bandă, *rapid la alarmare, lent la revenire* (deadband
  `threshold_deadband_pct`, implicit 2 %), suprimat pe date stale ca o
  pierdere de comunicație să nu poată declanșa o traversare-fantomă.

Se configurează din Config → Alerts sau blocul `alerts:`:

```yaml
alerts:
  enabled: true
  mqtt: true                                   # publică pe <topic_prefix>/alert
  webhook_url: "https://ntfy.example/gateway"  # gol = webhook oprit
  webhook_headers: { "X-API-Key": "secret" }
  webhook_body: { "message": "[{severity}] {source}: {message}" }
  min_interval_s: 300
  latency_ms: 1000
  buffer_points: 1000
  signals: { device: true, sink: true, latency: true, buffer: true,
             threshold: true }                 # threshold e implicit false
  threshold_deadband_pct: 2.0
  threshold_alert_on_start: true
```

Alertele sunt limitate per cheie (`min_interval_s`), oglindite în jurnalul
de evenimente și pe pagina Status. Gateway-ul *detectează și livrează*;
deduplicarea, rutarea și distribuția pe canale (Telegram/SMS/e-mail) țin de
receiverul tău de webhook / sistemul tău de notificări. Butonul **Test** (sau
`POST /api/alerts/test`) declanșează o alertă sintetică prin canalele reale
— cere login sau cheie API și are cooldown, pentru că generează trafic real.
Livrarea pe webhook e best-effort (fără retry) și refuză redirecturile.
Forma payload-ului și motorul de praguri în detaliu:
[alerts-webhooks.md](alerts-webhooks.md).

---

## 13. Diagnostice

Trusa de punere în funcțiune (pagina Diagnostics). Totul aici e read-only pe
bus.

- **Bus monitor** — captură la nivel de cadru a fiecărei tranzacții Modbus:
  hex TX/RX, funcție/adresă/count decodate, rezultat
  (ok / excepție / fără răspuns / eroare CRC), latență — și **fiecare retry
  ca intrare separată**, ca să vezi exact ce face o legătură instabilă. Ring
  buffer doar în RAM (implicit 1000, până la 20000 de cadre): pornește
  dezactivat și nu persistă niciodată, deci o sesiune uitată nu poate crește
  în memoria unei cutii pornite de un an.
- **Register probe** — citire one-shot a oricărei adrese (FC1–4, count 1–8)
  pe orice dispozitiv Modbus, decodată în **toate felurile plauzibile**: o
  matrice de tip de date (uint16/int16, float/int32/uint32,
  double/int64/uint64) × ordine de cuvinte (ABCD / CDAB / BADC / DCBA), plus
  hex brut și ASCII. E bancul de lucru pentru endianness: când o valoare se
  citește ca gunoi, combinația corectă tip+ordine e de obicei vizibilă în
  matrice. NaN e arătat ca o constatare (SunSpec „not available"), nu
  ascuns.
- **Payload sample** — ia un payload JSON complet de la un dispozitiv HTTP
  sau MQTT salvat (MQTT retained → instant), pentru picker-ul de
  `json_path`.
- **Taxonomia erorilor** — eșecurile de citire sunt numărate pe tip:
  `timeout` (fără răspuns), `exception_N` (dispozitivul a răspuns cu
  excepția Modbus N — legătura e bună, cererea e greșită), `connection`
  (nivel TCP/serial). Vizibile per dispozitiv pe Status și `/metrics`.
  Politica de retry e deținută de gateway (`retry_attempts`/`retry_delay`),
  niciodată dublată de biblioteca Modbus, iar un răspuns scurt sau gol
  contează ca eșec, nu ca succes. *Reachability* e un **verdict de
  legătură**, nu unul per batch: un dispozitiv e declarat inaccesibil doar
  după ce se declanșează backstop-ul de eșecuri consecutive (care și
  forțează redeschiderea unei legături blocate-dar-deschise), deci un
  singur batch de registre cronic prost nu poate flapa evenimente
  `unreachable/recovered` cât timp legătura e bună — pierderea per batch
  rămâne vizibilă ca `batch_failures` + `stale_groups` per grup în
  suprafața de sănătate.
- **Query now** întoarce atât valoarea **brută** de pe fir cât și, când
  adresa e un registru selectat, un câmp aditiv **`corrected`** — exact
  valoarea pe care pipeline-ul de poll ar publica-o (scale/offset/enum
  aplicate, stateless: o citire de debug nu avansează niciodată filtrul
  monotonic). Dacă cele două diferă pe neașteptate, declarația de
  scale/decodare a registrului e locul unde te uiți.

---

## 14. Scrieri Modbus & lease-uri dead-man

Scrierea în hardware de câmp e apărată în adâncime. Totul e **oprit
implicit**.

1. Activează `security.allow_writes: true` (Config → Security).
2. Scrierile trebuie **autentificate** — activează login-ul sau setează
   `API_KEY` (scrierile anonime sunt refuzate chiar cu poarta deschisă).
3. Registrul trebuie declarat **writable în template-ul dispozitivului**, cu
   limite opționale `write_min` / `write_max`; codificarea (tip de date,
   scale și `offset` — inversat la scriere, `raw = (value − offset) ×
   scale`, inclusiv la revert-ul de siguranță) vine mereu din rândul de
   template, niciodată de la apelant.
4. **Dispozitivul primar e mereu read-only**; dispozitivele HTTP/JSON și
   registrele input/discrete nu se pot scrie.
5. Limită de rată per IP (`security.write_rate_limit_per_s`, implicit
   10/s).

`POST /api/devices/<id>/write` cu `{"address": ..., "value": ...}` scrie
FC6/FC16 (holding) sau FC5 (coil), verifică prin recitire și consemnează
fiecare încercare în audit.

**Lease-uri dead-man.** Adaugă `"lease_ms": 5000` și scrierea armează un
lease: dacă nu e reînnoit (printr-o altă scriere cu lease) în 5 s,
gateway-ul readuce registrul la valoarea `write_safe` declarată în template.
Lease-urile sunt **rezistente la crash**: setul activ persistă pe disc, deci
dacă gateway-ul însuși cade, registrele revin la valoarea sigură la
următorul boot. E primitiva corectă pentru bucle de control de tip limitare
de export / setpoint de putere: un controller căzut nu poate lăsa în urmă un
setpoint periculos. Lease-urile active: `GET /api/writes/leases`.

---

## 15. Siguranța configurației: snapshot-uri, rollback, backup

Config → **Backup & Snapshots**.

- **Snapshot-uri automate** — fiecare modificare de configurație reușită
  (dispozitive, registre, template-uri, metere virtuale, setări) face un
  snapshot al întregului pachet de configurație (debounce de 2 s
  coalizează rafalele). Se păstrează 50; snapshot manual cu notă oricând.
- **Diff semantic** — orice snapshot se compară cu configurația live sau cu
  alt snapshot **cheie cu cheie** (nu zgomot de linii): *ce ar anula un
  rollback*. Valorile secrete sunt mascate.
- **Rollback** — restaurarea înlocuiește pachetul verbatim, după un snapshot
  `pre-restore`, deci un rollback e el însuși reversibil.
- **Centura de siguranță last-known-good (LKG)** — după ~5 minute de
  funcționare sănătoasă, pachetul e marcat LKG. Dacă `config.yaml` nu se
  poate parsa la boot (editare manuală greșită, scriere ruptă), gateway-ul
  restaurează LKG automat și pornește — o cutie nesupravegheată își revine
  singură. Un fișier corupt blochează și salvările (o copie rămâne ca
  `config.yaml.bad`), deci valorile implicite nu-ți pot suprascrie niciodată
  configurația reală.
- **Auto-vindecarea configurației (`config.yaml.good`)** — independent de
  snapshot-uri, fiecare încărcare reușită păstrează o copie
  `config.yaml.good`; o editare coruptă ulterioară cade înapoi pe acel
  **last-known-good** (niciodată pe defaults goale), deci primarul continuă
  să citească hostul corect printr-o editare greșită. Condiția apare ca
  `config.healthy` în `/api/status` și ridică o alertă; salvările rămân
  blocate până repari fișierul. Vindecarea prinde și cazul viclean al unui
  fișier care încă se *parsează*, dar e o carcasă trunchiată (fișier gol,
  un scalar simplu, tăiat înaintea secțiunilor pe care le scrie fiecare
  salvare): o poartă de plauzibilitate îl rutează prin aceeași cale `.bad`
  + heal, iar un snapshot care ar *pierde dispozitive* nu e niciodată
  promovat peste `.good`-ul existent (o eliminare intenționată de
  dispozitiv împrospătează `.good` chiar prin salvare). Selecțiile de
  registre primesc același contract: `selected_registers.json` — al
  primarului **și al fiecărui dispozitiv** — își ține propria pereche
  `.good`/`.bad`, deci o selecție trunchiată se vindecă în loc să golească
  tăcut fiecare poller.
- **Backup export/import (ZIP)** — pentru portabilitate între hosturi.
  Exportul **elimină secretele** (credențiale MQTT/Influx, hash-uri de
  parole, headere de webhook/REST-push) și identitatea hostului implicit;
  `include_secrets=true` cere rolul admin sau cheia API și se consemnează în
  audit. Importul (body ZIP brut, ≤25 MB) face **merge** peste configurația
  live, ca secretele eliminate să supraviețuiască, și face întâi un snapshot
  `pre-import`. Snapshot-urile, prin contrast, sunt puncte de restaurare
  locale cu fidelitate completă — descărcarea unuia e păzită ca un export cu
  secrete.

**Ce intră în backup/snapshot:** `config.yaml`, registrele selectate ale
fiecărui dispozitiv, template-urile de dispozitiv ale utilizatorului,
`virtual_meters.yaml` + template-urile de metere virtuale din
`config/templates/`, presetările calculate și profilurile hardware ale
Builder-ului. Registrul de passkeys (`passkeys.json`) intră **doar** în
backup-ul cu secrete (`include_secrets=true`) și în snapshot-uri — altfel
un restore ar debloca autentificarea.

**Ce NU intră (și cum le salvezi separat):**
- `audit.jsonl` / `events.jsonl` — istoric operațional, se rotesc singure;
  dacă ai nevoie de ele pentru analiză, copiază-le manual.
- YAML-urile nodurilor ESP32 din Device Builder — trăiesc în **volumul
  ESPHome** (`esphome-config`), nu în `config/` al gateway-ului. Include-l
  în back-up-ul de infrastructură (Duplicati etc.) dacă folosești Builder-ul.
- `write_leases.json` — efemer prin design (lease-urile se reconstruiesc).

**Capcană:** snapshot-urile stau sub `config/snapshots/` în volumul de
configurație — protejează împotriva editărilor greșite, nu împotriva
pierderii volumului. Ține și un ZIP exportat altundeva.

---

## 16. Securitate

**Login-ul e PORNIT de la prima rulare** — o instalare proaspătă generează
o parolă de admin (tipărită o singură dată în log) și activează
autentificarea. Restul straturilor (allowlist IP, cheie API, TLS, URL
canonic) sunt opt-in — activează-le din **Config → Security** pe măsură ce
expunerea crește. Apărare în adâncime: fiecare strat se aplică independent.

### 16.1 Login & roluri

Un cont **admin**, plus conturi opționale **operator** și **viewer**:

| Rol | Poate | Nu poate |
|---|---|---|
| `viewer` | vede tot (GET), rulează interogările de registre la cerere | schimba ceva |
| `operator` | acțiuni live: diagnostice, bus trace, discovery, teste de dispozitiv, payload samples, **scrieri Modbus** (în limitele din template), test de alertă, reload de registre, propriile passkey-uri | orice ajunge într-un fișier de configurație (dispozitive, registre, template-uri, vmetere, setări, snapshot-uri), audit trail |
| `admin` | tot | — |

Parolele sunt hash-uite (PBKDF2-SHA256, 600k iterații); lasă câmpul de
parolă gol la salvare ca să o păstrezi pe cea curentă. **Activarea
login-ului cu o parolă `admin` goală sau implicită e refuzată la fiecare
punct de intrare** — ruta din UI, un `config.yaml` editat de mână, un
import de configurație și o restaurare de snapshot lovesc toate aceeași
gardă, deci nicio cale nu produce un gateway care *pare* încuiat, dar
acceptă `admin/admin`.
Login-urile eșuate se blochează per IP (`lockout_threshold` /
`lockout_minutes`, implicit 5 / 5 min). Sesiunile sunt cookie-uri HttpOnly,
glisante 7 zile, persistate ca hash-uri SHA-256 în `config/sessions.json` —
un restart de container te ține logat. Rotirea oricărei parole revocă
**fiecare** sesiune (un cookie vechi nu poate supraviețui rotației) — cu
excepția autorului: salvarea de securitate îți re-emite propria sesiune,
deci schimbarea parolelor nu te scoate niciodată *pe tine* din cont în
mijlocul treburii. Audit trail-ul e doar pentru admin.

### 16.2 Passkey-uri (WebAuthn)

Login fără parolă cu un autentificator de platformă sau o cheie de
securitate. Înrolare din meniul de utilizator (fiecare cont își
administrează propriile passkey-uri; adminul le vede pe toate). Cerințe și
capcane:

- Browserele rulează WebAuthn doar în **context securizat**: deschide UI-ul
  prin `localhost` (pe cutie) sau printr-un **hostname peste HTTPS** (de ex.
  `gateway.lan` în spatele Traefik). O adresă IP brută e respinsă — RP ID-ul
  trebuie să fie un hostname.
- Un passkey e legat de hostname-ul pe care a fost înrolat; alt nume =
  înrolare din nou.
- Login-ul cu passkey împarte lockout-ul cu parola, și poți înrola
  **înainte** să activezi login-ul, ca să nu rămâi blocat la mijloc de
  migrare.

### 16.3 HTTPS & reverse proxy (Traefik)

- **TLS încorporat**: indică în Config → Security un certificat + cheie sub
  `config/`, sau lasă gol pentru o pereche self-signed generată automat
  (**restart pentru aplicare**; browserele avertizează pe self-signed).
  Cheile private generate sunt create cu modul `0600`.
- **În spatele unui reverse proxy** (recomandat pentru certificate reale):
  termină TLS în Traefik/nginx/Caddy și setează `ui.trusted_proxies` la
  IP-ul proxy-ului (de ex. adresa containerului Traefik). Doar atunci sunt
  onorate `X-Forwarded-For` / `X-Forwarded-Proto` — altfel allowlist-ul de
  IP-uri, lockout-ul de login și audit trail-ul ar vedea toate proxy-ul în
  loc de clientul real, iar cookie-urile de sesiune n-ar fi marcate Secure.
  Gol (implicit) = nu ai încredere în nimeni. Schiță minimă Traefik: rutează
  `gateway.example.com` → `:8080` și adaugă IP-ul de rețea al containerului
  gateway-ului în `trusted_proxies`.
- **Adresa canonică** (`ui.canonical_url`): odată ce cutia e accesibilă
  printr-un hostname real peste HTTPS, setează asta (de ex.
  `https://gateway.lan`). UI-ul injectează atunci un mic redirect client-side
  care duce orice vizitator ce a deschis-o pe IP brut sau HTTP simplu către
  originea canonică — așa cookie-urile, passkey-urile și HSTS se leagă toate de
  un singur hostname. E o redirecționare din browser, nu din server, deci IP-ul
  nu e niciodată *blocat*: adaugă **`?local`** la URL ca să rămâi pe IP (setează
  un flag sticky `mbg-stay-local` în acel browser) — portița când DNS-ul/proxy-ul
  e picat și trebuie să ajungi direct la cutie.

### 16.4 Allowlist de IP-uri

`security.allowlist` — un IP/CIDR pe linie (de ex. `192.168.1.0/24`); gol =
deschis. Loopback-ul e mereu permis; păzește și `/ws`, `/health` și
`/metrics`. Cardul arată *IP-ul tău curent* ca să nu te blochezi singur.
**Notă Docker:** în spatele rețelei bridge implicite, clienții apar adesea
cu IP-ul gateway-ului docker — verifică „IP-ul tău curent" și pune în listă
ce vezi efectiv, sau folosește rețea host/macvlan pentru filtrare reală per
client. Blocat totuși? Editează `security.allowlist` în
`config/config.yaml` și repornește.

### 16.5 Cheie API

Setează `API_KEY` în environment ca să ceri `X-API-Key` la fiecare cerere de
modificare, independent de login — util pentru scripturi și CI. GET-urile
read-only și interogările la cerere rămân deschise. Cheia păzește și
**WebSocket-ul Device Builder** capabil de OTA (`/api/builder/stream`):
scripturile trimit headerul `X-API-Key`; browserele — care nu pot seta
headere WS custom — trimit subprotocolul `mbg-api-key.<base64url(key)>`
(UI-ul o face automat; un parametru de query e deliberat neacceptat — ar
scurge cheia în logurile de acces).

### 16.5b Întărirea browserului

Cererile de modificare venite de pe un **alt site** sunt respinse din start
(verificări `Sec-Fetch-Site` / `Origin` — o pagină drive-by din browserul
operatorului nu poate declanșa modificări de configurație), inclusiv pe
`/ws`. Se aplică headerele de securitate standard, inclusiv pe shell-ul de
login, iar valoarea URL-ului canonic e escapată la ieșire împotriva
stored-XSS. Secretele nu fac niciodată drumul dus-întors spre browser:
exporturile, listările de environment și logurile redactează tokenurile și
hash-urile de parole.

### 16.6 Audit trail

JSONL append-only sub `config/audit.jsonl` (rotire 1 MB × 5 fișiere):
login-uri (inclusiv eșecuri și lockout-uri), evenimente de passkey,
**fiecare scriere Modbus**, exporturi/importuri de configurație, restaurări
de snapshot — cu utilizator, IP, acțiune, țintă, status; valorile secrete
din payload-uri sunt redactate înainte de scriere. Îl vezi/filtrezi pe
pagina Status (admin) sau îl exporți CSV prin `GET /api/audit/export.csv`.

---

## 17. Observabilitate: Status, /metrics, evenimente

- **Pagina Status** — sănătatea per dispozitiv (conectat, rată de poll,
  taxonomia erorilor, vârsta datelor, latență), statistici de sink
  (publicate/sărite/eșuate, contoare de buffer), clienți WebSocket, resurse
  de proces (CPU, RSS, thread-uri, FD-uri), evenimente și alerte recente.
- **`/metrics`** — format text Prometheus, fără login (un scraper nu se
  poate loga), dar în spatele allowlist-ului de IP-uri; doar contoare și
  sănătate, niciodată configurație. Serii: `gateway_device_up/poll_rate/
  reads_total/errors_total/read_latency_ms/staleness_seconds/health`,
  `gateway_mqtt_connected/published_total`, `gateway_influx_connected/
  written_total/buffer_points/dropped_total`, `gateway_vmeter_up/
  requests_total/request_rate/errors_total/connections/quality`. Exemplu de
  scrape:

  ```yaml
  scrape_configs:
    - job_name: multi-bus-gateway
      static_configs: [{ targets: ["gateway:8080"] }]
  ```
- **`/health`** — proba containerului. Întoarce HTTP 503 **doar** când un
  meter virtual activat e cu adevărat picat (ceva ce un restart poate
  repara); un meter upstream inaccesibil degradează doar corpul răspunsului
  și rămâne HTTP 200 — restartul containerului nu-ți repară cablarea, iar
  watchdog-ul vmeter deja protejează consumatorii.
- **Jurnalul de evenimente** — ring persistat (`config/events.jsonl`,
  ultimele 300): eșecuri de citire, conectări/deconectări de sink, ciclul de
  viață al vmeterelor, rollback-uri, alerte.

---

## 18. Limbi & fus orar

- **Limbi** — UI-ul vine cu engleză și română; selectorul e în bara de
  titlu. Limbile sunt fișiere simple: copiază `ui/languages/en.json` în
  `<cod>.json`, traduci, și apare în selector — fără rebuild. Vezi
  `ui/languages/README.md`.
- **Fus orar** — Config → General. O zonă IANA (validată) care determină
  granițele calendaristice ale raportului lunar de energie; se aplică live.
  Implicit `Europe/Bucharest`.

---


## 18b. Rulare pe hardware limitat (Raspberry Pi)

Multi-Bus Gateway rulează confortabil pe un Raspberry Pi 3/4/5 sau pe un Intel
de putere mică. E frugal prin design — la o instalare tipică cu un singur meter
folosește ~90 MB RAM și câteva procente CPU, cu buffere mărginite și fără
scurgeri.

**Singurul lucru care contează pe un Pi: intervalul de poll realtime.** Tot ce
face gateway-ul per valoare (citire, parsare, publicare, buffering) se întâmplă
sub un singur GIL Python, deci CPU-ul total scalează cu *poll-uri pe secundă*,
iar un core Cortex-A53 e de ~8–10x mai lent decât un core Intel de desktop.
RAM-ul și numărul de thread-uri nu sunt niciodată zidul — CPU-ul la o cadență
rapidă este.

**Plicul de capacitate (măsurat + proiectat):**

| Config | RSS | Thread-uri | RPi 3 CPU @ realtime 1 s | RPi 3 @ 250 ms |
|--------|-----|------------|--------------------------|----------------|
| 1 dispozitiv | 65–80 MB | ~14 | 3–5 % | 10–18 % |
| 5 dispozitive + 3 vmeters | 90–110 MB | ~30 | 15–20 % | 55–75 % |
| 10 dispozitive + 10 vmeters | 130–160 MB | ~58 | 35–50 % | saturează un core |

**Setări recomandate pe un Pi 3:**

- Ține **realtime la 1 s** pentru monitorizare generală. Rezervă polling-ul
  sub-secundă (ex. 250 ms) pentru *singurul* registru care conduce o buclă de
  control în timp real — un meter de grid care alimentează o limită de export a
  invertorului — nu pentru toată harta.
- Folosește grupurile **normal (5 s)** și **slow (60 s)** pentru tot ce nu e
  critic pentru control (contoare de energie, temperaturi, diagnostic).
- Podeaua intervalului e 50 ms; `0` este refuzat (ar inunda bus-ul).
- Un Pi 4/5 are ~2–3x marja unui Pi 3 — cadența de 250 ms e ok acolo pentru o
  hartă mică.

**Regula de bază:** un RPi 3 rulează confortabil **~4–5 dispozitive + ~3 metere
virtuale** cu realtime ≥ 1 s. Peste asta, scalează *lateral* (un al doilea Pi /
host per bus), nu *pe verticală* — GIL-ul nu poate fi eliminat într-un proces.


## 18c. Ce văd consumatorii când o sursă cade

Promisiunea de bază a gateway-ului: **nu raportează niciodată absența ca o
valoare plauzibilă.** Când un dispozitiv nu mai răspunde, o citire eșuează sau
se pierde o conexiune, nicio ieșire nu primește un `0`/`false`/ultima-ghicire
inventat. Absența rămâne absență — dar *arată* diferit pe fiecare ieșire, așa
că un sistem din aval (Node-RED, Home Assistant, un PLC, Grafana) trebuie să
citească semnalul corect.

### Comportament pe fiecare ieșire

| Ieșire | La pierderea sursei | Cum o detectează consumatorul |
|--------|---------------------|-------------------------------|
| **MQTT** | topicurile de valori nu se mai actualizează; topicul retained ține ULTIMA valoare | abonează-te la `<prefix>/status` (retained) + LWT — `offline` = stale; NU te baza doar pe topicul de valoare |
| **InfluxDB** | nicio scriere → **gap** în serie (niciodată o linie plată cu valoarea veche) | `last()` + vârsta timestamp-ului, sau o alertă "no data" |
| **Meter virtual** | politica aleasă: `legacy` ține ultimele cuvinte · `fail` întoarce o excepție Modbus · `sentinel` servește SunSpec NA (NaN / 0x8000 / 0xFFFF) · `hold` ține până la un cap apoi fail. Dacă TOATE sursele-s stale, serverul se oprește (connection refused, ca un meter scos din priză). | excepția / conexiunea refuzată, și **blocul de calitate in-band la 61440** (stare + vârstă) |
| **HTTP push / output** | niciun push nou (ultimul payload nu e re-trimis ca proaspăt) | absența unui update; NaN/inf sunt respinse, niciodată emise ca JSON invalid |

### Singurul lucru de configurat în aval

Pe **MQTT**, *valoarea* retained nu poartă un marcaj de prospețime per-valoare —
semnalul de prospețime stă pe topicul `<prefix>/status` și pe Last-Will. Un
consumator care citește doar valoarea și ignoră status-ul poate acționa pe date
vechi. De aceea o buclă de control ar trebui să condiționeze pe prospețime (ex.
`armat ∧ lider ∧ telemetrie-proaspătă` în Node-RED), nu doar pe „am o valoare".
Dacă ai nevoie de prospețime in-band **fără** MQTT, dă meterului virtual
politica `fail` și citește blocul de calitate la 61440.

### Cum se compară cu alte echipamente

Acest comportament urmează standardele consacrate, nu inventează unul propriu:

- **SunSpec** (modelul de-facto pentru solar/metere) definește exact
  santinelele "not accessible" — NaN pentru float, `0x8000`/`0xFFFF` pentru
  întregi — pe care le emite politica `sentinel`, deci un consumator
  SunSpec-aware le înțelege nativ.
- **Home Assistant / MQTT**: topicul de disponibilitate `<prefix>/status` +
  Last Will este pattern-ul standard de availability pe care îl consumă tot
  ecosistemul.
- **Time-series (InfluxDB / Prometheus)**: un gap (nicio scriere) este
  reprezentarea idiomatică și corectă a datelor lipsă — nu backfill-uiești
  niciodată o valoare veche.

Acolo unde gateway-urile Modbus industriale obișnuite au ca default *ținerea
tăcută a ultimei valori fără semnal de staleness* — capcana periculoasă —
acest gateway o oferă (`hold`) doar ca alegere explicită, alături de `fail`
mai sigur (o excepție de meter-loss) și de blocul de calitate in-band. Pe
scurt: nimic riscant nu e reinventat; acolo unde industria are un footgun, se
oferă alternativa corectă.

## 19. Depanare

| Simptom | Verifică |
|---------|----------|
| Punctul Modbus roșu | host/port/unit id corecte? Modbus TCP activat pe dispozitiv? firewall? Folosește Diagnostics → probe: un răspuns cu excepție înseamnă totuși un dispozitiv viu |
| Valorile arată ca gunoiul | tip de date sau ordine de cuvinte greșite — rulează register probe și citește matricea tip×ordine |
| O citire unită eșuează cu *illegal data address* | slave strict/cu goluri — setează `max_gap: 0` pe dispozitiv |
| UI-ul arată o versiune veche după update | hard-refresh (bundle-ul e cache-busted, dar proxy-urile pot cache-ui) |
| Meter virtual „stale / starting" | sursa nu e proaspătă — verifică conexiunea dispozitivului; watchdog-ul refuză să servească date stale prin design |
| Consumatorul nu ajunge la un meter virtual | portul e în gama publicată de compose? accesibil din rețeaua consumatorului? urmărește tab-ul Logs pentru citiri |
| Entități MQTT lipsă în HA | broker accesibil? discovery activat? verifică `docker compose logs` |
| Avertismente de write-retry InfluxDB | URL/token/bucket corecte? Clientul reîncearcă ~5 min, apoi batch-ul e recuperat în buffer și replay-at — urmărește `replayed_total`/`dropped_total` |
| Login-ul refuză să se activeze | setează întâi o parolă nouă de admin — `admin` implicit nu poate fi folosită |
| Blocat (login) | așteaptă `lockout_minutes`, sau repornește containerul (contorul de lockout e în memorie; sesiunile supraviețuiesc restartului) |
| Blocat (allowlist IP) | editează `security.allowlist` în `config/config.yaml`, repornește |
| Înrolarea passkey eșuează | ești pe URL cu IP sau HTTP simplu — folosește `localhost` sau un hostname peste HTTPS |
| Configurația s-a stricat după o editare | boot-ul restaurează automat last-known-good; fișierul stricat rămâne ca `config.yaml.bad`; sau fă rollback la un snapshot din Config → Backup |
| Scrierile întorc 403 | `security.allow_writes` oprit, lipsă credențial (login/cheie API), registru nedeclarat writable, sau valoare în afara `write_min`/`write_max` |

Tot blocat? Deschide un issue — include `docker compose logs` și
configurația ta (redactată).
