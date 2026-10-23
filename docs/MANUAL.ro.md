# Manual de utilizare — Multi-Bus Gateway

[🇬🇧 English](MANUAL.md) | 🇷🇴 **Română**

Ghid pas cu pas pentru tehnician/integrator: de la o instalare curată la un
gateway cu mai multe dispozitive, contoare virtuale, diagnostice și acces
securizat. Documente însoțitoare (în engleză):

- **[architecture.md](architecture.md)** — cum funcționează motorul (diagrame).
- **[API.md](API.md)** — referința completă a endpoint-urilor REST, cu rolurile necesare.
- **[api-examples.md](api-examples.md)** — API-ul prin exemple: comenzi, reguli, MQTT, un client Python.
- **[virtual-meter-spec.md](virtual-meter-spec.md)** — contractul la nivel de
  fir al unui contor virtual (politicile de prospețime, blocul de calitate la 61440).
- **[device-catalog.md](device-catalog.md)** — fiecare hartă de registre
  inclusă și sursa față de care a fost verificată.
- **[csv-import.md](csv-import.md)** — importul unei hărți de registre CSV de la producător.
- **[alerts-webhooks.md](alerts-webhooks.md)** — alertele de infrastructură și pragurile pe valori.

## Cuprins
1. [De ce ai nevoie](#1-de-ce-ai-nevoie)
2. [Instalare (Docker)](#2-instalare-docker)
3. [Prima configurare](#3-prima-configurare)
4. [Interfața web, filă cu filă](#4-interfața-web-filă-cu-filă)
5. [Dispozitive și șabloane de dispozitiv](#5-dispozitive-și-șabloane-de-dispozitiv)
5b. [Instalații — un datalogger, mai multe unități](#5b-instalații--un-datalogger-mai-multe-unități)
6. [Registre, grupuri de citire și praguri](#6-registre-grupuri-de-citire-și-praguri)
7. [Registre calculate](#7-registre-calculate)
8. [MQTT și Home Assistant](#8-mqtt-și-home-assistant)
9. [InfluxDB și Grafana](#9-influxdb-și-grafana)
10. [REST push și fluxul HTTP/JSON](#10-rest-push-și-fluxul-httpjson)
11. [Contoare virtuale — pas cu pas](#11-contoare-virtuale--pas-cu-pas)
11b. [Noduri ESP — un serviciu separat](#11b-noduri-esp--un-serviciu-separat)
11c. [Calitatea energiei — înregistratorul de evenimente PQ (Janitza)](#11c-calitatea-energiei--înregistratorul-de-evenimente-pq-janitza)
11d. [Server OPC UA — pentru SCADA și panouri HMI](#11d-server-opc-ua--pentru-scada-și-panouri-hmi)
12. [Alerte și webhook-uri](#12-alerte-și-webhook-uri)
13. [Diagnostic](#13-diagnostic)
14. [Scrieri Modbus și lease-uri dead-man](#14-scrieri-modbus-și-lease-uri-dead-man)
14b. [Comenzi — scrieri cu nume, din șablon](#14b-comenzi--scrieri-cu-nume-din-șablon)
14c. [Reguli — controlerul declarativ](#14c-reguli--controlerul-declarativ)
15. [Siguranța configurației: snapshot-uri, revenire, backup](#15-siguranța-configurației-snapshot-uri-revenire-backup)
16. [Securitate](#16-securitate)
17. [Observabilitate: Status, /metrics, evenimente](#17-observabilitate-status-metrics-evenimente)
18. [Limbi și fus orar](#18-limbi-și-fus-orar)
19. [Depanare](#19-depanare)

---

> Designul defensiv din spatele întregului manual — fiecare protecție,
> garanție de livrare și refacere automată — e catalogat în
> [reliability.md](reliability.md).

## 1. De ce ai nevoie

- Cel puțin o sursă: un dispozitiv **Modbus TCP** (de ex. un Janitza
  UMG 512-PRO, cu portul 502 activat), un dispozitiv **Modbus RTU** pe o
  linie serială (`/dev/ttyUSB0` + adaptor RS-485), un endpoint **HTTP/JSON**
  (Fronius Solar API, Shelly, Tasmota…) sau un broker **MQTT** cu topicuri
  de telemetrie.
- Un host cu **Docker + Docker Compose** (amd64 sau arm64 — merge și pe un
  Raspberry Pi).
- *(Opțional)* **propriul** broker MQTT și/sau InfluxDB, dacă le preferi
  celor incluse — stiva implicită le aduce pe amândouă.

---

## 2. Instalare (Docker)

```bash
# 1) Ia codul
git clone https://github.com/sm26449/multi-bus-gateway.git
cd multi-bus-gateway

# 2) Creează fișierul de mediu — OBLIGATORIU: cele patru secrete ale stivei nu au valori implicite
cp .env.example .env

# 3) Pornește stiva COMPLETĂ — gateway + broker MQTT (mosquitto) +
#    InfluxDB + Grafana (MQTT Explorer doar cu --profile debug). Tot ce îi
#    trebuie produsului e în acest fișier; nimic extern de instalat.
docker compose up -d

# 4) Ia parola de admin generată (doar la prima pornire), apoi deschide interfața
docker compose logs multi-bus-gateway | grep -A3 'FIRST RUN'
#    http://<host>:8080
```

Din prima, fluxul merge cap-coadă: gateway-ul publică în brokerul inclus
(brokerul lui implicit este `mosquitto`), **MQTT Explorer** pe `:4000` arată
fiecare topic, iar InfluxDB se configurează singur la prima pornire
(org/bucket `multibus`; schimbă parola și tokenul în `.env`, apoi activează
ieșirea în Setări → InfluxDB și lipește tokenul — Grafana e pe `:3000`).
Vrei mai puțin? Pornește doar ce îți trebuie:
`docker compose up -d multi-bus-gateway mosquitto`. Preferi mai târziu
brokerul sau InfluxDB-ul tău? Îndreaptă gateway-ul spre ele din interfață —
cele incluse sunt containere obișnuite, pe care le poți opri.

Ai deja o stivă mai mare pe rețeaua comună? Fișierul de bază își *numește*
rețeaua `pv-stack-network` (o schimbi cu `PV_STACK_NETWORK` în `.env`),
astfel că o instalare nouă o creează și serviciile de mai târziu i se pot
alătura. Dacă rețeaua — și brokerul/InfluxDB-ul — **există deja**, folosește
fișierul suplimentar și pornește doar serviciile gateway-ului:

```bash
docker compose -f docker-compose.yml -f docker-compose.external-network.yml \
  up -d multi-bus-gateway
```

Porturile publicate de fișierul compose implicit: `8080` (interfață/API),
`1883` + `9001` (MQTT inclus + WebSockets), `4000` (MQTT Explorer, doar cu
`--profile debug`, pe 127.0.0.1), `8086` (InfluxDB), `3000` (Grafana),
`1502–1512` (plaja contoarelor virtuale, o mărești cu
`VMETER_PORT_START/END`) și `502` (Modbus standard, pentru consumatorii care
îl cer — scoate-l dacă hostul îl folosește deja). Containerele rulează
**fără root** (gateway-ul cu uid 10001; bridge-ul serial cu uid 10002, cu
`/dev` doar în citire și acces doar la tty), de aceea compose-ul setează
`net.ipv4.ip_unprivileged_port_start=0` — doar în spațiul de rețea al
containerului, ca un proces neprivilegiat să poată asculta pe `:502`; pe
host nu se schimbă nimic. Pentru un dispozitiv RTU, fie pornești bridge-ul
serial inclus — `docker compose --profile rtu-bridge up -d` (recomandat; vezi
[rtu-serial.md](rtu-serial.md)) — fie dai adaptorul direct containerului:
`devices: ["/dev/ttyUSB0:/dev/ttyUSB0"]` într-un override de compose.

Jurnale: `docker compose logs -f`.

---

## 3. Prima configurare

> **Sfat:** după prima pornire poți seta totul din interfață — conexiunile
> dispozitivelor în pagina **Dispozitive**, MQTT/InfluxDB în **Setări** —
> se salvează în `config/config.yaml` și se aplică fără restart. `.env` doar
> pre-completează o instalare nouă; o valoare din mediu are prioritate și
> **blochează** câmpul în interfață (scoate-o din mediu ca să-l poți edita).

Variabilele de mediu opționale:

| Variabilă | Ce este | Exemplu |
|----------|-----------|---------|
| `MODBUS_HOST` / `MODBUS_PORT` / `MODBUS_UNIT_ID` | conexiunea dispozitivului principal | `192.168.1.100` / `502` / `1` |
| `MQTT_ENABLED` / `MQTT_BROKER` / `MQTT_PORT` | ieșirea MQTT | `true` / `mosquitto` / `1883` |
| `MQTT_USERNAME` / `MQTT_PASSWORD` / `MQTT_PREFIX` / `MQTT_PUBLISH_MODE` | detalii MQTT | — |
| `INFLUXDB_ENABLED` / `INFLUXDB_URL` / `INFLUXDB_TOKEN` / `INFLUXDB_ORG` / `INFLUXDB_BUCKET` | ieșirea InfluxDB | — |
| `UI_PORT` | portul interfeței web | `8080` |
| `UI_HOST` | adresa pe care ascultă — fără container, implicit loopback (`127.0.0.1`); imaginea de container setează `0.0.0.0` (expunerea o decide maparea de porturi din compose) | `0.0.0.0` |
| `API_KEY` | cere `X-API-Key` la cererile care modifică ceva | — |
| `VMETER_PORT_START` / `VMETER_PORT_END` | plaja de porturi a contoarelor virtuale | `1502` / `1512` |

Punctele de stare din bara de sus a interfeței (Modbus / MQTT / InfluxDB)
devin verzi pe măsură ce fiecare legătură se conectează — apasă pe unul
pentru detalii.

---

## 4. Interfața web, filă cu filă

Deschide `http://<host>:8080`. Navigarea de sus:

- **Tablou de bord** — o **bandă a amplasamentului** (câte un card pe
  instalație, cu puterea totală live și unitățile conectate, plus
  dispozitivele fixate pe prezentare cu pioneza din rândul lor — fiecare
  card fixat se poate personaliza: alegi valoarea afișată, îi dai o
  etichetă, schimbi ordinea; contoarele interne ale gateway-ului sunt în
  pagina Status) + o **prezentare a flotei**: cu două sau mai multe
  dispozitive pagina se deschide pe câte un rând pe dispozitiv (punct de
  stare, numărul de alarme pe praguri explicite, primele lui registre de
  tablou ca valori principale), grupate pe instalație și ordonate cu cele
  mai grave primele, cu căutare și filtrul *Doar probleme*. Un clic pe rând
  deschide vederea dispozitivului: până la 12 widget-uri principale (alese
  și ordonate în *Personalizează*) deasupra unor secțiuni pliabile care
  grupează TOATE valorile după măsurătoare; lângă *Toate dispozitivele* un
  buton schimbă rapid dispozitivul. Culorile implicite ale widget-urilor
  urmează convenția de faze din Setări → General. O instalare cu un singur
  dispozitiv sare peste prezentare și se deschide direct pe dispozitiv.
- **Dispozitive** — fiecare sursă, cu starea ei live, grupate pe
  instalație. Găsești una cu căsuța de căutare (`/` sare la ea) și cu
  filtrele: *tip* (contor, invertor, baterie, pompă de căldură…, dedus din
  șablon și din rolul dispozitivului), *stare*, *protocol*. Tot aici sunt
  asistentul **Adaugă dispozitiv**, **Adaugă instalație** (§5b),
  **Descoperă dispozitive** și **bridge-urile** (cutiile prin care se ajunge
  la o magistrală serială). Un dispozitiv deschis are pagina lui, cu filele
  în trei grupuri:
  - *Live* — *Prezentare* (ce citește acum, sănătatea datelor, ultima
    citire), *Istoric*, *Energie* și *Calitatea energiei* la un contor care o
    înregistrează;
  - *Configurare* — *Conexiune* (adresa, șablonul, cât de des citește; o
    unitate dintr-o instalație arată în loc *Citită prin*, setată pe
    instalație), *Măsurători* (ce se citește), *Calculate*, *Comenzi*
    (§14b), *Contoare* (§5.4), *Ieșiri*, *Atribute* (§5c);
  - *Diagnostic* — *Monitor* (cadrele lui pe fir) și *Jurnal* (evenimentele
    lui de achiziție).

  Meniul ⋯ ține ce se face *asupra* dispozitivului: tipul, Dezactivează,
  Șterge. Un dispozitiv pe care ceva îl folosește — o regulă, un contor
  virtual, un dispozitiv pe care îl măsoară, o formulă care îl numește — nu
  poate fi șters: pagina arată ce îl folosește (*Folosit de*) și oferă în
  loc **Dezactivează**. Scoate întâi acele folosiri; e decizia
  operatorului și nu se face niciodată în locul tău.
- **Ieșiri** (într-un dispozitiv) — unde pleacă valorile lui. În stânga,
  fiecare ieșire (MQTT, InfluxDB, HTTP/JSON, OPC UA, REST push) cu starea ei
  și destinația (topic, bucket, câte valori); în dreapta, setările celei
  alese. MQTT și InfluxDB spun și **când** pleacă o valoare — la schimbare
  sau la fiecare citire, cât de des se retrimite una neschimbată (MQTT), cel
  mult cât de des se scrie un punct (InfluxDB) — ca în Setări sau proprii
  dispozitivului (unitățile unei instalații le împart pe ale grupului lor).
  Fiecare comutator se aplică imediat, fără restartul citirii; ce are câmpuri
  de completat (REST push, valorile OPC UA) se salvează din panoul propriu.
  Săgețile te mută prin listă; ieșirea deschisă e ținută minte. **Lacătul de
  scriere** e la *Comenzile* dispozitivului (sau în *Conexiune* când nu are
  comenzi); **înregistratorul de evenimente PQ** e în fila *Calitatea
  energiei*.
- **Reguli** — controlerul declarativ: câte un card pe regulă, cu starea,
  cerut → real, deciziile, Armează / În shadow (vezi §14c).
- **Șabloane** — catalogul din spatele dispozitivelor: **hărți de
  dispozitiv** (cum se citește un echipament real) și **emulări de contor**
  (șabloanele contoarelor virtuale din §11) — incluse, încărcate, importate
  din CSV/YAML, editate.
- **Status** — sănătatea fluxului, tipurile de erori, evenimente, alerte,
  resursele folosite (vezi §17).
- **Setări** — setările gateway-ului, câte o filă: *MQTT*, *InfluxDB*,
  *OPC UA* (§11d), *Backup și restaurare* (snapshot-uri, revenire, export —
  §15), *Securitate* (autentificare, roluri, passkeys, HTTPS, scrieri Modbus
  — §16), *Alerte* (§12), *General* (fus orar, culori), *Amplasament* (locația și
  atributele lui — §5c) și, când e configurat un serviciu
  însoțitor, *Module*.
- **Contoare virtuale** — servesc valorile live ca un contor Modbus
  standard (vezi §11).
- **Diagnostic** — trusa de punere în funcțiune: monitorul de magistrală,
  sonda de registre, descoperirea, scanarea SunSpec (vezi §13).

Când gateway-ul e actualizat cât timp o pagină e deschisă, o bară anunță
asta: **Reîncarcă** încarcă versiunea nouă, **Mai târziu** ascunde bara până
la următoarea actualizare. Pagina nu se reîncarcă niciodată singură —
salvează întâi ce editezi.

---

## 5. Dispozitive și șabloane de dispozitiv

O instalare citește **mai multe surse**. Fiecare dispozitiv leagă o
**conexiune** de un **șablon de dispozitiv** (harta de registre a acelui tip
de echipament) și de **propria rutare a datelor** (prefixul de topic MQTT,
bucket-ul și tag-ul InfluxDB, comutatoarele ieșirilor).

### 5.1 Descoperire — găsește întâi dispozitivul

Dispozitive → *Descoperă dispozitive*:

- **Scanare Modbus TCP** — parcurge o rețea privată (maxim /24) pe un port
  (implicit 502) după dispozitive care răspund. Doar citire; limitată la LAN.
- **Parcurgere de unit ID** — încearcă ID-urile de unitate/slave (implicit
  1–32) pe un host TCP sau pe o linie serială RTU.
- **Scanare SunSpec** — parcurge lanțul de modele SunSpec (markerul `SunS`
  la 40000/50000/0, apoi fiecare model declarat, inclusiv blocul de
  identitate). Primul pas firesc pentru echipamente Fronius/SolarEdge/Huawei.
- **Scanare ESPHome** — parcurge LAN-ul pe portul API-ului nativ (6053) și
  întreabă fiecare nod cine este (nume, versiune; nodurile cu API criptat
  sunt totuși găsite). Merge din Docker — scanarea e unicast, deci nu
  depinde de mDNS/multicast, care nu trece de bridge-ul Docker.
- **Răsfoire topicuri MQTT** — se conectează la un broker și arată
  topicurile active cu o previzualizare a mesajelor (cele reținute apar
  imediat; o scurtă ascultare prinde publicatorii live), ca să alegi un
  topic în loc să-l scrii.
- **Descoperire Fronius Solar API** — enumeră invertoarele și contoarele din
  spatele unui DataManager, cu indicația că contorul e la unitatea Modbus 240.

*Folosește* pe orice rezultat pre-completează asistentul Adaugă dispozitiv.

### 5.2 Adaugă un dispozitiv (asistent)

1. **Conexiune** — alege protocolul:
   - **Modbus TCP**: host, port, unit ID, timeout. Un slave din spatele unui
     convertor în modul *Modbus TCP to RTU* (gateway) se adaugă tot aici:
     **Accesat → Prin gateway-ul …** alege magistrala gateway-ului și rămâne de
     scris doar unit ID-ul. **Prin UDP** e pentru puținele dispozitive al
     căror manual spune Modbus peste UDP.
   - **Modbus RTU**, trei moduri ([rtu-serial.md](rtu-serial.md)):
     - **Prin rețea (recomandat)**: o magistrală din spatele unui
       **bridge** (bridge-ul nostru serial pe un Raspberry Pi sau server,
       ori un convertor transparent RS-485–Ethernet), aleasă după bridge și
       magistrală. Bridge-urile se adaugă cu **Adaugă bridge** în pagina
       Dispozitive ([rtu-over-network.md](rtu-over-network.md)).
     - **Serial direct**: portul serial (de ex. `/dev/ttyUSB0`), baud,
       paritate, biți de stop, cu adaptorul mapat în container.
       **Framing**: RTU, sau ASCII pentru dispozitivele mai vechi care
       vorbesc Modbus ASCII ([rtu-serial.md §5.1](rtu-serial.md#51-modbus-ascii)).
     - **Doar ascultare (tap)**: pentru o magistrală care are deja un master
       (pachetul master al unui BMS, datalogger-ul unui producător).
       Gateway-ul nu transmite niciodată; decodează răspunsurile unității
       observate, în ritmul masterului. Magistrala poate fi un port serial
       local sau magistrala unui bridge transparent. Nu există *Testează
       conexiunea*; după salvare, *Test* pe dispozitiv spune ce a auzit
       tap-ul ([rtu-serial.md §7](rtu-serial.md)).

     Mai mulți slave pe o magistrală sunt mai multe dispozitive, cu unit
     ID-uri diferite: împart o conexiune și își așteaptă rândul. O
     magistrală e fie interogată, fie ascultată. Fiecare magistrală din
     cardul unui bridge arată cât de ocupat i-a fost firul în ultimul minut
     — *ocupat 37 % · capacitate rămasă ~63 %* — măsurat pe fir (timpul de
     așteptare a rândului nu se numără). Când magistrala nu mai face față
     scrie *peste capacitate*: încetinește grupurile de citire care nu au
     nevoie de viteză, crește viteza (baud) sau împarte slave-urile pe două
     magistrale. Treci cu mouse-ul peste bară ca să vezi partea fiecărui
     dispozitiv.
   - **Schimbarea ulterioară:** în pagina dispozitivului, **Configurare →
     Conexiune** este același formular. Un dispozitiv Modbus poate trece
     între Modbus TCP, UDP, RTU, ASCII, prin rețea și doar ascultare,
     pentru că harta lui citește oricum după adresă. ID-ul, topicurile și
     istoricul rămân. Hărțile HTTP și MQTT citesc după cale, deci trecerea
     spre sau dinspre ele cere un dispozitiv nou. Contorul principal rămâne
     pe Modbus TCP (adresa i se poate schimba), iar un dispozitiv care are
     componente își păstrează felul în care e accesat.
   - **Salvarea:** Salvează trimite doar ce ai schimbat — un formular
     neatins nu trimite nimic. Schimbarea *felului* în care e accesat e
     explicată lângă alegere. Alt protocol (UDP sau ASCII pornit ori oprit,
     TCP spre RTU) pornește conexiunea de la zero: setările pe care
     formularul nu le arată (reîncercări, reglaje) revin la valorile
     implicite. Mutarea pe un bridge sau de pe el le păstrează: pe un
     bridge, adresa și linia serială proprii se golesc (le are bridge-ul);
     de pe el, primește adresa pe care o scrii (portul 502 dacă nu-l
     schimbi). Un dispozitiv de pe un bridge îl părăsește doar când alegi
     altă cale (o adresă directă, un port serial pe acest host sau *Scoate-l
     de pe bridge…*); alegerea altei magistrale îl mută și îi păstrează
     topicurile și istoricul. Un timeout Modbus e între 1 și 30 s; unul
     salvat în afara plajei (scris de mână) rămâne până îl schimbi. Un refuz
     al gateway-ului apare sub câmpul la care se referă.
   - **HTTP/JSON**: un URL care întoarce JSON; fiecare registru își scoate
     valoarea cu un `json_path` (de ex. `Body.Data.PowerReal_P_Sum`).
     URL-urile trebuie să ducă la un host din LAN-ul privat, dacă nu e
     setat `security.allow_nonlan_http_devices` (protecție SSRF).
   - **MQTT**: broker, port, autentificare/TLS, un topic de abonare; fiecare
     registru citește din mesajul JSON după `json_path` sau ia mesajul gol
     ca număr, și poate avea topicul lui, cu wildcard-uri `+`/`#`.

   Apasă **Testează conexiunea**: pentru Modbus, *orice* răspuns la nivel de
   protocol — chiar și o excepție — dovedește un dispozitiv viu; pentru HTTP
   cu un șablon ales, testul spune câte `json_path`-uri au fost găsite;
   pentru MQTT se conectează și așteaptă scurt un mesaj. La un dispozitiv
   salvat, Test întreabă dispozitivul așa cum rulează cât timp nu e nimic
   schimbat; odată ce ai schimbat ceva, încearcă conexiunea așa cum o ține
   formularul — unitatea și magistrala editate pe un bridge, adresa scrisă
   după ce l-ai părăsit, variabilele de topic și keepalive-ul incluse — cu
   parola salvată când câmpul e lăsat gol. Test nu salvează nimic. Un tap
   de ascultare schimbat nu e sondat (nu transmite niciodată): salvează-l,
   apoi Test spune ce aude.
2. **Șablon** — alege din bibliotecă (hărțile incluse sunt în
   [device-catalog.md](device-catalog.md)). Ca să scrii unul din lista de
   registre a producătorului, urmează **[device-templates.md](device-templates.md)**:
   adrese, ordinea cuvintelor, scala, editorul, CSV/YAML și testarea, cu
   exemple. Poți **încărca** un șablon `.json` (verificat rând cu rând; la
   ID-uri în conflict ești întrebat înainte de suprascriere), **crea** unul
   în editor sau **importa o hartă CSV sau YAML**
   ([csv-import.md](csv-import.md), [yaml-import.md](yaml-import.md)).
   Cele incluse sunt doar în citire — *Duplică pentru editare*. Un șablon
   folosit de un dispozitiv nu poate fi șters.
3. **Rutarea datelor** — **id**-ul dispozitivului devine cheia de rutare:
   valorile se publică sub prefixul de topic MQTT al dispozitivului
   (previzualizare live) și ajung în bucket-ul lui InfluxDB (creat automat,
   păstrare 90 de zile), cu tag-ul lui de dispozitiv. **Atenție:** identitatea
   de rutare e fixă după creare — schimbarea ei ar lăsa orfane istoricul și
   entitățile Home Assistant.

După creare, dispozitivul începe imediat să citească: șabloanele îngrijite
își aleg singure setul recomandat de registre (harta Janitza alege 58 din
4.126); un șablon neîngrijit cu peste 300 de registre nu alege nimic, ca să
nu inunde MQTT/InfluxDB cu mii de serii dintr-un clic.

**Instalări existente:** contorul inițial apare automat ca dispozitivul #1,
cu identitatea de rutare blocată — topicurile, bucket-ul, tag-urile și
entitățile HA rămân identice.

**Ștergerea unui dispozitiv** îi păstrează fișierul de registre pe disc
(siguranța datelor) și e refuzată cât timp ceva îl folosește — o regulă
care îl comandă sau îl citește, un pas de secvență, câmpul calculat al altui
dispozitiv, un dispozitiv pe care îl măsoară, un contor virtual pe care îl
alimentează, o formulă de șablon care îl numește. Pagina lui le arată sub
**Folosit de**; schimbă-le întâi sau **dezactivează** dispozitivul (toate
referințele rămân neatinse).

### 5.3 Cum se arată o unitate — decide șablonul

Ce arată interfața pentru un tip de dispozitiv vine din blocul `display` al
șablonului, nu dintr-o listă din cod: cum se numește o unitate (*Pachet de
baterii*), coloanele rândului ei în pagina instalației, valorile ei în
flotă, titlul principal al instalației, care câmpuri sunt alarme, care
secțiuni ale tabloului sunt o **grilă** (celulele unui pachet ca plăcuțe,
cea slabă și cea plină marcate, citite la milivolt) sau arată **doar ce e
activ** (34 de alarme la zero devin *Nimic activ*) și care valori sunt
**măști de biți** (*Celula 3, Celula 7* în loc de `68`). Blocul e citit
live: îmbunătățești șablonul și fiecare unitate îl arată imediat. Scrie-l pe
al tău într-un șablon
([config-reference.md — `display:`](config-reference.md#display--how-a-unit-of-this-kind-is-shown));
fără el, decide rolul dispozitivului, ca înainte.

Etichetele, unitățile de măsură, descrierile, categoriile și totalurile, în
schimb, sunt **copiate** în unitate la creare. Când un șablon le
îmbunătățește, pagina instalației (și lista de dispozitive, cu insigna
*actualizare șablon*) spune **Șablonul a fost actualizat** — doar atunci.
**Revizuiește și aplică** (admin) arată fiecare schimbare cu valoarea veche
și cea nouă și câte unități atinge, plus câmpurile calculate pe care
șablonul le-a primit (bifează-le ca să le adaugi), și le aplică pe toate
unitățile odată. Le duce pe rândurile pe care unitatea le are deja;
selecția, marcajele, ieșirile, pragurile și formulele tale rămân cum sunt.
Butonul e în pagina instalației; un dispozitiv de sine stătător se
reîmprospătează prin API (`POST /api/devices/{id}/refresh-from-template`).

### 5.4 Electricitate măsurată de alte dispozitive — contoare cu roluri

O pompă de căldură, un boiler sau un încărcător adesea nu are contor
propriu; îl măsoară un contor de pe alimentarea lui. Un UPS sau un convertor
are un contor pe intrare și unul pe ieșire. **Dispozitiv → Contoare** le
numește, fiecare într-un rol:

| Rol | Când | Ce primește dispozitivul |
|---|---|---|
| **Consum** | singurul contor de pe alimentare | `power_electric` (W, mereu pozitivă) și `energy_electric` (contorul de energie al contorului, în unitatea lui) |
| **Intrare** + **Ieșire** | câte un contor pe fiecare parte (un UPS, un încărcător, un convertor) | `power_in`, `power_out` (W), `energy_in`, `energy_out` când legăturile numesc un contor de energie, `power_loss` (W), `efficiency_pct` și `load_pct` (%, față de puterea nominală a dispozitivului, atributul `rating`) |

Pentru fiecare contor alegi dispozitivul, câmpul lui de putere și,
opțional, contorul lui de energie; lista arată doar câmpuri măsurate în
W/kW (putere) și Wh/kWh (energie). **Semn**: *auto* citește puterea pozitivă
indiferent cum e montat contorul; *as is* și *invert* o spun explicit. Un
contor poate servi mai multe dispozitive; un lanț de contoare nu se poate
întoarce la dispozitiv.

Valorile se calculează când raportează contorul, deci un dispozitiv fără
comunicație proprie e totuși măsurat. `power_loss`, `efficiency_pct` și
`load_pct` se publică doar când se poate avea încredere în ele — ambele
citiri proaspete și luate apropiat în timp, intrarea stabilă, ieșirea nu
peste intrare dincolo de eroarea contoarelor, ieșirea peste o sarcină
minimă. Altfel valoarea lipsește (topicul ei MQTT e golit), iar pagina
dispozitivului spune de ce. Două setări de sub contoare ajustează asta:
**Sarcină minimă** (implicit 100 W) și **Toleranța contoarelor** (implicit 2 %).

Se publică și se stochează cu dispozitivul, ca valorile lui proprii. Fila
lui **Energie** numără `energy_electric`, iar șablonul lui poate calcula cu
ele: harta Samsung MIM-B19N calculează `cop_estimated =
heat_output_estimated / power_electric`.

**Un dispozitiv fără nicio conexiune** — un UPS fără comunicație, o
sarcină pe care doar o măsori — se adaugă cu **Adaugă dispozitiv → Fără
conexiune: măsurat prin contoare**: nu citește nimic singur, iar contoarele
lui sunt felul în care e măsurat.

Un dispozitiv care se măsoară singur (un invertor, o pompă de căldură cu
contor integrat) nu are nevoie de nimic aici. Nu e disponibil pentru
unitățile unei instalații. API: `GET /api/meter-roles` (rolurile și
setările), `PUT /api/devices/{id}/meters` cu `{meters: [{role, device,
power, energy?, sign?}], options?}`; `[]` scoate toate legăturile. Vechiul
`PUT /api/devices/{id}/energy-meter` setează încă un singur contor de consum.

---

## 5b. Instalații — un datalogger, mai multe unități

O centrală fotovoltaică e un amplasament în spatele unui singur dispozitiv
master — un Fronius DataManager, un logger Deye/Huawei, un bridge RS-485
multi-drop — care stă în fața mai multor invertoare, a contorului de la
racordul la rețea și uneori a unei baterii. O **instalație** (blocul
`endpoints:`) declară asta o singură dată: **grupuri** de unități
(invertoare, contor de rețea, totaluri de amplasament…), fiecare citit prin
una sau mai multe **surse** ordonate (Modbus TCP și/sau un apel HTTP în
stilul Solar API). Fiecare unitate devine un dispozitiv obișnuit, cu
identitatea, istoricul și intrarea lui în Home Assistant; primești
preluare automată între surse, totaluri pe grup care nu adună niciodată
invertoare cu un contor, un singur lacăt de scriere și o pagină care se
citește de sus în jos: *produce — e fiecare unitate în regulă — cum e
citită — unde publică*.

### 5b.1 Adaug-o din interfață (asistentul)

Dispozitive → **Adaugă instalație**. Patru pași; nimic nu se creează
înainte de *Creează instalația* din ultimul pas.

1. **Instalația** — numele (ID-ul îl urmează, fix după creare), **cum se
   ajunge la datalogger** (*Solar API*, *Modbus TCP* sau *Ambele* —
   recomandat: cel rapid deține câmpurile pe care le oferă, Modbus
   completează restul), adresa lui și **Publică sub** (rădăcina topicurilor:
   `pv` → `pv/inverters/1/…`). **Test** sondează ce ai bifat; cu Solar API
   testul *este* descoperirea.
2. **Ce conține** — ce a raportat datalogger-ul, ca bife: *Invertoare*,
   *Totaluri de amplasament* (producție · consum · rețea · autonomie),
   *Contor de rețea* (nebifat intenționat: datele de rețea se citesc mai bine
   direct din contor). *Adaugă un grup de mână* dacă datalogger-ul nu a
   răspuns.
3. **Cum se citește** — intervale cu nume pe fiecare cale (Solar API:
   putere, tensiuni, curenți la 2 s, contoarele de energie la 30 s; Modbus:
   citirea completă la 20 s, contoarele și datele statice la 120 s),
   minimul măsurat când datalogger-ul e lent și — cu ambele căi — cât timp
   Solar API rămâne sursa de autoritate înainte ca Modbus să-și ia câmpurile
   înapoi (30 s).
4. **Verificare** — grupurile, unitățile, sursele în ordinea priorității,
   topicurile reale și ID-urile dispozitivelor care vor fi create.

Refuzate înainte să se creeze ceva: un ID greșit, un grup fără unități sau
fără șablon, un grup pe care nu-l poate citi nimic și un unit ID revendicat
de două grupuri (o adresă pe fir, două dispozitive care se întrec pe ea).

### 5b.2 Același lucru în `config.yaml`

```yaml
endpoints:
  - id: pv
    name: Fronius PV
    enabled: true
    mqtt:
      topic_prefix: pv/units/${unit_id}            # implicit pe unitate; un grup îl poate suprascrie
      aggregate_prefix: pv/${group_id}/summary     # totalurile grupului (implicit mbg/endpoints/<id>/…)
    influxdb: { bucket: pv, device_tag: "${device_id}" }
    groups:
      - id: inverters
        role: inverter
        units: [1, 2, 3, 4]                        # dispozitivele pv-u1 … pv-u4
        mqtt: { topic_prefix: pv/inverters/${unit_id} }
        sources:                                   # ORDINEA ESTE PRIORITATEA
          - id: solar_api
            protocol: http
            url: "http://192.168.1.50/solar_api/v1/GetInverterRealtimeData.cgi?Scope=Device&DeviceId=${unit_id}&DataCollection=CommonInverterData"
            template: fronius_solar_api_inverter
            timeout: 5
            poll_groups: { realtime: { interval: 2 }, normal: { interval: 30 } }
            stale_after_s: 30                      # tăcut 30 s → Modbus îi ia câmpurile
          - id: sunspec
            protocol: tcp
            host: 192.168.1.50
            port: 502
            timeout: 3
            template: fronius_sunspec_inverter
            poll_groups: { normal: { interval: 20 }, slow: { interval: 120 } }
            stale_after_s: 0                       # nu cedează niciodată — potrivit pentru contoare
        commands: [{ name: power_limit }, { name: restore }]   # §14b
      - id: grid                                 # alt tip de unitate: șablon propriu, total propriu
        role: meter
        units: [{ unit_id: 240, id: pv-meter-240, name: Grid meter }]
        sources: [{ id: sunspec, protocol: tcp, host: 192.168.1.50, port: 502,
                    template: fronius_sunspec_meter, poll_groups: { normal: { interval: 5 } } }]
    write_locked: false                            # o instalație, un lacăt — pentru fiecare unitate
```

`${unit_id}`, `${endpoint_id}` și `${device_id}` se înlocuiesc pe fiecare
unitate în prefixul de topic, bucket, tag, nume **și** conexiune (`url`,
`host`). O instalație fără `groups:` e un singur grup implicit care își ține
`units:` / `sources:` / `connection:` direct, așa că intrările mai vechi merg
neschimbate. Fiecare câmp: [config-reference.md](config-reference.md).

### 5b.3 Ce primești

- **Dispozitive**: câte unul pe unitate (`<id>-u<unitate>`; asistentul
  numește unitățile de amplasament `<id>-site`, contoarele descoperite
  `<id>-meter-<unitate>`), fiecare cu socket-ul lui, selecția de registre pe
  sursă, bucket-ul și dispozitivul HA, gestionate *prin* instalație —
  crearea, editarea și ștergerea unui dispozitiv refuză ID-urile lor.
- **Pagina instalației**: starea și numărătoarea; *produce acum / azi /
  autonomie / autoconsum* (din grupul de amplasament, altfel suma
  invertoarelor, altfel liniuțe); **Citită prin** — fiecare sursă cu
  protocolul, intervalul, latența și rata de eșec în ultimele cinci minute;
  apoi **câte un card pe grup**: tabelul unităților (starea în cuvinte,
  ultima citire, verdictul fiecărei surse pe unitate), totalul și topicul
  lui și tabelul surselor cu săgeți, pentru că **ordinea este prioritatea**:
  prima sursă care oferă un câmp îl furnizează, una de mai târziu
  completează doar după ce cea dinainte a tăcut mai mult decât *învechit
  după*. **Testează unitățile** întreabă fiecare unitate prin fiecare sursă;
  unitățile, sursele și topicurile se editează pe cardurile grupurilor.
- **Pagina unei unități**: **Citită prin** înlocuiește *Conexiune*;
  **Măsurători** arată un selector de sursă când unitatea e citită pe mai
  multe căi (fiecare sursă cu harta și bifele ei); **Comenzi** (§14b). O
  unitate citită pe două căi e `ok` doar cât timp sunt toate în regulă: o
  sursă căzută → `degraded` (*„citit bine prin HTTP, partea Modbus nu
  răspunde”*), niciuna → `down`, nimic selectat → `idle`; trecerile sunt
  evenimente (`source_down` / `source_ok`) în Jurnalul ei.
- **Topicuri**: valorile unității sub prefixul ei, plus frunzele de viață
  pe care le are orice dispozitiv (`availability`, `runtime/status`,
  `runtime/last_seen`, `runtime/read_errors`). **Totalurile grupului** la
  fiecare 10 s pe topicul de totaluri al grupului (implicit
  `mbg/endpoints/<id>/…`, grupurile următoare `mbg/endpoints/<id>/<grup>/…`,
  sau `aggregate_prefix`-ul tău): puterile și curenții adunați,
  tensiunile/frecvența/temperaturile mediate, contoarele de energie adunate
  din valorile *ultimele cunoscute* și publicate doar când fiecare unitate
  activă are una, plus `units_online`, `units_total` și `status` (`online` /
  `partial` / `offline`) la **fiecare** ciclu. Un grup cu o singură unitate
  nu publică total. Aceleași totaluri ajung în InfluxDB cu tag-urile
  `device=<id instalație>, aggregate=endpoint`. `GET /api/endpoints/{id}`
  întoarce configurația și starea live într-un singur obiect.

### 5b.4 Note de siguranță

- **Identitatea de rutare e fixă după creare** (prefixul de topic,
  bucket-ul, tag-ul de dispozitiv) — schimbarea ei ar lăsa orfane istoricul
  și entitățile HA (tag-urile statice, totalurile și ieșirile suplimentare
  din §5b.5 rămân editabile); ID-ul scris de mână al unei unități
  supraviețuiește regrupării din același motiv. **Lacătul de scriere,
  ieșirea HTTP și REST push se declară o dată** și se aplică fiecărei
  unități; scrierile merg la prima sursă care le poate face — o sursă Solar
  API e doar în citire.
- **Editările nu fac găuri în achiziție**: o editare doar de setări (nume,
  comutatorul totalurilor, numele afișat al unei unități) lasă toate
  citirile să ruleze; o schimbare a felului în care se citesc unitățile
  repornește doar unitățile pe care le schimbă. Un grup dezactivat rămâne
  vizibil și editabil; doar nu mai citește.
- **Datele vechi nu devin niciodată o valoare.** Un câmp pe care nu-l oferă
  nimeni lipsește (noaptea DataManager-ul *omite* `PAC`/`UAC`… în loc să
  trimită zero; șablonul refuză să inventeze unul); o unitate blocată iese
  dintr-un total de putere, dar niciodată dintr-un contor de energie, așa că
  totalurile nu merg niciodată înapoi.
- **Restartul gateway-ului**: unitățile revin din `config.yaml` cu
  selecțiile salvate și citesc imediat; topicurile reținute păstrează
  ultimele valori până atunci — condiționează consumatorii pe
  `availability`, nu pe o valoare (§18c). **Repornirea sau somnul de
  noapte al datalogger-ului**: sursele devin `down`, unitățile `down`,
  grupul `offline` (publicat chiar și când nimic nu e proaspăt); la
  revenire, prima sursă care răspunde își ia câmpurile înapoi, în ordinea
  priorității.
- **Ștergerea** păstrează fișierele de registre ale unităților pe disc și e
  refuzată cât timp ceva folosește instalația sau una dintre unități (o
  regulă, un contor virtual, o formulă — listate sub **Folosită de**);
  dezactiveaz-o în loc. **Datalogger-ul e comun și lent**: unitățile de pe
  un `host:port` sunt serializate (`serialize_endpoint`),
  `endpoint_min_gap_s` adaugă o pauză, `max_connections` se stabilește prin
  măsurare (`scripts/calibrate_endpoint.py`); un rând ratat e un eveniment
  `bus_busy`, nu o defecțiune a dispozitivului.

### 5b.5 Totalurile, tag-urile și ieșirile InfluxDB proprii

Cum se fac totalurile și unde se scriu decizi tu, în pagina instalației —
nimic din asta nu e fixat în cod:

- **Cum se fac totalurile** arată fiecare total cu operația lui și
  câmpurile unităților din care e construit, marcat *șablon* sau *al tău*.
  Editează unul ca să schimbi ce îl alimentează (temperatura maximă a unui
  banc de baterii peste senzorii celulelor *și* cel de ambient) sau
  **Adaugă total** pentru unul nou (`sum`, `avg`, `min`, `max`, `spread`
  peste orice câmpuri au unitățile, sau `mode` — valoarea majoritară,
  inclusiv text). Un total al tău îl înlocuiește pe cel din șablon cu
  același nume; **Înapoi la șablon** renunță la versiunea ta.
- **InfluxDB — tag-uri pe punctele fiecărei unități**: tag-uri statice ca
  `battery_id = ${unit_id}`, înlocuite pe fiecare unitate și scrise pe
  punctele brute și calculate. Un tag face parte din identitatea unei serii
  — schimbarea lui începe o serie nouă.
- **Totalurile se scriu și în**: ieșiri InfluxDB suplimentare — o
  măsurătoare, un bucket (gol = al instalației), când se scrie (la schimbare
  sau la fiecare ciclu), tag-uri statice și o mapare de câmpuri
  *nume ← total × scală*. Așa un tablou sau un raport care citește o
  măsurătoare proprie merge mai departe când se schimbă dispozitivul din
  spate (de ex. `total_power ← pack_total_power`, `energy_remaining ←
  pack_energy_remaining × 0.001` pentru kWh).

Salvarea unei ieșiri nu repornește niciodată o unitate; schimbarea
tag-urilor da. O editare a felului în care citește un grup (adresa unei
surse, intervalul ei) repornește doar unitățile acelui grup — celelalte
unități ale instalației citesc mai departe — iar salvarea spune pe care le-a
repornit. Salvarea unei instalații neschimbate nu schimbă nimic. O intrare
greșită e refuzată, cu numele câmpului. Toate trei sunt și în `config.yaml`
(`totals:`, `influxdb.tags`, `influxdb.outputs` —
[config-reference.md](config-reference.md)).

### 5b.6 Depanare

| Simptom | Verifică |
|---------|-------|
| Asistentul refuză un grup | fără unități, fără șablon sau fără o cale de citire — bifează o cale în pasul 1; sau același unit ID e în două grupuri |
| O sursă Solar API răspunde, dar unitatea nu citește nimic | un URL scris de mână cu o interogare greșită întoarce tot 200 OK, cu corp gol — folosește apelul presetat (`…&DeviceId=${unit_id}&DataCollection=CommonInverterData`) |
| Unitățile oscilează `ok ↔ degraded` toată ziua | intervale prea rapide pentru datalogger: Solar API ≥ 5 s pe unitate, SunSpec `normal` 20–60 s; coloana *eșuate (5 min)* spune care sursă |
| Latența Modbus crește, apoi citirile cad în rafale | datalogger-ul cedează peste ~26 de tranzacții pe minut: încetinește grupurile Modbus, setează `endpoint_min_gap_s` (0,3 s), citește câmpurile rapide prin HTTP |
| `units_online` < `units_total` noaptea | normal: invertoarele care dorm sunt `down`; contoarele de energie își păstrează ultima valoare, totalul spune `partial` / `offline` |
| Un total nu apare niciodată | grupul are o singură unitate (fără total, intenționat), sau unei unități active îi lipsește un contor de energie — suma e reținută, nu publicată incompletă |
| O unitate nu are fila *Conexiune* | corect: conexiunea ei e pe surse — folosește *Citită prin* și editorul de surse din cardul grupului |

---

## 5c. Atributele echipamentului — ce este echipamentul

Fapte pe care nu le poartă nicio măsurătoare, pentru sistemele care citesc
gateway-ul (un EMS, un HMI, un SCADA):
- **unde e amplasamentul**: Setări → **Amplasament**;
- **un contor este racordul la rețea**, cu limitele lui de import și
  export: fila **Atribute** a dispozitivului;
- **intrările PV ale unui invertor**: panouri, kWp, orientare, înclinare,
  câte un rând pe orientare;
- **energia utilizabilă și limitele unei baterii**;
- **puterea nominală a unui invertor**.

Apeși **+** și alegi din catalogul gateway-ului; nimic nu e obligatoriu.
Coordonatele sunt arătate aproximativ oricui nu e admin și sunt scoase
dintr-un backup curățat. Un EMS le poate seta pe toate prin API, cu un token
de serviciu de admin. Ghidul complet: [attributes.md](attributes.md).

---

## 6. Registre, grupuri de citire și praguri

Pagina dispozitivului → **Măsurători**:

- **Toate disponibile** — catalogul din șablonul dispozitivului (răsfoire/căutare;
  4.126 de intrări pentru Janitza). Adaugă de mână un **registru propriu**
  dacă harta nu îl are.
- **Bifate** — ce se citește efectiv. Pe fiecare registru: grupul de
  citire, tipul de date, scala (valoarea inginerească = brut ÷ scală), tipul
  de registru (holding FC3 / input FC4 / coil FC1 / discrete FC2), topicul
  MQTT, măsurătoarea și tag-urile InfluxDB, widget-ul de tablou și
  **pragurile**.

**Grupurile de citire** (`realtime` / `normal` / `slow` implicit) citesc
fiecare la intervalul lor — editabil pe dispozitiv între 0,05 s și 24 h,
aplicat live. Registrele dintr-un grup sunt unite în citiri pe bloc, care
trec peste goluri de până la `max_gap` (implicit 10); slave-urile stricte
care refuză blocurile unite cu *illegal data address* ar trebui să aibă
`max_gap: 0`.

**Pragurile** colorează o valoare (warningLow/High, dangerLow/High) pe
tablou și pe indicatoare. Sunt pe registru — și aceleași limite pot declanșa
**alerte** la trecerea lor: activează motorul de praguri inclus
(`alerts.signals.threshold`, vezi §12).

Salvarea selecției reîncarcă pe loc doar citirile acelui dispozitiv.

### 6.1 Nume canonice de câmpuri

Ca automatizările, tablourile și predicțiile să rămână previzibile, **numele**
unui registru vine dintr-un dicționar comun — aceeași mărime fizică are
același nume pe orice dispozitiv (`voltage_l1_n` peste tot, nu `ull_0` pe un
contor și `v_l1` pe altul). Numele devine frunza topicului MQTT, câmpul
InfluxDB și tag-ul `name`; topicul MQTT e ierarhic (`voltage/l1_n`), iar
câmpul InfluxDB e numele canonic plat. Lista completă e în
[`docs/canonical-fields.md`](canonical-fields.md) (56 de câmpuri). Convenția:
`<mărime>_<poziție>` — `voltage_l1_n`, `current_l2`, `power_active_total`,
`energy_active_import`, `frequency`, …

- **În editorul de registre**, câmpul *Nume* se completează din dicționar,
  marchează un nume necanonic cu o sugestie „ai vrut să spui …?” și
  completează topicul MQTT, măsurătoarea InfluxDB, **unitatea de măsură** și
  **categoria** când numele e canonic — fiecare doar cât timp e gol sau încă
  e ce a pus dicționarul; ce ai scris tu rămâne.
- **În editorul de șabloane** (Șabloane → *Hartă nouă* / *Editează*),
  numele fiecărui rând e verificat live, se arată un rezumat „**N/M
  canonice**”, iar **Auto-canonizează** deduce nume canonice pentru o hartă
  importată criptică, din eticheta și unitatea fiecărui rând. E prudent
  intenționat — ce nu știe sigur rămâne galben ca să-l setezi tu, niciodată
  redenumit greșit. Verifică grila, apoi Salvează. Alegerea unui nume canonic
  (de mână sau cu Auto-canonizează) completează **unitatea de măsură** goală
  a rândului din dicționar (`voltage_l1_n` → V) și, pe un rând abia adăugat,
  **categoria** lui (`voltage`); o unitate sau o categorie scrisă de tine nu
  e suprascrisă niciodată. Un rând existent își păstrează categoria:
  categoria e locul unde stă istoricul rândului în InfluxDB, iar redenumirea
  unui rând nu trebuie să-l mute.
- Un șablon promite nume canonice cu `"canonical": true`; numele necanonice
  sau duplicate apar atunci ca avertisment în managerul de șabloane.

**Unitatea de măsură face parte din contract.** Fiecare câmp canonic are o
unitate canonică (vezi coloana Unit din
[`canonical-fields.md`](canonical-fields.md)); energia e în **familia Wh**
(Wh/varh/VAh). Un contor a cărui hartă nativă e în kWh face conversia în
**scala** selecției (`scale/1000`) — valoarea publicată trebuie să fie deja
în unitatea canonică, altfel fiecare tablou între dispozitive, fiecare
legare de contor virtual și fiecare dublură de preluare moștenește o eroare
tăcută de ×1000. Două protecții impun asta:

- **În editorul de șabloane**, celula *Unitate* arată unitatea canonică ca
  indiciu pentru numele canonice și devine **galbenă live** când unitatea
  scrisă diferă, cu remedierea explicată (ajustează scala sau redenumește
  rândul). Un buton **→ Wh** de lângă ea setează unitatea canonică dintr-un
  clic — după ce scala chiar o livrează (un kWh nativ are nevoie întâi de
  `scale/1000`).
- **La salvare**, selectarea unui nume canonic cu unitate nepotrivită scrie
  un avertisment în jurnal (salvarea merge — avertismentul e alarma).

Hărțile de referință ale producătorilor (de ex. Janitza UMG512) sunt mai
vechi și își păstrează numele native — schema se aplică șabloanelor pe care
le construiești sau le imporți.

### 6.2 Igiena citirii și decodării (opțional)

Tot ce e aici e **oprit implicit** — pornește-l global, pe dispozitiv sau pe
registru, când echipamentul o cere.

- **Decalaj la pornire** — `polling.startup_jitter_s` (valoarea globală) sau
  `startup_jitter_s` sub `connection:` a unui dispozitiv. Fiecare grup de
  citire așteaptă o întârziere aleatoare în `[0, min(interval, jitter)]`
  înaintea **primei** citiri, ca mai multe dispozitive/grupuri să nu
  pornească în același timp și să lovească un transport comun (mai ales un
  bridge serial RTU prin rețea) la pornire. `0` = oprit.
- **Lista de registre ilegale** — `illegal_registers` sub `connection:` a
  unui dispozitiv: adresele la care slave-ul răspunde cu *illegal data
  address* (excepția 02). Citirile pe bloc nu trec niciodată peste una, iar
  un registru selectat aflat pe una e sărit — soluția pentru o gaură
  *înăuntrul* unui bloc continuu, pe care `max_gap: 0` nu o rezolvă.
  Zecimal sau `0x…`.
- **Filtrul de cadre numai cu zero** — `drop_all_zero: true` pe un
  dispozitiv: un dispozitiv adormit (un invertor noaptea) poate răspunde cu
  un cadru NUMAI CU ZERO în loc de o eroare; publicat ca atare, se citește
  ca 0 V / 0 W reali. Cu filtrul pornit, un grup de citire ale cărui valori
  numerice sunt toate exact zero (și sunt ≥2) e aruncat, iar memoria
  păstrează ultimele valori bune până se trezește dispozitivul.

```yaml
polling:
  startup_jitter_s: 2          # valoarea globală pentru toate dispozitivele
devices:
  - id: inverter
    connection:
      host: 192.168.1.60
      startup_jitter_s: 5      # suprascriere pe dispozitiv
      illegal_registers: [0x2100, 505]
      drop_all_zero: true      # doarme noaptea
```

- **Opțiuni de decodare pe registru** (editorul de registre/șabloane sau
  JSON-ul șablonului):

| Cheie | Efect |
|---|---|
| `offset` | valoarea inginerească = `brut / scală + offset` — deplasări de zero sau de unitate (de ex. Kelvin×10 → °C cu `scale: 10, offset: -273.15`); ignorat pentru rândurile enum/bitfield |
| `nan` | marcaj de indisponibil: `true` = valoarea SunSpec a tipului de date (`0x8000`/`0xFFFF`…), sau o valoare brută explicită / o listă. O potrivire se citește ca *lipsă*, niciodată ca un număr fără sens (−32768 °C). Float NaN/Inf e aruncat mereu |
| `monotonic: true` | contoare cumulative (Wh/kWh/varh): un salt neplauzibil **în oricare direcție** e aruncat (memoria păstrează ultima valoare bună) — o cădere, dar și un salt în sus >50% peste bază (un cuvânt superior corupt ar injecta altfel GWh fantomă). O resetare reală și susținută e adoptată doar după citiri de confirmare coerente (la 5% una de alta) și scrie un WARNING cu noua bază |
| `enum` / `bits` (+ `mask`/`shift`) | decodează un cuvânt de stare în text — `enum: {7: "Fault"}` (nemapat → `unknown (n)`), `bits: {0: "overvoltage"}` alătură numele biților setați; se construiește vizual cu butonul **Decodează stări** din editorul de șabloane. Hărțile incluse îl folosesc și pentru registrele de identitate (de ex. codul de detecție EM24 1651, ID-ul de model Fronius 65A 731) |

Aceste opțiuni se aplică **identic pe orice transport** — sursele Modbus,
HTTP/JSON și MQTT trec prin aceeași cale de la fir la valoare (marcaj →
enum/biți → scală+offset → monotonic), așa că un contor de energie primit
prin MQTT (Shelly, Zigbee2MQTT…) are aceeași protecție la depășire ca unul
Modbus. Când o valoare e aruncată, motivul e una din trei etape vizibile:
`sentinel` (declarată indisponibilă), `decode_failed` (stare nemapabilă,
scrisă o singură dată în jurnal) sau `filter_drop` (respinsă de monotonic,
în jurnalul de depanare).

---

## 7. Registre calculate

Pagina dispozitivului → **Calculate**: obții măsurători noi printr-o
formulă din valorile live. Exemple:

| Scop | Expresie |
|---|---|
| Factor de putere | `_G_P_SUM3 / _G_S_SUM3` |
| Suma fazelor | `p_l1 + p_l2 + p_l3` |
| Conversie de unitate | `energy_wh / 1000` |
| Dezechilibrul curenților % | `(max(i1,i2,i3) - avg(i1,i2,i3)) / avg(i1,i2,i3) * 100` |
| Puterea medie dintr-un contor de energie | `(E - prev(E)) / dt * 3600` |
| Între dispozitive | `grid.p_total + pv.p_total` (ID-urile cu cratimă merg: `samsung-dvm.x`) |
| Un contor, altfel o estimare | `first(power_electric, power_electric_estimated)` |
| Invertoare care răspund | `count_fresh(pv-u1.power_active_total, pv-u2.power_active_total)` |
| Cât de veche e o valoare | `age(house_load)` (secunde, de la cea mai veche intrare) |
| O valoare doar când intrările sunt proaspete | `(a + b) if count_fresh(a, b) == 2 else absent()` |
| O funcție în trepte | `steps(soc, 0, 15, 30, 25, 50, 40, 75, 60, 100)` |
| Netezită (constantă de timp de 5 minute) | `ema(house_load, 300)` |
| Un prag care nu oscilează | `hysteresis(ruuvi-cabinet.ambient_temp, 35, 40)` |
| Totalul unei instalații | `battery-bank.pack_average_soc` |

- Constructorul oferă etichete de măsurători pe care dai clic, o **paletă
  de funcții** care arată fiecare funcție cu semnătura ei, o
  **previzualizare live** și formule presetate — îți poți salva propriile
  formule ca presetări. Funcțiile, pe familii:
  - aritmetică — `min max avg abs round sqrt pow floor ceil clamp popcount`,
    `pi e`, comparații, `a if cond else b`;
  - valori care pot lipsi — `first fresh count_fresh age absent`;
  - schimbarea în timp — `prev dt ema hysteresis steps`;
  - locul și ceasul — `sun_elevation sun_azimuth is_day`,
    `hour minute weekday day month between`;
  - ce știe gateway-ul — `attr` (§5c) și totalurile unei instalații după
    ID-ul ei.
- `first(a, b, …)` ia primul argument care are o valoare — un contor când e
  legat unul, altfel o estimare; e sărit doar dacă niciunul nu are.
- `fresh(x)`, `count_fresh(…)`, `age(x)`, `absent()` și `steps(…)` sunt
  explicate, cu exemple, în
  [templates-advanced.md](templates-advanced.md#calculated-fields). O
  formulă care sare peste o intrare învechită cu ele nu e marcată veche din
  cauza ei.
- O formulă conține doar ASCII simplu. Un caracter venit dintr-un
  copy/paste (`−`, un spațiu care nu se rupe, ghilimele curbe, un caracter
  invizibil, o literă care doar seamănă) e numit sub câmp și refuzat la
  salvare, cu ce trebuie scris în loc.
- `prev(x)` și `dt` fac posibile calculele de rată; o formulă cu stare nu
  poate fi previzualizată dintr-o dată (interfața spune asta) — dă valori de
  la a doua evaluare încolo.
- Expresiile sunt verificate față de o listă sigură înainte de salvare; la
  rulare, o intrare lipsă sare peste acea rundă (fără valori parțiale sau
  inventate), iar o eroare nu poate opri citirea.
- O valoare calculată merge spre **toate** ieșirile (MQTT, InfluxDB,
  contoare virtuale, fluxuri) și apare în Monitor/Istoric ca orice
  măsurătoare. Grupul ei de citire decide cât de des se recalculează.

---

## 8. MQTT și Home Assistant

Setări → **MQTT**: broker, port, autentificare, QoS, retain și TLS (portul
8883; certificatul CA pentru verificarea brokerului, opțional certificat +
cheie de client pentru TLS mutual — pune fișierele sub `config/` și indică
căile lor din container; „sari peste verificare” e doar pentru teste).

- **Topicuri**: `<prefixul dispozitivului>/<topicul registrului sau numele
  derivat>`. Dispozitivul #1 își păstrează prefixul istoric; dispozitivele
  noi folosesc implicit `mqtt.default_topic_pattern` (`mbg/devices/{device}`);
  unitățile unei instalații folosesc prefixul grupului (§5b).
- **Modul de publicare**: `changed` (implicit — se publică doar valorile
  schimbate; memoria de schimbări confirmă doar după o publicare reușită,
  deci o cădere a brokerului nu pierde nimic) sau `all` (fiecare citire).
- **Heartbeat** — `mqtt.heartbeat_interval` (secunde, `0` = oprit,
  implicit): în modul `changed`, forțează republicarea unei valori
  *neschimbate* după N secunde, ca o citire constantă să aibă un timestamp
  proaspăt și Home Assistant să nu gri-zeze entitatea în perioadele lungi
  fără schimbări.
- **Disponibilitate**: un Last-Will marchează `<prefix>/status` = `offline`
  dacă gateway-ul cade; `online` e reținut la conectare. **Fiecare dispozitiv
  publică un topic de disponibilitate reținut, inclusiv cel principal**, iar
  memoria de disponibilitate se golește la reconectare, deci e mereu
  reafirmată — un consumator nu trebuie să ghicească din tăcere. E confirmată
  doar după o publicare reușită, deci o sughițare a brokerului nu poate lăsa
  în urmă un `online` vechi.
- **Comenzile reținute nu sunt niciodată executate** — un mesaj MQTT reținut
  pe un topic de *comandă* (un `mosquitto_pub -r` rătăcit, un HA cu
  `retain: true`) ar acționa altfel echipamentul la **fiecare** reconectare.
  Mesajele reținute de pe topicurile de comandă sunt aruncate, iar copia
  reținută e ștearsă la abonare.
- **Descoperirea Home Assistant**: pornită implicit. Fiecare dispozitiv
  devine un dispozitiv HA cu registrele selectate ca senzori (`unique_id`
  `mbg_dev_<dispozitiv>_<adresă>_<nume>` pentru dispozitivele care nu sunt
  primul), cu clase de dispozitiv/stare potrivite unităților de măsură, plus
  un **`binary_sensor` de conectivitate** pe dispozitiv (disponibilitatea).
  Registrele pe care șablonul le marchează ca inscriptibile devin entități HA
  **`number`/`select`** (limitele din șablon, păzite dublu de
  `mqtt.allow_write_entities` + `security.allow_writes` — vezi §14);
  descoperirea cu scriere se înregistrează la pornire, deci controlul
  supraviețuiește unui restart. Contoarele virtuale își publică propriile
  entități de diagnostic (starea, rata cererilor, erorile, prospețimea…).
  Ștergerea unui dispozitiv îi golește descoperirea reținută, iar HA îi
  scoate entitățile.
- **Migrarea prefixelor de topic** — `mqtt.compat_aliases` publică dublu
  numele vechi de topic lângă cele noi, ca consumatorii să poată fi mutați
  fără gol (vezi [config-reference.md](config-reference.md)); scoate
  aliasurile după ce s-a mutat fiecare consumator.

**Atenție:** cu `retain: true` (implicit) un consumator care se abonează
târziu vede totuși ultima valoare — dar după un restart al brokerului fără
persistență, valorile reapar doar când se publică din nou; gateway-ul își
golește memoria de schimbări la fiecare reconectare și republică toată
starea exact din acest motiv.

---

## 9. InfluxDB și Grafana

Setări → **InfluxDB**: URL, token, org, bucket. Bucket-urile pe dispozitiv
se creează automat, cu păstrare 90 de zile; măsurătoarea și tag-urile pe
registru se setează în fila Măsurători. Îndreaptă Grafana spre același
bucket. Amândouă vin în stiva compose implicită — InfluxDB se configurează
singur la prima pornire, iar Grafana ascultă pe `:3000` (vezi §2); aici e
vorba doar de legarea gateway-ului la ele (sau la instanțele tale).

**Garanțiile datelor.** Fiecare punct poartă momentul *citirii*, nu al
trimiterii. Dacă InfluxDB nu mai e accesibil, punctele merg într-un **tampon
de stocare și retrimitere** (implicit **120 de minute / 200.000 de puncte**,
limitat la ~40 MB — reglezi `influxdb.buffer_minutes` /
`buffer_max_points`) și sunt retrimise cu timestamp-urile lor originale la
reconectare, fără dubluri. Fereastra e **relativă la date** (măsurată de la
cel mai nou punct din tampon, nu după ceas), deci un restart întârziat nu
aruncă un snapshot valid. Cu `influxdb.buffer_persist: true` (implicit),
tamponul supraviețuiește și unui restart în timpul căderii
(`config/influx_buffer.jsonl`). Loturile la care clientul renunță după ~5
minute de reîncercări interne sunt recuperate în același tampon. **Erorile
de autentificare sunt detectate explicit**: un 401/403/404 (token rotit,
bucket șters) setează semnalul `influx_auth_failed`, ridică o alertă pentru
operator și pune punctele înapoi în tampon în loc să le arunce — doar
punctele chiar greșite (400/422) sunt vreodată aruncate, iar
`writes_confirmed` / `last_confirm_age_s` dovedesc că datele chiar ajung
(vechiul mod de eșec era `connected: true` cu 100 % pierdere). Căderile mai
lungi decât fereastra aruncă punctele cele mai vechi; pentru tensiunile
Janitza, înregistrarea internă a contorului le poate completa cu
`python -m multibus.backfill` — completarea își ia schema punctelor (tag-uri,
câmpuri, măsurătoare) **din selecția live de registre**, deci punctele
reparate ajung în serii identice, iar o adresă deselectată e sărită, nu
scrisă cu o schemă ghicită. Urmărește `buffer_points` / `replayed_total` /
`dropped_total` în `/api/status` sau `gateway_influx_buffer_points` în
`/metrics`.

MQTT intenționat **nu** se retrimite: e o magistrală live — la reconectare
se republică starea curentă.

Fila **Istoric** a fiecărui dispozitiv (linii agregate cu banda min/max) și
**Energie** (totaluri lunare din contoarele cumulative + defalcare pe zile,
în fusul orar din §18; selecția contoarelor din fila Energie e pe
dispozitiv) citesc înapoi aceste date.

---

## 10. REST push și fluxul HTTP/JSON

Pagina dispozitivului → **Ieșiri** — două ieșiri suplimentare pe
dispozitiv, amândouă opționale:

- **Ieșirea HTTP/JSON** — servește valorile live ale dispozitivului ca JSON
  doar în citire la `GET /api/meters/<id>` (în stilul Solar API, după numele
  registrului, cu un semnal `stale`). Gateway-ul nu are nimic de interogat;
  orice client HTTP îl citește. Atenție: cu autentificarea pornită, fluxul
  cere o sesiune sau ca IP-ul consumatorului să fie acoperit de regulile tale
  de acces.
- **REST push** — trimite prin POST valorile dispozitivului ca JSON la un
  URL extern, la un interval (minim 5 s): `{enabled, url, interval_s,
  headers, format, verify_tls, timeout}`. `format: native` trimite
  `{nume: {value, unit, ts}}`; `flat` trimite `{nume: valoare}`.
  Autentificarea merge în `headers` (păstrate mascate, neatinse la salvare).
  Destinațiile *pot* fi externe (spre deosebire de intrările HTTP ale
  dispozitivelor), dar redirecționările sunt refuzate, ca acreditările tale
  să nu poată fi retrimise altundeva. Livrarea e fără confirmare; cardul
  arată ultima stare, iar **Test** trimite o dată imediat.

---

## 11. Contoare virtuale — pas cu pas

Scopul: alt sistem (Victron ESS, un invertor Fronius, orice client SunSpec)
să citească acest gateway ca pe contorul pe care *el* îl așteaptă.

> ⚠️ Un contor virtual poate alimenta o buclă de control. Fă pașii 11.1→11.3
> (validare în paralel) înainte să-l faci vreodată singurul contor al unui
> consumator.

**11.0 — Publică porturile (o dată).** Fișierul compose publică
`1502–1512` (plus `502`). Alege porturile instanțelor în această plajă; o
mărești cu `VMETER_PORT_START/END` + recrearea containerului dacă îți
trebuie mai multe.

**11.1 — Alege sau creează un șablon.** Contoare virtuale → **Șabloane**.
Incluse: `em24_av53` (Carlo Gavazzi EM24 → Victron), `fronius_ts_native`
(Fronius Smart Meter TS → DataManager), `fronius_sunspec_meter` (SunSpec 213
float). Un rând de șablon = adresă, tip, scală, ordinea cuvintelor și o
**sursă**: un registru live (`nume`, sau `dispozitiv.registru` pentru alt
dispozitiv), o constantă sau o `sumă` de mai multe surse. Import/export ca
YAML.

**11.2 — Adaugă o instanță.** În fila **Contoare**: șablonul, un port liber,
unit id-ul, dispozitivul sursă, politica de învechire → **Adaugă instanță**.
Pornește **dezactivată**.

**11.3 — Validează în paralel.** Activeaz-o și îndreaptă spre `host:port` un
consumator de *test* — sau doar urmărește fila **Jurnal**. Jurnalul
cererilor arată exact ce citește consumatorul, când și ce i s-a răspuns
(ultimele 1024 de cereri). Compară cu contorul real. **Statistici** arată
rata, erorile, registrele cele mai citite; **Decodare** interpretează orice
plajă de adrese înapoi în variabilele ei sursă.

**11.4 — Alege politica de învechire** (`on_stale`) — ce servește un rând
învechit sau lipsă:

| Politică | Comportament | Pentru |
|---|---|---|
| `legacy` (implicit) | comportamentul clasic cu o singură sursă: un singur ceas de prospețime pe instanță; golurile păstrează ultimele cuvinte | contoarele existente — neatinse |
| `fail` | orice citire care atinge un rând învechit → **excepție Modbus** (fără adevăr parțial) | consumatori de control (Victron, PLC) |
| `sentinel` | N/A SunSpec: float→NaN, int16→0x8000, uint16→0xFFFF… | consumatori care înțeleg marcajele |
| `hold` | ultima valoare până la `max_hold_s` (implicit 30 s), apoi ca `fail` | afișaje tolerante |

Lipsa nu e servită **niciodată** ca 0/fals. Sumele iau calitatea celui mai
slab membru. În modurile cu politică, serverul rămâne pornit cât timp măcar o
sursă e proaspătă; toate moarte → nu mai răspunde, ca protecția proprie a
consumatorului la pierderea contorului să intre în funcțiune.

Două rafinări pe rând:

- **Un rând a cărui sursă nu s-a rezolvat niciodată** (registru redenumit,
  deselectat, o greșeală în șablon) pică verdictul de prospețime direct și
  ridică un eveniment `unresolved` — diferit de *învechit*. Înaintea acestei
  reguli, blocul pornit cu zero ar fi servit un 0 W plauzibil cât timp
  contorul părea sănătos; acum contorul refuză vizibil până când sursa se
  rezolvă o dată.
- **Limită de prospețime pe rând** — un registru poate avea propriul
  `stale_after_s`, care o suprascrie pe a instanței, astfel încât un senzor
  BLE de 60 de secunde să poată sta în același contor cu rânduri de rețea de
  250 ms fără învechiri false; limita efectivă se deduce și automat din
  ritmul grupului de citire care o produce.

**11.5 — Blocul de calitate în bandă (opțional).** Dacă consumatorul
(PLC/SCADA) trebuie să știe calitatea datelor pe aceeași conexiune Modbus,
activează *Bloc de calitate în bandă* pe instanță. Servește un bloc doar în
citire la **61440** (identic pentru orice contor):

| Adresă | Tip | Înțeles |
|--------:|------|---------|
| 61440 | u16 | versiunea formatului (1) |
| 61441 | u16 | 0 legacy · 1 ok · 2 degradat · 3 învechit |
| 61442–61444 | u16 | rânduri proaspete / învechite / lipsă |
| 61445 | u16 | totalul rândurilor de date |
| 61446 | u32 | vârsta celei mai noi valori proaspete (s); 0xFFFFFFFF = niciodată |

Protecția tipică la consumator: *folosește datele doar cât timp 61441 == 1;
alarmă la 3*. Specificația completă pe fir:
[virtual-meter-spec.md](virtual-meter-spec.md).

**11.6 — Comutarea.** Îndreaptă consumatorul real spre contorul virtual.
Ceasul de prospețime e plasa de siguranță. Aceeași hartă e servită și ca
JSON la `/api/virtual-meters/<id>/values`, după convenția agregatorului
(`value: null` + `quality` + `age_s`, `last_value` separat) — un SCADA poate
citi totul dintr-o interogare.

**11.7 — Surse redundante (preluare).** Un contor care alimentează o buclă
de control nu ar trebui să se oprească la căderea unei surse. Două niveluri,
pe același motor:

- **Pe registru** — în editorul de șabloane, lista tipului de sursă oferă
  **Failover (live…)**: o listă de candidați separați prin virgulă, în
  ordinea priorității (`dispozitiv.registru` pentru o sursă de pe alt
  dispozitiv). YAML:
  `source: { failover: ["_G_P_SUM3", "fronius.power_active_total"] }`
  (`combined` e un alias acceptat). Fiecare reconstruire servește **primul
  candidat proaspăt**, îi sare în ordine pe cei lipsă sau învechiți și
  revine imediat ce sursa principală e iar proaspătă.
- **Pe instanță** — lista *Dispozitiv sursă secundar (failover)* din
  fereastra Adaugă/Editează instanța (`device_fallback:` pe instanță)
  numește un **dispozitiv geamăn**. La pornire, fiecare registru live dat
  doar prin nume e rescris în perechea `[nume, <geamăn>.nume]` — fără
  editări de șablon, pentru că numele canonice sunt identice pe toate
  dispozitivele. Rândurile constante, sumele, cele care au deja failover și
  cele cu `dispozitiv.registru` explicit rămân cum au fost scrise.
  **Contoarele cumulative de energie sunt excluse intenționat**: geamănul e
  alt contor fizic, deci totalul lui de-o viață ar fi un salt nemonoton care
  strică statisticile kWh din aval (un Fronius DataManager tratează un
  contor care merge înapoi ca defect). Rândurile de contor rămân pe sursa
  principală și, la o cădere, **îngheață la ultima valoare bună** în orice
  politică — un contor înghețat e o afirmație adevărată („energia livrată
  până acum”) — în timp ce rândurile instantanee trec pe geamăn; când sursa
  principală revine, contorul continuă cu un salt legitim înainte, ca după
  orice repornire a unui contor. Geamănul e verificat la salvare (dispozitiv
  cunoscut, diferit de sursă) și din nou la pornire (o valoare greșită e
  ignorată cu un avertisment — nu blochează niciodată contorul). Un geamăn
  configurat, dar deconectat, se activează imediat ce publică.

**Legătura cu învechirea:** preluarea servește doar un candidat
*proaspăt*; dacă niciunul nu e, rândul se degradează prin politica
`on_stale` a instanței exact ca o singură sursă învechită (sub `fail` citirea
e refuzată). **Evenimente de comutare:** fiecare schimbare a sursei servite
ajunge în jurnalul de evenimente și în jurnalul procesului — `warn` la
trecerea pe o sursă de prioritate mai mică, `info` la revenire — iar cardul
contorului arată `✓ pe sursa principală` / `⇢ n/m pe rezervă`.

**Starea publicată.** Starea MQTT reținută a fiecărui contor
(`<prefix mqtt>/vmeter/<id>/state`) conține politica și limitele de
învechire (`on_stale`, `stale_after_s`, `max_hold_s`), numărătoarea calității
la ultima reconstruire (proaspete/învechite/lipsă) și rutarea live a
preluării (pe registru: candidații, sursa activă, `on_primary`) — un monitor
extern vede *cum* se degradează contorul și ce sursă îl alimentează, nu doar
că s-a învechit.

**Atenție:** contorul răspunde pe orice unit id (cel configurat e
informativ); citirile din afara hărții emulate răspund intenționat cu
*illegal data address* (consumatorii recunosc contoarele sondând adresele
mici); ștergerea unui dispozitiv sursă e refuzată cât timp un contor îl
folosește.

---

## 11b. Noduri ESP — un serviciu separat

Programarea plăcilor ESP32/ESP8266 și evidența lor nu se mai fac în
gateway: sunt treaba serviciului separat **multi-bus-gateway-esphome** (a
înlocuit Device Builder). Dintr-un browser de pe calculatorul la care e
conectată placa, identifică cipul prin USB (Web Serial), scrie o imagine de
bază, o configurație ESPHome completă sau un firmware propriu, setează
Wi-Fi-ul prin același cablu și arată nodurile care se anunță prin MQTT.

Rulează-l sub același nume de host ca gateway-ul (de ex. sub `/nodes`), cu
reverse proxy-ul întrebând `GET /api/auth/forward` înaintea fiecărei cereri
— autentificarea gateway-ului îl acoperă atunci, cu aceiași utilizatori și
roluri. Setează **Setări → Securitate → Serviciul de noduri ESP**
(`ui.nodes_url`, de ex. `/nodes/`) și apare în **Setări → Module**, cu
starea lui și un buton Deschide (sau declară-l în `modules:`, cu o
descriere).

Un nod care publică prin MQTT se adaugă aici ca orice dispozitiv MQTT;
**scanarea ESPHome** din Descoperă dispozitive (§5.1) găsește în continuare
nodurile ESPHome din LAN.

---

## 11c. Calitatea energiei — înregistratorul de evenimente PQ (Janitza)

Analizoarele Janitza din seria UMG (604/605/508/511/512) înregistrează în
aparat evenimentele de calitate a energiei — goluri de tensiune,
supratensiuni, întreruperi, variații rapide de tensiune (RVC), abateri de
frecvență — plus forme de undă RMS pe semiperioadă de ~50 s în jurul
fiecărui eveniment. Problema: contorul păstrează doar un **inel de 32 de
evenimente** (o zi agitată în rețea îl umple în câteva ore), iar Modbus
expune doar contoarele de la punerea în funcțiune, nu înregistrările.
Gateway-ul citește înregistratorul prin firmware-ul web al contorului și
**arhivează totul permanent** în InfluxDB.

### De ce ai nevoie

1. Un dispozitiv al cărui șablon e din familia Jasic — șablonul declară
   capacitatea cu cheia `pq_recorder: "jasic"` la primul nivel (inclusă în
   `janitza_umg512_pro`; pentru alt model UMG adaugi doar cheia în șablonul
   tău — fără modificări de cod).
2. **Ieșirea InfluxDB activată** pe dispozitiv (acolo stă istoricul).
3. Acces HTTP de la gateway la contor (portul 80 — endpoint-urile
   firmware-ului web Jasic, fără autentificare).

### Pornire (din interfață, ~30 de secunde)

**Dispozitive → deschide dispozitivul → fila Calitatea energiei → cardul
„Înregistrator de evenimente PQ”** → bifează *Activează*, lasă *Interogare*
la 60 s și *Arhivează formele de undă ale evenimentelor* pornit →
**Salvează**. Linia de stare de sub card confirmă:
`✓ înregistratorul rulează · ultima interogare … · N evenimente / M tranzienți`.

Echivalentul în `config.yaml` (cel principal = secțiune plată; celelalte
dispozitive în intrarea lor din `devices[]`):

```yaml
pq_recorder:
  enabled: true
  poll_s: 60               # păstrează-l scurt — vezi „Limitări” mai jos
  archive_waveforms: true
  base_url: ""             # implicit: http://<host-ul conexiunii>
```

### Folosire

Fila **Calitatea energiei** din pagina dispozitivului (apare doar la
dispozitivele al căror șablon declară capacitatea):

- **stânga** — istoricul arhivat al evenimentelor (nu doar inelul!), grupat
  pe zile, cu un punct de gravitate: roșu = întrerupere, galben = abatere de
  tensiune/curent/frecvență, turcoaz = RVC. Selectorul de interval
  (24h/7z/30z/90z) îl filtrează.
- **dreapta** — detaliile evenimentului ales (cauzele pe fază, durata,
  min/max/medie, pragul de declanșare) și **forma de undă** înregistrată
  (RMS la 10 ms); selectorul de canal alege urma (UL1..UL4, IL1..IL4,
  perechile L-L). Cel mai nou eveniment e ales automat.

### Unde ajung datele

| Destinație | Ce | Când |
|---|---|---|
| InfluxDB `pq_events` | fiecare eveniment (cauză+canal, durată, min/max/medie) | la fiecare interogare, fără dubluri |
| InfluxDB `pq_counters` | contoarele contorului de la punerea în funcțiune | la fiecare interogare |
| InfluxDB `pq_waveforms` | urmele RMS ale canalelor implicate | la evenimente NOI |
| MQTT `<prefix>/pq/event` | ultimul eveniment nou, ca JSON (reținut) | la evenimente noi |
| Alerte (cap. 12) | întrerupere → `critical`, abatere → `warning`, RVC → `info` | la evenimente noi, limitate ca rată pe dispozitiv |

### Limitări de știut (măsurate pe echipament real)

- **Contorul servește prin HTTP doar fereastra de captură cea mai NOUĂ.**
  Cererea unei ferestre mai vechi întoarce datele celei mai noi, în aceeași
  formă, fără eroare — gateway-ul verifică fiecare urmă și respinge datele
  străine, deci nu se arhivează niciodată ceva greșit; consecința practică e
  că forma de undă a unui eveniment poate fi salvată doar cât timp e încă
  cea mai nouă din contor. De aici `poll_s: 60`; într-o rafală de
  evenimente în același minut, doar cel mai nou își păstrează urma.
- **În timpul unei căderi de rețea contorul e de obicei stins și el**
  (alimentat auxiliar din partea măsurată): evenimentul de *început* al
  întreruperii s-ar putea să nu fie scris niciodată, iar interogările eșuează
  cât timp rețeaua lipsește (înregistratorul reîncearcă singur; primul eșec
  dintr-o serie se scrie la nivel info). Vrei vizibilitate *în timpul*
  căderilor? Pune alimentarea auxiliară a contorului pe un UPS.
- Evenimentele deja aflate în inel **înainte** de pornirea înregistratorului
  sunt arhivate ca evenimente (prima sincronizare e tăcută — fără alerte),
  dar formele lor de undă în general nu mai pot fi recuperate — interfața
  arată sincer „Nicio formă de undă disponibilă”.

### Depanare rapidă

| Simptom | Cauza probabilă |
|---|---|
| Nu există fila Calitatea energiei | șablonul dispozitivului nu declară `pq_recorder: "jasic"`, sau ieșirea InfluxDB e oprită |
| Listă de evenimente goală | înregistratorul abia a fost pornit (istoricul se adună de acum) sau intervalul ales e prea scurt |
| „Nicio formă de undă disponibilă” | eveniment de dinainte de înregistrator sau o rafală de evenimente — vezi Limitări |
| `ultima eroare` în linia de stare | contorul nu răspunde prin HTTP (cădere? IP schimbat? verifică `base_url`) |

Detaliile de implementare (endpoint-uri, decodarea măștii de cauze, schema
măsurătorilor, API) sunt în [pq-recorder.md](pq-recorder.md).

## 11d. Server OPC UA — pentru SCADA și panouri HMI

Setări → **OPC UA**: pornește-l, verifică portul (4840), Salvează și aplică.
Fiecare dispozitiv și fiecare valoare apar ca
`Objects/MBG/Devices/<dispozitiv>/<valoare>`, doar în citire. Fiecare
valoare are:
- **calitatea** ei: Good; Uncertain când e veche; Uncertain-no-communication,
  cu ultima valoare, când dispozitivul nu răspunde;
- **momentul citirii** (SourceTimestamp).

Clienții se autentifică cu conturile gateway-ului. Criptarea e obligatorie
dacă nu alegi altfel. Ghidul, cu UaExpert, Weintek și Ignition, e
[opcua.md](opcua.md).

## 12. Alerte și webhook-uri

Trei familii de alerte pe o singură cale de livrare (MQTT + webhook):

- **Sănătatea infrastructurii** — un dispozitiv sau o ieșire cade, latența
  de citire rămâne mare, tamponul InfluxDB se umple.
- **Alarmele dispozitivelor** — câmpurile de alarmă declarate de șablonul
  unui dispozitiv (un cod de eroare, un cuvânt de defect, un semnal calculat
  ca „descărcare peste 100 °C”) declanșează când devin active și rămân așa
  `for_s`, din nou la o valoare nouă și încă o dată când dispar
  (`signals.alarm`, pornit implicit).
- **Pragurile pe valori** — limitele de avertizare/pericol pe registru din
  §6 devin evenimente de alertă printr-un motor cu histerezis inclus
  (**oprit implicit**, `signals.threshold`): cinci benzi, declanșează doar
  la trecerea între benzi, *repede la alarmă, încet la revenire* (zona moartă
  `threshold_deadband_pct`, implicit 2 %), suprimat pe date învechite, ca o
  pierdere de comunicație să nu declanșeze o trecere fantomă.

Configurezi în Setări → Alerte sau în blocul `alerts:`:

```yaml
alerts:
  enabled: true
  mqtt: true                                   # publică în <topic_prefix>/alert
  webhook_url: "https://ntfy.example/gateway"  # gol = webhook oprit
  webhook_headers: { "X-API-Key": "secret" }
  webhook_body: { "message": "[{severity}] {source}: {message}" }
  min_interval_s: 300
  latency_ms: 1000
  buffer_points: 1000
  signals: { device: true, sink: true, latency: true, buffer: true,
             threshold: true, alarm: true }    # threshold e implicit false, alarm implicit true
  threshold_deadband_pct: 2.0
  threshold_alert_on_start: true
```

Alertele sunt limitate ca rată pe cheie (`min_interval_s`), copiate în
jurnalul de evenimente și în pagina Status. Gateway-ul *detectează și
livrează*; eliminarea dublurilor, rutarea și distribuirea pe canale
(Telegram/SMS/e-mail) sunt treaba receptorului de webhook / a sistemului tău
de notificări. Butonul **Test** (sau `POST /api/alerts/test`) trimite o
alertă de probă prin canalele reale — cere autentificare sau o cheie API și
are o pauză obligatorie, pentru că produce trafic real spre exterior.
Livrarea prin webhook e fără garanție (fără reîncercare) și refuză
redirecționările. Forma mesajului și motorul de praguri în detaliu:
[alerts-webhooks.md](alerts-webhooks.md).

---

## 13. Diagnostic

Trusa de punere în funcțiune (pagina Diagnostic). Totul de aici e doar în
citire pe magistrală.

- **Monitorul de magistrală** — captură la nivel de cadru a fiecărei
  tranzacții Modbus: hex TX/RX, funcția/adresa/numărul decodate, rezultatul
  (ok / excepție / fără răspuns / eroare CRC), latența — și **fiecare
  reîncercare ca intrare separată**, ca să vezi exact ce face o legătură
  instabilă. Tampon circular doar în memorie (implicit 1000, până la 20000
  de cadre): pornește dezactivat și nu se salvează, deci o sesiune uitată nu
  poate crește în RAM-ul unei cutii care merge de un an.
- **Sonda de registre** — o citire unică la orice adresă (FC1–4, număr 1–8)
  pe orice dispozitiv Modbus, decodată **în orice fel plauzibil**: o
  matrice tip de date (uint16/int16, float/int32/uint32, double/int64/uint64)
  × ordinea cuvintelor (ABCD / CDAB / BADC / DCBA), plus hex brut și ASCII.
  E bancul de lucru pentru endianness: când o valoare iese fără sens,
  combinația corectă tip+ordine e de obicei chiar în fața ta în matrice. NaN
  e arătat ca o constatare (SunSpec „indisponibil”), nu ascuns.
- **Mostră de mesaj** — ia un mesaj JSON complet de la un dispozitiv HTTP sau
  MQTT salvat (MQTT reținut → imediat), ca să alegi `json_path`-ul.
- **Tipurile de erori** — eșecurile de citire sunt numărate pe tip:
  `timeout` (fără răspuns), `exception_N` (dispozitivul a răspuns cu
  excepția Modbus N — legătura e bună, cererea e greșită), `connection` (la
  nivel TCP/serial). Apar pe dispozitiv în Status și în `/metrics`. Politica
  de reîncercare e a gateway-ului (`retry_attempts`/`retry_delay`), nu e
  dublată de biblioteca Modbus, iar un răspuns scurt sau gol se numără ca
  eșec, nu ca succes. *Accesibilitatea* e un **verdict al legăturii**, nu al
  unui bloc: un dispozitiv e declarat inaccesibil doar după ce se declanșează
  protecția de eșecuri consecutive (care și redeschide forțat o legătură
  blocată, dar deschisă), deci un bloc de registre cronic prost nu poate
  produce evenimente `unreachable/recovered` alternative cât timp legătura e
  bună — pierderea pe bloc rămâne vizibilă ca `batch_failures` + `stale_groups`
  pe grup în starea de sănătate.
- **Interoghează acum** întoarce atât valoarea **brută** de pe fir, cât și,
  când adresa e un registru selectat, un câmp suplimentar **`corrected`** —
  valoarea exactă pe care ar publica-o citirea normală (scală/offset/enum
  aplicate, fără stare: o citire de depanare nu avansează filtrul monotonic).
  Dacă cele două diferă neașteptat, declarația de scală/decodare a
  registrului e locul unde trebuie să te uiți.

---

## 14. Scrieri Modbus și lease-uri dead-man

Scrierea în echipamentele din teren e păzită pe mai multe niveluri. Totul e
**oprit implicit**.

1. Activează `security.allow_writes: true` (Setări → Securitate).
2. Scrierile trebuie să fie **autentificate** — pornește autentificarea sau
   setează `API_KEY` (scrierile anonime sunt refuzate chiar și cu poarta
   deschisă).
3. Registrul trebuie declarat **inscriptibil în șablonul dispozitivului**,
   cu limite opționale `write_min` / `write_max`; codarea (tipul de date,
   scala și `offset` — inversat la scriere, `brut = (valoare − offset) ×
   scală`, inclusiv la revenirea de siguranță) vine mereu din rândul
   șablonului, niciodată de la apelant.
4. **Dispozitivul principal e blocat la scriere implicit**
   (`security.primary_write_locked`; deblochează-l intenționat, din fila lui
   Comenzi, sau Conexiune când nu are comenzi); dispozitivele HTTP/JSON și
   registrele input/discrete nu pot fi scrise.
5. Limită de rată pe IP (`security.write_rate_limit_per_s`, implicit 10/s).

`POST /api/devices/<id>/write` cu `{"address": ..., "value": ...}` scrie
FC6/FC16 (holding) sau FC5 (coil), verifică prin citire înapoi și scrie
fiecare încercare în jurnalul de audit.

**Lease-uri dead-man.** Adaugă `"lease_ms": 5000` și scrierea armează un
lease: dacă nu e reînnoit (de altă scriere cu lease) în 5 s, gateway-ul
readuce registrul la valoarea `write_safe` declarată în șablon. Lease-urile
**rezistă la căderi**: setul activ se salvează pe disc, deci dacă gateway-ul
însuși cade, registrele revin la sigur la pornirea următoare. E primitiva
corectă pentru buclele de control al limitei de export / al setpoint-ului de
putere: un controler căzut nu poate lăsa în urmă un setpoint periculos.
Lease-urile active: `GET /api/writes/leases`.

---

## 14b. Comenzi — scrieri cu nume, din șablon

O scriere brută de registru (§14) spune *ce adresă*. O **comandă** spune
*ce*: `power_limit = 60 %`, `restore`. Șablonul dispozitivului ține
**rețeta** — ce registre, în ce ordine, într-un singur cadru, verificate cum
— astfel încât un controler (interfața, Node-RED, Home Assistant, un script)
nu scrie niciodată adrese de mână, iar gateway-ul nu decide niciodată
*când* (asta e o regulă, §14c, sau controlerul tău). Fiecare cale ajunge la
același motor, cu aceleași porți, aceeași înregistrare de audit și același
vocabular al verdictelor.

### 14b.1 Pornește una din interfață

1. **Întâi porțile** (§14): `security.allow_writes: true`, autentificarea
   pornită sau `API_KEY` setat și unitatea neblocată la scriere (pe o
   instalație, lacătul acoperă fiecare unitate).
2. Șablonul trebuie să declare comanda. `fronius_sunspec_inverter` aduce
   `power_limit` (modelul SunSpec 123: `value` 0–100 %, `revert_s` implicit
   600, `ramp_s`) și `restore` (un alias: `power_limit` 100 %, revert 0). Un
   șablon fără `commands` nu oferă nimic.
3. **Bifeaz-o**: pagina instalației → cardul grupului → *Editează grupul* →
   **Comenzi acceptate de acest grup** (se aplică fiecărei unități). Pe un
   dispozitiv de sine stătător: pagina lui → **Comenzi** → **Activată pe
   acest dispozitiv**. Nebifată = refuzată peste tot. **Test** (o rulare de
   probă) merge înainte s-o bifezi: citește dispozitivul și arată ce s-ar
   scrie, fără să scrie.
4. **Rulează-o**: butonul **Comenzi…** al cardului de grup — comanda,
   parametrii ei (limitele, valorile implicite și unitățile vin din șablon),
   ținta (*fiecare unitate a grupului* sau o singură unitate), apoi **Test**
   (o rulare de probă: valorile curente ale protecției și registrele exacte
   care s-ar scrie — nimic pe fir) sau **Execută** (cu o confirmare).
   Rezultatul e un cuvânt pe unitate. Fila **Comenzi** a unității are
   aceleași Execută / Test, ultimul rezultat și istoricul recent.

### 14b.2 Același lucru în YAML

Presetarea, în șablon (limbajul producătorului):

```yaml
commands:
  power_limit:
    label: Active power limit
    params:
      value:    { unit: "%", min: 0, max: 100, required: true, aliases: [limit_pct] }
      revert_s: { unit: s, default: 600, min: 0, max: 65535 }
      ramp_s:   { unit: s, default: 0,   min: 0, max: 65535 }
    guard:                                    # trebuie să fie adevărat, altfel nu se scrie nimic
      - { read: controls_model_id, expect: 123 }
      - { read: wmaxlimpct_sf, in: [-2, -1, 0] }
    writes:                                   # registre holding consecutive → UN SINGUR FC16
      - { register: power_limit_pct,      value: "${value}" }
      - { register: power_limit_win_s,    value: 0 }
      - { register: power_limit_revert_s, value: "${revert_s}" }
      - { register: power_limit_ramp_s,   value: "${ramp_s}" }
      - { register: power_limit_enabled,  value: { if: "${value} < 100", then: 1, else: 0 } }
    settle_s: 1
    verify:
      - { read: power_limit_pct,     expect: "${value}", tolerance: 1 }
      - { read: power_limit_enabled, expect: { if: "${value} < 100", then: 1, else: 0 } }
    safe: { value: 100, revert_s: 0 }         # ce restabilesc un lease sau o regulă
    readback_group: controls                  # recitit imediat după o rulare
  restore:
    alias: { command: power_limit, params: { value: 100, revert_s: 0 } }
```

Legarea, pe un grup al unei instalații sau pe un dispozitiv (ce declari
*tu*; `faces` sunt implicit toate trei, `lease_s > 0` armează dead-man-ul
din §14 cu parametrii `safe` ai comenzii — lease-ul e armat imediat ce a
plecat un cadru, indiferent ce a spus citirea înapoi, iar revenirea lui
rulează rețeta sigură chiar dacă între timp scrierile au fost oprite sau
dispozitivul blocat, la expirare, la o oprire curată și, după o cădere, la
pornirea următoare):

```yaml
commands:
  - { name: power_limit }
  - { name: restore, faces: { api: true, mqtt: true, ha: false }, confirm: true, lease_s: 0 }
```

O legare cu propriile `writes` e o rețetă directă, verificată ca una din
șablon. Expresiile sunt intenționat minuscule — literali, `${param}`, un
`if`, un operator aritmetic; orice mai mult e treaba unui controler. Design
și gramatică: [commands-design.md](commands-design.md).

### 14b.3 Ce face o rulare și ce vezi

Citirile de protecție (decodate prin șablon; o protecție picată e recitită o
dată înainte să fie crezută — dataloggerele dau uneori gunoi din tampon) →
codarea prin șablon (tip, scală, factorul de scală SunSpec live, offset,
ordinea octeților, `write_min/max`) → cadrele (registrele consecutive într-un
singur FC16, deci nu există niciodată un bloc scris pe jumătate) → așteptare
→ citirile de verificare. Un singur verdict, același cuvânt pe orice cale:
**`success`** (citirea înapoi s-a potrivit), **`mismatch`** (scris,
dispozitivul ține altă valoare), **`unverified`** (scris, citirea înapoi nu
a răspuns), **`rejected`** (refuzat înainte de fir, cu motivul), **`error`**
(o scriere a eșuat; rezultatul spune câte cadre plecaseră înainte).

După `success`/`mismatch`, valorile verificate intră în memoria live, iar
`readback_group` e recitit; cu `revert_s` setat e recitit din nou la +5, +20,
+60 și +180 s după momentul propriu de revenire al dispozitivului, ca MQTT,
InfluxDB, HA și regulile să vadă invertorul eliberând singur limita. Fiecare
rulare e auditată (`action: command`: cine, calea, unitatea, parametrii,
cadrele, înainte → după, verdictul), scrisă în Jurnalul unității și
publicată pe MQTT: `<prefixul unității>/cmd/result` (nereținut) și
`<prefixul unității>/cmd/<nume>/state` reținut (ce i s-a spus ultima dată
dispozitivului, pentru un controler care repornește). Citirea înapoi Fronius
se publică ca `<prefixul unității>/controls/power_limit_pct`,
`…/power_limit_enabled`, `…/power_limit_revert_s`, `…/power_limit_ramp_s`,
`…/connected` (grupul `controls` e citit intenționat o dată pe oră —
fiecare rulare îl recitește imediat).

**Cele patru căi** (interfața, ca mai sus, și):

- **API HTTP** — `GET /api/devices/{id}/commands` (ce oferă unitatea:
  parametrii cu limite și valori implicite, căile, activarea, ultimul
  rezultat — descoperă asta în loc să-l scrii de mână),
  `POST /api/devices/{id}/commands/{nume}` `{"value": 60, "revert_s": 300}`
  (+ opțional `lease_s`), `…/dry-run`,
  `POST /api/endpoints/{id}/groups/{gid}/commands/{nume}` (fiecare unitate,
  câte un rezultat; o unitate care refuză nu le oprește pe celelalte, nimic
  nu e anulat), `GET /api/commands/history?device=`. HTTP 200 pentru
  success/mismatch/unverified, 422 rejected, 502 error, 403 porți, 409 fără
  sursă Modbus ([API.md](API.md)).
- **MQTT** — `<prefixul unității>/cmd/<nume>` și `<prefixul grupului>/cmd/<nume>`:
  un număr simplu (parametrul `value`) sau un obiect de parametri; `source`
  numește apelantul în audit; aliasurile vechi (`limit_pct`,
  `revert_timeout`, `ramp_time`) sunt acceptate. Cere
  `mqtt.allow_write_entities` **și** o autentificare la broker
  (`mqtt.username`) — cu un broker anonim, o publicare ar fi o scriere în
  echipament pentru oricine din LAN, deci e refuzată. **Comenzile reținute nu
  sunt niciodată executate.**
- **Home Assistant** — entitatea `number` a registrului pe care o comandă îl
  scrie primul (`power_limit_pct`) rulează *comanda*, niciodată o scriere
  brută de registru (singură n-ar face nimic, sau ar strica); aceleași porți
  ca la MQTT.

### 14b.4 Note de siguranță

- **Porțile, în ordine, pe orice cale**: oferită de șablon → activată de o
  legare → calea e permisă → nu e deținută de o regulă armată (§14c; trimite
  `override_s` ca s-o pui pe pauză) → `security.allow_writes` → nu e blocată
  la scriere → parametrii se înțeleg, sunt în limite și nu au nume
  necunoscute (o greșeală de scriere e refuzată, niciodată ignorată) → o
  sursă Modbus rulează (o unitate citită doar prin Solar API nu poate scrie)
  → limita de rată pe cale și pe unitate. Autentificarea (§14) e verificată
  de ruta API înaintea tuturor acestora. O singură comandă rulează odată; o a
  doua pentru aceeași unitate așteaptă, nu e niciodată unită cu prima.
  **Nimic nu se reîncearcă după ce a plecat un cadru** (scrierile dintr-o
  secvență nu sunt sigur repetabile); un dispozitiv inaccesibil răspunde
  `error`, fără nimic scris.
- **Revenirea pe dispozitiv (`revert_s`)** e ceasul propriu al invertorului:
  un controler mort nu lasă niciodată centrala limitată, fără niciun ajutor
  de la gateway; `restore` îl șterge. **Lease-ul pe gateway (`lease_s`)**
  rulează singur parametrii `safe` când nu e reînnoit; o rulare cu valorile
  sigure șterge lease-ul. Lease-urile se salvează pe disc, deci un gateway
  care cade readuce registrul la pornirea următoare (§14).
- **Restartul gateway-ului**: nicio comandă nu e retrimisă;
  `…/cmd/<nume>/state` reținut îi spune controlerului tău ce era înainte,
  recitirea `controls` le spune tuturor ce ține dispozitivul acum.
  **Repornirea dispozitivului**: invertorul revine la valorile lui implicite,
  iar următoarea recitire `controls` publică adevărul; o regulă (§14c)
  comandă din nou la această abatere, un controler simplu trebuie să
  urmărească singur `…/controls/*`.
- Fiecare apel e în jurnalul de audit (§16.6), cu `via` = `api` / `mqtt` /
  `ha` / `rule:<id>` / `lease-revert`.

### 14b.5 Depanare

| Simptom | Verifică |
|---------|-------|
| 404 *offers no command* | sursa Modbus a unității folosește un șablon fără `commands:`, sau numele e scris greșit — `GET /api/devices/{id}/commands` arată ce există |
| 403 *not enabled on …* / *does not accept the … face* | bifeaz-o la *Comenzi acceptate de acest grup*, sau corectează `faces` din legare |
| 409 *owned by rule …* / *no Modbus or MQTT source is running* | o regulă armată conduce această țintă (trimite `override_s`, sau *Pauză…* în pagina Reguli) / unitatea e citită doar prin HTTP — adaugă o sursă Modbus grupului |
| Dispozitiv MQTT: `rejected: … not read by this device` | un rând pe care comanda îl verifică sau îl scrie nu e selectat pe dispozitiv — selectează-l |
| Dispozitiv MQTT: `unverified: published, but no read-back arrived` | dispozitivul nu a răspuns pe topicul rândului în timpul de așteptare — ascultă pe topicul de scriere? (un GX are nevoie de „MQTT on LAN”) |
| `rejected: guard: controls_model_id is …` | modelul 123 nu e unde îl așteaptă șablonul, sau datalogger-ul a întors gunoi de două ori — **Test** arată valorile protecției |
| `…/controls/power_limit_enabled` e 0 cât timp `power_limit_pct` arată încă limita | s-a declanșat revenirea proprie a invertorului: șterge *enabled* și păstrează procentul — condiționează pe `enabled`, nu doar pe procent |
| Comanda MQTT e ignorată fără niciun semn | ambele porți de scriere pornite? autentificare la broker setată? mesajul e un număr sau un obiect? un mesaj **reținut** e aruncat intenționat — publică fără `-r` |
| Nicio entitate `number` în HA | `mqtt.allow_write_entities` + `security.allow_writes` pornite, dispozitivul neblocat la scriere, descoperirea pornită; entitățile se (re)înregistrează la pornire |
| 429 | `security.write_rate_limit_per_s` — controlerul trimite prea des |

### 14b.6 Comenzi prin MQTT (un Victron GX)

Și un dispozitiv citit prin MQTT rulează comenzi, când șablonul îi dă
rândurilor un **topic de scriere**. `victron_gx_mqtt` aduce comenzile ESS:
- `ess_setpoint` (setpoint-ul de rețea);
- `max_charge_current`, `max_charge_power`;
- `max_discharge_power`, `max_feed_in`;
- `min_soc`.

Se activează, se testează și se rulează ca oricare altele (§14b.1–3). Ce
diferă:
- rularea publică și apoi așteaptă ca valoarea să revină pe topicul
  rândului: *success*, *mismatch* sau *unverified*;
- **deja setată**: o valoare pe care dispozitivul o ține deja nu e publicată
  din nou;
- GX-ul ține aceste setări în flash. Șablonul lui distanțează la un minut
  două scrieri pe aceeași setare (un canal cu `min_interval_s`), pentru orice
  scriitor;
- comenzile **nu au valoare de eliberare**, pentru că depinde de
  amplasament. Setează una pe legarea dispozitivului când o regulă sau un
  lease trebuie să pună ceva înapoi:

  ```yaml
  commands:
    - { name: ess_setpoint, safe: { value: 0 } }
  ```

- un dispozitiv citit prin MQTT nu are scriere brută de registru: Home
  Assistant arată o entitate number doar pentru un rând pe care îl scrie o
  comandă.

Pe partea GX-ului: pornește *Settings → Services → MQTT on LAN*. Dă-i
dispozitivului variabilele de topic `portal` (ID-ul de portal VRM) și `vebus`
(instanța invertorului/încărcătorului). Pe 8883, setează `tls_insecure: true`
(*Acceptă certificatul propriu* în formular) sau `tls_ca`.

În fila **Configurare → Conexiune** a unui dispozitiv MQTT, topicul,
variabilele de topic și parola apar ca ••••, pentru că topicul și variabilele
lui pot conține ID-ul de portal al GX-ului. Ochiul de lângă fiecare îl
arată. Variabilele de topic sunt perechi `nume=valoare` separate prin
virgulă; o valoare care conține ea însăși o virgulă urmată de `nume=` scrie
acea virgulă ca `\,`. Tot acolo se setează keepalive-ul: cum spune șablonul,
propriul lui (la 5–3600 s) sau oprit.

**Bateriile și senzorii GX-ului sunt componentele lui.** Șablonul declară
două tipuri, iar un GX ține câte vede din fiecare:

| Componentă | Citește |
|---|---|
| `battery` | un monitor de baterie (SmartShunt, BMV: SOC, Ah consumați, timp rămas, istoricul lui) sau un BMS pe VE.Can (Seplos, Pylontech…: SOH, extremele celulelor, limitele de încărcare, module, alarme) |
| `temperature` | un serviciu de temperatură: un RuuviTag sau alt senzor BLE (temperatură, umiditate, presiune, bateria senzorului) sau o intrare cu fir |

În pagina GX-ului, cardul **Componente** le arată.

- **Caută pe dispozitiv** întreabă GX-ul ce are și arată fiecare baterie și
  fiecare senzor, cu produsul și numele setat pe GX. *Adaugă* ia unul. Nu se
  scrie nimic pe GX.
- **Adăugarea de mână** ia instanța arătată pe GX (*Settings → Devices*, sau
  lista de dispozitive din VRM), de exemplu 288 sau 22.

Fiecare componentă e un dispozitiv de sine stătător pentru valorile,
istoricul, regulile și atributele ei. ID-ul ei e `<GX>-<tip>-<instanță>`
(`victron-gx-battery-288`) și publică sub `mbg/devices/<id>/…`. Nu are
conexiune proprie: citește prin cea a GX-ului, deci un GX cu cinci
componente e tot o conexiune și o republicare. Pagina ei arată din ce face
parte. Se redenumește, se oprește sau se scoate din cardul GX-ului.
Scoaterea îi păstrează selecția de măsurători, deci adăugarea aceleiași
instanțe o reia.

Un câmp pe care un model nu îl are nu ajunge niciodată. Componentele sunt
doar în citire; alarmele unei baterii se numără în pagina ei și în alerte.
Scrierea printr-o componentă și componentele pe dispozitive Modbus sau HTTP
nu există încă.

---

## 14c. Reguli — controlerul declarativ

O **regulă** decide *când* rulează o comandă (§14b) și cu *ce* parametri:
urmărește un semnal construit din registre live, așteaptă să se
stabilizeze și îi cere țintei ce vrea — o limită la supratensiune pe un
invertor, o revenire când rețeaua a dispărut. E declarativă intenționat
(intrări → un semnal sau o condiție cu semantică de timp → o comandă),
niciodată un mediu de scripting; orice dincolo de asta rămâne în Node-RED,
care vorbește cu aceleași comenzi. Două lucruri pe care un controler extern
nu ți le dă: bucla e **închisă** (citirea înapoi e deja în memorie, deci
regula comandă din nou la abatere, nu la un ceas) și **eșuează în siguranță**
(un semnal învechit nu relaxează niciodată o limită).

### 14c.1 Configureaz-o din interfață

1. Ținta trebuie să ofere o **comandă activată** (§14b.1); doar astfel de
   ținte apar în listă.
2. Pagina **Reguli** → **Adaugă regulă**. Întâi tipul: *Trepte* (praguri →
   valoare), *Condiție* (adevărat / fals) sau *Ajustare* (pas cu pas, sus /
   jos, până când semnalul stă într-o bandă). Ținta: o unitate sau un grup
   întreg (comanda se împarte pe unități). Semnalul: o expresie peste valori
   live scrise `dispozitiv.registru` — gramatica registrelor calculate
   (`min`, `max`, `avg`, `abs`, `+ - * /`; ID-urile cu cratimă merg). Apoi
   tabelul de trepte (*Semnal de la → Cere*, eticheta, *Imediat* = calea de
   urgență), *Normal din nou sub* (zona moartă), intervalul valid al
   semnalului și *pas maxim*, politicile la învechire și la dezactivare și
   câmpurile de timp — fiecare cu valoarea implicită și o explicație de un
   rând. **Previzualizare acum** arată ce ar vedea și ce ar vrea regula în
   această secundă, fără să păstreze nimic.
3. **Salvează.** O regulă nouă e salvată în **shadow** (butonul de mod se
   numește *În umbră*): evaluează, decide, scrie în jurnal și publică — și
   nu scrie nimic în echipament. Urmărește-i cardul: starea în cuvinte,
   semnalul, **cerut → real** pe unitate cu vârsta valorii reale, ultima
   decizie și motivul ei, **Decizii** (jurnalul).
4. **Armează…** — un buton separat, cu o confirmare care numește ținta.
   Armarea cere `security.allow_writes` și un apelant autentificat și e
   auditată. *În umbră* o readuce înapoi.

Tot pe card: **Plafon…** (un maxim pentru ce poate cere regula, pentru un
număr dat de ore), **Pauză…** (o suprascriere, în minute, ca tu sau alt
sistem să puteți comanda ținta), activare/dezactivare, Editează, Șterge.

### 14c.2 Același lucru în `rules.yaml`

Regulile stau în `config/rules.yaml` (trec peste mai multe dispozitive),
scrise de interfață și de API, incluse în snapshot-uri și backup-uri (§15).

```yaml
rules:
  - id: ov-u1
    label: Over-voltage protection · inverter 1
    enabled: true
    mode: shadow                                 # shadow | armed
    target: { device: pv-u1, command: power_limit }   # sau {endpoint: pv, group: inverters, command: …}
    kind: steps
    signal: "max(pv-u1.voltage_l1_n, pv-u1.voltage_l2_n, pv-u1.voltage_l3_n)"
    signal_valid: { min: 100, max: 350, max_step: 10 }
    stale_after_s: 60
    steps:                                       # crescător; câștigă cea mai mare treaptă potrivită
      - { at: 250.0, value: 80, label: Warning }
      - { at: 251.0, value: 70, label: Moderate }
      - { at: 252.5, value: 60, label: Severe }
      - { at: 253.0, value: 50, label: Emergency, fast: true }   # fără debounce
    release_below: 248.0                         # normal doar sub această valoare (zona moartă 248..250)
    normal: { value: 100 }
    params: { revert_s: 0, ramp_s: 10 }          # trimiși cu fiecare cerere
    timing: { every_s: 2, debounce: 3, min_interval_s: 30, reassert_s: 120 }
    on_stale: hold                               # hold | safe | un număr (eșuează în siguranță la el)
    on_disable: safe                             # safe | hold

  - id: grid-lost-restore                        # o condiție: adevărat → A, fals → B (sau nimic)
    target: { endpoint: pv, group: inverters, command: power_limit }
    kind: condition
    when: "pv-meter-240.frequency < 45 or pv-meter-240.voltage_ln_avg < 100"
    then: { params: { value: 100, revert_s: 0 } }
    else: null
    timing: { every_s: 2, debounce: 2, min_interval_s: 60 }
    stale_after_s: 30
```

**Mutarea regulilor.** **Export** (toate, sau una din cardul ei) dă un
fișier YAML cu definițiile; **Import** verifică întâi fiecare regulă pe acest
gateway (*nouă* / *înlocuiește* / *există deja* / *nu poate fi importată*, cu
motivul) și apoi le salvează pe cele valide. Regulile importate ajung mereu
în shadow, iar o regulă armată nu e niciodată înlocuită de un import. Rețete
și fluxul de lucru: **[rules.md](rules.md)**.

Valori implicite și limite: `every_s` 2 (0,5–3600), `debounce` 3 (1–100 de
eșantioane), `min_interval_s` 30, `reassert_s` 120 (0 = niciodată),
`stale_after_s` 60, `on_stale: hold`, `on_disable: safe`, `mode: shadow`.
`release_below` trebuie să fie la sau sub prima treaptă; `normal` e
obligatoriu. Toleranța citirii înapoi e toleranța `verify` a comenzii.
Design: [rules-design.md](rules-design.md).

### 14c.3 Cum decurge o evaluare și ce vezi

La fiecare `every_s`, motorul calculează semnalul (vârsta lui e a celei mai
**vechi** intrări), citește din memorie valoarea reală a țintei și cere o
**decizie**:

1. **Validitate** — un eșantion în afara `signal_valid.min/max` e *ignorat*
   (numărat). O schimbare mai mare decât `max_step` față de ultima citire
   acceptată e *ținută* până o confirmă eșantionul următor: un artefact de un
   eșantion nu ajunge niciodată la o treaptă, nici măcar la una `fast`; un
   salt real costă un interval de citire. Vârsta peste `stale_after_s` →
   starea **stale**: `hold` păstrează ultima cerere, `safe` cere valorile
   `safe` ale comenzii, un număr cere acea valoare.
2. **Dorit** — `steps`: cea mai mare treaptă cu `at <= semnal`; niciuna și
   semnal sub `release_below` → `normal`; între ele → se păstrează starea
   curentă. `condition`: `then` / `else`; cu `release`, răspunsul e reținut —
   odată ce `when` a fost adevărat rămâne `then` până devine adevărat
   `release` (histerezis, vezi
   [rules.md §3.5](rules.md#35-a-heat-pump-buffer-thermostat-condition-with-a-release)).
   `trim`: peste `above` → un `step` în sus (sau `gain` × distanța peste
   prag, cel mult `step`), sub `below` → unul în jos, între `min`..`max` și
   `off_value` sub `min`. Face o singură ajustare odată, fiecare trimisă și
   cu `min_interval_s` așteptat înainte de următoarea, pornind de la valoarea
   reală ([rules.md §3.6](rules.md#36-a-drive-that-soaks-up-the-solar-surplus-trim)).
   **Timp și temporizări:** expresiile citesc și ceasul (`hour()`,
   `weekday()`, `between("10:00", "16:00")`, în `ui.timezone`) și contoarele
   regulilor (`run_today_h("id")`, `starts_today("id")`). Temporizările sunt
   `for_s`, `min_on_s`, `min_off_s`, `max_on_s` (cu o pauză) și
   `max_starts_per_h`. Țin o pornire sau o oprire, niciodată calea sigură la
   învechire ([rules.md §1](rules.md#1-before-you-write-one)). `schedule`:
   ultima intrare `{days, at, value}` deja atinsă (până la o săptămână în
   urmă), sau `off_value` cât timp `when`-ul opțional e fals
   ([rules.md §1](rules.md#1-before-you-write-one)). O **forțare** (`POST
   /api/rules/{id}/force`, *Forțează…* din card, MQTT `{"force": …}`) cere o
   valoare pentru cel mult 24 h, oricare ar fi semnalul, apoi regula decide
   din nou. **Mai multe reguli pe o țintă** (faza 3): fiecare regulă are un
   `layer` (safety 90, manual 70, schedule 50, optimize 30) sau un
   `priority`; canalul țintei le combină după prioritate (câștigă cea mai
   mare care are o părere), `min` sau `max` (limite); o regulă care pierde
   *cedează*. Regulile de siguranță nu sunt niciodată puse pe pauză din
   afară. Protecția canalului (`min_off_s`, `max_starts_per_h`…) se aplică
   oricărui scriitor
   ([rules.md §3.9](rules.md#39-several-rules-on-one-target-layers)).
   **Secvențe** (pagina Reguli, `config/sequences.yaml`): comenzi în ordine
   cu `wait_s`, `wait_until` și `check`, anulate de `on_abort` când un pas
   eșuează sau rularea e oprită; pornite din card, din API sau MQTT
   `mbg/sequences/<id>/run`
   ([rules.md §3.10](rules.md#310-sequences-commands-in-order)). Apoi se
   aplică **plafonul** (`cerut = min(cerut, plafon)`; nu expiră niciodată
   *într-o* treaptă).
3. **Debounce** — o nouă valoare dorită are nevoie de `debounce`
   **eșantioane** consecutive care sunt de acord (citiri distincte, nu
   tic-uri); treptele `fast` sar peste.
4. **Trimitere** — dacă valoarea reală se potrivește deja, nu se trimite
   nimic; la mai puțin de `min_interval_s` de la ultima comandă → *ține
   (limită de rată)*. **Armată**: comanda rulează pe aceeași cale ca orice
   altă cale (`via: rule:<id>`, aceleași porți, audit și `…/cmd/result`).
   **Shadow**: *shadow (ar rula …)*.
5. **Bucla închisă** — fără nimic nou de cerut, valoarea reală e comparată cu
   cea cerută: diferită peste toleranță mai mult de `reassert_s` →
   *reassert*; valoarea reală lipsă sau mai veche de 3 × intervalul ei de
   citire → *sweep* (recitită acum, cel mult o dată pe minut), în loc de o
   scriere oarbă.

Publicat: **`mbg/rules/<id>/state`** reținut `{mode, enabled, state, signal,
units: {<unitate>: {state, want, clamp, paused_until, last_action, ignored,
guarded}}, decision, reason, ts}` la fiecare schimbare;
**`mbg/rules/<id>/event`** la treceri; jurnalul de evenimente și pagina
Status; un punct **`rule_event`** în bucket-ul InfluxDB al unității pentru
fiecare schimbare de stare sau de cerere și pentru fiecare comandă trimisă
(tag-uri `device`, `rule`, `state`, `action`, `result`; câmpuri `signal`,
`want_value`, `actual`, `reason`) — istoricul pe care îl citește Grafana;
inelul de decizii (200 pe regulă, `GET /api/rules/{id}/decisions`). Trei
comenzi eșuate la rând ridică o alertă (§12); regula continuă să încerce la
`min_interval_s`.

**`mbg/rules/<id>/set`** acceptă `{"enabled": false}`, `{"clamp": {"max":
60, "expires_s": 7200}}` / `{"clamp": null}`, `{"override_s": 900}` și un
`source` opțional (auditat). Mesajele reținute sunt aruncate; oricine poate
publica pe brokerul tău poate face aceste lucruri, deci ține brokerul cu
autentificare. **Armarea nu e disponibilă prin MQTT** — doar din interfață
și din API.

### 14c.4 Note de siguranță

- **Regulile noi sunt în shadow.** Armarea e un act separat, confirmat și
  auditat, care cere `security.allow_writes` și autentificare sau o cheie
  API. Folosește perioada de shadow: cerut vs real pe card e dovada.
- **Un singur stăpân pe țintă + comandă.** O regulă armată își deține ținta:
  o comandă din API, MQTT sau HA primește `409 rejected: owned by rule <id>`
  dacă nu trimite `override_s`, care pune regula pe pauză (auditat, vizibil
  pe card). Două reguli armate împart o țintă doar ca straturi ale canalului
  ei (§14c.3): câștigă stratul cel mai înalt care are o părere. În shadow,
  ținta rămâne a apelanților externi, deci un controler existent merge mai
  departe în timpul probei.
- **Eșuează în siguranță.** Învechirea nu relaxează niciodată: `hold`
  păstrează o limitare deja activă; un număr (`on_stale: 80`) e alegerea
  când „orb” trebuie să însemne „limitat” — dar acolo unde tensiunile pe
  faze citesc 0 V după ce invertoarele adorm, o valoare fixă ar fi scrisă în
  fiecare seară în invertoare adormite, de aceea implicit e `hold`. **A lăsa
  o limită în picioare e refuzat**: dezactivarea, dezarmarea sau ștergerea
  unei reguli armate care și-a mutat ținta de la valorile `safe` ale comenzii
  rulează întâi `safe` (`on_disable: safe`).
- **Restartul gateway-ului**: `rules.yaml` e reîncărcat; doar plafoanele și
  pauzele supraviețuiesc (`rules_state.json`). Starea e reconstruită din
  valorile live, cu debounce ca de obicei; o cerere pe care dispozitivul o
  ține deja nu e trimisă, deci un restart nu produce niciodată o scriere
  surpriză. O regulă a cărei țintă sau al cărei semnal nu se mai rezolvă
  arată *nu poate rula* și nu face nimic. **Repornirea dispozitivului**:
  valoarea reală se abate de la cea cerută, iar după `reassert_s` regula
  comandă din nou — dacă valoarea reală nu e prea veche, caz în care întâi o
  recitește. Cu scrierile oprite global, deciziile unei reguli armate sunt
  înregistrate ca `rejected`, iar alerta pornește după trei.
- **Țintele de grup** se împart pe unități — cerere, valoare reală și decizii
  pe unitate; starea regulii e cea a celei mai rele unități; o unitate care
  refuză nu le oprește pe celelalte.

### 14c.5 Depanare

| Simptom | Verifică |
|---------|-------|
| Cardul spune *nu poate rula* | ținta nu oferă comanda (neactivată pe grup), sau semnalul numește un registru care nu se rezolvă — eroarea e scrisă pe card |
| Starea blocată pe *stale* | cea mai veche intrare e mai veche decât `stale_after_s`: o sursă căzută, un ID de dispozitiv greșit în semnal, sau o valoare citită mai rar decât limita (un grup citit la 60 s sub `stale_after_s: 60` se învechește între citiri) |
| Multe *ignored* / *guarded* pe topicul de stare | semnalul iese din `signal_valid` (0 V noaptea e normal) sau sare mai mult decât `max_step` — un artefact singular a fost ținut; dacă se repetă, pasul maxim e prea strâns pentru acea sursă |
| Regula vrea o treaptă, dar nu se trimite nimic | shadow (*ar rula*), limita de rată (*mai sunt N s*), debounce (*2/3 spre Warning*), sau valoarea reală se potrivește deja |
| Comenzile din Node-RED / HA întorc acum 409 | o regulă armată deține ținta — trimite `override_s`, folosește *Pauză…*, sau pune regula înapoi în shadow |
| Armarea e refuzată | `security.allow_writes` oprit, fără autentificare / `API_KEY`, sau regula nu poate rula (motivul e pe card) |
| *reassert* la fiecare câteva minute | dispozitivul nu ține valoarea cerută (ceasul lui propriu de revenire, un al doilea scriitor) — citește `…/cmd/result` și jurnalul Decizii |
| Alerta *rule … cannot apply …* | trei eșecuri la rând: scrieri oprite, unitate blocată la scriere, protecție picată (model 123 / factor de scală), fără sursă Modbus — rulează comanda de mână din fila Comenzi a unității ca să vezi motivul |

---

## 15. Siguranța configurației: snapshot-uri, revenire, backup

Setări → **Backup și restaurare**.

- **Snapshot-uri automate** — fiecare schimbare reușită de configurație
  (dispozitive, registre, șabloane, contoare virtuale, setări) face un
  snapshot al întregului pachet de configurație (o întârziere de 2 s
  grupează rafalele). Se păstrează 50; snapshot-uri manuale cu o notă oricând.
- **Diferența semantică** — orice snapshot se compară cu configurația live
  sau cu alt snapshot **cheie cu cheie** (nu zgomot pe rânduri): *ce ar
  anula o revenire*. Valorile secrete sunt mascate.
- **Revenirea** — restaurarea înlocuiește pachetul exact cum e, după ce face
  un snapshot `pre-restore`, deci o revenire e și ea reversibilă.
- **Centura de siguranță la pornire (last-known-good, LKG)** — după ~5
  minute de funcționare sănătoasă, pachetul e marcat LKG. Dacă
  `config.yaml` nu poate fi citit la pornire (o editare manuală greșită, o
  scriere întreruptă), gateway-ul restaurează automat LKG și pornește — o
  cutie nesupravegheată revine singură. Un fișier corupt blochează și
  salvările (o copie e păstrată ca `config.yaml.bad`), deci valorile
  implicite nu pot suprascrie niciodată configurația ta reală.
- **Autorepararea configurației (`config.yaml.good`)** — independent de
  snapshot-uri, fiecare încărcare reușită păstrează o copie
  `config.yaml.good`; o editare coruptă ulterioară revine la acel fișier
  **bun cunoscut** (niciodată la valorile implicite goale), deci dispozitivul
  principal citește mai departe hostul corect în timpul unei editări
  greșite. Situația apare ca `config.healthy` în `/api/status` și ridică o
  alertă; salvările rămân blocate până se repară fișierul. Repararea prinde
  și cazul subtil al unui fișier care încă *se citește*, dar e un rest trunchiat
  (fișier gol, un singur scalar, tăiat înaintea secțiunilor pe care le scrie
  orice salvare): o verificare de plauzibilitate îl trimite pe aceeași cale
  `.bad` + reparare, iar un snapshot care ar *pierde dispozitive* nu e
  niciodată promovat peste `.good`-ul existent (o ștergere intenționată de
  dispozitiv reîmprospătează `.good` prin salvarea însăși). Selecțiile de
  registre au același contract: `selected_registers.json` — al
  dispozitivului principal **și al fiecărui dispozitiv** — își păstrează
  perechea `.good`/`.bad`, deci o selecție trunchiată se repară în loc să
  golească pe tăcute toate citirile.
- **Export/import de backup (ZIP)** — pentru mutarea între hosturi.
  Exportul **scoate secretele** (acreditările MQTT/Influx, hash-urile
  parolelor, header-ele de webhook/REST push) și identitatea hostului
  implicit; `include_secrets=true` cere rolul de admin (cheia API ține loc
  doar cât timp autentificarea e oprită) și e scris în audit. Importul (corp
  ZIP brut, ≤25 MB) **se combină** peste configurația live, deci secretele
  scoase rămân, și face întâi un snapshot `pre-import`. Snapshot-urile, în
  schimb, sunt puncte de restaurare locale complete — descărcarea unuia e
  păzită ca un export cu secrete.

**Ce intră într-un backup/snapshot:** `config.yaml`, registrele selectate
ale fiecărui dispozitiv, șabloanele de dispozitiv ale utilizatorului,
`virtual_meters.yaml` + șabloanele contoarelor virtuale din
`config/templates/`, presetările de registre calculate și regulile
(`rules.yaml`). Registrul de passkeys (`passkeys.json`) intră **doar** în
backup-ul cu secrete (`include_secrets=true`) și în snapshot-uri — altfel o
restaurare ar deschide autentificarea.

**Ce NU intră (și cum îl salvezi separat):**
- `audit.jsonl` / `events.jsonl` — istoric operațional, se rotesc singure;
  copiază-le de mână dacă îți trebuie pentru analiză.
- `write_leases.json` — trecător prin design (lease-urile se refac singure).

**Atenție:** snapshot-urile stau sub `config/snapshots/` în volumul de
configurație — te apără de editări greșite, nu de pierderea volumului.
Păstrează și un ZIP exportat în altă parte.

**Recuperarea după dezastru (host pierdut / volum pierdut), de la cap la
coadă:**

1. Pe hostul nou: `git clone … && cp .env.example .env && docker compose
   up -d` — un gateway nou pornește cu acreditările primei rulări (scrise o
   dată în jurnal).
2. Autentifică-te, mergi în Setări → **Backup și restaurare** →
   **Restaurează backup**, încarcă ZIP-ul exportat (un export cu secrete
   restaurează și passkeys-urile și acreditările dispozitivelor; unul curățat
   îți cere să reintroduci secretele).
3. Importul aplică dintr-o dată configurația, dispozitivele, selecțiile de
   registre, contoarele virtuale și presetările calculate. Cu **mai multe
   dispozitive**, răspunsul poate seta `restart_required` — repornește
   containerul (`docker compose restart multi-bus-gateway`) ca fiecare
   citire să pornească din definițiile restaurate.
4. Verifică: `/health` arată contoarele, Status arată fiecare dispozitiv
   citind, iar ESS-ul/consumatorii tăi se reconectează singuri la porturile
   contoarelor virtuale.

---

## 16. Securitate

**Autentificarea e PORNITĂ de la prima rulare** — o instalare nouă
generează o parolă de admin (scrisă o dată în jurnal) și activează
autentificarea. Celelalte niveluri (lista de IP-uri permise, cheia API, TLS,
adresa canonică) sunt opționale — pornește-le din **Setări → Securitate** pe
măsură ce crește expunerea. Apărare în adâncime: fiecare nivel se aplică
independent.

> **Stiva inclusă are propriile uși.** Compose-ul implicit expune și
> brokerul (`1883`/`9001`, cu autentificare din 3.81.0 prin
> `MQTT_USERNAME`/`MQTT_PASSWORD` din `.env`), MQTT Explorer (`4000`, un
> vizualizator fără autentificare — oprit dacă nu folosești
> `--profile debug`, doar local pe host când e pornit), InfluxDB (`8086`) și
> Grafana (`3000`, utilizator `admin` / `GF_SECURITY_ADMIN_PASSWORD` din
> `.env`). Pe orice dincolo de un LAN de încredere, setează cele patru
> secrete obligatorii în `.env` (brokerul refuză valoarea de exemplu), lasă
> `STACK_BIND=127.0.0.1` și scoate mapările de porturi de care nu ai nevoie.

### 16.1 Autentificare și roluri

Un cont de **admin**, plus conturi opționale de **operator** și **viewer**:

| Rol | Poate | Nu poate |
|---|---|---|
| `viewer` | să vadă totul (GET), să ruleze interogările de registre la cerere | să schimbe ceva |
| `operator` | acțiuni live: diagnostic, captura de magistrală, descoperire, testele dispozitivelor și instalațiilor, mostre de mesaje, **scrieri Modbus** și comenzi (în limitele șablonului), rularea secvențelor, testul de alertă, reîncărcarea registrelor, propriile passkeys | orice ajunge într-un fișier de configurație (dispozitive, registre, șabloane, contoare virtuale, setări, snapshot-uri), jurnalul de audit |
| `admin` | tot | — |

Parolele sunt păstrate ca hash (PBKDF2-SHA256, 600k iterații); lasă un câmp
de parolă gol la salvare ca s-o păstrezi pe cea curentă. **Activarea
autentificării cu o parolă de `admin` goală sau implicită e refuzată la
orice intrare** — ruta din interfață, un `config.yaml` editat de mână, un
import de configurație și o restaurare de snapshot trec toate prin aceeași
verificare, deci nicio cale nu dă un gateway care *pare* închis, dar acceptă
`admin/admin`. Autentificările eșuate blochează IP-ul
(`lockout_threshold` / `lockout_minutes`, implicit 5 / 5 min). Sesiunile
sunt cookie-uri HttpOnly, de 7 zile glisante, cu o limită absolută de 30 de
zile (3.80.0: cookie-ul e reemis pe măsură ce sesiunea glisează, deci o filă
folosită zilnic rămâne autentificată până la limită), salvate ca hash-uri
SHA-256 ale tokenurilor în `config/sessions.json` — un restart al
containerului te lasă autentificat. Schimbarea oricărei parole revocă
**toate** sesiunile (un cookie vechi nu poate supraviețui schimbării) —
mai puțin a autorului: salvarea de securitate îți reemite propria sesiune,
deci schimbarea parolelor nu *te* scoate niciodată din lucru. Același lucru
se întâmplă când un import de backup sau o restaurare de snapshot schimbă
acreditările (3.84.0): toate sesiunile sunt revocate, a adminului care
restaurează e reemisă, registrul de passkeys e reîncărcat din fișierul
restaurat, iar passkeys-urile conturilor care nu mai există sunt șterse.
Jurnalul de audit e doar pentru admin.

### 16.2 Passkeys (WebAuthn)

Autentificare fără parolă, cu un autentificator de platformă sau o cheie de
securitate. Înscrierea se face din meniul utilizatorului (fiecare cont și le
gestionează pe ale lui; adminul le vede pe toate). Cerințe și capcane:

- Browserele rulează WebAuthn doar într-un **context securizat**: deschide
  interfața prin `localhost` (pe cutie) sau printr-un **nume de host peste
  HTTPS** (de ex. `gateway.lan` în spatele Traefik). O adresă IP simplă e
  refuzată — RP ID-ul trebuie să fie un nume de host.
- Un passkey e legat de numele de host pe care a fost înscris; alt nume =
  înscrii din nou.
- Autentificarea cu passkey împarte blocarea cu parola, iar înscrierea se
  poate face **înainte** de pornirea autentificării, deci nu rămâi niciodată
  blocat la jumătatea migrării.

### 16.3 HTTPS și reverse proxy (Traefik)

- **TLS inclus**: îndreaptă Setări → Securitate spre un certificat + cheie
  din `config/`, sau lasă gol ca să se genereze automat o pereche
  autosemnată (**restart ca să se aplice**; browserele avertizează la
  certificatele autosemnate). Cheile private generate au modul `0600`.
- **În spatele unui reverse proxy** (recomandat pentru certificate reale):
  termină TLS în Traefik/nginx/Caddy și setează `ui.trusted_proxies` la IP-ul
  proxy-ului (de ex. adresa containerului Traefik). Doar atunci sunt luate în
  seamă `X-Forwarded-For` / `X-Forwarded-Proto` — altfel lista de IP-uri
  permise, blocarea la autentificare și jurnalul de audit ar vedea toate
  proxy-ul în locul clientului real, iar cookie-urile de sesiune nu ar fi
  marcate Secure. Gol (implicit) = nu se are încredere în nimeni. Ideea
  minimă pentru Traefik: rutează `gateway.example.com` → `:8080` și adaugă
  IP-ul de rețea al containerului gateway în `trusted_proxies`.
- **Adresa canonică** (`ui.canonical_url`): când cutia e accesibilă printr-un
  nume de host propriu peste HTTPS, setează-o (de ex. `https://gateway.lan`).
  Interfața injectează atunci o mică redirecționare în browser care trimite
  orice vizitator venit prin IP sau HTTP simplu spre originea canonică —
  astfel cookie-urile, passkeys-urile și HSTS se leagă toate de un singur
  nume de host. E o direcționare în browser, nu o redirecționare de server,
  deci IP-ul nu e niciodată *blocat*: adaugă **`?local`** la URL ca să rămâi
  pe IP (setează un semnal persistent `mbg-stay-local` în acel browser), care
  e ieșirea de urgență când DNS-ul sau proxy-ul nu merge și trebuie să ajungi
  direct la cutie.

### 16.4 Lista de IP-uri permise

`security.allowlist` — câte un IP/CIDR pe rând (de ex. `192.168.1.0/24`);
gol = deschis. Loopback-ul e mereu permis; păzește și `/ws`, `/health` și
`/metrics`. Cardul arată *IP-ul tău curent*, ca să nu te blochezi singur.
**Notă Docker:** în spatele rețelei bridge implicite, clienții apar adesea
ca IP-ul gateway-ului Docker — verifică „IP-ul tău curent” și permite ce vezi
de fapt, sau folosește rețea host/macvlan pentru filtrare reală pe client.
Te-ai blocat totuși? Editează `security.allowlist` în `config/config.yaml`
și repornește.

### 16.5 Cheia API

Setează `API_KEY` în mediu ca să ceri `X-API-Key` la fiecare cerere care
schimbă ceva, independent de autentificare — util pentru scripturi și CI.
GET-urile doar în citire și POST-urile de interogare la cerere rămân
deschise.

### 16.5b Protecții în browser

Cererile care schimbă ceva venite de pe **alt site** sunt respinse direct
(verificările `Sec-Fetch-Site` / `Origin` — o pagină rău-intenționată din
browserul operatorului nu poate declanșa schimbări de configurație),
inclusiv pe `/ws`. Se aplică header-ele standard de securitate, inclusiv pe
pagina de autentificare; politica Content Security nu permite script inline
(`script-src 'self'` — interfața nu are handlere de evenimente inline,
fiecare acțiune e un atribut `data-action`), deci o valoare care scapă de
escaparea la afișare tot nu se poate executa. Valoarea adresei canonice e
escapată la afișare împotriva XSS-ului salvat. Secretele nu se întorc
niciodată în browser: exporturile, listele de mediu și jurnalele ascund
tokenurile și hash-urile parolelor.

### 16.6 Jurnalul de audit

JSONL doar cu adăugare în `config/audit.jsonl` (rotire 1 MB × 5 fișiere):
autentificări (inclusiv eșecuri și blocări), evenimente de passkey,
**fiecare scriere Modbus**, exporturi/importuri de configurație, restaurări
de snapshot — cu utilizatorul, IP-ul, acțiunea, ținta, starea; valorile
secrete din mesaje sunt ascunse înainte de scriere. Vezi-l și filtrează-l în
pagina Status (admin), sau exportă CSV prin `GET /api/audit/export.csv`.

---

## 17. Observabilitate: Status, /metrics, evenimente

- **Pagina Status** — sănătatea fiecărui dispozitiv (conectat, rata de
  citire, tipurile de erori, vârsta datelor, latența), statisticile
  ieșirilor (publicate/sărite/eșuate, contoarele tamponului), clienții
  WebSocket, resursele procesului (CPU, RSS, thread-uri, descriptori de
  fișiere), evenimentele și alertele recente.
- **`/metrics`** — format text Prometheus, fără autentificare (un colector
  nu se poate autentifica), dar tot în spatele listei de IP-uri permise; doar
  contoare și sănătate, niciodată configurație. Serii:
  `gateway_device_up/poll_rate/reads_total/errors_total/read_latency_ms/staleness_seconds/health`,
  `gateway_mqtt_connected/published_total`,
  `gateway_influx_connected/written_total/buffer_points/dropped_total`,
  `gateway_vmeter_up/requests_total/request_rate/errors_total/connections/quality`.
  Exemplu de colectare:

  ```yaml
  scrape_configs:
    - job_name: multi-bus-gateway
      static_configs: [{ targets: ["gateway:8080"] }]
  ```
- **`/health`** — sonda containerului. Întoarce HTTP 503 **doar** când un
  contor virtual activat chiar nu merge (ceva ce un restart ar putea
  repara); un contor sursă inaccesibil degradează starea din corp, dar rămâne
  HTTP 200 — repornirea containerului nu îți repară cablajul, iar ceasul de
  prospețime al contorului virtual protejează deja consumatorii.
- **Jurnalul de evenimente** — inel salvat (`config/events.jsonl`, ultimele
  300): eșecuri de citire, conectarea/deconectarea ieșirilor, ciclul de viață
  al contoarelor virtuale, reveniri, alerte.

---

## 18. Limbi și fus orar

- **Limbi** — interfața vine în engleză și română; selectorul e în bara de
  titlu. Limbile sunt fișiere simple: copiază `ui/languages/en.json` în
  `<cod>.json`, traduce-l și apare în selector — fără reconstruire. Un număr
  folosește formele de plural ale limbii (`cheie.one`, `cheie.few`,
  `cheie.other`…); `python tools/i18n_check.py` arată ce îi mai lipsește unei
  traduceri. Vezi `ui/languages/README.md`.
- **Fus orar** — Setări → General. O zonă IANA (verificată) care stabilește
  granițele lunilor pentru energia lunară; aplicată live. Implicit
  `Europe/Bucharest`.

---

## 18b. Rularea pe echipamente modeste (Raspberry Pi)

Multi-Bus Gateway merge confortabil pe un Raspberry Pi 3/4/5 sau pe o cutie
Intel de consum redus. E econom prin design — la o instalare tipică cu un
singur contor folosește ~90 MB RAM și câteva procente de CPU, cu tampoane
limitate și fără scurgeri.

**Singurul lucru care contează pe un Pi: intervalul de citire realtime.**
Tot ce face gateway-ul pe fiecare valoare (citire, interpretare, publicare,
tamponare) se întâmplă sub un singur GIL Python, deci CPU-ul total crește cu
*citirile pe secundă*, iar un nucleu Cortex-A53 e de ~8–10 ori mai lent decât
unul Intel de desktop. RAM-ul și numărul de thread-uri nu sunt niciodată
limita — CPU-ul la un ritm rapid este.

**Capacitatea (măsurată + estimată):**

| Configurație | RSS | Thread-uri | CPU RPi 3 la realtime 1 s | RPi 3 la 250 ms |
|-------|-----|---------|--------------------------|----------------|
| 1 dispozitiv | 65–80 MB | ~14 | 3–5 % | 10–18 % |
| 5 dispozitive + 3 contoare virtuale | 90–110 MB | ~30 | 15–20 % | 55–75 % |
| 10 dispozitive + 10 contoare virtuale | 130–160 MB | ~58 | 35–50 % | saturează un nucleu |

**Setări recomandate pe un Pi 3:**

- Păstrează **realtime la 1 s** pentru monitorizare generală. Rezervă citirea
  sub o secundă (de ex. 250 ms) pentru *singurul* registru care conduce o
  buclă de control în timp real — un contor de rețea care alimentează limita
  de export a unui invertor — nu pentru toată harta.
- Folosește grupurile **normal (5 s)** și **slow (60 s)** pentru tot ce nu e
  critic pentru control (contoare de energie, temperaturi, diagnostic).
- Intervalul minim e 50 ms; `0` e refuzat (ar inunda magistrala).
- Un Pi 4/5 are de ~2–3 ori rezerva unui Pi 3 — ritmul de 250 ms e în regulă
  acolo pentru o hartă mică.

**Regulă practică:** un RPi 3 duce confortabil **~4–5 dispozitive + ~3
contoare virtuale** cu realtime ≥ 1 s. Peste atât, extinde *în lățime* (un
al doilea Pi / host pe magistrală), nu *în sus* — GIL-ul nu poate fi depășit
într-un singur proces.

## 18c. Ce văd consumatorii când se pierde o sursă

Promisiunea de bază a gateway-ului: **nu raportează niciodată lipsa ca pe o
valoare plauzibilă.** Când un dispozitiv nu mai răspunde, o citire eșuează
sau o conexiune cade, nicio ieșire nu primește un `0`/`false`/ultima
ghicire inventată. Lipsa rămâne lipsă — dar *arată* diferit pe fiecare
ieșire, deci un sistem din aval (Node-RED, Home Assistant, un PLC, Grafana)
trebuie să citească semnalul potrivit.

### Comportamentul pe fiecare ieșire

| Ieșire | La pierderea sursei | Cum detectează consumatorul |
|--------|----------------|-----------------------------|
| **MQTT** | topicurile de valoare nu se mai actualizează; topicul reținut păstrează ULTIMA valoare | abonează-te la `<prefix>/status` (reținut) + LWT — `offline` înseamnă învechit; NU te baza doar pe un topic de valoare |
| **InfluxDB** | fără scriere → un **gol** în serie (niciodată o linie plată cu valoarea veche) | `last()` + vârsta timestamp-ului, sau o alertă „no data” |
| **Contor virtual** | politica aleasă: `legacy` păstrează ultimele cuvinte · `fail` întoarce o excepție Modbus · `sentinel` servește N/A SunSpec (NaN / 0x8000 / 0xFFFF) · `hold` păstrează până la o limită, apoi eșuează. Dacă TOATE sursele sunt învechite, serverul se oprește (conexiune refuzată, ca un contor scos din priză). | excepția / conexiunea refuzată și **blocul de calitate în bandă la 61440** (stare + vârstă) |
| **HTTP push / ieșire** | nicio trimitere nouă (ultimul mesaj nu e retrimis ca proaspăt) | lipsa unei actualizări; NaN/inf sunt respinse, niciodată emise ca JSON invalid |

### Singurul lucru de configurat în aval

Pe **MQTT**, *valoarea* reținută nu are un marcaj de prospețime pe valoare
— semnalul de prospețime e pe topicul `<prefix>/status` și în Last-Will. Un
consumator care citește doar valoarea și ignoră starea poate acționa pe date
învechite. De aceea o buclă de control ar trebui să se condiționeze pe
prospețime (de ex. în Node-RED `armat ∧ lider ∧ telemetrie-proaspătă`), nu
doar pe „am o valoare”. Dacă îți trebuie prospețime în bandă **fără** MQTT,
dă-i contorului virtual politica `fail` și citește blocul de calitate la
61440.

### Comparația cu alte echipamente

Comportamentul urmează standardele consacrate, în loc să inventeze unele
proprii:

- **SunSpec** (modelul de facto pentru solar/contoare) definește exact
  marcajele de „inaccesibil” — NaN pentru float, `0x8000`/`0xFFFF` pentru
  întregi — pe care le emite politica `sentinel`, deci un consumator care
  înțelege SunSpec le înțelege nativ.
- **Home Assistant / MQTT**: topicul de disponibilitate `<prefix>/status` +
  Last-Will e tiparul standard de disponibilitate pe care îl folosește tot
  ecosistemul.
- **Serii de timp (InfluxDB / Prometheus)**: un gol (fără scriere) e
  reprezentarea idiomatică și corectă a datelor lipsă — nu completezi
  niciodată cu o valoare învechită.

Acolo unde gateway-urile Modbus industriale obișnuite *păstrează implicit
ultima valoare fără niciun semnal de învechire* — capcana periculoasă —
acest gateway oferă asta (`hold`) doar ca alegere explicită, lângă varianta
mai sigură `fail` (o excepție de pierdere a contorului) și blocul de
calitate în bandă. Pe scurt: nimic riscant nu e reinventat; unde industria
are o capcană, e oferită alternativa corectă.

## 19. Depanare

| Simptom | Verifică |
|---------|-------|
| Punctul Modbus e roșu | host/port/unit id corecte? Modbus TCP activat pe dispozitiv? firewall? Folosește Diagnostic → sonda: un răspuns cu excepție înseamnă tot că dispozitivul e viu |
| Valorile arată a gunoi | tip de date sau ordinea cuvintelor greșite — rulează sonda de registre și citește matricea tip×ordine |
| O citire unită eșuează cu *illegal data address* | slave strict / cu goluri — setează `max_gap: 0` pe dispozitiv |
| Interfața arată o versiune veche după actualizare | o pagină deschisă peste actualizare arată *Gateway-ul a fost actualizat* — apasă **Reîncarcă**; dacă revine pagina veche, reîncarcă forțat (un proxy poate ține cache) |
| Contorul virtual „învechit / pornește” | sursa nu e proaspătă — verifică conexiunea dispozitivului; ceasul de prospețime refuză intenționat să servească date învechite |
| Consumatorul nu ajunge la un contor virtual | portul e în plaja publicată din compose? accesibil din rețeaua consumatorului? urmărește fila Jurnal pentru citirile care sosesc |
| Lipsesc entitățile MQTT în HA | brokerul e accesibil? descoperirea e pornită? verifică `docker compose logs` |
| Avertismente de reîncercare la scrierea în InfluxDB | URL/token/bucket corecte? Clientul reîncearcă ~5 min, apoi lotul e recuperat în tampon și retrimis — urmărește `replayed_total`/`dropped_total` |
| Autentificarea refuză să se activeze | setează întâi o parolă nouă de admin — `admin` implicit nu poate fi folosit |
| Blocat (autentificare) | așteaptă `lockout_minutes`, sau repornește containerul (numărătoarea blocării e în memorie; sesiunile supraviețuiesc restartului) |
| Blocat (lista de IP-uri permise) | editează `security.allowlist` în `config/config.yaml`, repornește |
| Înscrierea passkey eșuează | ești pe un URL cu IP sau pe HTTP simplu — folosește `localhost` sau un nume de host peste HTTPS |
| Configurația s-a stricat după o editare | pornirea restaurează automat ultima configurație bună; fișierul stricat e păstrat ca `config.yaml.bad`; sau revino la un snapshot din Setări → Backup și restaurare |
| Scrierile întorc 403 | `security.allow_writes` oprit, fără acreditare (autentificare/cheie API), registrul nedeclarat inscriptibil, sau valoare în afara `write_min`/`write_max` |

Tot blocat? Deschide un issue — include `docker compose logs` și
configurația ta (cu secretele ascunse).
