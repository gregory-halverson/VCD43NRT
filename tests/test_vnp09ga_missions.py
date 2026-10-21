from VCD43NRT.VNP09GA import (
    DEFAULT_MISSION,
    VIIRS_MISSIONS,
    mission_from_filename,
    modland_tile_from_filename,
    normalize_mission,
    normalize_mission_priority,
)
from VCD43NRT.VIIRS import VIIRS
from VCD43NRT.VIIRS_granule import VIIRSGranule
from VCD43NRT.VNP09GA import VNP09GA
from VCD43NRT.VNP09GA_granule import VNP09GAGranule
from VCD43NRT.VJ109GA import VJ109GA
from VCD43NRT.VJ109GA_granule import VJ109GAGranule
from VCD43NRT.VJ209GA import VJ209GA
from VCD43NRT.VJ209GA_granule import VJ209GAGranule


def test_normalize_mission_aliases():
    assert normalize_mission("SNPP") == "SNPP"
    assert normalize_mission("noaa-20") == "NOAA20"
    assert normalize_mission("jpss-2") == "NOAA21"


def test_normalize_mission_priority_string():
    priority = normalize_mission_priority("SNPP,NOAA20,NOAA21", mission="SNPP", fallback_mode=True)
    assert priority == ("SNPP", "NOAA20", "NOAA21")


def test_default_mission_priority_behavior():
    disabled = normalize_mission_priority(None, mission="NOAA20", fallback_mode=False)
    enabled = normalize_mission_priority(None, mission=DEFAULT_MISSION, fallback_mode=True)

    assert disabled == ("NOAA20",)
    assert enabled == ("SNPP", "NOAA20", "NOAA21")


def test_filename_parsing_for_supported_prefixes():
    snpp_name = "VNP09GA.A2026283.h08v05.002.2026284000000.h5"
    n20_name = "JNP09GA.A2026283.h08v05.002.2026284000000.h5"
    n21_name = "SNP09GA.A2026283.h08v05.002.2026284000000.h5"

    assert modland_tile_from_filename(snpp_name) == "h08v05"
    assert mission_from_filename(snpp_name) == "SNPP"
    assert mission_from_filename(n20_name) == "NOAA20"
    assert mission_from_filename(n21_name) == "NOAA21"


def test_mission_registry_contains_required_entries():
    assert "SNPP" in VIIRS_MISSIONS
    assert "NOAA20" in VIIRS_MISSIONS
    assert "NOAA21" in VIIRS_MISSIONS

    assert VIIRS_MISSIONS["SNPP"].short_name == "VNP09GA"
    assert VIIRS_MISSIONS["NOAA20"].short_name == "JNP09GA"
    assert VIIRS_MISSIONS["NOAA21"].short_name == "SNP09GA"


def test_viirs_class_hierarchy():
    assert issubclass(VNP09GAGranule, VIIRSGranule)
    assert issubclass(VJ109GAGranule, VIIRSGranule)
    assert issubclass(VJ209GAGranule, VIIRSGranule)

    assert issubclass(VNP09GA, VIIRS)
    assert issubclass(VJ109GA, VIIRS)
    assert issubclass(VJ209GA, VIIRS)
