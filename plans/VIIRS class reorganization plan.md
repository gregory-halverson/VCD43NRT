# VIIRS Class Reorganization Plan

Reorganize the current mixed implementation into explicit base classes plus product-specific subclasses, with one class per Python module in a flat `VCD43NRT` package layout.

## Objectives

- Consolidate shared granule logic into `VIIRSGranule`.
- Add product-specific granule classes inheriting from `VIIRSGranule`:
  - `VNP09GAGranule`
  - `VJ109GAGranule`
  - `VJ209GAGranule`
- Organize data-access classes in parallel:
  - Base class: `VIIRS`
  - Product classes: `VNP09GA`, `VJ109GA`, `VJ209GA`
- Implement each class in its own Python module.

## Decisions Already Made

- Use class names `VJ109GA` and `VJ209GA` exactly.
- Breaking import path changes are allowed.
- Use a flat module layout under `VCD43NRT` (no nested subpackages required).

## Proposed Module Layout

- `VCD43NRT/VIIRS_granule.py` -> `VIIRSGranule`
- `VCD43NRT/VNP09GA_granule.py` -> `VNP09GAGranule`
- `VCD43NRT/VJ109GA_granule.py` -> `VJ109GAGranule`
- `VCD43NRT/VJ209GA_granule.py` -> `VJ209GAGranule`
- `VCD43NRT/VIIRS.py` -> `VIIRS`
- `VCD43NRT/VNP09GA.py` -> `VNP09GA`
- `VCD43NRT/VJ109GA.py` -> `VJ109GA`
- `VCD43NRT/VJ209GA.py` -> `VJ209GA`

Optional support modules if needed for clean dependencies:

- `VCD43NRT/VIIRS_missions.py`
- `VCD43NRT/VIIRS_parsing.py`
- `VCD43NRT/VIIRS_query.py`

## Class Responsibility Boundaries

### Granule hierarchy

`VIIRSGranule` should own shared behavior:

- filename parsing (`filename_base`, `filename_stem`)
- tile/date/hv extraction (`tile`, `date_UTC`, `hv`, `h`, `v`)
- product path generation (`product_directory`, `product_filename`)
- reusable cloud mask + dataset loading helpers
- geometry helpers and generic I/M dispatch utilities

Product-specific granule subclasses should own:

- mission/product identifying constants
- dataset path templates if mission-specific differences are required

### Data-access hierarchy

`VIIRS` should own shared behavior:

- mission normalization and priority handling
- CMR query orchestration
- fallback and dedup policy by date/tile
- download staging/retry/move behavior
- in-memory granule table management

`VNP09GA`, `VJ109GA`, and `VJ209GA` should own:

- mission identity defaults
- granule subclass binding (factory behavior)
- product-specific defaults/directories if they differ

## Implementation Phases

1. Extract mission and filename utility logic from the current monolithic implementation into support modules.
2. Create `VIIRSGranule` module and migrate common granule behavior.
3. Create three product granule modules and reduce subclass logic to mission/product specifics.
4. Create `VIIRS` base data-access module from shared orchestration logic.
5. Create `VNP09GA`, `VJ109GA`, and `VJ209GA` data-access modules from `VIIRS`.
6. Rewire downstream imports/usages (including `VNP43NRT`) to new module paths.
7. Update tests and docs for new import paths and class layout.
8. Remove obsolete monolithic paths once the new structure is verified.

## Verification Checklist

- Mission parsing and normalization behavior remains unchanged.
- Filename-to-mission and filename-to-tile extraction remains unchanged.
- Fallback mission priority behavior remains unchanged.
- Each data-access class returns the correct granule subclass.
- Existing processing workflows still produce expected outputs.
- No stale imports referencing removed monolithic class locations.

## Risks and Mitigations

- Import churn due to allowed breaking changes:
  - Mitigation: update all in-repo imports and tests in one commit.
- Circular dependencies during extraction:
  - Mitigation: split mission/parsing/query helpers into support modules first.
- Behavior drift while moving logic:
  - Mitigation: lock expected behavior with targeted tests before and after move.
