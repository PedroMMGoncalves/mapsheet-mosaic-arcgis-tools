"""Tool 2, Mosaic Sheets by Map, run on the outputs of tool 1 (arcpy stub)."""

import os

import pytest

from conftest import calls, latest, make_params, read_csv


@pytest.fixture
def clipped(tbx, fake, collection):
    """Run tool 1, then show one copy per sheet in the active map."""
    tool = tbx.ClipSheets()
    tool.execute(make_params(tool, in_folder=collection.inp, cartogram="carto",
                             prefix="MAG", out_folder=collection.out, dry_run=False), None)
    o = lambda name: os.path.join(collection.out, name)  # noqa: E731
    fake.LAYERS[:] = [
        fake.Layer("MAG_51B60925.tif", o("MAG_51B60925.tif")),
        fake.Layer("Copies", group=True, visible=False),
        fake.Layer("Copies\\MAG_51B60925_dup1.tif", o("MAG_51B60925_dup1.tif")),
        fake.Layer("MAG_51B60925_dup2.tif", o("MAG_51B60925_dup2.tif"), visible=False),
        fake.Layer("MAG_51B60926.tif", o("MAG_51B60926.tif")),
        fake.Layer("MAG_51B61001.tif", o("MAG_51B61001.tif")),
        fake.Layer("MAG_51B61002.tif", o("MAG_51B61002.tif")),
        fake.Layer("O_+0-160_MAG.TIF", os.path.join(collection.inp, "O_+0-160_MAG.TIF")),
    ]
    fake.CALLS.clear()
    fake.MESSAGES.clear()
    scratch = os.path.join(collection.root, "scratch")
    os.makedirs(scratch)
    fake.env.scratchFolder = scratch
    return collection


def _mosaic(tbx, col, **kw):
    values = dict(cartogram="carto", prefix="MAG", out_folder=col.mosaic)
    values.update(kw)
    tool = tbx.MosaicSheets()
    params = make_params(tool, **values)
    tool.updateMessages(params)
    assert not {p.name: p.error for p in params if p.error}
    tool.execute(params, None)


def _errors(fake):
    return [t for level, t in fake.MESSAGES if level == "ERROR"]


def test_defaults_and_transformation_dropdown(tbx, fake, collection):
    tool = tbx.MosaicSheets()
    p = {x.name: x for x in tool.getParameterInfo()}
    assert p["group_field"].value == "Folha_25k" and p["code_field"].value == "COD_50255k"
    assert p["out_sr"].value.factoryCode == 3763
    assert p["cell_size"].value == 0.4 and p["nodata"].value == 0
    assert p["expand_grey"].value is True
    params = list(p.values())
    p["cartogram"].value, p["cartogram"].altered = "carto", True
    tool.updateParameters(params)
    assert p["transformation"].value == "Lisboa_To_ETRS_1989_NTv2"
    assert "Lisboa_To_ETRS_1989_1" in p["transformation"].filter.list
    p["out_sr"].value = fake.SpatialReference(102164)     # same datum
    tool.updateParameters(params)
    assert p["transformation"].value is None


def test_validation_messages(tbx, collection):
    tool = tbx.MosaicSheets()
    params = make_params(tool, use_visible=False, prefix="M/G", cell_size=0)
    tool.updateMessages(params)
    assert {p.name for p in params if p.error} == {"rasters", "prefix", "cell_size"}


def test_mosaic_by_group_field(tbx, fake, clipped):
    _mosaic(tbx, clipped)
    mos = calls(fake, "MosaicToNewRaster")
    assert [c[1][2] for c in mos] == ["MAG_Folha_25k_609.tif", "MAG_Folha_25k_610.tif"]
    for c in mos:
        inputs, _f, _n, sr, ptype, cell, bands, method, _cm = c[1]
        assert sr.factoryCode == 3763 and ptype == "8_BIT_UNSIGNED" and bands == 3
        assert cell == 0.4 and method == "LAST"
        assert "dup" not in inputs and "O_+0-160" not in inputs
    assert calls(fake, "SetRasterProperties")[0][2]["nodata"] == "1 0;2 0;3 0"
    rows = read_csv(latest(clipped.mosaic, "MAG_mosaic_Folha_25k_*.csv"))
    assert [r["group_value"] for r in rows] == ["609", "610"]
    assert rows[0]["codes"] == "51B60925 51B60926" and rows[0]["missing"] == ""
    assert all(r["state"] == "OK" and r["notes"] == "" for r in rows)
    assert rows[0]["transformation"] == "Lisboa_To_ETRS_1989_NTv2"
    assert not [t for lv, t in fake.MESSAGES if lv == "WARN"]


def test_each_sheet_projected_once_on_the_common_grid(tbx, fake, clipped):
    _mosaic(tbx, clipped)
    scratch = os.path.join(clipped.root, "scratch")
    prj = calls(fake, "ProjectRaster")
    assert sorted(os.path.basename(c[1][0]) for c in prj) == [
        "MAG_51B60925.tif", "MAG_51B60926.tif", "MAG_51B61001.tif", "MAG_51B61002.tif"]
    for c in prj:
        _src, out, sr, resampling, cell, trans, reg = c[1]
        assert os.path.dirname(out) == scratch and os.path.basename(out).startswith("msm_prj_")
        assert sr.factoryCode == 3763 and resampling == "BILINEAR" and cell == 0.4
        assert trans == "Lisboa_To_ETRS_1989_NTv2" and reg == "0 0"
        # settings that could clip or re-snap the projected sheet are cleared
        assert c[2]["snapRaster"] is None and c[2]["extent"] is None
        assert out not in fake.EXISTING                    # temporary, deleted
    # the mosaic is made from the projected sheets, which share the 0.4 m grid
    mos = calls(fake, "MosaicToNewRaster")
    assert all(os.path.basename(p).startswith("msm_prj_")
               for c in mos for p in c[1][0].split(";"))
    for name in ("MAG_Folha_25k_609.tif", "MAG_Folha_25k_610.tif"):
        e = fake.RASTERS[os.path.join(clipped.mosaic, name)]["extent"]
        assert all(abs(v / 0.4 - round(v / 0.4)) < 1e-9 for v in e)


def test_alignment_warning_when_mosaic_grid_is_off(tbx, fake, clipped):
    fake.STATE["mosaic_aligned"] = False
    _mosaic(tbx, clipped)
    rows = read_csv(latest(clipped.mosaic, "MAG_mosaic_Folha_25k_*.csv"))
    assert all("grid not aligned" in r["notes"] for r in rows)


def test_missing_sheets_reported(tbx, fake, clipped):
    fake.LAYERS[6].visible = False      # MAG_51B61002.tif switched off
    _mosaic(tbx, clipped)
    rows = read_csv(latest(clipped.mosaic, "MAG_mosaic_Folha_25k_*.csv"))
    assert rows[1]["missing"] == "51B61002" and rows[1]["n_sheets"] == "1"


def test_other_group_field(tbx, fake, clipped):
    _mosaic(tbx, clipped, group_field="Folha_50k")
    assert [c[1][2] for c in calls(fake, "MosaicToNewRaster")] == ["MAG_Folha_50k_51-B.tif"]


def test_same_datum_needs_no_transformation(tbx, fake, clipped):
    _mosaic(tbx, clipped, out_sr=fake.SpatialReference(102164))
    rows = read_csv(latest(clipped.mosaic, "MAG_mosaic_Folha_25k_*.csv"))
    assert all(r["transformation"] == "" for r in rows)
    assert all(c[1][5] == "" for c in calls(fake, "ProjectRaster"))


def test_existing_mosaic_not_rewritten(tbx, fake, clipped):
    _mosaic(tbx, clipped)
    _mosaic(tbx, clipped)
    assert len(calls(fake, "MosaicToNewRaster")) == 2
    rows = read_csv(latest(clipped.mosaic, "MAG_mosaic_Folha_25k_*.csv"))
    assert [r["state"] for r in rows] == ["EXISTS", "EXISTS"]


def test_two_copies_of_a_sheet_stop_the_run(tbx, fake, clipped):
    fake.LAYERS[1].visible = True       # group with the _dup1 copy switched on
    _mosaic(tbx, clipped)
    assert not calls(fake, "MosaicToNewRaster")
    errors = _errors(fake)
    assert any("Only one raster per cartogram sheet is allowed" in t
               and t.endswith(": 1") for t in errors)
    assert any(t.startswith("[1/1] 51B60925 | 2 rasters | MAG_51B60925.tif, "
                            "MAG_51B60925_dup1.tif") for t in errors), errors
    assert "switch off all but one copy" in errors[-1]


def test_greyscale_among_rgb_is_expanded(tbx, fake, clipped, monkeypatch):
    grey = os.path.join(clipped.out, "MAG_51B61001.tif")
    fake.RASTERS[grey]["bands"] = 1
    monkeypatch.setattr(tbx, "_has_colour_table", lambda p: False)
    _mosaic(tbx, clipped)
    mos = calls(fake, "MosaicToNewRaster")
    assert [c[1][2] for c in mos] == ["MAG_Folha_25k_609.tif", "MAG_Folha_25k_610.tif"]
    assert all(c[1][6] == 3 for c in mos)
    tmp = os.path.join(clipped.root, "scratch", "msm_rgb_MAG_51B61001.tif")
    prj = os.path.join(clipped.root, "scratch", "msm_prj_MAG_51B61001.tif")
    # the three-band copy, not the single-band sheet, is projected and mosaicked
    assert [c[1][1] for c in calls(fake, "ProjectRaster") if c[1][0] == tmp] == [prj]
    assert not [c for c in calls(fake, "ProjectRaster") if c[1][0] == grey]
    assert prj in mos[1][1][0].split(";")
    assert prj not in fake.EXISTING
    comp = calls(fake, "CompositeBands")
    assert len(comp) == 1 and comp[0][1] == ([grey, grey, grey], tmp)
    # copy made in the native CRS: projecting / clipping settings cleared
    assert comp[0][2]["outputCoordinateSystem"] is None and comp[0][2]["extent"] is None
    assert ("SetRasterProperties", (tmp,), {"nodata": "1 0;2 0;3 0"}) in fake.CALLS
    assert tmp not in fake.EXISTING                       # temporary copy deleted
    assert fake.RASTERS[grey]["bands"] == 1               # tool 1 output untouched
    rows = read_csv(latest(clipped.mosaic, "MAG_mosaic_Folha_25k_*.csv"))
    assert rows[1]["notes"] == "expanded to 3 bands: MAG_51B61001.tif"
    assert rows[1]["bands"] == "3" and rows[0]["notes"] == ""
    assert not _errors(fake)


@pytest.mark.parametrize("colour_table", [True, None])
def test_palette_or_unreadable_single_band_is_not_expanded(tbx, fake, clipped,
                                                           monkeypatch, colour_table):
    fake.RASTERS[os.path.join(clipped.out, "MAG_51B61001.tif")]["bands"] = 1
    monkeypatch.setattr(tbx, "_has_colour_table", lambda p: colour_table)
    _mosaic(tbx, clipped)
    assert not calls(fake, "MosaicToNewRaster") and not calls(fake, "CompositeBands")
    errors = _errors(fake)
    assert any(t.startswith("Single-band rasters with a colour table") and
               t.endswith("MAG_51B61001.tif") for t in errors)


def test_mixed_band_count_stops_the_run(tbx, fake, clipped):
    fake.RASTERS[os.path.join(clipped.out, "MAG_51B61001.tif")]["bands"] = 1
    _mosaic(tbx, clipped, expand_grey=False)
    assert not calls(fake, "MosaicToNewRaster")
    errors = _errors(fake)
    assert any("share pixel type and band count" in t for t in errors)
    # the deviating raster is named; the most frequent combination is counted
    assert "  1 band(s), pixel type U8: 1 raster(s): MAG_51B61001.tif" in errors
    assert "  3 band(s), pixel type U8: 3 raster(s) (most frequent)" in errors
    assert "convert the rasters listed" in errors[-1]


def test_crs_mismatch_in_extra_rasters_stops_the_run(tbx, fake, clipped):
    extra = os.path.join(clipped.out, "MAG_other_crs.tif")
    fake.RASTERS[extra] = dict(extent=(0.0, 0.0, 10.0, 10.0), cell=0.4, wkid=3763,
                               bands=3, ptype="U8")
    _mosaic(tbx, clipped, rasters=[extra])
    assert not calls(fake, "MosaicToNewRaster")
    assert any("differs from the cartogram" in t for t in _errors(fake))


def test_empty_group_value_stops_the_run(tbx, fake, clipped):
    fake.FCS["carto"]["rows"][0][1]["Folha_25k"] = None
    _mosaic(tbx, clipped)
    assert not calls(fake, "MosaicToNewRaster")
    assert any("has no value in field Folha_25k" in t for t in _errors(fake))


def test_nodata_outside_pixel_type_range_stops_the_run(tbx, fake, clipped):
    _mosaic(tbx, clipped, nodata=-1)
    assert not calls(fake, "MosaicToNewRaster")
    assert any("outside the range of pixel type U8" in t for t in _errors(fake))
