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


# --------------------------------------------------------------------------
# Infrastructure
# --------------------------------------------------------------------------

INFRA = REPO / "infra"


def terraform_sources() -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8") for path in INFRA.glob("*.tf")}


def variable_block(name: str) -> str:
    """The text of one ``variable "name" { ... }`` block in variables.tf."""
    source = read("infra/variables.tf")
    match = re.search(rf'^variable "{name}" \{{\n(.*?)^\}}', source, re.S | re.M)
    assert match, f"variables.tf declares no variable {name!r}"
    return match.group(1)


def variable_default(name: str) -> str:
    match = re.search(r"^  default\s*=\s*(.+)$", variable_block(name), re.M)
    assert match, f"variable {name!r} has no default"
    return match.group(1).strip().strip('"')


def test_scheduled_cadence_matches_the_cadence_the_code_assumes():
    """One cadence, in two files that cannot import each other.

    Terraform starts the job on this cadence and tells it so through an
    environment variable, so in production there is one number. This pins
    the two *defaults* together, so that a local run and the tests reason
    about the same schedule production actually keeps.
    """
    from orbital_watch.config import CADENCE_ENV, DEFAULT_REFRESH_CADENCE_MINUTES

    assert int(variable_default("refresh_cadence_minutes")) == (
        DEFAULT_REFRESH_CADENCE_MINUTES
    )
    job = read("infra/job.tf")
    assert f'name  = "{CADENCE_ENV}"' in job
    assert "value = tostring(var.refresh_cadence_minutes)" in job
    assert 'schedule  = "*/${var.refresh_cadence_minutes} * * * *"' in read(
        "infra/scheduler.tf"
    )


def test_job_is_configured_through_the_variables_the_code_reads():
    from orbital_watch.config import PUBLIC_BUCKET_ENV, STATE_BUCKET_ENV

    job = read("infra/job.tf")
    assert f'name  = "{STATE_BUCKET_ENV}"' in job
    assert f'name  = "{PUBLIC_BUCKET_ENV}"' in job


def test_run_deadline_fits_inside_the_task_timeout():
    """The job stops itself before Cloud Run has to."""
    from orbital_watch.config import REFRESH_CADENCE, RUN_DEADLINE_SECONDS

    (timeout,) = re.findall(r'^\s*timeout = "(\d+)s"$', read("infra/job.tf"), re.M)

    assert RUN_DEADLINE_SECONDS < int(timeout) < REFRESH_CADENCE.total_seconds()


def test_monitoring_and_page_look_for_the_document_the_job_writes():
    from orbital_watch.config import DOCUMENT_KEY

    monitoring = read("infra/monitoring.tf")
    assert f'content = "\\"{DOCUMENT_KIND}\\""' in monitoring
    assert f"/{DOCUMENT_KEY}" in monitoring
    # The site check recognises the page by its title.
    (matcher,) = re.findall(r'content = "(<title>[^"]+)"', monitoring)
    assert matcher in read("web/index.html")


def test_hosting_site_is_the_same_in_terraform_and_the_firebase_config():
    targets = json.loads(read(".firebaserc"))
    (project,) = targets["targets"]
    (sites,) = targets["targets"][project]["hosting"].values()

    assert project == variable_default("project_id") == targets["projects"]["default"]
    assert sites == [variable_default("hosting_site_id")]


def test_no_service_account_key_is_created_anywhere():
    """Every credential here is a token minted per use. No key exists to leak."""
    for name, source in terraform_sources().items():
        assert "google_service_account_key" not in source, name
        assert "private_key" not in source, name
        assert not re.search(r"^\s*credentials\s*=", source, re.M), name


def test_the_alert_address_has_no_default():
    """A default would put an address in the repository. It must be supplied."""
    block = variable_block("alert_email")

    assert not re.search(r"^\s*default\s*=", block, re.M)
    assert re.search(r"^\s*sensitive\s*=\s*true$", block, re.M)


def test_no_address_or_account_id_is_written_into_the_infrastructure():
    """Nothing that identifies a person or a billing account is committed."""
    committed = "\n".join(terraform_sources().values()) + read(
        "infra/tests/invariants.tftest.hcl"
    )
    addresses = set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", committed))

    # Service account addresses built from the project ID, and the placeholder
    # the mocked tests use, are the only ones there should be.
    assert all(
        address.endswith(".iam.gserviceaccount.com") or address.endswith("@example.com")
        for address in addresses
    ), addresses
    assert not re.search(r"\b[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}\b", committed)


def test_apis_are_never_disabled_on_destroy():
    """The project is shared; turning an API off could break the portfolio."""
    assert "disable_on_destroy = false" in read("infra/services.tf")


def test_terraform_ignores_only_the_image_the_deploy_owns():
    job = read("infra/job.tf")
    (ignored,) = re.findall(r"ignore_changes = \[\n(.*?)\n    \]", job, re.S)

    assert {item.strip().rstrip(",") for item in ignored.splitlines()} == {
        "client",
        "client_version",
        "template[0].template[0].containers[0].image",
    }


# --------------------------------------------------------------------------
# Automation
# --------------------------------------------------------------------------

WORKFLOWS = sorted((REPO / ".github" / "workflows").glob("*.yml"))


def test_only_the_two_expected_workflows_exist():
    """No scheduled workflow: the schedule lives in Cloud Scheduler, once.

    A third file here -- in particular anything with a cron trigger -- would
    be a second scheduler, which is how the first design broke.
    """
    assert [path.name for path in WORKFLOWS] == ["ci.yml", "deploy.yml"]
    for path in WORKFLOWS:
        assert "schedule:" not in path.read_text(), f"{path.name} has a cron trigger"


def test_every_action_is_pinned_to_a_commit():
    """A tag can be re-pointed at other code; a commit SHA cannot."""
    for path in WORKFLOWS:
        uses = re.findall(r"^\s*-?\s*uses:\s*(\S+)(.*)$", path.read_text(), re.M)
        assert uses, f"{path.name} uses no actions?"
        for reference, trailing in uses:
            assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", reference), (
                f"{path.name}: {reference} is not pinned to a full commit SHA"
            )
            assert re.search(r"#\s*v\d+\.\d+\.\d+", trailing), (
                f"{path.name}: {reference} should say which release the SHA is"
            )


def test_workflows_use_no_secrets():
    """Keyless by construction: there is nothing to store and nothing to leak."""
    for path in WORKFLOWS:
        assert not re.search(r"secrets\.\w|secrets\[", path.read_text()), path.name


def test_only_the_deploy_job_may_request_an_identity_token():
    import yaml

    for path in WORKFLOWS:
        workflow = yaml.safe_load(path.read_text())
        assert workflow["permissions"] == {"contents": "read"}, path.name
        for name, job_definition in workflow["jobs"].items():
            wants_token = "id-token" in job_definition.get("permissions", {})
            assert wants_token == (path.name == "deploy.yml" and name == "production"), (
                f"{path.name}:{name}"
            )


def test_ci_and_the_image_build_use_the_same_uv():
    (image_version,) = re.findall(r"astral-sh/uv:([\d.]+)@sha256", read("Dockerfile"))

    for path in WORKFLOWS:
        (workflow_version,) = re.findall(r'UV_VERSION: "([\d.]+)"', path.read_text())
        assert workflow_version == image_version, path.name


def test_container_base_images_are_pinned_by_digest():
    images = re.findall(r"^ARG \w+_IMAGE=(\S+)$", read("Dockerfile"), re.M)

    assert len(images) == 2
    for image in images:
        assert re.search(r":[\w.-]+@sha256:[0-9a-f]{64}$", image), image
    # And nothing is pulled by a bare name further down.
    for line in re.findall(r"^FROM (\S+)", read("Dockerfile"), re.M):
        assert line.startswith("${"), f"FROM {line} bypasses the pinned images"
