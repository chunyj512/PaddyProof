#!/usr/bin/env python3
"""Record the console walkthrough and turn it into docs/demo.gif and docs/demo.mp4.

    python web/app.py                       # in one terminal (any port)
    python tools/record_demo.py [base_url]  # default http://127.0.0.1:8000

Needs: pip install playwright imageio-ffmpeg && playwright install chromium
The script resets the demo state before and after, so it can be re-run.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
W, H = 1280, 800

CURSOR = """
(() => {
  const c = document.createElement('div');
  c.style.cssText = 'position:fixed;z-index:99999;width:18px;height:18px;border-radius:50%;' +
    'background:rgba(20,99,58,.35);border:2px solid #14633a;pointer-events:none;left:-40px;top:-40px;' +
    'transform:translate(-50%,-50%);transition:width .12s,height .12s';
  const add = () => document.body && document.body.appendChild(c);
  document.readyState === 'loading' ? document.addEventListener('DOMContentLoaded', add) : add();
  addEventListener('mousemove', e => { c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px'; }, true);
  addEventListener('mousedown', () => { c.style.width = c.style.height = '28px'; }, true);
  addEventListener('mouseup', () => { c.style.width = c.style.height = '18px'; }, true);
})();
"""


def reset() -> None:
    urllib.request.urlopen(urllib.request.Request(BASE + "/api/console/demo/reset", method="POST"), timeout=60)


def click(pg: Page, selector: str, pause: int = 900, **kw) -> None:
    loc = pg.locator(selector).first
    loc.scroll_into_view_if_needed()
    box = loc.bounding_box()
    pg.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=22)
    pg.wait_for_timeout(250)
    loc.click(**kw)
    pg.wait_for_timeout(pause)


def scroll(pg: Page, y: int, pause: int = 900) -> None:
    pg.evaluate(f"window.scrollTo({{top:{y},behavior:'smooth'}})")
    pg.wait_for_timeout(pause)


def walkthrough(pg: Page) -> None:
    pg.goto(BASE + "/#/overview")
    pg.wait_for_selector(".kpis")
    pg.wait_for_timeout(2600)                       # tiles load; this lead-in is trimmed
    pg.mouse.move(640, 300, steps=10)
    pg.wait_for_timeout(1800)
    scroll(pg, 560, 1700)

    # Vietnam project -> a plot that has an anchor -> evidence drawer
    click(pg, "tr[data-go='#/projects/VN-ANGIANG']", 2400)
    click(pg, "#pjSeg button[data-k='review']", 900)
    click(pg, "#pjSeg button[data-k='all']", 500)
    click(pg, "#pjQ", 200)
    pg.keyboard.type("0076", delay=110)
    pg.wait_for_timeout(600)
    click(pg, "#pjTable tr[data-id]", 2600)
    pg.locator("#drawerBody").evaluate("e => e.scrollTo({top: 520, behavior: 'smooth'})")
    pg.wait_for_timeout(2200)
    click(pg, "#drawerBody a[href^='#/anchors/']", 2600)

    # Anchor: tamper with one stored record
    click(pg, "#anTamper", 1200)
    pg.locator("#chNote").scroll_into_view_if_needed()
    pg.wait_for_timeout(3600)

    # Review queue: record a field inspection
    click(pg, ".nav a[data-view='review']", 1800)
    click(pg, "#rvTable button[data-res]", 900)
    pg.select_option("#rvOutcome", "non_compliant")
    click(pg, "#rvNote", 200)
    pg.keyboard.type("Outlet closed, standing water on arrival", delay=38)
    pg.wait_for_timeout(500)
    click(pg, "#rvSave", 2400)

    # Report
    click(pg, ".nav a[data-view='reports']", 2000)
    scroll(pg, 420, 1700)
    scroll(pg, 0, 700)

    # Languages
    click(pg, "#langBtn", 700)
    click(pg, "#langMenu button[data-lang='vi']", 2300)
    click(pg, "#langBtn", 600)
    click(pg, "#langMenu button[data-lang='ko']", 1900)
    click(pg, ".nav a[data-view='overview']", 2800)


def main() -> None:
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    DOCS.mkdir(exist_ok=True)
    reset()
    tmp = Path(tempfile.mkdtemp(prefix="ppdemo"))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": W, "height": H}, color_scheme="light",
                                  record_video_dir=str(tmp), record_video_size={"width": W, "height": H})
        ctx.add_init_script(CURSOR)
        ctx.add_init_script("try{localStorage.setItem('pp.lang','en')}catch(e){}")
        pg = ctx.new_page()
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        walkthrough(pg)
        ctx.close()
        browser.close()
    reset()
    if errors:
        print("page errors:", errors)
    webm = next(tmp.glob("*.webm"))
    trim = ["-ss", "2.6"]
    subprocess.run([ffmpeg, "-y", *trim, "-i", str(webm), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-crf", "23", "-movflags", "+faststart", str(DOCS / "demo.mp4")],
                   check=True, capture_output=True)
    vf = "fps=8,scale=960:-1:flags=lanczos"
    subprocess.run([ffmpeg, "-y", *trim, "-i", str(webm), "-vf", f"{vf},palettegen=max_colors=128:stats_mode=diff",
                    str(tmp / "pal.png")], check=True, capture_output=True)
    subprocess.run([ffmpeg, "-y", *trim, "-i", str(webm), "-i", str(tmp / "pal.png"), "-lavfi",
                    f"{vf}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle",
                    str(DOCS / "demo.gif")], check=True, capture_output=True)
    shutil.rmtree(tmp, ignore_errors=True)
    for f in ("demo.gif", "demo.mp4"):
        print(f"{DOCS / f}  {(DOCS / f).stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
