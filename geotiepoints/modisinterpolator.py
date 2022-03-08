#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright (c) 2018 PyTroll community

# Author(s):

#   Martin Raspaud <martin.raspaud@smhi.se>

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Interpolation of geographical tiepoints using the second order interpolation
scheme implemented in the CVIIRS software, as described here:
Compact VIIRS SDR Product Format User Guide (V1J)
http://www.eumetsat.int/website/wcm/idc/idcplg?IdcService=GET_FILE&dDocName=PDF_DMT_708025&RevisionSelectionMethod=LatestReleased&Rendition=Web
"""

import xarray as xr
import dask.array as da
import numpy as np
import warnings

from .geointerpolator import lonlat2xyz, xyz2lonlat
from .simple_modis_interpolator import scanline_mapblocks

R = 6371.
# Aqua scan width and altitude in km
scan_width = 10.00017
H = 705.


def compute_phi(zeta):
    return np.arcsin(R * np.sin(zeta) / (R + H))


def compute_theta(zeta, phi):
    return zeta - phi


def compute_zeta(phi):
    return np.arcsin((R + H) * np.sin(phi) / R)


def compute_expansion_alignment(satz_a, satz_b, satz_c, satz_d):
    """All angles in radians."""
    zeta_a = satz_a
    zeta_b = satz_b

    phi_a = compute_phi(zeta_a)
    phi_b = compute_phi(zeta_b)
    theta_a = compute_theta(zeta_a, phi_a)
    theta_b = compute_theta(zeta_b, phi_b)
    phi = (phi_a + phi_b) / 2
    zeta = compute_zeta(phi)
    theta = compute_theta(zeta, phi)
    # Workaround for tiepoints symetrical about the subsatellite-track
    denominator = np.where(theta_a == theta_b, theta_a * 2, theta_a - theta_b)

    c_expansion = 4 * (((theta_a + theta_b) / 2 - theta) / denominator)

    sin_beta_2 = scan_width / (2 * H)

    d = ((R + H) / R * np.cos(phi) - np.cos(zeta)) * sin_beta_2
    e = np.cos(zeta) - np.sqrt(np.cos(zeta) ** 2 - d ** 2)

    c_alignment = 4 * e * np.sin(zeta) / denominator

    return c_expansion, c_alignment


def get_corners(arr):
    arr_a = arr[:, :-1, :-1]
    arr_b = arr[:, :-1, 1:]
    arr_c = arr[:, 1:, 1:]
    arr_d = arr[:, 1:, :-1]
    return arr_a, arr_b, arr_c, arr_d


class ModisInterpolator:

    def __init__(self, cres, fres, cscan_full_width=None):
        if cres == 1000:
            self.cscan_len = 10
            self.cscan_width = 1
            self.cscan_full_width = 1354
        elif cres == 5000:
            self.cscan_len = 2
            self.cscan_width = 5
            if cscan_full_width is None:
                self.cscan_full_width = 271
            else:
                self.cscan_full_width = cscan_full_width

        self._cres = cres
        self._res_factor = cres // fres
        f_factor = {
            250: 4,
            500: 2,
            1000: 1,
        }[fres]
        self.fscan_width = f_factor * self.cscan_width
        self.fscan_full_width = 1354 * f_factor
        self.fscan_len = f_factor * 10 // self.cscan_len

    def interpolate(self, orig_lons, orig_lats, satz1):
        cscan_len = self.cscan_len
        cscan_full_width = self.cscan_full_width

        fscan_width = self.fscan_width
        fscan_len = self.fscan_len
        new_lons, new_lats = _interpolate(
            orig_lons,
            orig_lats,
            satz1,
            self._cres,
            self.cscan_len,
            self.cscan_width,
            self.cscan_full_width,
            self.fscan_len,
            self.fscan_width,
            self.fscan_full_width,
            res_factor=self._res_factor,
            rows_per_scan=self.cscan_len,
        )
        return new_lons, new_lats


@scanline_mapblocks
def _interpolate(
        lon1,
        lat1,
        satz1,
        cres,
        cscan_len,
        cscan_width,
        cscan_full_width,
        fscan_len,
        fscan_width,
        fscan_full_width,
        res_factor,
        rows_per_scan,
):
    get_coords = _get_coords_1km if cres == 1000 else _get_coords_5km
    expand_tiepoint_array = _expand_tiepoint_array_1km if cres == 1000 else _expand_tiepoint_array_5km
    scans = satz1.shape[0] // cscan_len
    satz1 = satz1.reshape((-1, cscan_len, cscan_full_width))

    satz_a, satz_b, satz_c, satz_d = get_corners(np.deg2rad(satz1))

    c_exp, c_ali = compute_expansion_alignment(satz_a, satz_b, satz_c, satz_d)

    x, y = get_coords(cscan_len, cscan_full_width, fscan_len, fscan_width, fscan_full_width, scans)
    i_rs, i_rt = np.meshgrid(x, y)

    p_os = 0
    p_ot = 0

    s_s = (p_os + i_rs) * 1. / fscan_width
    s_t = (p_ot + i_rt) * 1. / fscan_len

    cols = fscan_width
    lines = fscan_len

    c_exp_full = expand_tiepoint_array(cscan_width, cscan_full_width, fscan_width, c_exp, lines, cols)
    c_ali_full = expand_tiepoint_array(cscan_width, cscan_full_width, fscan_width, c_ali, lines, cols)

    a_track = s_t
    a_scan = (s_s + s_s * (1 - s_s) * c_exp_full + s_t * (1 - s_t) * c_ali_full)

    res = []
    datasets = lonlat2xyz(lon1, lat1)
    for data in datasets:
        data = data.reshape((-1, cscan_len, cscan_full_width))
        data_a, data_b, data_c, data_d = get_corners(data)
        data_a = expand_tiepoint_array(cscan_width, cscan_full_width, fscan_width, data_a, lines, cols)
        data_b = expand_tiepoint_array(cscan_width, cscan_full_width, fscan_width, data_b, lines, cols)
        data_c = expand_tiepoint_array(cscan_width, cscan_full_width, fscan_width, data_c, lines, cols)
        data_d = expand_tiepoint_array(cscan_width, cscan_full_width, fscan_width, data_d, lines, cols)

        data_1 = (1 - a_scan) * data_a + a_scan * data_b
        data_2 = (1 - a_scan) * data_d + a_scan * data_c
        data = (1 - a_track) * data_1 + a_track * data_2

        res.append(data)
    new_lons, new_lats = xyz2lonlat(*res)
    return new_lons.astype(lon1.dtype), new_lats.astype(lat1.dtype)


def _get_coords_1km(cscan_len, cscan_full_width, fscan_len, fscan_width, fscan_full_width, scans):
    y = (np.arange((cscan_len + 1) * fscan_len) % fscan_len) + .5
    y = y[fscan_len // 2:-(fscan_len // 2)]
    y[:fscan_len//2] = np.arange(-fscan_len/2 + .5, 0)
    y[-(fscan_len//2):] = np.arange(fscan_len + .5, fscan_len * 3 / 2)
    y = np.tile(y, scans)

    x = np.arange(fscan_full_width) % fscan_width
    x[-fscan_width:] = np.arange(fscan_width, fscan_width * 2)
    return x, y


def _get_coords_5km(cscan_len, cscan_full_width, fscan_len, fscan_width, fscan_full_width, scans):
    y = np.arange(fscan_len * cscan_len) - 2
    y = np.tile(y, scans)

    x = (np.arange(fscan_full_width) - 2) % fscan_width
    x[0] = -2
    x[1] = -1
    if cscan_full_width == 271:
        x[-2] = 5
        x[-1] = 6
    elif cscan_full_width == 270:
        x[-7] = 5
        x[-6] = 6
        x[-5] = 7
        x[-4] = 8
        x[-3] = 9
        x[-2] = 10
        x[-1] = 11
    else:
        raise NotImplementedError("Can't interpolate if 5km tiepoints have less than 270 columns.")
    return x, y


def _expand_tiepoint_array_1km(cscan_width, cscan_full_width, fscan_width, arr, lines, cols):
    arr = np.repeat(arr, lines, axis=1)
    arr = np.concatenate((arr[:, :lines//2, :], arr, arr[:, -(lines//2):, :]), axis=1)
    arr = np.repeat(arr.reshape((-1, cscan_full_width - 1)), cols, axis=1)
    return np.hstack((arr, arr[:, -cols:]))


def _expand_tiepoint_array_5km(cscan_width, cscan_full_width, fscan_width, arr, lines, cols):
    arr = np.repeat(arr, lines * 2, axis=1)
    arr = np.repeat(arr.reshape((-1, cscan_full_width - 1)), cols, axis=1)
    factor = fscan_width // cscan_width
    if cscan_full_width == 271:
        return np.hstack((arr[:, :2 * factor], arr, arr[:, -2 * factor:]))
    else:
        return np.hstack((arr[:, :2 * factor], arr, arr[:, -fscan_width:], arr[:, -2 * factor:]))


def modis_1km_to_250m(lon1, lat1, satz1):
    """Interpolate MODIS geolocation from 1km to 250m resolution."""
    interp = ModisInterpolator(1000, 250)
    return interp.interpolate(lon1, lat1, satz1)


def modis_1km_to_500m(lon1, lat1, satz1):
    """Interpolate MODIS geolocation from 1km to 500m resolution."""
    interp = ModisInterpolator(1000, 500)
    return interp.interpolate(lon1, lat1, satz1)


def modis_5km_to_1km(lon1, lat1, satz1):
    """Interpolate MODIS geolocation from 5km to 1km resolution."""
    interp = ModisInterpolator(5000, 1000, lon1.shape[1])
    return interp.interpolate(lon1, lat1, satz1)


def modis_5km_to_500m(lon1, lat1, satz1):
    """Interpolate MODIS geolocation from 5km to 500m resolution."""
    warnings.warn("Interpolating 5km geolocation to 500m resolution "
                  "may result in poor quality")
    interp = ModisInterpolator(5000, 500, lon1.shape[1])
    return interp.interpolate(lon1, lat1, satz1)


def modis_5km_to_250m(lon1, lat1, satz1):
    """Interpolate MODIS geolocation from 5km to 250m resolution."""
    warnings.warn("Interpolating 5km geolocation to 250m resolution "
                  "may result in poor quality")
    interp = ModisInterpolator(5000, 250, lon1.shape[1])
    return interp.interpolate(lon1, lat1, satz1)
