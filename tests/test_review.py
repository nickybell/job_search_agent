"""Step 3 review loop: decision capture and the two revision routes.

The loop is driven entirely through :mod:`jsa.prompting`, so a scripted ``ask``
stands in for the terminal. That also exercises ``ask_choice``'s validation
loop, since it delegates to the same function.
"""

from __future__ import annotations

import pytest
from conftest import make_posting

from jsa import db, prompting, review


class ScriptedPrompt:
    """Answers prompts from a fixed script; records the pre-filled defaults."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, message: str, *, default: str = "") -> str:
        self.calls.append((message, default))
        if not self.answers:
            # Running dry means the loop asked more than the test expected;
            # Quit is how a real Ctrl-D would end the session.
            raise prompting.Quit
        return self.answers.pop(0)


@pytest.fixture
def drive(monkeypatch, config, client):
    """Seed N postings, then run the review loop against a scripted terminal."""

    def _drive(answers: list[str], count: int = 2) -> ScriptedPrompt:
        for i in range(count):
            db.insert_posting(
                client,
                make_posting(
                    company=f"Company{i}",
                    url=f"https://job-boards.greenhouse.io/c{i}/jobs/{i}",
                    canonical_url=f"https://job-boards.greenhouse.io/c{i}/jobs/{i}",
                ),
            )
        scripted = ScriptedPrompt(answers)
        monkeypatch.setattr(prompting, "ask", scripted)
        monkeypatch.setattr(review, "_open_in_browser", lambda url: None)
        review.run_review(config)
        return scripted

    return _drive


def decisions(client) -> list[tuple]:
    rows = client.execute(
        "SELECT company, decision, fit_feedback FROM postings ORDER BY id"
    ).fetchall()
    return [tuple(r) for r in rows]


# --- parse_feedback_entry (pure) ------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", (None, None, False)),
        ("   ", (None, None, False)),
        ("strong fit", (None, "strong fit", False)),
        (":a", ("Apply", None, False)),
        (":s", ("Skip", None, False)),
        (":apply changed my mind", ("Apply", "changed my mind", False)),
        (":SKIP  too junior", ("Skip", "too junior", False)),
        (":b", (None, None, True)),
        (":back", (None, None, True)),
        # A colon inside a real comment must never be read as a command.
        ("comp: too low", (None, "comp: too low", False)),
        ("note :a is a command", (None, "note :a is a command", False)),
    ],
)
def test_parse_feedback_entry(raw, expected):
    assert review.parse_feedback_entry(raw) == expected


# --- the loop --------------------------------------------------------------


def test_straightforward_pass_records_both(drive, client):
    drive(["a", "great fit", "s", "wrong level"])
    assert decisions(client) == [
        ("Company0", "Apply", "great fit"),
        ("Company1", "Skip", "wrong level"),
    ]


def test_feedback_command_flips_the_decision_and_keeps_the_comment(drive, client):
    # Chose Skip, then changed their mind mid-comment.
    drive(["s", ":a actually a strong fit", "s", ""])
    assert decisions(client) == [
        ("Company0", "Apply", "actually a strong fit"),
        ("Company1", "Skip", None),
    ]


def test_feedback_back_command_returns_to_the_decision_prompt(drive, client):
    scripted = drive(["a", ":b", "s", "wrong level", "s", ""])
    assert decisions(client)[0] == ("Company0", "Skip", "wrong level")
    # Four prompts for the first posting: decision, feedback, decision, feedback.
    assert scripted.calls[2][0].startswith("  [a]pply")


def test_back_amends_the_previous_posting_with_prefilled_values(drive, client):
    scripted = drive(["s", "too junior", "b", "a", "reread it, worth applying", "s", ""])
    assert decisions(client) == [
        ("Company0", "Apply", "reread it, worth applying"),
        ("Company1", "Skip", None),
    ]
    # The amended comment must arrive pre-filled, so the user edits it rather
    # than retyping it...
    assert "too junior" in [default for _, default in scripted.calls]
    # ...but the decision prompt must NOT be pre-filled, or typing the natural
    # 'a' on top of a pre-filled 's' would submit 'sa'. It states the current
    # decision in the label instead.
    amend_prompt = scripted.calls[3][0]
    assert amend_prompt.endswith("(Enter keeps Skip): ")
    assert scripted.calls[3][1] == ""


def test_enter_at_an_amend_prompt_keeps_the_recorded_decision(drive, client):
    # Stepping back to fix only the comment must not force re-picking a/s.
    drive(["a", "typo hree", "b", "", "typo here", "s", ""])
    assert decisions(client)[0] == ("Company0", "Apply", "typo here")


def test_enter_is_not_accepted_before_a_decision_exists(drive, client):
    # A bare Enter on a fresh posting must re-prompt, not silently record.
    drive(["", "a", "", "s", "", ""])
    assert decisions(client)[0] == ("Company0", "Apply", None)


def test_back_is_not_offered_on_the_first_posting(drive):
    scripted = drive(["a", "", "a", ""])
    first_prompt = scripted.calls[0][0]
    assert "[b]ack" not in first_prompt
    assert "[b]ack" in scripted.calls[2][0]


def test_final_amend_offer_can_revise_the_last_entry(drive, client):
    # ... "b" at the end-of-backlog offer reopens the final posting.
    drive(["a", "", "s", "typo", "b", "a", "meant Apply", ""])
    assert decisions(client) == [
        ("Company0", "Apply", None),
        ("Company1", "Apply", "meant Apply"),
    ]


def test_final_amend_offer_declined_by_enter(drive, client):
    drive(["a", "", "s", "", ""])
    assert decisions(client) == [("Company0", "Apply", None), ("Company1", "Skip", None)]


def test_quit_leaves_the_remaining_backlog_undecided(drive, client):
    drive(["a", "noted", "q"])
    assert decisions(client) == [("Company0", "Apply", "noted"), ("Company1", None, None)]


def test_invalid_key_reprompts(drive, client):
    drive(["x", "a", "", "s", "", ""])
    assert decisions(client)[0] == ("Company0", "Apply", None)


def test_ctrl_d_mid_session_keeps_committed_decisions(drive, client):
    # The script runs dry after the first decision, which raises Quit.
    drive(["a", "saved"])
    assert decisions(client) == [("Company0", "Apply", "saved"), ("Company1", None, None)]


def test_empty_backlog_is_a_no_op(monkeypatch, config, client, capsys):
    monkeypatch.setattr(prompting, "ask", ScriptedPrompt([]))
    review.run_review(config)
    assert "No postings awaiting review" in capsys.readouterr().out
