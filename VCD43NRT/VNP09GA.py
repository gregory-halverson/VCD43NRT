import logging
import os
import warnings
from dataclasses import dataclass
from datetime import datetime, date
from os import remove
from os.path import exists, join, abspath, expanduser
import re
from pathlib import Path
import tempfile
from typing import Dict, List, Optional, Sequence, Tuple, Union

import earthaccess
import h5py
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap
from dateutil import parser
from skimage.transform import resize

import colored_logging as cl
import rasters
from rasters import Raster, RasterGrid, RasterGeometry, Point, Polygon
from modland import generate_modland_grid, parsehv

from .constants import *

try:
    from .login import login
    from .daterange import get_date
    from .exceptions import *
except ImportError:  # pragma: no cover - legacy direct-import fallback
    from login import login
    from daterange import get_date
    from exceptions import *


class VIIRSGranule:
    CLOUD_DATASET_NAME = "HDFEOS/GRIDS/VNP_Grid_1km_2D/Data Fields/SurfReflect_QF1_1"

    def __init__(self, filename: str, working_directory: str = None, products_directory: str = None):
        if working_directory is None:
            working_directory = WORKING_DIRECTORY

        working_directory = abspath(expanduser(working_directory))

        if products_directory is None:
            products_directory = join(working_directory, "VIIRS_products")

        products_directory = abspath(expanduser(products_directory))

        self._filename = abspath(filename)
        self._cloud_mask = None
        self.working_directory = working_directory
        self.products_directory = products_directory

    def __repr__(self):
        return f"VIIRSGranule(filename={self.filename!r}, products_directory={self.products_directory!r})"

    @property
    def filename(self):
        return self._filename

    @property
    def filename_base(self):
        return Path(self.filename).name

    @property
    def filename_stem(self) -> str:
        return Path(self.filename).stem

    @property
    def tile(self):
        return self.filename_base.split(".")[2]

    @property
    def hv(self):
        return parsehv(self.tile)

    @property
    def h(self):
        return self.hv[0]

    @property
    def v(self):
        return self.hv[1]

    @property
    def date_UTC(self):
        return datetime.strptime(self.filename_base.split(".")[1][1:], "%Y%j")

    def product_directory(self, product):
        if self.products_directory is None:
            return None
        return join(self.products_directory, product, f"{self.date_UTC:%Y.%m.%d}")

    def product_filename(self, product: str) -> str:
        if self.product_directory(product) is None:
            raise ValueError("no product directory given")
        return join(self.product_directory(product), f"{self.filename_stem}_{product}.tif")

NDVI_COLORMAP = LinearSegmentedColormap.from_list(
    name="NDVI",
    colors=[
        "#0000ff",
        "#000000",
        "#745d1a",
        "#e1dea2",
        "#45ff01",
        "#325e32"
    ]
)

ALBEDO_COLORMAP = "gray"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VIIRSMissionConfig:
    key: str
    display_name: str
    short_name: str
    product_prefix: str
    concept_id_env: str
    default_concept_id: Optional[str] = None

    @property
    def concept_id(self) -> Optional[str]:
        override = os.environ.get(self.concept_id_env)
        if override:
            return override
        return self.default_concept_id


VIIRS_MISSIONS: Dict[str, VIIRSMissionConfig] = {
    "SNPP": VIIRSMissionConfig(
        key="SNPP",
        display_name="Suomi NPP",
        short_name="VNP09GA",
        product_prefix="VNP",
        concept_id_env="VCD43NRT_VNP09GA_CONCEPT_ID",
        default_concept_id="C2631841556-LPCLOUD",
    ),
    "NOAA20": VIIRSMissionConfig(
        key="NOAA20",
        display_name="NOAA-20 (JPSS-1)",
        short_name="JNP09GA",
        product_prefix="JNP",
        concept_id_env="VCD43NRT_JNP09GA_CONCEPT_ID",
    ),
    "NOAA21": VIIRSMissionConfig(
        key="NOAA21",
        display_name="NOAA-21 (JPSS-2)",
        short_name="SNP09GA",
        product_prefix="SNP",
        concept_id_env="VCD43NRT_SNP09GA_CONCEPT_ID",
    ),
}

DEFAULT_MISSION = "SNPP"
DEFAULT_MISSION_PRIORITY: Tuple[str, ...] = ("SNPP", "NOAA20", "NOAA21")

# Accept aliases so callers can use common mission labels.
MISSION_ALIASES = {
    "SNPP": "SNPP",
    "SUOMI": "SNPP",
    "SUOMINPP": "SNPP",
    "NPP": "SNPP",
    "NOAA20": "NOAA20",
    "NOAA-20": "NOAA20",
    "JPSS1": "NOAA20",
    "JPSS-1": "NOAA20",
    "N20": "NOAA20",
    "NOAA21": "NOAA21",
    "NOAA-21": "NOAA21",
    "JPSS2": "NOAA21",
    "JPSS-2": "NOAA21",
    "N21": "NOAA21",
}

VIIRS_FILENAME_REGEX = re.compile(r"^(VNP|JNP|SNP)09GA\.[^.]+\.([^.]+)\.\d{3}\.\d+\.h5$")
PREFIX_TO_MISSION = {
    "VNP": "SNPP",
    "JNP": "NOAA20",
    "SNP": "NOAA21",
}


def normalize_mission(mission: str) -> str:
    if mission is None:
        return DEFAULT_MISSION

    normalized = str(mission).strip().upper()
    normalized = MISSION_ALIASES.get(normalized, normalized)

    if normalized not in VIIRS_MISSIONS:
        allowed = ", ".join(sorted(VIIRS_MISSIONS))
        raise ValueError(f"Unknown VIIRS mission '{mission}'. Allowed values: {allowed}")

    return normalized


def normalize_mission_priority(
        mission_priority: Optional[Sequence[str]],
        mission: str,
        fallback_mode: bool) -> Tuple[str, ...]:
    if mission_priority is None:
        if fallback_mode:
            return DEFAULT_MISSION_PRIORITY
        return (mission,)

    if isinstance(mission_priority, str):
        mission_priority = [entry.strip() for entry in mission_priority.split(",") if entry.strip()]

    normalized = tuple(normalize_mission(entry) for entry in mission_priority)
    if len(normalized) == 0:
        raise ValueError("mission_priority cannot be empty")

    # Deduplicate while preserving order.
    ordered_unique = tuple(dict.fromkeys(normalized))

    return ordered_unique

class VIIRSUnavailableError(Exception):
    pass


class VNP09GAGranule(VIIRSGranule):
    CLOUD_DATASET_NAME = "HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SurfReflect_QF1_1"

    def get_cloud_mask(self, target_shape: tuple = None) -> Raster:
        h, v = self.hv

        if self._cloud_mask is None:
            with h5py.File(self.filename, "r") as f:
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore")
                    QF1 = np.array(f[self.CLOUD_DATASET_NAME])

                cloud_levels = (QF1 >> 2) & 3
                cloud_mask = cloud_levels > 0
                self._cloud_mask = cloud_mask
        else:
            cloud_mask = self._cloud_mask

        if target_shape is not None:
            cloud_mask = resize(cloud_mask, target_shape, order=0).astype(bool)
            shape = target_shape
        else:
            shape = cloud_mask.shape

        geometry = generate_modland_grid(h, v, shape[0])
        cloud_mask = Raster(cloud_mask, geometry=geometry)

        return cloud_mask

    cloud_mask = property(get_cloud_mask)

    def dataset(
            self,
            filename: str,
            dataset_name: str,
            scale_factor: float,
            cloud_mask: Raster = None,
            apply_cloud_mask: bool = True,
            geometry: RasterGeometry = None,
            resampling: str = None) -> Raster:

        with h5py.File(filename, "r") as f:
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore")
                dataset = f[dataset_name]
                DN = np.array(dataset)

            if "_FillValue" in dataset.attrs:
                fill_value = dataset.attrs["_FillValue"]
            else:
                fill_value = dataset.attrs["_Fillvalue"]

            h, v = self.hv
            grid = generate_modland_grid(h, v, DN.shape[0])
            logger.info(f"opening VIIRS file: {cl.file(self.filename)}")
            logger.info(f"loading {cl.val(dataset_name)} at {cl.val(f'{grid.cell_size:0.2f} m')} resolution")
            DN = np.where(DN == fill_value, np.nan, DN)
            DN = Raster(DN, geometry=grid)

        data = DN * scale_factor

        if apply_cloud_mask:
            if cloud_mask is None:
                cloud_mask = self.get_cloud_mask(target_shape=DN.shape)

            data = rasters.where(cloud_mask, np.nan, data)

        if geometry is not None:
            data = data.to_geometry(geometry, resampling=resampling)

        return data

    @property
    def geometry_M(self) -> RasterGrid:
        return generate_modland_grid(*self.hv, 1200)

    @property
    def geometry_I(self) -> RasterGrid:
        return generate_modland_grid(*self.hv, 2400)

    def geometry(self, band: str) -> RasterGrid:
        try:
            band_letter = band[0]
        except Exception as e:
            raise ValueError(f"invalid band: {band}")

        if band_letter == "I":
            return self.geometry_I
        elif band_letter == "M":
            return self.geometry_M
        else:
            raise ValueError(f"invalid band: {band}")

    def get_sensor_zenith_M(
            self,
            geometry: RasterGeometry = None,
            save_data: bool = False,
            save_preview: bool = False,
            product_filename: str = None) -> Raster:
        if product_filename is None:
            product_filename = self.product_filename(f"sensor_zenith_M")

        image = None

        if product_filename is not None and exists(product_filename):
            try:
                logger.info(f"loading VIIRS sensor zenith: {cl.file(product_filename)}")
                image = Raster.open(product_filename)
            except Exception as e:
                logger.exception(e)
                logger.warning(f"removing corrupted file: {product_filename}")
                remove(product_filename)
                image = None

        if image is None:
            image = self.dataset(
                self.filename,
                f"HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SensorZenith_1",
                0.01,
                cloud_mask=None,
                apply_cloud_mask=False
            )

        if np.all(np.isnan(image)):
            raise ValueError("blank sensor zenith image")

        if save_data and not exists(product_filename):
            logger.info(f"writing VIIRS M-band sensor zenith: {cl.file(product_filename)} {cl.val(image.shape)}")
            image.to_geotiff(product_filename)

            if save_preview:
                image.percentilecut.to_geojpeg(product_filename.replace(".tif", ".jpeg"), quality=20, remove_XML=True)

        if geometry is not None:
            image = image.to_geometry(geometry)

        return image

    sensor_zenith_M = property(get_sensor_zenith_M)

    def get_sensor_zenith_I(
            self,
            geometry: RasterGeometry = None,
            save_data: bool = False,
            save_preview: bool = False,
            product_filename: str = None) -> Raster:
        if product_filename is None:
            product_filename = self.product_filename(f"sensor_zenith_I")

        image = None

        if product_filename is not None and exists(product_filename):
            try:
                logger.info(f"loading VIIRS I-band sensor zenith: {cl.file(product_filename)}")
                image = Raster.open(product_filename)
            except Exception as e:
                logger.exception(e)
                logger.warning(f"removing corrupted file: {product_filename}")
                remove(product_filename)
                image = None

        if image is None:
            h, v = self.hv
            grid_I = generate_modland_grid(h, v, 2400)

            image = self.dataset(
                self.filename,
                f"HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SensorZenith_1",
                0.01,
                cloud_mask=None,
                apply_cloud_mask=False,
                geometry=grid_I,
                resampling="cubic"
            )

        if np.all(np.isnan(image)):
            raise ValueError("blank sensor zenith image")

        if save_data and not exists(product_filename):
            logger.info(f"writing VIIRS sensor zenith: {cl.file(product_filename)} {cl.val(image.shape)}")
            image.to_geotiff(product_filename)

            if save_preview:
                image.percentilecut.to_geojpeg(product_filename.replace(".tif", ".jpeg"), quality=20, remove_XML=True)

        if geometry is not None:
            image = image.to_geometry(geometry)

        return image

    sensor_zenith_I = property(get_sensor_zenith_I)

    def sensor_zenith(
            self,
            band: str,
            geometry: RasterGeometry = None,
            save_data: bool = False,
            save_preview: bool = False,
            product_filename: str = None) -> Raster:
        try:
            band_letter = band[0]
        except Exception as e:
            raise ValueError(f"invalid band: {band}")

        if band_letter == "I":
            return self.get_sensor_zenith_I(
                geometry=geometry,
                save_data=save_data,
                save_preview=save_preview,
                product_filename=product_filename
            )
        elif band_letter == "M":
            return self.get_sensor_zenith_M(
                geometry=geometry,
                save_data=save_data,
                save_preview=save_preview,
                product_filename=product_filename
            )
        else:
            raise ValueError(f"invalid band: {band}")

    def get_sensor_azimuth_M(
            self,
            geometry: RasterGeometry = None,
            save_data: bool = False,
            save_preview: bool = False,
            product_filename: str = None) -> Raster:
        if product_filename is None:
            product_filename = self.product_filename(f"sensor_azimuth_M")

        image = None

        if product_filename is not None and exists(product_filename):
            try:
                logger.info(f"loading VIIRS M-band sensor azimuth: {cl.file(product_filename)}")
                image = Raster.open(product_filename)
            except Exception as e:
                logger.exception(e)
                logger.warning(f"removing corrupted file: {product_filename}")
                remove(product_filename)
                image = None

        if image is None:
            image = self.dataset(
                self.filename,
                f"HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SensorAzimuth_1",
                0.01,
                cloud_mask=None,
                apply_cloud_mask=False
            )

        if np.all(np.isnan(image)):
            raise ValueError("blank sensor azimuth image")

        if save_data and not exists(product_filename):
            logger.info(f"writing VIIRS sensor azimuth: {cl.file(product_filename)} {cl.val(image.shape)}")
            image.to_geotiff(product_filename)

            if save_preview:
                image.percentilecut.to_geojpeg(product_filename.replace(".tif", ".jpeg"), quality=20, remove_XML=True)

        if geometry is not None:
            image = image.to_geometry(geometry)

        return image

    sensor_azimuth_M = property(get_sensor_azimuth_M)

    def get_sensor_azimuth_I(
            self,
            geometry: RasterGeometry = None,
            save_data: bool = False,
            save_preview: bool = False,
            product_filename: str = None) -> Raster:
        if product_filename is None:
            product_filename = self.product_filename(f"sensor_azimuth_I")

        image = None

        if product_filename is not None and exists(product_filename):
            try:
                logger.info(f"loading VIIRS I-band sensor azimuth: {cl.file(product_filename)}")
                image = Raster.open(product_filename)
            except Exception as e:
                logger.exception(e)
                logger.warning(f"removing corrupted file: {product_filename}")
                remove(product_filename)
                image = None

        if image is None:
            h, v = self.hv
            grid_I = generate_modland_grid(h, v, 2400)

            image = self.dataset(
                self.filename,
                f"HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SensorAzimuth_1",
                0.01,
                cloud_mask=None,
                apply_cloud_mask=False,
                geometry=grid_I,
                resampling="cubic"
            )

        if np.all(np.isnan(image)):
            raise ValueError("blank sensor azimuth image")

        if save_data and not exists(product_filename):
            logger.info(f"writing VIIRS sensor azimuth: {cl.file(product_filename)} {cl.val(image.shape)}")
            image.to_geotiff(product_filename)

            if save_preview:
                image.percentilecut.to_geojpeg(product_filename.replace(".tif", ".jpeg"), quality=20, remove_XML=True)

        if geometry is not None:
            image = image.to_geometry(geometry)

        return image

    sensor_azimuth_I = property(get_sensor_azimuth_I)

    def sensor_azimuth(
            self,
            band: str,
            geometry: RasterGeometry = None,
            save_data: bool = False,
            save_preview: bool = False,
            product_filename: str = None) -> Raster:
        try:
            band_letter = band[0]
        except Exception as e:
            raise ValueError(f"invalid band: {band}")

        if band_letter == "I":
            return self.get_sensor_azimuth_I(
                geometry=geometry,
                save_data=save_data,
                save_preview=save_preview,
                product_filename=product_filename
            )
        elif band_letter == "M":
            return self.get_sensor_azimuth_M(
                geometry=geometry,
                save_data=save_data,
                save_preview=save_preview,
                product_filename=product_filename
            )
        else:
            raise ValueError(f"invalid band: {band}")

    def get_solar_zenith_M(
            self,
            geometry: RasterGeometry = None,
            save_data: bool = False,
            save_preview: bool = False,
            product_filename: str = None) -> Raster:
        if product_filename is None:
            product_filename = self.product_filename(f"solar_zenith_M")

        image = None

        if product_filename is not None and exists(product_filename):
            try:
                logger.info(f"loading VIIRS M-band solar zenith: {cl.file(product_filename)}")
                image = Raster.open(product_filename)
            except Exception as e:
                logger.exception(e)
                logger.warning(f"removing corrupted file: {product_filename}")
                remove(product_filename)
                image = None

        if image is None:
            image = self.dataset(
                self.filename,
                f"HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SolarZenith_1",
                0.01,
                cloud_mask=None,
                apply_cloud_mask=False
            )

        if np.all(np.isnan(image)):
            raise ValueError("blank solar zenith image")

        if save_data and not exists(product_filename):
            logger.info(f"writing VIIRS solar zenith: {cl.file(product_filename)} {cl.val(image.shape)}")
            image.to_geotiff(product_filename)

            if save_preview:
                image.percentilecut.to_geojpeg(product_filename.replace(".tif", ".jpeg"), quality=20, remove_XML=True)

        if geometry is not None:
            image = image.to_geometry(geometry)

        return image

    solar_zenith_M = property(get_solar_zenith_M)

    def get_solar_zenith_I(
            self,
            geometry: RasterGeometry = None,
            save_data: bool = False,
            save_preview: bool = False,
            product_filename: str = None) -> Raster:
        if product_filename is None:
            product_filename = self.product_filename("solar_zenith_I")

        image = None

        if product_filename is not None and exists(product_filename):
            try:
                logger.info(f"loading VIIRS I-band solar zenith: {cl.file(product_filename)}")
                image = Raster.open(product_filename)
            except Exception as e:
                logger.exception(e)
                logger.warning(f"removing corrupted file: {product_filename}")
                remove(product_filename)
                image = None

        if image is None:
            h, v = self.hv
            grid_I = generate_modland_grid(h, v, 2400)

            image = self.dataset(
                self.filename,
                f"HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SolarZenith_1",
                0.01,
                cloud_mask=None,
                apply_cloud_mask=False,
                geometry=grid_I,
                resampling="cubic"
            )

        if np.all(np.isnan(image)):
            raise ValueError("blank solar zenith image")

        if save_data and not exists(product_filename):
            logger.info(f"writing VIIRS solar zenith: {cl.file(product_filename)} {cl.val(image.shape)}")

            image.to_geotiff(product_filename)

            if save_preview:
                image.percentilecut.to_geojpeg(product_filename.replace(".tif", ".jpeg"), quality=20, remove_XML=True)

        if geometry is not None:
            image = image.to_geometry(geometry)

        return image

    solar_zenith_I = property(get_solar_zenith_I)

    def solar_zenith(
            self,
            band: str,
            geometry: RasterGeometry = None,
            save_data: bool = False,
            save_preview: bool = False,
            product_filename: str = None) -> Raster:
        try:
            band_letter = band[0]
        except Exception as e:
            raise ValueError(f"invalid band: {band}")

        if band_letter == "I":
            return self.get_solar_zenith_I(
                geometry=geometry,
                save_data=save_data,
                save_preview=save_preview,
                product_filename=product_filename
            )
        elif band_letter == "M":
            return self.get_solar_zenith_M(
                geometry=geometry,
                save_data=save_data,
                save_preview=save_preview,
                product_filename=product_filename
            )
        else:
            raise ValueError(f"invalid band: {band}")

    def get_solar_azimuth_M(
            self,
            geometry: RasterGeometry = None,
            save_data: bool = False,
            save_preview: bool = False,
            product_filename: str = None) -> Raster:
        if product_filename is None:
            product_filename = self.product_filename("solar_azimuth_M")

        image = None

        if product_filename is not None and exists(product_filename):
            try:
                logger.info(f"loading VIIRS M-band solar azimuth: {cl.file(product_filename)}")
                image = Raster.open(product_filename)
            except Exception as e:
                logger.exception(e)
                logger.warning(f"removing corrupted file: {product_filename}")
                remove(product_filename)
                image = None

        if image is None:
            image = self.dataset(
                self.filename,
                f"HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SolarAzimuth_1",
                0.01,
                cloud_mask=None,
                apply_cloud_mask=False
            )

        if np.all(np.isnan(image)):
            raise ValueError("blank solar azimuth image")

        if save_data and not exists(product_filename):
            logger.info(f"writing VIIRS solar azimuth: {cl.file(product_filename)} {cl.val(image.shape)}")
            image.to_geotiff(product_filename)

            if save_preview:
                image.percentilecut.to_geojpeg(product_filename.replace(".tif", ".jpeg"), quality=20, remove_XML=True)

        if geometry is not None:
            image = image.to_geometry(geometry)

        return image

    solar_azimuth_M = property(get_solar_azimuth_M)

    def get_solar_azimuth_I(
            self,
            geometry: RasterGeometry = None,
            save_data: bool = False,
            save_preview: bool = False,
            product_filename: str = None) -> Raster:
        if product_filename is None:
            product_filename = self.product_filename("solar_azimuth_I")

        image = None

        if product_filename is not None and exists(product_filename):
            try:
                logger.info(f"loading VIIRS I-band solar azimuth: {cl.file(product_filename)}")
                image = Raster.open(product_filename)
            except Exception as e:
                logger.exception(e)
                logger.warning(f"removing corrupted file: {product_filename}")
                remove(product_filename)
                image = None

        if image is None:
            h, v = self.hv
            grid_I = generate_modland_grid(h, v, 2400)

            image = self.dataset(
                self.filename,
                f"HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SolarAzimuth_1",
                0.01,
                cloud_mask=None,
                apply_cloud_mask=False,
                geometry=grid_I,
                resampling="cubic"
            )

        if np.all(np.isnan(image)):
            raise ValueError("blank solar azimuth image")

        if save_data and not exists(product_filename):
            logger.info(f"writing VIIRS solar azimuth: {cl.file(product_filename)} {cl.val(image.shape)}")
            image.to_geotiff(product_filename)

            if save_preview:
                image.percentilecut.to_geojpeg(product_filename.replace(".tif", ".jpeg"), quality=20, remove_XML=True)

        if geometry is not None:
            image = image.to_geometry(geometry)

        return image

    solar_azimuth_I = property(get_solar_azimuth_I)

    def solar_azimuth(
            self,
            band: str,
            geometry: RasterGeometry = None,
            save_data: bool = False,
            save_preview: bool = False,
            product_filename: str = None) -> Raster:
        try:
            band_letter = band[0]
        except Exception as e:
            raise ValueError(f"invalid band: {band}")

        if band_letter == "I":
            return self.get_solar_azimuth_I(
                geometry=geometry,
                save_data=save_data,
                save_preview=save_preview,
                product_filename=product_filename
            )
        elif band_letter == "M":
            return self.get_solar_azimuth_M(
                geometry=geometry,
                save_data=save_data,
                save_preview=save_preview,
                product_filename=product_filename
            )
        else:
            raise ValueError(f"invalid band: {band}")

    def get_M_band(
            self,
            band: int,
            cloud_mask: Raster = None,
            apply_cloud_mask: bool = True,
            geometry: RasterGeometry = None,
            save_data: bool = False,
            save_preview: bool = False,
            product_filename: str = None) -> Raster:
        if product_filename is None:
            product_filename = self.product_filename(f"M{band}")

        image = None

        if product_filename is not None and exists(product_filename):
            try:
                logger.info(f"loading VIIRS M-band {band} surface reflectance: {cl.file(product_filename)}")
                image = Raster.open(product_filename)
            except Exception as e:
                logger.exception(e)
                logger.warning(f"removing corrupted file: {product_filename}")
                remove(product_filename)
                image = None

        if image is None:
            image = self.dataset(
                self.filename,
                f"HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SurfReflect_M{int(band)}_1",
                0.0001,
                cloud_mask=cloud_mask,
                apply_cloud_mask=apply_cloud_mask
            )

        if save_data and not exists(product_filename):
            logger.info(f"writing VIIRS M{band}: {cl.file(product_filename)}")
            image.to_geotiff(product_filename)

            if save_preview:
                image.percentilecut.to_geojpeg(product_filename.replace(".tif", ".jpeg"), quality=20, remove_XML=True)

        if geometry is not None:
            image = image.to_geometry(geometry)

        return image

    def get_I_band(
            self,
            band: int,
            cloud_mask: Raster = None,
            apply_cloud_mask: bool = True,
            geometry: RasterGeometry = None,
            save_data: bool = False,
            save_preview: bool = False,
            product_filename: str = None) -> Raster:
        if product_filename is None:
            product_filename = self.product_filename(f"I{band}")

        image = None

        if product_filename is not None and exists(product_filename):
            try:
                logger.info(f"loading VIIRS I-band {band} surface reflectance: {cl.file(product_filename)}")
                image = Raster.open(product_filename)
            except Exception as e:
                logger.exception(e)
                logger.warning(f"removing corrupted file: {product_filename}")
                remove(product_filename)
                image = None

        if image is None:
            image = self.dataset(
                self.filename,
                f"HDFEOS/GRIDS/VIIRS_Grid_500m_2D/Data Fields/SurfReflect_I{int(band)}_1",
                0.0001,
                cloud_mask=cloud_mask,
                apply_cloud_mask=apply_cloud_mask
            )

        if save_data and not exists(product_filename):
            logger.info(f"writing VIIRS I{band}: {cl.file(product_filename)}")
            image.to_geotiff(product_filename)

            if save_preview:
                image.percentilecut.to_geojpeg(product_filename.replace(".tif", ".jpeg"), quality=20, remove_XML=True)

        if geometry is not None:
            image = image.to_geometry(geometry)

        return image

    def band(
            self,
            band: str,
            cloud_mask: Raster = None,
            apply_cloud_mask: bool = True,
            geometry: RasterGeometry = None,
            save_data: bool = False,
            save_preview: bool = False,
            product_filename: str = None) -> Raster:
        try:
            band_letter = band[0]
            band_number = int(band[1:])
        except Exception as e:
            raise ValueError(f"invalid band: {band}")

        if band_letter == "I":
            return self.get_I_band(
                band=band_number,
                cloud_mask=cloud_mask,
                apply_cloud_mask=apply_cloud_mask,
                geometry=geometry,
                save_data=save_data,
                save_preview=save_preview,
                product_filename=product_filename
            )
        elif band_letter == "M":
            return self.get_M_band(
                band=band_number,
                cloud_mask=cloud_mask,
                apply_cloud_mask=apply_cloud_mask,
                geometry=geometry,
                save_data=save_data,
                save_preview=save_preview,
                product_filename=product_filename
            )
        else:
            raise ValueError(f"invalid band: {band}")

    def get_red(
            self,
            cloud_mask: Raster = None,
            apply_cloud_mask: bool = True,
            geometry: RasterGeometry = None,
            save_data: bool = False,
            save_preview: bool = False,
            product_filename: str = None) -> Raster:
        return self.get_I_band(
            band=1,
            cloud_mask=cloud_mask,
            apply_cloud_mask=apply_cloud_mask,
            geometry=geometry,
            save_data=save_data,
            save_preview=save_preview,
            product_filename=product_filename
        )

    red = property(get_red)

    def get_NIR(
            self,
            cloud_mask: Raster = None,
            apply_cloud_mask: bool = True,
            geometry: RasterGeometry = None,
            save_data: bool = False,
            save_preview: bool = False,
            product_filename: str = None) -> Raster:
        return self.get_I_band(
            band=2,
            cloud_mask=cloud_mask,
            apply_cloud_mask=apply_cloud_mask,
            geometry=geometry,
            save_data=save_data,
            save_preview=save_preview,
            product_filename=product_filename
        )

    NIR = property(get_NIR)

    def get_NDVI(
            self,
            cloud_mask: Raster = None,
            apply_cloud_mask: bool = True,
            geometry: RasterGeometry = None,
            save_data: bool = False,
            save_preview: bool = False,
            product_filename: str = None) -> Raster:
        if product_filename is None:
            product_filename = self.product_filename("NDVI")

        if product_filename is not None and exists(product_filename):
            logger.info(f"loading VIIRS NDVI: {cl.file(product_filename)}")
            NDVI = Raster.open(product_filename)
        else:
            red = self.get_red(
                cloud_mask=cloud_mask,
                apply_cloud_mask=apply_cloud_mask,
                geometry=geometry,
                save_data=save_data,
                save_preview=save_preview
            )

            NIR = self.get_NIR(
                cloud_mask=cloud_mask,
                apply_cloud_mask=apply_cloud_mask,
                geometry=geometry,
                save_data=save_data,
                save_preview=save_preview
            )

            NDVI = np.clip((NIR - red) / (NIR + red), -1, 1)

        if save_data and not exists(product_filename):
            logger.info(f"writing VIIRS NDVI: {cl.file(product_filename)}")
            NDVI.to_geotiff(product_filename)

            if save_preview:
                NDVI.percentilecut.to_geojpeg(product_filename.replace(".tif", ".jpeg"))

        if geometry is not None:
            NDVI = NDVI.to_geometry(geometry)

        NDVI.cmap = NDVI_COLORMAP

        return NDVI

    NDVI = property(get_NDVI)

    def get_albedo(
            self,
            cloud_mask: Raster = None,
            apply_cloud_mask: bool = True,
            geometry: RasterGeometry = None,
            save_data: bool = False,
            save_preview: bool = False,
            product_filename: str = None) -> Raster:
        if product_filename is None:
            product_filename = self.product_filename("albedo")

        if product_filename is not None and exists(product_filename):
            logger.info(f"loading VIIRS albedo: {cl.file(product_filename)}")
            albedo = Raster.open(product_filename)
        else:
            b1 = self.get_M_band(
                1,
                cloud_mask=cloud_mask,
                apply_cloud_mask=apply_cloud_mask,
                geometry=geometry,
                save_data=save_data,
                save_preview=save_preview
            )

            b2 = self.get_M_band(
                2,
                cloud_mask=cloud_mask,
                apply_cloud_mask=apply_cloud_mask,
                geometry=geometry,
                save_data=save_data,
                save_preview=save_preview
            )

            b3 = self.get_M_band(
                3,
                cloud_mask=cloud_mask,
                apply_cloud_mask=apply_cloud_mask,
                geometry=geometry,
                save_data=save_data,
                save_preview=save_preview
            )

            b4 = self.get_M_band(
                4,
                cloud_mask=cloud_mask,
                apply_cloud_mask=apply_cloud_mask,
                geometry=geometry,
                save_data=save_data,
                save_preview=save_preview
            )

            b5 = self.get_M_band(
                5,
                cloud_mask=cloud_mask,
                apply_cloud_mask=apply_cloud_mask,
                geometry=geometry,
                save_data=save_data,
                save_preview=save_preview
            )

            b7 = self.get_M_band(
                7,
                cloud_mask=cloud_mask,
                apply_cloud_mask=apply_cloud_mask,
                geometry=geometry,
                save_data=save_data,
                save_preview=save_preview
            )

            b8 = self.get_M_band(
                8,
                cloud_mask=cloud_mask,
                apply_cloud_mask=apply_cloud_mask,
                geometry=geometry,
                save_data=save_data,
                save_preview=save_preview
            )

            b10 = self.get_M_band(
                10,
                cloud_mask=cloud_mask,
                apply_cloud_mask=apply_cloud_mask,
                geometry=geometry,
                save_data=save_data,
                save_preview=save_preview
            )

            b11 = self.get_M_band(
                11,
                cloud_mask=cloud_mask,
                apply_cloud_mask=apply_cloud_mask,
                geometry=geometry,
                save_data=save_data,
                save_preview=save_preview
            )

            # https://lpdaac.usgs.gov/documents/194/VNP43_ATBD_V1.pdf
            albedo = 0.2418 * b1 \
                     - 0.201 * b2 \
                     + 0.2093 * b3 \
                     + 0.1146 * b4 \
                     + 0.1348 * b5 \
                     + 0.2251 * b7 \
                     + 0.1123 * b8 \
                     + 0.0860 * b10 \
                     + 0.0803 * b11 \
                     - 0.0131

            albedo = np.clip(albedo, 0, 1)

        if save_data and not exists(product_filename):
            logger.info(f"writing VIIRS albedo: {cl.file(product_filename)}")
            albedo.to_geotiff(product_filename)

        if geometry is not None:
            logger.info(f"projecting VIIRS albedo from {cl.val(albedo.geometry.cell_size)} to {cl.val(geometry.cell_size)}")
            albedo = albedo.to_geometry(geometry)

        albedo.cmap = ALBEDO_COLORMAP

        return albedo

    albedo = property(get_albedo)


def earliest_datetime(date_in: Union[date, str]) -> datetime:
    if isinstance(date_in, str):
        datetime_in = parser.parse(date_in)
    else:
        datetime_in = date_in

    date_string = datetime_in.strftime("%Y-%m-%d")
    return parser.parse(f"{date_string}T00:00:00Z")


def latest_datetime(date_in: Union[date, str]) -> datetime:
    if isinstance(date_in, str):
        datetime_in = parser.parse(date_in)
    else:
        datetime_in = date_in

    date_string = datetime_in.strftime("%Y-%m-%d")
    return parser.parse(f"{date_string}T23:59:59Z")


def modland_tile_from_filename(filename: str) -> str:
    match = VIIRS_FILENAME_REGEX.match(filename)
    if match is None:
        raise RuntimeError(f"Invalid filename found through VIIRS CMR search: {filename}")

    return match.group(2)


def mission_from_filename(filename: str) -> str:
    match = VIIRS_FILENAME_REGEX.match(filename)
    if match is None:
        raise RuntimeError(f"Invalid filename found through VIIRS CMR search: {filename}")

    mission = PREFIX_TO_MISSION.get(match.group(1))
    if mission is None:
        raise RuntimeError(f"Unsupported VIIRS mission prefix in filename: {filename}")

    return mission


# TODO: Deduplicate between VIIRS and HLS
def VIIRS_CMR_query(
        start_date: Union[date, str],
        end_date: Union[date, str],
        mission: str,
        target_geometry: Point or Polygon or RasterGeometry = None,
        tile: str = None,
) -> List[earthaccess.search.DataGranule]:
    """function to search for VIIRS at tile in date range"""
    mission_key = normalize_mission(mission)
    mission_config = VIIRS_MISSIONS[mission_key]

    query = earthaccess.granule_query() \
        .temporal(earliest_datetime(start_date), latest_datetime(end_date))

    concept_id = mission_config.concept_id
    if concept_id:
        query = query.concept_id(concept_id)
    else:
        query = query.short_name(mission_config.short_name)

    if isinstance(target_geometry, Point):
        query = query.point(target_geometry.x, target_geometry.y)
    if isinstance(target_geometry, Polygon):
        ring = target_geometry.exterior
        if not ring.is_ccw:
            ring = ring.reverse()
        coordinates = ring.coords
        query = query.polygon(coordinates)
    if isinstance(target_geometry, RasterGeometry):
        ring = target_geometry.corner_polygon_latlon.exterior
        if not ring.is_ccw:
            ring = ring.reverse()
        coordinates = ring.coords
        query = query.polygon(coordinates)
    if tile is not None:
        query = query.readable_granule_name(f"*.{tile}.*")

    granules: List[earthaccess.search.DataGranule]
    try:
        granules = query.get()
    except Exception as e:
        raise CMRServerUnreachable(e)
    granules = sorted(granules, key=lambda granule: granule["umm"]["TemporalExtent"]["RangeDateTime"]["BeginningDateTime"])

    logger.info(f"Found the following granules for {mission_config.display_name} using the CMR search:")
    for granule in granules:
        logger.info("  " + cl.file(granule["meta"]["native-id"]))
    logger.info(f"Number of {mission_config.display_name} granules found using CMR search: {len(granules)}")

    return granules


class VNP09GA:
    DEFAULT_WORKING_DIRECTORY = "."
    DEFAULT_DOWNLOAD_DIRECTORY = "VNP09GA_download"
    DEFAULT_PRODUCTS_DIRECTORY = "VNP09GA_products"
    DEFAULT_MOSAIC_DIRECTORY = "VNP09GA_mosaics"
    DEFAULT_RESAMPLING = "nearest"

    CLOUD_DATASET_NAME = "HDFEOS/GRIDS/VNP_Grid_1km_2D/Data Fields/SurfReflect_QF1_1"

    def __init__(
            self,
            working_directory: str = None,
            download_directory: str = None,
            products_directory: str = None,
            mosaic_directory: str = None,
            mission: str = DEFAULT_MISSION,
            mission_priority: Optional[Sequence[str]] = None,
            fallback_mode: bool = False,
            resampling: str = None):

        if resampling is None:
            resampling = self.DEFAULT_RESAMPLING

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
        date_UTC = get_date(granule["umm"]["TemporalExtent"]["RangeDateTime"]["BeginningDateTime"])

        return join(
            self.download_directory,
            mission_cfg.short_name,
            f"{date_UTC:%Y.%m.%d}",
            filename,
        )

    def _search_for_mission(
            self,
            mission: str,
            start_date: Union[date, str],
            end_date: Union[date, str],
            geometry: Point or Polygon or RasterGeometry = None,
            tile: str = None) -> List[earthaccess.search.DataGranule]:
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
            tile: str = None) -> List[earthaccess.search.DataGranule]:
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
                # First mission in priority wins for a date/tile key.
                if key not in granules_by_key:
                    granules_by_key[key] = granule

        return sorted(
            granules_by_key.values(),
            key=lambda granule: granule["umm"]["TemporalExtent"]["RangeDateTime"]["BeginningDateTime"],
        )

    def add_granules(self, granules: List[earthaccess.search.DataGranule]):
        data = pd.DataFrame([
            {
                "date_UTC": get_date(granule["umm"]["TemporalExtent"]["RangeDateTime"]["BeginningDateTime"]),
                "tile": modland_tile_from_filename(self._granule_filename(granule)),
                "mission": mission_from_filename(self._granule_filename(granule)),
                "granule": granule,
            }
            for granule in granules
        ])

        if len(data) == 0:
            return

        priority_index = {mission: idx for idx, mission in enumerate(self.active_missions)}

        combined = pd.concat([self._granules, data], ignore_index=True)
        combined["priority"] = combined["mission"].map(lambda value: priority_index.get(value, len(priority_index)))
        combined = combined.sort_values(by=["date_UTC", "tile", "priority"])
        self._granules = combined.drop_duplicates(subset=["date_UTC", "tile"], keep="first").drop(columns=["priority"])

    def download_granules(self, granules: List[earthaccess.search.DataGranule]) -> List[str]:
        # Check if any of the granules have already been downloaded, and if so record the file path for that granule.
        #  Save the granules that haven't been downloaded to download them later.
        granules_to_download = []
        output_paths = []
        for granule in granules:
            output_file_path = self._download_output_path(granule)
            if Path(output_file_path).exists():
                output_paths.append(output_file_path)
            else:
                granules_to_download.append(granule)

        # Early exit
        if len(granules_to_download) == 0:
            logger.info("All VIIRS granules have already been downloaded")
            return output_paths

        # Make sure to remove this before we return, so we use try..finally to avoid exceptions causing issues
        temporary_parent_directory = join(self.download_directory, "tmp")
        os.makedirs(temporary_parent_directory, exist_ok=True)
        temporary_download_directory = tempfile.mkdtemp(dir=temporary_parent_directory)
        
        try:
            last_download_exception = None
            for _ in range(0, RETRIES):
                download_exception = None
                downloaded_granules = []

                file_paths = earthaccess.download(granules_to_download, local_path=temporary_download_directory)

                for (granule, download_file_path) in zip(granules_to_download, file_paths):
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

    def prefetch_VNP09GA(
            self,
            start_date: Union[date, str],
            end_date: Union[date, str],
            geometry: Point or Polygon or RasterGeometry = None):
        # Fetch list of granules to download
        granules = self._search_with_fallback(
            start_date=start_date,
            end_date=end_date,
            geometry=geometry,
        )

        self.add_granules(granules)

        self.download_granules(granules)

    def search(
            self,
            date_UTC: date,
            tile: str) -> Union[earthaccess.search.DataGranule, None]:
        if "date_UTC" not in self._granules.columns:
            raise ValueError(f"date_UTC column not in granules table")

        subset = self._granules[(self._granules['date_UTC'] == date_UTC) & (self._granules['tile'] == tile)]
        if len(subset) > 0:
            return subset.iloc[0].granule

        granules = self._search_with_fallback(
            start_date=date_UTC,
            end_date=date_UTC,
            tile=tile,
        )

        if len(granules) == 0:
            return None

        if len(granules) > 1:
            logger.warning("Found more VIIRS granules than expected")

        self.add_granules(granules)

        return granules[0]

    def granule(
            self,
            date_UTC: date,
            tile: str) -> VNP09GAGranule:
        if isinstance(date_UTC, str):
            date_UTC = parser.parse(date_UTC).date()

        logger.info(f"searching VNP09GA tile {tile} date {date_UTC}")
        granule = self.search(
            date_UTC=date_UTC,
            tile=tile
        )

        if granule is None:
            raise VIIRSUnavailableError(f"VNP09GA URL not available at tile {tile} on date {date_UTC}")

        output_path = self.download_granules([granule])[0]

        output_granule = VNP09GAGranule(
            filename=output_path,
            products_directory=self.products_directory
        )

        return output_granule
