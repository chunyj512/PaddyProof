"""Screenshot every console view (used for manual checks). Usage: shots.py OUTDIR [lang] [scheme]"""
import sys, pathlib
from playwright.sync_api import sync_playwright
out = pathlib.Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
lang = sys.argv[2] if len(sys.argv) > 2 else "en"
scheme = sys.argv[3] if len(sys.argv) > 3 else "light"
BASE = "http://127.0.0.1:8765"
with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1440, "height": 900}, color_scheme=scheme)
    ctx.add_init_script(f"try{{localStorage.setItem('pp.lang','{lang}')}}catch(e){{}}")
    pg = ctx.new_page()
    errs = []
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errs.append(str(e)))
    for name, h in [("overview", "#/overview"), ("project_vn", "#/projects/VN-ANGIANG"),
                    ("project_kr", "#/projects/KR-IKSAN"), ("anchors", "#/anchors/PP-VN-0002"),
                    ("review", "#/review"), ("reports", "#/reports/VN-ANGIANG"), ("analysis", "#/analysis")]:
        pg.goto(BASE + "/" + h); pg.reload(); pg.wait_for_timeout(2500)
        pg.screenshot(path=str(out / f"{name}.png"), full_page=True)
    pg.goto(BASE + "/#/projects/KR-IKSAN"); pg.reload(); pg.wait_for_timeout(2000)
    pg.click("#pjTable tr[data-id]"); pg.wait_for_timeout(1200)
    pg.screenshot(path=str(out / "drawer_kr.png"))
    print("console errors:", errs)
    b.close()
