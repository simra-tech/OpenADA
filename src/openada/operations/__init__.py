"""Semantic operation dispatch above native OpenADA drivers."""

from .circuit_simulate import (
    MAX_SHARED_ANALYSIS_POINTS,
    MAX_SOURCE_BYTES,
    circuit_simulation_parameter_issue,
    circuit_simulation_parameters_match,
    decorate_circuit_simulation_result,
    invalid_circuit_simulation_request,
    inspect_simulation_deck,
    inspect_transient_deck,
    parse_simulation_analysis_line,
    simulate_circuit_profile,
)
from .drc_review import review_drc
from .drc_compare import compare_drc
from .result_measure import MEASUREMENT_KINDS, measure_result, normalized_series_sha256
from .result_series_extract import extract_result_series
from .result_spectral_measure import SPECTRAL_METRIC_KINDS, measure_spectrum
from .result_transfer_measure import TRANSFER_METRIC_KINDS, measure_transfer
from .specification_evaluate import SPECIFICATION_LIMIT_KINDS, evaluate_specification
from .simulate import (
    DISPATCH_EXTENSION,
    PDK_BINDING_EXTENSION,
    TARGET_EXTENSION,
    SimulationRequestError,
    SimulationTarget,
    classify_target,
    simulate,
    simulate_legacy_native,
)
from .testbench_simulate import (
    TESTBENCH_EVIDENCE_ASSERTION,
    TESTBENCH_SIMULATE_PROFILE,
    simulate_testbench,
    resolve_testbench_driver,
)

__all__ = [
    "MAX_SHARED_ANALYSIS_POINTS",
    "MAX_SOURCE_BYTES",
    "circuit_simulation_parameter_issue",
    "circuit_simulation_parameters_match",
    "decorate_circuit_simulation_result",
    "invalid_circuit_simulation_request",
    "inspect_simulation_deck",
    "inspect_transient_deck",
    "parse_simulation_analysis_line",
    "MEASUREMENT_KINDS",
    "SPECTRAL_METRIC_KINDS",
    "TRANSFER_METRIC_KINDS",
    "extract_result_series",
    "measure_result",
    "measure_spectrum",
    "measure_transfer",
    "normalized_series_sha256",
    "SPECIFICATION_LIMIT_KINDS",
    "DISPATCH_EXTENSION",
    "PDK_BINDING_EXTENSION",
    "TARGET_EXTENSION",
    "SimulationRequestError",
    "SimulationTarget",
    "TESTBENCH_EVIDENCE_ASSERTION",
    "TESTBENCH_SIMULATE_PROFILE",
    "classify_target",
    "simulate",
    "simulate_circuit_profile",
    "simulate_legacy_native",
    "simulate_testbench",
    "resolve_testbench_driver",
    "evaluate_specification",
    "review_drc",
    "compare_drc",
]
