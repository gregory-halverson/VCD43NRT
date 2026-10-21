# VCD43NRT

VCD43NRT is a minimal Python package for working with near-real-time VIIRS/NRT BRDF, albedo, and NDVI products built around the `VNP43NRT`, `VNP43IA4`, and `VNP43MA3` classes.

This repository intentionally contains only the code required for the VIIRS/NRT support layer: the Python package modules that access VIIRS granules through the Earthaccess/CMR path, the MODIS/VIIRS BRDF and solar geometry helpers, and the embedded Julia project used for the VNP43NRT processing path.

The older ECOSTRESS L2T/STARS orchestration code and unrelated product-generation infrastructure have been removed so the package matches the current codebase rather than the earlier full pipeline.

## Purpose

The package provides Python-side support for:

- `VNP43NRT`: near-real-time VIIRS/NRT processing and BRDF correction workflow
- `VNP43IA4`: VIIRS surface reflectance/albedo product interface
- `VNP43MA3`: VIIRS albedo product support

It includes the supporting utilities needed to:

- discover and download VIIRS granules through Earthaccess/CMR
- locate MODLAND tiles and generate grids
- apply solar geometry and BRDF corrections
- manage date ranges and timing helpers
- interface with the embedded Julia implementation in the package-local Julia project

## Package layout

The active package is organized as a flat Python package under `VCD43NRT/` with a separate Julia project at the package root:

- `VCD43NRT/` — Python package modules
- `VCD43NRT/Project.toml` — Julia project metadata
- `VCD43NRT/src/` — Julia source tree
- `VCD43NRT/process_VNP43NRT.jl` — Julia entrypoint used by the Python wrapper

## Prerequisites

This package needs:

- Python 3.11+
- Julia installed and available on `PATH`
- an EarthData account for downloading data products

## Authentication

The package expects EarthData credentials in `~/.netrc`:

```text
machine urs.earthdata.nasa.gov
login <USERNAME>
password <PASSWORD>
```

## Installation

From a clone of this repository:

```bash
pip install .
```

Or install from a local source tree in editable mode during development:

```bash
pip install -e .
```

## Julia setup

The package includes a Julia project stored in the package directory. Instantiate the Julia environment before running the Julia-backed processing path:

```bash
julia --project=VCD43NRT -e 'using Pkg; Pkg.instantiate()'
```

## Usage

```python
from VCD43NRT import VNP43NRT, VNP43IA4, VNP43MA3
```

The package is designed to be imported as a regular Python module and used from the classes above rather than through the older ECOSTRESS-specific CLI flow.

### Single-granule test run

Use the helper script below to generate one VNP43NRT granule for a specific tile/date:

```bash
python scripts/generate_single_vnp43nrt_granule.py \
	--tile h09v05 \
	--date 2026-10-10 \
	--working-directory ./scratch/vcd43nrt
```

Optional flags:

- `--diagnostics` to also write diagnostic layers
- `--initialize-julia` to instantiate the Julia project before processing

## Notes

This repository is intentionally reduced to the runtime classes and utilities that support the VIIRS/NRT products. It is not a full ECOSTRESS data-product orchestration repository, and it does not include the older L2T/STARS product-generation pipeline that was removed during the package simplification. The legacy LP-DAAC direct-download pool is no longer part of the default package path; the supported workflow uses Earthaccess/CMR-backed VIIRS granule discovery and download.

## References

- MODIS/VIIRS BRDF and albedo algorithm literature
- NASA Earthdata and CMR documentation
- VNP43 and VIIRS-related product documentation from the relevant NASA/VIIRS product guides
