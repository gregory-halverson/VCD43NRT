import logging
import warnings
from datetime import datetime
from os import remove
from os.path import abspath, exists, expanduser, join
from pathlib import Path

import h5py
import numpy as np
from matplotlib.colors import LinearSegmentedColormap
from skimage.transform import resize

import colored_logging as cl
import rasters
from rasters import Raster, RasterGeometry, RasterGrid
from modland import generate_modland_grid, parsehv

from .constants import WORKING_DIRECTORY


logger = logging.getLogger(__name__)

NDVI_COLORMAP = LinearSegmentedColormap.from_list(
    name="NDVI",
    colors=["#0000ff", "#000000", "#745d1a", "#e1dea2", "#45ff01", "#325e32"],
)
ALBEDO_COLORMAP = "gray"


class VIIRSGranule:
    PRODUCT_SHORT_NAME = "VIIRS"
    CLOUD_DATASET_NAME = "HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SurfReflect_QF1_1"

    SENSOR_ZENITH_DATASET = "HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SensorZenith_1"
    SENSOR_AZIMUTH_DATASET = "HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SensorAzimuth_1"
    SOLAR_ZENITH_DATASET = "HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SolarZenith_1"
    SOLAR_AZIMUTH_DATASET = "HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SolarAzimuth_1"

    M_BAND_TEMPLATE = "HDFEOS/GRIDS/VIIRS_Grid_1km_2D/Data Fields/SurfReflect_M{band}_1"
    I_BAND_TEMPLATE = "HDFEOS/GRIDS/VIIRS_Grid_500m_2D/Data Fields/SurfReflect_I{band}_1"

    M_BAND_CELLS = 1200
    I_BAND_CELLS = 2400

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
        return f"{self.__class__.__name__}(filename={self.filename!r}, products_directory={self.products_directory!r})"

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

    def get_cloud_mask(self, target_shape: tuple = None) -> Raster:
        h, v = self.hv

        if self._cloud_mask is None:
            with h5py.File(self.filename, "r") as f:
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore")
                    qf1 = np.array(f[self.CLOUD_DATASET_NAME])

                cloud_levels = (qf1 >> 2) & 3
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
        return Raster(cloud_mask, geometry=geometry)

    cloud_mask = property(get_cloud_mask)

    def dataset(
        self,
        filename: str,
        dataset_name: str,
        scale_factor: float,
        cloud_mask: Raster = None,
        apply_cloud_mask: bool = True,
        geometry: RasterGeometry = None,
        resampling: str = None,
    ) -> Raster:
        with h5py.File(filename, "r") as f:
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore")
                dataset = f[dataset_name]
                dn = np.array(dataset)

            if "_FillValue" in dataset.attrs:
                fill_value = dataset.attrs["_FillValue"]
            else:
                fill_value = dataset.attrs["_Fillvalue"]

            h, v = self.hv
            grid = generate_modland_grid(h, v, dn.shape[0])
            logger.info(f"opening VIIRS file: {cl.file(self.filename)}")
            logger.info(f"loading {cl.val(dataset_name)} at {cl.val(f'{grid.cell_size:0.2f} m')} resolution")
            dn = np.where(dn == fill_value, np.nan, dn)
            dn = Raster(dn, geometry=grid)

        data = dn * scale_factor

        if apply_cloud_mask:
            if cloud_mask is None:
                cloud_mask = self.get_cloud_mask(target_shape=dn.shape)
            data = rasters.where(cloud_mask, np.nan, data)

        if geometry is not None:
            data = data.to_geometry(geometry, resampling=resampling)

        return data

    @property
    def geometry_M(self) -> RasterGrid:
        return generate_modland_grid(*self.hv, self.M_BAND_CELLS)

    @property
    def geometry_I(self) -> RasterGrid:
        return generate_modland_grid(*self.hv, self.I_BAND_CELLS)

    def geometry(self, band: str) -> RasterGrid:
        try:
            band_letter = band[0]
        except Exception:
            raise ValueError(f"invalid band: {band}")

        if band_letter == "I":
            return self.geometry_I
        if band_letter == "M":
            return self.geometry_M
        raise ValueError(f"invalid band: {band}")

    def _load_or_compute_dataset_product(
        self,
        product_name: str,
        dataset_name: str,
        scale_factor: float,
        geometry: RasterGeometry = None,
        save_data: bool = False,
        save_preview: bool = False,
        product_filename: str = None,
        apply_cloud_mask: bool = False,
        cloud_mask: Raster = None,
        resampling: str = None,
        log_name: str = None,
    ) -> Raster:
        if product_filename is None:
            product_filename = self.product_filename(product_name)

        image = None

        if product_filename is not None and exists(product_filename):
            try:
                if log_name is None:
                    log_name = product_name
                logger.info(f"loading {self.PRODUCT_SHORT_NAME} {log_name}: {cl.file(product_filename)}")
                image = Raster.open(product_filename)
            except Exception as e:
                logger.exception(e)
                logger.warning(f"removing corrupted file: {product_filename}")
                remove(product_filename)
                image = None

        if image is None:
            image = self.dataset(
                self.filename,
                dataset_name,
                scale_factor,
                cloud_mask=cloud_mask,
                apply_cloud_mask=apply_cloud_mask,
                geometry=geometry,
                resampling=resampling,
            )

        if np.all(np.isnan(image)):
            raise ValueError(f"blank {product_name} image")

        if save_data and not exists(product_filename):
            logger.info(f"writing {self.PRODUCT_SHORT_NAME} {product_name}: {cl.file(product_filename)} {cl.val(image.shape)}")
            image.to_geotiff(product_filename)

            if save_preview:
                image.percentilecut.to_geojpeg(product_filename.replace(".tif", ".jpeg"), quality=20, remove_XML=True)

        return image

    def _get_angle(
        self,
        product_suffix: str,
        dataset_name: str,
        band_letter: str,
        geometry: RasterGeometry = None,
        save_data: bool = False,
        save_preview: bool = False,
        product_filename: str = None,
    ) -> Raster:
        if band_letter == "I":
            base_geometry = self.geometry_I
            resampling = "cubic"
        elif band_letter == "M":
            base_geometry = None
            resampling = None
        else:
            raise ValueError(f"invalid band: {band_letter}")

        image = self._load_or_compute_dataset_product(
            product_name=f"{product_suffix}_{band_letter}",
            dataset_name=dataset_name,
            scale_factor=0.01,
            geometry=base_geometry,
            save_data=save_data,
            save_preview=save_preview,
            product_filename=product_filename,
            apply_cloud_mask=False,
            resampling=resampling,
            log_name=f"{band_letter}-band {product_suffix.replace('_', ' ')}",
        )

        if geometry is not None:
            image = image.to_geometry(geometry)

        return image

    def get_sensor_zenith_M(self, geometry: RasterGeometry = None, save_data: bool = False, save_preview: bool = False, product_filename: str = None) -> Raster:
        return self._get_angle("sensor_zenith", self.SENSOR_ZENITH_DATASET, "M", geometry, save_data, save_preview, product_filename)

    def get_sensor_zenith_I(self, geometry: RasterGeometry = None, save_data: bool = False, save_preview: bool = False, product_filename: str = None) -> Raster:
        return self._get_angle("sensor_zenith", self.SENSOR_ZENITH_DATASET, "I", geometry, save_data, save_preview, product_filename)

    sensor_zenith_M = property(get_sensor_zenith_M)
    sensor_zenith_I = property(get_sensor_zenith_I)

    def sensor_zenith(self, band: str, geometry: RasterGeometry = None, save_data: bool = False, save_preview: bool = False, product_filename: str = None) -> Raster:
        band_letter = band[0]
        if band_letter == "I":
            return self.get_sensor_zenith_I(geometry, save_data, save_preview, product_filename)
        if band_letter == "M":
            return self.get_sensor_zenith_M(geometry, save_data, save_preview, product_filename)
        raise ValueError(f"invalid band: {band}")

    def get_sensor_azimuth_M(self, geometry: RasterGeometry = None, save_data: bool = False, save_preview: bool = False, product_filename: str = None) -> Raster:
        return self._get_angle("sensor_azimuth", self.SENSOR_AZIMUTH_DATASET, "M", geometry, save_data, save_preview, product_filename)

    def get_sensor_azimuth_I(self, geometry: RasterGeometry = None, save_data: bool = False, save_preview: bool = False, product_filename: str = None) -> Raster:
        return self._get_angle("sensor_azimuth", self.SENSOR_AZIMUTH_DATASET, "I", geometry, save_data, save_preview, product_filename)

    sensor_azimuth_M = property(get_sensor_azimuth_M)
    sensor_azimuth_I = property(get_sensor_azimuth_I)

    def sensor_azimuth(self, band: str, geometry: RasterGeometry = None, save_data: bool = False, save_preview: bool = False, product_filename: str = None) -> Raster:
        band_letter = band[0]
        if band_letter == "I":
            return self.get_sensor_azimuth_I(geometry, save_data, save_preview, product_filename)
        if band_letter == "M":
            return self.get_sensor_azimuth_M(geometry, save_data, save_preview, product_filename)
        raise ValueError(f"invalid band: {band}")

    def get_solar_zenith_M(self, geometry: RasterGeometry = None, save_data: bool = False, save_preview: bool = False, product_filename: str = None) -> Raster:
        return self._get_angle("solar_zenith", self.SOLAR_ZENITH_DATASET, "M", geometry, save_data, save_preview, product_filename)

    def get_solar_zenith_I(self, geometry: RasterGeometry = None, save_data: bool = False, save_preview: bool = False, product_filename: str = None) -> Raster:
        return self._get_angle("solar_zenith", self.SOLAR_ZENITH_DATASET, "I", geometry, save_data, save_preview, product_filename)

    solar_zenith_M = property(get_solar_zenith_M)
    solar_zenith_I = property(get_solar_zenith_I)

    def solar_zenith(self, band: str, geometry: RasterGeometry = None, save_data: bool = False, save_preview: bool = False, product_filename: str = None) -> Raster:
        band_letter = band[0]
        if band_letter == "I":
            return self.get_solar_zenith_I(geometry, save_data, save_preview, product_filename)
        if band_letter == "M":
            return self.get_solar_zenith_M(geometry, save_data, save_preview, product_filename)
        raise ValueError(f"invalid band: {band}")

    def get_solar_azimuth_M(self, geometry: RasterGeometry = None, save_data: bool = False, save_preview: bool = False, product_filename: str = None) -> Raster:
        return self._get_angle("solar_azimuth", self.SOLAR_AZIMUTH_DATASET, "M", geometry, save_data, save_preview, product_filename)

    def get_solar_azimuth_I(self, geometry: RasterGeometry = None, save_data: bool = False, save_preview: bool = False, product_filename: str = None) -> Raster:
        return self._get_angle("solar_azimuth", self.SOLAR_AZIMUTH_DATASET, "I", geometry, save_data, save_preview, product_filename)

    solar_azimuth_M = property(get_solar_azimuth_M)
    solar_azimuth_I = property(get_solar_azimuth_I)

    def solar_azimuth(self, band: str, geometry: RasterGeometry = None, save_data: bool = False, save_preview: bool = False, product_filename: str = None) -> Raster:
        band_letter = band[0]
        if band_letter == "I":
            return self.get_solar_azimuth_I(geometry, save_data, save_preview, product_filename)
        if band_letter == "M":
            return self.get_solar_azimuth_M(geometry, save_data, save_preview, product_filename)
        raise ValueError(f"invalid band: {band}")

    def get_M_band(
        self,
        band: int,
        cloud_mask: Raster = None,
        apply_cloud_mask: bool = True,
        geometry: RasterGeometry = None,
        save_data: bool = False,
        save_preview: bool = False,
        product_filename: str = None,
    ) -> Raster:
        image = self._load_or_compute_dataset_product(
            product_name=f"M{band}",
            dataset_name=self.M_BAND_TEMPLATE.format(band=int(band)),
            scale_factor=0.0001,
            cloud_mask=cloud_mask,
            apply_cloud_mask=apply_cloud_mask,
            save_data=save_data,
            save_preview=save_preview,
            product_filename=product_filename,
            log_name=f"M-band {band} surface reflectance",
        )

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
        product_filename: str = None,
    ) -> Raster:
        image = self._load_or_compute_dataset_product(
            product_name=f"I{band}",
            dataset_name=self.I_BAND_TEMPLATE.format(band=int(band)),
            scale_factor=0.0001,
            cloud_mask=cloud_mask,
            apply_cloud_mask=apply_cloud_mask,
            save_data=save_data,
            save_preview=save_preview,
            product_filename=product_filename,
            log_name=f"I-band {band} surface reflectance",
        )

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
        product_filename: str = None,
    ) -> Raster:
        try:
            band_letter = band[0]
            band_number = int(band[1:])
        except Exception:
            raise ValueError(f"invalid band: {band}")

        if band_letter == "I":
            return self.get_I_band(
                band=band_number,
                cloud_mask=cloud_mask,
                apply_cloud_mask=apply_cloud_mask,
                geometry=geometry,
                save_data=save_data,
                save_preview=save_preview,
                product_filename=product_filename,
            )
        if band_letter == "M":
            return self.get_M_band(
                band=band_number,
                cloud_mask=cloud_mask,
                apply_cloud_mask=apply_cloud_mask,
                geometry=geometry,
                save_data=save_data,
                save_preview=save_preview,
                product_filename=product_filename,
            )
        raise ValueError(f"invalid band: {band}")

    def get_red(self, cloud_mask: Raster = None, apply_cloud_mask: bool = True, geometry: RasterGeometry = None, save_data: bool = False, save_preview: bool = False, product_filename: str = None) -> Raster:
        return self.get_I_band(1, cloud_mask, apply_cloud_mask, geometry, save_data, save_preview, product_filename)

    red = property(get_red)

    def get_NIR(self, cloud_mask: Raster = None, apply_cloud_mask: bool = True, geometry: RasterGeometry = None, save_data: bool = False, save_preview: bool = False, product_filename: str = None) -> Raster:
        return self.get_I_band(2, cloud_mask, apply_cloud_mask, geometry, save_data, save_preview, product_filename)

    NIR = property(get_NIR)

    def get_NDVI(
        self,
        cloud_mask: Raster = None,
        apply_cloud_mask: bool = True,
        geometry: RasterGeometry = None,
        save_data: bool = False,
        save_preview: bool = False,
        product_filename: str = None,
    ) -> Raster:
        if product_filename is None:
            product_filename = self.product_filename("NDVI")

        if product_filename is not None and exists(product_filename):
            logger.info(f"loading {self.PRODUCT_SHORT_NAME} NDVI: {cl.file(product_filename)}")
            ndvi = Raster.open(product_filename)
        else:
            red = self.get_red(cloud_mask, apply_cloud_mask, geometry, save_data, save_preview)
            nir = self.get_NIR(cloud_mask, apply_cloud_mask, geometry, save_data, save_preview)
            ndvi = np.clip((nir - red) / (nir + red), -1, 1)

        if save_data and not exists(product_filename):
            logger.info(f"writing {self.PRODUCT_SHORT_NAME} NDVI: {cl.file(product_filename)}")
            ndvi.to_geotiff(product_filename)

            if save_preview:
                ndvi.percentilecut.to_geojpeg(product_filename.replace(".tif", ".jpeg"))

        if geometry is not None:
            ndvi = ndvi.to_geometry(geometry)

        ndvi.cmap = NDVI_COLORMAP
        return ndvi

    NDVI = property(get_NDVI)

    def get_albedo(
        self,
        cloud_mask: Raster = None,
        apply_cloud_mask: bool = True,
        geometry: RasterGeometry = None,
        save_data: bool = False,
        save_preview: bool = False,
        product_filename: str = None,
    ) -> Raster:
        if product_filename is None:
            product_filename = self.product_filename("albedo")

        if product_filename is not None and exists(product_filename):
            logger.info(f"loading {self.PRODUCT_SHORT_NAME} albedo: {cl.file(product_filename)}")
            albedo = Raster.open(product_filename)
        else:
            b1 = self.get_M_band(1, cloud_mask, apply_cloud_mask, geometry, save_data, save_preview)
            b2 = self.get_M_band(2, cloud_mask, apply_cloud_mask, geometry, save_data, save_preview)
            b3 = self.get_M_band(3, cloud_mask, apply_cloud_mask, geometry, save_data, save_preview)
            b4 = self.get_M_band(4, cloud_mask, apply_cloud_mask, geometry, save_data, save_preview)
            b5 = self.get_M_band(5, cloud_mask, apply_cloud_mask, geometry, save_data, save_preview)
            b7 = self.get_M_band(7, cloud_mask, apply_cloud_mask, geometry, save_data, save_preview)
            b8 = self.get_M_band(8, cloud_mask, apply_cloud_mask, geometry, save_data, save_preview)
            b10 = self.get_M_band(10, cloud_mask, apply_cloud_mask, geometry, save_data, save_preview)
            b11 = self.get_M_band(11, cloud_mask, apply_cloud_mask, geometry, save_data, save_preview)

            albedo = (
                0.2418 * b1
                - 0.201 * b2
                + 0.2093 * b3
                + 0.1146 * b4
                + 0.1348 * b5
                + 0.2251 * b7
                + 0.1123 * b8
                + 0.0860 * b10
                + 0.0803 * b11
                - 0.0131
            )
            albedo = np.clip(albedo, 0, 1)

        if save_data and not exists(product_filename):
            logger.info(f"writing {self.PRODUCT_SHORT_NAME} albedo: {cl.file(product_filename)}")
            albedo.to_geotiff(product_filename)

        if geometry is not None:
            logger.info(f"projecting {self.PRODUCT_SHORT_NAME} albedo from {cl.val(albedo.geometry.cell_size)} to {cl.val(geometry.cell_size)}")
            albedo = albedo.to_geometry(geometry)

        albedo.cmap = ALBEDO_COLORMAP
        return albedo

    albedo = property(get_albedo)
