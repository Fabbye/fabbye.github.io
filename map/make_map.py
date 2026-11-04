#!/usr/bin/env python3
"""Draw the map on the back of the card.

The map shows Reykjavík and Esja, with the University of Iceland marked. It is
made from the Copernicus DEM GLO-30: the script downloads the two tiles that
cover the area, projects them to ISN93 / Lambert 1993 (EPSG:3057), smooths the
surface a little, draws contours and the coastline, and writes the result into
index.html between the <!-- map:start --> and <!-- map:end --> markers.

    pip install numpy scipy rasterio contourpy shapely pyproj
    python map/make_map.py

Contours derived from Copernicus DEM GLO-30: © DLR e.V. 2010-2014 and
© Airbus Defence and Space GmbH 2014-2018, provided under COPERNICUS by the
European Union and ESA; all rights reserved.
"""

import math
import pathlib
import re
import urllib.request

import numpy as np
import rasterio
from contourpy import FillType, LineType, contour_generator
from pyproj import Transformer
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject
from scipy.ndimage import gaussian_filter
from shapely.geometry import LineString, Polygon

ROOT = pathlib.Path(__file__).resolve().parent.parent
CACHE = ROOT / "map" / ".cache"
INDEX_HTML = ROOT / "index.html"

TILE_URL = "https://copernicus-dem-30m.s3.amazonaws.com/{0}/{0}.tif"
TILES = [
    "Copernicus_DSM_COG_10_N64_00_W023_00_DEM",
    "Copernicus_DSM_COG_10_N64_00_W022_00_DEM",
]

CRS = "EPSG:3057"  # ISN93 / Lambert 1993
CELL = 25  # metres per cell of the projected grid
MARGIN = 40  # extra cells around the frame, so smoothing has no edge effects

# Map frame in EPSG:3057 metres: 30.9 x 20 km, the 85:55 shape of a business card.
FRAME = (346_000, 402_500, 376_900, 422_500)  # xmin, ymin, xmax, ymax
VIEW_W, VIEW_H = 1700, 1100  # SVG units across and down the frame

INTERVAL = 50  # contour interval in metres
INDEX = 250  # every index contour is drawn heavier
SMOOTH = 2.0  # Gaussian smoothing of the surface, in cells
SIMPLIFY = 1.2  # Douglas-Peucker tolerance, in SVG units
MIN_LENGTH = 14  # drop contours shorter than this, in SVG units

POI = ("University of Iceland", 64.14056, -21.94944)  # Main Building
LABELS = [  # text, lat, lon, css class
    ("Esja", 64.2567, -21.6865, "peak"),
    ("Reykjavík", 64.1229, -21.8258, "place"),
    ("Faxaflói", 64.1741, -22.0474, "water"),
]
SCALE_KM = 5

TO_GRID = Transformer.from_crs("EPSG:4326", CRS, always_xy=True)


def download_tiles():
    CACHE.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in TILES:
        path = CACHE / f"{name}.tif"
        if not path.exists():
            print(f"downloading {name}")
            urllib.request.urlretrieve(TILE_URL.format(name), path)
        paths.append(path)
    return paths


def projected_dem(paths):
    """Mosaic the tiles onto one EPSG:3057 grid around the frame."""
    xmin, ymin, xmax, ymax = FRAME
    width = round((xmax - xmin) / CELL) + 2 * MARGIN
    height = round((ymax - ymin) / CELL) + 2 * MARGIN
    transform = from_origin(xmin - MARGIN * CELL, ymax + MARGIN * CELL, CELL, CELL)
    dem = np.full((height, width), np.nan, dtype="float32")
    for path in paths:
        with rasterio.open(path) as src:
            part = np.full_like(dem, np.nan)
            reproject(
                rasterio.band(src, 1), part,
                dst_transform=transform, dst_crs=CRS, dst_nodata=np.nan,
                resampling=Resampling.bilinear,
            )
            dem = np.where(np.isnan(dem), part, dem)
    if np.isnan(dem).any():
        raise SystemExit("the tiles do not cover the whole frame")
    return dem


def to_view(xs, ys):
    """EPSG:3057 metres to SVG units, with y pointing down."""
    xmin, ymin, xmax, ymax = FRAME
    return ((np.asarray(xs) - xmin) * VIEW_W / (xmax - xmin),
            (ymax - np.asarray(ys)) * VIEW_H / (ymax - ymin))


def place(lat, lon):
    vx, vy = to_view(*TO_GRID.transform([lon], [lat]))
    return round(float(vx[0]), 1), round(float(vy[0]), 1)


def path_data(lines, closed=False):
    """Compact SVG path data: an absolute moveto, then relative linetos in whole units."""
    out = []
    for coords in lines:
        pts = np.rint(np.asarray(coords)).astype(int)
        keep = np.ones(len(pts), bool)
        keep[1:] = np.any(pts[1:] != pts[:-1], axis=1)
        pts = pts[keep]
        if len(pts) < 2:
            continue
        steps = " ".join(f"{dx} {dy}" for dx, dy in np.diff(pts, axis=0))
        out.append(f"M{pts[0][0]} {pts[0][1]}l{steps}{'z' if closed else ''}")
    return re.sub(r" -", "-", "".join(out))


def contour_lines(z, xs, ys, level):
    lines = []
    for seg in contour_generator(xs, ys, z, line_type=LineType.Separate).lines(level):
        line = LineString(np.column_stack(to_view(seg[:, 0], seg[:, 1])))
        if line.length < MIN_LENGTH:
            continue
        line = line.simplify(SIMPLIFY, preserve_topology=False)
        if len(line.coords) >= 2:
            lines.append(list(line.coords))
    return lines


def sea_rings(land, xs, ys):
    rings = []
    gen = contour_generator(xs, ys, land, fill_type=FillType.OuterOffset)
    for pts, offsets in zip(*gen.filled(-1.0, 0.5)):
        for a, b in zip(offsets[:-1], offsets[1:]):
            ring = Polygon(np.column_stack(to_view(pts[a:b, 0], pts[a:b, 1])))
            if abs(ring.area) < 30:  # specks smaller than a few pixels
                continue
            rings.append(list(ring.exterior.simplify(SIMPLIFY * 0.6).coords))
    return rings


def dms(value, pos, neg):
    hemi = pos if value >= 0 else neg
    total = round(abs(value) * 3600)
    d, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{d}°{m:02d}′{s:02d}″{hemi}"


def north_rotation(lat, lon):
    """Angle of true north from grid north at a point, in degrees clockwise."""
    x0, y0 = TO_GRID.transform(lon, lat)
    x1, y1 = TO_GRID.transform(lon, lat + 0.01)
    return math.degrees(math.atan2(x1 - x0, y1 - y0))


def build():
    dem = projected_dem(download_tiles())
    surface = gaussian_filter(dem, SMOOTH)[MARGIN:-MARGIN, MARGIN:-MARGIN]
    land = gaussian_filter((dem > 0.5).astype("float32"), 1.0)[MARGIN:-MARGIN, MARGIN:-MARGIN]
    xmin, ymin, xmax, ymax = FRAME
    xs = xmin + CELL * (np.arange(surface.shape[1]) + 0.5)
    ys = ymax - CELL * (np.arange(surface.shape[0]) + 0.5)

    minor, major = [], []
    for level in range(INTERVAL, int(surface.max()) + 1, INTERVAL):
        (major if level % INDEX == 0 else minor).extend(contour_lines(surface, xs, ys, level))
    sea = sea_rings(land, xs, ys)
    coast = contour_lines(land, xs, ys, 0.5)

    name, lat, lon = POI
    coords = f"{dms(lat, 'N', 'S')}&ensp;{dms(lon, 'E', 'W')}"
    px, py = place(lat, lon)
    mark = 60  # size of the marker in SVG units; its dot sits at (30, 36)

    labels = []
    for text, llat, llon, cls in LABELS:
        x, y = place(llat, llon)
        labels.append(f'<text class="label {cls}" x="{x}" y="{y}" text-anchor="middle">{text}</text>')

    svg = "\n".join([
        f'<svg class="map" viewBox="0 0 {VIEW_W} {VIEW_H}" role="img" aria-labelledby="map-title">',
        f'<title id="map-title">Contour map of Reykjavík and Esja, with the {name} marked</title>',
        f'<path class="sea" d="{path_data(sea, closed=True)}"/>',
        f'<path class="contour" d="{path_data(minor)}"/>',
        f'<path class="contour index" d="{path_data(major)}"/>',
        f'<path class="coast" d="{path_data(coast)}"/>',
        *labels,
        f'<use class="poi" href="#trig" x="{px - mark / 2}" y="{py - mark * 0.6}" width="{mark}" height="{mark}"/>',
        "</svg>",
    ])

    cqi_per_km = 100 / ((xmax - xmin) / 1000)  # the map spans the card's full width
    key = "\n".join([
        '<figcaption class="map-key">',
        f'<span class="poi-name"><svg class="trig" aria-hidden="true"><use href="#trig"/></svg>{name}</span>',
        f'<span class="coords">{coords}</span>',
        f'<span class="scale" style="--scale-width: {SCALE_KM * cqi_per_km:.2f}cqi" aria-hidden="true">',
        '<span class="scale-bar"></span>',
        f'<span class="scale-labels"><span>0</span><span>{SCALE_KM} km</span></span>',
        f'<svg class="north" viewBox="-10 -14 20 28" style="rotate: {north_rotation(lat, lon):.1f}deg">'
        '<path d="M0-13 6 7 0 3-6 7Z"/></svg>',
        "</span>",
        "</figcaption>",
    ])
    return svg + "\n" + key


def main():
    block = build()
    html = INDEX_HTML.read_text(encoding="utf-8")
    match = re.search(r"([ \t]*)(<!-- map:start[^>]*-->).*?(<!-- map:end -->)", html, re.S)
    if not match:
        raise SystemExit("index.html has no <!-- map:start --> ... <!-- map:end --> markers")
    indent, start, end = match.groups()
    html = html[:match.start()] + f"{indent}{start}\n{block}\n{indent}{end}" + html[match.end():]
    INDEX_HTML.write_text(html, encoding="utf-8")
    print(f"updated {INDEX_HTML.name}, map block is {len(block.encode()) / 1024:.0f} KB")


if __name__ == "__main__":
    main()
