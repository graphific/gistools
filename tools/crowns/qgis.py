"""QGIS styles for the tops, trunks, crowns and canopy raster."""
from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from .io import CANOPY, FORMATS

HEIGHT_RAMP = ("#440154", "#3b528b", "#21918c", "#5ec962", "#fde725")
HEIGHT_CLASSES = 5
_PIPE_HEAD = ('<pipe><provider><resampling enabled="false" zoomedInResamplingMethod="nearestNeighbour" maxOversampling="2" '
              'zoomedOutResamplingMethod="nearestNeighbour"/></provider>')
_PIPE_ORIGIN = ('<minMaxOrigin><limits>None</limits><extent>WholeRaster</extent><statAccuracy>Estimated</statAccuracy>'
                '<cumulativeCutLower>0.02</cumulativeCutLower><cumulativeCutUpper>0.98</cumulativeCutUpper><stdDevFactor>2</stdDevFactor></minMaxOrigin>')
_PIPE_TAIL = ('<brightnesscontrast gamma="1" contrast="0" brightness="0"/><huesaturation colorizeStrength="100" saturation="0" colorizeRed="255" '
              'grayscaleMode="0" colorizeOn="0" colorizeBlue="128" invertColors="0" colorizeGreen="128"/><rasterresampler maxOversampling="2"/>'
              '<resamplingStage>resamplingFilter</resamplingStage></pipe>')


def shade(colours, share: float) -> str:
    """The colour a share of the way along a list of hex colours."""
    at = min(max(share, 0.0), 1.0) * (len(colours) - 1)
    i = min(int(at), len(colours) - 2)
    a, b = ([int(c[k:k + 2], 16) for k in (1, 3, 5)] for c in colours[i:i + 2])
    return "#" + "".join(f"{round(x + (y - x) * (at - i)):02x}" for x, y in zip(a, b, strict=True))


def _rgba(colour: str, alpha: int = 255) -> str:
    return ",".join(str(int(colour[k:k + 2], 16)) for k in (1, 3, 5)) + f",{alpha}"


def _symbol(name: str, kind: str, layer: str, properties: dict) -> str:
    """One symbol of a QGIS vector style, as QGIS writes it: a single layer of class `layer` with its properties."""
    options = "".join(f'<Option name="{k}" value="{v}" type="QString"/>' for k, v in properties.items())
    return (f'<symbol name="{name}" type="{kind}" alpha="1" clip_to_extent="1" force_rhr="0"><layer class="{layer}" enabled="1" locked="0" pass="0">'
            f'<Option type="Map">{options}</Option></layer></symbol>')


def crown_style(reach: float) -> str:
    """QGIS's style for the crowns: filled by the tree's height in HEIGHT_CLASSES classes up to `reach`, outlined."""
    step = reach / HEIGHT_CLASSES
    ranges = "".join(f'<range lower="{k * step:g}" upper="{(k + 1) * step if k < HEIGHT_CLASSES - 1 else 1000:g}" symbol="{k}" render="true" '
                     f'label={quoteattr(f"{k * step:g} to {(k + 1) * step:g} m" if k < HEIGHT_CLASSES - 1 else f"{k * step:g} m and taller")}/>' for k in range(HEIGHT_CLASSES))
    symbols = "".join(_symbol(str(k), "fill", "SimpleFill", {"color": _rgba(shade(HEIGHT_RAMP, k / (HEIGHT_CLASSES - 1)), 120), "style": "solid",
                                                              "outline_color": "255,255,255,255", "outline_style": "solid", "outline_width": "0.3",
                                                              "outline_width_unit": "MM", "joinstyle": "round"}) for k in range(HEIGHT_CLASSES))
    return (f'<renderer-v2 type="graduatedSymbol" attr="height_m" graduatedMethod="GraduatedColor" symbollevels="0" enableorderby="0" forceraster="0" '
            f'referencescale="-1"><ranges>{ranges}</ranges><symbols>{symbols}</symbols><classificationMethod id="Custom"/></renderer-v2>')


def dot_style(fill: str, rim: str, size: float) -> str:
    """QGIS's style for a layer of points: one small dot with a rim."""
    dot = _symbol("0", "marker", "SimpleMarker", {"name": "circle", "color": fill, "outline_color": rim, "outline_style": "solid",
                                                   "outline_width": "0.3", "outline_width_unit": "MM", "size": f"{size:g}", "size_unit": "MM"})
    return (f'<renderer-v2 type="singleSymbol" symbollevels="0" enableorderby="0" forceraster="0" referencescale="-1"><symbols>{dot}</symbols>'
            '<rotation/><sizescale/></renderer-v2>')


def height_style(reach: float) -> str:
    """QGIS's style for the canopy raster: the heights' colours from 0 to `reach`, with a legend entry at five values."""
    stops = ":".join(f"{k / (len(HEIGHT_RAMP) - 1):g};{_rgba(c)}" for k, c in enumerate(HEIGHT_RAMP[1:-1], 1))
    items = "".join(f'<item value="{reach * k / 4:g}" label="{reach * k / 4:g} m" alpha="255" color="{shade(HEIGHT_RAMP, k / 4)}"/>' for k in range(5))
    return (f'{_PIPE_HEAD}<rasterrenderer nodataColor="" band="1" classificationMax="{reach:g}" opacity="1" classificationMin="0" alphaBand="-1" '
            f'type="singlebandpseudocolor"><rasterTransparency/>{_PIPE_ORIGIN}<rastershader><colorrampshader maximumValue="{reach:g}" '
            f'labelPrecision="1" clip="0" colorRampType="INTERPOLATED" classificationMode="1" minimumValue="0"><colorramp name="[source]" type="gradient">'
            f'<Option type="Map"><Option value="{_rgba(HEIGHT_RAMP[0])}" name="color1" type="QString"/><Option value="{_rgba(HEIGHT_RAMP[-1])}" name="color2" type="QString"/>'
            f'<Option value="0" name="discrete" type="QString"/><Option value="gradient" name="rampType" type="QString"/>'
            f'<Option value="{stops}" name="stops" type="QString"/></Option></colorramp>{items}<rampLegendSettings useContinuousLegend="1" direction="0" '
            f'maximumLabel="" orientation="2" minimumLabel="" prefix="" suffix=" m"/></colorrampshader></rastershader></rasterrenderer>{_PIPE_TAIL}')


def write_qgis(out: Path, crs, reach: float, formats) -> list[str]:
    """`<file>.qml` for each file, and `trees.qlr` opening tops, trunks, crowns and the canopy.

    The layer group sits inside an empty group: QGIS reads the children of the first group and drops that group.
    A GeoJSON layer is longitude and latitude. Where both formats were written, the GeoPackages are opened."""
    from rasterio.crs import CRS

    def style(body: str, geometry: int | None = None) -> str:
        kind = f"<layerGeometryType>{geometry}</layerGeometryType>\n" if geometry is not None else ""
        return ("<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>\n"
                f'<qgis maxScale="0" styleCategories="Symbology" minScale="1e+08" version="3.34.0" hasScaleBasedVisibilityFlag="0">\n{body}\n<blendMode>0</blendMode>\n{kind}</qgis>\n')

    def system(of) -> str:
        return f'<srs><spatialrefsys nativeFormat="Wkt"><wkt>{escape(of.to_wkt())}</wkt><authid>{of.to_string()}</authid></spatialrefsys></srs>' if of is not None else ""

    ending = FORMATS["gpkg" if "gpkg" in formats else "geojson"][1]
    srs = {"raster": system(crs), "vector": system(CRS.from_epsg(4326) if ending == ".geojson" and crs is not None else crs)}

    def source(name: str) -> str:
        return f"./{name}{ending}" + (f"|layername={name}" if ending == ".gpkg" else "")

    layers = (("tree_tops", source("tree_tops"), "ogr", "vector", 'geometry="Point" wkbType="Point"', dot_style("255,255,255,255", "30,30,30,255", 1.6),
               "the top of every tree found: a point with its height and its crown's size", 0),
              ("tree_stems", source("tree_stems"), "ogr", "vector", 'geometry="Point" wkbType="Point"', dot_style("120,70,20,255", "255,255,255,255", 1.4),
               "where the trunk of every tree found is expected to stand", 0),
              ("tree_crowns", source("tree_crowns"), "ogr", "vector", 'geometry="Polygon" wkbType="Polygon"', crown_style(reach),
               "the crown of every tree found, coloured by the tree's height", 2),
              ("canopy_height", f"./{CANOPY}", "gdal", "raster", "", height_style(reach), "the canopy raster the trees were read from, in metres above ground", None))
    tree, maps = "", ""
    for name, source_, provider, kind, shape, body, text, geometry in layers:
        (out / f"{name}.qml").write_text(style(body, geometry))
        tree += (f'<layer-tree-layer id="trees_{name}" name="{name}" source={quoteattr(source_)} providerKey="{provider}" checked="Qt::Checked" expanded="1">'
                 '<customproperties><Option/></customproperties></layer-tree-layer>')
        extra = '<noData><noDataList bandNo="1" useSrcNoData="1"/></noData>' if kind == "raster" else ""
        maps += (f'<maplayer type="{kind}" {shape} hasScaleBasedVisibilityFlag="0" maxScale="0" minScale="1e+08" styleCategories="AllStyleCategories"><id>trees_{name}</id>'
                 f'<datasource>{escape(source_)}</datasource><layername>{name}</layername><abstract>{escape(text)}</abstract>{srs[kind]}'
                 f'<provider encoding="{"UTF-8" if kind == "vector" else ""}">{provider}</provider>{extra}{body}<blendMode>0</blendMode></maplayer>')
    group = '<layer-tree-group name="{}" checked="Qt::Checked" expanded="1"><customproperties><Option/></customproperties>{}</layer-tree-group>'
    (out / "trees.qlr").write_text(f'<!DOCTYPE qgis-layer-definition>\n<qlr>\n{group.format("", group.format("trees", tree))}\n<maplayers>{maps}</maplayers>\n</qlr>\n')
    return ["trees.qlr", *(f"{name}.qml" for name, *_ in layers)]
