"""Render the app icons from one SVG with headless Chrome:
    python mobile/icons/make_icons.py [path-to-chrome]
A brass escutcheon with a keyhole, on the GUI's ink colour."""
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).parent
CHROME = sys.argv[1] if len(sys.argv) > 1 else "google-chrome"
INK, BRASS, BRASS_HI = "#1D2A38", "#A88350", "#C9A46A"


def svg(shape: str, scale: float = 1.0) -> str:
    bg = {"square": f'<rect width="100" height="100" fill="{INK}"/>',
          "round": f'<circle cx="50" cy="50" r="50" fill="{INK}"/>',
          "full": f'<rect width="100" height="100" fill="{INK}"/>'}[shape]
    plate = (f'<g transform="translate(50 50) scale({scale}) translate(-50 -50)">'
             f'<circle cx="50" cy="50" r="31" fill="{BRASS}"/>'
             f'<circle cx="50" cy="50" r="31" fill="none" stroke="{BRASS_HI}" stroke-width="2.2"/>'
             f'<circle cx="50" cy="44" r="8.5" fill="{INK}"/>'
             f'<path d="M45.2 47 L54.8 47 L58 67 L42 67 Z" fill="{INK}"/></g>')
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">{bg}{plate}</svg>'


def render(markup: str, size: int, out: pathlib.Path) -> None:
    with tempfile.TemporaryDirectory() as d:
        page = pathlib.Path(d) / "i.html"
        page.write_text(f'<html><body style="margin:0;background:transparent">'
                        f'<div style="width:{size}px;height:{size}px">{markup.replace("<svg ", f"<svg width={size} height={size} ")}</div></body></html>')
        subprocess.run([CHROME, "--headless", "--disable-gpu", "--hide-scrollbars",
                        f"--user-data-dir={d}/p", "--default-background-color=00000000",
                        f"--window-size={size},{size}", f"--screenshot={out}", page.as_uri()],
                       check=True, capture_output=True)


jobs = []
for n in (48, 72, 96, 144, 192):
    jobs += [(svg("round"), n, f"locklyctl-round-{n}.png"), (svg("square"), n, f"locklyctl-square-{n}.png")]
for n in (320, 480, 640, 960, 1280):                       # Android splash
    jobs.append((svg("square", 0.45), n, f"locklyctl-square-{n}.png"))
for n in (108, 162, 216, 324, 432):                        # adaptive: keep to the safe zone
    jobs.append((svg("full", 0.62), n, f"locklyctl-adaptive-{n}.png"))
for n in (20, 29, 40, 58, 60, 76, 80, 87, 120, 152, 167, 180, 1024):
    jobs.append((svg("full"), n, f"locklyctl-{n}.png"))
for n in (640, 1280, 1920):                                # iOS splash
    jobs.append((svg("full", 0.35), n, f"locklyctl-{n}.png"))
for markup, n, name in jobs:
    render(markup, n, HERE / name)
(HERE / "locklyctl.svg").write_text(svg("square") + "\n")
print(f"{len(jobs)} icons written to {HERE}")
