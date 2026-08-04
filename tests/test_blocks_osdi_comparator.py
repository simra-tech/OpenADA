"""OSDI backend battery for comparator_clocked (P1 graduation gate).

Compiles the library's continuous Verilog-A backend through the reviewed OSDI
path and holds it to the contract's golden cases PLUS the characterization
matrix the review gate demands: calibrated latency over the admitted (td,
tedge) space, edge-time convention, falling-decision symmetry, both hysteresis
directions, deterministic non-uic startup, multi-instance independence,
timestep refinement, shifted rails/reference, and the documented limitations
(minimum clock pulse width, clock-high-at-start transparency).

Every deck runs WITHOUT uic, matching the golden cases' `"uic": false`.
"""

from __future__ import annotations

import re
import subprocess
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from openada import osdi_compile as oc  # noqa: E402

OPENVAF = shutil.which("openvaf-r") or shutil.which("openvaf")
NGSPICE = shutil.which("ngspice")
CC_VA = ROOT / "blocks/bhv-core/blocks/comparator_clocked/comparator_clocked.va"
CC_PORTS = ["inp", "inn", "clk", "out", "vss"]
CC_DEFAULTS = {
    "vhi": 1.0,
    "vlo": 0.0,
    "td": 2e-9,
    "tedge": 1e-9,
    "vhyst": 0.0,
    "vth_clk": 0.5,
    "clk_band": 0.01,
    "dif_band": 1e-6,
    "rout": 100.0,
}
#: Declared latency regularization for this backend: |t50 - td| <= this bound
#: over the admitted space (td > tedge/2 + 7 ps). Measured worst case in the
#: calibration sweep was 0.44% of td; 1% + 10 ps holds the declared claim with
#: headroom for solver/timestep variation.
LATENCY_BOUND = lambda td: 0.01 * td + 1e-11  # noqa: E731

native = pytest.mark.skipif(
    OPENVAF is None or NGSPICE is None,
    reason="OSDI path needs an OpenVAF compiler and an OSDI-capable ngspice",
)

_MEAS_RE = re.compile(r"^(\w+)\s*=\s*([-+0-9.eE]+)", re.MULTILINE)


@pytest.fixture(scope="module")
def prelude():
    module = oc.compile_verilog_a(
        CC_VA.read_text(), "bhv_comparator_clocked_v1", Path(tempfile.mkdtemp())
    )
    return oc.osdi_preload_prelude([(module, CC_PORTS, CC_DEFAULTS)])


def _run(prelude: str, body: str, control: str) -> dict[str, float]:
    deck = "* osdi comparator battery\n" + prelude + body + ".control\n" + control + ".endc\n.end\n"
    path = Path(tempfile.mktemp(suffix=".cir"))
    path.write_text(deck)
    r = subprocess.run(
        [NGSPICE, "-b", str(path)], capture_output=True, text=True, timeout=120
    )
    out = r.stdout + r.stderr
    assert "error" not in out.lower().replace("no error", ""), out[-1500:]
    return {m.group(1): float(m.group(2)) for m in _MEAS_RE.finditer(out)}


# --- the three golden cases, verbatim fixtures, via the OSDI wrapper ---


@native
def test_golden_decide_basic(prelude):
    m = _run(
        prelude,
        "X1 inp inn clk out 0 bhv_comparator_clocked_v1 td=2n tedge=1n\n"
        "Vinp inp 0 PWL(0 0 1u 1)\nVinn inn 0 DC 0.55\n"
        "Vclk clk 0 PULSE(0 1 0 1n 1n 48n 100n)\nRload out 0 10k\n",
        "tran 0.5n 1.2u\nmeas tran tdecide WHEN v(out)=0.5 RISE=1\n",
    )
    # the case relation (602.5 ns, atol 2 ns) ...
    assert abs(m["tdecide"] - 6.025e-7) < 2e-9
    # ... and the tight calibrated bound this backend actually delivers
    assert abs(m["tdecide"] - 6.025e-7) < 2e-10


@native
def test_golden_hysteresis_band(prelude):
    m = _run(
        prelude,
        "X1 inp inn clk out 0 bhv_comparator_clocked_v1 td=2n tedge=1n vhyst=0.2\n"
        "Vinp inp 0 PWL(0 0.08 0.9u 0.08 1.1u 0.12 2.5u 0.12)\nVinn inn 0 DC 0\n"
        "Vclk clk 0 PULSE(0 1 0.5u 1n 1n 48n 1u)\nRload out 0 10k\n",
        "tran 1n 2.5u\n"
        "meas tran a1 FIND v(out) AT=0.9u\nmeas tran a2 FIND v(out) AT=1.9u\n",
    )
    assert abs(m["a1"] - 0.0) < 0.02
    assert abs(m["a2"] - 1.0) < 0.02


@native
def test_golden_ambiguity_low(prelude):
    m = _run(
        prelude,
        "X1 inp inn clk out 0 bhv_comparator_clocked_v1 td=2n tedge=1n\n"
        "Vinp inp 0 DC 0.3\nVinn inn 0 DC 0.3\n"
        "Vclk clk 0 PULSE(0 1 0.5u 1n 1n 48n 1u)\nRload out 0 10k\n",
        "tran 1n 3.5u\nmeas tran outmax MAX v(out) FROM=0.4u TO=3.5u\n",
    )
    assert abs(m["outmax"]) < 0.02


# --- calibrated latency + edge over the admitted parameter space ---


@native
@pytest.mark.parametrize(
    "td, tedge",
    [
        (5e-10, 1e-10),
        (5e-10, 9e-10),  # near the td > tedge/2 boundary
        (2e-9, 1e-10),
        (2e-9, 1e-9),
        (2e-9, 3.9e-9),  # near the boundary at default td
        (1e-8, 1e-9),
        (5e-8, 4e-9),
    ],
)
def test_latency_calibration_bound(prelude, td, tedge):
    tstep = max(td / 40, 1e-11)
    m = _run(
        prelude,
        f"X1 inp inn clk out 0 bhv_comparator_clocked_v1 td={td} tedge={tedge}\n"
        "Vinp inp 0 DC 0.7\nVinn inn 0 DC 0.55\n"
        "Vclk clk 0 PULSE(0 1 600n 1n 1n 300n 600n)\nRload out 0 10k\n",
        f"tran {tstep} {600e-9 + 2e-9 + 30 * td}\n"
        "meas tran t50 WHEN v(out)=0.5 RISE=1\n",
    )
    err = m["t50"] - 600.5e-9 - td
    assert abs(err) < LATENCY_BOUND(td), f"latency err {err:.3e} at td={td}"


@native
@pytest.mark.parametrize("tedge", [1e-10, 1e-9, 4e-9])
def test_edge_time_matches_native_convention(prelude, tedge):
    # the native backend's linear ramp spans tedge, so its 10-90 is 0.8*tedge;
    # this backend realizes the same 10-90 within 15%
    td = max(5 * tedge, 2e-9)
    m = _run(
        prelude,
        f"X1 inp inn clk out 0 bhv_comparator_clocked_v1 td={td} tedge={tedge}\n"
        "Vinp inp 0 DC 0.7\nVinn inn 0 DC 0.55\n"
        "Vclk clk 0 PULSE(0 1 600n 1n 1n 300n 600n)\nRload out 0 10k\n",
        f"tran {tedge / 20} {600e-9 + 3 * td + 3 * tedge}\n"
        "meas tran t10 WHEN v(out)=0.1 RISE=1\n"
        "meas tran t90 WHEN v(out)=0.9 RISE=1\n",
    )
    measured = m["t90"] - m["t10"]
    assert abs(measured - 0.8 * tedge) < 0.15 * 0.8 * tedge


@native
def test_falling_decision_is_symmetric(prelude):
    # decision high on the first edge (t=100n), low on the second (t=700n):
    # the falling out 50% crossing lands td after the second clock threshold
    m = _run(
        prelude,
        "X1 inp inn clk out 0 bhv_comparator_clocked_v1 td=2n tedge=1n\n"
        "Vinp inp 0 PWL(0 0.7 0.4u 0.7 0.5u 0.4 1.5u 0.4)\nVinn inn 0 DC 0.55\n"
        "Vclk clk 0 PULSE(0 1 100n 1n 1n 200n 600n)\nRload out 0 10k\n",
        "tran 0.05n 1.5u\n"
        "meas tran trise WHEN v(out)=0.5 RISE=1\n"
        "meas tran tfall WHEN v(out)=0.5 FALL=1\n",
    )
    assert abs(m["trise"] - (100.5e-9 + 2e-9)) < LATENCY_BOUND(2e-9)
    assert abs(m["tfall"] - (700.5e-9 + 2e-9)) < LATENCY_BOUND(2e-9)


@native
def test_hysteresis_releases_high_retained_decision(prelude):
    # mirror of the golden case: with the decision retained HIGH the threshold
    # shifts DOWN by vhyst/2; -0.08 V holds high, -0.12 V flips low
    m = _run(
        prelude,
        "X1 inp inn clk out 0 bhv_comparator_clocked_v1 td=2n tedge=1n vhyst=0.2\n"
        "Vinp inp 0 PWL(0 0.3 1.4u 0.3 1.6u -0.08 2.4u -0.08 2.6u -0.12 3.5u -0.12)\n"
        "Vinn inn 0 DC 0\n"
        "Vclk clk 0 PULSE(0 1 0.5u 1n 1n 48n 1u)\nRload out 0 10k\n",
        "tran 1n 3.5u\n"
        "meas tran a1 FIND v(out) AT=0.9u\n"   # +0.3 -> high
        "meas tran a2 FIND v(out) AT=1.9u\n"   # -0.08 > -0.1 shifted -> stays high
        "meas tran a3 FIND v(out) AT=2.9u\n",  # -0.12 < -0.1 shifted -> flips low
    )
    assert abs(m["a1"] - 1.0) < 0.02
    assert abs(m["a2"] - 1.0) < 0.02
    assert abs(m["a3"] - 0.0) < 0.02


# --- determinism, refinement, instances, rails ---


@native
def test_op_and_dc_hold_the_deterministic_default_low(prelude):
    # contract: decision (and out) stay at the deterministic default LOW before
    # the first rising clock event; OP and an unclocked DC sweep never decide
    m = _run(
        prelude,
        "X1 inp inn clk out 0 bhv_comparator_clocked_v1 td=2n tedge=1n\n"
        "Vinp inp 0 DC 0.9\nVinn inn 0 DC 0.1\nVclk clk 0 DC 0\nRload out 0 10k\n",
        "op\nlet vout_op = v(out)\nprint vout_op\n",
    )
    assert abs(m["vout_op"]) < 1e-3


@native
def test_timestep_refinement_invariance(prelude):
    times = {}
    for tstep in ("1n", "0.1n", "0.02n"):
        m = _run(
            prelude,
            "X1 inp inn clk out 0 bhv_comparator_clocked_v1 td=2n tedge=1n\n"
            "Vinp inp 0 DC 0.7\nVinn inn 0 DC 0.55\n"
            "Vclk clk 0 PULSE(0 1 600n 1n 1n 300n 600n)\nRload out 0 10k\n",
            f"tran {tstep} 0.7u\nmeas tran t50 WHEN v(out)=0.5 RISE=1\n",
        )
        times[tstep] = m["t50"]
    spread = max(times.values()) - min(times.values())
    assert spread < 1e-10, times  # < 100 ps over a 50x tstep range


@native
def test_multi_instance_independent(prelude):
    m = _run(
        prelude,
        "X1 inpa 0 clk outa 0 bhv_comparator_clocked_v1 td=2n tedge=1n\n"
        "X2 inpb 0 clk outb 0 bhv_comparator_clocked_v1 td=6n tedge=1n vhi=1.8\n"
        "Vinpa inpa 0 DC 0.2\nVinpb inpb 0 DC 0.2\n"
        "Vclk clk 0 PULSE(0 1 100n 1n 1n 300n 600n)\nRla outa 0 10k\nRlb outb 0 10k\n",
        "tran 0.05n 0.5u\n"
        "meas tran ta WHEN v(outa)=0.5 RISE=1\n"
        "meas tran tb WHEN v(outb)=0.9 RISE=1\n"
        "meas tran bmax MAX v(outb) FROM=0.3u TO=0.5u\n",
    )
    assert abs(m["ta"] - (100.5e-9 + 2e-9)) < LATENCY_BOUND(2e-9)
    assert abs(m["tb"] - (100.5e-9 + 6e-9)) < LATENCY_BOUND(6e-9)  # 0.9 = its 50% level
    assert abs(m["bmax"] - 1.8) < 0.02


@native
def test_shifted_rails_and_reference(prelude):
    # vss tied to 1 V, rails 1.2/2.0: decision/latency measured against the
    # shifted 50% level (vhi+vlo)/2 = 1.6
    m = _run(
        prelude,
        "X1 inp inn clk out ref bhv_comparator_clocked_v1 td=2n tedge=1n vhi=1.0 vlo=0.2\n"
        "Vref ref 0 DC 1\n"
        "Vinp inp 0 DC 1.7\nVinn inn 0 DC 1.55\n"
        "Vclk clk 0 PULSE(1 2 600n 1n 1n 300n 600n) \nRload out ref 10k\n",
        # vhi/vlo are AGAINST vss (=1 V): absolute rails 1.2/2.0, 50% at 1.6
        "tran 0.05n 0.7u\nmeas tran t50 WHEN v(out)=1.6 RISE=1\n"
        "meas tran omax MAX v(out) FROM=0.62u TO=0.7u\n",
    )
    assert abs(m["t50"] - (600.5e-9 + 2e-9)) < LATENCY_BOUND(2e-9)
    assert abs(m["omax"] - 2.0) < 0.02


# --- documented limitations, asserted as defined behavior ---


@native
def test_min_clock_pulse_width_limitation(prelude):
    # Documented limitation: the slave acquires with tau_trk = td/8, so a
    # clk-high phase below ~tau_trk*ln2 (~0.09*td) leaves the held state under
    # 0.5 and no decision ever fires (deterministic low); phases between that
    # boundary and ~td decide LATE. A 2*td pulse decides at full level; a
    # 0.05*td pulse must not decide. (Boundary measured: no decision at 0.15n,
    # decision at 0.3n for td=2n.)
    ok = _run(
        prelude,
        "X1 inp inn clk out 0 bhv_comparator_clocked_v1 td=2n tedge=1n\n"
        "Vinp inp 0 DC 0.7\nVinn inn 0 DC 0.55\n"
        "Vclk clk 0 PULSE(0 1 100n 0.2n 0.2n 4n 600n)\nRload out 0 10k\n",
        "tran 0.02n 0.4u\nmeas tran omax MAX v(out) FROM=0.1u TO=0.4u\n",
    )
    assert ok["omax"] > 0.98
    narrow = _run(
        prelude,
        "X1 inp inn clk out 0 bhv_comparator_clocked_v1 td=2n tedge=1n\n"
        "Vinp inp 0 DC 0.7\nVinn inn 0 DC 0.55\n"
        "Vclk clk 0 PULSE(0 1 100n 0.02n 0.02n 0.1n 600n)\nRload out 0 10k\n",
        "tran 0.01n 0.4u\nmeas tran omax MAX v(out) FROM=0.1u TO=0.4u\n",
    )
    assert narrow["omax"] < 0.1


@native
def test_retention_holds_indefinitely(prelude):
    # the weak bistable regeneration on the slave (the continuous latch)
    # restores the held decision against leakage/gmin: a decision taken at
    # 1 us must still be at full level 400 us later with the clock low, EVEN
    # after the input reverses at 5 us (the pre-fix decay collapsed the output
    # at ~250 us)
    m = _run(
        prelude,
        "X1 inp inn clk out 0 bhv_comparator_clocked_v1 td=2n tedge=1n\n"
        "Vinp inp 0 PWL(0 0.7 4.9u 0.7 5u 0.3 500u 0.3)\nVinn inn 0 DC 0.55\n"
        "Vclk clk 0 PULSE(0 1 1u 1n 1n 3u 1000u)\nRload out 0 10k\n",
        "tran 50n 500u\n"
        "meas tran o100u FIND v(out) AT=100u\n"
        "meas tran o400u FIND v(out) AT=400u\n",
    )
    assert abs(m["o100u"] - 1.0) < 0.02
    assert abs(m["o400u"] - 1.0) < 0.02


@native
def test_slow_clock_edge_keeps_the_latency_reference(prelude):
    # a 100 ns clock edge (vs the 1 ns golden edges): the latency stays
    # referenced to the vth_clk crossing because the sampling aperture is
    # symmetric around it (threshold crossing at 550 ns here)
    m = _run(
        prelude,
        "X1 inp inn clk out 0 bhv_comparator_clocked_v1 td=2n tedge=1n\n"
        "Vinp inp 0 DC 0.7\nVinn inn 0 DC 0.55\n"
        "Vclk clk 0 PULSE(0 1 500n 100n 100n 300n 1000n)\nRload out 0 10k\n",
        "tran 0.1n 0.8u\nmeas tran t50 WHEN v(out)=0.5 RISE=1\n",
    )
    assert abs(m["t50"] - (550e-9 + 2e-9)) < 2e-10


@native
def test_clock_high_at_start_holds_the_default_low(prelude):
    # Contract-matching: when the clock is already high at t=0 there has been
    # no rising edge, and the master froze at its deterministic OP default
    # (low) — so out holds vlo, exactly like the event backends.
    m = _run(
        prelude,
        "X1 inp inn clk out 0 bhv_comparator_clocked_v1 td=2n tedge=1n\n"
        "Vinp inp 0 DC 0.7\nVinn inn 0 DC 0.55\n"
        "Vclk clk 0 DC 1\nRload out 0 10k\n",
        "tran 0.05n 50n\nmeas tran omax MAX v(out) FROM=10n TO=50n\n",
    )
    assert m["omax"] < 0.02
