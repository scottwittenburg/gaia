import logging
import uuid
import os
import numpy as np
import pandas as pd
try:
    import osr
except ImportError:
    from osgeo import osr
from geopandas import GeoDataFrame
import pysal
import gaia.pysal_weights as wt
from gaia.core import get_abspath, config, GaiaException
from gaia.inputs import VectorFileIO, RasterFileIO, WeightFileIO, JsonFileIO, reproject
import gaia.formats as formats
from gaia.gdal_functions import gdal_clip, gdal_calc


logger = logging.getLogger('gaia.processes')


class GaiaProcess(object):
    """
    Defines a process to run on geospatial inputs
    """

    # TODO: Enforce required inputs and args
    required_inputs = tuple()
    required_args = tuple()

    def __init__(self, inputs=None, output=None, args=None, parent=None):
        self.inputs = inputs
        self.output = output
        self.args = args
        self.parent = parent
        self.id = str(uuid.uuid4())

    def compute(self):
        previous_input = None
        for input in self.inputs:
            if input.data is None:
                input.read()
            if previous_input:
                if input.get_epsg() != previous_input.get_epsg():
                    reproject(input, previous_input.epsg)
            previous_input = input

    def purge(self):
        self.output.delete()

    def get_outpath(self, uri=config['gaia']['output_path']):
        ids_path = '{}/{}'.format(
            self.parent, self.id) if self.parent else self.id
        return get_abspath(
            os.path.join(uri, ids_path,
                         '{}{}'.format(self.id, self.default_output[0])))


class BufferProcess(GaiaProcess):
    """
    Generates a buffer polygon around the geometries of the input data.
    The size of the buffer is determined by the 'buffer_size' args key
    and the unit of measure should be meters.  If inputs are not in a
    metric projection they will be reprojected to EPSG:3857.
    """
    required_inputs = (('input', formats.VECTOR),)
    required_args = ('buffer_size',)
    default_output = formats.JSON

    def __init__(self, **kwargs):
        super(BufferProcess, self).__init__(**kwargs)
        if not self.output:
            self.output = VectorFileIO(name='result',
                                       uri=self.get_outpath())

    def compute(self):
        original_projection = self.inputs[0].get_epsg()
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(int(original_projection))
        if not srs.GetAttrValue('UNIT').lower().startswith('met'):
            reproject(self.inputs[0], 3857)
        else:
            original_projection = None

        super(BufferProcess, self).compute()
        # TODO: Don't assume GeoPandas Dataframe. Could be PostGIS,Girder,etc.
        first_df = self.inputs[0].read()
        buffer = first_df.buffer(self.args['buffer_size'])
        buffer_df = GeoDataFrame(geometry=buffer)
        self.output.data = buffer_df
        self.output.write()
        if original_projection:
            reproject(self.output, original_projection)
            self.output.write()


class SubsetProcess(GaiaProcess):
    """
    Generates a raster dataset representing the portion of the input raster
    dataset that is contained within a vector polygon.
    """
    required_inputs = (('clip', formats.JSON), ('raster', formats.RASTER))
    default_output = formats.RASTER

    def __init__(self, **kwargs):
        """
        Create a process to subset a raster by a vector polygon
        :param clip_io: IO object containing vector polygon data
        :param raster_io: IO object containing raster data
        :param kwargs:
        :return: SubsetProcess object
        """
        super(SubsetProcess, self).__init__(**kwargs)
        if not self.output:
            self.output = RasterFileIO(name='result',
                                       uri=self.get_outpath())

    def compute(self):
        super(SubsetProcess, self).compute()
        clip_df = self.inputs[0].read()
        raster_img = self.inputs[1].read()
        # Merge all features in vector input
        raster_output = self.output.uri
        self.output.create_output_dir(raster_output)
        clip_json = clip_df.geometry.unary_union.__geo_interface__
        self.output.data = gdal_clip(raster_img, raster_output, clip_json)


class WithinProcess(GaiaProcess):
    """
    Similar to SubsetProcess but for vectors: calculates the features within
    a vector dataset that are within (or whose centroids are within) the
    polygons of a second vector dataset.
    """

    required_inputs = (('first', formats.VECTOR), ('second', formats.VECTOR))
    default_output = formats.JSON

    def __init__(self, **kwargs):
        super(WithinProcess, self).__init__(**kwargs)
        if not self.output:
            self.output = VectorFileIO(name='result',
                                       uri=self.get_outpath())

    def compute(self):
        super(WithinProcess, self).compute()
        # TODO: Don't assume GeoPandas Dataframe. Could be PostGIS,Girder,etc.
        if len(self.inputs) != 2:
            raise GaiaException('WithinProcess requires 2 inputs')
        first_df = self.inputs[0].read()
        second_df = self.inputs[1].read()
        first_within = first_df[first_df.geometry.within(
            second_df.geometry.unary_union)]
        self.output.data = first_within
        self.output.write()


class IntersectsProcess(GaiaProcess):
    """
    Calculates the features within the first vector dataset that touch
    the features of the second vector dataset.
    """

    default_output = formats.JSON

    def __init__(self, **kwargs):
        super(IntersectsProcess, self).__init__(**kwargs)
        if not self.output:
            self.output = VectorFileIO(name='result',
                                       uri=self.get_outpath())

    def compute(self):
        super(IntersectsProcess, self).compute()
        first_df = self.inputs[0].read()
        second_df = self.inputs[1].read()
        first_intersects = first_df[first_df.geometry.intersects(
            second_df.geometry.unary_union)]
        self.output.data = first_intersects
        self.output.write()
        logger.debug(self.output)


class DifferenceProcess(GaiaProcess):
    """
    Calculates which features of the first vector dataset do not
    intersect the features of the second dataset.
    """

    default_output = formats.JSON

    def __init__(self, **kwargs):
        super(DifferenceProcess, self).__init__(**kwargs)
        if not self.output:
            self.output = VectorFileIO(name='result',
                                       uri=self.get_outpath())

    def compute(self):
        super(DifferenceProcess, self).compute()
        first_df = self.inputs[0].read()
        second_df = self.inputs[1].read()

        first_difference = first_df[-first_df.geometry.intersects(
            second_df.geometry.unary_union)]

        self.output.data = first_difference
        self.output.write()
        logger.debug(self.output)


class UnionProcess(GaiaProcess):
    """
    Combines two vector datasets into one.
    """

    default_output = formats.JSON

    def __init__(self, **kwargs):
        super(UnionProcess, self).__init__(**kwargs)
        if not self.output:
            self.output = VectorFileIO(name='result',
                                       uri=self.get_outpath())

    def compute(self):
        super(UnionProcess, self).compute()
        first_df = self.inputs[0].read()
        second_df = self.inputs[1].read()

        uniondf = GeoDataFrame(pd.concat([first_df, second_df]))

        self.output.data = uniondf
        self.output.write()
        logger.debug(self.output)


class CentroidProcess(GaiaProcess):
    """
    Calculates the centroid point of a vector dataset.
    """

    default_output = formats.JSON

    def __init__(self, **kwargs):
        super(CentroidProcess, self).__init__(**kwargs)
        if not self.output:
            self.output = VectorFileIO(name='result',
                                       uri=self.get_outpath())

    def compute(self):
        super(CentroidProcess, self).compute()
        first_df = self.inputs[0].read()

        first_centroids = first_df.geometry.centroid

        centroids = GeoDataFrame(first_centroids[first_df.centroid.within(
            first_df.geometry)])

        centroids.columns = ['geometry']
        self.output.data = centroids
        self.output.write()
        logger.debug(self.output)


class DistanceProcess(GaiaProcess):
    """
    Calculates the minimum distance from each feature of the first dataset
    to the nearest feature of the second dataset.
    """

    default_output = formats.JSON

    def __init__(self, **kwargs):
        super(DistanceProcess, self).__init__(**kwargs)
        if not self.output:
            self.output = VectorFileIO(name='result',
                                       uri=self.get_outpath())

    def compute(self):
        property_name = 'min_dist'
        super(DistanceProcess, self).compute()
        first_df = self.inputs[0].read()
        second_df = self.inputs[1].read()

        first_gs = first_df.geometry

        second_gs = second_df.geometry

        first_length = len(first_gs)

        min_dist = np.empty(first_length)

        for i, first_features in enumerate(first_gs):
            min_dist[i] = np.min([first_features.distance(second_features)
                                  for second_features in second_gs])

        first_df[property_name] = min_dist

        self.output.data = first_df
        self.output.write()
        logger.debug(self.output)

class RasterMathProcess(GaiaProcess):
    """
    Performs raster math/algebra based on supplied arguments.
    Inputs should consist of at least one raster IO object.
    Required arg is 'calc', an equation for the input rasters.
    Example: "A + B / (C * 2.4)".  The letters in the equation
    should correspond to the names of the inputs.
    """
    required_inputs = (('A', formats.RASTER),)
    required_args = ('calc',)
    optional_args = ('bands', 'nodata', 'allBands', 'output_type')
    default_output = formats.RASTER

    def __init__(self, **kwargs):
        super(RasterMathProcess, self).__init__(**kwargs)
        if not self.output:
            self.output = RasterFileIO(name='result',
                                       uri=self.get_outpath())

    def compute(self):
        super(RasterMathProcess, self).compute()

        calculation = self.args['calc']
        rasters = [x.read() for x in self.inputs]
        bands = self.args.get('bands' or None)
        nodata = self.args.get('nodata' or None)
        all_bands = self.args.get('allBands' or None)
        otype = self.args.get('output_type' or None)
        self.output.create_output_dir(self.output.uri)
        self.output.data = gdal_calc(calculation,
                                     self.output.uri,
                                     rasters,
                                     bands=bands,
                                     nodata=nodata,
                                     allBands=all_bands,
                                     output_type=otype)

class ClusterProcess(GaiaProcess):
    """
    Local Moran's I Calculation (Local Indicators of Spatial Association, or LISAs) to identifying clusters in data
    https://pysal.readthedocs.org/en/latest/users/tutorials/autocorrelation.html#local-moran-s-i

    Returns original vector layer with associated Moran's I statistics, including:
    lm_Is: float, Moran's I
    lm_q: float, quadrat location
    lm_p_sims: float, p-value based on permutations (low p-value means observed Is differ from expected Is significantly)
    lm_sig: boolean, True if p_sims is below 0.05
    """
    required_inputs = (('input', formats.VECTOR),)
    required_args = ('var_col')
    optional_args = ('adjust_by_col')
    default_output = formats.JSON

    def __init__(self, **kwargs):
        super(ClusterProcess, self).__init__(**kwargs)
        if not self.output:
            self.output = VectorFileIO(name='result',
                                       uri=self.get_outpath())

    def compute(self):
        super(ClusterProcess, self).compute()

        first_df = self.inputs[0].read()
        col = self.args['var_col']
        adjust_by_col = self.args.get('adjust_by_col' or None)

        # filter out null fields or else weight functions won't work
        filter_out = first_df[col].isnull()
        filtered_df = first_df[filter_out != True].reset_index()

        # get Local Moran's I
        f = np.array(filtered_df[col])
        w = wt.gpd_contiguity(filtered_df)
        if adjust_by_col:
            adjust_by = np.array(filtered_df[adjust_by_col])
            lm = pysal.esda.moran.Moran_Local_Rate(f, adjust_by, w, permutations=9999)
        else:
            lm = pysal.Moran_Local(f, w, permutations=9999)

        sig = lm.p_sim<0.05
        filtered_df['lm_sig'] = sig
        filtered_df['lm_p_sim'] = lm.p_sim
        filtered_df['lm_q'] = lm.q
        filtered_df['lm_Is'] = lm.Is

        self.output.data = filtered_df
        self.output.write()
        logger.debug(self.output)

class AutocorrelationProcess(GaiaProcess):
    """
    Calculate Moran's I global autocorrelation for the input data.
    Default number of permutations = 999
    Uses contiguity weight (queen) by default.

    Returns the following Moran's I attributes as json:
    I: float, value of Moran's I
    EI: float, expected value of I under normality assumption
    p_norm: float, p-value of I under normality assumption
    EI_sim: float, average value of I from permutations
    p_sim: array, p-value based on permutations (one-tailed)
    z_sim: float, standardized I based on permutations
    p_z_sim: float, p-value based on standard normal approximation from permutations
    """
    required_inputs = (('input', formats.VECTOR),)
    required_args = ('var_col')
    optional_args = ('adjust_by_col', 'permutations')
    default_output = formats.JSON

    def __init__(self, **kwargs):
        super(AutocorrelationProcess, self).__init__(**kwargs)
        if not self.output:
            self.output = JsonFileIO(name='result',
                                       uri=self.get_outpath())

    def compute(self):
        super(AutocorrelationProcess, self).compute()
        for input in self.inputs:
            if input.name == 'input':
                first_df = input.read()
        col = self.args['var_col']
        adjust_by_col = self.args.get('adjust_by_col' or None)
        permutations = self.args.get('permutations' or None)
        if not permutations:
            permutations = 999

        # filter out null fields
        filter_out = first_df[col].isnull()
        filtered_df = first_df[filter_out != True]

        # get Global Moran's I
        f = np.array(filtered_df[col])
        w = wt.gpd_contiguity(filtered_df)
        if adjust_by_col:
            adjust_by = np.array(filtered_df[adjust_by_col])
            mi = pysal.esda.moran.Moran_Rate(f, adjust_by, w, permutations=permutations)
        else:
            mi = pysal.Moran(f, w, permutations=permutations)

        keep = ['I', 'EI', 'p_norm', 'EI_sim', 'p_sim', 'z_sim', 'p_z_sim']
        mi_dict = {k:getattr(mi, k) for k in keep}

        self.output.data = mi_dict
        self.output.write()
        logger.debug(self.output)


class WeightProcess(GaiaProcess):
    """
    Calculate spatial weight.
    weight_type available includes: contiguity, knnW, distanceBandW, kernel
    """
    required_inputs = (('input', formats.VECTOR),)
    required_args = ('weight_type')
    default_output = formats.WEIGHT

    def __init__(self, **kwargs):
        super(WeightProcess, self).__init__(**kwargs)
        if not self.output:
            self.output = WeightFileIO(name='result',
                                       uri=self.get_outpath())

    def compute(self):
        super(WeightProcess, self).compute()
        for input in self.inputs:
            if input.name == 'input':
                first_df = input.read()
        weight_type = self.args['weight_type']
        if weight_type == 'contiguity':
            w = wt.gpd_contiguity(first_df)
        elif weight_type == 'knnW':
            w = wt.gpd_knnW(first_df)
        elif weight_type == 'distanceBandW':
            w = wt.gpd_distanceBandW(first_df)
        elif weight_type == 'kernel':
            w = wt.gpd_kernel(first_df)
        # TODO: add params related to dif weight types
        else:
            print u'weight type {0} not available'.format(weight_type)
        self.output.data = w
        self.output.write()
        logger.debug(self.output)