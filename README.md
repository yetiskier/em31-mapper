# EM31 Mapper

Real-time ground-conductivity mapping for a Geonics **EM31-MK2** plus any NMEA GPS,
built for a **Juniper Mesa2** (Windows 10, 7" touch, portrait or landscape).

It reads both instruments over COM ports, positions every EM31 reading by
interpolating between GPS fixes, logs everything to disk, and draws a live
gridded conductivity (or in-phase) map with your track as you walk.


**[User manual](docs/USER_MANUAL.md)** — installing on the Mesa2, field workflow, every setting, output files and troubleshooting.

## Getting it onto the Mesa2

**Prebuilt (recommended):** unzip `EM31Mapper-Windows-x64.zip`, copy the `EM31Mapper`
folder to the Mesa2 and run `EM31Mapper.exe`. Nothing else to install: Python, Qt
and the Microsoft C++ runtime are all inside the folder. See `README-FIRST.txt` in it.
`EM31Mapper.exe --selftest` checks the install end to end (report in
`Documents\EM31Mapper\selftest.txt`).

**Rebuilding yourself** (after changing the code), on any Windows PC with Python 3.12:
double-click `build_exe.bat`. It installs the pinned packages from `requirements.txt`,
builds from `EM31Mapper.spec` and runs the self-test on the result. Or push to GitHub
and the included workflow does the same on a Windows runner.

Dependencies are pinned on purpose: Qt 6.9 (6.10+ needs a Windows component missing
from older Windows 10 images) and NumPy 2.0. SciPy isn't needed.

## Field setup

| Item | Setting |
|---|---|
| EM31-MK2 | 9600 baud, 8N1 (fixed by the instrument). RS-232 cable via USB-serial adapter, or Bluetooth SPP. |
| GPS | Any receiver streaming **GGA** (GSA optional, for PDOP). Mesa2 internal GNSS or an external receiver over Bluetooth/USB. |
| Bluetooth | Pair, then use the *outgoing* COM port shown in Bluetooth settings → More Bluetooth options → COM Ports. |

Close EM31win (and any GPS utility) first — only one program can hold a COM port.
If a link drops the app keeps retrying every 2 s and the readout turns red.

### Positioning settings
* **GPS latency** – time between a fix's epoch and its arrival on the COM port
  (typically 0.05–0.3 s). Tune by walking one line in both directions: if
  anomalies shift between directions, adjust until they line up.
* **Layback / cross-track** – EM31 boom centre relative to the GPS antenna, metres.
  Positive layback = sensor behind the antenna. With a backpack antenna over the
  boom centre leave both at 0.
* **Minimum fix quality** – readings bracketed by worse fixes are logged unpositioned.

## Planned lines and aerial imagery

**Planned lines** — More → *Load planned lines / boundary*. Select one or several
files at once (e.g. transects + boundary). Supported:

| Format | What's used |
|---|---|
| GeoPackage `.gpkg` | every feature layer; a layer picker opens (boundary + transects are pre-ticked; a duplicate serpentine track or endpoint layer is left off) |
| GeoJSON `.geojson/.json` | LineString/MultiLineString (lines), Polygon/MultiPolygon (boundaries), Points |
| CSV | `transect_id/line,end,latitude,longitude` or `…,easting,northing,utm_zone`; rows with the same id become one line, ordered by `end` = start/end |
| GPX, KML | tracks/routes/LineStrings, Polygons, waypoints |

Coordinates may be WGS84 lat/lon (EPSG:4326 / CRS84), WGS84 UTM (EPSG:326xx/327xx),
NAD83 or ETRS89 UTM, or Web Mercator — read from the GeoJSON `crs` member or the
GeoPackage SRS. (NAD83/ETRS89 are treated as WGS84, ≈1 m.) Point layers with a shared
line id (e.g. `transect_endpoints` with `transect_id` + `end`) are joined into lines.
Boundaries are drawn in orange and are never used for guidance. A survey summary
`.json` (no geometry) is recognised and explained rather than failing silently.

The banner above the map gives live guidance for the active line:

| Banner | Meaning |
|---|---|
| `ON LINE  Line L07  42 m to go` (green) | within the on-line tolerance (Settings → Layers, default 0.5 m) |
| `◀ 0.84 m` (amber/red) | you are right of the line — steer left (arrows follow your direction of travel) |
| `END OF LINE L07 — tap New line` (blue) | less than 1.5 m left |

A single long route (e.g. a serpentine track) gets a **turn countdown** ("42 m to
turn", then "TURN") instead of the whole remaining length.

**New line** advances to the next planned line (More → *Previous planned line* goes
back); tapping near a line on the map selects it. Settings → Layers can switch to
*automatic: nearest line*. The CSV's `line` column records the planned line name.
Guidance uses the sensor position (layback/cross-track applied) and works in
either walking direction.

**Imagery, option 1 — download before you go (offline tiles).** More → *Download
imagery for offline use*: choose the planned-lines extent (+ margin), the current
view, or a lat/lon + radius, and a zoom (18 ≈ 0.5 m/pixel). The dialog shows the tile
count and size first. Tiles are cached in `Documents\EM31Mapper\imagery_cache` and
displayed automatically whenever you're over the area — no connection needed in the field.
Default source is **USGS Imagery** (NAIP-based, public domain, US coverage; the
service permits bulk tile export). Settings → Layers can point at a custom
`{z}/{x}/{y}` URL, e.g. an institutional imagery server — many commercial basemaps
(Google, Bing, Esri World Imagery) don't allow bulk offline caching.

**Imagery, option 2 — your own file.** More → *Load imagery file*: GeoTIFF (UTM
WGS84/NAD83, WGS84 lat/lon, or Web Mercator; 4-band NAIP shown as RGB; large files
use their overviews or are downsampled to ~6000 px), or a JPG/PNG/TIFF with a world
file (`.jgw/.pgw/.tfw/.wld`; projection from the `.prj`, or you're asked once).
NAIP orthophotos from USGS EarthExplorer or your own drone orthomosaics work well.

The plan file and imagery file reload automatically next time the app starts.
Imagery and grid opacity are in Settings → Layers / Map.

## Dipole modes (VMD / HMD) and the difference map

The EM31's dipole mode is set physically: normal carry is **VMD** (vertical
dipoles, effective depth ~6 m); rotate the instrument 90° about the boom axis for
**HMD** (horizontal dipoles, ~3 m). Every EM31 record reports the mode it was taken
in, so the app never relies on you remembering:

* Settings → Instrument → *Survey dipole mode* says which mode you intend. If the
  instrument reports the other one, the conductivity readout turns red:
  `⚠ HMD — survey set to VMD`.
* Each CSV row has `dipole` (VMD/HMD) and `depth_m` (nominal depth, editable in
  Settings — use ~3 m / 1.5 m for the 2 m EM31-SH).
* VMD and HMD readings are kept as separate map layers. More → *Map dipole mode*
  (or Settings → Map) shows: follow the instrument, VMD only, HMD only, or
  **Difference VMD − HMD**.

**Difference map.** The two modes are never measured at exactly the same spots, so
both are gridded onto one common grid and subtracted cell by cell (only where both
have coverage). It uses a diverging colour scale centred on zero:
red = VMD > HMD (conductivity increasing with depth, e.g. a conductive layer or
water table below ~3 m), blue = HMD > VMD (conductive near-surface over more
resistive ground). Typical workflow: walk the planned lines in VMD, rotate the
instrument and change the setting to HMD, then walk them again; or alternate modes
line by line. Export grid saves whichever view is showing (`grid_cond_VMD-HMD_….asc`).
Keep the instrument height the same for both passes, since HMD is more sensitive to
carrying height. Values are apparent conductivities, so the difference is a
qualitative layering indicator rather than an inversion.

## Moving around the map

| Touch | Mouse / stylus |
|---|---|
| one-finger drag = pan | drag = pan |
| two-finger pinch = zoom (about your fingers) | wheel = zoom |
| tap near a line = select it | click near a line = select it |
| **+ / −** buttons (top-left of the map) | same |

The scale bar (bottom-left) updates as you zoom. With **Follow** on, + / − zoom
around your position and Follow keeps the zoom you chose; panning by hand turns
Follow off — tap Follow to re-centre. **Fit** shows all data (or the plan).

## Buttons
**Connect** opens ports (live readouts, no logging) · **Record** starts a new survey file
· **Mark** writes a numbered marker at the current position · **New line** increments the
line number · **Fit / Follow** control the map view · **More** → raw stream monitor,
load an earlier CSV onto the map, export grid/PNG, clear map.

The map grid is inverse-distance weighted (cell size and search radius in
Settings → Map) and recomputed every few seconds in the background; large surveys
coarsen automatically to keep the Mesa2 responsive. Final grids for interpretation
are better made afterwards from the CSV.

## Output files (Documents\EM31Mapper by default)
| File | Contents |
|---|---|
| `EM31_YYYYMMDD_HHMMSS.csv` | One row per reading: time, line, conductivity (mS/m), in-phase (ppt), range, dipole (VMD/HMD), nominal depth, marker, raw counts, UTM E/N of the **sensor** (offsets applied), antenna lat/lon, altitude, fix quality, sats, HDOP, heading, GPS gap. Comment rows hold MARK / LINE events. Flushed to disk every 2 s. |
| `…_em31_raw.bin` | Exact bytes from the EM31 port (reprocess or debug later) |
| `…_gps.nmea` | Every NMEA sentence with PC timestamp |
| `grid_*.asc` + `.prj` | Exported ESRI ASCII grid in WGS84 / UTM — opens directly in QGIS, ArcGIS, Surfer |

## EM31 data format notes
Decoding follows the Geonics EM31win manual (Appendix A): each record is an info
byte (marker, dipole, range bits) followed by two signed 4-digit readings, scaled by
range (conductivity −0.25/−0.025/−0.0025 for ranges 1000/100/10; in-phase −0.025 in
OPER mode). EM31-SH short boom divides in-phase by 3.35 (Settings → Instrument).

**First time on the real instrument:** open More → *Raw stream monitor* with the
EM31 on. You should see records like `A4 2D 32 35 35 39 …  ¤-2559-…` scrolling at
~11 Hz, and the readout should match the console display. If bytes arrive but no
readings decode, save a few seconds of `_em31_raw.bin` — the parser lives in
`em31mapper/em31.py` and is easy to adjust.

## Code layout
`em31.py` instrument decoding · `nmea.py` GPS · `fusion.py` time-based positioning ·
`geo.py` UTM + tile maths · `plan.py` planned lines + guidance ·
`imagery.py` GeoTIFF/world files + tile cache · `gridding.py` NumPy IDW + ASCII grid · `mapnav.py` touch pan/pinch/zoom · `logger.py` files · `sources.py`
serial threads + simulator · `app.py` GUI. Tests: `python -m pytest tests`.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Aesir Consulting LLC.
