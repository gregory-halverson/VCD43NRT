import logging
import os
import tempfile
from datetime import date
from os.path import abspath, expanduser, join
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

import earthaccess
import pandas as pd
from dateutil import parser
from rasters import Point, Polygon, RasterGeometry

from .constants import RETRIES
from .VIIRS_missions import (
    DEFAULT_MISSION,
    VIIRS_MISSIONS,
    mission_from_filename,
    modland_tile_from_filename,
    normalize_mission,
    normalize_mission_priority,
)
from .VIIRS_query import VIIRS_CMR_query
from .VIIRS_granule import VIIRSGranule

try:
    from .login import login
    from .daterange import get_date
except ImportError:  # pragma: no cover - legacy direct-import fallback
    from login import login
    from daterange import get_date


logger = logging.getLogger(__name__)


class DownloadFailed(ConnectionError):
    pass


class VIIRSUnavailableError(Exception):
    pass


class VIIRS:
    DEFAULT_WORKING_DIRECTORY = "."
    DEFAULT_DOWNLOAD_DIRECTORY = "VIIRS_download"
    DEFAULT_PRODUCTS_DIRECTORY = "VIIRS_products"
    DEFAULT_MOSAIC_DIRECTORY = "VIIRS_mosaics"
    DEFAULT_RESAMPLING = "nearest"

    PRODUCT_SHORT_NAME = "VIIRS"
    DEFAULT_MISSION = DEFAULT_MISSION
    GRANULE_CLASS = VIIRSGranule

    def __init__(
        self,
        working_directory: str = None,
        download_directory: str = None,
        products_directory: str = None,
        mosaic_directory: str = None,
        mission: str = None,
        mission_priority: Optional[Sequence[str]] = None,
        fallback_mode: bool = False,
        resampling: str = None,
    ):
        if resampling is None:
            resampling = self.DEFAULT_RESAMPLING

        if mission is None:
            mission = self.DEFAULT_MISSION

        self.resampling = resampling
        self.mission = normalize_mission(mission)
        self.fallback_mode = bool(fallback_mode)
        self.mission_priority = normalize_mission_priority(
            mission_priority=mission_priority,
            mission=self.mission,
            fallback_mode=self.fallback_mode,
        )
        self._granules = pd.DataFrame(columns=["date_UTC", "tile", "mission", "granule"])

        if working_directory is None:
            working_directory = self.DEFAULT_WORKING_DIRECTORY

        working_directory = abspath(expanduser(working_directory))

        if download_directory is None:
            download_directory = join(working_directory, self.DEFAULT_DOWNLOAD_DIRECTORY)

        download_directory = abspath(expanduser(download_directory))

        if products_directory is None:
            products_directory = join(working_directory, self.DEFAULT_PRODUCTS_DIRECTORY)

        products_directory = abspath(expanduser(products_directory))

        if mosaic_directory is None:
            mosaic_directory = join(working_directory, self.DEFAULT_MOSAIC_DIRECTORY)

        mosaic_directory = abspath(expanduser(mosaic_directory))

        self.working_directory = working_directory
        self.download_directory = download_directory
        self.products_directory = products_directory
        self.mosaic_directory = mosaic_directory

        self.auth = login()

    @property
    def active_missions(self) -> Tuple[str, ...]:
        return self.mission_priority if self.fallback_mode else (self.mission,)

    @staticmethod
    def _granule_filename(granule: earthaccess.search.DataGranule) -> str:
        links = granule.data_links()
        if len(links) == 0:
            raise ValueError("granule is missing data links")
        return Path(links[0]).name

    def _download_output_path(self, granule: earthaccess.search.DataGranule) -> str:
        filename = self._granule_filename(granule)
        mission_key = mission_from_filename(filename)
        mission_cfg = VIIRS_MISSIONS[mission_key]
        date_utc = get_date(granule["umm"]["TemporalExtent"]["RangeDateTime"]["BeginningDateTime"])

        return join(self.download_directory, mission_cfg.short_name, f"{date_utc:%Y.%m.%d}", filename)

    def _search_for_mission(
        self,
        mission: str,
        start_date: Union[date, str],
        end_date: Union[date, str],
        geometry: Point or Polygon or RasterGeometry = None,
        tile: str = None,
    ) -> List[earthaccess.search.DataGranule]:
        return VIIRS_CMR_query(
            start_date=start_date,
            end_date=end_date,
            mission=mission,
            target_geometry=geometry,
            tile=tile,
        )

    def _search_with_fallback(
        self,
        start_date: Union[date, str],
        end_date: Union[date, str],
        geometry: Point or Polygon or RasterGeometry = None,
        tile: str = None,
    ) -> List[earthaccess.search.DataGranule]:
        if tile is not None:
            for mission_key in self.active_missions:
                granules = self._search_for_mission(
                    mission=mission_key,
                    start_date=start_date,
                    end_date=end_date,
                    geometry=geometry,
                    tile=tile,
                )
                if len(granules) > 0:
                    return granules
            return []

        granules_by_key: Dict[Tuple[date, str], earthaccess.search.DataGranule] = {}
        for mission_key in self.active_missions:
            granules = self._search_for_mission(
                mission=mission_key,
                start_date=start_date,
                end_date=end_date,
                geometry=geometry,
            )

            for granule in granules:
                filename = self._granule_filename(granule)
                key = (
                    get_date(granule["umm"]["TemporalExtent"]["RangeDateTime"]["BeginningDateTime"]),
                    modland_tile_from_filename(filename),
                )
                if key not in granules_by_key:
                    granules_by_key[key] = granule

        return sorted(
            granules_by_key.values(),
            key=lambda granule: granule["umm"]["TemporalExtent"]["RangeDateTime"]["BeginningDateTime"],
        )

    def add_granules(self, granules: List[earthaccess.search.DataGranule]):
        data = pd.DataFrame(
            [
                {
                    "date_UTC": get_date(granule["umm"]["TemporalExtent"]["RangeDateTime"]["BeginningDateTime"]),
                    "tile": modland_tile_from_filename(self._granule_filename(granule)),
                    "mission": mission_from_filename(self._granule_filename(granule)),
                    "granule": granule,
                }
                for granule in granules
            ]
        )

        if len(data) == 0:
            return

        priority_index = {mission: idx for idx, mission in enumerate(self.active_missions)}

        combined = pd.concat([self._granules, data], ignore_index=True)
        combined["priority"] = combined["mission"].map(lambda value: priority_index.get(value, len(priority_index)))
        combined = combined.sort_values(by=["date_UTC", "tile", "priority"])
        self._granules = combined.drop_duplicates(subset=["date_UTC", "tile"], keep="first").drop(columns=["priority"])

    def download_granules(self, granules: List[earthaccess.search.DataGranule]) -> List[str]:
        granules_to_download = []
        output_paths = []
        for granule in granules:
            output_file_path = self._download_output_path(granule)
            if Path(output_file_path).exists():
                output_paths.append(output_file_path)
            else:
                granules_to_download.append(granule)

        if len(granules_to_download) == 0:
            logger.info("All VIIRS granules have already been downloaded")
            return output_paths

        temporary_parent_directory = join(self.download_directory, "tmp")
        os.makedirs(temporary_parent_directory, exist_ok=True)
        temporary_download_directory = tempfile.mkdtemp(dir=temporary_parent_directory)

        try:
            last_download_exception = None
            for _ in range(0, RETRIES):
                download_exception = None
                downloaded_granules = []

                file_paths = earthaccess.download(granules_to_download, local_path=temporary_download_directory)

                for granule, download_file_path in zip(granules_to_download, file_paths):
                    if isinstance(download_file_path, Exception):
                        if download_exception is None:
                            download_exception = download_file_path
                        continue

                    download_file_path = Path(download_file_path)
                    output_file_path = self._download_output_path(granule)
                    Path(output_file_path).parent.mkdir(parents=True, exist_ok=True)
                    download_file_path.rename(output_file_path)

                    output_paths.append(output_file_path)
                    downloaded_granules.append(granule)

                if download_exception is not None:
                    last_download_exception = download_exception
                    for granule in downloaded_granules:
                        granules_to_download.remove(granule)
                    logger.warning("Encountered an exception while downloading VIIRS files:", exc_info=download_exception)
                    logger.info(f"Retrying the VIIRS download with the remaining {len(granules_to_download)} granules.")
                else:
                    granules_to_download = []
                    break

            if len(granules_to_download) > 0:
                raise DownloadFailed("Error when downloading VIIRS files") from last_download_exception
        finally:
            Path(temporary_download_directory).rmdir()

        return output_paths

    def prefetch(
        self,
        start_date: Union[date, str],
        end_date: Union[date, str],
        geometry: Point or Polygon or RasterGeometry = None,
    ):
        granules = self._search_with_fallback(start_date=start_date, end_date=end_date, geometry=geometry)
        self.add_granules(granules)
        self.download_granules(granules)

    def search(self, date_UTC: date, tile: str) -> Union[earthaccess.search.DataGranule, None]:
        if "date_UTC" not in self._granules.columns:
            raise ValueError("date_UTC column not in granules table")

        subset = self._granules[(self._granules["date_UTC"] == date_UTC) & (self._granules["tile"] == tile)]
        if len(subset) > 0:
            return subset.iloc[0].granule

        granules = self._search_with_fallback(start_date=date_UTC, end_date=date_UTC, tile=tile)

        if len(granules) == 0:
            return None

        if len(granules) > 1:
            logger.warning("Found more VIIRS granules than expected")

        self.add_granules(granules)

        return granules[0]

    def granule(self, date_UTC: date, tile: str):
        if isinstance(date_UTC, str):
            date_UTC = parser.parse(date_UTC).date()

        logger.info(f"searching {self.PRODUCT_SHORT_NAME} tile {tile} date {date_UTC}")
        granule = self.search(date_UTC=date_UTC, tile=tile)

        if granule is None:
            raise VIIRSUnavailableError(f"{self.PRODUCT_SHORT_NAME} URL not available at tile {tile} on date {date_UTC}")

        output_path = self.download_granules([granule])[0]

        return self.GRANULE_CLASS(filename=output_path, products_directory=self.products_directory)
