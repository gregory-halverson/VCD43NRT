#!/usr/bin/env python3
"""Generate a single VNP43NRT granule for a specified tile and date.

Example:
    python scripts/generate_single_vnp43nrt_granule.py \
      --tile h09v05 \
      --date 2026-10-10 \
      --working-directory ./scratch/vcd43nrt
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import colored_logging as cl


def _import_vnp43nrt_class():
    """Import VNP43NRT from the source tree or an installed package."""
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    try:
        from VCD43NRT.VNP43NRT import VNP43NRT  # type: ignore
        return VNP43NRT
    except Exception:
        package_dir = repo_root / "VCD43NRT"
        if str(package_dir) not in sys.path:
            sys.path.insert(0, str(package_dir))
        from VNP43NRT import VNP43NRT  # type: ignore
        return VNP43NRT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate one VNP43NRT granule at a specific MODLAND tile/date"
    )
    parser.add_argument("--tile", default="h08v05", help="MODLAND tile, e.g. h09v05")
    parser.add_argument("--date", default="2026-10-10", help="UTC date (YYYY-MM-DD)")
    parser.add_argument(
        "--working-directory",
        default="~/data/VCD43NRT",
        help="Working/output directory used by VNP43NRT (default: ~/data/VCD43NRT)",
    )
    parser.add_argument(
        "--diagnostics",
        action="store_true",
        help="Write diagnostic layers in addition to required outputs",
    )
    parser.add_argument(
        "--initialize-julia",
        action="store_true",
        help="Instantiate Julia project before processing",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    cl.configure()
    args = build_parser().parse_args(argv)

    VNP43NRT = _import_vnp43nrt_class()

    processor = VNP43NRT(
        working_directory=args.working_directory,
        initialize_julia=args.initialize_julia,
    )

    granule_dir = processor.granule_directory(date_UTC=args.date, tile=args.tile)
    if Path(granule_dir).exists():
        print(f"Removing stale granule directory before generation: {granule_dir}")
        import shutil
        shutil.rmtree(granule_dir, ignore_errors=True)

    granule = processor.VNP43NRT(
        date_UTC=args.date,
        tile=args.tile,
        diagnostics=args.diagnostics,
    )

    print(f"Generated granule: {granule.granule_ID}")
    print(f"Output directory: {granule.directory}")

    if Path(granule.directory).exists():
        outputs = sorted(Path(granule.directory).glob("*.tif"))
        print(f"GeoTIFF count: {len(outputs)}")
        for tif in outputs:
            print(f" - {tif.name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
