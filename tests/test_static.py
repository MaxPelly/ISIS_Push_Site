import json

from push_site import __version__
from push_site.prefs import BASE_TOPICS
from tests.helpers import payload, signed


async def test_manifest_and_icons(site):
    resp = await site.get("/manifest.webmanifest")
    assert resp.status == 200 and resp.content_type == "application/manifest+json"
    manifest = json.loads(await resp.text())
    assert manifest["display"] == "standalone" and manifest["start_url"] == "/"
    for icon in manifest["icons"]:
        resp = await site.get(icon["src"])
        assert resp.status == 200 and resp.content_type == "image/png"
        assert "default-src 'self'" in resp.headers["Content-Security-Policy"]
        assert "camera=()" in resp.headers["Permissions-Policy"]


async def test_static_files_cannot_escape_their_directory(site):
    for path in ("/static/../app.py", "/static/%2e%2e/app.py", "/static/..%2fapp.py"):
        assert (await site.get(path)).status in (403, 404), path


async def test_service_worker_is_served_from_the_root_uncached(site):
    resp = await site.get("/sw.js")
    assert resp.status == 200 and resp.content_type == "text/javascript"
    assert resp.headers["Cache-Control"] == "no-cache"
    assert "showNotification" in await resp.text()


async def test_page_and_its_assets(site):
    resp = await site.get("/")
    assert resp.status == 200 and resp.content_type == "text/html"
    html = await resp.text()
    assert "<script>" not in html and "style=" not in html  # the CSP allows neither inline
    for path, kind in (("/static/app.js", "application/javascript"), ("/static/app.css", "text/css")):
        resp = await site.get(path)
        assert resp.status == 200 and kind in resp.content_type.replace("text/javascript", "application/javascript")
        assert resp.headers["Cache-Control"] == "no-cache"


async def test_page_shows_the_site_version(site):
    html = await (await site.get("/")).text()
    assert f">ISIS Push Site v{__version__}<" in html and "{{" not in html


async def test_page_shows_the_monitor_version_from_the_newest_notification(site):
    await site.post("/ingest", **signed(payload(id="1", monitor_version="1.0.0")))
    await site.post("/ingest", **signed(payload(id="2", monitor_version="1.1.0")))
    html = await (await site.get("/")).text()
    assert f">ISIS Push Site v{__version__} · ISIS Beam Monitor v1.1.0<" in html


async def test_feed_has_a_tab_and_a_css_filter_for_every_fixed_topic(site):
    html = await (await site.get("/")).text()
    css = await (await site.get("/static/app.css")).text()
    for tab in (*BASE_TOPICS, "runs"):
        assert f'id="tab-{tab}"' in html, tab
        assert f'li:not([data-tab="{tab}"])' in css and f':not(:has([data-tab="{tab}"]))::after' in css, tab


async def test_page_has_a_status_section_that_polls_the_status(site):
    html = await (await site.get("/")).text()
    js = await (await site.get("/static/app.js")).text()
    assert 'id="status"' in html and html.index('id="status"') < html.index('id="prefs"')  # at the top
    assert '"/api/status"' in js and "createElementNS" in js  # the sparkline is built, not inline markup
