# SPDX-License-Identifier: LGPL-2.1-or-later

"""Unit tests for display color normalize + DomainValue paint attachment."""

from __future__ import annotations

import pytest

from cadex_appearance import (
    appearance_cache_key,
    appearance_from_color,
    appearance_from_properties,
    normalize_color,
)


def test_normalize_color_floats():
    assert normalize_color((1, 0, 0)) == (1.0, 0.0, 0.0, 1.0)
    assert normalize_color([0.2, 0.4, 0.6, 0.5]) == (0.2, 0.4, 0.6, 0.5)


def test_normalize_color_bytes():
    r, g, b, a = normalize_color([255, 128, 0])
    assert r == pytest.approx(1.0)
    assert g == pytest.approx(128 / 255.0)
    assert b == pytest.approx(0.0)
    assert a == pytest.approx(1.0)


def test_normalize_color_hex():
    r, g, b, a = normalize_color("#ff8000")
    assert r == pytest.approx(1.0)
    assert g == pytest.approx(128 / 255.0)
    assert b == pytest.approx(0.0)
    assert a == pytest.approx(1.0)


def test_normalize_color_rejects_bad():
    with pytest.raises(ValueError):
        normalize_color("red")
    with pytest.raises(ValueError):
        normalize_color((300.0, 0.0, 0.0))
    with pytest.raises(ValueError):
        normalize_color((1, 0))


def test_appearance_from_color_and_properties():
    app = appearance_from_color("#00ff00")
    assert app["diffuse"][1] == pytest.approx(1.0)
    assert appearance_from_properties({"appearance": app}) == app
    assert appearance_from_properties({}) is None
    assert "d:1.000000" in appearance_cache_key(app)



def test_part_box_and_paint_color_property():
    from CadexScriptedDomains import XSCRIPT_WORKBENCH_PACKS
    from cadex_part_api import PartDomainAPI

    pack = XSCRIPT_WORKBENCH_PACKS["PartWorkbench"]
    api = PartDomainAPI(pack.api_exports, pack.output_types)
    cube = api.box(10, 20, 30, color=(1, 0, 0), label="Red")
    assert cube.properties["label"] == "Red"
    assert cube.properties["appearance"]["diffuse"][0] == pytest.approx(1.0)
    painted = api.paint(cube, color="#0000ff")
    assert painted.operation == "paint"
    assert painted.properties["appearance"]["diffuse"][2] == pytest.approx(1.0)


def test_paint_faces_list_and_map():
    from CadexScriptedDomains import XSCRIPT_WORKBENCH_PACKS
    from cadex_part_api import PartDomainAPI

    pack = XSCRIPT_WORKBENCH_PACKS["PartWorkbench"]
    api = PartDomainAPI(pack.api_exports, pack.output_types)
    body = api.box(10, 10, 10, color=(0.5, 0.5, 0.5))
    faces = api.paint_faces(body, faces=[1, 2], color=(1, 0, 0))
    app = faces.properties["appearance"]
    assert app["diffuse"][0] == pytest.approx(0.5)
    assert app["faces"]["1"][0] == pytest.approx(1.0)
    assert app["faces"]["2"][0] == pytest.approx(1.0)
    multi = api.paint_faces(
        faces, colors={3: "#00ff00", 1: (0, 0, 1)}
    )
    app2 = multi.properties["appearance"]
    assert app2["faces"]["1"][2] == pytest.approx(1.0)  # blue overwrite
    assert app2["faces"]["2"][0] == pytest.approx(1.0)  # kept red
    assert app2["faces"]["3"][1] == pytest.approx(1.0)  # green
    assert "paint_faces" in pack.api_exports
