"""Source-map rewriting and tenant-scoped paths (SPEC-07 §6).

AC-9's "the CDN URL returns 200" and AC-10's live cross-tenant fetch belong to
the coordinator's E2E run (object storage + edge). These tests prove the parts
that must hold before upload: deterministic URLs under the tenant prefix,
correct rewriting, and refusal of traversal.
"""

from __future__ import annotations

import pytest

from buildplane.detection import rewrite_source_maps, tenant_map_prefix

ORG_A = "11111111-1111-4111-8111-111111111111"
ORG_B = "22222222-2222-4222-8222-222222222222"
PROJECT = "33333333-3333-4333-8333-333333333333"
DEPLOYMENT = "44444444-4444-4444-8444-444444444444"

BASE = "https://cdn.example.com"


def _prefix(org: str = ORG_A) -> str:
    return tenant_map_prefix(BASE, org, PROJECT, DEPLOYMENT)


def test_js_comment_is_rewritten_to_the_tenant_prefix():
    files = {
        "assets/app.js": b"console.log(1)\n//# sourceMappingURL=app.js.map\n",
        "assets/app.js.map": b'{"version":3}',
    }
    result = rewrite_source_maps(files, _prefix())

    expected = f"{_prefix()}/assets/app.js.map"
    assert expected.encode() in result.files["assets/app.js"]
    assert len(result.rewritten) == 1
    assert result.rewritten[0].original_url == "app.js.map"
    assert result.rewritten[0].rewritten_url == expected
    assert result.files["assets/app.js.map"] == b'{"version":3}'


def test_absolute_and_data_urls_are_left_alone():
    files = {
        "app.js": (
            b"//# sourceMappingURL=https://other.example/app.map\n"
            b"//# sourceMappingURL=data:application/json;base64,e30=\n"
        ),
        "app.js.map": b"{}",
    }
    result = rewrite_source_maps(files, _prefix())
    assert result.files["app.js"] == files["app.js"]
    assert result.rewritten == ()


def test_missing_map_is_left_alone_with_a_warning():
    files = {"app.js": b"//# sourceMappingURL=missing.js.map\n"}
    result = rewrite_source_maps(files, _prefix())
    assert result.files["app.js"] == files["app.js"]
    assert result.rewritten == ()
    assert any("missing.js.map" in warning for warning in result.warnings)


def test_css_comment_keeps_its_closing_token():
    files = {
        "styles/site.css": b"body{}\n/*# sourceMappingURL=site.css.map */\n",
        "styles/site.css.map": b"{}",
    }
    result = rewrite_source_maps(files, _prefix())
    rewritten = result.files["styles/site.css"]
    assert f"{_prefix()}/styles/site.css.map */".encode() in rewritten


def test_nested_reference_resolves_relative_to_the_file():
    files = {
        "js/chunks/app.js": b"//# sourceMappingURL=app.js.map",
        "js/chunks/app.js.map": b"{}",
    }
    result = rewrite_source_maps(files, _prefix())
    assert result.rewritten[0].rewritten_url == f"{_prefix()}/js/chunks/app.js.map"


def test_root_relative_reference_resolves_from_output_root():
    files = {
        "js/app.js": b"//# sourceMappingURL=/maps/app.js.map",
        "maps/app.js.map": b"{}",
    }
    result = rewrite_source_maps(files, _prefix())
    assert result.rewritten[0].rewritten_url == f"{_prefix()}/maps/app.js.map"


def test_percent_encoded_filename_is_decoded_then_reencoded():
    files = {
        "app.js": b"//# sourceMappingURL=my%20map.js.map",
        "my map.js.map": b"{}",
    }
    result = rewrite_source_maps(files, _prefix())
    assert result.rewritten[0].rewritten_url == f"{_prefix()}/my%20map.js.map"


def test_traversal_is_refused():
    files = {"app.js": b"//# sourceMappingURL=../../etc/passwd"}
    result = rewrite_source_maps(files, _prefix())
    assert result.files["app.js"] == files["app.js"]
    assert result.rewritten == ()


def test_tenants_get_disjoint_prefixes():
    a = _prefix(ORG_A)
    b = _prefix(ORG_B)
    assert a != b
    assert not a.startswith(b)
    assert ORG_A in a and ORG_B in b
    assert f"/artifacts/{ORG_A}/" in a


def test_non_uuid_ids_are_rejected():
    with pytest.raises(ValueError):
        tenant_map_prefix(BASE, "../../etc", PROJECT, DEPLOYMENT)
    with pytest.raises(ValueError):
        tenant_map_prefix("not-a-url", ORG_A, PROJECT, DEPLOYMENT)


def test_prefix_is_reproducible():
    assert _prefix() == _prefix()


def test_rewrite_is_deterministic():
    files = {
        "b.js": b"//# sourceMappingURL=b.js.map",
        "b.js.map": b"{}",
        "a.js": b"//# sourceMappingURL=a.js.map",
        "a.js.map": b"{}",
    }
    first = rewrite_source_maps(files, _prefix())
    second = rewrite_source_maps(dict(reversed(list(files.items()))), _prefix())
    assert first.files == second.files
    assert first.rewritten == second.rewritten
