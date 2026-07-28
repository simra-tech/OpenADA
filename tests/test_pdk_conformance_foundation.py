"""Independent native PDK oracles and model-free metamorphic checks.

The native fixtures in ``tests/fixtures/pdk-conformance`` are intentionally
not generated through :mod:`openada.pdk_bindings`.  Each PDK is parsed twice:
once by a hand-written native ngspice deck and once by one model-free portable
deck containing all metamorphic variants as electrically isolated circuits.
The session-scoped parametrized fixture caches both runs for every assertion in
this module; in particular, sky130's expensive library is not reparsed for
each property.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Mapping

import pytest

from openada.discovery import DiscoveryManager
from openada.engines.ngspice_outputs import RawSeriesExtraction, extract_analysis_raw
from openada.operations.simulate import PDK_BINDING_EXTENSION, simulate
from openada.pdk_bindings import REGISTRY, bind_deck, resolve_pdk_binding


pytestmark = pytest.mark.conformance

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "pdk-conformance"
VERIFY_PATH = ROOT / "conformance" / "circuit-simulate-v0alpha2" / "verify.py"
PDK_IDS = ("ihp-sg13g2", "sky130A", "gf180mcuD", "freepdk45")


def _load_independent_verifier():
    specification = importlib.util.spec_from_file_location(
        "openada_pdk_conformance_verify",
        VERIFY_PATH,
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


VERIFY = _load_independent_verifier()


@dataclass(frozen=True, slots=True)
class FixtureSpec:
    pdk_id: str
    directory: Path
    expected: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class LocatedPdk:
    pdk_root: Path
    tree: Path
    revision: str


@dataclass(slots=True)
class PdkConformanceRun:
    spec: FixtureSpec
    located: LocatedPdk
    ngspice_version_text: str
    native_process: subprocess.CompletedProcess[str]
    native_log: str
    native_parse_error: str | None
    native_variables: list[str]
    native_rows: list[list[float | complex]]
    pipeline_payload: dict[str, Any]
    pipeline_capture: Mapping[str, Any] | None
    pipeline_extraction: RawSeriesExtraction | None
    pipeline_parse_error: str | None
    pipeline_variables: list[str]
    pipeline_rows: list[list[float | complex]]
    bound_text: str


def _fixture_spec(pdk_id: str) -> FixtureSpec:
    directory = FIXTURE_ROOT / pdk_id
    expected = json.loads((directory / "expected.json").read_text(encoding="utf-8"))
    return FixtureSpec(pdk_id=pdk_id, directory=directory, expected=expected)


SPECS = tuple(_fixture_spec(pdk_id) for pdk_id in PDK_IDS)


def _candidate_roots(spec: FixtureSpec) -> tuple[Path, ...]:
    variable = f"OPENADA_TEST_{spec.pdk_id.upper().replace('-', '_')}_ROOT"
    candidates: list[Path] = []
    for value in (os.environ.get(variable), os.environ.get("PDK_ROOT")):
        if value:
            candidates.append(Path(value).expanduser())
    candidates.extend(
        (
            Path.home() / ".cache" / "openada" / "pdk-root",
            Path("/foss/pdks"),
            Path("/foss/model-kits/freepdk45/1.4"),
        )
    )
    cache = Path.home() / ".cache" / "openada" / "pdks" / spec.pdk_id
    if cache.is_dir():
        candidates.extend(sorted(cache.iterdir()))

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        absolute = candidate.absolute()
        key = str(absolute)
        if key in seen:
            continue
        seen.add(key)
        unique.append(absolute)
    return tuple(unique)


def _installed_revision(tree: Path) -> str:
    commit = tree / "COMMIT"
    if commit.is_file():
        value = commit.read_text(encoding="utf-8", errors="replace").strip()
        if re.fullmatch(r"[0-9a-f]{40}", value):
            return value
    return tree.resolve().parent.name


def _locate_pdk(spec: FixtureSpec) -> LocatedPdk | None:
    binding = REGISTRY[spec.pdk_id]
    probe, _ = binding.library_entries[0].resolve(binding.default_corner)
    for candidate in _candidate_roots(spec):
        contained = candidate / spec.pdk_id
        if (contained / probe).is_file():
            return LocatedPdk(
                pdk_root=candidate,
                tree=contained,
                revision=_installed_revision(contained),
            )
        if (candidate / probe).is_file():
            return LocatedPdk(
                pdk_root=candidate,
                tree=candidate,
                revision=_installed_revision(candidate),
            )
    return None


def _ngspice() -> Path | None:
    installed = Path("/usr/bin/ngspice")
    if installed.is_file():
        return installed
    discovered = shutil.which("ngspice")
    return None if discovered is None else Path(discovered)


def _single_row_values(
    variables: list[str],
    rows: list[list[float | complex]],
) -> dict[str, float | complex]:
    assert len(rows) == 1, f"expected one OP row, received {len(rows)}"
    assert len(variables) == len(rows[0])
    return dict(zip(variables, rows[0]))


def _pipeline_capture(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    native = (
        payload.get("data", {})
        .get("extensions", {})
        .get("org.openada", {})
        .get("native_data", {})
    )
    captures = native.get("output_captures", [])
    return next(
        (
            capture
            for capture in captures
            if isinstance(capture, Mapping) and capture.get("kind") == "raw"
        ),
        None,
    )


def _execute_conformance_run(
    spec: FixtureSpec,
    *,
    work: Path,
    ngspice: Path,
    located: LocatedPdk,
    version_text: str,
) -> PdkConformanceRun:
    native_directory = work / "native"
    native_directory.mkdir(parents=True)
    native_deck = native_directory / "native.spice"
    native_raw = native_directory / "native.raw"
    native_log_path = native_directory / "native.log"
    native_template = (spec.directory / "native.spice").read_text(encoding="utf-8")
    assert "@PDK@" in native_template
    native_text = native_template.replace("@PDK@", str(located.tree))
    assert "@PDK@" not in native_text
    native_deck.write_text(native_text, encoding="utf-8")

    native_process = subprocess.run(
        [
            str(ngspice),
            "-n",
            "-b",
            "-r",
            str(native_raw),
            "-o",
            str(native_log_path),
            str(native_deck),
        ],
        cwd=native_directory,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=600,
    )
    native_log = (
        native_log_path.read_text(encoding="utf-8", errors="replace")
        if native_log_path.is_file()
        else ""
    )
    native_variables: list[str] = []
    native_rows: list[list[float | complex]] = []
    native_parse_error: str | None = None
    if native_raw.is_file():
        try:
            native_variables, native_rows = VERIFY.parse_ngspice_binary(
                native_raw,
                analysis_type="op",
            )
        except Exception as exc:  # the independent parser owns its exception type
            native_parse_error = f"{type(exc).__name__}: {exc}"
    else:
        native_parse_error = "native ngspice did not create native.raw"

    pipeline_directory = work / "pipeline"
    payload = simulate(
        spec.directory / "portable.spice",
        pipeline_directory,
        discovery=DiscoveryManager(),
        pdk=spec.pdk_id,
        pdk_root=located.pdk_root,
        timeout=600.0,
    )
    capture = _pipeline_capture(payload)
    pipeline_extraction: RawSeriesExtraction | None = None
    pipeline_variables: list[str] = []
    pipeline_rows: list[list[float | complex]] = []
    pipeline_parse_error: str | None = None
    if capture is not None:
        raw_path = Path(str(capture["path"]))
        selected_variables = list(spec.expected["pipeline"]["vectors"].values())
        pipeline_extraction = extract_analysis_raw(
            raw_path,
            backend="ngspice",
            analysis=payload.get("data", {}).get("analysis", {}),
            selected_variables=selected_variables,
            expected_bytes=int(capture["bytes"]),
            expected_sha256=str(capture["sha256"]),
        )
        try:
            pipeline_variables, pipeline_rows = VERIFY.parse_ngspice_binary(
                raw_path,
                analysis_type="op",
            )
        except Exception as exc:  # independent verifier failure is test evidence
            pipeline_parse_error = f"{type(exc).__name__}: {exc}"
    else:
        pipeline_parse_error = "OpenADA did not retain a raw output"

    bound_path = pipeline_directory / "decks" / "portable.spice"
    bound_text = (
        bound_path.read_text(encoding="utf-8", errors="replace")
        if bound_path.is_file()
        else ""
    )
    return PdkConformanceRun(
        spec=spec,
        located=located,
        ngspice_version_text=version_text,
        native_process=native_process,
        native_log=native_log,
        native_parse_error=native_parse_error,
        native_variables=native_variables,
        native_rows=native_rows,
        pipeline_payload=payload,
        pipeline_capture=capture,
        pipeline_extraction=pipeline_extraction,
        pipeline_parse_error=pipeline_parse_error,
        pipeline_variables=pipeline_variables,
        pipeline_rows=pipeline_rows,
        bound_text=bound_text,
    )


@pytest.fixture(scope="session", params=SPECS, ids=lambda spec: spec.pdk_id)
def pdk_conformance_run(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
) -> PdkConformanceRun:
    spec: FixtureSpec = request.param
    located = _locate_pdk(spec)
    if located is None:
        pytest.skip(f"no installed {spec.pdk_id} collateral")
    ngspice = _ngspice()
    if ngspice is None:
        pytest.skip("ngspice is not installed")
    version = subprocess.run(
        [str(ngspice), "-v"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )
    expected_version = str(spec.expected["ngspice_version"])
    if f"ngspice-{expected_version}" not in version.stdout:
        pytest.skip(
            f"{spec.pdk_id} oracle is pinned to ngspice {expected_version}; "
            f"installed runtime reports {version.stdout.strip()!r}"
        )
    return _execute_conformance_run(
        spec,
        work=tmp_path_factory.mktemp(f"pdk-conformance-{spec.pdk_id}"),
        ngspice=ngspice,
        located=located,
        version_text=version.stdout,
    )


@pytest.mark.parametrize("spec", SPECS, ids=lambda spec: spec.pdk_id)
def test_pdk_conformance_fixture_is_independently_authored(spec: FixtureSpec):
    native = (spec.directory / "native.spice").read_text(encoding="utf-8")
    portable = (spec.directory / "portable.spice").read_text(encoding="utf-8")
    expected = spec.expected

    assert expected["schema"] == "openada-pdk-conformance-fixture/v1"
    assert expected["pdk_id"] == spec.pdk_id
    assert "@PDK@" in native
    assert "nmos.core" not in native
    assert expected["device"]["native_model"] in native
    assert "nmos.core" in portable
    assert ".subckt permuted_nmos g b d s" in portable
    assert "Xpermuted g_permuted 0 d_permuted 0 permuted_nmos" in portable
    assert expected["observable"]["name"] == "i(vd_ref)"
    assert expected["observable"]["unit"] == "A"
    assert expected["known_limitations"]
    multi_finger = expected.get("multi_finger")
    if spec.pdk_id == "sky130A":
        assert isinstance(multi_finger, Mapping)
    if isinstance(multi_finger, Mapping):
        multi_observable = multi_finger["observable"]
        assert multi_finger["nf"] > 1
        assert multi_observable["unit"] == "A"
        assert multi_observable["name"] == "i(vd_nf2)"
        assert "Vd_nf2 " in native
        assert "nf=2" in native
        assert "NF=2" in portable
    for source in expected["documentation"]:
        path = Path(source["path"])
        assert not path.is_absolute()
        assert ".." not in path.parts
        assert source["supports"]


def test_native_pdk_reference_matches_committed_oracle(
    pdk_conformance_run: PdkConformanceRun,
):
    run = pdk_conformance_run
    expected = run.spec.expected
    observable = expected["observable"]

    assert run.located.revision == expected["pdk_revision"]
    for source in expected["documentation"]:
        assert (run.located.tree / source["path"]).is_file(), source
    assert run.native_process.returncode == 0, (
        run.native_process.stdout
        + run.native_process.stderr
        + "\n"
        + run.native_log[-8_000:]
    )
    assert run.native_parse_error is None
    values = _single_row_values(run.native_variables, run.native_rows)
    assert observable["name"] in values
    assert math.isclose(
        float(values[observable["name"]]),
        float(observable["expected"]),
        rel_tol=float(observable["relative_tolerance"]),
        abs_tol=float(observable["absolute_tolerance"]),
    )
    multi_finger = expected.get("multi_finger")
    if isinstance(multi_finger, Mapping):
        multi_observable = multi_finger["observable"]
        assert multi_observable["name"] in values
        assert math.isclose(
            float(values[multi_observable["name"]]),
            float(multi_observable["expected"]),
            rel_tol=float(multi_observable["relative_tolerance"]),
            abs_tol=float(multi_observable["absolute_tolerance"]),
        )


def test_model_free_pipeline_reproduces_independent_native_reference(
    pdk_conformance_run: PdkConformanceRun,
):
    run = pdk_conformance_run
    expected = run.spec.expected
    payload = run.pipeline_payload
    observable = expected["observable"]

    assert payload.get("execution", {}).get("status") == "completed", payload.get(
        "diagnostics"
    )
    assert payload.get("engineering", {}).get("status") == "pass", payload.get(
        "diagnostics"
    )
    assert run.pipeline_capture is not None
    assert run.pipeline_extraction is not None
    assert run.pipeline_extraction.valid, run.pipeline_extraction
    assert run.pipeline_parse_error is None
    assert "nmos.core" not in run.bound_text

    facts = payload["data"]["extensions"][PDK_BINDING_EXTENSION]
    assert facts["pdk_id"] == expected["pdk_id"]
    assert facts["corner"] == expected["corner"]
    assert facts["rewritten_device_count"] == expected["pipeline"][
        "rewritten_device_count"
    ]
    assert facts["roles_bound"] == [expected["device"]["role"]]
    assert facts["device_models"][expected["device"]["role"]] == expected["device"][
        "native_model"
    ]
    assert float(facts["simulation_temperature_c"]) == expected["temperature_c"]

    independent_values = _single_row_values(
        run.pipeline_variables,
        run.pipeline_rows,
    )
    extracted_values = {
        signal.name: signal.real_values[0]
        for signal in run.pipeline_extraction.signals
    }
    for name, value in extracted_values.items():
        assert math.isclose(
            value,
            float(independent_values[name]),
            rel_tol=1e-15,
            abs_tol=1e-18,
        )

    native_values = _single_row_values(run.native_variables, run.native_rows)
    pipeline_value = extracted_values[observable["name"]]
    assert math.isclose(
        pipeline_value,
        float(observable["expected"]),
        rel_tol=float(observable["relative_tolerance"]),
        abs_tol=float(observable["absolute_tolerance"]),
    )
    assert math.isclose(
        pipeline_value,
        float(native_values[observable["name"]]),
        rel_tol=1e-10,
        abs_tol=1e-14,
    )
    multi_finger = expected.get("multi_finger")
    if isinstance(multi_finger, Mapping):
        multi_observable = multi_finger["observable"]
        native_multi_finger = float(native_values[multi_observable["name"]])
        pipeline_multi_finger = extracted_values[multi_observable["name"]]
        assert math.isclose(
            pipeline_multi_finger,
            float(multi_observable["expected"]),
            rel_tol=float(multi_observable["relative_tolerance"]),
            abs_tol=float(multi_observable["absolute_tolerance"]),
        )
        assert math.isclose(
            pipeline_multi_finger,
            native_multi_finger,
            rel_tol=1e-10,
            abs_tol=1e-14,
        )


def test_equivalent_units_and_permuted_wrapper_preserve_the_operating_point(
    pdk_conformance_run: PdkConformanceRun,
):
    run = pdk_conformance_run
    assert run.pipeline_extraction is not None and run.pipeline_extraction.valid
    expected = run.spec.expected
    names = expected["pipeline"]["vectors"]
    tolerances = expected["metamorphic"]
    values = {
        signal.name: signal.real_values[0]
        for signal in run.pipeline_extraction.signals
    }
    reference = values[names["reference"]]
    for variant in ("unit_equivalent", "permuted_wrapper"):
        assert math.isclose(
            values[names[variant]],
            reference,
            rel_tol=float(tolerances["equivalence_relative_tolerance"]),
            abs_tol=float(tolerances["equivalence_absolute_tolerance"]),
        ), (run.spec.pdk_id, variant, reference, values[names[variant]])


def test_polarity_width_and_supply_metamorphic_properties(
    pdk_conformance_run: PdkConformanceRun,
):
    run = pdk_conformance_run
    assert run.pipeline_extraction is not None and run.pipeline_extraction.valid
    expected = run.spec.expected
    names = expected["pipeline"]["vectors"]
    tolerances = expected["metamorphic"]
    values = {
        signal.name: signal.real_values[0]
        for signal in run.pipeline_extraction.signals
    }
    reference = values[names["reference"]]

    assert math.isclose(
        values[names["reversed_sense"]],
        -reference,
        rel_tol=float(tolerances["equivalence_relative_tolerance"]),
        abs_tol=float(tolerances["equivalence_absolute_tolerance"]),
    )
    width_ratio = abs(values[names["double_width"]] / reference)
    assert float(tolerances["double_width_ratio_min"]) <= width_ratio <= float(
        tolerances["double_width_ratio_max"]
    ), (run.spec.pdk_id, width_ratio)
    native_width = expected.get("independent_native_width_crosscheck")
    if isinstance(native_width, Mapping):
        assert math.isclose(
            width_ratio,
            float(native_width["ratio"]),
            rel_tol=1e-10,
            abs_tol=1e-14,
        )
    assert abs(values[names["higher_supply"]]) > abs(values[names["lower_supply"]]), (
        run.spec.pdk_id,
        values[names["lower_supply"]],
        values[names["higher_supply"]],
    )


def test_freepdk45_manual_length_ambiguity_is_not_hidden():
    expected = next(spec.expected for spec in SPECS if spec.pdk_id == "freepdk45")
    crosscheck = expected["documentation_crosscheck"]
    manual = float(crosscheck["manual_ion_a_per_um"])
    at_45nm = abs(float(crosscheck["native_l45m_a"]))
    at_50nm = abs(float(crosscheck["native_l50m_a"]))

    assert abs(at_45nm / manual - 1.0) > 0.15
    assert abs(at_50nm / manual - 1.0) < 0.005
    assert expected["device"]["l_m"] == 5e-8
    assert expected["observable"]["expected"] == crosscheck["native_l50m_a"]


def test_gf180_minimum_width_scaling_crosscheck_is_not_hidden():
    expected = next(spec.expected for spec in SPECS if spec.pdk_id == "gf180mcuD")
    native = expected["independent_native_width_crosscheck"]
    observed_ratio = abs(float(native["i2_a"]) / float(native["i1_a"]))

    assert native["w2_m"] == 2.0 * native["w1_m"]
    assert math.isclose(observed_ratio, native["ratio"], rel_tol=1e-15)
    assert native["ratio"] < 1.7
    assert expected["metamorphic"]["double_width_ratio_min"] == 1.5


def test_sky130_binding_states_the_documented_wnflag_option():
    spec = next(spec for spec in SPECS if spec.pdk_id == "sky130A")
    located = _locate_pdk(spec)
    if located is None:
        pytest.skip("no installed sky130A collateral")
    resolved = resolve_pdk_binding(
        spec.pdk_id,
        located.pdk_root,
        corner=str(spec.expected["corner"]),
    )
    portable = (spec.directory / "portable.spice").read_text(encoding="utf-8")
    bound, _ = bind_deck(portable, resolved)

    assert re.search(
        r"(?im)^\s*\.options?\s+.*\bwnflag\s*=\s*1(?:\s|$)",
        bound,
    )
