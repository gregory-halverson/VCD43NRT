## Plan: JPSS Multi-Mission VIIRS Access

Add optional multi-mission VIIRS support without breaking existing APIs by introducing a mission registry, parameterizing CMR discovery and local product naming, and implementing mission-priority fallback across Suomi NPP, NOAA-20, and NOAA-21. Keep current VNP behavior as default while enabling automatic fallback when data is unavailable for a date/tile.

**Steps**
1. Phase 1: Baseline and interface contract
2. Confirm public entry points that must remain stable: VNP09GA constructor and VNP43NRT constructor, plus existing methods used by examples and README workflows. Document current defaults and expected behavior from VNP-only mode. This is blocking for all later steps.
3. Define new optional parameters with backward-compatible defaults:
- mission: single mission selector (default SNPP behavior)
- mission_priority: ordered list for fallback (default [SNPP, NOAA20, NOAA21] when fallback mode is enabled)
- fallback_mode: boolean or strategy enum controlling whether to search one mission or try ordered missions
4. Phase 2: Mission configuration abstraction
5. Create a new mission/product config module containing per-mission metadata: product prefix, CMR concept ID, filename regex patterns, dataset path templates, scale factors, and albedo coefficients. Keep SNPP values as currently implemented and add placeholders or validated values for NOAA20 and NOAA21.
6. Add mission normalization helpers and validation to reject unsupported mission keys early with clear errors.
7. Phase 3: CMR query and granule matching refactor
8. Refactor VIIRS_CMR_query to accept mission configuration and perform mission-scoped queries.
9. Implement mission-priority query orchestration:
- For single date/tile requests: query missions in order and stop at first mission with matching granule(s)
- For date ranges/geometry prefetch: query each mission and deduplicate by date+tile with deterministic priority winner
10. Update filename parsing and tile extraction to support mission-specific product prefixes instead of hardcoded VNP regex.
11. Phase 4: Download and storage layout updates
12. Update download path structure so cached files remain mission-disambiguated and do not collide between missions.
13. Update products and mosaics directory naming to remain backward-compatible for SNPP defaults while supporting mission-aware directories when non-SNPP missions are used.
14. Ensure granule bookkeeping table stores mission in addition to date_UTC/tile/granule so search and cache logic can resolve fallback consistently. Depends on steps 8-13.
15. Phase 5: Granule processing parameterization
16. Update VNP09GAGranule dataset path construction to use mission config templates rather than repeated hardcoded strings.
17. Centralize reflectance/angle scale factors and albedo coefficient usage through mission config to minimize repeated constants.
18. Add mission-aware albedo calculation hooks so coefficients can differ by mission in future without changing call sites.
19. Phase 6: VNP43NRT integration
20. Thread mission/fallback parameters from VNP43NRT into internal VNP09GA instance.
21. Keep VNP43NRT output behavior compatible by default, but optionally include mission in granule ID/output directory naming for non-SNPP runs to avoid ambiguity.
22. Verify staging directory generation and Julia handoff remain mission-agnostic, with only input source selection changing. Depends on phases 3-5.
23. Phase 7: Documentation and examples
24. Update README usage to show:
- Legacy SNPP-only usage unchanged
- Single explicit mission usage (NOAA20, NOAA21)
- Priority fallback usage across missions
25. Update example script to optionally accept mission and mission priority arguments.
26. Update package description text where needed to reflect multi-mission VIIRS coverage.
27. Phase 8: Verification and smoke testing
28. Add unit-level tests for:
- mission config resolution and validation
- filename parsing across mission prefixes
- fallback selection behavior given mocked CMR responses
- download path disambiguation and deduplication priority rules
29. Add optional live smoke test workflow (credential-gated) that queries one known date/tile per mission and verifies at least one granule can be discovered and downloaded.
30. Run end-to-end dry run for a tile/date with fallback enabled and confirm generated NDVI/albedo outputs and no regression in SNPP default flow.

**Relevant files**
- /Users/gregoryhalverson/Library/CloudStorage/GoogleDrive-gregory.halverson@gmail.com/My Drive/Projects/VCD43NRT/VCD43NRT/VNP09GA.py — primary CMR query, filename parsing, download paths, granule parsing, albedo constants
- /Users/gregoryhalverson/Library/CloudStorage/GoogleDrive-gregory.halverson@gmail.com/My Drive/Projects/VCD43NRT/VCD43NRT/VNP43NRT.py — top-level workflow orchestration, VNP09GA composition, output granule naming
- /Users/gregoryhalverson/Library/CloudStorage/GoogleDrive-gregory.halverson@gmail.com/My Drive/Projects/VCD43NRT/VCD43NRT/VIIRSDownloader.py — abstract interfaces; likely no API changes but confirm compatibility
- /Users/gregoryhalverson/Library/CloudStorage/GoogleDrive-gregory.halverson@gmail.com/My Drive/Projects/VCD43NRT/examples/generate_single_VNP43NRT_granule.py — CLI/example arguments and usage patterns
- /Users/gregoryhalverson/Library/CloudStorage/GoogleDrive-gregory.halverson@gmail.com/My Drive/Projects/VCD43NRT/README.md — user-facing behavior, setup notes, mission examples
- /Users/gregoryhalverson/Library/CloudStorage/GoogleDrive-gregory.halverson@gmail.com/My Drive/Projects/VCD43NRT/pyproject.toml — optional metadata text updates

**Verification**
1. Static checks: run package tests after adding mission/fallback tests; verify no regressions in existing SNPP behavior.
2. Constructor compatibility checks: instantiate VNP09GA() and VNP43NRT() with no new args and confirm defaults are unchanged.
3. Mission parsing tests: validate date/tile extraction for SNPP, NOAA20, NOAA21 filename samples.
4. Fallback tests with mocked CMR: ensure selection honors mission_priority order and stops at first available mission for date/tile workflows.
5. Live smoke checks (when Earthdata credentials exist): run CMR discovery and one granule download for each configured mission, then one fallback-enabled call where first mission is intentionally unavailable.
6. End-to-end product check: generate one granule with fallback enabled and confirm NDVI/albedo files are produced with expected directory naming.

**Decisions**
- Include both NOAA-20 and NOAA-21 in this scope.
- Runtime behavior uses mission-priority fallback when enabled.
- Preserve no-break API compatibility; all new behavior is opt-in or default-safe.
- Include live network smoke validation when credentials are available.
- In scope: access and processing support for equivalent JPSS VIIRS products through existing workflow.
- Out of scope: major class renames, wholesale package redesign, and unrelated algorithm changes beyond mission parameterization.

**Further Considerations**
1. Mission labeling in output IDs: Option A keep VNP43NRT prefix always for compatibility, Option B append mission token for non-SNPP outputs, Option C always include mission token. Recommendation: Option B.
2. Concept ID maintenance: Option A hardcode IDs in source, Option B load from lightweight config file, Option C allow env override with source defaults. Recommendation: Option C for operational flexibility.
3. Fallback semantics for range prefetch: Option A first mission that has any data wins globally, Option B choose best mission per date/tile record, Option C download all missions and merge later. Recommendation: Option B.