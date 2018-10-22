#!/usr/bin/env python
# -*- coding: utf-8 -*-
# pyresample, Resampling of remote sensing image data in python
#
# Copyright (C) 2010-2016
#
# Authors:
#    Esben S. Nielsen
#    Thomas Lavergne
#    Adam Dybbroe
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public License for more
# details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Classes for geometry operations"""

import hashlib
import warnings
from collections import OrderedDict
from logging import getLogger

import numpy as np
import yaml
from pyproj import Geod

from pyresample import CHUNK_SIZE, utils
from pyresample._spatial_mp import Cartesian, Cartesian_MP, Proj, Proj_MP
from pyresample.boundary import AreaDefBoundary, Boundary, SimpleBoundary

try:
    from xarray import DataArray
except ImportError:
    DataArray = np.ndarray

logger = getLogger(__name__)


class DimensionError(ValueError):
    pass


class IncompatibleAreas(ValueError):
    """Error when the areas to combine are not compatible."""


class BaseDefinition(object):
    """Base class for geometry definitions.

    .. versionchanged:: 1.8.0

        `BaseDefinition` no longer checks the validity of the provided
        longitude and latitude coordinates to improve performance. Longitude
        arrays are expected to be between -180 and 180 degrees, latitude -90
        to 90 degrees. Use `pyresample.utils.check_and_wrap` to preprocess
        your arrays.

    """

    def __init__(self, lons=None, lats=None, nprocs=1):
        if type(lons) != type(lats):
            raise TypeError('lons and lats must be of same type')
        elif lons is not None:
            if not isinstance(lons, (np.ndarray, DataArray)):
                lons = np.asanyarray(lons)
                lats = np.asanyarray(lats)
            if lons.shape != lats.shape:
                raise ValueError('lons and lats must have same shape')

        self.nprocs = nprocs
        self.lats = lats
        self.lons = lons
        self.ndim = None
        self.cartesian_coords = None
        self.hash = None

    def __getitem__(self, key):
        """Slice a 2D geographic definition."""
        y_slice, x_slice = key
        return self.__class__(
            lons=self.lons[y_slice, x_slice],
            lats=self.lats[y_slice, x_slice],
            nprocs=self.nprocs
        )

    def __hash__(self):
        """Compute the hash of this object."""
        if self.hash is None:
            self.hash = int(self.update_hash().hexdigest(), 16)
        return self.hash

    def __eq__(self, other):
        """Test for approximate equality"""
        if self is other:
            return True
        if other.lons is None or other.lats is None:
            other_lons, other_lats = other.get_lonlats()
        else:
            other_lons = other.lons
            other_lats = other.lats

        if self.lons is None or self.lats is None:
            self_lons, self_lats = self.get_lonlats()
        else:
            self_lons = self.lons
            self_lats = self.lats

        if self_lons is other_lons and self_lats is other_lats:
            return True
        if isinstance(self_lons, DataArray) and np.ndarray is not DataArray:
            self_lons = self_lons.data
            self_lats = self_lats.data
        if isinstance(other_lons, DataArray) and np.ndarray is not DataArray:
            other_lons = other_lons.data
            other_lats = other_lats.data
        try:
            from dask.array import allclose
        except ImportError:
            from numpy import allclose
        try:
            return (allclose(self_lons, other_lons, atol=1e-6, rtol=5e-9, equal_nan=True) and
                    allclose(self_lats, other_lats, atol=1e-6, rtol=5e-9, equal_nan=True))
        except (AttributeError, ValueError):
            return False

    def __ne__(self, other):
        """Test for approximate equality"""

        return not self.__eq__(other)

    def get_area_extent_for_subset(self, row_LR, col_LR, row_UL, col_UL):
        """Calculate extent for a subdomain of this area

        Rows are counted from upper left to lower left and columns are
        counted from upper left to upper right.

        Args:
            row_LR (int): row of the lower right pixel
            col_LR (int): col of the lower right pixel
            row_UL (int): row of the upper left pixel
            col_UL (int): col of the upper left pixel

        Returns:
            area_extent (tuple):
                Area extent (LL_x, LL_y, UR_x, UR_y) of the subset

        Author:
            Ulrich Hamann

        """

        (a, b) = self.get_proj_coords(data_slice=(row_LR, col_LR))
        a = a - 0.5 * self.pixel_size_x
        b = b - 0.5 * self.pixel_size_y
        (c, d) = self.get_proj_coords(data_slice=(row_UL, col_UL))
        c = c + 0.5 * self.pixel_size_x
        d = d + 0.5 * self.pixel_size_y

        return a, b, c, d

    def get_lonlat(self, row, col):
        """Retrieve lon and lat of single pixel

        Parameters
        ----------
        row : int
        col : int

        Returns
        -------
        (lon, lat) : tuple of floats
        """

        if self.ndim != 2:
            raise DimensionError(('operation undefined '
                                  'for %sD geometry ') % self.ndim)
        elif self.lons is None or self.lats is None:
            raise ValueError('lon/lat values are not defined')
        return self.lons[row, col], self.lats[row, col]

    def get_lonlats(self, data_slice=None, **kwargs):
        """Base method for lon lat retrieval with slicing"""

        if self.lons is None or self.lats is None:
            raise ValueError('lon/lat values are not defined')
        elif data_slice is None:
            return self.lons, self.lats
        else:
            return self.lons[data_slice], self.lats[data_slice]

    def get_lonlats_dask(self, chunks=CHUNK_SIZE):
        """Get the lon lats as a single dask array."""
        import dask.array as da
        lons, lats = self.get_lonlats()

        if isinstance(lons.data, da.Array):
            return lons.data, lats.data
        else:
            lons = da.from_array(np.asanyarray(lons),
                                 chunks=chunks)
            lats = da.from_array(np.asanyarray(lats),
                                 chunks=chunks)
        return lons, lats

    def get_boundary_lonlats(self):
        """Return Boundary objects."""
        s1_lon, s1_lat = self.get_lonlats(data_slice=(0, slice(None)))
        s2_lon, s2_lat = self.get_lonlats(data_slice=(slice(None), -1))
        s3_lon, s3_lat = self.get_lonlats(data_slice=(-1, slice(None, None, -1)))
        s4_lon, s4_lat = self.get_lonlats(data_slice=(slice(None, None, -1), 0))
        return (SimpleBoundary(s1_lon.squeeze(), s2_lon.squeeze(), s3_lon.squeeze(), s4_lon.squeeze()),
                SimpleBoundary(s1_lat.squeeze(), s2_lat.squeeze(), s3_lat.squeeze(), s4_lat.squeeze()))

    def get_bbox_lonlats(self):
        """Returns the bounding box lons and lats"""

        s1_lon, s1_lat = self.get_lonlats(data_slice=(0, slice(None)))
        s2_lon, s2_lat = self.get_lonlats(data_slice=(slice(None), -1))
        s3_lon, s3_lat = self.get_lonlats(data_slice=(-1, slice(None, None, -1)))
        s4_lon, s4_lat = self.get_lonlats(data_slice=(slice(None, None, -1), 0))
        return zip(*[(s1_lon.squeeze(), s1_lat.squeeze()),
                     (s2_lon.squeeze(), s2_lat.squeeze()),
                     (s3_lon.squeeze(), s3_lat.squeeze()),
                     (s4_lon.squeeze(), s4_lat.squeeze())])

    def get_cartesian_coords(self, nprocs=None, data_slice=None, cache=False):
        """Retrieve cartesian coordinates of geometry definition

        Parameters
        ----------
        nprocs : int, optional
            Number of processor cores to be used.
            Defaults to the nprocs set when instantiating object
        data_slice : slice object, optional
            Calculate only cartesian coordnates for the defined slice
        cache : bool, optional
            Store result the result. Requires data_slice to be None

        Returns
        -------
        cartesian_coords : numpy array
        """

        if self.cartesian_coords is None:
            # Coordinates are not cached
            if nprocs is None:
                nprocs = self.nprocs

            if data_slice is None:
                # Use full slice
                data_slice = slice(None)

            lons, lats = self.get_lonlats(nprocs=nprocs, data_slice=data_slice)

            if nprocs > 1:
                cartesian = Cartesian_MP(nprocs)
            else:
                cartesian = Cartesian()

            cartesian_coords = cartesian.transform_lonlats(np.ravel(lons),
                                                           np.ravel(lats))

            if isinstance(lons, np.ndarray) and lons.ndim > 1:
                # Reshape to correct shape
                cartesian_coords = cartesian_coords.reshape(lons.shape[0],
                                                            lons.shape[1], 3)

            if cache and data_slice is None:
                self.cartesian_coords = cartesian_coords
        else:
            # Coordinates are cached
            if data_slice is None:
                cartesian_coords = self.cartesian_coords
            else:
                cartesian_coords = self.cartesian_coords[data_slice]

        return cartesian_coords

    @property
    def corners(self):
        """Returns the corners of the current area.
        """
        from pyresample.spherical_geometry import Coordinate
        return [Coordinate(*self.get_lonlat(0, 0)),
                Coordinate(*self.get_lonlat(0, -1)),
                Coordinate(*self.get_lonlat(-1, -1)),
                Coordinate(*self.get_lonlat(-1, 0))]

    def __contains__(self, point):
        """Is a point inside the 4 corners of the current area? This uses
        great circle arcs as area boundaries.
        """
        from pyresample.spherical_geometry import point_inside, Coordinate
        corners = self.corners

        if isinstance(point, tuple):
            return point_inside(Coordinate(*point), corners)
        else:
            return point_inside(point, corners)

    def overlaps(self, other):
        """Tests if the current area overlaps the *other* area. This is based
        solely on the corners of areas, assuming the boundaries to be great
        circles.

        Parameters
        ----------
        other : object
            Instance of subclass of BaseDefinition

        Returns
        -------
        overlaps : bool
        """

        from pyresample.spherical_geometry import Arc

        self_corners = self.corners

        other_corners = other.corners

        for i in self_corners:
            if i in other:
                return True
        for i in other_corners:
            if i in self:
                return True

        self_arc1 = Arc(self_corners[0], self_corners[1])
        self_arc2 = Arc(self_corners[1], self_corners[2])
        self_arc3 = Arc(self_corners[2], self_corners[3])
        self_arc4 = Arc(self_corners[3], self_corners[0])

        other_arc1 = Arc(other_corners[0], other_corners[1])
        other_arc2 = Arc(other_corners[1], other_corners[2])
        other_arc3 = Arc(other_corners[2], other_corners[3])
        other_arc4 = Arc(other_corners[3], other_corners[0])

        for i in (self_arc1, self_arc2, self_arc3, self_arc4):
            for j in (other_arc1, other_arc2, other_arc3, other_arc4):
                if i.intersects(j):
                    return True
        return False

    def get_area(self):
        """Get the area of the convex area defined by the corners of the current
        area.
        """
        from pyresample.spherical_geometry import get_polygon_area

        return get_polygon_area(self.corners)

    def intersection(self, other):
        """Returns the corners of the intersection polygon of the current area
        with *other*.

        Parameters
        ----------
        other : object
            Instance of subclass of BaseDefinition

        Returns
        -------
        (corner1, corner2, corner3, corner4) : tuple of points
        """
        from pyresample.spherical_geometry import intersection_polygon
        return intersection_polygon(self.corners, other.corners)

    def overlap_rate(self, other):
        """Get how much the current area overlaps an *other* area.

        Parameters
        ----------
        other : object
            Instance of subclass of BaseDefinition

        Returns
        -------
        overlap_rate : float
        """

        from pyresample.spherical_geometry import get_polygon_area
        other_area = other.get_area()
        inter_area = get_polygon_area(self.intersection(other))
        return inter_area / other_area

    def get_area_slices(self, area_to_cover):
        """Compute the slice to read based on an `area_to_cover`."""
        raise NotImplementedError


class CoordinateDefinition(BaseDefinition):
    """Base class for geometry definitions defined by lons and lats only"""

    def __init__(self, lons, lats, nprocs=1):
        if not isinstance(lons, (np.ndarray, DataArray)):
            lons = np.asanyarray(lons)
            lats = np.asanyarray(lats)
        super(CoordinateDefinition, self).__init__(lons, lats, nprocs)
        if lons.shape == lats.shape and lons.dtype == lats.dtype:
            self.shape = lons.shape
            self.size = lons.size
            self.ndim = lons.ndim
            self.dtype = lons.dtype
        else:
            raise ValueError(('%s must be created with either '
                              'lon/lats of the same shape with same dtype') %
                             self.__class__.__name__)

    def concatenate(self, other):
        if self.ndim != other.ndim:
            raise DimensionError(('Unable to concatenate %sD and %sD '
                                  'geometries') % (self.ndim, other.ndim))
        klass = _get_highest_level_class(self, other)
        lons = np.concatenate((self.lons, other.lons))
        lats = np.concatenate((self.lats, other.lats))
        nprocs = min(self.nprocs, other.nprocs)
        return klass(lons, lats, nprocs=nprocs)

    def append(self, other):
        if self.ndim != other.ndim:
            raise DimensionError(('Unable to append %sD and %sD '
                                  'geometries') % (self.ndim, other.ndim))
        self.lons = np.concatenate((self.lons, other.lons))
        self.lats = np.concatenate((self.lats, other.lats))
        self.shape = self.lons.shape
        self.size = self.lons.size

    def __str__(self):
        # Rely on numpy's object printing
        return ('Shape: %s\nLons: %s\nLats: %s') % (str(self.shape),
                                                    str(self.lons),
                                                    str(self.lats))


class GridDefinition(CoordinateDefinition):
    """Grid defined by lons and lats

    Parameters
    ----------
    lons : numpy array
    lats : numpy array
    nprocs : int, optional
        Number of processor cores to be used for calculations.

    Attributes
    ----------
    shape : tuple
        Grid shape as (rows, cols)
    size : int
        Number of elements in grid
    lons : object
        Grid lons
    lats : object
        Grid lats
    cartesian_coords : object
        Grid cartesian coordinates
    """

    def __init__(self, lons, lats, nprocs=1):
        super(GridDefinition, self).__init__(lons, lats, nprocs)
        if lons.shape != lats.shape:
            raise ValueError('lon and lat grid must have same shape')
        elif lons.ndim != 2:
            raise ValueError('2 dimensional lon lat grid expected')


def get_array_hashable(arr):
    """Compute a hashable form of the array `arr`.

    Works with numpy arrays, dask.array.Array, and xarray.DataArray.
    """
    # look for precomputed value
    if isinstance(arr, DataArray) and np.ndarray is not DataArray:
        return arr.attrs.get('hash', get_array_hashable(arr.data))
    else:
        try:
            return arr.name.encode('utf-8')  # dask array
        except AttributeError:
            return np.asarray(arr).view(np.uint8)  # np array


class SwathDefinition(CoordinateDefinition):
    """Swath defined by lons and lats.

    Parameters
    ----------
    lons : numpy array
    lats : numpy array
    nprocs : int, optional
        Number of processor cores to be used for calculations.

    Attributes
    ----------
    shape : tuple
        Swath shape
    size : int
        Number of elements in swath
    ndims : int
        Swath dimensions
    lons : object
        Swath lons
    lats : object
        Swath lats
    cartesian_coords : object
        Swath cartesian coordinates

    """

    def __init__(self, lons, lats, nprocs=1):
        if not isinstance(lons, (np.ndarray, DataArray)):
            lons = np.asanyarray(lons)
            lats = np.asanyarray(lats)
        super(SwathDefinition, self).__init__(lons, lats, nprocs)
        if lons.shape != lats.shape:
            raise ValueError('lon and lat arrays must have same shape')
        elif lons.ndim > 2:
            raise ValueError('Only 1 and 2 dimensional swaths are allowed')

    def __hash__(self):
        """Compute the hash of this object."""
        if self.hash is None:
            self.hash = int(self.update_hash().hexdigest(), 16)
        return self.hash

    def update_hash(self, the_hash=None):
        if the_hash is None:
            the_hash = hashlib.sha1()
        the_hash.update(get_array_hashable(self.lons))
        the_hash.update(get_array_hashable(self.lats))
        try:
            if self.lons.mask is not np.bool_(False):
                the_hash.update(get_array_hashable(self.lons.mask))
        except AttributeError:
            pass
        return the_hash

    def _compute_omerc_parameters(self, ellipsoid):
        """Compute the oblique mercator projection bouding box parameters."""
        lines, cols = self.lons.shape
        lon1, lon2 = np.asanyarray(self.lons[[0, -1], int(cols / 2)])
        lat1, lat, lat2 = np.asanyarray(
            self.lats[[0, int(lines / 2), -1], int(cols / 2)])

        proj_dict2points = {'proj': 'omerc', 'lat_0': lat, 'ellps': ellipsoid,
                            'lat_1': lat1, 'lon_1': lon1,
                            'lat_2': lat2, 'lon_2': lon2,
                            'no_rot': True
                            }

        # We need to compute alpha-based omerc for geotiff support
        lonc, lat0 = Proj(**proj_dict2points)(0, 0, inverse=True)
        az1, az2, dist = Geod(**proj_dict2points).inv(lonc, lat0, lon2, lat2)
        azimuth = az1
        az1, az2, dist = Geod(**proj_dict2points).inv(lonc, lat0, lon1, lat1)
        if abs(az1 - azimuth) > 1:
            if abs(az2 - azimuth) > 1:
                logger.warning("Can't find appropriate azimuth.")
            else:
                azimuth += az2
                azimuth /= 2
        else:
            azimuth += az1
            azimuth /= 2
        if abs(azimuth) > 90:
            azimuth = 180 + azimuth

        prj_params = {'proj': 'omerc', 'alpha': float(azimuth), 'lat_0': float(lat0), 'lonc': float(lonc),
                      'gamma': 0,
                      'ellps': ellipsoid}

        return prj_params

    def _compute_generic_parameters(self, projection, ellipsoid):
        """Compute the projection bb parameters for most projections."""
        lines, cols = self.lons.shape
        lat_0 = self.lats[int(lines / 2), int(cols / 2)]
        lon_0 = self.lons[int(lines / 2), int(cols / 2)]
        return {'proj': projection, 'ellps': ellipsoid,
                'lat_0': lat_0, 'lon_0': lon_0}

    def get_edge_lonlats(self):
        """Get the concatenated boundary of the current swath."""
        lons, lats = self.get_bbox_lonlats()
        blons = np.ma.concatenate(lons)
        blats = np.ma.concatenate(lats)
        return blons, blats

    def compute_bb_proj_params(self, proj_dict):
        projection = proj_dict['proj']
        ellipsoid = proj_dict.get('ellps', 'WGS84')
        if projection == 'omerc':
            return self._compute_omerc_parameters(ellipsoid)
        else:
            new_proj = self._compute_generic_parameters(projection, ellipsoid)
            new_proj.update(proj_dict)
            return new_proj

    def compute_optimal_bb_area(self, proj_dict=None):
        """Compute the "best" bounding box area for this swath with `proj_dict`.

        By default, the projection is Oblique Mercator (`omerc` in proj.4), in
        which case the right projection angle `alpha` is computed from the
        swath centerline. For other projections, only the appropriate center of
        projection and area extents are computed.
        """
        if proj_dict is None:
            proj_dict = {}
        projection = proj_dict.setdefault('proj', 'omerc')
        area_id = projection + '_otf'
        description = 'On-the-fly ' + projection + ' area'
        lines, cols = self.lons.shape
        width = int(cols * 1.1)
        height = int(lines * 1.1)

        proj_dict = self.compute_bb_proj_params(proj_dict)

        area = DynamicAreaDefinition(area_id, description, proj_dict)
        lons, lats = self.get_edge_lonlats()
        return area.freeze((lons, lats), shape=(height, width))


class DynamicAreaDefinition(object):
    """An AreaDefintion containing just a subset of the needed parameters.

    The purpose of this class is to be able to adapt the area extent and shape
    of the area to a given set of longitudes and latitudes, such that e.g.
    polar satellite granules can be resampled optimaly to a give projection.
    """

    def __init__(self, area_id=None, description=None, proj_dict=None,
                 width=None, height=None, area_extent=None,
                 optimize_projection=False, rotation=None):
        """Initialize the DynamicAreaDefinition.

        area_id:
          The name of the area.
        description:
          The description of the area.
        proj_dict:
          The dictionary of projection parameters. Doesn't have to be complete.
        height, width:
          The shape of the resulting area.
        area_extent:
          The area extent of the area.
        optimize_projection:
          Whether the projection parameters have to be optimized.
        rotation:
          Rotation in degrees (negative is cw)
          """
        self.area_id = area_id
        self.description = description
        self.proj_dict = proj_dict
        self.x_size = self.width = width
        self.y_size = self.height = height
        self.area_extent = area_extent
        self.optimize_projection = optimize_projection
        self.rotation = rotation

    # size = (x_size, y_size) and shape = (y_size, x_size)
    def compute_domain(self, corners, resolution=None, shape=None):
        """Compute shape and area_extent from corners and [shape or resolution] info.
        
        Corners represents the center of pixels, while area_extent represents the edge of pixels.
        """
        if resolution is not None and shape is not None:
            raise ValueError("Both resolution and shape can't be provided.")
        elif resolution is None and shape is None:
            raise ValueError("Either resolution or shape must be provided.")

        if shape:
            height, width = shape
            x_resolution = (corners[2] - corners[0]) * 1.0 / (width - 1)
            y_resolution = (corners[3] - corners[1]) * 1.0 / (height - 1)
        else:
            try:
                x_resolution, y_resolution = resolution
            except TypeError:
                x_resolution = y_resolution = resolution
            width = int(np.rint((corners[2] - corners[0]) * 1.0 /
                                 x_resolution + 1))
            height = int(np.rint((corners[3] - corners[1]) * 1.0 /
                                 y_resolution + 1))

        area_extent = (corners[0] - x_resolution / 2,
                       corners[1] - y_resolution / 2,
                       corners[2] + x_resolution / 2,
                       corners[3] + y_resolution / 2)
        return area_extent, width, height

    def freeze(self, lonslats=None,
               resolution=None, shape=None,
               proj_info=None, rotation=None):
        """Create an AreaDefintion from this area with help of some extra info.

        lonlats:
          the geographical coordinates to contain in the resulting area.
        resolution:
          the resolution of the resulting area.
        shape:
          the shape of the resulting area.
        proj_info:
          complementing parameters to the projection info.
        rotation:
          rotation in degrees (negative is cw)

        Resolution and shape parameters are ignored if the instance is created
        with the `optimize_projection` flag set to True.
        """
        if proj_info is not None:
            self.proj_dict.update(proj_info)

        if self.optimize_projection:
            return lonslats.compute_optimal_bb_area(self.proj_dict)
        if not self.area_extent or not self.width or not self.height:
            proj4 = Proj(**self.proj_dict)
            try:
                lons, lats = lonslats
            except (TypeError, ValueError):
                lons, lats = lonslats.get_lonlats()
            xarr, yarr = proj4(np.asarray(lons), np.asarray(lats))
            xarr[xarr > 9e29] = np.nan
            yarr[yarr > 9e29] = np.nan
            corners = [np.nanmin(xarr), np.nanmin(yarr),
                       np.nanmax(xarr), np.nanmax(yarr)]
            # Note: size=(width, height) was changed to shape=(height, width).
            domain = self.compute_domain(corners, resolution, shape)
            self.area_extent, self.width, self.height = domain
        return AreaDefinition(self.area_id, self.description, '',
                              self.proj_dict, self.width, self.height,
                              self.area_extent, self.rotation)


def invproj(data_x, data_y, proj_dict):
    """Perform inverse projection."""
    # XXX: does pyproj copy arrays? What can we do so it doesn't?
    target_proj = Proj(**proj_dict)
    return np.dstack(target_proj(data_x, data_y, inverse=True))


class AreaDefinition(BaseDefinition):
    """Holds definition of an area.

    Parameters
    ----------
    area_id : str
        ID of area
    name : str
        Name of area
    proj_id : str
        ID of projection
    proj_dict : dict
        Dictionary with Proj.4 parameters
    width : int
        x dimension in number of pixels
    height : int
        y dimension in number of pixels
    rotation: float
        rotation in degrees (negative is cw)
    area_extent : list
        Area extent as a list (LL_x, LL_y, UR_x, UR_y)
    nprocs : int, optional
        Number of processor cores to be used
    lons : numpy array, optional
        Grid lons
    lats : numpy array, optional
        Grid lats

    Attributes
    ----------
    area_id : str
        ID of area
    name : str
        Name of area
    proj_id : str
        ID of projection
    proj_dict : dict
        Dictionary with Proj.4 parameters
    width : int
        x dimension in number of pixels
    height : int
        y dimension in number of pixels
    rotation: float
        rotation in degrees (negative is cw)
    shape : tuple
        Corresponding array shape as (rows, cols)
    size : int
        Number of points in grid
    area_extent : tuple
        Area extent as a tuple (LL_x, LL_y, UR_x, UR_y)
    area_extent_ll : tuple
        Area extent in lons lats as a tuple (LL_lon, LL_lat, UR_lon, UR_lat)
    pixel_size_x : float
        Pixel width in projection units
    pixel_size_y : float
        Pixel height in projection units
    top_left_extent : list
        Coordinates (UL_x, UL_y) of upper left corner of upper left pixel in projection units
    pixel_offset_x : float
        x offset between projection center and upper left corner of upper
        left pixel in units of pixels.
    pixel_offset_y : float
        y offset between projection center and upper left corner of upper
        left pixel in units of pixels..
    proj4_string : str
        Projection defined as Proj.4 string
    lons : object
        Grid lons
    lats : object
        Grid lats
    cartesian_coords : object
        Grid cartesian coordinates
    projection_x_coords : object
        Grid projection x coordinate
    projection_y_coords : object
        Grid projection y coordinate
    """

    def __init__(self, area_id, name, proj_id, proj_dict, width, height,
                 area_extent, rotation=None, nprocs=1, lons=None, lats=None,
                 dtype=np.float64):
        if not isinstance(proj_dict, dict):
            raise TypeError('Wrong type for proj_dict: %s. Expected dict.'
                            % type(proj_dict))

        super(AreaDefinition, self).__init__(lons, lats, nprocs)
        self.area_id = area_id
        self.name = name
        self.proj_id = proj_id
        self.x_size = self.width = int(width)
        self.y_size = self.height = int(height)
        self.shape = (height, width)
        self.crop_offset = (0, 0)
        try:
            self.rotation = float(rotation)
        except TypeError:
            self.rotation = 0
        if lons is not None:
            if lons.shape != self.shape:
                raise ValueError('Shape of lon lat grid must match '
                                 'area definition')
        self.size = height * width
        self.ndim = 2
        self.pixel_size_x = (area_extent[2] - area_extent[0]) / float(width)
        self.pixel_size_y = (area_extent[3] - area_extent[1]) / float(height)
        self.proj_dict = utils.convert_proj_floats(proj_dict.items())
        self.area_extent = tuple(area_extent)

        # Calculate area_extent in lon lat
        proj = Proj(**proj_dict)
        corner_lons, corner_lats = proj((area_extent[0], area_extent[2]),
                                        (area_extent[1], area_extent[3]),
                                        inverse=True)
        self.area_extent_ll = (corner_lons[0], corner_lats[0],
                               corner_lons[1], corner_lats[1])

        # Calculate projection coordinates of extent of upper left pixel
        self.top_left_extent = (float(area_extent[0]), float(area_extent[3]))

        # Pixel_offset defines the distance to projection center from origin
        # (UL) of image in units of pixels.
        self.pixel_offset_x = -self.area_extent[0] / self.pixel_size_x
        self.pixel_offset_y = self.area_extent[3] / self.pixel_size_y

        self._projection_x_coords = None
        self._projection_y_coords = None

        self.dtype = dtype

    @classmethod
    def from_extent(cls, area_id, projection, area_extent, shape, **kwargs):
        """Create an AreaDefinition object from area_extent and shape.

        Parameters
        ----------
        area_id : str
            ID of area
        projection : dict or str
            Projection parameters as a proj4_dict or proj4_string
        area_extent : list
            Area extent as a list (lower_left_x, lower_left_y, upper_right_x, upper_right_y)
        shape : list
            Number of pixels in the x and y direction (height, width)
        description : str, optional
            Description/name of area. Defaults to area_id
        proj_id : str, optional
            ID of projection
        units : str, optional
            Default projection units: meters, radians, or degrees
        rotation: float, optional
            rotation in degrees (negative is cw)
        nprocs : int, optional
            Number of processor cores to be used
        lons : numpy array, optional
            Grid lons
        lats : numpy array, optional
            Grid lats


        * **units** accepts '\xb0', 'deg', 'degrees', 'rad', 'radians', 'm', 'meters'. The order of default is:
            1. units expressed with each variable through a DataArray's attr attribute.
            2. units passed to **units**
            3. units used in **projection**
            4. meters

        Returns
        -------
        AreaDefinition : AreaDefinition
        """
        return utils.from_params(area_id, projection, area_extent=area_extent, shape=shape, **kwargs)

    @classmethod
    def from_circle(cls, area_id, projection, center, radius, shape=None, resolution=None, **kwargs):
        """Create an AreaDefinition object from center, radius, and shape or from center, radius, and resolution.

        Parameters
        ----------
        area_id : str
            ID of area
        projection : dict or str
            Projection parameters as a proj4_dict or proj4_string
        center : list
            Center of projection (x, y)
        radius : list or float
            Length from the center to the edges of the projection (dx, dy)
        resolution : list or float, optional
            Size of pixels: (dx, dy)
        shape : list, optional
            Number of pixels in the x and y direction (height, width)
        description : str, optional
            Description/name of area. Defaults to area_id
        proj_id : str, optional
            ID of projection
        units : str, optional
            Default projection units: meters, radians, or degrees
        rotation: float, optional
            rotation in degrees (negative is cw)
        nprocs : int, optional
            Number of processor cores to be used
        lons : numpy array, optional
            Grid lons
        lats : numpy array, optional
            Grid lats


        * **units** accepts '\xb0', 'deg', 'degrees', 'rad', 'radians', 'm', 'meters'. The order of default is:
            1. units expressed with each variable through a DataArray's attr attribute.
            2. units passed to **units**
            3. units used in **projection**
            4. meters
        * **resolution** and **radius** can be specified with one value if dx == dy

        Returns
        -------
        AreaDefinition or DynamicAreaDefinition : AreaDefinition or DynamicAreaDefinition
            If shape or resolution are provided, an AreaDefinition object is returned.
            Else a DynamicAreaDefinition object is returned
        """
        return utils.from_params(area_id, projection, center=center, radius=radius, shape=shape,
                                 resolution=resolution, **kwargs)

    @classmethod
    def from_area_of_interest(cls, area_id, projection, center, resolution, shape, **kwargs):
        """Create an AreaDefinition object from center, resolution, and shape.

        Parameters
        ----------
        area_id : str
            ID of area
        projection : dict or str
            Projection parameters as a proj4_dict or proj4_string
        center : list
            Center of projection (x, y)
        resolution : list or float
            Size of pixels: (dx, dy). Can be specified with one value if dx == dy
        shape : list
            Number of pixels in the x and y direction (height, width)
        description : str, optional
            Description/name of area. Defaults to area_id
        proj_id : str, optional
            ID of projection
        units : str, optional
            Default projection units: meters, radians, or degrees
        rotation: float, optional
            rotation in degrees (negative is cw)
        nprocs : int, optional
            Number of processor cores to be used
        lons : numpy array, optional
            Grid lons
        lats : numpy array, optional
            Grid lats


        * **units** accepts '\xb0', 'deg', 'degrees', 'rad', 'radians', 'm', 'meters'. The order of default is:
            1. units expressed with each variable through a DataArray's attr attribute.
            2. units passed to **units**
            3. units used in **projection**
            4. meters

        Returns
        -------
        AreaDefinition : AreaDefinition
        """
        return utils.from_params(area_id, projection, center=center, resolution=resolution, shape=shape, **kwargs)

    @classmethod
    def from_geotiff(cls, area_id, projection, top_left_extent, resolution, shape, **kwargs):
        """Create an AreaDefinition object from top_left_extent, resolution, and shape.

        Parameters
        ----------
        area_id : str
            ID of area
        projection : dict or str
            Projection parameters as a proj4_dict or proj4_string
        top_left_extent : list
            Upper left corner of upper left pixel (x, y)
        resolution : list or float
            Size of pixels in **meters**: (dx, dy). Can be specified with one value if dx == dy
        shape : list
            Number of pixels in the x and y direction (height, width)
        description : str, optional
            Description/name of area. Defaults to area_id
        proj_id : str, optional
            ID of projection
        units : str, optional
            Default projection units: meters, radians, or degrees
        rotation: float, optional
            rotation in degrees (negative is cw)
        nprocs : int, optional
            Number of processor cores to be used
        lons : numpy array, optional
            Grid lons
        lats : numpy array, optional
            Grid lats


        * **units** accepts '\xb0', 'deg', 'degrees', 'rad', 'radians', 'm', 'meters'. The order of default is:
            1. units expressed with each variable through a DataArray's attr attribute.
            2. units passed to **units**
            3. units used in **projection**
            4. meters

        Returns
        -------
        AreaDefinition : AreaDefinition
        """
        return utils.from_params(area_id, projection, top_left_extent=top_left_extent, resolution=resolution,
                                 shape=shape, **kwargs)

    def __hash__(self):
        """Compute the hash of this object."""
        if self.hash is None:
            self.hash = int(self.update_hash().hexdigest(), 16)
        return self.hash

    @property
    def proj_str(self):
        return utils.proj4_dict_to_str(self.proj_dict, sort=True)

    def __str__(self):
        # We need a sorted dictionary for a unique hash of str(self)
        proj_dict = self.proj_dict
        proj_str = ('{' +
                    ', '.join(["'%s': '%s'" % (str(k), str(proj_dict[k]))
                               for k in sorted(proj_dict.keys())]) +
                    '}')
        if not self.proj_id:
            third_line = ""
        else:
            third_line = "Projection ID: {0}\n".format(self.proj_id)
        return ('Area ID: {0}\nDescription: {1}\n{2}'
                'Projection: {3}\nNumber of columns: {4}\nNumber of rows: {5}\n'
                'Area extent: {6}').format(self.area_id, self.name, third_line,
                                           proj_str, self.x_size, self.y_size,
                                           tuple(round(x, 4) for x in self.area_extent))

    __repr__ = __str__

    def to_cartopy_crs(self):
        from pyresample._cartopy import from_proj
        bounds = (self.area_extent[0],
                  self.area_extent[2],
                  self.area_extent[1],
                  self.area_extent[3])
        crs = from_proj(self.proj_str, bounds=bounds)
        return crs

    def create_areas_def(self):
        to_dump = OrderedDict()
        res = OrderedDict()
        to_dump[self.area_id] = res

        res['description'] = self.name
        res['shape'] = OrderedDict([('height', self.y_size),
                                    ('width', self.x_size)])
        res['area_extent'] = OrderedDict([('lower_left_xy',
                                           list(self.area_extent[:2])),
                                          ('upper_right_xy',
                                           list(self.area_extent[2:])),
                                          ('units', 'm')
                                          ])

        return ordered_dump(to_dump)

    def create_areas_def_legacy(self):
        proj_dict = self.proj_dict
        proj_str = ','.join(["%s=%s" % (str(k), str(proj_dict[k]))
                             for k in sorted(proj_dict.keys())])

        fmt = "REGION: {name} {{\n"
        fmt += "\tNAME:\t{name}\n"
        fmt += "\tPCS_ID:\t{area_id}\n"
        fmt += "\tPCS_DEF:\t{proj_str}\n"
        fmt += "\tXSIZE:\t{x_size}\n"
        fmt += "\tYSIZE:\t{y_size}\n"
        # fmt += "\tROTATION:\t{rotation}\n"
        fmt += "\tAREA_EXTENT: {area_extent}\n}};\n"
        area_def_str = fmt.format(name=self.name, area_id=self.area_id,
                                  proj_str=proj_str, x_size=self.x_size,
                                  y_size=self.y_size,
                                  area_extent=self.area_extent)
        return area_def_str

    def __eq__(self, other):
        """Test for equality"""

        try:
            return ((self.proj_str == other.proj_str) and
                    (self.shape == other.shape) and
                    (np.allclose(self.area_extent, other.area_extent)))
        except AttributeError:
            return super(AreaDefinition, self).__eq__(other)

    def __ne__(self, other):
        """Test for equality"""

        return not self.__eq__(other)

    def update_hash(self, the_hash=None):
        """Update a hash, or return a new one if needed."""
        if the_hash is None:
            the_hash = hashlib.sha1()
        the_hash.update(self.proj_str.encode('utf-8'))
        the_hash.update(np.array(self.shape))
        the_hash.update(np.array(self.area_extent))
        return the_hash

    def colrow2lonlat(self, cols, rows):
        """
        Return longitudes and latitudes for the given image columns
        and rows. Both scalars and arrays are supported.
        To be used with scarse data points instead of slices
        (see get_lonlats).
        """
        p = Proj(self.proj_str)
        x = self.projection_x_coords
        y = self.projection_y_coords
        return p(y[y.size - cols], x[x.size - rows], inverse=True)

    def lonlat2colrow(self, lons, lats):
        """
        Return image columns and rows for the given longitudes
        and latitudes. Both scalars and arrays are supported.
        Same as get_xy_from_lonlat, renamed for convenience.
        """
        return self.get_xy_from_lonlat(lons, lats)

    def get_xy_from_lonlat(self, lon, lat):
        """Retrieve closest x and y coordinates (column, row indices) for the
        specified geolocation (lon,lat) if inside area. If lon,lat is a point a
        ValueError is raised if the return point is outside the area domain. If
        lon,lat is a tuple of sequences of longitudes and latitudes, a tuple of
        masked arrays are returned.

        :Input:

        lon : point or sequence (list or array) of longitudes
        lat : point or sequence (list or array) of latitudes

        :Returns:

        (x, y) : tuple of integer points/arrays
        """

        if isinstance(lon, list):
            lon = np.array(lon)
        if isinstance(lat, list):
            lat = np.array(lat)

        if ((isinstance(lon, np.ndarray) and
             not isinstance(lat, np.ndarray)) or (not isinstance(lon, np.ndarray) and isinstance(lat, np.ndarray))):
            raise ValueError("Both lon and lat needs to be of " +
                             "the same type and have the same dimensions!")

        if isinstance(lon, np.ndarray) and isinstance(lat, np.ndarray):
            if lon.shape != lat.shape:
                raise ValueError("lon and lat is not of the same shape!")

        pobj = Proj(self.proj_str)
        xm_, ym_ = pobj(lon, lat)

        return self.get_xy_from_proj_coords(xm_, ym_)

    def get_xy_from_proj_coords(self, xm, ym):
        """Find closest grid cell index for a specified projection coordinate.

        If xm, ym is a tuple of sequences of projection coordinates, a tuple
        of masked arrays are returned.

        Args:
            xm (list or array): point or sequence of x-coordinates in
                                 meters (map projection)
            ym (list or array): point or sequence of y-coordinates in
                                 meters (map projection)

        Returns:
            x, y : column and row grid cell indexes as 2 scalars or arrays

        Raises:
            ValueError: if the return point is outside the area domain

        """

        if isinstance(xm, list):
            xm = np.array(xm)
        if isinstance(ym, list):
            ym = np.array(ym)

        if ((isinstance(xm, np.ndarray) and
             not isinstance(ym, np.ndarray)) or (not isinstance(xm, np.ndarray) and isinstance(ym, np.ndarray))):
            raise ValueError("Both projection coordinates xm and ym needs to be of " +
                             "the same type and have the same dimensions!")

        if isinstance(xm, np.ndarray) and isinstance(ym, np.ndarray):
            if xm.shape != ym.shape:
                raise ValueError(
                    "projection coordinates xm and ym is not of the same shape!")

        upl_x = self.area_extent[0]
        upl_y = self.area_extent[3]
        xscale = (self.area_extent[2] -
                  self.area_extent[0]) / float(self.x_size)
        # because rows direction is the opposite of y's
        yscale = (self.area_extent[1] -
                  self.area_extent[3]) / float(self.y_size)

        x__ = (xm - upl_x) / xscale
        y__ = (ym - upl_y) / yscale

        if isinstance(x__, np.ndarray) and isinstance(y__, np.ndarray):
            mask = (((x__ < 0) | (x__ >= self.x_size)) |
                    ((y__ < 0) | (y__ >= self.y_size)))
            return (np.ma.masked_array(x__.astype('int'), mask=mask,
                                       fill_value=-1, copy=False),
                    np.ma.masked_array(y__.astype('int'), mask=mask,
                                       fill_value=-1, copy=False))
        else:
            if ((x__ < 0 or x__ >= self.x_size) or
                    (y__ < 0 or y__ >= self.y_size)):
                raise ValueError('Point outside area:( %f %f)' % (x__, y__))
            return int(x__), int(y__)

    def get_lonlat(self, row, col):
        """Retrieves lon and lat values of single point in area grid

        Parameters
        ----------
        row : int
        col : int

        Returns
        -------
        (lon, lat) : tuple of floats
        """

        lon, lat = self.get_lonlats(nprocs=None, data_slice=(row, col))
        return np.asscalar(lon), np.asscalar(lat)

    def get_proj_vectors_dask(self, chunks=CHUNK_SIZE, dtype=None):
        import dask.array as da
        if dtype is None:
            dtype = self.dtype

        if not isinstance(chunks, int):
            y_chunks = chunks[0]
            x_chunks = chunks[1]
        else:
            y_chunks = chunks
            x_chunks = chunks

        target_x = da.arange(self.x_size, chunks=x_chunks, dtype=dtype) * self.pixel_size_x +\
            self.top_left_extent[0] + self.pixel_size_x / 2
        target_y = da.arange(self.y_size, chunks=y_chunks, dtype=dtype) * - \
            self.pixel_size_y + self.top_left_extent[1] - self.pixel_size_y / 2
        return target_x, target_y

    def get_proj_coords_dask(self, chunks=CHUNK_SIZE, dtype=None):
        # TODO: Add rotation
        import dask.array as da
        target_x, target_y = self.get_proj_vectors_dask(chunks, dtype)
        return da.meshgrid(target_x, target_y)

    def get_proj_coords(self, data_slice=None, cache=False, dtype=None):
        """Get projection coordinates of grid.

        Parameters
        ----------
        data_slice : slice object, optional
            Calculate only coordinates for specified slice
        cache : bool, optional
            Store the result. Requires data_slice to be None

        Returns
        -------
        (target_x, target_y) : tuple of numpy arrays
            Grids of area x- and y-coordinates in projection units

        """

        def do_rotation(xspan, yspan, rot_deg=0):
            rot_rad = np.radians(rot_deg)
            rot_mat = np.array([[np.cos(rot_rad), np.sin(rot_rad)],
                                [-np.sin(rot_rad), np.cos(rot_rad)]])
            x, y = np.meshgrid(xspan, yspan)
            return np.einsum('ji, mni -> jmn', rot_mat, np.dstack([x, y]))

        def get_val(val, sub_val, max):
            # Get value with substitution and wrapping
            if val is None:
                return sub_val
            else:
                if val < 0:
                    # Wrap index
                    return max + val
                else:
                    return val

        if self._projection_x_coords is not None and self._projection_y_coords is not None:
            # Projection coords are cached
            if data_slice is None:
                return self._projection_x_coords, self._projection_y_coords
            else:
                return self._projection_x_coords[data_slice], self._projection_y_coords[data_slice]

        is_single_value = False
        is_1d_select = False

        if dtype is None:
            dtype = self.dtype

        target_x = np.arange(self.x_size, dtype=dtype) * self.pixel_size_x +\
            self.top_left_extent[0] + self.pixel_size_x / 2
        target_y = np.arange(self.y_size, dtype=dtype) * -self.pixel_size_y +\
            self.top_left_extent[1] - self.pixel_size_y / 2
        if data_slice is None or data_slice == slice(None):
            pass
        elif isinstance(data_slice, slice):
            target_y = target_y[data_slice]
        else:
            target_y = target_y[data_slice[0]]
            target_x = target_x[data_slice[1]]

        if self.rotation != 0:
            res = do_rotation(target_x, target_y, self.rotation)
            target_x, target_y = res[0, :, :], res[1, :, :]
        else:
            target_x, target_y = np.meshgrid(target_x, target_y)

        if is_single_value:
            # Return single values
            target_x = float(target_x)
            target_y = float(target_y)
        elif is_1d_select:
            # Reshape to 1D array
            target_x = target_x.reshape((target_x.size,))
            target_y = target_y.reshape((target_y.size,))

        if cache and data_slice is None:
            # Cache the result if requested
            self._projection_x_coords = target_x
            self._projection_y_coords = target_y

        return target_x, target_y

    @property
    def projection_x_coords(self):
        return self.get_proj_coords(data_slice=(0, slice(None)))[0].squeeze()

    @property
    def projection_y_coords(self):
        return self.get_proj_coords(data_slice=(slice(None), 0))[1].squeeze()

    @property
    def proj_x_coords(self):
        warnings.warn(
            "Deprecated, use 'projection_x_coords' instead", DeprecationWarning)
        return self.projection_x_coords

    @property
    def proj_y_coords(self):
        warnings.warn(
            "Deprecated, use 'projection_y_coords' instead", DeprecationWarning)
        return self.projection_y_coords

    @property
    def outer_boundary_corners(self):
        """Return the lon,lat of the outer edges of the corner points
        """
        from pyresample.spherical_geometry import Coordinate
        proj = Proj(**self.proj_dict)

        corner_lons, corner_lats = proj((self.area_extent[0], self.area_extent[2],
                                         self.area_extent[2], self.area_extent[0]),
                                        (self.area_extent[3], self.area_extent[3],
                                         self.area_extent[1], self.area_extent[1]),
                                        inverse=True)
        return [Coordinate(corner_lons[0], corner_lats[0]),
                Coordinate(corner_lons[1], corner_lats[1]),
                Coordinate(corner_lons[2], corner_lats[2]),
                Coordinate(corner_lons[3], corner_lats[3])]

    def get_lonlats_dask(self, chunks=CHUNK_SIZE, dtype=None):
        from dask.array import map_blocks

        dtype = dtype or self.dtype
        target_x, target_y = self.get_proj_coords_dask(chunks, dtype)

        res = map_blocks(invproj, target_x, target_y,
                         chunks=(target_x.chunks[0], target_x.chunks[1], 2),
                         new_axis=[2], proj_dict=self.proj_dict)

        return res[:, :, 0], res[:, :, 1]

    def get_lonlats(self, nprocs=None, data_slice=None, cache=False, dtype=None):
        """Return lon and lat arrays of area.

        Parameters
        ----------
        nprocs : int, optional
            Number of processor cores to be used.
            Defaults to the nprocs set when instantiating object
        data_slice : slice object, optional
            Calculate only coordinates for specified slice
        cache : bool, optional
            Store result the result. Requires data_slice to be None

        Returns
        -------
        (lons, lats) : tuple of numpy arrays
            Grids of area lons and and lats
        """

        if dtype is None:
            dtype = self.dtype

        if self.lons is None or self.lats is None:
            # Data is not cached
            if nprocs is None:
                nprocs = self.nprocs

            # Proj.4 definition of target area projection
            if nprocs > 1:
                target_proj = Proj_MP(**self.proj_dict)
            else:
                target_proj = Proj(**self.proj_dict)

            # Get coordinates of local area as ndarrays
            target_x, target_y = self.get_proj_coords(
                data_slice=data_slice, dtype=dtype)

            # Get corresponding longitude and latitude values
            lons, lats = target_proj(target_x, target_y, inverse=True,
                                     nprocs=nprocs)
            lons = np.asanyarray(lons, dtype=dtype)
            lats = np.asanyarray(lats, dtype=dtype)

            if cache and data_slice is None:
                # Cache the result if requested
                self.lons = lons
                self.lats = lats

            # Free memory
            del (target_x)
            del (target_y)
        else:
            # Data is cached
            if data_slice is None:
                # Full slice
                lons = self.lons
                lats = self.lats
            else:
                lons = self.lons[data_slice]
                lats = self.lats[data_slice]

        return lons, lats

    @property
    def proj4_string(self):
        """Return projection definition as Proj.4 string."""
        warnings.warn("'proj4_string' is deprecated, please use 'proj_str' "
                      "instead.", DeprecationWarning)
        return utils.proj4_dict_to_str(self.proj_dict)

    def get_area_slices(self, area_to_cover):
        """Compute the slice to read based on an `area_to_cover`."""
        if not isinstance(area_to_cover, AreaDefinition):
            raise NotImplementedError('Only AreaDefinitions can be used')

        # Intersection only required for two different projections
        if area_to_cover.proj_str == self.proj_str:
            logger.debug('Projections for data and slice areas are'
                         ' identical: %s',
                         area_to_cover.proj_dict.get('proj', area_to_cover.proj_dict.get('init')))
            # Get xy coordinates
            llx, lly, urx, ury = area_to_cover.area_extent
            x, y = self.get_xy_from_proj_coords([llx, urx], [lly, ury])

            xstart = 0 if x[0] is np.ma.masked else x[0]
            ystart = 0 if y[1] is np.ma.masked else y[1]
            xstop = self.x_size if x[1] is np.ma.masked else x[1] + 1
            ystop = self.y_size if y[0] is np.ma.masked else y[0] + 1

            return slice(xstart, xstop), slice(ystart, ystop)

        if self.proj_dict.get('proj') != 'geos':
            raise NotImplementedError("Source projection must be 'geos' if "
                                      "source/target projections are not "
                                      "equal.")

        data_boundary = Boundary(*get_geostationary_bounding_box(self))
        if area_to_cover.proj_dict.get('proj') == 'geos':
            area_boundary = Boundary(
                *get_geostationary_bounding_box(area_to_cover))
        else:
            area_boundary = AreaDefBoundary(area_to_cover, 100)

        intersection = data_boundary.contour_poly.intersection(
            area_boundary.contour_poly)
        if intersection is None:
            logger.debug('Cannot determine appropriate slicing.')
            raise NotImplementedError
        x, y = self.get_xy_from_lonlat(np.rad2deg(intersection.lon),
                                       np.rad2deg(intersection.lat))

        return (slice(np.ma.min(x), np.ma.max(x) + 1),
                slice(np.ma.min(y), np.ma.max(y) + 1))

    def crop_around(self, other_area):
        """Crop this area around `other_area`."""
        xslice, yslice = self.get_area_slices(other_area)
        return self[yslice, xslice]

    def __getitem__(self, key):
        """Apply slices to the area_extent and size of the area."""
        yslice, xslice = key
        new_area_extent = ((self.top_left_extent[0] + self.pixel_size_x / 2 +
                            (xslice.start - 0.5) * self.pixel_size_x),
                           (self.top_left_extent[1] - self.pixel_size_y / 2 -
                            (yslice.stop - 0.5) * self.pixel_size_y),
                           (self.top_left_extent[0] + self.pixel_size_x / 2 +
                            (xslice.stop - 0.5) * self.pixel_size_x),
                           (self.top_left_extent[1] - self.pixel_size_y / 2 -
                            (yslice.start - 0.5) * self.pixel_size_y))

        new_area = AreaDefinition(self.area_id, self.name,
                                  self.proj_id, self.proj_dict,
                                  xslice.stop - xslice.start,
                                  yslice.stop - yslice.start,
                                  new_area_extent)
        new_area.crop_offset = (self.crop_offset[0] + yslice.start,
                                self.crop_offset[1] + xslice.start)
        return new_area


def get_geostationary_angle_extent(geos_area):
    """Get the max earth (vs space) viewing angles in x and y."""

    # get some projection parameters
    req = geos_area.proj_dict['a'] / 1000
    rp = geos_area.proj_dict['b'] / 1000
    h = geos_area.proj_dict['h'] / 1000 + req

    # compute some constants
    aeq = 1 - req ** 2 / (h ** 2)
    ap_ = 1 - rp ** 2 / (h ** 2)

    # generate points around the north hemisphere in satellite projection
    # make it a bit smaller so that we stay inside the valid area
    xmax = np.arccos(np.sqrt(aeq))
    ymax = np.arccos(np.sqrt(ap_))
    return xmax, ymax


def get_geostationary_bounding_box(geos_area, nb_points=50):
    """Get the bbox in lon/lats of the valid pixels inside `geos_area`.

    Args:
      nb_points: Number of points on the polygon
    """
    xmax, ymax = get_geostationary_angle_extent(geos_area)

    # generate points around the north hemisphere in satellite projection
    # make it a bit smaller so that we stay inside the valid area
    x = np.cos(np.linspace(-np.pi, 0, int(nb_points / 2))) * (xmax - 0.0001)
    y = -np.sin(np.linspace(-np.pi, 0, int(nb_points / 2))) * (ymax - 0.0001)

    ll_x, ll_y, ur_x, ur_y = geos_area.area_extent

    x *= geos_area.proj_dict['h']
    y *= geos_area.proj_dict['h']

    x = np.clip(np.concatenate([x, x[::-1]]), min(ll_x, ur_x), max(ll_x, ur_x))
    y = np.clip(np.concatenate([y, -y]), min(ll_y, ur_y), max(ll_y, ur_y))

    return Proj(**geos_area.proj_dict)(x, y, inverse=True)


def combine_area_extents_vertical(area1, area2):
    """Combine the area extents of areas 1 and 2."""
    if (area1.area_extent[0] == area2.area_extent[0] and
            area1.area_extent[2] == area2.area_extent[2]):
        current_extent = list(area1.area_extent)
        if np.isclose(area1.area_extent[1], area2.area_extent[3]):
            current_extent[1] = area2.area_extent[1]
        elif np.isclose(area1.area_extent[3], area2.area_extent[1]):
            current_extent[3] = area2.area_extent[3]
    else:
        raise IncompatibleAreas(
            "Can't concatenate area definitions with "
            "incompatible area extents: "
            "{0} and {1}".format(area1, area2))
    return current_extent


def concatenate_area_defs(area1, area2, axis=0):
    """Append *area2* to *area1* and return the results."""
    different_items = (set(area1.proj_dict.items()) ^
                       set(area2.proj_dict.items()))
    if axis == 0:
        same_size = area1.x_size == area2.x_size
    else:
        raise NotImplementedError('Only vertical contatenation is supported.')
    if different_items or not same_size:
        raise IncompatibleAreas("Can't concatenate area definitions with "
                                "different projections: "
                                "{0} and {1}".format(area1, area2))

    if axis == 0:
        area_extent = combine_area_extents_vertical(area1, area2)
        x_size = int(area1.x_size)
        y_size = int(area1.y_size + area2.y_size)
    else:
        raise NotImplementedError('Only vertical contatenation is supported.')
    return AreaDefinition(area1.area_id, area1.name, area1.proj_id,
                          area1.proj_dict, x_size, y_size,
                          area_extent)


class StackedAreaDefinition(BaseDefinition):
    """Definition based on muliple vertically stacked AreaDefinitions."""

    def __init__(self, *definitions, **kwargs):
        """Base this instance on *definitions*.

        *kwargs* used here are `nprocs` and `dtype` (see AreaDefinition).
        """
        nprocs = kwargs.get('nprocs', 1)
        super(StackedAreaDefinition, self).__init__(nprocs=nprocs)
        self.dtype = kwargs.get('dtype', np.float64)
        self.defs = []
        self.proj_dict = {}
        for definition in definitions:
            self.append(definition)

    @property
    def x_size(self):
        return self.defs[0].x_size

    @property
    def y_size(self):
        return sum(definition.y_size for definition in self.defs)

    @property
    def size(self):
        return self.y_size * self.x_size

    def append(self, definition):
        """Append another definition to the area."""
        if isinstance(definition, StackedAreaDefinition):
            for area in definition.defs:
                self.append(area)
            return
        if definition.y_size == 0:
            return
        if not self.defs:
            self.proj_dict = definition.proj_dict
        elif self.proj_dict != definition.proj_dict:
            raise NotImplementedError('Cannot append areas:'
                                      ' Proj.4 dict mismatch')
        try:
            self.defs[-1] = concatenate_area_defs(self.defs[-1], definition)
        except (IncompatibleAreas, IndexError):
            self.defs.append(definition)

    def get_lonlats(self, nprocs=None, data_slice=None, cache=False, dtype=None):
        """Return lon and lat arrays of the area."""

        llons = []
        llats = []
        try:
            row_slice, col_slice = data_slice
        except TypeError:
            row_slice = slice(0, self.y_size)
            col_slice = slice(0, self.x_size)
        offset = 0
        for definition in self.defs:
            local_row_slice = slice(max(row_slice.start - offset, 0),
                                    min(max(row_slice.stop - offset, 0),
                                        definition.y_size),
                                    row_slice.step)
            lons, lats = definition.get_lonlats(nprocs=nprocs,
                                                data_slice=(local_row_slice,
                                                            col_slice),
                                                cache=cache,
                                                dtype=dtype)

            llons.append(lons)
            llats.append(lats)
            offset += lons.shape[0]

        self.lons = np.vstack(llons)
        self.lats = np.vstack(llats)

        return self.lons, self.lats

    def get_lonlats_dask(self, chunks=CHUNK_SIZE, dtype=None):
        """"Return lon and lat dask arrays of the area."""
        import dask.array as da
        llons = []
        llats = []
        for definition in self.defs:
            lons, lats = definition.get_lonlats_dask(chunks=chunks,
                                                     dtype=dtype)

            llons.append(lons)
            llats.append(lats)

        self.lons = da.concatenate(llons, axis=0)
        self.lats = da.concatenate(llats, axis=0)

        return self.lons, self.lats

    def squeeze(self):
        """Generate a single AreaDefinition if possible."""
        if len(self.defs) == 1:
            return self.defs[0]
        else:
            return self

    @property
    def proj4_string(self):
        """Returns projection definition as Proj.4 string"""
        warnings.warn("'proj4_string' is deprecated, please use 'proj_str' "
                      "instead.", DeprecationWarning)
        return self.defs[0].proj_str

    @property
    def proj_str(self):
        """Returns projection definition as Proj.4 string"""
        return self.defs[0].proj_str

    def update_hash(self, the_hash=None):
        for areadef in self.defs:
            the_hash = areadef.update_hash(the_hash)
        return the_hash


def _get_slice(segments, shape):
    """Generator for segmenting a 1D or 2D array"""

    if not (1 <= len(shape) <= 2):
        raise ValueError('Cannot segment array of shape: %s' % str(shape))
    else:
        size = shape[0]
        slice_length = int(np.ceil(float(size) / segments))
        start_idx = 0
        end_idx = slice_length
        while start_idx < size:
            if len(shape) == 1:
                yield slice(start_idx, end_idx)
            else:
                yield (slice(start_idx, end_idx), slice(None))
            start_idx = end_idx
            end_idx = min(start_idx + slice_length, size)


def _flatten_cartesian_coords(cartesian_coords):
    """Flatten array to (n, 3) shape"""

    shape = cartesian_coords.shape
    if len(shape) > 2:
        cartesian_coords = cartesian_coords.reshape(shape[0] *
                                                    shape[1], 3)
    return cartesian_coords


def _get_highest_level_class(obj1, obj2):
    if (not issubclass(obj1.__class__, obj2.__class__) or
            not issubclass(obj2.__class__, obj1.__class__)):
        raise TypeError('No common superclass for %s and %s' %
                        (obj1.__class__, obj2.__class__))

    if obj1.__class__ == obj2.__class__:
        klass = obj1.__class__
    elif issubclass(obj1.__class__, obj2.__class__):
        klass = obj2.__class__
    else:
        klass = obj1.__class__
    return klass


def ordered_dump(data, stream=None, Dumper=yaml.Dumper, **kwds):
    class OrderedDumper(Dumper):
        pass

    def _dict_representer(dumper, data):
        return dumper.represent_mapping(
            yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
            data.items(), flow_style=False)

    OrderedDumper.add_representer(OrderedDict, _dict_representer)
    return yaml.dump(data, stream, OrderedDumper, **kwds)
