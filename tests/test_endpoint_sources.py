# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""One endpoint, several SOURCES — because identity belongs to the unit.

A master device offers the same slave over several protocols. A Fronius
DataManager speaks Modbus TCP (complete, measured 1945-2376 ms a read) and a
Solar API over HTTP (partial, 54 ms, and it does not disturb the Modbus side).
Declaring that as two endpoints gave two device identities, two topic trees and
two sets of aggregates for ONE physical inverter — duplicated data dressed up as
a feature, and it was removed from production the evening it was built.

A source is only HOW a value arrived. These tests keep that line from blurring:
one unit, one topic, one bucket, one history, fed by an ordered list of sources
where the order IS the precedence.
"""
from tests.test_devices import write_config

TWO_SOURCES = """
endpoints:
  - id: fronius
    name: PV plant
    units: [1, 2]
    sources:
      - id: sunspec
        protocol: tcp
        host: 192.168.1.240
        port: 502
        template: fronius_sunspec_inverter
        poll_groups: { normal: { interval: 20 }, slow: { interval: 120 } }
      - id: solar_api
        protocol: http
        url: "http://192.168.1.240/api.cgi?DeviceId=${unit_id}&p=${endpoint_id}"
        template: fronius_solar_api_inverter
        poll_groups: { realtime: { interval: 5 } }
        stale_after_s: 15
"""


# ── the shape ────────────────────────────────────────────────────────────────

def test_one_endpoint_declaration_reaches_every_unit_over_every_source(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=TWO_SOURCES)
    devs = cfg.endpoint_devices('fronius')
    assert [d.id for d in devs] == ['fronius-u1', 'fronius-u2']
    for d in devs:
        assert [s.id for s in d.sources] == ['sunspec', 'solar_api']


def test_identity_belongs_to_the_unit_not_to_the_source(tmp_path):
    """The whole point. Two sources must not become two topic trees."""
    cfg = write_config(tmp_path, extra_yaml=TWO_SOURCES)
    d = cfg.get_device('fronius-u1')
    assert d.mqtt_topic_prefix == 'mbg/devices/fronius-u1'
    assert d.influxdb_device_tag == 'fronius-u1'
    # and nothing in the routing identity mentions a source
    for s in d.sources:
        assert s.id not in d.mqtt_topic_prefix
        assert s.id not in d.influxdb_device_tag


def test_each_source_keeps_its_own_address_template_and_rhythm(tmp_path):
    """A slow complete source and a fast partial one do not share a rhythm."""
    cfg = write_config(tmp_path, extra_yaml=TWO_SOURCES)
    by_id = {s.id: s for s in cfg.get_device('fronius-u2').sources}

    mb = by_id['sunspec']
    assert mb.protocol == 'tcp' and mb.template == 'fronius_sunspec_inverter'
    assert mb.connection.host == '192.168.1.240' and mb.connection.unit_id == 2
    assert {k: v.interval for k, v in mb.poll_groups.items()} == {'normal': 20, 'slow': 120}

    api = by_id['solar_api']
    assert api.protocol == 'http' and api.template == 'fronius_solar_api_inverter'
    # an HTTP master addresses its units by URL, not by a unit id in a frame
    assert api.http['url'].endswith('DeviceId=2&p=fronius')
    assert {k: v.interval for k, v in api.poll_groups.items()} == {'realtime': 5}


def test_the_order_of_declaration_is_the_precedence(tmp_path):
    """First source to offer a field owns it; a later one fills in only once the
    earlier has gone stale. Freshest-wins was rejected: it oscillates."""
    cfg = write_config(tmp_path, extra_yaml=TWO_SOURCES)
    srcs = cfg.get_device('fronius-u1').sources
    assert srcs[0].id == 'sunspec'          # declared first, so it wins ties
    assert srcs[0].stale_after_s == 0.0     # never yields (safe for counters)
    assert srcs[1].stale_after_s == 15.0    # may take over after 15 s of silence


# ── nothing existing breaks ──────────────────────────────────────────────────

def test_a_plain_connection_is_the_shorthand_for_one_source(tmp_path):
    """Every config written before sources existed is a one-source endpoint, and
    `dev.connection` must keep meaning what it always meant."""
    cfg = write_config(tmp_path, extra_yaml="""
endpoints:
  - id: legacy
    template: fronius_sunspec_inverter
    connection: { protocol: tcp, host: 192.0.2.7, port: 502 }
    units: [1, 2]
""")
    d = cfg.get_device('legacy-u1')
    assert len(d.sources) == 1
    s = d.sources[0]
    assert s.id == 'default'
    assert s.template == 'fronius_sunspec_inverter'
    assert s.connection.host == d.connection.host == '192.0.2.7'
    assert s.connection.unit_id == d.connection.unit_id == 1
    assert d.protocol == 'tcp'


def test_a_plain_device_also_gets_its_one_source(tmp_path):
    cfg = write_config(tmp_path, extra_yaml="""
devices:
  - id: solo
    template: eastron_sdm120
    connection: { protocol: tcp, host: 192.0.2.8, unit_id: 9 }
""")
    d = cfg.get_device('solo')
    assert [s.id for s in d.sources] == ['default']
    assert d.sources[0].connection.unit_id == 9


# ── the guards ───────────────────────────────────────────────────────────────

def test_two_sources_with_one_name_are_refused(tmp_path):
    """Duplicate ids would make provenance ambiguous, which defeats recording
    it at all."""
    cfg = write_config(tmp_path, extra_yaml="""
endpoints:
  - id: dup
    units: [1]
    sources:
      - { id: a, protocol: tcp, host: 192.0.2.1, template: eastron_sdm120 }
      - { id: a, protocol: tcp, host: 192.0.2.2, template: eastron_sdm120 }
""")
    srcs = cfg.get_device('dup-u1').sources
    assert [s.id for s in srcs] == ['a']
    assert srcs[0].connection.host == '192.0.2.1'     # the first one wins


def test_a_malformed_source_is_skipped_not_fatal(tmp_path):
    """One bad entry must not cost the whole boot — the same rule devices[]
    already follows."""
    cfg = write_config(tmp_path, extra_yaml="""
endpoints:
  - id: mixed
    units: [1]
    sources:
      - { id: good, protocol: tcp, host: 192.0.2.1, template: eastron_sdm120 }
      - { id: bad, protocol: tcp, host: 192.0.2.2, port: "not-a-port" }
""")
    assert [s.id for s in cfg.get_device('mixed-u1').sources] == ['good']


def test_a_source_inherits_the_endpoint_template_when_it_declares_none(tmp_path):
    cfg = write_config(tmp_path, extra_yaml="""
endpoints:
  - id: inherit
    template: eastron_sdm120
    units: [1]
    sources:
      - { id: only, protocol: tcp, host: 192.0.2.1 }
""")
    assert cfg.get_device('inherit-u1').sources[0].template == 'eastron_sdm120'


# ── the primary is a device too ──────────────────────────────────────────────

def test_the_primary_device_has_a_source(tmp_path):
    """It is synthesized from the flat sections rather than built from a raw
    dict, so it must be handed its source explicitly. Without one it has no way
    to be reached at all — every device is started from its sources — and the
    Janitza went dark in production for exactly this reason."""
    from tests.test_devices import write_config
    cfg = write_config(tmp_path)
    d = cfg.primary_device
    assert [s.id for s in d.sources] == ['default']
    s = d.sources[0]
    assert s.protocol == 'tcp'
    # the SAME ModbusConfig object, so a live UI edit still reaches the poller
    assert s.connection is cfg.modbus
    assert s.template == 'janitza_umg512_pro'


def test_every_device_can_be_started_from_its_sources(tmp_path):
    """The invariant the regression broke: no device may have an empty source
    list, or build_device_client silently returns None and it never polls."""
    from tests.test_devices import write_config
    cfg = write_config(tmp_path, extra_yaml="""
devices:
  - id: extra
    template: eastron_sdm120
    connection: { protocol: tcp, host: 192.0.2.5 }
endpoints:
  - id: ep
    template: eastron_sdm120
    connection: { protocol: tcp, host: 192.0.2.6 }
    units: [1, 2]
""")
    assert cfg.devices
    for d in cfg.devices:
        assert d.sources, f"device {d.id} has no source — it would never poll"


# ── a plant holds more than one kind of thing ────────────────────────────────

PLANT = """
endpoints:
  - id: fronius
    name: Fronius PV
    mqtt: { topic_prefix: "mbg/devices/${device_id}" }
    groups:
      - id: inverters
        role: inverter
        template: fronius_sunspec_inverter
        connection: { protocol: tcp, host: 192.168.1.240, port: 502 }
        units: [1, 2]
      - id: grid
        role: meter
        template: fronius_sunspec_meter
        connection: { protocol: tcp, host: 192.168.1.240, port: 502 }
        units:
          - { unit_id: 240, id: fronius-meter-240, name: Grid meter }
"""


def test_one_plant_holds_inverters_and_its_meter(tmp_path):
    """A PV plant is an installation, not one kind of device. Modelling the
    meter as a separate endpoint was the same duplication we removed at the
    source level, one level up."""
    from tests.test_devices import write_config
    cfg = write_config(tmp_path, extra_yaml=PLANT)
    devs = {d.id: d for d in cfg.endpoint_devices('fronius')}
    assert set(devs) == {'fronius-u1', 'fronius-u2', 'fronius-meter-240'}
    assert devs['fronius-u1'].group_id == 'inverters'
    assert devs['fronius-u1'].role == 'inverter'
    assert devs['fronius-meter-240'].group_id == 'grid'
    assert devs['fronius-meter-240'].role == 'meter'


def test_a_group_brings_its_own_template(tmp_path):
    from tests.test_devices import write_config
    cfg = write_config(tmp_path, extra_yaml=PLANT)
    devs = {d.id: d for d in cfg.endpoint_devices('fronius')}
    assert devs['fronius-u1'].template == 'fronius_sunspec_inverter'
    assert devs['fronius-meter-240'].template == 'fronius_sunspec_meter'


def test_a_units_hand_written_id_survives_being_grouped(tmp_path):
    """Moving the meter into the plant must not rename it: `fronius-meter-240`
    keeps its topic, its bucket tag and its history."""
    from tests.test_devices import write_config
    cfg = write_config(tmp_path, extra_yaml=PLANT)
    d = cfg.get_device('fronius-meter-240')
    assert d is not None
    assert d.mqtt_topic_prefix == 'mbg/devices/fronius-meter-240'


def test_a_group_can_be_switched_off_on_its_own(tmp_path):
    from tests.test_devices import write_config
    cfg = write_config(tmp_path, extra_yaml=PLANT.replace(
        "      - id: grid\n", "      - id: grid\n        enabled: false\n"))
    devs = {d.id: d for d in cfg.endpoint_devices('fronius')}
    # it still materializes — visible and editable — but does not poll
    assert devs['fronius-meter-240'].enabled is False
    assert devs['fronius-u1'].enabled is True


def test_an_endpoint_without_groups_is_one_implicit_group(tmp_path):
    """Every endpoint written before groups existed is one of these, and no
    device may be renamed by the change."""
    from tests.test_devices import write_config
    cfg = write_config(tmp_path, extra_yaml="""
endpoints:
  - id: legacy
    template: fronius_sunspec_inverter
    connection: { protocol: tcp, host: 192.0.2.9 }
    units: [1, 2]
""")
    assert cfg.endpoint_group_ids('legacy') == ['units']
    assert [d.id for d in cfg.endpoint_devices('legacy')] == ['legacy-u1', 'legacy-u2']
    assert all(d.group_id == 'units' for d in cfg.endpoint_devices('legacy'))
