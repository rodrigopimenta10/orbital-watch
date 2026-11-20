"""Things that must stay in step across files written in different languages.

The pipeline is Python, the page is JavaScript, the hosting rules are JSON,
the infrastructure is Terraform and the automation is YAML. Each of them
repeats a fact that another one owns -- a version number, a cadence, a
header -- and nothing but a test will notice when two copies drift apart.
The first design was bitten twice by exactly that (DECISIONS.md §18, §19),
so each such pair is pinned here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from orbital_watch.config import BAKED_DOCUMENT_PATH, LOCAL_LIVE_DOCUMENT_PATH
from orbital_watch.document import DOCUMENT_KIND, SCHEMA_VERSION

REPO = Path(__file__).resolve().parent.parent


def read(relative: str) -> str:
    return (REPO / relative).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Pipeline <-> page
# --------------------------------------------------------------------------


def test_page_and_pipeline_agree_on_the_document_they_exchange():
    """A page that expects another schema refuses every document it is sent."""
    core = read("web/assets/core.js")

    assert f"var SCHEMA_VERSION = {SCHEMA_VERSION};" in core
    assert f'var DOCUMENT_KIND = "{DOCUMENT_KIND}";' in core


def test_page_defaults_point_where_a_local_run_puts_the_document():
    for source in (read("web/assets/core.js"), read("web/assets/config.js")):
        assert f'liveDocumentUrl: "{LOCAL_LIVE_DOCUMENT_PATH}"' in source
        assert f'bakedDocumentUrl: "{BAKED_DOCUMENT_PATH}"' in source


def test_page_loads_its_scripts_in_dependency_order():
    html = read("web/index.html")
    scripts = re.findall(r'<script src="([^"]+)"', html)

    assert scripts == [
        "assets/theme.js",
        "assets/config.js",
        "assets/core.js",
        "assets/app.js",
    ]
    # The Content-Security-Policy allows scripts and styles from files only.
    assert "<script>" not in html
    assert "style=" not in html
    assert "onclick=" not in html


# --------------------------------------------------------------------------
# Hosting
# --------------------------------------------------------------------------


def hosting() -> dict:
    return json.loads(read("firebase.json"))["hosting"]


def headers_for(source: str) -> dict[str, str]:
    for rule in hosting()["headers"]:
        if rule["source"] == source:
            return {header["key"]: header["value"] for header in rule["headers"]}
    raise AssertionError(f"firebase.json has no header rule for {source!r}")


def test_content_security_policy_allows_the_site_itself_and_one_other_origin():
    """Everything is same-origin except reading the published document.

    The policy is spelled out directive by directive so that any change which
    introduces an outside dependency -- a font, a script, an analytics call --
    fails here instead of quietly widening what the page may talk to.
    """
    policy = headers_for("**")["Content-Security-Policy"]
    directives = dict(
        part.strip().split(" ", 1) for part in policy.split(";") if part.strip()
    )

    assert directives == {
        "default-src": "'none'",
        "script-src": "'self'",
        "style-src": "'self'",
        "img-src": "'self' data:",
        "connect-src": "'self' https://storage.googleapis.com",
        "font-src": "'self'",
        "base-uri": "'none'",
        "form-action": "'none'",
        "frame-ancestors": "'none'",
    }


def test_hardening_headers_are_set_on_every_response():
    everywhere = headers_for("**")

    assert everywhere["X-Content-Type-Options"] == "nosniff"
    assert everywhere["Referrer-Policy"] == "no-referrer"
    assert everywhere["Cross-Origin-Opener-Policy"] == "same-origin"
    assert "geolocation=()" in everywhere["Permissions-Policy"]


def test_baked_document_and_assets_are_never_cached_for_long():
    """Asset names carry no fingerprint, so they must be revalidated."""
    baked_dir = "/" + BAKED_DOCUMENT_PATH.rsplit("/", 1)[0] + "/**"

    assert (
        headers_for(baked_dir)["Cache-Control"] == "public, max-age=300, must-revalidate"
    )
    assert headers_for("/assets/**")["Cache-Control"] == (
        "public, max-age=3600, must-revalidate"
    )


def test_hosting_serves_the_directory_the_site_is_built_into():
    config = hosting()
    targets = json.loads(read(".firebaserc"))["targets"]

    assert config["public"] == "dist"
    assert "rewrites" not in config, "the site is files only; nothing is proxied"
    (sites,) = (project["hosting"][config["target"]] for project in targets.values())
    assert sites == ["rodrigopimenta-orbital-watch"]
