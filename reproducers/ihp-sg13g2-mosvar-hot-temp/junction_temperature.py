#!/usr/bin/env python3
"""Evaluate ngspice-46's default diode junction-potential temperature law."""

import math


# ngspice-46 constants from src/include/ngspice/const.h.
CHARGE = 1.6021766208e-19
BOLTZMANN = 1.38064852e-23
K_OVER_Q = BOLTZMANN / CHARGE
REFERENCE_K = 300.15
NOMINAL_K = 300.15

# IHP dsubw model value from sg13g2_svaricaphv_mod.lib.
JUNCTION_POTENTIAL = 0.1


def temperature_adjusted_junction_potential(temp_c: float) -> float:
    """Mirror the TLEVC=0 DIOtJctPot calculation in ngspice-46."""
    temp_k = temp_c + 273.15
    thermal_voltage = K_OVER_Q * temp_k
    temperature_ratio = temp_k / REFERENCE_K
    bandgap = 1.16 - 7.02e-4 * temp_k**2 / (temp_k + 1108.0)
    argument = (
        -bandgap / (2.0 * BOLTZMANN * temp_k)
        + 1.1150877 / (BOLTZMANN * (REFERENCE_K + REFERENCE_K))
    )
    potential_factor = -2.0 * thermal_voltage * (
        1.5 * math.log(temperature_ratio) + CHARGE * argument
    )

    nominal_thermal_voltage = K_OVER_Q * NOMINAL_K
    nominal_bandgap = (
        1.16 - 7.02e-4 * NOMINAL_K**2 / (NOMINAL_K + 1108.0)
    )
    nominal_argument = (
        -nominal_bandgap / (2.0 * BOLTZMANN * NOMINAL_K)
        + 1.1150877 / (2.0 * BOLTZMANN * REFERENCE_K)
    )
    nominal_ratio = NOMINAL_K / REFERENCE_K
    nominal_potential_factor = -2.0 * nominal_thermal_voltage * (
        1.5 * math.log(nominal_ratio) + CHARGE * nominal_argument
    )
    nominal_potential = (
        (JUNCTION_POTENTIAL - nominal_potential_factor) / nominal_ratio
    )
    return potential_factor + temperature_ratio * nominal_potential


def zero_crossing_c() -> float:
    """Bisect the first hot-temperature zero of the adjusted potential."""
    lower_c = 52.0
    upper_c = 53.0
    for _ in range(100):
        middle_c = (lower_c + upper_c) / 2.0
        if temperature_adjusted_junction_potential(middle_c) > 0.0:
            lower_c = middle_c
        else:
            upper_c = middle_c
    return (lower_c + upper_c) / 2.0


def main() -> None:
    control_v = 0.3
    print(f"DIOtJctPot zero: {zero_crossing_c():.14f} degC")
    print("temp_C       DIOtJctPot_V          log_argument          log(argument)")
    for temp_c in (52.4697, 52.4697998, 52.4698028, 52.4699, 60.0):
        potential_v = temperature_adjusted_junction_potential(temp_c)
        diode_voltage_v = -control_v
        log_argument = 1.0 - diode_voltage_v / potential_v
        logarithm = math.log(log_argument) if log_argument > 0.0 else math.nan
        print(
            f"{temp_c:10.7f}  {potential_v:+.12e}  "
            f"{log_argument:+.12e}  {logarithm}"
        )


if __name__ == "__main__":
    main()
