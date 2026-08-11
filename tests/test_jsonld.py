"""schema.org JSON-LD capture, the fallback for unsupported ATS platforms.

Parsing is exercised against hand-written fixtures rather than saved pages,
so each test states one structural claim about what real pages do. The shapes
here are the ones actually observed in the wild on 2026-08-11: Workday and
Ashby embed a JobPosting server-side; Greenhouse and Lever embed none.
"""

from __future__ import annotations

import pytest

from jsa.ats import jsonld


def page(*blocks: str) -> str:
    scripts = "".join(f'<script type="application/ld+json">{b}</script>' for b in blocks)
    return f"<html><head>{scripts}</head><body>ignored</body></html>"


JOB = """
{"@context":"https://schema.org","@type":"JobPosting",
 "title":"Enablement Lead",
 "description":"<p>Build the <b>thing</b>.</p>",
 "jobLocation":{"@type":"Place","address":{"@type":"PostalAddress",
   "addressLocality":"Santa Clara","addressRegion":"CA","addressCountry":"US"}}}
"""


# --- finding the JobPosting ------------------------------------------------


def test_extracts_a_plain_job_posting():
    got = jsonld.extract_job_posting(page(JOB))
    assert got is not None
    assert got.title == "Enablement Lead"
    assert got.location == "Santa Clara, CA, US"


def test_finds_the_posting_inside_an_at_graph_wrapper():
    # Many CMS-driven career sites wrap everything in @graph.
    wrapped = '{"@context":"https://schema.org","@graph":[' + JOB + "]}"
    assert jsonld.extract_job_posting(page(wrapped)).title == "Enablement Lead"


def test_finds_the_posting_in_a_top_level_array():
    assert jsonld.extract_job_posting(page("[" + JOB + "]")).title == "Enablement Lead"


def test_skips_non_job_blocks_to_find_the_job_one():
    # Sites routinely ship Organization/BreadcrumbList blocks alongside.
    other = '{"@type":"Organization","name":"Acme"}'
    assert jsonld.extract_job_posting(page(other, JOB)).title == "Enablement Lead"


def test_handles_a_type_expressed_as_a_list():
    multi = JOB.replace('"@type":"JobPosting"', '"@type":["JobPosting","Thing"]')
    assert jsonld.extract_job_posting(page(multi)) is not None


def test_a_malformed_block_does_not_prevent_finding_a_later_good_one():
    # One broken script tag must not cost us the whole page.
    assert jsonld.extract_job_posting(page("{not json,,,", JOB)).title == "Enablement Lead"


@pytest.mark.parametrize(
    "html",
    [
        "<html><body>no structured data at all</body></html>",  # Greenhouse, Lever
        "",
        page('{"@type":"Organization","name":"Acme"}'),
        page("{broken"),
    ],
)
def test_returns_none_rather_than_raising(html):
    assert jsonld.extract_job_posting(html) is None


# --- location rendering ----------------------------------------------------


def test_remote_only_postings_render_as_remote():
    remote = '{"@type":"JobPosting","title":"X","jobLocationType":"TELECOMMUTE"}'
    assert jsonld.extract_job_posting(page(remote)).location == "Remote"


def test_multiple_locations_are_joined_and_deduplicated():
    multi = """
    {"@type":"JobPosting","title":"X","jobLocation":[
      {"address":{"addressLocality":"Austin"}},
      {"address":{"addressLocality":"Austin"}},
      {"address":{"addressLocality":"Denver"}}]}
    """
    assert jsonld.extract_job_posting(page(multi)).location == "Austin; Denver"


def test_a_posting_without_a_location_yields_none():
    bare = '{"@type":"JobPosting","title":"X","description":"<p>hi</p>"}'
    assert jsonld.extract_job_posting(page(bare)).location is None


# --- description rendering -------------------------------------------------


def test_description_html_becomes_markdown():
    got = jsonld.extract_job_posting(page(JOB))
    assert jsonld.to_markdown(got) == "Build the **thing**."


def test_entity_escaped_descriptions_are_unescaped_first():
    # Some sites escape the markup instead of embedding it, the same trap
    # Greenhouse's `content` field sets. Without detection the JD would land in
    # the database as literal "&lt;p&gt;" noise.
    escaped = (
        '{"@type":"JobPosting","title":"X",'
        '"description":"&lt;p&gt;Build the &lt;b&gt;thing&lt;/b&gt;.&lt;/p&gt;"}'
    )
    got = jsonld.extract_job_posting(page(escaped))
    assert jsonld.to_markdown(got) == "Build the **thing**."


def test_real_html_is_not_double_unescaped():
    # An &amp; inside real HTML must survive as a literal ampersand.
    raw = '{"@type":"JobPosting","title":"X","description":"<p>R&amp;D team</p>"}'
    got = jsonld.extract_job_posting(page(raw))
    assert jsonld.to_markdown(got) == "R&D team"


def test_a_missing_description_renders_empty_rather_than_raising():
    bare = '{"@type":"JobPosting","title":"X"}'
    assert jsonld.to_markdown(jsonld.extract_job_posting(page(bare))) == ""
