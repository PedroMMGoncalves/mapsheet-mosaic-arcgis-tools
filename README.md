# Map Sheet Mosaic — ArcGIS Pro toolbox

Python toolbox (`MapSheetMosaic.pyt`) for archives of scanned, georeferenced
map sheets. It removes the collar of each sheet using the sheet polygons of a
cartogram, names the result by sheet code, and mosaics selected sheets by a
grouping attribute of the cartogram (e.g. the parent map sheet), projecting to
the target coordinate system with a single resampling.

The toolbox was developed for series of 1:5000 sheets but makes no assumption
on sheet size, map scale, file naming, band count or pixel type: these are
taken from the cartogram, the parameters and the rasters.

## Method

1. **Sheet identification.** The sheet covered by each raster is the cartogram
   polygon with the largest area of intersection with the raster extent.
   File names are not used for identification, so misnamed files are assigned
   to the correct sheet. Coverage is reported as intersection area / sheet
   area (%).
2. **Collar removal.** Each raster is clipped by its sheet polygon in the
   native coordinate system (`Clip`, `ClippingGeometry`,
   `NO_MAINTAIN_EXTENT`). The source cell grid is preserved; no resampling
   occurs. Cells outside the neat line receive the NoData value.
3. **Copy selection.** Duplicate scans of the same sheet are written as
   `PREFIX_CODE.tif`, `PREFIX_CODE_dup1.tif`, … The choice of the copy to use
   is made by the operator by switching layers on or off in a map; the tools
   never choose between copies.
4. **Mosaic and projection.** Sheets are grouped by the value of a cartogram
   field and mosaicked with `Mosaic To New Raster` directly into the output
   coordinate system, with an explicit datum transformation when the datums
   differ. Projection and mosaicking therefore share one resampling step. The
   output extent is snapped to multiples of the cell size, and the grid
   alignment is verified after writing.

No radiometric balancing is applied between scans. Source rasters are never
modified.

## Requirements

- ArcGIS Pro 3.x, default Python environment (`arcpy`, `numpy`). No additional
  packages.
- All geoprocessing tools used (Clip, Mosaic To New Raster, Set Raster
  Properties, Calculate Statistics, Build Pyramids) are available at every
  licence level.
- A polygon cartogram of the sheets, in the same coordinate system as the
  georeferenced rasters, with a sheet code field.

## Installation

Download or clone the repository and add `MapSheetMosaic.pyt` to an ArcGIS Pro
project (Catalog pane → Toolboxes → Add Toolbox).

## Workflow

1. Run **1. Clip and Rename Sheets** with *Dry run* enabled and review the
   CSV plan (sheet assignment, coverage, duplicates, output names).
2. Run the same tool with *Dry run* disabled to write the clipped sheets.
3. Add the clipped sheets to a map. Where a sheet has several copies, switch
   off the copies that are not to be used (column `use` = 1 marks sheets with
   a single copy and sufficient coverage).
4. Run **2. Mosaic Sheets by Map** to produce one mosaic per group.

## Tool 1 — Clip and Rename Sheets

| Parameter | Description | Default |
|---|---|---|
| Input raster folder | Folder containing the georeferenced scans | — |
| Include subfolders | Search the folder tree recursively | on |
| Raster file extensions | Extensions to list, separated by `;` | `.tif;.tiff` |
| Cartogram | Polygon layer of the sheets | — |
| Sheet code field | Field holding the sheet code used in output names | `COD_50255k` |
| Cartogram fields copied to the CSV | Optional attributes reported per raster | `Folha_25k;Folha_50k` |
| Output prefix | Prefix of output names (letters, digits, `_`, `-`) | — |
| Variant regular expression | Expression with a named group `var` applied to the file name stem; the match is recorded as the scan variant | `^[^_]+_[^_]+_(?P<var>.+)$` |
| Output folder | Destination of rasters, CSV and log (must differ from the input folder) | — |
| Minimum sheet coverage (%) | Below this value the state is `LOW_COVERAGE` | 99 |
| NoData value | Assigned to all bands of the clipped rasters | 0 |
| Count source pixels equal to NoData | Counts pixels inside the sheet that already carry the NoData value and would therefore become NoData | on |
| Dry run | Write CSV and log only | on |

Outputs, written to the output folder:

- `PREFIX_CODE.tif` (GeoTIFF, LZW, statistics and pyramids), plus
  `PREFIX_CODE_dupN.tif` for further copies in sorted input order. Existing
  outputs are not overwritten, so an interrupted run can be resumed.
- `PREFIX_plan_<timestamp>.csv` (dry run) or `PREFIX_clip_<timestamp>.csv`:
  one row per input raster, `;`-delimited, UTF-8 with BOM.
- A `.log` file with the same name, flushed after every line.

CSV columns: `prefix`, `original_file`, `original_path`, `code`, the selected
cartogram fields, `variant`, `coverage_pct`, `cell_x`, `cell_y`, `bands`,
`pixel_type`, `n_pix_nodata`, `output_name`, `output_path`, `state`, `use`,
`t_check_s`, `t_clip_s`, `notes`.

States: `OK`, `LOW_COVERAGE`, `NO_SHEET` (no intersection with the
cartogram), `CRS_MISMATCH` (raster and cartogram coordinate systems differ),
`ERROR` (details in `notes`).

## Tool 2 — Mosaic Sheets by Map

| Parameter | Description | Default |
|---|---|---|
| Use raster layers visible in the active map | Takes visible raster layers whose file name starts with `PREFIX_`; a layer inside a hidden group layer counts as hidden | on |
| Additional rasters | Optional explicit list of rasters | — |
| Cartogram | Polygon layer of the sheets | — |
| Sheet code field | Field holding the sheet code | `COD_50255k` |
| Group by field | Cartogram field defining the mosaics (one mosaic per value) | `Folha_25k` |
| Output prefix | Prefix of output names | — |
| Output folder | Destination of mosaics, CSV and log | — |
| Output coordinate system | Target coordinate system | ETRS89 / PT-TM06 (EPSG:3763) |
| Geographic transformation | Filled from `ListTransformations`, preferring an NTv2 grid; empty when the datums are equal | automatic |
| Cell size | Output cell size, in output coordinate system units | 0.4 |
| Resampling method | `BILINEAR`, `NEAREST` or `CUBIC` (also used for pyramids) | `BILINEAR` |
| NoData value | Assigned to all bands of the mosaics | 0 |

Before mosaicking, each input is identified again against the cartogram. The
run stops without writing if two inputs fall on the same sheet, if an input
does not share the cartogram coordinate system or falls outside it, or if the
inputs differ in pixel type or band count.

Outputs: `PREFIX_<field>_<value>.tif` per group, and
`PREFIX_mosaic_<field>_<timestamp>.csv` / `.log`. CSV columns:
`group_field`, `group_value`, `n_sheets`, `codes`, `missing` (cartogram
sheets of the group without an input), `rasters`, `output_path`,
`pixel_type`, `bands`, `transformation`, `cell_size`, `t_mosaic_s`, `state`
(`OK`, `EXISTS`, `ERROR`), `notes`.

Tool 2 reads the current project and therefore runs in the foreground.

## Default values

The defaults correspond to the first collection processed and must be
reviewed for other collections: 1:5000 sheets of 2 × 2 km in Lisboa
Hayford-Gauss IPCC (ESRI:102165), RGB 8-bit scans at about 300 dpi (cell
≈ 0.42 m), cartogram fields `COD_50255k`, `Folha_25k`, `Folha_50k`, file
names of the form `O_<M><P>_<variant>.TIF` (e.g. `O_+30-170_MAG2.TIF`), and
target system ETRS89 / PT-TM06 (EPSG:3763) with an NTv2 datum
transformation.

## Log format

Log lines are written in fixed-width columns (zero-padded item counter, names
padded to the longest name of the run, right-aligned durations), e.g.

```
2026-11-05 10:12:31 [INFO]  [007/254] O_+30-170_MAG2.TIF -> MAG_51B60925_dup1.tif | OK     | check    0.5 s | clip   16.2 s |   16.8 s | elapsed  0:01:58 | ETA  1:09:50
```

Alignment is exact when the `.log` file is viewed in a monospaced font.

## Limitations

- The raster footprint is its axis-aligned extent. For rectified scans this
  includes the NoData corners, so `coverage_pct` can overestimate the area
  covered by image data. Ties in overlap area are resolved by cartogram
  order.
- Rasters and cartogram must share the coordinate system; no on-the-fly
  projection is made for identification.
- The NoData count reads the sheet window into memory (about 68 MB for a
  2 × 2 km sheet at 0.42 m with three 8-bit bands) and assumes north-up
  rasters. A pixel is counted when it equals the NoData value in at least one
  band; since NoData is stored per band, such a pixel loses only the affected
  band(s).
- `Mosaic To New Raster` has no NoData or background parameter; the NoData
  value is assigned afterwards with `Set Raster Properties`. This assumes that
  cells not covered by any input are written with that value, which requires
  verification for NoData values other than 0. The same NoData value should
  be used in both tools.
- The mosaic cell size is fixed; coarser inputs are upsampled, which adds
  pixels but no information.
- Where inputs overlap (sheet boundaries), the last input in sorted order is
  kept.
- Tool 1 lists raster files in folders only (not rasters stored in
  geodatabases) and always writes GeoTIFF.

## Validation status

- Tool 1: sheet identification, clipping and reporting validated in ArcGIS
  Pro on 254 rasters (≈ 16 s per raster for clipping with LZW compression,
  pyramids and statistics; ≈ 0.5 s for the NoData check).
- Tool 2: not yet validated in ArcGIS Pro. Pending checks: population of the
  transformation list, honouring of the extent environment (absence of the
  grid alignment warning), and absence of systematic offsets against a
  PT-TM06 reference.
- The parameterisation of NoData value, file extensions, CSV fields and
  grouping field, and the log formatting, have been checked with a stand-in
  for `arcpy` only and are pending validation in ArcGIS Pro.
