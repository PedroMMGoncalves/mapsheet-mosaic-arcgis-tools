# Changelog

Notable changes to Map Sheet Mosaic. Follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/). `TOOLBOX_VERSION` in
[`MapSheetMosaic.pyt`](MapSheetMosaic.pyt) tracks the same value as `version:` in
[`CITATION.cff`](CITATION.cff).

## [0.1.0] - 2026-11-05

First public version. Tool 1 in its original form was validated in ArcGIS Pro on
254 rasters; the changes listed below and the whole of Tool 2 are pending validation
in ArcGIS Pro (see *Validation status* in the README).

### Added

- **Tool 1, Clip and Rename Sheets.** Sheet identification by largest area of overlap
  between the raster extent and the cartogram polygons; collar removal with `Clip`
  (`ClippingGeometry`, `NO_MAINTAIN_EXTENT`, source grid preserved); output
  `PREFIX_CODE.tif`, further copies `_dup1`, `_dup2`, ...; dry run; resumable runs;
  timestamped CSV report and log.
- **Tool 2, Mosaic Sheets by Map.** Inputs from the raster layers visible in the
  active map (group visibility respected) and/or an explicit list; re-identification
  against the cartogram; one mosaic per value of a grouping field with
  `Mosaic To New Raster` directly in the output coordinate system; explicit datum
  transformation (NTv2 preferred); extent snapped to cell multiples and grid
  alignment verified after writing; missing sheets per group reported.
- **Parameters for collection-independent use:** raster file extensions, cartogram
  fields copied to the CSV, grouping field, NoData value. Pixel type and band count
  of the mosaics are read from the inputs.
- **Input checks.** NoData value tested against the pixel type range; empty sheet
  codes and empty group values reported; Tool 2 stops before writing when two inputs
  fall on the same sheet, when coordinate systems differ, or when inputs differ in
  pixel type or band count; output folder must differ from the input folder.
- **Fixed-width log.** Zero-padded item counter, names padded to the longest name of
  the run, right-aligned durations, elapsed time and ETA; free-text notes appended
  last. The first line records the toolbox version.
- **Regression suite** (`tests/`, pytest) with a stand-in for `arcpy`, and a CI
  workflow running it on Python 3.9, 3.11 and 3.13.

### Changed (relative to the internal version)

- All user interface text, log text, state values and CSV column names are in English.
  State values: `OK`, `LOW_COVERAGE`, `NO_SHEET`, `CRS_MISMATCH`, `ERROR`, `EXISTS`.
- The NoData check counts pixels equal to the NoData value in **any** band (previously:
  value 0 in all three bands), since NoData is stored per band.
- Tool 2 output names are `PREFIX_<field>_<value>.tif` (previously `PREFIX_25k_<value>.tif`
  or `PREFIX_50k_<value>.tif`).
- Parameter names changed with the translation (e.g. `cartograma` to `cartogram`);
  scripted calls using keyword arguments must be updated.

### Fixed

- Exclusion of the output folder from the input listing no longer excludes sibling
  folders whose names start with the same characters.
