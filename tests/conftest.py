"""Test setup for MapSheetMosaic.pyt.

The toolbox imports arcpy at module load time. `arcpy_stub` is installed
under the name `arcpy` before the .pyt is executed with importlib, so the
tools run end to end on a machine without ArcGIS Pro. The `collection`
fixture builds a small synthetic archive: four 2 x 2 km sheets in two
1:25k groups and a set of scans covering the usual cases (duplicates,
low coverage, no sheet, CRS mismatch, unmatched file name, failing clip).
"""

import csv
import glob
import importlib.machinery
import importlib.util
import os
import sys
from types import SimpleNamespace

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.dont_write_bytecode = True
sys.path.insert(0, HERE)

import arcpy_stub  # noqa: E402

sys.modules["arcpy"] = arcpy_stub


def _load_toolbox():
    path = os.path.join(ROOT, "MapSheetMosaic.pyt")
    loader = importlib.machinery.SourceFileLoader("MapSheetMosaic", path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


TOOLBOX = _load_toolbox()


@pytest.fixture
def tbx():
    return TOOLBOX


@pytest.fixture
def fake():
    arcpy_stub.reset()
    yield arcpy_stub
    arcpy_stub.reset()


def _sheet(x, y, code, f25, f50):
    return ((float(x), float(y), x + 2000.0, y + 2000.0),
            {"COD_50255k": code, "Folha_25k": f25, "Folha_50k": f50})


@pytest.fixture
def collection(fake, tmp_path):
    inp, sub = tmp_path / "in", tmp_path / "in" / "sub"
    out, out2, mosaic = tmp_path / "out", tmp_path / "out2", tmp_path / "mosaic"
    for d in (sub, out, out2, mosaic):
        d.mkdir(parents=True)

    fake.FCS["carto"] = dict(wkid=102165, rows=[
        _sheet(200000, 138000, "51B60925", 609.0, "51-B"),
        _sheet(202000, 138000, "51B60926", 609.0, "51-B"),
        _sheet(200000, 136000, "51B61001", 610.0, "51-B"),
        _sheet(202000, 136000, "51B61002", 610.0, "51-B"),
    ])

    def scan(name, folder, extent, **kw):
        path = os.path.join(str(folder), name)
        open(path, "w").close()
        d = dict(extent=tuple(float(v) for v in extent), cell=0.423, wkid=102165,
                 bands=3, ptype="U8")
        d.update(kw)
        fake.RASTERS[path] = d
        return path

    collar = (199900, 137900, 202100, 140100)          # sheet 51B60925 + 100 m collar
    scan("O_+0-160_MAG.TIF", inp, collar)
    scan("O_+0-160_MAG2.TIF", inp, collar, nodata_px=5, source_nodata_px=7)
    scan("O_+0-160_MAG_FERR.tif", sub, (199950, 137950, 202050, 140050))
    scan("O_+2-160_MAG.TIF", inp, (201900, 137900, 204100, 140100))
    scan("O_+0-162_MAG.tiff", sub, (200000, 136500, 202000, 138000))   # 75 % coverage
    scan("O_+9-999_MAG.TIF", inp, (500000, 500000, 502000, 502000))    # no sheet
    scan("O_+0-170_MAG.TIF", inp, collar, wkid=3763)                   # other CRS
    scan("BADNAME.TIF", inp, (201900, 135900, 204100, 138100))         # regex mismatch
    scan("O_+2-162_FAIL.TIF", inp, (201900, 135900, 204100, 138100))   # clip fails
    scan("O_+2-164_MAG.jp2", inp, (201900, 135900, 204100, 138100))    # extension not listed
    scan("MAG_old.tif", out, (201900, 135900, 204100, 138100))         # inside output folder
    open(os.path.join(str(inp), "O_+0-160_MAG.TIF.aux.xml"), "w").close()

    return SimpleNamespace(root=str(tmp_path), inp=str(inp), sub=str(sub), out=str(out),
                           out2=str(out2), mosaic=str(mosaic))


def make_params(tool, **values):
    """getParameterInfo() with the given values set by parameter name."""
    params = tool.getParameterInfo()
    for p in params:
        if p.name in values:
            if p.multiValue:
                p.values = values[p.name]
            else:
                p.value = values[p.name]
    return params


def latest(folder, pattern):
    found = sorted(glob.glob(os.path.join(folder, pattern)))
    assert found, "no file {} in {}".format(pattern, folder)
    return found[-1]


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


def calls(fake, name):
    return [c for c in fake.CALLS if c[0] == name]
