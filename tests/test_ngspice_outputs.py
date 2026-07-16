from __future__ import annotations

from io import BytesIO
import json
import struct

from openada.engines.ngspice_outputs import (
    _BoundedLineReader,
    _validate_ascii_payload,
    analysis_raw_counts,
    OutputValidation,
    ValidationLimits,
    validate_ngspice_raw,
    validate_ngspice_wrdata,
    validate_xyce_raw,
)


def _raw_header(
    *,
    plotname: str,
    flags: str,
    points: int,
    variables: list[str],
    marker: str,
) -> bytes:
    variable_table = "".join(
        f"\t{index}\t{definition}\n" for index, definition in enumerate(variables)
    )
    return (
        "Title: validator fixture\n"
        "Date: fixture\n"
        "Command: ngspice fixture\n"
        f"Plotname: {plotname}\n"
        f"Flags: {flags}\n"
        f"No. Variables: {len(variables)}\n"
        f"No. Points: {points}\n"
        "Variables:\n"
        f"{variable_table}"
        f"{marker}:\n"
    ).encode("ascii")


def _binary_plot(
    *,
    plotname: str,
    points: int,
    variables: list[str],
    values: list[float],
    flags: str = "real",
) -> bytes:
    return _raw_header(
        plotname=plotname,
        flags=flags,
        points=points,
        variables=variables,
        marker="Binary",
    ) + struct.pack(f"={len(values)}d", *values)


def _ascii_plot(
    *,
    plotname: str,
    points: int,
    variables: list[str],
    rows: list[list[str]],
    flags: str = "real",
) -> bytes:
    payload: list[str] = []
    for index, row in enumerate(rows):
        payload.append(f" {index}\t{row[0]}\n")
        payload.extend(f"\t{value}\n" for value in row[1:])
        payload.append("\n")
    return _raw_header(
        plotname=plotname,
        flags=flags,
        points=points,
        variables=variables,
        marker="Values",
    ) + "".join(payload).encode("ascii")


def test_raw_validator_accepts_appended_binary_and_ascii_plots(tmp_path):
    raw = tmp_path / "appended.raw"
    constants = _binary_plot(
        plotname="constants",
        points=1,
        variables=["yes notype", "temperature temperature"],
        values=[1.0, 27.0],
    )
    transient = _ascii_plot(
        plotname="Transient Analysis",
        points=2,
        variables=["time time", "v(out) voltage"],
        rows=[["0.0", "1.2"], ["1e-9", "0.0"]],
    )
    raw.write_bytes(constants + transient)

    result = validate_ngspice_raw(raw)

    assert result.valid is True
    assert result.reason == "valid"
    assert result.metadata["plot_count"] == 2
    assert result.metadata["analysis_plot_count"] == 1
    assert result.metadata["has_analysis_plot"] is True
    assert result.metadata["value_count"] == 6
    assert [plot["encoding"] for plot in result.metadata["plots"]] == [
        "binary",
        "ascii",
    ]
    json.dumps(result.to_dict())


def test_raw_validator_accepts_complex_binary_and_ascii_values(tmp_path):
    raw = tmp_path / "complex.raw"
    binary = _binary_plot(
        plotname="AC Analysis",
        points=1,
        variables=["frequency frequency", "v(out) voltage"],
        values=[1.0, 0.0, 0.5, -0.25],
        flags="complex",
    )
    ascii_values = _ascii_plot(
        plotname="AC Analysis",
        points=1,
        variables=["frequency frequency", "v(out) voltage"],
        rows=[["1.0,0.0", "(0.5,-0.25)"]],
        flags="complex",
    )
    raw.write_bytes(binary + ascii_values)

    result = validate_ngspice_raw(raw)

    assert result.valid is True
    assert result.metadata["value_count"] == 4
    assert result.metadata["numeric_scalar_count"] == 8
    assert all(plot["numeric_type"] == "complex" for plot in result.metadata["plots"])


def test_request_binding_accepts_reviewed_backend_specific_nonaligned_ac_grids(
    tmp_path,
):
    cases = (
        (
            "ngspice",
            validate_ngspice_raw,
            [10.0, 41.212853, 169.849925, 700.0],
        ),
        (
            "xyce",
            validate_xyce_raw,
            [10.0, 31.6227766, 100.0, 316.227766],
        ),
    )
    analysis = {
        "type": "ac",
        "sweep": "dec",
        "points": 2,
        "start_hz": 10.0,
        "stop_hz": 700.0,
        "extensions": {},
    }

    for backend, validator, axis in cases:
        raw = tmp_path / f"{backend}.raw"
        raw.write_bytes(
            _ascii_plot(
                plotname="AC Analysis",
                points=len(axis),
                variables=[
                    "frequency frequency",
                    "v(in) voltage",
                    "v(out) voltage",
                ],
                rows=[
                    [f"({frequency:.10g},0)", "(1,0)", "(0.5,-0.5)"]
                    for frequency in axis
                ],
                flags="complex",
            )
        )
        validation = validator(raw)

        assert validation.valid is True
        assert analysis_raw_counts(
            {"validation": validation.to_dict()}, analysis
        ) == (4, 2, 16)


def test_request_binding_treats_dc_stop_as_an_upper_limit(tmp_path):
    axis = [0.1 + index * 0.5 for index in range(30)]
    raw = tmp_path / "dc.raw"
    raw.write_bytes(
        _ascii_plot(
            plotname="DC transfer characteristic",
            points=len(axis),
            variables=["v(v-sweep) voltage", "v(in) voltage", "v(out) voltage"],
            rows=[
                [f"{value:.17g}", f"{value:.17g}", f"{value / 2:.17g}"]
                for value in axis
            ],
        )
    )
    validation = validate_ngspice_raw(raw)
    analysis = {
        "type": "dc",
        "source_name": "VSWEEP",
        "source_unit": "V",
        "start": 0.1,
        "stop": 15.0,
        "step": 0.5,
        "extensions": {},
    }

    assert validation.valid is True
    assert analysis_raw_counts(
        {"validation": validation.to_dict()}, analysis
    ) == (30, 2, 60)


def test_request_binding_accepts_ngspice_generic_dc_axis_for_named_source(tmp_path):
    raw = tmp_path / "dc-v1.raw"
    raw.write_bytes(
        _ascii_plot(
            plotname="DC transfer characteristic",
            points=3,
            variables=["v(v-sweep) voltage", "v(out) voltage"],
            rows=[["0", "1.2"], ["0.5", "0.6"], ["1", "0"]],
        )
    )
    validation = validate_ngspice_raw(raw)
    analysis = {
        "type": "dc",
        "source_name": "V1",
        "source_unit": "V",
        "start": 0.0,
        "stop": 1.0,
        "step": 0.5,
        "extensions": {},
    }

    assert validation.valid is True
    assert analysis_raw_counts({"validation": validation.to_dict()}, analysis) == (
        3,
        1,
        3,
    )


def test_request_binding_accepts_exact_ngspice_linearized_transient_plot(tmp_path):
    raw = tmp_path / "linearized.raw"
    raw.write_bytes(
        _ascii_plot(
            plotname="Transient Analysis (linearized)",
            points=4,
            variables=["time time", "v(out) voltage"],
            rows=[
                ["5e-7", "0.0"],
                ["5.3125e-7", "0.5"],
                ["5.625e-7", "1.0"],
                ["5.9375e-7", "0.5"],
            ],
        )
    )
    validation = validate_ngspice_raw(raw)
    analysis = {
        "type": "tran",
        "step_s": 3.125e-8,
        "start_s": 5e-7,
        "stop_s": 5.9375e-7,
        "extensions": {},
    }

    assert validation.valid is True
    assert analysis_raw_counts(
        {"validation": validation.to_dict()}, analysis
    ) == (4, 1, 4)


def test_raw_validator_handles_unpadded_variable_dimensions(tmp_path):
    raw = tmp_path / "unpadded.raw"
    variables = ["yes notype dims=1", "short voltage dims=3", "long voltage"]
    raw.write_bytes(
        _binary_plot(
            plotname="Transient Analysis",
            points=5,
            variables=variables,
            values=[1.0, 0.0, 0.0, 1.0, 1.0, 2.0, 2.0, 3.0, 4.0],
            flags="real unpadded",
        )
    )

    result = validate_ngspice_raw(raw)

    assert result.valid is True
    assert result.metadata["value_count"] == 9
    assert result.metadata["plots"][0]["unpadded"] is True


def test_raw_validator_handles_ascii_unpadded_variable_dimensions(tmp_path):
    raw = tmp_path / "unpadded-ascii.raw"
    raw.write_bytes(
        _ascii_plot(
            plotname="Transient Analysis",
            points=5,
            variables=[
                "short voltage dims=1",
                "medium voltage dims=3",
                "long voltage",
            ],
            rows=[
                ["1.0", "2.0", "3.0"],
                ["2.1", "3.1"],
                ["2.2", "3.2"],
                ["3.3"],
                ["3.4"],
            ],
            flags="real unpadded",
        )
    )

    result = validate_ngspice_raw(raw)

    assert result.valid is True
    assert result.metadata["value_count"] == 9
    assert result.metadata["numeric_scalar_count"] == 9


def test_unpadded_ascii_validation_does_not_rescan_every_dimension_per_point():
    class CountingLengths(list[int]):
        element_visits = 0

        def __iter__(self):
            for value in super().__iter__():
                self.element_visits += 1
                yield value

    points = 100
    lengths = CountingLengths([1] * 999 + [points])
    payload = b"0 0\n" + (b"\t0\n" * 999) + b"".join(
        f"{index} 0\n".encode("ascii") for index in range(1, points)
    )
    reader = _BoundedLineReader(BytesIO(payload), 65_536, "raw")

    _validate_ascii_payload(
        reader,
        points=points,
        variable_lengths=lengths,
        complex_values=False,
        unpadded=True,
    )

    assert lengths.element_visits <= 2 * len(lengths)


def test_raw_validator_rejects_constants_only(tmp_path):
    raw = tmp_path / "constants.raw"
    raw.write_bytes(
        _binary_plot(
            plotname="constants",
            points=1,
            variables=["yes notype", "foo notype"],
            values=[1.0, 2.0],
        )
    )

    result = validate_ngspice_raw(raw)

    assert result.valid is False
    assert result.reason == "raw.constants_only"
    assert result.metadata["has_analysis_plot"] is False


def test_raw_validator_rejects_nonempty_file_without_plots(tmp_path):
    raw = tmp_path / "whitespace.raw"
    raw.write_bytes(b"\n\r\n")

    result = validate_ngspice_raw(raw)

    assert result.valid is False
    assert result.reason == "raw.no_plots"


def test_raw_validator_rejects_truncated_binary_payload(tmp_path):
    raw = tmp_path / "truncated.raw"
    complete = _binary_plot(
        plotname="Transient Analysis",
        points=2,
        variables=["time time", "v(out) voltage"],
        values=[0.0, 1.0, 1.0e-9, 0.0],
    )
    raw.write_bytes(complete[:-1])

    result = validate_ngspice_raw(raw)

    assert result.valid is False
    assert result.reason == "raw.truncated_binary_payload"


def test_raw_validator_rejects_truncated_ascii_payload(tmp_path):
    raw = tmp_path / "truncated-ascii.raw"
    raw.write_bytes(
        _raw_header(
            plotname="Transient Analysis",
            flags="real",
            points=2,
            variables=["time time", "v(out) voltage"],
            marker="Values",
        )
        + b"0 0.0\n\t1.0\n"
    )

    result = validate_ngspice_raw(raw)

    assert result.valid is False
    assert result.reason == "raw.truncated_ascii_payload"


def test_raw_validator_enforces_explicit_count_and_line_limits(tmp_path):
    raw = tmp_path / "bounded.raw"
    raw.write_bytes(
        _binary_plot(
            plotname="Transient Analysis",
            points=2,
            variables=["time time", "v(out) voltage"],
            values=[0.0, 1.0, 1.0e-9, 0.0],
        )
    )

    point_limited = validate_ngspice_raw(raw, limits=ValidationLimits(max_raw_points=1))
    line_limited = validate_ngspice_raw(raw, limits=ValidationLimits(max_line_bytes=8))

    assert point_limited.reason == "raw.point_count_invalid"
    assert line_limited.reason == "raw.line_too_long"


def test_raw_validator_rejects_symlinks_and_non_finite_values(tmp_path):
    raw = tmp_path / "nonfinite.raw"
    raw.write_bytes(
        _binary_plot(
            plotname="Transient Analysis",
            points=1,
            variables=["time time", "v(out) voltage"],
            values=[0.0, float("nan")],
        )
    )
    linked = tmp_path / "linked.raw"
    linked.symlink_to(raw)

    assert validate_ngspice_raw(raw).reason == "raw.non_finite_value"
    assert validate_ngspice_raw(linked).reason == "file.not_regular"


def test_wrdata_validator_accepts_real_complex_vecnames_and_append_shapes(tmp_path):
    table = tmp_path / "waveforms.dat"
    table.write_text(
        "time v(out) time i(v1)\n"
        "0.0 1.2 0.0 -1e-3\n"
        "1e-9 0.0 1e-9 -2e-3\n"
        "frequency v(out) v(out)\n"
        "1.0 0.5 -0.25\n"
        "10.0 0.1 -0.05\n"
        "\n"
        "0.0 1.0 2.0\n"
        "1.0 3.0 4.0\n",
        encoding="ascii",
    )

    result = validate_ngspice_wrdata(table)

    assert result.valid is True
    assert result.reason == "valid"
    assert result.metadata == {
        "format": "ngspice-wrdata",
        "bytes": table.stat().st_size,
        "row_count": 6,
        "numeric_value_count": 20,
        "header_row_count": 2,
        "section_count": 3,
        "shape_count": 2,
        "column_count_min": 3,
        "column_count_max": 4,
    }
    json.dumps(result.to_dict())


def test_wrdata_validator_accepts_undelimited_append_shape_change(tmp_path):
    table = tmp_path / "append-no-names.dat"
    table.write_text(
        "0.0 1.0 0.0 -1e-3\n"
        "1.0 0.5 0.0\n"
        "10.0 0.1 -0.05\n",
        encoding="ascii",
    )

    result = validate_ngspice_wrdata(table)

    assert result.valid is True
    assert result.metadata["section_count"] == 2
    assert result.metadata["shape_count"] == 2


def test_wrdata_validator_rejects_headers_without_finite_data(tmp_path):
    table = tmp_path / "headers.dat"
    table.write_text("time v(out)\n", encoding="ascii")

    result = validate_ngspice_wrdata(table)

    assert result.valid is False
    assert result.reason == "wrdata.header_without_data"


def test_wrdata_validator_rejects_nonfinite_and_mixed_rows(tmp_path):
    nonfinite = tmp_path / "nonfinite.dat"
    nonfinite.write_text("0.0 nan\n", encoding="ascii")
    mixed = tmp_path / "mixed.dat"
    mixed.write_text("0.0 voltage\n", encoding="ascii")

    assert validate_ngspice_wrdata(nonfinite).reason == "wrdata.non_finite_value"
    assert validate_ngspice_wrdata(mixed).reason == "wrdata.mixed_row"


def test_wrdata_validator_enforces_row_line_and_file_limits(tmp_path):
    table = tmp_path / "bounded.dat"
    table.write_text("0.0 1.0\n1.0 2.0\n", encoding="ascii")

    row_limited = validate_ngspice_wrdata(table, limits=ValidationLimits(max_wrdata_rows=1))
    line_limited = validate_ngspice_wrdata(table, limits=ValidationLimits(max_line_bytes=4))
    file_limited = validate_ngspice_wrdata(
        table,
        limits=ValidationLimits(max_file_bytes=table.stat().st_size - 1),
    )

    assert row_limited.reason == "wrdata.too_many_rows"
    assert line_limited.reason == "wrdata.line_too_long"
    assert file_limited.reason == "file.too_large"


def test_validation_result_api_is_typed_and_json_serializable(tmp_path):
    table = tmp_path / "data.dat"
    table.write_text("0 1\n", encoding="ascii")

    result = validate_ngspice_wrdata(table)

    assert isinstance(result, OutputValidation)
    assert json.loads(json.dumps(result.to_dict()))["valid"] is True
