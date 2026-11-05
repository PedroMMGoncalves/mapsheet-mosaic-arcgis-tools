# -*- coding: utf-8 -*-
"""
MapSheetMosaic.pyt
==================

ArcGIS Pro Python toolbox for archives of scanned, georeferenced map sheets.

Tool 1 identifies the cartogram sheet covered by each raster from its
georeferencing, clips the collar (the area outside the neat line) with the
sheet polygon and renames the result by sheet code. Tool 2 mosaics the
selected clipped sheets grouped by any cartogram field (e.g. the parent map
sheet at a smaller scale) and projects them to the output coordinate system
in the same step, so that each pixel is resampled only once.

Workflow
--------
1. Tool "1. Clip and Rename Sheets" with dry run ON  -> review the CSV plan.
2. Same tool with dry run OFF                        -> clipped sheets + CSV.
3. Add the clipped sheets to a map and switch off the copies that are not
   to be used (the CSV `use` column flags sheets with a single copy).
4. Tool "2. Mosaic Sheets by Map"                    -> one mosaic per group.

Dependencies
------------
- ArcGIS Pro 3.x with its default Python environment (arcpy, numpy, and
  GDAL/osgeo to detect colour tables); Python standard library otherwise.
  No additional packages.
- Clip, Composite Bands, Mosaic To New Raster, Set Raster Properties,
  Calculate Statistics and Build Pyramids are Data Management tools
  available at every licence level.
- Tool 2 reads the CURRENT project (visible layers) and therefore runs in
  the foreground only.

Design decisions
----------------
- Sheet identity is derived from georeferencing (largest overlap between
  raster extent and cartogram polygon), never from the file name.
- Tool 1 output name is PREFIX_CODE.tif; further copies of the same sheet
  receive _dup1, _dup2, ... in sorted input order. The file name is used
  only to extract a "variant" label, which is recorded in the CSV.
- Clipping is performed in the native CRS with NO_MAINTAIN_EXTENT, which
  keeps the source cell grid (no resampling in tool 1).
- Projection and datum transformation are applied once, in tool 2.
- A single NoData value (parameter, default 0) is assigned to all bands.
  Tool 1 optionally counts source pixels inside the sheet that already
  carry this value, since they become NoData after clipping.
- Pixel type and band count of the mosaics are taken from the inputs.
  When single-band greyscale rasters (no colour table) occur among
  three-band rasters of the same pixel type, tool 2 mosaics temporary
  three-band copies of them (band repeated with Composite Bands, values and
  grid unchanged); the copies are deleted after each mosaic and the clipped
  sheets of tool 1 are left as they are. Optional, on by default.
- No radiometric balancing: each scan keeps its original tones.
- The choice between duplicate scans is left to the operator; neither tool
  selects copies automatically.
- Source rasters are never modified; outputs are written to a separate
  folder. Existing outputs are skipped, so interrupted runs can be resumed.
- Each run writes a timestamped .log and .csv to the output folder.

Collection-specific defaults
----------------------------
No sheet size, map scale or file naming scheme is assumed by the code. The
default parameter values correspond to the first collection processed and
must be reviewed for other collections:
- Sheet code field COD_50255k; CSV / grouping fields Folha_25k and
  Folha_50k (cartogram of 1:5000 sheets of 2 x 2 km in Lisboa
  Hayford-Gauss IPCC, ESRI:102165).
- Variant regular expression ^[^_]+_[^_]+_(?P<var>.+)$, written for file
  names of the form O_<M><P>_<variant> (e.g. O_+30-170_MAG2 -> MAG2).
- Output coordinate system ETRS89 / PT-TM06 (EPSG:3763) and cell size
  0.4 m (scans at 300 dpi, source cell about 0.42 m).
- Raster file extensions .tif and .tiff.

Methodological limitations
--------------------------
- The raster footprint is its axis-aligned extent. For rectified scans this
  includes the NoData corners, so `coverage_pct` (intersection area / sheet
  area) can overestimate the area covered by image data. When a raster
  overlaps two sheets by exactly the same area, the first sheet in
  cartogram order is taken.
- Rasters and cartogram must share the CRS (compared by WKID, else by
  name); no on-the-fly projection is made for identification.
- The NoData count reads the sheet window into memory (rows x columns x
  bands x bytes per cell; about 68 MB for a 2 x 2 km sheet at 0.42 m with
  three 8-bit bands) and assumes north-up rasters without rotation terms.
  A pixel is counted if it equals the NoData value in at least one band.
  NoData is stored per band, so such a pixel loses only the affected
  band(s), which appears as a hole or as a colour defect.
- The NoData value must be representable in the pixel type of integer
  rasters; otherwise tool 1 does not clip the raster (state ERROR) and
  tool 2 stops.
- Mosaic To New Raster has no NoData or background parameter. The NoData
  value is assigned afterwards with Set Raster Properties, which assumes
  that cells not covered by any input are written with that value. This
  requires verification for NoData values other than 0. The same NoData
  value should be used in both tools.
- The mosaic cell size is fixed (default 0.4 output CRS units). Coarser
  inputs are upsampled, which adds pixels but no information.
- Alignment of the mosaic grid to multiples of the cell size is requested
  through the extent environment and verified after writing; a warning is
  issued when the grid is not aligned.
- Where inputs overlap (sheet boundaries) the mosaic keeps the last input
  in sorted order (mosaic method LAST).
- Datums are compared by geographic CRS name. When they differ, a datum
  transformation is always applied explicitly; a transformation whose
  name contains "NTv2" is preferred when listed.
- All inputs of a tool 2 run must share pixel type and band count, except
  for the expansion of single-band greyscale rasters described above.
  Palette rasters (colour table), rasters whose colour table cannot be read
  with GDAL, and other band-count combinations stop the run.
- A greyscale sheet expanded to three bands has R = G = B; it is displayed
  as in the original scan but carries no colour information.
- Tool 1 lists raster files by extension in a folder tree; rasters stored
  inside geodatabases are not listed. Tool 1 always writes GeoTIFF.
"""

import collections
import csv
import datetime
import math
import os
import re
import time
import warnings
from types import SimpleNamespace

import arcpy
import numpy as np


TOOLBOX_VERSION = "0.1.0"  # same value as `version:` in CITATION.cff

CSV_DELIM = ";"  # list separator of spreadsheet software in decimal-comma locales
# Tool 1 CSV: these columns, with the selected cartogram fields in between
CLIP_CSV_HEAD = ["prefix", "original_file", "original_path", "code"]
CLIP_CSV_TAIL = [
    "variant", "coverage_pct", "cell_x", "cell_y", "bands", "pixel_type",
    "n_pix_nodata", "output_name", "output_path", "state", "n_copies", "use",
    "t_check_s", "t_clip_s", "notes",
]
COPY_STATES = ("OK", "LOW_COVERAGE")  # states that count as a usable copy of a sheet
MOSAIC_CSV_COLUMNS = [
    "group_field", "group_value", "n_sheets", "codes", "missing", "rasters",
    "output_path", "pixel_type", "bands", "transformation", "cell_size",
    "t_mosaic_s", "state", "notes",
]

# Defaults of the first collection processed (see module docstring)
DEFAULT_CODE_FIELD = "COD_50255k"
DEFAULT_ATTR_FIELDS = ["Folha_25k", "Folha_50k"]
DEFAULT_GROUP_FIELD = "Folha_25k"
DEFAULT_VARIANT_REGEX = r"^[^_]+_[^_]+_(?P<var>.+)$"
DEFAULT_EXTENSIONS = ".tif;.tiff"
DEFAULT_OUT_WKID = 3763
DEFAULT_CELL_SIZE = 0.4

TMP_CLIP_FC = r"memory\clip_sheet"

# arcpy Raster.pixelType -> (Mosaic To New Raster pixel_type, min, max).
# min / max bound the NoData value of integer types (None: floating point).
PIXEL_TYPES = {
    "U1": ("1_BIT", 0, 1),
    "U2": ("2_BIT", 0, 3),
    "U4": ("4_BIT", 0, 15),
    "U8": ("8_BIT_UNSIGNED", 0, 255),
    "S8": ("8_BIT_SIGNED", -128, 127),
    "U16": ("16_BIT_UNSIGNED", 0, 65535),
    "S16": ("16_BIT_SIGNED", -32768, 32767),
    "U32": ("32_BIT_UNSIGNED", 0, 2 ** 32 - 1),
    "S32": ("32_BIT_SIGNED", -2 ** 31, 2 ** 31 - 1),
    "F32": ("32_BIT_FLOAT", None, None),
    "F64": ("64_BIT", None, None),
}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _norm(value):
    """Normalise attribute values to clean strings (609.0 -> '609')."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _num(value):
    """Return an int for integral numbers (0.0 -> 0), else the float."""
    value = float(value)
    return int(value) if value.is_integer() else value


def _safe(text):
    """Keep only characters that are safe in file names."""
    return re.sub(r"[^A-Za-z0-9_\-]", "_", text).strip("_")


def _same_sr(a, b):
    """Compare spatial references by WKID when available, else by name."""
    if a.factoryCode and b.factoryCode:
        return a.factoryCode == b.factoryCode
    return a.name == b.name


def _same_path(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _nodata_fits(pixel_type, value):
    """True if `value` can be stored in a raster of `pixel_type`.

    Unknown pixel types are accepted (the geoprocessing tool decides).
    """
    _name, lo, hi = PIXEL_TYPES.get(pixel_type, (None, None, None))
    if lo is None:
        return True
    return float(value).is_integer() and lo <= value <= hi


def _parse_extensions(text):
    """'.tif; TIFF, jp2' -> ('.jp2', '.tif', '.tiff')."""
    items = re.split(r"[;,\s]+", text or "")
    return tuple(sorted({"." + x.lower().lstrip(".") for x in items if x.strip(".")}))


def _split_multi(text):
    """Split a multivalue parameter string ("a;'b c'") into items."""
    items = [x.strip().strip("'") for x in (text or "").split(";")]
    return [x for x in items if x]


def _add_note(row, text):
    """Append `text` to the `notes` column of a CSV row."""
    row["notes"] = "; ".join(x for x in (row["notes"], text) if x)


def _timestamp():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def _fmt_dur(seconds):
    """Format seconds as H:MM:SS."""
    seconds = int(round(seconds))
    return "{}:{:02d}:{:02d}".format(seconds // 3600, (seconds % 3600) // 60, seconds % 60)


def _counter(i, n):
    """Zero-padded item counter of constant width, e.g. [007/254]."""
    w = len(str(n))
    return "[{:0{w}d}/{:0{w}d}]".format(i, n, w=w)


def _width(names):
    """Length of the longest string (0 for an empty sequence)."""
    return max([len(s) for s in names] or [0])


class RunLog(object):
    """Write messages to the Geoprocessing pane and to a .log file.

    The file is flushed after every line, so it survives a crash or a
    cancelled run and shows exactly how far processing went.

    Column alignment: the level tag is padded to a constant width and
    callers pad names to the longest name of the run, so each line type
    forms fixed-width columns in a monospaced viewer.
    """

    def __init__(self, path):
        self.path = path
        self.t0 = time.perf_counter()
        self._f = open(path, "a", encoding="utf-8")

    def _write(self, level, text):
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._f.write("{} {:<7} {}\n".format(stamp, "[" + level + "]", text))
        self._f.flush()

    def info(self, text):
        self._write("INFO", text)
        arcpy.AddMessage(text)

    def warn(self, text):
        self._write("WARN", text)
        arcpy.AddWarning(text)

    def error(self, text):
        self._write("ERROR", text)
        arcpy.AddError(text)

    def progress(self, i, n, label, step_s, note=""):
        """Log item i of n with its own duration, elapsed total and ETA.

        `label` must already be padded by the caller. `note` (free text,
        e.g. an error message) is appended last so it cannot shift the
        fixed-width columns.
        """
        elapsed = time.perf_counter() - self.t0
        eta = elapsed / i * (n - i) if i else 0.0
        line = "{} {} | {:>6.1f} s | elapsed {:>8} | ETA {:>8}".format(
            _counter(i, n), label, step_s, _fmt_dur(elapsed), _fmt_dur(eta))
        self.info(line + (" | " + note if note else ""))

    def close(self):
        self.info("Total time: {} | log: {}".format(
            _fmt_dur(time.perf_counter() - self.t0), self.path))
        self._f.close()


def _list_rasters(folder, recursive, exts, exclude=None):
    """List files ending in one of `exts`, sorted case-insensitively.

    `exclude` (a folder) and its subfolders are skipped, so outputs written
    inside the input tree are not reprocessed.
    """
    ex = os.path.normcase(os.path.abspath(exclude)) if exclude else None

    def excluded(root):
        root = os.path.normcase(os.path.abspath(root))
        return ex is not None and (root == ex or root.startswith(ex + os.sep))

    found = []
    if recursive:
        for root, _dirs, files in os.walk(folder):
            if not excluded(root):
                found += [os.path.join(root, f) for f in files
                          if f.lower().endswith(exts)]
    elif not excluded(folder):
        found = [os.path.join(folder, f) for f in os.listdir(folder)
                 if f.lower().endswith(exts)
                 and os.path.isfile(os.path.join(folder, f))]
    return sorted(found, key=str.lower)


def _load_cartogram(fc, code_f, attr_fields=()):
    """Read cartogram polygons, sheet codes and the requested attributes."""
    sr = arcpy.Describe(fc).spatialReference
    names = list(dict.fromkeys([code_f] + list(attr_fields)))  # unique, ordered
    sheets = []
    with arcpy.da.SearchCursor(fc, ["SHAPE@"] + names) as cur:
        for row in cur:
            shp = row[0]
            if shp is None:
                continue
            vals = dict(zip(names, (_norm(v) for v in row[1:])))
            e = shp.extent
            sheets.append({
                "geom": shp, "code": vals[code_f], "attrs": vals,
                "xmin": e.XMin, "ymin": e.YMin, "xmax": e.XMax, "ymax": e.YMax,
                "area": shp.area,
            })
    return sr, sheets


def _footprint(ras):
    """Axis-aligned raster extent as a polygon in the raster CRS."""
    e = ras.extent
    pts = [(e.XMin, e.YMin), (e.XMin, e.YMax), (e.XMax, e.YMax),
           (e.XMax, e.YMin), (e.XMin, e.YMin)]
    return arcpy.Polygon(arcpy.Array([arcpy.Point(*p) for p in pts]),
                         ras.spatialReference)


def _best_sheet(fp, sheets):
    """Return (sheet, coverage_pct) of the sheet with the largest overlap."""
    e = fp.extent
    best, best_area = None, 0.0
    for s in sheets:
        # Cheap bounding-box rejection before the geometric intersection
        if s["xmax"] <= e.XMin or s["xmin"] >= e.XMax or \
           s["ymax"] <= e.YMin or s["ymin"] >= e.YMax:
            continue
        inter = fp.intersect(s["geom"], 4)
        if inter and inter.area > best_area:
            best, best_area = s, inter.area
    if best is None:
        return None, 0.0
    return best, 100.0 * best_area / best["area"]


def _count_nodata(path, ras, sheet, nodata):
    """Count pixels of the sheet window equal to `nodata` in any band.

    After clipping with this NoData value such pixels become NoData in the
    affected band(s). Source NoData (e.g. rectification corners) is read as
    a different value, so it is not counted.
    """
    e = ras.extent
    xmin, ymin = max(e.XMin, sheet["xmin"]), max(e.YMin, sheet["ymin"])
    xmax, ymax = min(e.XMax, sheet["xmax"]), min(e.YMax, sheet["ymax"])
    ncols = int(math.floor((xmax - xmin) / ras.meanCellWidth))
    nrows = int(math.floor((ymax - ymin) / ras.meanCellHeight))
    if ncols <= 0 or nrows <= 0:
        return 0
    sentinel = nodata - 1 if nodata > 0 else nodata + 1  # any value != nodata
    arr = arcpy.RasterToNumPyArray(path, arcpy.Point(xmin, ymin), ncols, nrows,
                                   nodata_to_value=sentinel)
    if arr.ndim == 2:  # single-band raster
        arr = arr[np.newaxis]
    return int(np.any(arr == nodata, axis=0).sum())


def _clip(in_path, sheet, out_path, nodata):
    """Clip by the sheet polygon with the given NoData, keeping the source grid."""
    if arcpy.Exists(TMP_CLIP_FC):
        arcpy.management.Delete(TMP_CLIP_FC)
    arcpy.management.CopyFeatures([sheet["geom"]], TMP_CLIP_FC)
    rect = "{} {} {} {}".format(sheet["xmin"], sheet["ymin"],
                                sheet["xmax"], sheet["ymax"])
    arcpy.management.Clip(in_path, rect, out_path, TMP_CLIP_FC, str(nodata),
                          "ClippingGeometry", "NO_MAINTAIN_EXTENT")


def _has_colour_table(path):
    """True if band 1 carries a colour table (palette raster), False if it
    does not (plain greyscale), None if this cannot be determined (GDAL not
    importable or format not readable by GDAL)."""
    try:
        with warnings.catch_warnings():  # GDAL exception-mode FutureWarning
            warnings.simplefilter("ignore", FutureWarning)
            from osgeo import gdal  # bundled with ArcGIS Pro
            ds = gdal.Open(path)
            if ds is None:
                return None
            return ds.GetRasterBand(1).GetColorTable() is not None
    except Exception:
        return None


def _expand_to_rgb(path, out_path, nodata):
    """Write a three-band copy of a single-band greyscale raster.

    The band is repeated three times with Composite Bands, so cell values
    and grid are copied unchanged (no resampling), and the NoData value is
    set on all bands. Environment settings that could project, clip or snap
    the copy are cleared for the call.
    """
    if arcpy.Exists(out_path):
        arcpy.management.Delete(out_path)
    with arcpy.EnvManager(outputCoordinateSystem=None, extent=None, snapRaster=None,
                          pyramid="NONE", rasterStatistics="NONE"):
        arcpy.management.CompositeBands([path, path, path], out_path)
    arcpy.management.SetRasterProperties(
        out_path, nodata="1 {0};2 {0};3 {0}".format(nodata))


def _param(name, label, dtype, direction="Input", ptype="Required",
           value=None, multi=False):
    p = arcpy.Parameter(name=name, displayName=label, datatype=dtype,
                        parameterType=ptype, direction=direction,
                        multiValue=multi)
    if value is not None:
        p.value = value
    return p


def _by_name(params):
    """Index tool parameters by name."""
    return {p.name: p for p in params}


def _check_prefix(p):
    if p.value and _safe(p.valueAsText) != p.valueAsText:
        p.setErrorMessage("Use letters, digits, '_' or '-' only.")


# --------------------------------------------------------------------------
# Toolbox
# --------------------------------------------------------------------------

class Toolbox(object):
    def __init__(self):
        self.label = "Map Sheet Mosaic"
        self.alias = "mapsheetmosaic"
        self.tools = [ClipSheets, MosaicSheets]


# --------------------------------------------------------------------------
# Tool 1
# --------------------------------------------------------------------------

class ClipSheets(object):
    """Identify each raster's sheet by georeferencing, clip, rename, log."""

    def __init__(self):
        self.label = "1. Clip and Rename Sheets"
        self.description = ("Identifies the cartogram sheet covered by each raster "
                            "from its georeferencing (largest overlap), clips the "
                            "collar with the sheet polygon and writes "
                            "PREFIX_CODE.tif plus a CSV report and a log.")
        self.canRunInBackground = False

    def getParameterInfo(self):
        p_in = _param("in_folder", "Input raster folder", "DEFolder")
        p_rec = _param("recursive", "Include subfolders", "GPBoolean", value=True)
        p_ext = _param("extensions", "Raster file extensions (separated by ;)",
                       "GPString", value=DEFAULT_EXTENSIONS)
        p_fc = _param("cartogram", "Cartogram (sheet polygons)", "GPFeatureLayer")
        p_code = _param("code_field", "Sheet code field", "Field",
                        value=DEFAULT_CODE_FIELD)
        p_attr = _param("attr_fields", "Cartogram fields copied to the CSV",
                        "Field", ptype="Optional", multi=True)
        p_attr.values = DEFAULT_ATTR_FIELDS
        for p in (p_code, p_attr):
            p.parameterDependencies = [p_fc.name]
        p_pre = _param("prefix", "Output prefix (e.g. MAG)", "GPString")
        p_rx = _param("variant_regex", "Variant regular expression (group 'var')",
                      "GPString", value=DEFAULT_VARIANT_REGEX)
        p_out = _param("out_folder", "Output folder", "DEFolder")
        p_cov = _param("min_coverage", "Minimum sheet coverage (%)", "GPDouble",
                       value=99.0)
        p_nd = _param("nodata", "NoData value (all bands)", "GPDouble", value=0)
        p_chk = _param("check_nodata",
                       "Count source pixels equal to NoData inside the sheet",
                       "GPBoolean", value=True)
        p_dry = _param("dry_run", "Dry run (write CSV and log only)", "GPBoolean",
                       value=True)
        return [p_in, p_rec, p_ext, p_fc, p_code, p_attr, p_pre, p_rx,
                p_out, p_cov, p_nd, p_chk, p_dry]

    def updateMessages(self, params):
        P = _by_name(params)
        _check_prefix(P["prefix"])
        rx = P["variant_regex"]
        if rx.value:
            try:
                if "var" not in re.compile(rx.valueAsText).groupindex:
                    rx.setErrorMessage("The expression must define a group (?P<var>...).")
            except re.error as exc:
                rx.setErrorMessage("Invalid regular expression: {}".format(exc))
        ext = P["extensions"]
        if ext.value and not _parse_extensions(ext.valueAsText):
            ext.setErrorMessage("List at least one extension, e.g. .tif;.tiff")
        p_in, p_out = P["in_folder"], P["out_folder"]
        if p_in.value and p_out.value and \
                _same_path(p_in.valueAsText, p_out.valueAsText):
            p_out.setErrorMessage("The output folder must differ from the input folder.")
        cov = P["min_coverage"]
        if cov.value is not None and not 0 <= cov.value <= 100:
            cov.setErrorMessage("Coverage must be between 0 and 100 %.")

    def execute(self, params, messages):
        P = _by_name(params)
        c = SimpleNamespace(
            in_folder=P["in_folder"].valueAsText,
            recursive=bool(P["recursive"].value),
            exts=_parse_extensions(P["extensions"].valueAsText),
            fc=P["cartogram"].valueAsText,
            code_f=P["code_field"].valueAsText,
            attr_fields=_split_multi(P["attr_fields"].valueAsText),
            prefix=P["prefix"].valueAsText,
            vrx=re.compile(P["variant_regex"].valueAsText),
            out_folder=P["out_folder"].valueAsText,
            min_cov=float(P["min_coverage"].value),
            nodata=_num(P["nodata"].value),
            check_nodata=bool(P["check_nodata"].value),
            dry_run=bool(P["dry_run"].value),
        )
        # One timestamp shared by the .log and the .csv of this run
        tag = "plan" if c.dry_run else "clip"
        run_id = "{}_{}_{}".format(c.prefix, tag, _timestamp())
        log = RunLog(os.path.join(c.out_folder, run_id + ".log"))
        try:
            log.info("Tool: 1. Clip and Rename Sheets | Map Sheet Mosaic {} | dry run={}".format(
                TOOLBOX_VERSION, c.dry_run))
            log.info("Input: {} (subfolders={}, extensions {}) | output: {}".format(
                c.in_folder, c.recursive, " ".join(c.exts), c.out_folder))
            log.info("Cartogram: {} | code field {} | CSV fields: {} | NoData {}".format(
                c.fc, c.code_f, ", ".join(c.attr_fields) or "-", c.nodata))
            self._run(log, run_id, c)
        finally:
            log.close()

    def _run(self, log, run_id, c):
        carto_sr, sheets = _load_cartogram(c.fc, c.code_f, c.attr_fields)
        dup_codes = sorted(k for k, v in collections.Counter(
            s["code"] for s in sheets).items() if v > 1)
        if dup_codes:
            log.warn("Duplicate codes in the cartogram: {}".format(
                ", ".join(dup_codes[:20])))
        log.info("Cartogram: {} sheets, CRS {}".format(len(sheets), carto_sr.name))

        # CSV columns: fixed head, cartogram fields, fixed tail
        fixed = set(CLIP_CSV_HEAD + CLIP_CSV_TAIL)
        attr_cols = [(f, "field_" + f if f in fixed else f) for f in c.attr_fields]
        columns = CLIP_CSV_HEAD + [col for _f, col in attr_cols] + CLIP_CSV_TAIL

        rasters = _list_rasters(c.in_folder, c.recursive, c.exts, exclude=c.out_folder)
        log.info("Rasters found: {}".format(len(rasters)))
        if not rasters:
            return

        # ---- Pass 1: identify sheets and plan names (no writing) ----------
        t_pass1 = time.perf_counter()
        rows, used_names = [], set()
        n_ras = len(rasters)
        w_in = _width([os.path.basename(p) for p in rasters])
        arcpy.SetProgressor("step", "Identifying sheets...", 0, n_ras, 1)
        for k_ras, path in enumerate(rasters, 1):
            fname = os.path.basename(path)
            head = "{} {} |".format(_counter(k_ras, n_ras), fname.ljust(w_in))
            stem = os.path.splitext(fname)[0]
            m = c.vrx.match(stem)
            row = dict.fromkeys(columns, "")
            row.update(prefix=c.prefix, original_file=fname, original_path=path,
                       variant=_safe((m.group("var") or "") if m else stem), use="0")
            if not m:
                _add_note(row, "no regex match; variant = full file name")
            try:
                ras = arcpy.Raster(path)
                row.update(cell_x="{:.4f}".format(ras.meanCellWidth),
                           cell_y="{:.4f}".format(ras.meanCellHeight),
                           bands=str(ras.bandCount), pixel_type=ras.pixelType)
                if not _same_sr(ras.spatialReference, carto_sr):
                    row["state"] = "CRS_MISMATCH"
                    log.warn("{} CRS mismatch ({})".format(head, ras.spatialReference.name))
                    continue
                sheet, cov = _best_sheet(_footprint(ras), sheets)
                if sheet is None:
                    row["state"] = "NO_SHEET"
                    log.warn("{} does not intersect any cartogram sheet".format(head))
                    continue
                row.update(code=sheet["code"], coverage_pct="{:.2f}".format(cov))
                for f, col in attr_cols:
                    row[col] = sheet["attrs"][f]
                problem = ""
                if not sheet["code"]:
                    problem = "empty sheet code in the cartogram"
                elif not _nodata_fits(ras.pixelType, c.nodata):
                    problem = "NoData {} outside the range of pixel type {}".format(
                        c.nodata, ras.pixelType)
                if problem:
                    row["state"] = "ERROR"
                    _add_note(row, problem)
                    log.warn("{} {}".format(head, problem))
                    continue
                row["state"] = "OK" if cov >= c.min_cov else "LOW_COVERAGE"
                if row["state"] == "LOW_COVERAGE":
                    log.warn("{} coverage {:>5.1f} % of sheet {}".format(
                        head, cov, sheet["code"]))
                base = "{}_{}".format(c.prefix, _safe(sheet["code"]))
                name, k = base, 0
                while name.lower() in used_names:  # in-run collision -> _dupN
                    k += 1
                    name = "{}_dup{}".format(base, k)
                used_names.add(name.lower())
                row["output_name"] = name + ".tif"
                row["output_path"] = os.path.join(c.out_folder, name + ".tif")
                row["_sheet"], row["_ras"] = sheet, ras
            except Exception as exc:  # keep going; the CSV records the failure
                row["state"] = "ERROR"
                _add_note(row, str(exc)[:200])
                log.warn("{} identification error: {}".format(head, exc))
            finally:
                rows.append(row)
                arcpy.SetProgressorPosition()
        log.info("Identification completed in {}".format(
            _fmt_dur(time.perf_counter() - t_pass1)))

        # ---- Copies per sheet; `use` = 1 only for single-copy sheets OK --
        per_code = collections.Counter(
            r["code"] for r in rows if r["code"] and r["state"] in COPY_STATES)
        for r in rows:
            if r["code"] and r["state"] in COPY_STATES:
                r["n_copies"] = str(per_code[r["code"]])
            if r["code"] and r["state"] == "OK" and per_code[r["code"]] == 1:
                r["use"] = "1"

        # ---- Pass 2: clip (skipped in dry run) -----------------------------
        if not c.dry_run:
            todo = [r for r in rows if "_sheet" in r]
            n = len(todo)
            w_src = _width([r["original_file"] for r in todo])
            w_dst = _width([r["output_name"] for r in todo])
            log.info("Clipping {} rasters...".format(n))
            t_pass2 = time.perf_counter()
            arcpy.SetProgressor("step", "Clipping...", 0, n, 1)
            with arcpy.EnvManager(compression="LZW",
                                  pyramid="PYRAMIDS -1 BILINEAR",
                                  rasterStatistics="STATISTICS 1 1",
                                  overwriteOutput=False):
                for i, r in enumerate(todo, 1):
                    t_item = time.perf_counter()
                    names = "{} -> {}".format(r["original_file"].ljust(w_src),
                                              r["output_name"].ljust(w_dst))
                    note = ""
                    try:
                        if c.check_nodata:
                            t = time.perf_counter()
                            n0 = _count_nodata(r["original_path"], r["_ras"],
                                               r["_sheet"], c.nodata)
                            r["t_check_s"] = "{:.1f}".format(time.perf_counter() - t)
                            r["n_pix_nodata"] = str(n0)
                            if n0 > 0:
                                log.warn("{} {} | {} pixels equal to NoData ({}) "
                                         "in at least one band".format(
                                             _counter(i, n), names, n0, c.nodata))
                        if arcpy.Exists(r["output_path"]):
                            _add_note(r, "output exists, not rewritten")
                            status = "EXISTS"
                        else:
                            t = time.perf_counter()
                            _clip(r["original_path"], r["_sheet"], r["output_path"],
                                  c.nodata)
                            r["t_clip_s"] = "{:.1f}".format(time.perf_counter() - t)
                            status = "OK"
                    except Exception as exc:
                        r["state"] = "ERROR"
                        r["use"] = "0"
                        _add_note(r, str(exc)[:200])
                        status, note = "ERROR", str(exc)[:120]
                    log.progress(i, n, "{} | {:<6} | check {:>6} s | clip {:>6} s".format(
                        names, status, r["t_check_s"] or "-", r["t_clip_s"] or "-"),
                        time.perf_counter() - t_item, note)
                    arcpy.SetProgressorPosition()
            if arcpy.Exists(TMP_CLIP_FC):
                arcpy.management.Delete(TMP_CLIP_FC)
            log.info("Clipping completed in {}".format(
                _fmt_dur(time.perf_counter() - t_pass2)))

        # ---- CSV (timestamped, so operator edits are never overwritten) ---
        csv_path = os.path.join(c.out_folder, run_id + ".csv")
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=columns, delimiter=CSV_DELIM,
                               extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

        # ---- Summary -------------------------------------------------------
        states = collections.Counter(r["state"] for r in rows)
        log.info("States: " + ", ".join(
            "{}={}".format(k, v) for k, v in sorted(states.items())))
        # Tool 2 accepts one raster per cartogram sheet: list every sheet
        # that has several usable copies, with the data needed to choose.
        copies = collections.defaultdict(list)
        for r in rows:
            if r["code"] and r["state"] in COPY_STATES:
                copies[r["code"]].append(r)
        dups = sorted(k for k, v in copies.items() if len(v) > 1)
        if dups:
            log.warn("Sheets with more than one copy: {} (use=0). Tool 2 accepts one "
                     "raster per sheet: keep one copy of each sheet below.".format(len(dups)))
            w_code = _width(dups)
            for i, code in enumerate(dups, 1):
                log.warn("{} {} | {} copies | {}".format(
                    _counter(i, len(dups)), code.ljust(w_code), len(copies[code]),
                    ", ".join("{} [{}, cell {} m, {} band(s)]".format(
                        r["output_name"], r["variant"], r["cell_x"], r["bands"])
                        for r in copies[code])))
        else:
            log.info("Sheets with more than one copy: 0")
        log.info("CSV: {}".format(csv_path))


# --------------------------------------------------------------------------
# Tool 2
# --------------------------------------------------------------------------

def _visible_raster_layers():
    """Return data sources of raster layers visible in the active map.

    A layer counts as visible only if its own checkbox is on and no parent
    group layer is switched off (listLayers() flattens the tree, so
    ancestry is derived from longName).
    """
    m = arcpy.mp.ArcGISProject("CURRENT").activeMap
    if m is None:
        raise RuntimeError("No active map.")
    layers = m.listLayers()
    hidden = {l.longName for l in layers if l.isGroupLayer and not l.visible}
    paths = []
    for l in layers:
        if not (l.isRasterLayer and l.visible):
            continue
        parts = l.longName.split("\\")
        ancestors = {"\\".join(parts[:k]) for k in range(1, len(parts))}
        if not ancestors & hidden:
            paths.append(l.dataSource)
    return paths


def _pick_transformation(available):
    """Prefer an NTv2 grid transformation, else the first one listed."""
    for t in available:
        if "NTV2" in t.upper():
            return t
    return available[0] if available else ""


class MosaicSheets(object):
    """Mosaic clipped sheets grouped by a cartogram field, projecting once.

    Inputs are the rasters chosen by the operator: all raster layers visible
    in the active map whose file name starts with PREFIX_, and/or an
    explicit list. Each raster's sheet is identified again from its
    georeferencing against the cartogram, so the tool does not depend on
    file names or on the CSV of tool 1.
    """

    def __init__(self):
        self.label = "2. Mosaic Sheets by Map"
        self.description = ("Mosaics the raster layers visible in the active map "
                            "(and/or a list), grouped by a cartogram field such as "
                            "the parent map sheet, projecting with a single "
                            "resampling.")
        self.canRunInBackground = False  # needs the CURRENT project

    def getParameterInfo(self):
        p_vis = _param("use_visible",
                       "Use raster layers visible in the active map (file name PREFIX_*)",
                       "GPBoolean", value=True)
        p_ras = _param("rasters", "Additional rasters (optional)", "GPRasterLayer",
                       ptype="Optional", multi=True)
        p_fc = _param("cartogram", "Cartogram (sheet polygons)", "GPFeatureLayer")
        p_code = _param("code_field", "Sheet code field", "Field",
                        value=DEFAULT_CODE_FIELD)
        p_grp = _param("group_field", "Group by field", "Field",
                       value=DEFAULT_GROUP_FIELD)
        for p in (p_code, p_grp):
            p.parameterDependencies = [p_fc.name]
        p_pre = _param("prefix", "Output prefix (e.g. MAG)", "GPString")
        p_out = _param("out_folder", "Output folder", "DEFolder")
        p_sr = _param("out_sr", "Output coordinate system", "GPSpatialReference",
                      value=arcpy.SpatialReference(DEFAULT_OUT_WKID))
        p_tr = _param("transformation", "Geographic transformation (filled automatically)",
                      "GPString", ptype="Optional")
        p_tr.filter.type = "ValueList"
        p_cs = _param("cell_size", "Cell size (output CRS units)", "GPDouble",
                      value=DEFAULT_CELL_SIZE)
        p_rs = _param("resampling", "Resampling method", "GPString", value="BILINEAR")
        p_rs.filter.type = "ValueList"
        p_rs.filter.list = ["BILINEAR", "NEAREST", "CUBIC"]
        p_nd = _param("nodata", "NoData value (all bands)", "GPDouble", value=0)
        p_exp = _param("expand_grey",
                       "Expand single-band greyscale inputs to three bands when the "
                       "other inputs are RGB", "GPBoolean", value=True)
        return [p_vis, p_ras, p_fc, p_code, p_grp, p_pre, p_out, p_sr, p_tr,
                p_cs, p_rs, p_nd, p_exp]

    def updateParameters(self, params):
        # Transformations are listed for cartogram CRS -> output CRS; the
        # rasters must share the cartogram CRS (checked in execute).
        P = _by_name(params)
        p_fc, p_sr, p_tr = P["cartogram"], P["out_sr"], P["transformation"]
        if p_fc.value and p_sr.value and (p_fc.altered or p_sr.altered) \
                and not p_tr.altered:
            try:
                in_sr = arcpy.Describe(p_fc.valueAsText).spatialReference
                available = arcpy.ListTransformations(in_sr, p_sr.value)
            except Exception:
                available = []
            if available:
                p_tr.filter.list = available
                p_tr.value = _pick_transformation(available)
            else:
                p_tr.value = None

    def updateMessages(self, params):
        P = _by_name(params)
        if not P["use_visible"].value and not P["rasters"].value:
            P["rasters"].setErrorMessage("Enable the visible rasters or list input rasters.")
        _check_prefix(P["prefix"])
        cs = P["cell_size"]
        if cs.value is not None and cs.value <= 0:
            cs.setErrorMessage("Cell size must be positive.")

    def execute(self, params, messages):
        P = _by_name(params)
        c = SimpleNamespace(
            use_visible=bool(P["use_visible"].value),
            extra=P["rasters"].valueAsText,
            fc=P["cartogram"].valueAsText,
            code_f=P["code_field"].valueAsText,
            group_f=P["group_field"].valueAsText,
            prefix=P["prefix"].valueAsText,
            out_folder=P["out_folder"].valueAsText,
            out_sr=P["out_sr"].value,
            trans=(P["transformation"].valueAsText or "").strip(),
            cs=float(P["cell_size"].value),
            resampling=P["resampling"].valueAsText,
            nodata=_num(P["nodata"].value),
            expand_grey=bool(P["expand_grey"].value),
        )
        run_id = "{}_mosaic_{}_{}".format(c.prefix, _safe(c.group_f), _timestamp())
        log = RunLog(os.path.join(c.out_folder, run_id + ".log"))
        try:
            log.info("Tool: 2. Mosaic Sheets by Map | Map Sheet Mosaic {} | group by field {}".format(
                TOOLBOX_VERSION, c.group_f))
            log.info("Output CRS: {} | transformation: {} | cell: {} | {} | NoData {}".format(
                c.out_sr.name, c.trans or "-", c.cs, c.resampling, c.nodata))
            self._run(log, run_id, c)
        finally:
            log.close()

    def _run(self, log, run_id, c):
        # ---- Collect inputs ------------------------------------------------
        paths = []
        if c.use_visible:
            vis = _visible_raster_layers()
            # Ignore other rasters in the map (originals, DEMs, ...): only
            # files named PREFIX_* by tool 1 are taken from the map.
            tag = (c.prefix + "_").lower()
            keep = [p for p in vis if os.path.basename(p).lower().startswith(tag)]
            log.info("Visible rasters in map: {} | with prefix {}_: {} | ignored: {}".format(
                len(vis), c.prefix, len(keep), len(vis) - len(keep)))
            paths += keep
        for item in _split_multi(c.extra):
            paths.append(arcpy.Describe(item).catalogPath)
        seen, inputs = set(), []
        for p in paths:  # de-duplicate, keep order
            k = os.path.normcase(os.path.abspath(p))
            if k not in seen:
                seen.add(k)
                inputs.append(p)
        if not inputs:
            log.error("No input rasters.")
            return
        log.info("Input rasters (duplicates removed): {}".format(len(inputs)))

        # ---- Identify each raster's sheet from its georeferencing --------
        carto_sr, sheets = _load_cartogram(c.fc, c.code_f, [c.group_f])
        per_sheet, kinds, bad = {}, {}, 0
        n_in = len(inputs)
        w_in = _width([os.path.basename(p) for p in inputs])
        for k_in, p in enumerate(inputs, 1):
            head = "{} {} |".format(_counter(k_in, n_in), os.path.basename(p).ljust(w_in))
            ras = arcpy.Raster(p)
            kinds.setdefault((ras.pixelType, ras.bandCount), []).append(p)
            if not _same_sr(ras.spatialReference, carto_sr):
                log.error("{} CRS {} differs from the cartogram ({})".format(
                    head, ras.spatialReference.name, carto_sr.name))
                bad += 1
                continue
            sheet, _cov = _best_sheet(_footprint(ras), sheets)
            if sheet is None:
                log.error("{} does not intersect any sheet".format(head))
                bad += 1
                continue
            if not sheet["attrs"][c.group_f]:
                log.error("{} sheet {} has no value in field {}".format(
                    head, sheet["code"], c.group_f))
                bad += 1
                continue
            per_sheet.setdefault(sheet["code"], []).append((p, sheet))
        # ---- One raster per cartogram sheet (safeguard) -------------------
        conflicts = {k: v for k, v in per_sheet.items() if len(v) > 1}
        if conflicts:
            log.error("Only one raster per cartogram sheet is allowed. Sheets with "
                      "more than one raster: {}".format(len(conflicts)))
            w_code = _width(conflicts)
            for i, (k, v) in enumerate(sorted(conflicts.items()), 1):
                log.error("{} {} | {} rasters | {}".format(
                    _counter(i, len(conflicts)), k.ljust(w_code), len(v),
                    ", ".join(sorted(os.path.basename(x[0]) for x in v))))

        # ---- Single-band greyscale among RGB: expand to three bands -------
        # Only when every other input has three bands of the same pixel type
        # and the single-band rasters carry no colour table (palette rasters
        # would need their colour table applied, not their index repeated).
        expand = set()
        if c.expand_grey and len(kinds) == 2:
            (pt_a, nb_a), (pt_b, nb_b) = sorted(kinds)
            if pt_a == pt_b and {nb_a, nb_b} == {1, 3}:
                singles = kinds[(pt_a, 1)]
                not_grey = [p for p in singles if _has_colour_table(p) is not False]
                names = ", ".join(sorted(os.path.basename(p) for p in singles))
                if not_grey:
                    log.error("Single-band rasters with a colour table, or not readable "
                              "by GDAL, are not expanded to three bands: {}".format(
                                  ", ".join(sorted(os.path.basename(p) for p in not_grey))))
                else:
                    expand = set(singles)
                    kinds = {(pt_a, 3): kinds[(pt_a, 3)] + singles}
                    log.info("Single-band greyscale rasters to be expanded to three bands "
                             "(band repeated, no resampling): {}: {}".format(
                                 len(singles), names))

        # ---- Pixel type, band count and NoData shared by all inputs -------
        pixel_type, n_bands = "", 0
        if len(kinds) > 1:
            log.error("All inputs must share pixel type and band count. "
                      "Combinations found: {}".format(len(kinds)))
            ordered = sorted(kinds.items(), key=lambda kv: (-len(kv[1]), kv[0]))
            for j, ((pt, nb), names) in enumerate(ordered):
                if j == 0:  # most frequent combination: count only
                    log.error("  {} band(s), pixel type {}: {} raster(s) "
                              "(most frequent)".format(nb, pt, len(names)))
                else:       # the others: every raster, so they can be found
                    log.error("  {} band(s), pixel type {}: {} raster(s): {}".format(
                        nb, pt, len(names),
                        ", ".join(sorted(os.path.basename(x) for x in names))))
            bad += 1
        else:
            (pt, n_bands), = kinds
            if pt not in PIXEL_TYPES:
                log.error("Unsupported pixel type: {}".format(pt))
                bad += 1
            elif not _nodata_fits(pt, c.nodata):
                log.error("NoData {} outside the range of pixel type {}".format(
                    c.nodata, pt))
                bad += 1
            else:
                pixel_type = PIXEL_TYPES[pt][0]
        if bad or conflicts:
            hints = []
            if conflicts:
                hints.append("switch off all but one copy of each sheet listed")
            if len(kinds) > 1:
                hints.append("switch off or convert the rasters listed with a "
                             "different band count or pixel type")
            log.error("Nothing was written. Correct the input selection{} and run "
                      "again.".format(" (" + "; ".join(hints) + ")" if hints else ""))
            return
        log.info("Pixel type {} | bands {}".format(pixel_type, n_bands))

        # ---- Datum transformation: required when datums differ ------------
        trans = c.trans
        if carto_sr.GCS.name != c.out_sr.GCS.name:
            available = arcpy.ListTransformations(carto_sr, c.out_sr)
            if not trans:
                trans = _pick_transformation(available)
                if not trans:
                    log.error("Datums differ ({} -> {}) and no transformation "
                              "is available.".format(carto_sr.GCS.name, c.out_sr.GCS.name))
                    return
                log.info("Transformation selected automatically: {}".format(trans))
            elif trans not in available:
                log.warn("Transformation '{}' is not listed by ListTransformations "
                         "for this CRS pair.".format(trans))
        else:
            trans = ""

        # ---- Group by field value and list expected sheets ---------------
        groups, expected = {}, {}
        for code, [(p, sheet)] in per_sheet.items():
            groups.setdefault(sheet["attrs"][c.group_f], []).append((code, p))
        for s in sheets:
            expected.setdefault(s["attrs"][c.group_f], set()).add(s["code"])
        n = len(groups)
        out_names = {key: "{}_{}_{}.tif".format(c.prefix, _safe(c.group_f), _safe(key))
                     for key in groups}
        clash = [v for v, k in collections.Counter(out_names.values()).items() if k > 1]
        if clash:
            log.error("Different group values map to the same output name: {}".format(
                ", ".join(sorted(clash))))
            return
        log.info("Groups to mosaic: {}".format(n))
        w_name = _width(out_names.values())
        w_cnt = _width([str(len(v)) for v in groups.values()])
        nodata_spec = ";".join("{} {}".format(b, c.nodata) for b in range(1, n_bands + 1))
        scratch = getattr(arcpy.env, "scratchFolder", None) or c.out_folder

        log_rows = []
        arcpy.SetProgressor("step", "Mosaicking...", 0, n, 1)
        for i, key in enumerate(sorted(groups), 1):
            t_item = time.perf_counter()
            members = sorted(groups[key])
            src = [p for _c, p in members]
            name = out_names[key]
            head = "{} {} |".format(_counter(i, n), name.ljust(w_name))
            out_path = os.path.join(c.out_folder, name)
            present = {k for k, _p in members}
            absent = sorted(expected.get(key, set()) - present)
            rec = dict.fromkeys(MOSAIC_CSV_COLUMNS, "")
            rec.update(group_field=c.group_f, group_value=key,
                       n_sheets=str(len(members)),
                       codes=" ".join(sorted(present)), missing=" ".join(absent),
                       rasters=" ".join(os.path.basename(p) for p in src),
                       output_path=out_path, pixel_type=pixel_type,
                       bands=str(n_bands), transformation=trans,
                       cell_size=str(c.cs))
            try:
                if arcpy.Exists(out_path):
                    rec["state"], rec["notes"] = "EXISTS", "not rewritten"
                else:
                    # Projected union extent snapped outward to cell multiples
                    cs = c.cs
                    xs, ys = [], []
                    for p in src:
                        fp = _footprint(arcpy.Raster(p))
                        pe = (fp.projectAs(c.out_sr, trans) if trans
                              else fp.projectAs(c.out_sr)).extent
                        xs += [pe.XMin, pe.XMax]
                        ys += [pe.YMin, pe.YMax]
                    ext = arcpy.Extent(math.floor(min(xs) / cs) * cs,
                                       math.floor(min(ys) / cs) * cs,
                                       math.ceil(max(xs) / cs) * cs,
                                       math.ceil(max(ys) / cs) * cs)
                    env = dict(outputCoordinateSystem=c.out_sr, extent=ext,
                               resamplingMethod=c.resampling, compression="LZW",
                               overwriteOutput=False)
                    if trans:
                        env["geographicTransformations"] = trans
                    # Temporary three-band copies of greyscale members, made in
                    # the native CRS before the projecting environment is set
                    tmp, mosaic_src = [], []
                    try:
                        for p in src:
                            if p in expand:
                                q = os.path.join(scratch, "msm_rgb_{}.tif".format(
                                    os.path.splitext(os.path.basename(p))[0]))
                                _expand_to_rgb(p, q, c.nodata)
                                tmp.append(q)
                                mosaic_src.append(q)
                            else:
                                mosaic_src.append(p)
                        with arcpy.EnvManager(**env):
                            arcpy.management.MosaicToNewRaster(
                                ";".join(mosaic_src), c.out_folder, name, c.out_sr,
                                pixel_type, cs, n_bands, "LAST", "FIRST")
                    finally:
                        for q in tmp:
                            if arcpy.Exists(q):
                                arcpy.management.Delete(q)
                    if tmp:
                        _add_note(rec, "expanded to 3 bands: {}".format(" ".join(
                            sorted(os.path.basename(p) for p in src if p in expand))))
                    arcpy.management.SetRasterProperties(out_path, nodata=nodata_spec)
                    arcpy.management.CalculateStatistics(out_path)
                    arcpy.management.BuildPyramids(out_path, -1, "NONE", c.resampling)
                    # Verify grid alignment (environment honouring is not assumed)
                    oe = arcpy.Raster(out_path).extent
                    off = [abs(v / cs - round(v / cs)) for v in (oe.XMin, oe.YMax)]
                    if max(off) > 1e-3:
                        msg = "grid not aligned to multiples of the cell size {}".format(cs)
                        _add_note(rec, msg)
                        log.warn("{} {}".format(head, msg))
                    rec["state"] = "OK"
                if absent:
                    log.warn("{} {} of {} sheets missing".format(
                        head, len(absent), len(expected.get(key, ()))))
            except Exception as exc:
                rec["state"], rec["notes"] = "ERROR", str(exc)[:200]
                log.warn("{} {}".format(head, exc))
            dt = time.perf_counter() - t_item
            rec["t_mosaic_s"] = "{:.1f}".format(dt)
            log.progress(i, n, "{} | {:>{w}} sheet(s) | {:<6}".format(
                name.ljust(w_name), len(members), rec["state"], w=w_cnt), dt)
            log_rows.append(rec)
            arcpy.SetProgressorPosition()

        csv_out = os.path.join(c.out_folder, run_id + ".csv")
        with open(csv_out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=MOSAIC_CSV_COLUMNS, delimiter=CSV_DELIM)
            w.writeheader()
            w.writerows(log_rows)
        log.info("Mosaics: {} | CSV: {}".format(len(log_rows), csv_out))
