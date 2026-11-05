"""Stand-in for the subset of arcpy used by MapSheetMosaic.pyt.

Allows the toolbox logic to run on a machine without ArcGIS Pro:

- geometry is reduced to axis-aligned rectangles;
- rasters and cartograms are registered in RASTERS / FCS by the tests;
- geoprocessing calls are recorded in CALLS, and their outputs are
  registered so that a later tool can read them (Clip -> Mosaic);
- projection is a fixed translation (SHIFT), sufficient to exercise the
  extent snapping and grid alignment check of tool 2.

Raster-touching behaviour of the real tools (resampling, compression,
NoData writing) is not represented and must be validated in ArcGIS Pro.
"""

import os

import numpy as np

RASTERS = {}    # path -> dict(extent, cell, wkid, bands, ptype, nodata_px, source_nodata_px)
FCS = {}        # name -> dict(wkid, rows=[(rect, {field: value})])
LAYERS = []     # layers of the active map
CALLS = []      # (name, args, kwargs) of geoprocessing calls
EXISTING = set()
MESSAGES = []   # (level, text)
STATE = {"honour_extent": True, "active_map": True}
SHIFT = (-287000.3, -300000.7)
_ENV = []


class _Env(object):
    scratchFolder = None


env = _Env()

_DTYPES = {"U8": np.uint8, "U16": np.uint16, "S16": np.int16, "F32": np.float32}
_PTYPE_OF = {"8_BIT_UNSIGNED": "U8", "16_BIT_UNSIGNED": "U16",
             "16_BIT_SIGNED": "S16", "32_BIT_FLOAT": "F32"}


def reset():
    for container in (RASTERS, FCS, LAYERS, CALLS, EXISTING, MESSAGES, _ENV):
        container.clear()
    STATE.update(honour_extent=True, active_map=True)
    env.scratchFolder = None


# ---- messages / progressor --------------------------------------------------

def AddMessage(text):
    MESSAGES.append(("INFO", text))


def AddWarning(text):
    MESSAGES.append(("WARN", text))


def AddError(text):
    MESSAGES.append(("ERROR", text))


def SetProgressor(*args):
    pass


def SetProgressorPosition(*args):
    pass


# ---- spatial reference and geometry ------------------------------------------

class _GCS(object):
    def __init__(self, name):
        self.name = name


class SpatialReference(object):
    NAMES = {
        102165: ("Lisboa_Hayford_Gauss_IPCC", "GCS_Lisbon"),
        102164: ("Lisboa_Hayford_Gauss_IGeoE", "GCS_Lisbon"),
        3763: ("ETRS_1989_Portugal_TM06", "GCS_ETRS_1989"),
    }

    def __init__(self, wkid):
        self.factoryCode = wkid
        self.name, gcs = self.NAMES[wkid]
        self.GCS = _GCS(gcs)


class Extent(object):
    def __init__(self, xmin, ymin, xmax, ymax):
        self.XMin, self.YMin, self.XMax, self.YMax = xmin, ymin, xmax, ymax


class Point(object):
    def __init__(self, x, y):
        self.X, self.Y = x, y


def Array(points):
    return list(points)


class Polygon(object):
    def __init__(self, points, sr=None, rect=None):
        if rect is None:
            xs = [p.X for p in points]
            ys = [p.Y for p in points]
            rect = (min(xs), min(ys), max(xs), max(ys))
        self.rect, self.sr = rect, sr
        self.extent = Extent(*rect)
        self.area = (rect[2] - rect[0]) * (rect[3] - rect[1])

    def intersect(self, other, dimension):
        a, b = self.rect, other.rect
        r = (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
        if r[2] <= r[0] or r[3] <= r[1]:
            return None
        return Polygon(None, self.sr, r)

    def projectAs(self, sr, transformation=None):
        dx, dy = SHIFT
        r = self.rect
        return Polygon(None, sr, (r[0] + dx, r[1] + dy, r[2] + dx, r[3] + dy))


# ---- rasters -----------------------------------------------------------------

class Raster(object):
    def __init__(self, path):
        if path not in RASTERS:
            raise RuntimeError("ERROR 000732: cannot open raster {}".format(path))
        d = RASTERS[path]
        self.extent = Extent(*d["extent"])
        self.meanCellWidth = self.meanCellHeight = d["cell"]
        self.spatialReference = SpatialReference(d["wkid"])
        self.bandCount = d["bands"]
        self.pixelType = d["ptype"]


def RasterToNumPyArray(path, lower_left_corner=None, ncols=None, nrows=None,
                       nodata_to_value=None):
    """Window filled with 100; the first `nodata_px` cells of band 1 are 0
    and the next `source_nodata_px` cells are source NoData."""
    d = RASTERS[path]
    arr = np.full((d["bands"], nrows, ncols), 100, dtype=_DTYPES[d["ptype"]])
    k0, k1 = d.get("nodata_px", 0), d.get("source_nodata_px", 0)
    flat = arr[0].reshape(-1)
    flat[:k0] = 0
    if nodata_to_value is not None:
        flat[k0:k0 + k1] = nodata_to_value
    return arr[0] if d["bands"] == 1 else arr


class _Describe(object):
    def __init__(self, item):
        if item in FCS:
            self.spatialReference = SpatialReference(FCS[item]["wkid"])
        self.catalogPath = item


def Describe(item):
    return _Describe(item)


class _SearchCursor(object):
    def __init__(self, fc, fields):
        self.rows, self.fields = FCS[fc]["rows"], fields

    def __enter__(self):
        out = []
        for rect, attrs in self.rows:
            row = [Polygon(None, None, rect)]
            for f in self.fields[1:]:
                if f not in attrs:
                    raise RuntimeError("ERROR 000728: field {} does not exist".format(f))
                row.append(attrs[f])
            out.append(tuple(row))
        return iter(out)

    def __exit__(self, *exc):
        return False


class da(object):
    SearchCursor = _SearchCursor


class EnvManager(object):
    def __init__(self, **settings):
        self.settings = settings

    def __enter__(self):
        _ENV.append(self.settings)
        return self

    def __exit__(self, *exc):
        _ENV.pop()
        return False


def Exists(path):
    return path in EXISTING


def ListTransformations(from_sr, to_sr, *args):
    if from_sr.GCS.name == to_sr.GCS.name:
        return []
    return ["Lisboa_To_ETRS_1989_1", "Lisboa_To_ETRS_1989_NTv2"]


class management(object):
    @staticmethod
    def Delete(path):
        EXISTING.discard(path)

    @staticmethod
    def CopyFeatures(geometries, out):
        EXISTING.add(out)

    @staticmethod
    def Clip(in_raster, rectangle, out_raster, template, nodata, geom, extent):
        CALLS.append(("Clip", (in_raster, rectangle, out_raster, template, nodata,
                               geom, extent), {}))
        if "FAIL" in os.path.basename(in_raster):
            raise RuntimeError("ERROR 999999: simulated clip failure")
        src = RASTERS[in_raster]
        RASTERS[out_raster] = dict(src, extent=tuple(float(v) for v in rectangle.split()),
                                   nodata_px=0, source_nodata_px=0)
        EXISTING.add(out_raster)

    @staticmethod
    def CompositeBands(inputs, out_raster):
        CALLS.append(("CompositeBands", (list(inputs), out_raster), dict(_ENV[-1]) if _ENV else {}))
        RASTERS[out_raster] = dict(RASTERS[inputs[0]], bands=len(inputs))
        EXISTING.add(out_raster)

    @staticmethod
    def MosaicToNewRaster(inputs, folder, name, sr, pixel_type, cell, bands,
                          method, colormap):
        CALLS.append(("MosaicToNewRaster", (inputs, folder, name, sr, pixel_type,
                                            cell, bands, method, colormap), {}))
        e = _ENV[-1]["extent"]
        off = 0.0 if STATE["honour_extent"] else 0.1
        out = os.path.join(folder, name)
        RASTERS[out] = dict(extent=(e.XMin + off, e.YMin + off, e.XMax + off, e.YMax + off),
                            cell=cell, wkid=sr.factoryCode, bands=bands,
                            ptype=_PTYPE_OF.get(pixel_type, "U8"))
        EXISTING.add(out)

    @staticmethod
    def SetRasterProperties(path, **kw):
        CALLS.append(("SetRasterProperties", (path,), kw))

    @staticmethod
    def CalculateStatistics(*args):
        CALLS.append(("CalculateStatistics", args, {}))

    @staticmethod
    def BuildPyramids(*args):
        CALLS.append(("BuildPyramids", args, {}))


# ---- project / map -----------------------------------------------------------

class Layer(object):
    """Map layer; `longName` uses backslashes for group nesting, as in Pro."""

    def __init__(self, longName, dataSource=None, group=False, visible=True):
        self.longName, self.dataSource = longName, dataSource
        self.isGroupLayer, self.isRasterLayer = group, not group
        self.visible = visible


class _Map(object):
    def listLayers(self):
        return list(LAYERS)


class mp(object):
    class ArcGISProject(object):
        def __init__(self, which):
            self.activeMap = _Map() if STATE["active_map"] else None


# ---- tool parameters ---------------------------------------------------------

class _Filter(object):
    def __init__(self):
        self.type, self.list = None, []


class Parameter(object):
    def __init__(self, name, displayName, datatype, parameterType, direction,
                 multiValue=False):
        self.name, self.displayName, self.datatype = name, displayName, datatype
        self.parameterType, self.direction = parameterType, direction
        self.multiValue = multiValue
        self.value, self.values = None, None
        self.filter = _Filter()
        self.parameterDependencies = []
        self.altered = False
        self.error = None

    @property
    def valueAsText(self):
        if self.multiValue:
            return ";".join(str(v) for v in self.values) if self.values else None
        if self.value is None:
            return None
        if isinstance(self.value, SpatialReference):
            return self.value.name
        if isinstance(self.value, bool):
            return "true" if self.value else "false"
        return str(self.value)

    def setErrorMessage(self, message):
        self.error = message
