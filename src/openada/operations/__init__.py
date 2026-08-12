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
from .experiment import run_experiment, validate_experiment
from .experiment_template import compile_experiment_template
from .result_measure import MEASUREMENT_KINDS, measure_result, normalized_series_sha256
from .result_osc_measure import (
    OSCILLATION_STATUSES,
    OSCILLATOR_MEASUREMENT_KINDS,
    measure_oscillator,
    oscillator_receipt_sha256,
)
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
# The retired ``testbench.simulate`` verb survives only as a CLI alias, so the
# only thing this package re-exports for it is the alias entry point the CLI
# dispatches to. Its profile and assertion identifiers belong to
# ``driver_registry`` and its driver lookup to the alias module itself; a second
# spelling of either here is how a retired name keeps finding new callers.
from .testbench_simulate import simulate_testbench
from .testbench_oracle import compare_testbench_observables
from .testbench_plan import (
    PreparedTestbenchPlan,
    TestbenchPlanIssue,
    load_testbench_plan_schema,
    validate_testbench_plan,
)
from .testbench_plan_ngspice import (
    NgspiceCompilationBundle,
    PreparedNgspiceCompilation,
    ResolvedBindingValue,
    TestbenchPlanCompileError,
    compile_testbench_plan_ngspice,
    prepare_testbench_plan_ngspice,
)
from .testbench_plan_runner import (
    HostNgspiceExecutor,
    TestbenchPlanRunResult,
    execute_testbench_plan_ngspice,
    publish_testbench_plan_run,
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
    "OSCILLATION_STATUSES",
    "OSCILLATOR_MEASUREMENT_KINDS",
    "SPECTRAL_METRIC_KINDS",
    "TRANSFER_METRIC_KINDS",
    "extract_result_series",
    "measure_result",
    "measure_oscillator",
    "measure_spectrum",
    "measure_transfer",
    "normalized_series_sha256",
    "oscillator_receipt_sha256",
    "SPECIFICATION_LIMIT_KINDS",
    "DISPATCH_EXTENSION",
    "PDK_BINDING_EXTENSION",
    "TARGET_EXTENSION",
    "SimulationRequestError",
    "SimulationTarget",
    "classify_target",
    "simulate",
    "simulate_circuit_profile",
    "simulate_legacy_native",
    "simulate_testbench",
    "evaluate_specification",
    "review_drc",
    "compare_drc",
    "run_experiment",
    "validate_experiment",
    "compile_experiment_template",
    "PreparedTestbenchPlan",
    "TestbenchPlanIssue",
    "load_testbench_plan_schema",
    "validate_testbench_plan",
    "NgspiceCompilationBundle",
    "PreparedNgspiceCompilation",
    "ResolvedBindingValue",
    "TestbenchPlanCompileError",
    "compile_testbench_plan_ngspice",
    "prepare_testbench_plan_ngspice",
    "HostNgspiceExecutor",
    "TestbenchPlanRunResult",
    "execute_testbench_plan_ngspice",
    "publish_testbench_plan_run",
    "compare_testbench_observables",
]
