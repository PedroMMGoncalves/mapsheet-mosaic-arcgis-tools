# -*- coding: utf-8 -*-
"""
MapSheetMosaic.pyt
==================

ArcGIS Pro Python toolbox to (1) clip georeferenced 1:5000 map sheets by the
sheet polygons of a cartogram (removing the collar) and rename them by sheet
code, and (2) mosaic the selected sheets by 1:25k or 1:50k map, projecting in
the same step (single resampling).

Workflow
--------
1. Tool "1. Clip and Rename Sheets" with dry-run ON -> review the CSV plan.
2. Same tool with dry-run OFF                      -> clipped sheets + CSV.
3. Add the clipped sheets to the map and switch off the copies you do not
   want (the CSV `usar` column marks single-copy sheets as a guide).
4. Tool "2. Mosaic Sheets by Map"                 -> one mosaic per map.

Dependencies
------------
- ArcGIS Pro 3.x (arcpy, numpy shipped with Pro). No extra packages.
- Licence: Clip and Mosaic To New Raster are core Data Management tools.
- Tool 2 reads the CURRENT project (visible layers), so it runs in the
  foreground only.

Design decisions
----------------
- Sheet identity comes from georeferencing (largest overlap between raster
  extent and cartogram polygon), never from the file name (names may be wrong).
- Output name is PREFIX_CODE.tif; further copies of the same sheet get
  _dup1, _dup2, ... The file name is used only to extract the "variant",
  recorded in the CSV.
- Each run writes a .log (per-file timing, ETA, totals) next to the CSV.
- Clipping is done in the native CRS with NO_MAINTAIN_EXTENT, which keeps the
  original cell grid (no resampling in step 1).
- Projection + datum transformation happen once, inside the mosaic step.
- NoData = 0 in all bands. Tool 1 counts pixels that are 0 in all three bands
  inside the sheet, so real black pixels that would become holes are reported.
- No colour balancing: different scans keep their original tones.
- Originals are never modified; outputs go to a separate folder.

Methodological limitations
--------------------------
- Footprint = axis-aligned raster extent. For rectified scans this includes
  the NoData corners, so `cobertura_pct` can overestimate real coverage.
- The zero-pixel check reads the sheet window into memory (about 70 MB per
  sheet at 0.42 m). It assumes north-up rasters without geotransform rotation.
- Mosaic cell size is fixed (default 0.4 m). Scans at about 240 dpi (0.53 m)
  are upsampled; this adds pixels, not information.
- Mosaic grid alignment to multiples of the cell size is attempted through the
  extent environment and then verified; a warning is issued if not aligned.
- The datum transformation must be given explicitly; the tool refuses to let
  ArcGIS choose silently when source and target datums differ.
"""

import csv
import datetime
import math
import os
import re
import time

import arcpy
import numpy as np


CSV_DELIM = ";"  # Excel PT default; the operator edits the CSV in Excel
CSV_COLUMNS = [
    "prefixo", "ficheiro_original", "caminho_original", "codigo",
    "folha_25k", "folha_50k", "variante", "cobertura_pct",
    "cell_x", "cell_y", "n_pix_rgb_zero", "nome_saida", "caminho_saida",
    "estado", "usar", "t_zero_s", "t_clip_s", "obs",
]
DEFAULT_VARIANT_REGEX = r"^[^_]+_[^_]+_(?P<var>.+)$"
TMP_CLIP_FC = r"memory\clip_sheet"


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


def _safe(text):
    """Keep only characters that are safe in file names."""
    return re.sub(r"[^A-Za-z0-9_\-]", "_", text).strip("_")


def _same_sr(a, b):
    """Compare spatial references by WKID when available, else by name."""
    if a.factoryCode and b.factoryCode:
        return a.factoryCode == b.factoryCode
    return a.name == b.name


def _timestamp():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def _fmt_dur(seconds):
    """Format seconds as H:MM:SS."""
    seconds = int(round(seconds))
    return "{}:{:02d}:{:02d}".format(seconds // 3600, (seconds % 3600) // 60, seconds % 60)


class RunLog(object):
    """Write messages to the Geoprocessing pane and to a .log file.

    The file is flushed after every line, so it survives a crash or a
    cancelled run and shows exactly how far processing went.
    """

    def __init__(self, path):
        self.path = path
        self.t0 = time.perf_counter()
        self._f = open(path, "a", encoding="utf-8")

    def _write(self, level, text):
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._f.write("{} [{}] {}\n".format(stamp, level, text))
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

    def progress(self, i, n, label, step_s):
        """Log item i of n with its own duration, elapsed total and ETA."""
        elapsed = time.perf_counter() - self.t0
        eta = elapsed / i * (n - i) if i else 0.0
        self.info("[{}/{}] {} | {:.1f} s | decorrido {} | ETA {}".format(
            i, n, label, step_s, _fmt_dur(elapsed), _fmt_dur(eta)))

    def close(self):
        self.info("Tempo total: {} | log: {}".format(
            _fmt_dur(time.perf_counter() - self.t0), self.path))
        self._f.close()


def _list_rasters(folder, recursive, exclude=None):
    """List .tif/.tiff files, sorted case-insensitively (deterministic order).

    `exclude` is skipped so outputs inside the input tree are not reprocessed.
    """
    exts = (".tif", ".tiff")
    exclude = os.path.normcase(os.path.abspath(exclude)) if exclude else None
    found = []
    if recursive:
        for root, _dirs, files in os.walk(folder):
            if exclude and os.path.normcase(os.path.abspath(root)).startswith(exclude):
                continue
            found += [os.path.join(root, f) for f in files if f.lower().endswith(exts)]
    else:
        found = [os.path.join(folder, f) for f in os.listdir(folder)
                 if f.lower().endswith(exts)]
    return sorted(found, key=str.lower)


def _load_cartograma(fc, code_f, f25, f50):
    """Read cartogram polygons and attributes into memory."""
    sr = arcpy.Describe(fc).spatialReference
    sheets = []
    with arcpy.da.SearchCursor(fc, ["SHAPE@", code_f, f25, f50]) as cur:
        for shp, code, a, b in cur:
            if shp is None:
                continue
            e = shp.extent
            sheets.append({
                "geom": shp, "code": _norm(code), "f25": _norm(a), "f50": _norm(b),
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


def _count_rgb_zero(path, ras, sheet):
    """Count pixels equal to 0 in bands 1-3 inside the sheet window.

    Such pixels would turn into NoData holes after clipping with NoData=0.
    Source NoData (e.g. rectification corners) is mapped to 255 to avoid
    false positives.
    """
    e = ras.extent
    xmin, ymin = max(e.XMin, sheet["xmin"]), max(e.YMin, sheet["ymin"])
    xmax, ymax = min(e.XMax, sheet["xmax"]), min(e.YMax, sheet["ymax"])
    ncols = int(math.floor((xmax - xmin) / ras.meanCellWidth))
    nrows = int(math.floor((ymax - ymin) / ras.meanCellHeight))
    if ncols <= 0 or nrows <= 0:
        return 0
    arr = arcpy.RasterToNumPyArray(path, arcpy.Point(xmin, ymin), ncols, nrows,
                                   nodata_to_value=255)
    if arr.ndim != 3 or arr.shape[0] < 3:
        return -1  # not a 3-band raster; flagged by caller
    return int(np.all(arr[:3] == 0, axis=0).sum())


def _clip(in_path, sheet, out_path):
    """Clip by the sheet polygon, NoData=0, keeping the source cell grid."""
    if arcpy.Exists(TMP_CLIP_FC):
        arcpy.management.Delete(TMP_CLIP_FC)
    arcpy.management.CopyFeatures([sheet["geom"]], TMP_CLIP_FC)
    rect = "{} {} {} {}".format(sheet["xmin"], sheet["ymin"],
                                sheet["xmax"], sheet["ymax"])
    arcpy.management.Clip(in_path, rect, out_path, TMP_CLIP_FC, "0",
                          "ClippingGeometry", "NO_MAINTAIN_EXTENT")


def _param(name, label, dtype, direction="Input", ptype="Required",
           value=None, multi=False):
    p = arcpy.Parameter(name=name, displayName=label, datatype=dtype,
                        parameterType=ptype, direction=direction,
                        multiValue=multi)
    if value is not None:
        p.value = value
    return p


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
        self.description = ("Identifica a folha 1:5000 pela georreferenciação, "
                            "recorta a bordadura pelo polígono do cartograma "
                            "e grava PREFIXO_CODIGO.tif + CSV.")
        self.canRunInBackground = False

    def getParameterInfo(self):
        p_in = _param("in_folder", "Pasta de rasters", "DEFolder")
        p_rec = _param("recursive", "Incluir subpastas", "GPBoolean", value=True)
        p_fc = _param("cartograma", "Cartograma (polígonos 1:5000)", "GPFeatureLayer")
        p_code = _param("code_field", "Campo de código", "Field", value="COD_50255k")
        p_25 = _param("f25_field", "Campo folha 1:25k", "Field", value="Folha_25k")
        p_50 = _param("f50_field", "Campo folha 1:50k", "Field", value="Folha_50k")
        for p in (p_code, p_25, p_50):
            p.parameterDependencies = [p_fc.name]
        p_pre = _param("prefix", "Prefixo (ex.: MAG)", "GPString")
        p_rx = _param("variant_regex", "Regex da variante (grupo 'var')", "GPString",
                      value=DEFAULT_VARIANT_REGEX)
        p_out = _param("out_folder", "Pasta de saída", "DEFolder")
        p_cov = _param("min_coverage", "Cobertura mínima da folha (%)", "GPDouble",
                       value=99.0)
        p_zero = _param("check_zero", "Contar píxeis RGB = 0 dentro da folha",
                        "GPBoolean", value=True)
        p_dry = _param("dry_run", "Dry-run (só gera o CSV)", "GPBoolean", value=True)
        return [p_in, p_rec, p_fc, p_code, p_25, p_50, p_pre, p_rx,
                p_out, p_cov, p_zero, p_dry]

    def updateMessages(self, params):
        pre = params[6]
        if pre.value and _safe(pre.valueAsText) != pre.valueAsText:
            pre.setErrorMessage("Use só letras, algarismos, '_' ou '-'.")
        rx = params[7]
        if rx.value:
            try:
                if "var" not in re.compile(rx.valueAsText).groupindex:
                    rx.setErrorMessage("A regex tem de ter um grupo (?P<var>...).")
            except re.error as exc:
                rx.setErrorMessage("Regex inválida: {}".format(exc))

    def execute(self, params, messages):
        in_folder = params[0].valueAsText
        recursive = bool(params[1].value)
        fc = params[2].valueAsText
        code_f, f25_f, f50_f = (params[i].valueAsText for i in (3, 4, 5))
        prefix = params[6].valueAsText
        vrx = re.compile(params[7].valueAsText)
        out_folder = params[8].valueAsText
        min_cov = float(params[9].value)
        check_zero = bool(params[10].value)
        dry_run = bool(params[11].value)

        # One timestamp shared by the .log and the .csv of this run
        tag = "plano" if dry_run else "recorte"
        run_id = "{}_{}_{}".format(prefix, tag, _timestamp())
        log = RunLog(os.path.join(out_folder, run_id + ".log"))
        try:
            log.info("Ferramenta: 1. Clip and Rename Sheets | dry-run={}".format(dry_run))
            log.info("Entrada: {} (subpastas={}) | saída: {}".format(
                in_folder, recursive, out_folder))
            self._run(log, run_id, in_folder, recursive, fc, code_f, f25_f, f50_f,
                      prefix, vrx, out_folder, min_cov, check_zero, dry_run)
        finally:
            log.close()

    def _run(self, log, run_id, in_folder, recursive, fc, code_f, f25_f, f50_f,
             prefix, vrx, out_folder, min_cov, check_zero, dry_run):
        carto_sr, sheets = _load_cartograma(fc, code_f, f25_f, f50_f)
        codes = [s["code"] for s in sheets]
        dup_codes = sorted({c for c in codes if codes.count(c) > 1})
        if dup_codes:
            log.warn("Códigos repetidos no cartograma: {}".format(
                ", ".join(dup_codes[:20])))
        log.info("Cartograma: {} folhas, CRS {}".format(len(sheets), carto_sr.name))

        rasters = _list_rasters(in_folder, recursive, exclude=out_folder)
        log.info("Rasters encontrados: {}".format(len(rasters)))
        if not rasters:
            return

        # ---- Pass 1: identify sheets and plan names (no writing) ----------
        t_pass1 = time.perf_counter()
        rows, used_names = [], set()
        arcpy.SetProgressor("step", "A identificar folhas...", 0, len(rasters), 1)
        for path in rasters:
            fname = os.path.basename(path)
            stem = os.path.splitext(fname)[0]
            m = vrx.match(stem)
            variant = _safe(m.group("var")) if m else _safe(stem)
            row = dict.fromkeys(CSV_COLUMNS, "")
            row.update(prefixo=prefix, ficheiro_original=fname,
                       caminho_original=path, variante=variant, usar="0")
            if not m:
                row["obs"] = "regex sem correspondência; variante = nome completo"
            try:
                ras = arcpy.Raster(path)
                row["cell_x"] = "{:.4f}".format(ras.meanCellWidth)
                row["cell_y"] = "{:.4f}".format(ras.meanCellHeight)
                if not _same_sr(ras.spatialReference, carto_sr):
                    row["estado"] = "CRS_DIFERENTE"
                    log.warn("{}: CRS diferente ({})".format(fname, ras.spatialReference.name))
                    rows.append(row)
                    arcpy.SetProgressorPosition()
                    continue
                sheet, cov = _best_sheet(_footprint(ras), sheets)
                if sheet is None:
                    row["estado"] = "SEM_FOLHA"
                    log.warn("{}: não intersecta nenhuma folha do cartograma".format(fname))
                    rows.append(row)
                    arcpy.SetProgressorPosition()
                    continue
                row.update(codigo=sheet["code"], folha_25k=sheet["f25"],
                           folha_50k=sheet["f50"], cobertura_pct="{:.2f}".format(cov))
                row["estado"] = "OK" if cov >= min_cov else "COBERTURA_BAIXA"
                if row["estado"] == "COBERTURA_BAIXA":
                    log.warn("{}: cobertura {:.1f}% da folha {}".format(
                        fname, cov, sheet["code"]))
                base = "{}_{}".format(prefix, _safe(sheet["code"]))
                name, k = base, 0
                while name.lower() in used_names:  # in-run collision -> _dupN
                    k += 1
                    name = "{}_dup{}".format(base, k)
                used_names.add(name.lower())
                row["nome_saida"] = name + ".tif"
                row["caminho_saida"] = os.path.join(out_folder, name + ".tif")
                row["_sheet"], row["_ras"] = sheet, ras
            except Exception as exc:  # keep going; the CSV records the failure
                row["estado"] = "ERRO"
                row["obs"] = str(exc)[:200]
                log.warn("{}: ERRO na identificação: {}".format(fname, exc))
            rows.append(row)
            arcpy.SetProgressorPosition()
        log.info("Identificação concluída em {}".format(
            _fmt_dur(time.perf_counter() - t_pass1)))

        # ---- `usar`: 1 only for single-copy sheets with good coverage -----
        per_code = {}
        for r in rows:
            if r["codigo"] and r["estado"] in ("OK", "COBERTURA_BAIXA"):
                per_code[r["codigo"]] = per_code.get(r["codigo"], 0) + 1
        for r in rows:
            if r["codigo"] and r["estado"] == "OK" and per_code.get(r["codigo"]) == 1:
                r["usar"] = "1"

        # ---- Pass 2: clip (skipped in dry-run) -----------------------------
        if not dry_run:
            todo = [r for r in rows if "_sheet" in r]
            n = len(todo)
            log.info("A recortar {} rasters...".format(n))
            t_pass2 = time.perf_counter()
            arcpy.SetProgressor("step", "A recortar...", 0, n, 1)
            with arcpy.EnvManager(compression="LZW",
                                  pyramid="PYRAMIDS -1 BILINEAR",
                                  rasterStatistics="STATISTICS 1 1",
                                  overwriteOutput=False):
                for i, r in enumerate(todo, 1):
                    t_item = time.perf_counter()
                    try:
                        if check_zero:
                            t = time.perf_counter()
                            n0 = _count_rgb_zero(r["caminho_original"], r["_ras"],
                                                 r["_sheet"])
                            r["t_zero_s"] = "{:.1f}".format(time.perf_counter() - t)
                            r["n_pix_rgb_zero"] = str(n0)
                            if n0 == -1:
                                r["obs"] = (r["obs"] + "; menos de 3 bandas").strip("; ")
                            elif n0 > 0:
                                log.warn("{}: {} píxeis RGB=0 viram NoData".format(
                                    r["ficheiro_original"], n0))
                        if arcpy.Exists(r["caminho_saida"]):
                            r["obs"] = (r["obs"] + "; já existia, não reescrito").strip("; ")
                            status = "já existia"
                        else:
                            t = time.perf_counter()
                            _clip(r["caminho_original"], r["_sheet"], r["caminho_saida"])
                            r["t_clip_s"] = "{:.1f}".format(time.perf_counter() - t)
                            status = "OK"
                    except Exception as exc:
                        r["estado"] = "ERRO"
                        r["usar"] = "0"
                        r["obs"] = (r["obs"] + "; " + str(exc)[:200]).strip("; ")
                        status = "ERRO: {}".format(str(exc)[:120])
                    log.progress(i, n, "{} -> {} | {} | zero {} s | clip {} s".format(
                        r["ficheiro_original"], r["nome_saida"], status,
                        r["t_zero_s"] or "-", r["t_clip_s"] or "-"),
                        time.perf_counter() - t_item)
                    arcpy.SetProgressorPosition()
            if arcpy.Exists(TMP_CLIP_FC):
                arcpy.management.Delete(TMP_CLIP_FC)
            log.info("Recorte concluído em {}".format(
                _fmt_dur(time.perf_counter() - t_pass2)))

        # ---- CSV (timestamped, so operator edits are never overwritten) ---
        csv_path = os.path.join(out_folder, run_id + ".csv")
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, delimiter=CSV_DELIM,
                               extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

        # ---- Summary -------------------------------------------------------
        states = {}
        for r in rows:
            states[r["estado"]] = states.get(r["estado"], 0) + 1
        n_dup = sum(1 for c, k in per_code.items() if k > 1)
        log.info("Estados: " + ", ".join(
            "{}={}".format(k, v) for k, v in sorted(states.items())))
        log.info("Folhas com mais de uma cópia (usar=0, rever): {}".format(n_dup))
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
        raise RuntimeError("Não há mapa activo.")
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
    """Mosaic clipped sheets by 1:25k or 1:50k map, projecting once.

    Inputs are the rasters the operator chose: all raster layers visible in
    the active map and/or an explicit list. Each raster's sheet is identified
    again from its georeferencing against the cartogram, so the tool does not
    depend on file names or on the CSV of tool 1.
    """

    GROUP_KEYS = {"Folha_25k": ("f25", "25k"), "Folha_50k": ("f50", "50k")}

    def __init__(self):
        self.label = "2. Mosaic Sheets by Map"
        self.description = ("Mosaica os rasters visíveis no mapa (e/ou uma lista), "
                            "agrupados por carta 1:25k ou 1:50k, projectando "
                            "numa só reamostragem.")
        self.canRunInBackground = False  # needs the CURRENT project

    def getParameterInfo(self):
        p_vis = _param("use_visible", "Usar todos os rasters visíveis no mapa activo",
                       "GPBoolean", value=True)
        p_ras = _param("rasters", "Rasters adicionais (opcional)", "GPRasterLayer",
                       ptype="Optional", multi=True)
        p_fc = _param("cartograma", "Cartograma (polígonos 1:5000)", "GPFeatureLayer")
        p_code = _param("code_field", "Campo de código", "Field", value="COD_50255k")
        p_25 = _param("f25_field", "Campo folha 1:25k", "Field", value="Folha_25k")
        p_50 = _param("f50_field", "Campo folha 1:50k", "Field", value="Folha_50k")
        for p in (p_code, p_25, p_50):
            p.parameterDependencies = [p_fc.name]
        p_grp = _param("group", "Agrupar por", "GPString", value="Folha_25k")
        p_grp.filter.type = "ValueList"
        p_grp.filter.list = list(self.GROUP_KEYS)
        p_pre = _param("prefix", "Prefixo de saída (ex.: MAG)", "GPString")
        p_out = _param("out_folder", "Pasta de saída", "DEFolder")
        p_sr = _param("out_sr", "Sistema de coordenadas de saída",
                      "GPSpatialReference", value=arcpy.SpatialReference(3763))
        p_tr = _param("transformation", "Transformação geográfica (automática)",
                      "GPString", ptype="Optional")
        p_tr.filter.type = "ValueList"
        p_cs = _param("cell_size", "Tamanho de célula (m)", "GPDouble", value=0.4)
        p_rs = _param("resampling", "Reamostragem", "GPString", value="BILINEAR")
        p_rs.filter.type = "ValueList"
        p_rs.filter.list = ["BILINEAR", "NEAREST", "CUBIC"]
        return [p_vis, p_ras, p_fc, p_code, p_25, p_50, p_grp, p_pre,
                p_out, p_sr, p_tr, p_cs, p_rs]

    def updateParameters(self, params):
        # Transformations are listed for cartogram CRS -> output CRS; the
        # rasters must share the cartogram CRS (checked in execute).
        p_fc, p_sr, p_tr = params[2], params[9], params[10]
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
        if not params[0].value and not params[1].value:
            params[1].setErrorMessage("Active os rasters visíveis ou indique rasters.")
        pre = params[7]
        if pre.value and _safe(pre.valueAsText) != pre.valueAsText:
            pre.setErrorMessage("Use só letras, algarismos, '_' ou '-'.")
        if params[11].value is not None and params[11].value <= 0:
            params[11].setErrorMessage("O tamanho de célula tem de ser positivo.")

    def execute(self, params, messages):
        use_visible = bool(params[0].value)
        extra = params[1].valueAsText
        fc = params[2].valueAsText
        code_f, f25_f, f50_f = (params[i].valueAsText for i in (3, 4, 5))
        group_name = params[6].valueAsText
        prefix = params[7].valueAsText
        out_folder = params[8].valueAsText
        out_sr = params[9].value
        trans = (params[10].valueAsText or "").strip()
        cs = float(params[11].value)
        resampling = params[12].valueAsText

        scale_tag = self.GROUP_KEYS[group_name][1]
        run_id = "{}_mosaico_{}_{}".format(prefix, scale_tag, _timestamp())
        log = RunLog(os.path.join(out_folder, run_id + ".log"))
        try:
            log.info("Ferramenta: 2. Mosaic Sheets by Map | agrupar por {}".format(group_name))
            log.info("CRS saída: {} | transformação: {} | célula: {} m | {}".format(
                out_sr.name, trans or "-", cs, resampling))
            self._run(log, run_id, use_visible, extra, fc, code_f, f25_f, f50_f,
                      group_name, scale_tag, prefix, out_folder, out_sr, trans,
                      cs, resampling)
        finally:
            log.close()

    def _run(self, log, run_id, use_visible, extra, fc, code_f, f25_f, f50_f,
             group_name, scale_tag, prefix, out_folder, out_sr, trans, cs, resampling):
        # ---- Collect inputs ------------------------------------------------
        paths = []
        if use_visible:
            vis = _visible_raster_layers()
            # Ignore other rasters in the map (originals, DEMs, ...): only
            # files named PREFIX_* by tool 1 are taken from the map.
            tag = (prefix + "_").lower()
            keep = [p for p in vis if os.path.basename(p).lower().startswith(tag)]
            log.info("Rasters visíveis no mapa: {} | com prefixo {}_: {} | ignorados: {}".format(
                len(vis), prefix, len(keep), len(vis) - len(keep)))
            paths += keep
        if extra:
            for item in extra.split(";"):
                item = item.strip().strip("'")
                if item:
                    paths.append(arcpy.Describe(item).catalogPath)
        seen, inputs = set(), []
        for p in paths:  # de-duplicate, keep order
            k = os.path.normcase(os.path.abspath(p))
            if k not in seen:
                seen.add(k)
                inputs.append(p)
        if not inputs:
            log.error("Nenhum raster de entrada.")
            return
        log.info("Rasters de entrada (sem repetições): {}".format(len(inputs)))

        # ---- Identify each raster's sheet from its georeferencing --------
        carto_sr, sheets = _load_cartograma(fc, code_f, f25_f, f50_f)
        key_attr = self.GROUP_KEYS[group_name][0]
        per_sheet, bad = {}, 0
        for p in inputs:
            name = os.path.basename(p)
            ras = arcpy.Raster(p)
            if not _same_sr(ras.spatialReference, carto_sr):
                log.error("{}: CRS {} diferente do cartograma ({})".format(
                    name, ras.spatialReference.name, carto_sr.name))
                bad += 1
                continue
            sheet, cov = _best_sheet(_footprint(ras), sheets)
            if sheet is None:
                log.error("{}: não intersecta nenhuma folha".format(name))
                bad += 1
                continue
            per_sheet.setdefault(sheet["code"], []).append((p, sheet))
        conflicts = {c: v for c, v in per_sheet.items() if len(v) > 1}
        for c, v in sorted(conflicts.items()):
            log.error("Folha {} com {} rasters: {}".format(
                c, len(v), ", ".join(os.path.basename(x[0]) for x in v)))
        if bad or conflicts:
            log.error("Corrija a selecção (desligue as cópias a mais no mapa) e volte a correr.")
            return

        # ---- Datum transformation: required when datums differ ------------
        if carto_sr.GCS.name != out_sr.GCS.name:
            available = arcpy.ListTransformations(carto_sr, out_sr)
            if not trans:
                trans = _pick_transformation(available)
                if not trans:
                    log.error("Datums diferentes ({} -> {}) e nenhuma transformação "
                              "disponível.".format(carto_sr.GCS.name, out_sr.GCS.name))
                    return
                log.info("Transformação escolhida automaticamente: {}".format(trans))
            elif trans not in available:
                log.warn("Transformação '{}' não aparece em ListTransformations "
                         "para este par de CRS.".format(trans))
        else:
            trans = ""

        # ---- Group by map and list expected sheets ------------------------
        groups, expected = {}, {}
        for code, [(p, sheet)] in per_sheet.items():
            groups.setdefault(sheet[key_attr], []).append((code, p))
        for s in sheets:
            expected.setdefault(s[key_attr], set()).add(s["code"])
        n = len(groups)
        log.info("Cartas a mosaicar: {}".format(n))

        log_rows = []
        arcpy.SetProgressor("step", "A mosaicar...", 0, n, 1)
        for i, key in enumerate(sorted(groups), 1):
            t_item = time.perf_counter()
            members = sorted(groups[key])
            src = [p for _c, p in members]
            name = "{}_{}_{}.tif".format(prefix, scale_tag, _safe(key))
            out_path = os.path.join(out_folder, name)
            present = {c for c, _p in members}
            absent = sorted(expected.get(key, set()) - present)
            rec = {"carta": key, "n_folhas": len(members),
                   "codigos": " ".join(sorted(present)),
                   "em_falta": " ".join(absent),
                   "rasters": " ".join(os.path.basename(p) for p in src),
                   "saida": out_path, "transformacao": trans, "cell_size": cs,
                   "t_mosaico_s": "", "estado": "", "obs": ""}
            try:
                if arcpy.Exists(out_path):
                    rec["estado"], rec["obs"] = "JA_EXISTE", "não reescrito"
                else:
                    # Projected union extent snapped outward to cell multiples
                    xs, ys = [], []
                    for p in src:
                        fp = _footprint(arcpy.Raster(p))
                        pe = (fp.projectAs(out_sr, trans) if trans
                              else fp.projectAs(out_sr)).extent
                        xs += [pe.XMin, pe.XMax]
                        ys += [pe.YMin, pe.YMax]
                    ext = arcpy.Extent(math.floor(min(xs) / cs) * cs,
                                       math.floor(min(ys) / cs) * cs,
                                       math.ceil(max(xs) / cs) * cs,
                                       math.ceil(max(ys) / cs) * cs)
                    env = dict(outputCoordinateSystem=out_sr, extent=ext,
                               resamplingMethod=resampling, compression="LZW",
                               overwriteOutput=False)
                    if trans:
                        env["geographicTransformations"] = trans
                    with arcpy.EnvManager(**env):
                        arcpy.management.MosaicToNewRaster(
                            ";".join(src), out_folder, name, out_sr,
                            "8_BIT_UNSIGNED", cs, 3, "LAST", "FIRST")
                    arcpy.management.SetRasterProperties(
                        out_path, nodata="1 0;2 0;3 0")
                    arcpy.management.CalculateStatistics(out_path)
                    arcpy.management.BuildPyramids(out_path, -1, "NONE", "BILINEAR")
                    # Verify grid alignment (environment honouring is not assumed)
                    oe = arcpy.Raster(out_path).extent
                    off = [abs(v / cs - round(v / cs)) for v in (oe.XMin, oe.YMax)]
                    if max(off) > 1e-3:
                        rec["obs"] = "grelha não alinhada a múltiplos de {} m".format(cs)
                        log.warn("{}: {}".format(name, rec["obs"]))
                    rec["estado"] = "OK"
                if absent:
                    log.warn("{}: {} de {} folhas em falta".format(
                        name, len(absent), len(expected.get(key, ()))))
            except Exception as exc:
                rec["estado"], rec["obs"] = "ERRO", str(exc)[:200]
                log.warn("{}: {}".format(name, exc))
            dt = time.perf_counter() - t_item
            rec["t_mosaico_s"] = "{:.1f}".format(dt)
            log.progress(i, n, "{} | {} folhas | {}".format(
                name, len(members), rec["estado"]), dt)
            log_rows.append(rec)
            arcpy.SetProgressorPosition()

        csv_out = os.path.join(out_folder, run_id + ".csv")
        with open(csv_out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(log_rows[0]), delimiter=CSV_DELIM)
            w.writeheader()
            w.writerows(log_rows)
        log.info("Mosaicos: {} | CSV: {}".format(len(log_rows), csv_out))
