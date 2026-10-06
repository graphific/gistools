"""QGIS layer file and per-band styles."""
from __future__ import annotations

import calendar
import colorsys
import math
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import numpy as np

from .table import (
    APART, BAND_NAMES, CELL_NOTE, FINE_TEXT, FLAG_NAMES, HEIGHT_BANDS, SOURCE_BANDS, SOURCE_NAMES,
)

HEIGHT_RAMP = ("#440154", "#3b528b", "#21918c", "#5ec962", "#fde725")
ERROR_RAMP = ("#fff5eb", "#fdbe85", "#fd8d3c", "#d94701", "#7f2704")
SHARE_RAMP = ("#D55E00", "#e8e8e8", "#0072B2")
YEAR_RAMP = ("#dadaeb", "#9e9ac8", "#6a51a3", "#3f007d")
SOURCE_COLOURS = {0: None, 1: "#0072B2", 2: "#D55E00", 3: "#bdbdbd"}


def shade(colours, share: float) -> str:
    """The colour a share of the way along a list of hex colours."""
    at = min(max(share, 0.0), 1.0) * (len(colours) - 1)
    i = min(int(at), len(colours) - 2)
    a, b = ([int(c[k:k + 2], 16) for k in (1, 3, 5)] for c in colours[i:i + 2])
    return "#" + "".join(f"{round(x + (y - x) * (at - i)):02x}" for x, y in zip(a, b, strict=True))


def _rgba(colour: str) -> str:
    return ",".join(str(int(colour[k:k + 2], 16)) for k in (1, 3, 5)) + ",255"


#: the parts of a QGIS raster style that do not change from band to band, as QGIS itself writes them
_PIPE_HEAD = ('<pipe><provider><resampling enabled="false" zoomedInResamplingMethod="nearestNeighbour" maxOversampling="2" '
              'zoomedOutResamplingMethod="nearestNeighbour"/></provider>')
_PIPE_ORIGIN = ('<minMaxOrigin><limits>None</limits><extent>WholeRaster</extent><statAccuracy>Estimated</statAccuracy>'
                '<cumulativeCutLower>0.02</cumulativeCutLower><cumulativeCutUpper>0.98</cumulativeCutUpper><stdDevFactor>2</stdDevFactor></minMaxOrigin>')
_PIPE_TAIL = ('<brightnesscontrast gamma="1" contrast="0" brightness="0"/><huesaturation colorizeStrength="100" saturation="0" colorizeRed="255" '
              'grayscaleMode="0" colorizeOn="0" colorizeBlue="128" invertColors="0" colorizeGreen="128"/><rasterresampler maxOversampling="2"/>'
              '<resamplingStage>resamplingFilter</resamplingStage></pipe>')


def stretched(band: int, lo: float, hi: float, colours, unit: str = "", clear=None) -> str:
    """QGIS's style for one band drawn as a colour ramp from `lo` to `hi`, with a legend entry at five values; `clear`
    is a value shown transparent."""
    stops = ":".join(f"{k / (len(colours) - 1):g};{_rgba(c)}" for k, c in enumerate(colours[1:-1], 1))
    items = "".join(f'<item value="{lo + (hi - lo) * k / 4:g}" label="{lo + (hi - lo) * k / 4:g}{unit}" alpha="255" color="{shade(colours, k / 4)}"/>' for k in range(5))
    gone = f'<singleValuePixelList><pixelListEntry min="{clear}" max="{clear}" percentTransparent="100"/></singleValuePixelList>' if clear is not None else ""
    return (f'{_PIPE_HEAD}<rasterrenderer nodataColor="" band="{band}" classificationMax="{hi:g}" opacity="1" classificationMin="{lo:g}" alphaBand="-1" '
            f'type="singlebandpseudocolor"><rasterTransparency>{gone}</rasterTransparency>{_PIPE_ORIGIN}<rastershader><colorrampshader maximumValue="{hi:g}" '
            f'labelPrecision="1" clip="0" colorRampType="INTERPOLATED" classificationMode="1" minimumValue="{lo:g}"><colorramp name="[source]" type="gradient">'
            f'<Option type="Map"><Option value="{_rgba(colours[0])}" name="color1" type="QString"/><Option value="{_rgba(colours[-1])}" name="color2" type="QString"/>'
            f'<Option value="0" name="discrete" type="QString"/><Option value="gradient" name="rampType" type="QString"/>'
            f'<Option value="{stops}" name="stops" type="QString"/></Option></colorramp>{items}<rampLegendSettings useContinuousLegend="1" direction="0" '
            f'maximumLabel="" orientation="2" minimumLabel="" prefix="" suffix="{unit}"/></colorrampshader></rastershader></rasterrenderer>{_PIPE_TAIL}')


def classed(band: int, entries) -> str:
    """QGIS's style for one band of classes: [(value, its colour or None for transparent, its name)]."""
    rows = "".join(f'<paletteEntry value="{value}" label={quoteattr(name)} alpha="{255 if colour else 0}" color="{colour or "#ffffff"}"/>' for value, colour, name in entries)
    return (f'{_PIPE_HEAD}<rasterrenderer nodataColor="" band="{band}" opacity="1" alphaBand="-1" type="paletted"><rasterTransparency/>{_PIPE_ORIGIN}'
            f'<colorPalette>{rows}</colorPalette><colorramp name="[source]" type="randomcolors"><Option/></colorramp></rasterrenderer>{_PIPE_TAIL}')


def flag_name(value: int) -> str:
    """What a value of `flags` says, its bits spelled out."""
    return " + ".join(text for bit, text in FLAG_NAMES.items() if value & bit) or "none"


def _source_styles(source: dict) -> dict[str, str]:
    """QGIS style pipe per source-file band. Classes come from the 10 m arrays."""
    years = sorted({int(y) for name in ("year", "meta_year", "eth_year") for y in np.unique(source[name]).tolist()} - {0})
    by_year = [(0, "#f0f0f0", "0: unknown")] + [(y, shade(YEAR_RAMP, k / max(len(years) - 1, 1)), str(y)) for k, y in enumerate(years)]
    months = [(0, "#f0f0f0", "0: none")] + [
        (m, "#" + "".join(f"{round(255 * c):02x}" for c in colorsys.hsv_to_rgb((m - 1) / 12, 0.55, 0.92)), f"{m}: {calendar.month_name[m]}") for m in range(1, 13)]
    flags = [(v, "#e4e4e4" if v == 0 else "#CC79A7" if v < APART else "#E69F00" if v == APART else "#a85d00", f"{v}: {flag_name(v)}") for v in range(2 * APART)]
    classes = {"source": [(v, SOURCE_COLOURS[v], f"{v}: {name}") for v, name in SOURCE_NAMES.items()], "year": by_year, "meta_year": by_year,
               "meta_month": months, "eth_year": by_year, "flags": flags}
    return {name: classed(i, classes[name]) if name in classes else stretched(i, 0, 100, SHARE_RAMP, " %", clear=SOURCE_BANDS[name][0])
            for i, name in enumerate(SOURCE_BANDS, 1)}


def drawn(planes: dict, source: dict, fine: list[str], fine_source: list[str], debug: bool) -> list[tuple[str, int, str, str, str]]:
    """(file, band, layer name, description, QGIS style). 10 m `height_m` keeps the plain band name, so it is the layer switched on."""
    seen = np.r_[planes["height_m"].ravel(), planes["eth_m"].ravel(), planes["meta_top_m"].ravel()]
    reach = max(10.0, 5.0 * math.ceil(float(np.nanpercentile(seen, 99.5)) / 5)) if np.isfinite(seen).any() else 30.0
    how = {"height": (0, reach, HEIGHT_RAMP, " m"), "error": (0, 10, ERROR_RAMP, " m"), "share": (0, 1, SHARE_RAMP, "")}
    bands = HEIGHT_BANDS if debug else HEIGHT_BANDS[:1]
    layers = []
    for file in fine:
        stem = Path(file).stem
        for i, (name, _unit, kind, text) in enumerate(bands, 1):
            label = f"{stem} {name}" if debug else stem
            desc = FINE_TEXT if name == "height_m" else f"{text}. {CELL_NOTE}"
            layers.append((file, i, label, desc, stretched(i, *how[kind])))
    layers += [("chm_10m.tif", i, name, text, stretched(i, *how[kind])) for i, (name, _unit, kind, text) in enumerate(bands, 1)]
    if not debug:
        return layers
    pipes = _source_styles(source)
    for file in ("chm_10m_source.tif", *fine_source):
        prefix = "" if file == "chm_10m_source.tif" else Path(file).stem + " "
        note = "" if prefix == "" else " " + CELL_NOTE
        layers += [(file, i, f"{prefix}{name}", SOURCE_BANDS[name][1] + note, pipes[name]) for i, name in enumerate(SOURCE_BANDS, 1)]
    return layers


def write_qgis(out: Path, layers: list, rasterio) -> list[str]:
    """The QGIS files beside the maps: a default style for each file (QGIS applies `<file>.qml` when the file is
    opened) and `chm_layers.qlr`, which opens every band as a layer of its own with its legend, the calibrated height
    switched on and the rest off. A layer is placed by the grid its own file is on: QGIS takes a layer's grid from the
    definition and not from the file, and the 1 m files are on Meta's grid, not the 10 m one. The group that holds the
    layers sits inside a nameless one: QGIS reads the children of a definition's first group and drops the group."""
    def style(pipe: str) -> str:
        return ("<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>\n"
                f'<qgis maxScale="0" styleCategories="Symbology" minScale="1e+08" version="3.34.0" hasScaleBasedVisibilityFlag="0">\n{pipe}\n<blendMode>0</blendMode>\n</qgis>\n')

    srs, written = {}, ["chm_layers.qlr"]
    for file in dict.fromkeys(layer[0] for layer in layers):
        (out / file).with_suffix(".qml").write_text(style(next(pipe for f, band, _name, _text, pipe in layers if f == file and band == 1)))
        written.append(Path(file).with_suffix(".qml").name)
        with rasterio.open(out / file) as src:
            srs[file] = f'<srs><spatialrefsys nativeFormat="Wkt"><wkt>{escape(src.crs.to_wkt())}</wkt><authid>{src.crs.to_string()}</authid></spatialrefsys></srs>'
    tree, maps = "", ""
    for file, band, name, text, pipe in layers:
        on = name == BAND_NAMES[0]
        layer_id = "chm_" + "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
        tree += (f'<layer-tree-layer id="{layer_id}" name="{name}" source="./{file}" providerKey="gdal" checked="{"Qt::Checked" if on else "Qt::Unchecked"}" '
                 f'expanded="{int(on)}"><customproperties><Option/></customproperties></layer-tree-layer>')
        maps += (f'<maplayer type="raster" hasScaleBasedVisibilityFlag="0" maxScale="0" minScale="1e+08" styleCategories="AllStyleCategories"><id>{layer_id}</id>'
                 f'<datasource>./{file}</datasource><layername>{name}</layername><abstract>{escape(text)}</abstract>{srs[file]}<provider encoding="">gdal</provider>'
                 f'<noData><noDataList bandNo="{band}" useSrcNoData="1"/></noData>{pipe}<blendMode>0</blendMode></maplayer>')
    group = '<layer-tree-group name="{}" checked="Qt::Checked" expanded="1"><customproperties><Option/></customproperties>{}</layer-tree-group>'
    (out / "chm_layers.qlr").write_text(f'<!DOCTYPE qgis-layer-definition>\n<qlr>\n{group.format("", group.format("calibrated canopy height", tree))}\n'
                                        f'<maplayers>{maps}</maplayers>\n</qlr>\n')
    return written

