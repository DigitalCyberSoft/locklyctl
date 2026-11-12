"""Render the app icons and logos from one SVG emblem with headless Chrome:
    python mobile/icons/make_icons.py [path-to-chrome]
The emblem is an arched brass door with signal arcs, on the GUI's ink colour.
It was drawn with OpenAI's image API on 2026-11-13 (gpt-image-2.6-sunburst,
with gpt-6.1-sol writing the SVG); edit it here. Besides Briefcase's sizes in
this folder it writes:
- locklyctl/gui/web/icon.png (window icon and favicon) and logo.svg (the
  emblem beside the wordmark in the app)
- packaging/locklyctl.ico (Windows) and packaging/locklyctl.icns (macOS)
- docs/logo-light.png and docs/logo-dark.png (the README's heading)"""
import base64
import pathlib
import re
import struct
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).parent
ROOT = HERE.parent.parent
CHROME = sys.argv[1] if len(sys.argv) > 1 else "google-chrome"
INK, BRASS, BRASS_HI = "#1D2A38", "#A88350", "#C9A46A"
EMBLEM = (f'<path fill="{BRASS}" d="M362 210C445 122 579 122 662 210A17 17 0 0 1 638 234C569 160 455 160 386 234'
          'A17 17 0 0 1 362 210Z"/>'
          f'<path fill="{BRASS}" d="M420 260C471 214 553 214 604 260A17 17 0 0 1 582 286C542 250 482 250 442 286'
          'A17 17 0 0 1 420 260Z"/>'
          f'<path fill="{BRASS_HI}" d="M298 520C298 402 394 306 512 306S726 402 726 520V866Q726 880 712 880H312'
          'Q298 880 298 866Z"/>'
          f'<path fill="{BRASS}" d="M340 838V520C340 425 417 348 512 348S684 425 684 520V838Z"/>'
          f'<path fill="{INK}" d="M598 628A33 33 0 1 1 632 628L644 680Q646 686 639 686H591Q584 686 586 680Z"/>')
EMBLEM_BOX = "290 136 444 752"          # the emblem's own bounds (298 144 428 736) plus a little room
WORDMARK = "font-weight:800;font-stretch:125%;letter-spacing:-0.01em"   # the GUI's .brand


def svg(shape: str, scale: float = 1.0) -> str:
    bg = {"square": f'<rect width="1024" height="1024" fill="{INK}"/>',
          "round": f'<circle cx="512" cy="512" r="512" fill="{INK}"/>',
          "full": f'<rect width="1024" height="1024" fill="{INK}"/>',
          "mac": f'<rect x="100" y="100" width="824" height="824" rx="185" fill="{INK}"/>'}[shape]
    emblem = f'<g transform="translate(512 512) scale({scale}) translate(-512 -512)">{EMBLEM}</g>'
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">{bg}{emblem}</svg>'


def render(markup: str, size: int, out: pathlib.Path) -> None:
    with tempfile.TemporaryDirectory() as d:
        page = pathlib.Path(d) / "i.html"
        page.write_text(f'<html><body style="margin:0;background:transparent">'
                        f'<div style="width:{size}px;height:{size}px">{markup.replace("<svg ", f"<svg width={size} height={size} ")}</div></body></html>')
        subprocess.run([CHROME, "--headless", "--disable-gpu", "--hide-scrollbars",
                        f"--user-data-dir={d}/p", "--default-background-color=00000000",
                        f"--window-size={size},{size}", f"--screenshot={out}", page.as_uri()],
                       check=True, capture_output=True)


def render_fitted(markup: str, out: pathlib.Path, scale: int = 2) -> None:
    """Screenshot ``markup`` cropped to its own size once the GUI's font has
    loaded: one pass measures it, the next captures it at ``scale``."""
    font = base64.b64encode((ROOT / "locklyctl/gui/web/fonts/Archivo-Variable-latin.woff2").read_bytes()).decode()
    style = (f'@font-face{{font-family:Archivo;src:url(data:font/woff2;base64,{font}) format("woff2");'
             'font-weight:100 900;font-stretch:62% 125%}'
             'body{margin:0;background:transparent}#m{display:inline-flex;align-items:center;padding:6px}'
             '#size{position:absolute;left:-9999px}')
    measure = ('<script>document.fonts.ready.then(() => { const r = document.getElementById("m")'
               '.getBoundingClientRect(); document.getElementById("size").textContent ='
               ' Math.ceil(r.width) + "," + Math.ceil(r.height); });</script>')
    with tempfile.TemporaryDirectory() as d:
        page = pathlib.Path(d) / "l.html"
        page.write_text(f'<html><head><style>{style}</style></head><body><div id="m">{markup}</div>'
                        f'<pre id="size"></pre>{measure}</body></html>')
        base = [CHROME, "--headless", "--disable-gpu", "--hide-scrollbars", f"--user-data-dir={d}/p",
                "--virtual-time-budget=3000"]
        dom = subprocess.run(base + ["--dump-dom", page.as_uri()], check=True, capture_output=True, text=True).stdout
        w, h = re.search(r'<pre id="size">(\d+),(\d+)</pre>', dom).groups()
        subprocess.run(base + ["--default-background-color=00000000", f"--force-device-scale-factor={scale}",
                               f"--window-size={w},{h}", f"--screenshot={out}", page.as_uri()],
                       check=True, capture_output=True)


def pngs(markup: str, sizes: tuple[int, ...]) -> dict[int, bytes]:
    with tempfile.TemporaryDirectory() as d:
        for n in sizes:
            render(markup, n, pathlib.Path(d) / f"{n}.png")
        return {n: (pathlib.Path(d) / f"{n}.png").read_bytes() for n in sizes}


def ico(images: dict[int, bytes]) -> bytes:
    """PNG entries, laid out as Pillow's IcoImagePlugin writes them."""
    out, offset = struct.pack("<HHH", 0, 1, len(images)), 6 + 16 * len(images)
    for n, data in images.items():
        out += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return out + b"".join(images.values())


ICNS_TYPES = {b"ic07": 128, b"ic08": 256, b"ic09": 512, b"ic10": 1024,
              b"ic11": 32, b"ic12": 64, b"ic13": 256, b"ic14": 512}


def icns(images: dict[int, bytes]) -> bytes:
    """PNG entries and a table of contents, laid out as Pillow's IcnsImagePlugin writes them."""
    entries = [(kind, images[n]) for kind, n in ICNS_TYPES.items()]
    toc = b"TOC " + struct.pack(">I", 8 + 8 * len(entries)) + b"".join(
        kind + struct.pack(">I", 8 + len(data)) for kind, data in entries)
    body = toc + b"".join(kind + struct.pack(">I", 8 + len(data)) + data for kind, data in entries)
    return b"icns" + struct.pack(">I", 8 + len(body)) + body


jobs = []
for n in (48, 72, 96, 144, 192):
    jobs += [(svg("round"), n, f"locklyctl-round-{n}.png"), (svg("square"), n, f"locklyctl-square-{n}.png")]
for n in (320, 480, 640, 960, 1280):                       # Android splash
    jobs.append((svg("square", 0.39), n, f"locklyctl-square-{n}.png"))
for n in (108, 162, 216, 324, 432):                        # adaptive: keep to the safe zone
    jobs.append((svg("full", 0.67), n, f"locklyctl-adaptive-{n}.png"))
for n in (20, 29, 40, 58, 60, 76, 80, 87, 120, 152, 167, 180, 1024):
    jobs.append((svg("full"), n, f"locklyctl-{n}.png"))
for n in (640, 1280, 1920):                                # iOS splash
    jobs.append((svg("full", 0.30), n, f"locklyctl-{n}.png"))
for markup, n, name in jobs:
    render(markup, n, HERE / name)
(HERE / "locklyctl.svg").write_text(svg("square") + "\n")
web = ROOT / "locklyctl" / "gui" / "web"
render(svg("round"), 256, web / "icon.png")
(web / "logo.svg").write_text(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{EMBLEM_BOX}">{EMBLEM}</svg>\n')
(ROOT / "packaging" / "locklyctl.ico").write_bytes(ico(pngs(svg("round"), (16, 24, 32, 48, 64, 128, 256))))
(ROOT / "packaging" / "locklyctl.icns").write_bytes(icns(pngs(svg("mac", 824 / 1024), (32, 64, 128, 256, 512, 1024))))
for theme, ink in (("light", INK), ("dark", "#E2E6E0")):    # the GUI's ink in each theme
    render_fitted(f'<svg viewBox="{EMBLEM_BOX}" height="68" style="margin-right:16px">{EMBLEM}</svg>'
                  f'<span style="font-family:Archivo;font-size:52px;line-height:1;{WORDMARK};color:{ink}">locklyctl</span>',
                  ROOT / "docs" / f"logo-{theme}.png")
print(f"{len(jobs)} icons written to {HERE}, plus the desktop icons and the logos")
