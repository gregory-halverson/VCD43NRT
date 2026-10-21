from .VIIRS import VIIRS, VIIRSUnavailableError
from .VIIRS_granule import ALBEDO_COLORMAP, NDVI_COLORMAP
from .VIIRS_missions import (
    DEFAULT_MISSION,
    DEFAULT_MISSION_PRIORITY,
    MISSION_ALIASES,
    PREFIX_TO_MISSION,
    VIIRS_FILENAME_REGEX,
    VIIRS_MISSIONS,
    mission_from_filename,
    modland_tile_from_filename,
    normalize_mission,
    normalize_mission_priority,
)
from .VIIRS_query import VIIRS_CMR_query, earliest_datetime, latest_datetime
from .VNP09GA_granule import VNP09GAGranule


class VNP09GA(VIIRS):
    DEFAULT_DOWNLOAD_DIRECTORY = "VNP09GA_download"
    DEFAULT_PRODUCTS_DIRECTORY = "VNP09GA_products"
    DEFAULT_MOSAIC_DIRECTORY = "VNP09GA_mosaics"

    PRODUCT_SHORT_NAME = "VNP09GA"
    DEFAULT_MISSION = "SNPP"
    GRANULE_CLASS = VNP09GAGranule

    # Backward-compatible method name retained for callers.
    def prefetch_VNP09GA(self, start_date, end_date, geometry=None):
        return self.prefetch(start_date=start_date, end_date=end_date, geometry=geometry)
