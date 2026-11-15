# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Endpoint (installation) expansion into units and groups."""
import logging

from typing import Dict, List, Optional


logger = logging.getLogger("multibus.core.config")


class EndpointsMixin:
    @staticmethod
    def _endpoint_units(p: Dict, holder: Dict = None) -> List[Dict]:
        """Normalize a ``units`` list. Accepts bare unit ids (``[1, 2, 3]``) or
        dicts (``{unit_id, id?, name?}``); returns ``{unit_id, id, name}`` with
        the id defaulting to ``<endpoint>-u<unit>``. Invalid entries are skipped
        with a warning.

        ``holder`` is the group the units belong to when the endpoint declares
        groups; ids still default off the ENDPOINT id, so moving a unit into a
        named group never renames it and never orphans its history."""
        pid = str(p.get('id', '')).strip()
        out: List[Dict] = []
        for u in ((holder or p).get('units') or []):
            raw_uid = u.get('unit_id') if isinstance(u, dict) else u
            try:
                uid = int(raw_uid)
                if not (0 <= uid <= 255):
                    raise ValueError
            except (TypeError, ValueError):
                logger.warning(f"endpoints[{pid}]: invalid unit {raw_uid!r} skipped")
                continue
            ov = u if isinstance(u, dict) else {}
            out.append({'unit_id': uid,
                        'id': str(ov.get('id') or f"{pid}-u{uid}").strip(),
                        'name': str(ov.get('name') or '')})
        return out

    @staticmethod
    def _endpoint_groups(p: Dict) -> List[Dict]:
        """The unit groups of one endpoint, in declaration order.

        An installation is not one kind of thing. A PV plant holds inverters AND
        the meter at its grid connection: different templates, different
        rhythms, and adding their powers together would be meaningless. So units
        live in groups, each with its own role, template, connection and
        sources.

        An endpoint that declares no groups is ONE implicit group holding its
        flat ``units``/``sources``/``connection`` — which is every endpoint
        written before groups existed, so nothing has to be rewritten and no
        device is renamed.
        """
        raw = p.get('groups')
        if not raw:
            return [{'id': 'units', 'role': p.get('role', ''),
                     'enabled': True,
                     'units': p.get('units') or [],
                     'template': p.get('template', ''),
                     'connection': p.get('connection') or {},
                     'sources': p.get('sources') or []}]
        out, seen = [], set()
        for i, g in enumerate(raw):
            if not isinstance(g, dict):
                logger.warning("endpoints[%s].groups[%d]: not a mapping — skipped",
                               p.get('id'), i)
                continue
            gid = str(g.get('id', '') or f'group{i + 1}').strip()
            if gid in seen:
                logger.warning("endpoints[%s]: duplicate group %r skipped",
                               p.get('id'), gid)
                continue
            seen.add(gid)
            out.append({**g, 'id': gid})
        return out

    def _expand_endpoints(self, endpoints: Optional[List[Dict]] = None) -> List[tuple]:
        """Expand ``endpoints:`` into (endpoint_id, raw-device-dict) pairs, shaped
        exactly like devices[] entries so both flow through _device_from_raw.
        ``${unit_id}`` / ``${endpoint_id}`` / ``${device_id}`` substitute in the
        MQTT topic prefix, InfluxDB bucket, device tag and name. ``endpoints``
        expands those instead of the stored ones (a preview, nothing kept)."""
        out: List[tuple] = []
        for p in (self._raw_endpoints if endpoints is None else endpoints):
            pid = str(p.get('id', '')).strip()
            if not pid:
                logger.warning("endpoints[]: entry without id skipped")
                continue
            # a disabled endpoint still materializes (units stay visible/managed);
            # enabled=False propagates so no clients/pollers are started
            mqtt_cfg = dict(p.get('mqtt', {}) or {})
            influx_cfg = dict(p.get('influxdb', {}) or {})
            opcua_cfg = dict(p.get('opcua', {}) or {})
            seen_units: set = set()
            # An installation is not one kind of thing. A PV plant holds
            # inverters AND the meter at its grid connection, and they differ in
            # template, in rhythm and in what it means to add them up — so units
            # live in GROUPS, each with its own role, template and sources. An
            # endpoint that declares none is one implicit group, which is what
            # every endpoint written before today is.
            for grp in self._endpoint_groups(p):
                if not grp.get('enabled', True):
                    # a disabled group still materializes: its units stay
                    # visible and editable, they simply do not poll
                    pass
                conn = dict(grp.get('connection', {}) or {})
                srcs = [dict(x) for x in (grp.get('sources') or [])
                        if isinstance(x, dict)]
                if not conn and not srcs:
                    # a group that names no way of reaching its units uses the
                    # installation's: turning a flat installation into groups
                    # must not force anyone to restate (and mistype) its bus
                    conn = dict(p.get('connection', {}) or {})
                    srcs = [dict(x) for x in (p.get('sources') or [])
                            if isinstance(x, dict)]
                gid = str(grp.get('id', '') or 'units')
                grole = str(grp.get('role', '') or '')
                for u in self._endpoint_units(p, grp):
                  uid, did = u['unit_id'], u['id']
                  if uid in seen_units:
                    logger.warning(f"endpoints[{pid}]: duplicate unit {uid} skipped")
                    continue
                  seen_units.add(uid)

                  def sub(s: str) -> str:
                      return (str(s).replace('${unit_id}', str(uid))
                                    .replace('${endpoint_id}', pid)
                                    .replace('${device_id}', did))

                    # A group may override the routing its endpoint declares. An
                  # installation's inverters, its grid meter and its site
                  # totals are different kinds of thing and belong on different
                  # branches: forcing one pattern on all of them is what
                  # produces `pv/units/240` where `pv/meter/grid` was meant.
                  m = dict(mqtt_cfg, **(grp.get('mqtt') or {}))
                  if m.get('topic_prefix'):
                      m['topic_prefix'] = sub(m['topic_prefix'])
                  if m.get('ha_node'):
                      m['ha_node'] = sub(m['ha_node'])
                  i = dict(influx_cfg, **(grp.get('influxdb') or {}))
                  # OPC UA: the group's block wins (its template, its fields)
                  o = dict(opcua_cfg, **(grp.get('opcua') or {}))
                  if i.get('bucket'):
                      i['bucket'] = sub(i['bucket'])
                  if i.get('device_tag'):
                      i['device_tag'] = sub(i['device_tag'])
                  if isinstance(i.get('tags'), dict):
                      i['tags'] = {k: sub(v) for k, v in i['tags'].items()}
                  # ${unit_id} / ${endpoint_id} / ${device_id} substitute in the
                  # CONNECTION too, not only in the routing identity. An HTTP
                  # master addresses its units by URL rather than by a unit id in
                  # a frame — a Fronius Solar API endpoint is
                  # `…?Scope=Device&DeviceId=${unit_id}` — so without this an
                  # endpoint could not describe an HTTP master at all, and four
                  # inverters would need four hand-written devices.
                  def _addr(raw: dict) -> dict:
                      """One unit's view of a connection block. ${unit_id} and
                      friends substitute in the ADDRESS fields too, because an
                      HTTP master addresses its units by URL rather than by a unit
                      id inside a frame."""
                      out = dict(raw, unit_id=uid)
                      for _k in ('url', 'host', 'serial_port', 'topic'):
                          if isinstance(out.get(_k), str) and '${' in out[_k]:
                              out[_k] = sub(out[_k])
                      return out

                  c = _addr(conn)
                  unit_sources = [_addr(x) for x in srcs]
                  out.append((pid, {
                      'id': did,
                      'name': u['name'] or f"{p.get('name') or pid} unit {uid}",
                      # the GROUP's template and enable flag win: a plant's
                      # meter is not the same device as its inverters, and a
                      # group can be switched off without touching the plant
                      'template': grp.get('template', '') or p.get('template', ''),
                      'enabled': (bool(p.get('enabled', True))
                                  and bool(grp.get('enabled', True))),
                      'group_id': gid,
                      'role': grole,
                      # the group's command bindings apply to every unit of it
                      'commands': list(grp.get('commands') or p.get('commands') or []),
                      # and its channel overrides (ADR-0019), like the bindings
                      **({'channels': dict(grp.get('channels') or p.get('channels'))}
                         if (grp.get('channels') or p.get('channels')) else {}),
                      'write_locked': bool(p.get('write_locked', False)),
                      'connection': c,
                      # declared once on the endpoint, resolved per unit
                      **({'sources': unit_sources} if unit_sources else {}),
                      'mqtt': m,
                      'influxdb': i,
                      **({'opcua': o} if o else {}),
                      # an endpoint is ONE endpoint: its sinks are declared once and
                      # apply to every unit, like the write lock above
                      **({'http_output': dict(p['http_output'])}
                         if p.get('http_output') else {}),
                      **({'rest_push': dict(p['rest_push'])}
                         if p.get('rest_push') else {}),
                  }))
        return out

