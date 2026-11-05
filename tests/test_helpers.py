"""Pure helpers: formatting, parsing, NoData range, listing, identification."""

import os
import re

import pytest


def test_counter_is_zero_padded_to_total_width(tbx):
    assert tbx._counter(7, 254) == "[007/254]"
    assert tbx._counter(254, 254) == "[254/254]"
    assert tbx._counter(1, 9) == "[1/9]"


def test_width_and_duration(tbx):
    assert tbx._width([]) == 0
    assert tbx._width(["ab", "abcd"]) == 4
    assert tbx._fmt_dur(0) == "0:00:00"
    assert tbx._fmt_dur(3725.4) == "1:02:05"


@pytest.mark.parametrize("text, expected", [
    (".tif;.tiff", (".tif", ".tiff")),
    (".tif; TIFF, jp2", (".jp2", ".tif", ".tiff")),
    (" ; ", ()),
    (None, ()),
])
def test_parse_extensions(tbx, text, expected):
    assert tbx._parse_extensions(text) == expected


def test_split_multi_and_numbers(tbx):
    assert tbx._split_multi("a;'b c'; ") == ["a", "b c"]
    assert tbx._split_multi(None) == []
    assert tbx._num(0.0) == 0 and isinstance(tbx._num(0.0), int)
    assert tbx._num(-9999.5) == -9999.5
    assert tbx._norm(609.0) == "609" and tbx._norm(None) == "" and tbx._norm(" 51-B ") == "51-B"
    assert tbx._safe("51-B/x y") == "51-B_x_y"


@pytest.mark.parametrize("ptype, value, ok", [
    ("U8", 0, True), ("U8", 255, True), ("U8", 256, False), ("U8", -1, False),
    ("U8", 0.5, False), ("S16", -32768, True), ("U1", 2, False),
    ("F32", -9999.5, True), ("XX", 1e9, True),
])
def test_nodata_fits(tbx, ptype, value, ok):
    assert tbx._nodata_fits(ptype, value) is ok


def test_list_rasters_filters_extension_and_excludes_output(tbx, tmp_path):
    for rel in ("a.TIF", "b.tiff", "c.jp2", "a.TIF.aux.xml", "sub/d.tif",
                "out/e.tif", "out2/f.tif"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
    exts = (".tif", ".tiff")
    names = [os.path.relpath(p, str(tmp_path)).replace(os.sep, "/") for p in
             tbx._list_rasters(str(tmp_path), True, exts, exclude=str(tmp_path / "out"))]
    # 'out' is excluded, the sibling 'out2' sharing its name prefix is not
    assert names == ["a.TIF", "b.tiff", "out2/f.tif", "sub/d.tif"]
    flat = tbx._list_rasters(str(tmp_path), False, exts)
    assert [os.path.basename(p) for p in flat] == ["a.TIF", "b.tiff"]


def test_best_sheet_largest_overlap_and_coverage(tbx, fake):
    s = [{"geom": fake.Polygon(None, None, r), "code": c, "area": 4e6,
          "xmin": r[0], "ymin": r[1], "xmax": r[2], "ymax": r[3]}
         for c, r in (("A", (0, 0, 2000, 2000)), ("B", (2000, 0, 4000, 2000)))]
    fp = fake.Polygon(None, None, (500, 0, 3000, 2000))   # 1500 m in A, 1000 m in B
    sheet, cov = tbx._best_sheet(fp, s)
    assert sheet["code"] == "A" and cov == pytest.approx(75.0)
    assert tbx._best_sheet(fake.Polygon(None, None, (9e5, 9e5, 9e5 + 1, 9e5 + 1)), s) == (None, 0.0)


def test_count_nodata_any_band_excludes_source_nodata(tbx, fake):
    fake.RASTERS["r"] = dict(extent=(0.0, 0.0, 100.0, 100.0), cell=1.0, wkid=102165,
                             bands=3, ptype="U8", nodata_px=5, source_nodata_px=7)
    sheet = {"xmin": 0.0, "ymin": 0.0, "xmax": 100.0, "ymax": 100.0}
    ras = fake.Raster("r")
    assert tbx._count_nodata("r", ras, sheet, 0) == 5
    fake.RASTERS["r"]["bands"] = 1
    assert tbx._count_nodata("r", fake.Raster("r"), sheet, 0) == 5


def test_runlog_columns_are_aligned(tbx, fake, tmp_path):
    log = tbx.RunLog(str(tmp_path / "t.log"))
    names = ["O_+30-170_MAG.TIF", "O_+30-170_MAG_FERR.TIF"]
    w = tbx._width(names)
    for i, (name, status, dt, note) in enumerate(
            [(names[0], "OK", 16.8, ""), (names[1], "ERROR", 123.4, "ERROR 999999: x")], 1):
        log.progress(i, 254, "{} | {:<6}".format(name.ljust(w), status), dt, note)
    log.warn("warning line")
    log.error("error line")
    log.close()
    lines = (tmp_path / "t.log").read_text(encoding="utf-8").splitlines()
    seps = [[m.start() for m in re.finditer(r" \| ", ln)][:4] for ln in lines[:2]]
    assert seps[0] == seps[1]
    assert lines[1].endswith("| ERROR 999999: x")
    # "YYYY-MM-DD HH:MM:SS " + level tag padded to 7 + " ": text starts at column 28
    assert [ln[20:27] for ln in lines] == ["[INFO] ", "[INFO] ", "[WARN] ",
                                           "[ERROR]", "[INFO] "]
    assert all(ln[27] == " " and ln[28] != " " for ln in lines), lines
