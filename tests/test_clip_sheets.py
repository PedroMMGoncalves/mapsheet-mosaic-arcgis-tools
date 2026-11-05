"""Tool 1, Clip and Rename Sheets, run end to end against the arcpy stub."""

import os
import re

from conftest import calls, latest, make_params, read_csv


def _run(tbx, col, **kw):
    values = dict(in_folder=col.inp, cartogram="carto", prefix="MAG",
                  out_folder=col.out, dry_run=False)
    values.update(kw)
    tool = tbx.ClipSheets()
    params = make_params(tool, **values)
    tool.updateMessages(params)
    assert not {p.name: p.error for p in params if p.error}
    tool.execute(params, None)
    return values


def test_defaults_are_the_documented_ones(tbx):
    p = {x.name: x for x in tbx.ClipSheets().getParameterInfo()}
    assert p["code_field"].value == "COD_50255k"
    assert p["attr_fields"].values == ["Folha_25k", "Folha_50k"]
    assert p["extensions"].value == ".tif;.tiff"
    assert p["nodata"].value == 0 and p["min_coverage"].value == 99.0
    assert p["dry_run"].value is True and p["check_nodata"].value is True
    for name in ("code_field", "attr_fields"):
        assert p[name].parameterDependencies == ["cartogram"]


def test_validation_messages(tbx, collection):
    tool = tbx.ClipSheets()
    params = make_params(tool, in_folder=collection.inp, out_folder=collection.inp,
                         prefix="MAG_", extensions=" ; ", variant_regex="(?P<x>.+)",
                         min_coverage=120)
    tool.updateMessages(params)
    assert {p.name for p in params if p.error} == {
        "out_folder", "prefix", "extensions", "variant_regex", "min_coverage"}
    params = make_params(tool, variant_regex="(?P<var>")
    tool.updateMessages(params)
    assert "Invalid regular expression" in [p for p in params
                                             if p.name == "variant_regex"][0].error


def test_dry_run_writes_plan_and_log_only(tbx, fake, collection):
    _run(tbx, collection, dry_run=True)
    assert not calls(fake, "Clip")
    rows = read_csv(latest(collection.out, "MAG_plan_*.csv"))
    assert len(rows) == 9
    assert os.path.exists(latest(collection.out, "MAG_plan_*.log"))


def test_identification_naming_states_and_use(tbx, fake, collection):
    _run(tbx, collection)
    rows = {r["original_file"]: r for r in read_csv(latest(collection.out, "MAG_clip_*.csv"))}
    # extension filter and output-folder exclusion
    assert "O_+2-164_MAG.jp2" not in rows and "MAG_old.tif" not in rows
    # identity from georeferencing; copies named in sorted path order
    assert rows["O_+0-160_MAG.TIF"]["output_name"] == "MAG_51B60925.tif"
    assert rows["O_+0-160_MAG2.TIF"]["output_name"] == "MAG_51B60925_dup1.tif"
    assert rows["O_+0-160_MAG_FERR.tif"]["output_name"] == "MAG_51B60925_dup2.tif"
    assert rows["BADNAME.TIF"]["code"] == "51B61002"
    # variant from the file name; unmatched names keep the full stem
    assert rows["O_+0-160_MAG2.TIF"]["variant"] == "MAG2"
    assert rows["O_+0-160_MAG_FERR.tif"]["variant"] == "MAG_FERR"
    assert rows["BADNAME.TIF"]["variant"] == "BADNAME"
    assert "no regex match" in rows["BADNAME.TIF"]["notes"]
    # cartogram fields copied, numeric values normalised
    assert rows["O_+0-160_MAG.TIF"]["Folha_25k"] == "609"
    assert rows["O_+0-160_MAG.TIF"]["Folha_50k"] == "51-B"
    # states
    assert rows["O_+0-162_MAG.tiff"]["state"] == "LOW_COVERAGE"
    assert rows["O_+0-162_MAG.tiff"]["coverage_pct"] == "75.00"
    assert rows["O_+9-999_MAG.TIF"]["state"] == "NO_SHEET"
    assert rows["O_+0-170_MAG.TIF"]["state"] == "CRS_MISMATCH"
    assert rows["O_+2-162_FAIL.TIF"]["state"] == "ERROR"
    assert "simulated clip failure" in rows["O_+2-162_FAIL.TIF"]["notes"]
    # `use`: 1 only for single-copy sheets with state OK
    assert rows["O_+2-160_MAG.TIF"]["use"] == "1"
    assert rows["O_+0-160_MAG.TIF"]["use"] == "0"
    assert rows["O_+0-162_MAG.tiff"]["use"] == "0"
    # pixel description and NoData check (source NoData not counted)
    assert rows["O_+0-160_MAG.TIF"]["bands"] == "3"
    assert rows["O_+0-160_MAG.TIF"]["pixel_type"] == "U8"
    assert rows["O_+0-160_MAG2.TIF"]["n_pix_nodata"] == "5"
    assert rows["O_+0-160_MAG.TIF"]["n_pix_nodata"] == "0"


def test_copies_per_sheet_column_and_summary(tbx, fake, collection):
    _run(tbx, collection, dry_run=True)
    rows = {r["original_file"]: r for r in read_csv(latest(collection.out, "MAG_plan_*.csv"))}
    assert rows["O_+0-160_MAG.TIF"]["n_copies"] == "3"
    assert rows["O_+0-162_MAG.tiff"]["n_copies"] == "1"      # LOW_COVERAGE counts
    assert rows["O_+2-160_MAG.TIF"]["n_copies"] == "1"
    assert rows["O_+9-999_MAG.TIF"]["n_copies"] == ""        # NO_SHEET
    warns = [t for lv, t in fake.MESSAGES if lv == "WARN"]
    assert any(t.startswith("Sheets with more than one copy: 2") for t in warns)
    line = [t for t in warns if t.startswith("[1/2] 51B60925 | 3 copies | ")]
    assert line and "MAG_51B60925_dup1.tif [MAG2, cell 0.4230 m, 3 band(s)]" in line[0]
    assert any(t.startswith("[2/2] 51B61002 | 2 copies | ") for t in warns)


def test_summary_counts_copies_after_clipping(tbx, fake, collection):
    _run(tbx, collection)   # the failing clip of 51B61002_dup1 leaves one copy
    warns = [t for lv, t in fake.MESSAGES if lv == "WARN"]
    assert any(t.startswith("Sheets with more than one copy: 1") for t in warns)
    assert not any("51B61002 |" in t for t in warns)


def test_clip_keeps_source_grid_and_uses_nodata(tbx, fake, collection):
    _run(tbx, collection, nodata=0)
    clips = calls(fake, "Clip")
    assert clips and all(c[1][4] == "0" for c in clips)
    assert all(c[1][5] == "ClippingGeometry" and c[1][6] == "NO_MAINTAIN_EXTENT"
               for c in clips)


def test_nodata_outside_pixel_type_range_is_not_clipped(tbx, fake, collection):
    _run(tbx, collection, nodata=300, out_folder=collection.out2)
    rows = read_csv(latest(collection.out2, "MAG_clip_*.csv"))
    assert not calls(fake, "Clip")
    assert all(r["state"] != "OK" for r in rows)
    assert any("outside the range of pixel type U8" in r["notes"] for r in rows)


def test_existing_outputs_are_skipped(tbx, fake, collection):
    _run(tbx, collection)
    n_first = len(calls(fake, "Clip"))
    _run(tbx, collection)
    assert len(calls(fake, "Clip")) == n_first + 1   # only the failing raster is retried
    rows = {r["original_file"]: r for r in read_csv(latest(collection.out, "MAG_clip_*.csv"))}
    assert "output exists" in rows["O_+0-160_MAG.TIF"]["notes"]


def test_custom_fields_and_column_collision(tbx, fake, collection):
    for _rect, attrs in fake.FCS["carto"]["rows"]:
        attrs["notes"] = "n/a"
    _run(tbx, collection, dry_run=True, attr_fields=["Folha_50k", "notes"])
    header = list(read_csv(latest(collection.out, "MAG_plan_*.csv"))[0])
    assert header[4:6] == ["Folha_50k", "field_notes"]
    assert "Folha_25k" not in header


def test_empty_sheet_code_is_an_error(tbx, fake, collection):
    fake.FCS["carto"]["rows"][1][1]["COD_50255k"] = None
    _run(tbx, collection, dry_run=True)
    rows = {r["original_file"]: r for r in read_csv(latest(collection.out, "MAG_plan_*.csv"))}
    assert rows["O_+2-160_MAG.TIF"]["state"] == "ERROR"
    assert "empty sheet code" in rows["O_+2-160_MAG.TIF"]["notes"]


def test_log_progress_lines_are_aligned(tbx, fake, collection):
    _run(tbx, collection)
    lines = open(latest(collection.out, "MAG_clip_*.log"), encoding="utf-8").read().splitlines()
    progress = [ln for ln in lines if "| check" in ln]
    assert len(progress) == 7
    assert all(re.search(r"\[\d/7\] ", ln) for ln in progress)
    # 6 fixed separators per line; an error note adds a 7th at the end
    seps = {tuple(m.start() for m in re.finditer(r" \| ", ln))[:6] for ln in progress}
    assert len(seps) == 1, progress
