# Manual de utilizare — Multi-Bus Gateway

[🇬🇧 English](MANUAL.md) | 🇷🇴 **Română**

Un ghid pas cu pas: de la o instalare nouă la monitorizare, integrări și servirea
contoarelor virtuale. Pentru analiza aprofundată a arhitecturii motorului de
contoare virtuale vezi **[VIRTUAL-METER.ro.md](VIRTUAL-METER.ro.md)**.

> 🇷🇴 Română. Versiunile localizate sunt binevenite prin PR.

## Cuprins
1. [De ce ai nevoie](#1-de-ce-ai-nevoie)
2. [Instalare (Docker)](#2-instalare-docker)
3. [Prima configurare](#3-prima-configurare)
4. [Interfața Web, tab cu tab](#4-interfața-web-tab-cu-tab)
5. [Dispozitive & template-uri (multi-device)](#5-dispozitive--template-uri-multi-device)
6. [Contoare virtuale — pas cu pas](#6-contoare-virtuale--pas-cu-pas)
7. [Home Assistant (MQTT)](#7-home-assistant-mqtt)
8. [InfluxDB & Grafana](#8-influxdb--grafana)
9. [Securitate (opțional)](#9-securitate-opțional)
10. [Depanare](#10-depanare)

---

## 1. De ce ai nevoie
- Un **Janitza UMG 512-PRO** (sau un UMG compatibil) accesibil în rețea cu
  **Modbus TCP activat** (portul implicit 502). Notează-i IP-ul.
- O gazdă cu **Docker + Docker Compose**.
- *(Opțional)* un broker MQTT (pentru Home Assistant) și/sau InfluxDB (pentru
  Grafana).

---

## 2. Instalare (Docker)

```bash
# 1) Get the code
git clone https://github.com/sm26449/multi-bus-gateway.git
cd multi-bus-gateway

# 2) Create your environment file
cp .env.example .env
#    edit .env — at minimum set MODBUS_HOST to your Janitza's IP (see step 3)

# 3) Start it
docker compose up -d

# 4) Open the UI
#    http://<host>:8080
```

Atât pentru o configurare doar de monitorizare. Jurnale: `docker compose logs -f`.

---

## 3. Prima configurare

> **Sfat:** după prima pornire poți seta tot din UI — conexiunea contorului pe
> cardul lui din **Devices**, iar MQTT/InfluxDB sub **Config** — se salvează în
> `config/config.yaml` și se aplică fără restart. `.env` e doar o cale comodă de a pre-popula un
> deploy nou. O valoare din env are întâietate și blochează acel câmp în UI.

Editează `.env` (sau setează aceleași variabile în compose-ul tău). Esențialul:

| Variabilă | Ce este | Exemplu |
|-----------|---------|---------|
| `MODBUS_HOST` | IP-ul Janitza | `192.168.1.100` |
| `MODBUS_PORT` | port Modbus TCP | `502` |
| `MODBUS_UNIT_ID` | unitate Modbus | `1` |
| `MQTT_BROKER` / `MQTT_PORT` | broker (opțional) | `192.168.1.100` / `1883` |
| `INFLUXDB_URL` / `INFLUXDB_TOKEN` | InfluxDB (opțional) | — |
| `UI_PORT` | port interfață Web | `8080` |

Repornește după editare: `docker compose up -d`. Punctul **Modbus** din colțul
dreapta-sus al interfeței devine verde când se conectează.

Poți de asemenea configura majoritatea acestor lucruri din interfață → tab-ul
**Config** (fără repornire pentru modificările de registre/poll — ele se reîncarcă
la cald).

---

## 4. Interfața Web, tab cu tab

Deschide `http://<host>:8080`.

Nav-ul de sus are patru zone — tot ce ține de un singur contor stă în
workspace-ul acelui dispozitiv, nu într-un meniu global:

- **Dashboard** — carduri KPI live globale + valorile pe care le-ai fixat. Apasă
  *Customize* pentru a alege carduri; comută între vizualizarea card/tabel.
- **Devices** — fiecare sursă (Modbus TCP, RTU în curând, sau **HTTP/JSON**) ca
  un card cu status live; wizard-ul **Add Device** e aici. Deschide un dispozitiv
  pentru **workspace-ul lui cu tab-uri**: *Overview* (rezumat read-only +
  sănătatea datelor), *Edit* (conexiune, template, intervale de poll, toggle-uri
  ieșiri MQTT/InfluxDB), *Registers* (harta Available/Selected) și *Monitor /
  History / Energy* pentru acel dispozitiv (vezi pasul 5).
- **Config** — doar setări globale: broker **MQTT**, conexiune **InfluxDB**,
  **Backup**, **Security**. Modificările se reîncarcă la cald.
- **Virtual Meters** — servește valorile live ca și contoare standard către alte
  sisteme (vezi pasul 6).

Cele trei puncte din dreapta-sus (Modbus / MQTT / InfluxDB) arată starea
conexiunii — apasă pe unul pentru detalii.

**Monitor / History / Energy per dispozitiv** — din workspace-ul unui dispozitiv:
*Monitor* (necesită polling) trage orice valoare pe un grafic live cu zoom;
*History* și *Energy* (necesită ieșirea InfluxDB a dispozitivului) citesc datele
stocate înapoi — linii de istoric cu bandă min/max și totaluri lunare de energie
import/export/reactivă/aparentă.

---

## 5. Dispozitive & template-uri (multi-device)

O singură instalare poate citi **mai multe surse**. Fiecare dispozitiv combină o
**conexiune** (Modbus TCP acum, RTU în curând, **HTTP/JSON**, sau **MQTT**) cu un
**template de dispozitiv** — harta de registre a acelui tip de echipament — și
**rutarea lui proprie de date**.

> **Nu știi adresa unui dispozitiv?** Devices → *Discover devices* → *Modbus TCP
> scan*: scanează un interval privat din LAN pe portul Modbus după dispozitive
> care răspund și poate mătura unit-id-urile pe un host. *Use* pre-completează
> wizard-ul.

**Adaugă un dispozitiv:** Devices → *Add Device*:

1. **Conexiune** — protocol, apoi Modbus (host/IP, port, unit ID, timeout),
   **HTTP/JSON** (un URL JSON; fiecare registru își extrage valoarea printr-un
   `json_path`, ex. un contor Fronius prin Solar API), sau **MQTT** (broker, port,
   un topic de abonare + opțional user/parolă/TLS; fiecare registru citește
   valoarea din payload-ul JSON prin `json_path`, sau ia payload-ul brut ca număr,
   și poate avea topicul lui cu wildcard `+`/`#`). Apasă **Test connection**: la
   Modbus orice răspuns confirmă un dispozitiv viu; la MQTT se conectează și
   așteaptă scurt un mesaj-eșantion.
2. **Template** — alege din bibliotecă (harta Janitza UMG 512-PRO e inclusă),
   **încarcă** un template `.json` (validat rând cu rând; conflictele întreabă
   înainte de suprascriere) sau **creează** unul în editor. În editor definești
   metadatele (id, producător, model) și registrele (adresă, nume, etichetă,
   unitate, tip de date, categorie, grup de poll); erorile sunt marcate exact
   pe rândul problematic. Built-in-urile sunt read-only — folosește *Duplicate
   to edit*. Un template folosit de un dispozitiv nu poate fi șters. *Export*
   descarcă template-ul pentru distribuire.
3. **Rutare date** — **id-ul** dispozitivului devine cheia de rutare: valorile
   se publică sub **prefixul de topic MQTT** al dispozitivului (preview-ul live
   arată un topic real de exemplu) și ajung în **bucket-ul InfluxDB** al lui
   (creat automat, retenție 90 de zile), cu tag-ul lui de device.

După creare, tab-ul **Registers** al dispozitivului arată catalogul lui din
template; selectezi ce se citește (sau folosești auto-select pentru registrele
implicite ale template-ului), salvezi — se reîncarcă doar pollerele acelui
dispozitiv. Fiecare card din pagina **Devices** arată sănătatea live (punct de
stare, rată de poll, vechimea datelor) și rutarea; workspace-ul are editare și
ștergere (ștergerea e blocată cât timp un contor virtual folosește dispozitivul).

**Instalările existente:** UMG 512-PRO al tău apare automat ca device #1 —
topicurile, bucket-ul, tagurile și entitățile Home Assistant rămân neschimbate,
iar identitatea de rutare e blocată ca să rămână byte-identică.

**Măsurători calculate** (workspace-ul dispozitivului → *Calculated*): derivi o
valoare nouă prin formulă din măsurătorile dispozitivului — factor de putere
`_P / _S`, o sumă pe faze, o conversie de unitate, `(E - prev(E)) / dt * 3600`
pentru putere medie dintr-un contor Wh etc. Builder-ul îți dă chip-uri clickabile
cu câmpurile din Measurements, o paletă de funcții/operatori și preview live;
preset-urile completează formule comune și îți poți *salva propriile preset-uri*.
O valoare calculată curge la toate ieșirile și apare în Monitor și History ca
orice măsurătoare.

**Ieșiri** (workspace-ul dispozitivului → *Outputs*): pe lângă MQTT și InfluxDB,
fiecare dispozitiv poate și **servi** valorile live ca JSON read-only la
`GET /api/meters/<id>` (stil Solar API, opt-in), și le poate **împinge** ca JSON
către un URL extern la interval (webhook / cloud), cu headere de auth stocate
mascat. Ambele se comută per dispozitiv, lângă sink-urile MQTT/InfluxDB.

---

## 6. Contoare virtuale — pas cu pas

Scop: a permite unui alt sistem (Victron ESS, un invertor Fronius, orice client
SunSpec) să citească acest unic Janitza ca și contorul pe care *el* îl așteaptă.

> ⚠️ Un contor virtual poate alimenta o buclă de control. Parcurge pașii 6.1→6.3
> (validare în paralel) înainte de a-l face vreodată singurul contor al unui
> consumator.

**6.0 — Publică porturile (o singură dată).** În `docker-compose.yml` intervalul de
porturi al contoarelor este publicat (implicit `1502-1512`, plus `502` pentru
Fronius). Alege porturile instanțelor în interiorul acelui interval. Lărgește
intervalul + recreează containerul dacă ai nevoie de mai multe.

**6.1 — Alege sau creează un șablon.** Mergi la **Virtual Meters → Templates**.
- Livrate: `em24_av53` (Carlo Gavazzi EM24 → Victron), `fronius_ts_native`
  (Fronius Smart Meter → DataManager), `fronius_sunspec_meter` (SunSpec generic).
- *Șablon nou*: definește fiecare registru (adresă, tip, scalare, sursă). Sursa
  poate fi un registru Janitza live, o constantă sau o sumă de registre.
- *Import*: adaugă un `.yaml` partajat de altcineva (validat înainte de salvare).

**6.2 — Adaugă o instanță.** Pe sub-tab-ul **Meters**, alege șablonul, un port
liber, unit id → **Add instance**. Pornește **dezactivată**.

**6.3 — Validează în paralel.** Activează instanța (comutator). Îndreaptă un
consumator de *test* — sau pur și simplu deschide tab-ul **Logs** — către
`host:port`. Urmărește jurnalul de interogări live: vezi exact ce citește
consumatorul, când și ce returnezi. Compară valorile servite cu contorul tău real.
Tab-ul **Stats** arată rata de cereri, erorile și ce registre sunt citite cel mai
mult.

**6.4 — Fă comutarea.** Odată ce ai încredere în el, îndreaptă consumatorul real
către contorul virtual și elimină contorul fizic dedicat al acestuia. **Watchdog-ul
de prospețime** este plasa ta de siguranță: dacă datele Janitza devin învechite,
contorul oprește răspunsul astfel încât fail-safe-ul propriu al consumatorului
pentru pierderea rețelei să se activeze.

**6.5 — Observă și exportă.** Tab-urile **Logs**/**Stats** păstrează ultimele 1024
de cereri + contoare în RAM. **Exportă** un șablon (YAML) pentru a-l partaja sau a
face backup; cardul contorului (acordeon) arată conexiunile active ale clienților
(ip:port).

---


### Registre de calitate pentru PLC/SCADA (opțional)

Dacă sistemul care CITEȘTE virtual meter-ul trebuie să știe cât de de
încredere sunt datele (proaspete vs ținute vs lipsă), bifează **„Bloc de
calitate in-band"** pe instanță (formularul de adăugare/editare). Meter-ul
servește atunci și un mic bloc read-only la adresa **61440** — identic
pentru orice virtual meter:

| Adresă | Tip | Semnificație |
|-------:|-----|--------------|
| 61440 | u16 | versiune format (1) |
| 61441 | u16 | 0 legacy · 1 ok · 2 degradat · 3 stale |
| 61442–61444 | u16 | rânduri fresh / stale / lipsă |
| 61445 | u16 | total rânduri de date |
| 61446 | u32 | vârsta celei mai noi valori fresh (s); 0xFFFFFFFF = niciodată |

Garda tipică în consumator: *folosește datele doar cât 61441 == 1; alarmă
pe 3*. Împerechează-l cu politica `fail`/`sentinel`/`hold` (cu `legacy`
starea e 0 — calitatea nu se judecă per registru). Specificația completă:
`docs/virtual-meter-spec.md`.

## 7. Home Assistant (MQTT)

Setează `MQTT_BROKER`/`MQTT_PORT` (și credențialele) în `.env`, repornește.
Monitorul publică **autodiscovery MQTT pentru Home Assistant**, astfel încât
entitățile apar automat sub dispozitiv. Un topic Last-Will marchează dispozitivul
ca offline dacă monitorul se oprește. Alege ce registre se publică (și topicurile
lor) în tab-ul **Registers** al dispozitivului.

---

## 8. InfluxDB & Grafana

Setează `INFLUXDB_URL`, `INFLUXDB_TOKEN`, `INFLUXDB_ORG`, `INFLUXDB_BUCKET` în
`.env`. Scrierile sunt grupate cu reîncercare/backoff automat și o protecție
anti-NaN. Îndreaptă Grafana către același bucket. Măsurătoarea/tag-urile per
registru sunt configurabile în tab-ul **Registers** al fiecărui dispozitiv. Profilurile compose
opționale pot porni un InfluxDB + Grafana local (vezi README).

**Garanții de date.** Fiecare punct e ștampilat cu ora *citirii* Modbus, nu a
flush-ului. Dacă InfluxDB devine inaccesibil, punctele intră într-un buffer
store-and-forward în RAM (implicit **10 minute / 50.000 de puncte**, reglabil
prin `influxdb.buffer_minutes` / `buffer_max_points` în `config.yaml`) și sunt
replay-ate cu timestamp-urile originale la reconectare — idempotent, pentru că
InfluxDB deduplică pe măsurătoare+taguri+timestamp, deci fără duplicate.
Batch-urile abandonate de client după ~5 min de reîncercări proprii sunt
recuperate în același buffer. Panele mai lungi decât fereastra bufferului pierd
punctele cele mai vechi (doar RAM — un restart golește bufferul); pentru
tensiuni, înregistrarea internă a contorului le poate reface prin
`python -m janitza.backfill`. Urmărește `buffer_points` / `replayed_total` /
`dropped_total` în **`/api/status`**. MQTT nu se replay-ează intenționat: e o
magistrală live (consumatorii acționează pe „acum”), iar la reconectare se
republică întreaga stare curentă.

---

## 9. Securitate (opțional)

Totul de mai jos este **dezactivat implicit** — aparatul e gândit pentru o
rețea de încredere. Activează din **Config → Security** când trebuie să fie
accesibil dintr-o rețea mai largă.

- **Autentificare** — cere utilizator/parolă pentru UI/API. Un **admin**
  (acces complet) și un **viewer** opțional (doar citire: vede tot, nu schimbă
  nimic). Parolele se stochează criptat (hash PBKDF2); lasă câmpul de parolă gol
  la salvare ca să o păstrezi. Autentificările eșuate sunt limitate per IP
  (blocare după N încercări, M minute — configurabile). Apare un ecran de login;
  butonul de deconectare e în bara de titlu.
- **HTTPS** — servește UI-ul prin TLS. Indică un certificat + cheie, sau lasă
  căile goale pentru a genera automat o pereche self-signed la următoarea
  pornire (**repornește containerul pentru a aplica**). Un certificat
  self-signed produce un avertisment în browser; pune un certificat real în
  producție.
- **MQTT TLS** — criptează legătura cu brokerul (portul 8883). Încarcă un
  certificat CA pentru a verifica brokerul și, opțional, un certificat + cheie
  de client pentru **mutual TLS**. Pune fișierele în `config/` și indică-le
  căile din container. „Sari peste verificare” e doar pentru test.
- **Listă IP permise** — restricționează ce IP-uri/subrețele pot accesa UI/API
  (una pe linie, ex. `192.168.1.0/24`; gol = deschis). Loopback e mereu permis.
  Cardul arată *IP-ul tău curent* ca să nu te blochezi singur. **Notă Docker:**
  în spatele rețelei bridge implicite, conexiunile par a veni de la IP-ul
  gateway-ului docker, nu de la clientul real — verifică „IP-ul tău curent” și
  permite ce vezi acolo; pentru filtrare reală per client folosește rețea host
  sau macvlan. Dacă te blochezi, editează `config/config.yaml`
  (`security.allowlist`) și repornește.
- **Cheie API** (existentă) — setează `API_KEY` în environment pentru a cere un
  header `X-API-Key` la cererile care modifică date, independent de login.

---

## 10. Depanare

| Simptom | Verifică |
|---------|----------|
| Punctul Modbus roșu | `MODBUS_HOST`/portul corecte? Modbus TCP activat pe Janitza? firewall? |
| Interfața arată versiunea veche după actualizare | reîmprospătează forțat browserul (bundle-ul aplicației are cache-busting, dar proxy-urile pot stoca în cache) |
| Contor virtual „stale / starting” | sursa Janitza nu este proaspătă — verifică conexiunea Modbus; watchdog-ul nu va servi date învechite, prin design |
| Consumatorul nu poate ajunge la un contor virtual | este portul în interiorul intervalului compose publicat? accesibil din rețeaua consumatorului? verifică tab-ul **Logs** pentru citiri primite |
| Entitățile MQTT lipsesc din HA | brokerul accesibil? autodiscovery activat? urmărește `docker compose logs` |
| Avertismente InfluxDB write-retry | URL/token/bucket InfluxDB corecte? Clientul reîncearcă ~5 min, apoi batch-ul e recuperat în bufferul RAM și replay-at la reconectare — vezi `replayed_total`/`dropped_total` în `/api/status` |

Tot blocat? Deschide un issue — include `docker compose logs` și configurația ta
(cu datele sensibile mascate). Vezi **[VIRTUAL-METER.ro.md](VIRTUAL-METER.ro.md)**
pentru detaliile interne ale motorului și cum să adaugi un nou șablon de contor.
