# Tests for the conservative canonical-name classifier that powers the
# "auto-canonicalize" button. The safe failure is None (leave it to the human);
# the dangerous failure is a valid-but-WRONG name. Cases below encode both the
# real vendor/Janitza maps (must map) and the adversarial false-positives found
# in review (must be None or the CORRECT name — never wrong).
from multibus.canonical_fields import guess_canonical, is_canonical


# (name, label, unit) -> expected canonical name or None
POSITIVE = [
    # vendor-map originals
    ("V_L1", "Voltage L1-N", "V", "voltage_l1_n"),
    ("V_L1_L2", "Voltage L1-L2", "V", "voltage_l1_l2"),
    ("V_L3_L2", "Voltage L3-L2", "V", "voltage_l2_l3"),
    ("V_avg", "Voltage L-N average", "V", "voltage_ln_avg"),
    ("I_L2", "Current L2", "A", "current_l2"),
    ("I_N", "Current Neutral", "A", "current_n"),
    ("I_avg", "Current average", "A", "current_avg"),
    ("P_L1", "Active power L1", "W", "power_active_l1"),
    ("P_total", "Active power total", "W", "power_active_total"),
    ("Q_total", "Reactive power total", "var", "power_reactive_total"),
    ("S_total", "Apparent power total", "VA", "power_apparent_total"),
    ("PF_L3", "Power factor L3", "", "power_factor_l3"),
    ("Freq", "Frequency", "Hz", "frequency"),
    ("Import_kWh", "Active energy import (total)", "kWh", "energy_active_import"),
    ("Export_kWh", "Active energy export (total)", "kWh", "energy_active_export"),
    ("Net_kWh", "Active energy net (total)", "kWh", "energy_active_net"),
    ("Total_kWh", "Total active energy", "kWh", "energy_active_total"),
    ("Import_kvarh", "Reactive energy import", "kvarh", "energy_reactive_import"),
    ("Total_kvarh", "Total reactive energy", "kvarh", "energy_reactive_total"),
    ("Import_L2_kWh", "Active energy import L2", "kWh", "energy_active_import_l2"),
    ("Energy_L1_Import", "Import active energy L1", "kWh", "energy_active_import_l1"),
    # Janitza live names with explicit L#-N / import-export wording
    ("_G_ULN[0]", "Voltage L1-N", "V", "voltage_l1_n"),
    ("_G_ULL[0]", "Voltage L1-L2", "V", "voltage_l1_l2"),
    ("_PLN[0]", "Active power L1", "W", "power_active_l1"),
    ("_G_P_SUM3", "Total active power", "W", "power_active_total"),
    ("_PF_TOTAL", "Power Factor Total", "", "power_factor_total"),
    ("_G_COS_PHI[0]", "Factor Putere L1", "", "power_factor_l1"),
    ("_WH_V[4]", "Energie Activa (Import)", "Wh", "energy_active_import"),
    # diagnostics
    ("Serial_ASCII", "Serial number", "", "serial"),
    ("Model_ID", "Meter model", "", "model_id"),
    # already canonical → passthrough
    ("voltage_l1_n", "x", "V", "voltage_l1_n"),
    # THD with phase
    ("THD_U_L1", "Voltage THD L1", "%", "thd_voltage_l1"),
    ("THD_I_L2", "THD current L2", "%", "thd_current_l2"),
]

# Adversarial: must NEVER return a wrong-but-valid name. Expected value is the
# CORRECT canonical or None; the test fails only if the guess is a THIRD value.
ADVERSARIAL = [
    # F1 — 'delivered'/'forward' polarity is vendor-specific → don't guess
    ("E1", "Real Energy Delivered", "kWh", None),
    ("Efwd", "Energy Forward", "kWh", None),
    # F2 — kvar must be reactive, never apparent
    ("Q_L1", "", "kvar", "power_reactive_l1"),
    ("Q_tot", "Total reactive power", "kvar", "power_reactive_total"),
    # F3 — 'total (import + export)' is the total counter, not import
    ("E", "Total active energy (import + export)", "kWh", "energy_active_total"),
    # F4 — 'consumption' must not become 'total' via a 'sum' substring
    ("E", "Active energy consumption", "kWh", None),
    ("P", "Power consumption", "W", None),
    # F5 — per-phase reactive energy isn't representable → None, never a collision
    ("Er1", "Reactive energy import L1", "varh", None),
    # F6 — line-line described in words
    ("V", "Voltage between L2 and L3", "V", "voltage_l2_l3"),
    # F7 — a stray digit-dash in the description must not spoof line-line
    ("V_L1", "Voltage L1", "V", "voltage_l1_n"),   # desc added below
    # F9 — apparent energy without unit is energy, not power
    ("EA", "Apparent energy total", "", "energy_apparent"),
    # F10 — reordered 'energy reactive import'
    ("X", "Energy reactive import", "", "energy_reactive_import"),
    # F11 — current THD, not voltage THD
    ("THD", "THD current L1", "%", "thd_current_l1"),
    # F12 — comms-config 'serial' must not become the serial diagnostic
    ("cfg", "RS485 serial address", "", None),
    # F14 — composite unit must not read as active power
    ("Irr", "Irradiance", "W/m2", None),
    # bare quantity with no position → None (ambiguous)
    ("V", "Voltage", "V", None),
]


def test_positive_cases_map_correctly():
    bad = []
    for name, label, unit, exp in POSITIVE:
        got = guess_canonical(name=name, label=label, unit=unit)
        if got != exp:
            bad.append(f"{name!r}/{label!r} -> {got} (want {exp})")
    assert not bad, "\n".join(bad)


def test_adversarial_cases_are_never_wrong():
    bad = []
    for name, label, unit, exp in ADVERSARIAL:
        desc = "3-4 wire system" if label == "Voltage L1" else ""
        got = guess_canonical(name=name, label=label, unit=unit, description=desc)
        if got != exp:
            bad.append(f"{name!r}/{label!r} -> {got} (want {exp})")
    assert not bad, "\n".join(bad)


def test_every_guess_is_canonical_or_none():
    for name, label, unit, _ in POSITIVE + ADVERSARIAL:
        got = guess_canonical(name=name, label=label, unit=unit)
        assert got is None or is_canonical(got), f"{name}: {got} not canonical"
