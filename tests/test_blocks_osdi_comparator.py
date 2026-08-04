"""OSDI battery for comparator_clocked_phys (the continuous-time block).

Compiles the block's Verilog-A through the reviewed OSDI path — with EVERY
OpenVAF generation present on the host (classic openvaf and openvaf-r) — and
holds it to its OWN contract: the golden cases, the declared latency
regularization (3% of td + 10 ps, overdrive >= 2*dif_band, clock phases >=
td, tstep <= td/10), rails reached in every latency case, edge convention
rising AND falling, both hysteresis directions, deterministic non-uic startup
(OP/DC low, clock parked high low), indefinite clk-low retention through an
input reversal, multi-instance, shifted rails/reference, timestep refinement,
and the documented limitations (minimum pulse width, overdrive-dependent
decision time near the dif_band boundary).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from openada import osdi_compile as oc  # noqa: E402

COMPILERS = [c for c in ("openvaf", "openvaf-r") if shutil.which(c)]
NGSPICE = shutil.which("ngspice")
CC_VA = ROOT / "blocks/bhv-core/blocks/comparator_clocked_phys/comparator_clocked_phys.va"
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


def LATENCY_BOUND(td: float) -> float:
    """The block's declared latency regularization (block.json)."""
    return 0.03 * td + 1e-11


native = pytest.mark.skipif(
    not COMPILERS or NGSPICE is None,
    reason="OSDI path needs an OpenVAF compiler and an OSDI-capable ngspice",
)

_MEAS_RE = re.compile(r"^(\w+)\s*=\s*([-+0-9.eE]+)", re.MULTILINE)


@pytest.fixture(scope="module", params=COMPILERS or ["missing"])
def prelude(request):
    """One battery run per OpenVAF generation available on the host."""
    module = oc.compile_verilog_a(
        CC_VA.read_text(),
        "bhv_comparator_clocked_phys_v1",
        Path(tempfile.mkdtemp()),
        openvaf_bin=request.param,
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
    assert r.returncode == 0, out[-1500:]
    assert "error" not in out.lower().replace("no error", ""), out[-1500:]
    return {m.group(1): float(m.group(2)) for m in _MEAS_RE.finditer(out)}


DUT = "bhv_comparator_clocked_phys_v1"


# --- the three golden cases, verbatim fixtures, via the OSDI wrapper ---


@native
def test_golden_decide_basic(prelude):
    m = _run(
        prelude,
        f"X1 inp inn clk out 0 {DUT} td=2n tedge=1n\n"
        "Vinp inp 0 PWL(0 0 1u 1)\nVinn inn 0 DC 0.55\n"
        "Vclk clk 0 PULSE(0 1 0 1n 1n 48n 100n)\nRload out 0 10k\n",
        "tran 0.5n 1.2u\nmeas tran tdecide WHEN v(out)=0.5 RISE=1\n"
        "meas tran omax MAX v(out) FROM=0.65u TO=1.2u\n",
    )
    # the case relation (602.5 ns, atol 2 ns), then the declared bound plus
    # the coarse-tstep measurement allowance (tstep/2 at 0.5 ns steps)
    assert abs(m["tdecide"] - 6.025e-7) < 2e-9
    assert abs(m["tdecide"] - 6.025e-7) < LATENCY_BOUND(2e-9) + 0.25e-9
    assert abs(m["omax"] - 1.0) < 0.02


@native
def test_golden_hysteresis_band(prelude):
    m = _run(
        prelude,
        f"X1 inp inn clk out 0 {DUT} td=2n tedge=1n vhyst=0.2\n"
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
        f"X1 inp inn clk out 0 {DUT} td=2n tedge=1n\n"
        "Vinp inp 0 DC 0.3\nVinn inn 0 DC 0.3\n"
        "Vclk clk 0 PULSE(0 1 0.5u 1n 1n 48n 1u)\nRload out 0 10k\n",
        "tran 1n 3.5u\nmeas tran outmax MAX v(out) FROM=0.4u TO=3.5u\n",
    )
    assert abs(m["outmax"]) < 0.02


# --- declared latency + rails + edges over the admitted parameter space ---


@native
@pytest.mark.parametrize(
    "td, tedge",
    [
        (5e-10, 1e-10),
        (5e-10, 9e-10),   # near the td > tedge/2 boundary
        (2e-9, 1e-10),
        (2e-9, 1e-9),
        (2e-9, 3.9e-9),   # near the boundary at default td
        (1e-8, 1e-9),
        (5e-8, 4e-9),
        (5e-11, 2e-11),   # near the contract's td minimum
    ],
)
def test_latency_and_rails_over_admitted_space(prelude, td, tedge):
    tstep = max(td / 40, 1e-12)
    m = _run(
        prelude,
        f"X1 inp inn clk out 0 {DUT} td={td} tedge={tedge}\n"
        "Vinp inp 0 DC 0.7\nVinn inn 0 DC 0.55\n"
        "Vclk clk 0 PULSE(0 1 600n 1n 1n 300n 600n)\nRload out 0 10k\n",
        f"tran {tstep} {600e-9 + 2e-9 + 30 * td + 5 * tedge}\n"
        "meas tran t50 WHEN v(out)=0.5 RISE=1\n"
        f"meas tran omax MAX v(out) FROM={600e-9 + td + 3 * tedge} TO={600e-9 + 2e-9 + 30 * td + 5 * tedge}\n",
    )
    err = m["t50"] - 600.5e-9 - td
    assert abs(err) < LATENCY_BOUND(td), f"latency err {err:.3e} at td={td} tedge={tedge}"
    assert abs(m["omax"] - 1.0) < 0.02, f"rail not reached at td={td} tedge={tedge}"


@native
@pytest.mark.parametrize("tedge", [1e-10, 1e-9, 4e-9])
def test_edge_time_matches_native_convention_both_directions(prelude, tedge):
    # the native backend's linear ramp spans tedge (10-90 = 0.8*tedge); the
    # slew-limited stage realizes the same within 15%, rising AND falling
    td = max(5 * tedge, 2e-9)
    m = _run(
        prelude,
        f"X1 inp inn clk out 0 {DUT} td={td} tedge={tedge}\n"
        "Vinp inp 0 PWL(0 0.7 1.1u 0.7 1.15u 0.4 3u 0.4)\nVinn inn 0 DC 0.55\n"
        "Vclk clk 0 PULSE(0 1 600n 1n 1n 300n 1200n)\nRload out 0 10k\n",
        f"tran {tedge / 20} {1.8e-6 + 3 * td + 3 * tedge}\n"
        "meas tran t10r WHEN v(out)=0.1 RISE=1\n"
        "meas tran t90r WHEN v(out)=0.9 RISE=1\n"
        "meas tran t90f WHEN v(out)=0.9 FALL=1\n"
        "meas tran t10f WHEN v(out)=0.1 FALL=1\n",
    )
    rise = m["t90r"] - m["t10r"]
    fall = m["t10f"] - m["t90f"]
    assert abs(rise - 0.8 * tedge) < 0.15 * 0.8 * tedge
    assert abs(fall - 0.8 * tedge) < 0.15 * 0.8 * tedge


@native
def test_falling_decision_is_symmetric(prelude):
    m = _run(
        prelude,
        f"X1 inp inn clk out 0 {DUT} td=2n tedge=1n\n"
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
    m = _run(
        prelude,
        f"X1 inp inn clk out 0 {DUT} td=2n tedge=1n vhyst=0.2\n"
        "Vinp inp 0 PWL(0 0.3 1.4u 0.3 1.6u -0.08 2.4u -0.08 2.6u -0.12 3.5u -0.12)\n"
        "Vinn inn 0 DC 0\n"
        "Vclk clk 0 PULSE(0 1 0.5u 1n 1n 48n 1u)\nRload out 0 10k\n",
        "tran 1n 3.5u\n"
        "meas tran a1 FIND v(out) AT=0.9u\n"
        "meas tran a2 FIND v(out) AT=1.9u\n"
        "meas tran a3 FIND v(out) AT=2.9u\n",
    )
    assert abs(m["a1"] - 1.0) < 0.02
    assert abs(m["a2"] - 1.0) < 0.02
    assert abs(m["a3"] - 0.0) < 0.02


# --- declared decision domain (overdrive dependence is contractual) ---


@native
def test_overdrive_domain_bounds_the_latency_claim(prelude):
    # at overdrive = 2*dif_band above the +dif_band/2 boundary the declared
    # latency holds; deep inside the soft region (0.01*dif_band above the
    # boundary, where the ramp drive is ~0.08) the decision is SLOW — the
    # documented overdrive-dependent domain: not decided by 600.5n + 3*td
    m = _run(
        prelude,
        f"X1 inp inn clk out 0 {DUT} td=2n tedge=1n\n"
        "Vinp inp 0 DC 2.5e-6\nVinn inn 0 DC 0\n"   # boundary 0.5e-6 + 2*dif_band
        "Vclk clk 0 PULSE(0 1 600n 1n 1n 300n 600n)\nRload out 0 10k\n",
        "tran 0.05n 0.7u\nmeas tran t50 WHEN v(out)=0.5 RISE=1\n",
    )
    assert abs(m["t50"] - (600.5e-9 + 2e-9)) < LATENCY_BOUND(2e-9)
    slow = _run(
        prelude,
        f"X1 inp inn clk out 0 {DUT} td=2n tedge=1n\n"
        "Vinp inp 0 DC 0.51e-6\nVinn inn 0 DC 0\n"  # 0.01*dif_band above boundary
        "Vclk clk 0 PULSE(0 1 600n 1n 1n 300n 600n)\nRload out 0 10k\n",
        "tran 0.05n 0.6065u\nmeas tran omax MAX v(out) FROM=600n TO=606.5n\n",
    )
    assert slow["omax"] < 0.5  # not decided within 3*td: outside the domain


# --- determinism, refinement, instances, rails, retention ---


@native
def test_op_and_dc_hold_the_deterministic_default_low(prelude):
    m = _run(
        prelude,
        f"X1 inp inn clk out 0 {DUT} td=2n tedge=1n\n"
        "Vinp inp 0 DC 0.9\nVinn inn 0 DC 0.1\nVclk clk 0 DC 0\nRload out 0 10k\n",
        "op\nlet vout_op = v(out)\nprint vout_op\n",
    )
    assert abs(m["vout_op"]) < 1e-3


@native
def test_clock_high_at_start_holds_the_default_low(prelude):
    m = _run(
        prelude,
        f"X1 inp inn clk out 0 {DUT} td=2n tedge=1n\n"
        "Vinp inp 0 DC 0.7\nVinn inn 0 DC 0.55\n"
        "Vclk clk 0 DC 1\nRload out 0 10k\n",
        "tran 0.05n 50n\nmeas tran omax MAX v(out) FROM=10n TO=50n\n",
    )
    assert m["omax"] < 0.02


@native
def test_retention_holds_indefinitely_clk_low(prelude):
    # slave regeneration: the decision taken at 1 us holds at full level to
    # 400 us with the clock low, THROUGH an input reversal at 5 us
    m = _run(
        prelude,
        f"X1 inp inn clk out 0 {DUT} td=2n tedge=1n\n"
        "Vinp inp 0 PWL(0 0.7 4.9u 0.7 5u 0.3 500u 0.3)\nVinn inn 0 DC 0.55\n"
        "Vclk clk 0 PULSE(0 1 1u 1n 1n 3u 1000u)\nRload out 0 10k\n",
        "tran 50n 500u\n"
        "meas tran o100u FIND v(out) AT=100u\n"
        "meas tran o400u FIND v(out) AT=400u\n",
    )
    assert abs(m["o100u"] - 1.0) < 0.02
    assert abs(m["o400u"] - 1.0) < 0.02


@native
def test_timestep_refinement_invariance(prelude):
    times = {}
    for tstep in ("1n", "0.1n", "0.02n"):
        m = _run(
            prelude,
            f"X1 inp inn clk out 0 {DUT} td=2n tedge=1n\n"
            "Vinp inp 0 DC 0.7\nVinn inn 0 DC 0.55\n"
            "Vclk clk 0 PULSE(0 1 600n 1n 1n 300n 600n)\nRload out 0 10k\n",
            f"tran {tstep} 0.7u\nmeas tran t50 WHEN v(out)=0.5 RISE=1\n",
        )
        times[tstep] = m["t50"]
    spread = max(times.values()) - min(times.values())
    assert spread < 5e-10, times  # < 0.5 ns over a 50x tstep range


@native
def test_multi_instance_independent(prelude):
    m = _run(
        prelude,
        f"X1 inpa 0 clk outa 0 {DUT} td=2n tedge=1n\n"
        f"X2 inpb 0 clk outb 0 {DUT} td=6n tedge=1n vhi=1.8\n"
        "Vinpa inpa 0 DC 0.2\nVinpb inpb 0 DC 0.2\n"
        "Vclk clk 0 PULSE(0 1 100n 1n 1n 300n 600n)\nRla outa 0 10k\nRlb outb 0 10k\n",
        "tran 0.05n 0.5u\n"
        "meas tran ta WHEN v(outa)=0.5 RISE=1\n"
        "meas tran tb WHEN v(outb)=0.9 RISE=1\n"
        "meas tran bmax MAX v(outb) FROM=0.3u TO=0.5u\n",
    )
    assert abs(m["ta"] - (100.5e-9 + 2e-9)) < LATENCY_BOUND(2e-9)
    assert abs(m["tb"] - (100.5e-9 + 6e-9)) < LATENCY_BOUND(6e-9)
    assert abs(m["bmax"] - 1.8) < 0.02


@native
def test_shifted_rails_and_reference(prelude):
    # vss tied to 1 V; vhi/vlo are AGAINST vss: absolute rails 1.2/2.0
    m = _run(
        prelude,
        f"X1 inp inn clk out ref {DUT} td=2n tedge=1n vhi=1.0 vlo=0.2\n"
        "Vref ref 0 DC 1\n"
        "Vinp inp 0 DC 1.7\nVinn inn 0 DC 1.55\n"
        "Vclk clk 0 PULSE(1 2 600n 1n 1n 300n 600n) \nRload out ref 10k\n",
        "tran 0.05n 0.7u\nmeas tran t50 WHEN v(out)=1.6 RISE=1\n"
        "meas tran omax MAX v(out) FROM=0.62u TO=0.7u\n",
    )
    assert abs(m["t50"] - (600.5e-9 + 2e-9)) < LATENCY_BOUND(2e-9)
    assert abs(m["omax"] - 2.0) < 0.02


@native
def test_slow_clock_edge_keeps_the_latency_reference(prelude):
    # 100 ns clock edges: latency stays referenced to the vth_clk crossing
    # (550 ns here) because the aperture is symmetric around it
    m = _run(
        prelude,
        f"X1 inp inn clk out 0 {DUT} td=2n tedge=1n\n"
        "Vinp inp 0 DC 0.7\nVinn inn 0 DC 0.55\n"
        "Vclk clk 0 PULSE(0 1 500n 100n 100n 300n 1000n)\nRload out 0 10k\n",
        "tran 0.1n 0.8u\nmeas tran t50 WHEN v(out)=0.5 RISE=1\n",
    )
    assert abs(m["t50"] - (550e-9 + 2e-9)) < LATENCY_BOUND(2e-9)


@native
def test_min_clock_pulse_width_limitation(prelude):
    # documented: phases under ~0.09*td never decide (deterministic low);
    # a 2*td pulse decides at full level
    ok = _run(
        prelude,
        f"X1 inp inn clk out 0 {DUT} td=2n tedge=1n\n"
        "Vinp inp 0 DC 0.7\nVinn inn 0 DC 0.55\n"
        "Vclk clk 0 PULSE(0 1 100n 0.2n 0.2n 4n 600n)\nRload out 0 10k\n",
        "tran 0.02n 0.4u\nmeas tran omax MAX v(out) FROM=0.1u TO=0.4u\n",
    )
    assert ok["omax"] > 0.98
    narrow = _run(
        prelude,
        f"X1 inp inn clk out 0 {DUT} td=2n tedge=1n\n"
        "Vinp inp 0 DC 0.7\nVinn inn 0 DC 0.55\n"
        "Vclk clk 0 PULSE(0 1 100n 0.02n 0.02n 0.1n 600n)\nRload out 0 10k\n",
        "tran 0.01n 0.4u\nmeas tran omax MAX v(out) FROM=0.1u TO=0.4u\n",
    )
    assert narrow["omax"] < 0.1


@native
def test_relational_constraint_is_enforced_on_the_osdi_path(tmp_path):
    # td <= tedge/2 + 7ps must be refused BEFORE any simulator launch, from
    # the composition's own verify_deck (the same fail-closed rule as cosim)
    from openada.block_library import load_block_library
    from openada.osdi_compile import OsdiCompileError, compose_blocks_osdi

    library = load_block_library("bhv-core")
    composition = compose_blocks_osdi(
        library, ["comparator_clocked_phys"], tmp_path
    )
    good = f"* ok\nX1 a b c d 0 {DUT} td=2n tedge=1n\n.end\n"
    composition.verify_deck(good)  # must not raise
    bad = f"* bad\nX1 a b c d 0 {DUT} td=0.4n tedge=1n\n.end\n"
    with pytest.raises(OsdiCompileError) as caught:
        composition.verify_deck(bad)
    assert caught.value.code == "osdi.parameters.constraint_violated"


@native
def test_constraint_checked_per_instance_both_orders(tmp_path):
    # the overlay bug: an invalid instance must be refused regardless of
    # whether a valid instance FOLLOWS or precedes it
    from openada.block_library import load_block_library
    from openada.osdi_compile import OsdiCompileError, compose_blocks_osdi

    library = load_block_library("bhv-core")
    composition = compose_blocks_osdi(library, ["comparator_clocked_phys"], tmp_path)
    invalid_then_valid = (
        f"* order A\nX1 a b c d 0 {DUT} td=0.4n tedge=1n\n"
        f"X2 e f g h 0 {DUT} td=2n tedge=1n\n.end\n"
    )
    valid_then_invalid = (
        f"* order B\nX1 a b c d 0 {DUT} td=2n tedge=1n\n"
        f"X2 e f g h 0 {DUT} td=0.4n tedge=1n\n.end\n"
    )
    for deck in (invalid_then_valid, valid_then_invalid):
        with pytest.raises(OsdiCompileError) as caught:
            composition.verify_deck(deck)
        assert caught.value.code == "osdi.parameters.constraint_violated"
    # vhi > vlo is also machine-enforced
    with pytest.raises(OsdiCompileError) as caught:
        composition.verify_deck(f"* swap\nX1 a b c d 0 {DUT} vhi=0 vlo=1\n.end\n")
    assert caught.value.code == "osdi.parameters.constraint_violated"


@native
def test_data_setup_window_is_contractual(prelude):
    # input switching to qualifying overdrive only 0.05*td before the clock
    # crossing is inside the declared setup window: the decision must NOT
    # appear at the declared latency (it is late or missed) -- while the same
    # overdrive stable from >= td before the crossing decides on time
    late = _run(
        prelude,
        f"X1 inp inn clk out 0 {DUT} td=2n tedge=1n\n"
        "Vinp inp 0 PWL(0 0.4 600.3n 0.4 600.4n 0.7 2u 0.7)\nVinn inn 0 DC 0.55\n"
        "Vclk clk 0 PULSE(0 1 600n 1n 1n 300n 600n)\nRload out 0 10k\n",
        "tran 0.02n 0.607u\nmeas tran omax MAX v(out) FROM=600n TO=602.56n\n",
    )
    assert late["omax"] < 0.5  # not decided at the declared 602.5n
    ontime = _run(
        prelude,
        f"X1 inp inn clk out 0 {DUT} td=2n tedge=1n\n"
        "Vinp inp 0 PWL(0 0.4 597n 0.4 598n 0.7 2u 0.7)\nVinn inn 0 DC 0.55\n"
        "Vclk clk 0 PULSE(0 1 600n 1n 1n 300n 600n)\nRload out 0 10k\n",
        "tran 0.02n 0.61u\nmeas tran t50 WHEN v(out)=0.5 RISE=1\n",
    )
    assert abs(ontime["t50"] - (600.5e-9 + 2e-9)) < LATENCY_BOUND(2e-9)


@native
def test_dc_sweep_and_perturbed_start_hold_low(prelude):
    # actual DC (not just OP): sweeping the input with the clock low never
    # decides; and a small physical perturbation of the start (clock at
    # 0.2 V, inside neither rail nor band) still resolves LOW
    m = _run(
        prelude,
        f"X1 inp inn clk out 0 {DUT} td=2n tedge=1n\n"
        "Vinp inp 0 DC 0\nVinn inn 0 DC 0.3\nVclk clk 0 DC 0\nRload out 0 10k\n",
        "dc Vinp 0 1 0.05\nmeas dc omax MAX v(out) FROM=0 TO=1\n",
    )
    assert m["omax"] < 1e-3
    p = _run(
        prelude,
        f"X1 inp inn clk out 0 {DUT} td=2n tedge=1n\n"
        "Vinp inp 0 DC 0.7\nVinn inn 0 DC 0.55\nVclk clk 0 DC 0.2\nRload out 0 10k\n",
        "tran 0.05n 50n\nmeas tran omax MAX v(out) FROM=10n TO=50n\n",
    )
    assert p["omax"] < 0.02


@native
def test_short_low_phase_limits_master_tracking(prelude):
    # the contract's phases >= td requirement also covers the LOW (master
    # acquisition) phase: with the low phase only 0.1*td, the master cannot
    # track a change that arrives during it, so the next edge samples stale
    # data (decision stays low); a full-length low phase tracks it (decides)
    stale = _run(
        prelude,
        f"X1 inp inn clk out 0 {DUT} td=2n tedge=1n\n"
        "Vinp inp 0 PWL(0 0.4 649.5n 0.4 649.7n 0.7 2u 0.7)\nVinn inn 0 DC 0.55\n"
        # low phase 0.2n: high 100..649.9n, low 649.9..650.1n, high again
        "Vclk clk 0 PULSE(0 1 100n 0.1n 0.1n 549.8n 550n)\nRload out 0 10k\n",
        "tran 0.02n 0.658u\nmeas tran omax MAX v(out) FROM=650n TO=655n\n",
    )
    assert stale["omax"] < 0.5
    tracked = _run(
        prelude,
        f"X1 inp inn clk out 0 {DUT} td=2n tedge=1n\n"
        "Vinp inp 0 PWL(0 0.4 620n 0.4 622n 0.7 2u 0.7)\nVinn inn 0 DC 0.55\n"
        # low phase 100n (>= td), rising edge at 700n
        "Vclk clk 0 PULSE(0 1 100n 1n 1n 499n 600n)\nRload out 0 10k\n",
        "tran 0.05n 0.71u\nmeas tran t50 WHEN v(out)=0.5 RISE=1\n",
    )
    assert abs(tracked["t50"] - (700.5e-9 + 2e-9)) < LATENCY_BOUND(2e-9)


@native
def test_direct_osdi_binding_is_refused(tmp_path):
    # binding the compiled module with a caller .model card (or a direct N
    # reference) would bypass every per-instance constraint check
    from openada.block_library import load_block_library
    from openada.osdi_compile import OsdiCompileError, compose_blocks_osdi

    library = load_block_library("bhv-core")
    composition = compose_blocks_osdi(library, ["comparator_clocked_phys"], tmp_path)
    model_bypass = (
        f"* direct model\n.model evil {DUT} td=0.4n tedge=1n\n"
        "N1 a b c d 0 evil\n.end\n"
    )
    alias_bypass = f"* direct alias\nN1 a b c d 0 {DUT}__osdi\n.end\n"
    for deck in (model_bypass, alias_bypass):
        with pytest.raises(OsdiCompileError) as caught:
            composition.verify_deck(deck)
        assert caught.value.code == "osdi.instantiation.direct"


@native
def test_operation_boundary_enforces_composition(tmp_path):
    # simulate() itself must (a) refuse a preload without its composition and
    # (b) refuse an invalid instantiation BEFORE any simulator launch
    from openada.block_library import load_block_library
    from openada.discovery import DiscoveryManager
    from openada.operations.simulate import simulate
    from openada.osdi_compile import compose_blocks_osdi

    library = load_block_library("bhv-core")
    composition = compose_blocks_osdi(library, ["comparator_clocked_phys"], tmp_path)
    deck = tmp_path / "bad.cir"
    deck.write_text(
        f"* invalid instance\nX1 inp inn clk out 0 {DUT} td=0.4n tedge=1n\n"
        "Vinp inp 0 DC 0.7\nVinn inn 0 DC 0.55\nVclk clk 0 DC 0\nRl out 0 10k\n"
        ".op\n.end\n"
    )
    without = simulate(
        deck,
        tmp_path / "ev1",
        discovery=DiscoveryManager(),
        backend="ngspice",
        osdi_preload_text=composition.prelude_text,
    )
    assert without["execution"]["status"] == "invalid_request"
    assert any(
        d.get("code") == "osdi.composition.missing" for d in without["diagnostics"]
    )
    with_comp = simulate(
        deck,
        tmp_path / "ev2",
        discovery=DiscoveryManager(),
        backend="ngspice",
        osdi_preload_text=composition.prelude_text,
        osdi_composition=composition,
    )
    assert with_comp["execution"]["status"] == "invalid_request"
    assert any(
        d.get("code") == "osdi.parameters.constraint_violated"
        for d in with_comp["diagnostics"]
    )
    # no launch happened for either refusal
    assert without["execution"]["command"] == []
    assert with_comp["execution"]["command"] == []


@native
def test_direct_binding_refused_despite_inline_comments(tmp_path):
    # ngspice starts inline comments at ';' (no whitespace needed) and at a
    # whitespace-preceded '$'; the screen must see through both
    from openada.block_library import load_block_library
    from openada.osdi_compile import OsdiCompileError, compose_blocks_osdi

    library = load_block_library("bhv-core")
    composition = compose_blocks_osdi(library, ["comparator_clocked_phys"], tmp_path)
    decks = (
        f"* semi\n.model evil {DUT};comment\nN1 a b c d 0 evil\n.end\n",
        f"* semi-alias\nN1 a b c d 0 {DUT}__osdi;comment\n.end\n",
        f"* dollar\n.model evil {DUT} $ comment\nN1 a b c d 0 evil\n.end\n",
        f"* direct type\nN1 a b c d 0 {DUT}\n.end\n",
    )
    for deck in decks:
        with pytest.raises(OsdiCompileError) as caught:
            composition.verify_deck(deck)
        assert caught.value.code == "osdi.instantiation.direct", deck


@native
def test_preload_composition_mismatch_is_refused(tmp_path):
    from openada.block_library import load_block_library
    from openada.discovery import DiscoveryManager
    from openada.operations.simulate import simulate
    from openada.osdi_compile import compose_blocks_osdi

    library = load_block_library("bhv-core")
    composition = compose_blocks_osdi(library, ["comparator_clocked_phys"], tmp_path)
    deck = tmp_path / "ok.cir"
    deck.write_text(
        f"* valid deck\nX1 inp inn clk out 0 {DUT} td=2n tedge=1n\n"
        "Vinp inp 0 DC 0.7\nVinn inn 0 DC 0.55\nVclk clk 0 DC 0\nRl out 0 10k\n"
        ".op\n.end\n"
    )
    result = simulate(
        deck,
        tmp_path / "ev",
        discovery=DiscoveryManager(),
        backend="ngspice",
        osdi_preload_text=composition.prelude_text + "* tampered\n",
        osdi_composition=composition,
    )
    assert result["execution"]["status"] == "invalid_request"
    assert any(
        d.get("code") == "osdi.composition.mismatch" for d in result["diagnostics"]
    )
    assert result["execution"]["command"] == []


@native
def test_direct_binding_refused_through_continuations_and_slash_comments(tmp_path):
    # ngspice strips comments from each PHYSICAL line before stitching '+'
    # continuations, so a protected name split behind a comment+continuation
    # (or a '//' comment) still binds -- and must still be refused
    from openada.block_library import load_block_library
    from openada.osdi_compile import OsdiCompileError, compose_blocks_osdi

    library = load_block_library("bhv-core")
    composition = compose_blocks_osdi(library, ["comparator_clocked_phys"], tmp_path)
    decks = (
        "* continuation\n.model evil ; ignored\n"
        f"+ {DUT}\nN1 a b c d 0 evil\n.end\n",
        f"* slashes\n.model evil {DUT}//ignored\nN1 a b c d 0 evil\n.end\n",
        f"* slashes-alias\nN1 a b c d 0 {DUT}__osdi//x\n.end\n",
    )
    for deck in decks:
        with pytest.raises(OsdiCompileError) as caught:
            composition.verify_deck(deck)
        assert caught.value.code == "osdi.instantiation.direct", deck
