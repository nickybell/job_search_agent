"""The direct job-add path: one user-supplied URL through Step 2.

The ATS detail fetch is stubbed, so nothing here makes a network request. What
is being pinned is that a hand-added row goes through the *same* canonicalize /
idempotent-insert / JD-capture machinery as a searched one, and that the two
deliberate departures hold: no search_findings row, and an unsupported ATS is
not a rejection.
"""

from __future__ import annotations

import httpx
import pytest
from conftest import make_posting

from jsa import db, manual
from jsa.ats.fetch import ATSDetail

GREENHOUSE_URL = "https://job-boards.greenhouse.io/acme/jobs/4567"


@pytest.fixture
def stub_fetch(monkeypatch):
    """Stub the ATS detail fetch; returns a recorder of what it was called with."""

    def _stub(detail=None, raises=None):
        seen = []

        def fake(resolved, http_client):
            seen.append(resolved)
            if raises is not None:
                raise raises
            return detail

        monkeypatch.setattr(manual, "fetch_detail", fake)
        return seen

    return _stub


def row_for(client, posting_id: int) -> dict:
    cursor = client.execute(
        "SELECT company, title, title_slug, normalized_company, jd_markdown, location, "
        "search_agent, decision, date_posted FROM postings WHERE id = ?",
        (posting_id,),
    )
    keys = (
        "company title title_slug normalized_company jd_markdown location "
        "search_agent decision date_posted"
    ).split()
    return dict(zip(keys, cursor.fetchone(), strict=True))


# --- company_from_board (pure) --------------------------------------------


@pytest.mark.parametrize(
    ("board", "expected"),
    [
        ("gitlab", "Gitlab"),
        ("scale-ai", "Scale Ai"),
        ("acme_corp", "Acme Corp"),
        ("foo.bar", "Foo Bar"),
        ("", ""),
    ],
)
def test_company_from_board(board, expected):
    assert manual.company_from_board(board) == expected


# --- the happy path --------------------------------------------------------


def test_add_captures_the_jd_and_tags_the_row_manual(config, client, stub_fetch):
    stub_fetch(
        ATSDetail(jd_markdown="## About\nfull JD", location="Remote, US", title="Head of CE")
    )
    result = manual.add_posting(GREENHOUSE_URL, config, interactive=False)

    assert result.status == "inserted"
    assert (result.platform, result.jd_captured) == ("greenhouse", True)
    row = row_for(client, result.posting_id)
    assert row["search_agent"] == "manual"
    assert row["jd_markdown"] == "## About\nfull JD"
    assert row["location"] == "Remote, US"
    # Company falls back to the board slug; the title comes from the ATS record.
    assert (row["company"], row["title"]) == ("Acme", "Head of CE")
    assert row["title_slug"] == "Head of CE"
    # And it lands in the review backlog exactly like a searched posting.
    assert row["decision"] is None
    assert [r[0] for r in db.pending_review(client)] == [result.posting_id]


def test_add_writes_no_search_findings_row(config, client, stub_fetch):
    # search_findings is A/B telemetry; a hand-added posting is not a search
    # result and must not skew agent coverage or precision.
    stub_fetch(ATSDetail(jd_markdown="jd", location=None, title="Head of CE"))
    manual.add_posting(GREENHOUSE_URL, config, interactive=False)
    assert client.execute("SELECT COUNT(*) FROM search_findings").fetchone()[0] == 0


def test_explicit_flags_win_over_the_ats_record(config, client, stub_fetch):
    stub_fetch(ATSDetail(jd_markdown="jd", location=None, title="ATS Canonical Title"))
    result = manual.add_posting(
        GREENHOUSE_URL,
        config,
        company="Acme Corporation, Inc.",
        title="Director, Customer Education",
        date_posted="2026-08-09",
        interactive=False,
    )
    row = row_for(client, result.posting_id)
    # The JD capture must not clobber a deliberate override with the ATS
    # transcription, and title_slug has to stay consistent with the title.
    assert row["title"] == "Director, Customer Education"
    assert row["title_slug"] == "Director, Customer Education"
    assert row["normalized_company"] == "Acme Corporation"
    assert row["date_posted"] == "2026-08-09"


# --- idempotency -----------------------------------------------------------


def test_re_adding_a_known_url_is_a_no_op(config, client, stub_fetch):
    stub_fetch(ATSDetail(jd_markdown="jd", location=None, title="Head of CE"))
    first = manual.add_posting(GREENHOUSE_URL, config, interactive=False)
    second = manual.add_posting(GREENHOUSE_URL, config, interactive=False)
    assert second.status == "already_present"
    assert second.posting_id == first.posting_id
    assert client.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 1


def test_a_tracking_decorated_url_matches_the_existing_row(config, client, stub_fetch):
    # Canonicalization is the single idempotency mechanism; the manual path
    # reuses it rather than adding a second one.
    stub_fetch(ATSDetail(jd_markdown="jd", location=None, title="Head of CE"))
    first = manual.add_posting(GREENHOUSE_URL, config, interactive=False)
    decorated = f"{GREENHOUSE_URL}?utm_source=linkedin&gh_src=abc#apply"
    second = manual.add_posting(decorated, config, interactive=False)
    assert (second.status, second.posting_id) == ("already_present", first.posting_id)


def test_add_does_not_disturb_a_row_the_pipeline_already_decided(config, client, stub_fetch):
    posting_id = db.insert_posting(client, make_posting(canonical_url=GREENHOUSE_URL))
    db.record_decision(client, posting_id, "Apply", "already reviewed")
    stub_fetch(ATSDetail(jd_markdown="jd", location=None, title="Head of CE"))
    result = manual.add_posting(GREENHOUSE_URL, config, interactive=False)
    assert result.status == "already_present"
    assert row_for(client, posting_id)["decision"] == "Apply"


# --- degraded paths --------------------------------------------------------


def test_an_unsupported_ats_still_inserts_without_a_jd(config, client, stub_fetch):
    # The four-ATS rule gates what the *search* may return; here the user has
    # already vouched for the posting.
    stub_fetch(ATSDetail(jd_markdown="unused", location=None, title=None))
    result = manual.add_posting(
        "https://acme.wd1.myworkdayjobs.com/careers/job/Remote/Enablement-Lead_R-42",
        config,
        company="Acme",
        title="Enablement Lead",
        interactive=False,
    )
    assert result.status == "inserted"
    assert (result.platform, result.jd_captured, result.fetch_error) == (None, False, None)
    assert row_for(client, result.posting_id)["jd_markdown"] is None


def test_a_failed_fetch_degrades_to_a_null_jd_rather_than_dropping_the_row(
    config, client, stub_fetch
):
    stub_fetch(raises=httpx.HTTPError("boom"))
    result = manual.add_posting(
        GREENHOUSE_URL, config, company="Acme", title="Enablement Lead", interactive=False
    )
    assert result.status == "inserted"
    assert result.jd_captured is False
    assert "HTTPError" in result.fetch_error
    assert row_for(client, result.posting_id)["jd_markdown"] is None


def test_missing_title_on_an_unsupported_url_fails_loudly(config, stub_fetch):
    stub_fetch(None)
    with pytest.raises(manual.ManualAddError, match="company and title are required"):
        manual.add_posting("https://example.com/careers/123", config, interactive=False)


@pytest.mark.parametrize("bad", ["acme.com/jobs/1", "ftp://acme.com/jobs/1", "   "])
def test_a_non_http_url_is_rejected(config, bad):
    with pytest.raises(manual.ManualAddError, match="not an http"):
        manual.add_posting(bad, config, interactive=False)


def test_interactive_prompts_are_prefilled_with_the_derived_values(
    config, client, stub_fetch, monkeypatch
):
    stub_fetch(ATSDetail(jd_markdown="jd", location=None, title="Head of CE"))
    seen: list[tuple[str, str]] = []

    def fake_ask(message, *, default=""):
        seen.append((message.strip(), default))
        return {"Company:": "Acme Corp", "Title:": "Head of Customer Education"}[message.strip()]

    monkeypatch.setattr(manual.prompting, "ask", fake_ask)
    result = manual.add_posting(GREENHOUSE_URL, config, interactive=True)

    assert seen == [("Company:", "Acme"), ("Title:", "Head of CE")]
    row = row_for(client, result.posting_id)
    assert (row["company"], row["title"]) == ("Acme Corp", "Head of Customer Education")
