"""Render the app icons from one SVG with headless Chrome:
    python mobile/icons/make_icons.py [path-to-chrome]
A brass escutcheon whose keyhole is the dot of a wireless sign, on the GUI's
ink colour. The emblem was drawn with OpenAI's image API on 2026-11-12
(gpt-image-2.6-sunburst, with gpt-6.1-sol writing the SVG); edit it here.
Besides Briefcase's sizes in this folder it writes the desktop icons:
locklyctl/gui/web/icon.png (window icon and favicon), packaging/locklyctl.ico
(Windows) and packaging/locklyctl.icns (macOS)."""
import pathlib
import struct
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).parent
ROOT = HERE.parent.parent
CHROME = sys.argv[1] if len(sys.argv) > 1 else "google-chrome"
INK, BRASS, BRASS_HI = "#1D2A38", "#A88350", "#C9A46A"
EMBLEM = (f'<circle cx="512" cy="512" r="369" fill="{BRASS_HI}"/>'
          f'<circle cx="512" cy="512" r="334" fill="{BRASS}"/>'
          f'<g fill="none" stroke="{INK}" stroke-width="44" stroke-linecap="round">'
          '<path d="M314 355C419 232 605 232 710 355"/><path d="M377 417C448 333 576 333 647 417"/></g>'
          f'<path fill="{INK}" d="M476 560.5A73 73 0 1 1 548 560.5L598 747Q602 759 586 759H438Q422 759 426 747Z"/>')


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
render(svg("round"), 256, ROOT / "locklyctl" / "gui" / "web" / "icon.png")
(ROOT / "packaging" / "locklyctl.ico").write_bytes(ico(pngs(svg("round"), (16, 24, 32, 48, 64, 128, 256))))
(ROOT / "packaging" / "locklyctl.icns").write_bytes(icns(pngs(svg("mac", 824 / 1024), (32, 64, 128, 256, 512, 1024))))
print(f"{len(jobs)} icons written to {HERE}, and the desktop icons")
