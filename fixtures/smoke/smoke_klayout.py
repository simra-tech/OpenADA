#!/usr/bin/env python3
"""Minimal KLayout batch smoke: create and inspect a tiny GDS."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

import klayout.db as kdb


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="External artifact directory (default: a fresh system temporary directory).",
    )
    args = parser.parse_args()
    out_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir is not None
        else Path(tempfile.mkdtemp(prefix="openada-klayout-smoke-")).resolve()
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    gds = out_dir / "toy.gds"

    layout = kdb.Layout()
    layout.dbu = 0.001
    cell = layout.create_cell("TOP")
    layer = layout.layer(1, 0)
    cell.shapes(layer).insert(kdb.Box(0, 0, 1000, 2000))
    layout.write(str(gds))

    readback = kdb.Layout()
    readback.read(str(gds))
    top = readback.top_cell()
    shape_count = sum(1 for _ in top.shapes(readback.layer(1, 0)).each())
    summary = {
        "success": top.name == "TOP" and shape_count == 1,
        "gds": str(gds),
        "top_cell": top.name,
        "dbu": readback.dbu,
        "bbox": top.bbox().to_s(),
        "layer_1_0_shapes": shape_count,
    }
    print(json.dumps(summary, indent=2))
    return 0 if summary["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
