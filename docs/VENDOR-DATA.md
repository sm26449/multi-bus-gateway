# Vendor register data — provenance & licensing position

Multi-Bus Gateway ships Modbus **register definitions** (addresses, names,
data types, units, scale factors) for the devices it can poll. This page
records where that data comes from and on what basis it is redistributed.

## What is bundled

| Files | Device | Source |
|---|---|---|
| `docs/modbus_data.json`, `docs/*.csv` (basic, energy, min/max/mean values, FFT harmonics, 200 ms measurands, config) | Janitza UMG 512-PRO | Janitza's **"Modbus Address List"** document — publicly available from janitza.de, no NDA or registration required |
| `templates/*.yaml` device maps (Carlo Gavazzi EM24, Eastron SDM120/630, ABB B21/B23, Fronius Smart Meter, Schneider iEM3000, …) | various meters | each manufacturer's publicly available Modbus documentation, cross-checked against real hardware and established open-source integrations |

## Basis for redistribution

- The bundled content is **factual interface data**: which register lives at
  which address, its type, unit and scaling. It is exactly the information a
  client must have to interoperate with the device at all — the gateway
  cannot poll a meter without its register map.
- The vendor **documents themselves are not reproduced**: no document
  text, layout or artwork is included — only the transcribed register
  facts (address, type, unit, scaling, the register's name and its short
  functional label, e.g. "Day (1..31)"), restructured into this project's
  own JSON/CSV/YAML schema. The Janitza map carries such a label on most
  of its 4 126 rows; they identify the register, they are not the
  manual's prose.
- Shipping such register maps for interoperability is long-standing,
  universal practice in open-source energy tooling (Victron's
  `dbus-modbus-client`, Home Assistant integrations, ESPHome components,
  Solar-Log-style projects all bundle equivalent maps).
- The register data is **not claimed under this project's AGPL copyright** —
  the AGPL license covers this project's code and original documentation.
  The factual register definitions are provided as-is, with source
  attribution above.

## Trademarks

Janitza, Carlo Gavazzi, Eastron, ABB, Fronius, Schneider Electric, Victron
and any other manufacturer names are trademarks of their respective owners.
They are used here **only to identify device compatibility**. This project
is **not affiliated with, sponsored, or endorsed by** any of these
manufacturers.

## If you are a rights holder

If you believe any bundled data goes beyond factual interoperability
information, contact the maintainer (see [SECURITY.md](../SECURITY.md) for
the contact address) and it will be reviewed promptly.
