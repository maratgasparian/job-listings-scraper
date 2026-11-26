"""Tests for cleaning, deduplicating, and flagging job listings."""

from datetime import date, timedelta

import pytest

from config import Place
from listings import (
    apply_new_flags,
    classify_place,
    company_name_key,
    dedupe,
    excluded_region,
    identity_keys,
    listing_key,
    normalize_url,
    roles_for_title,
    row_from_hit,
    snippet,
    sort_rows,
    title_is_excluded,
    title_is_relevant,
)
from regions import build_location_rules, canonical_country
from save_results import SEEN_TTL_DAYS, SeenMemory


def test_snippet_strips_html_and_collapses_whitespace():
    raw = "<p>Lead  the   research.</p>\n<p>Talk to customers.</p>"
    assert snippet(raw, 200) == "Lead the research. Talk to customers."


def test_snippet_turns_markdown_links_into_plain_text():
    raw = "See the [team page](https://example.com/team) for details."
    assert snippet(raw, 200) == "See the team page for details."


def test_snippet_trims_on_a_word_boundary():
    raw = "one two three four five"
    assert snippet(raw, 14) == "one two three…"


def test_snippet_of_empty_values_is_blank():
    assert snippet(None, 50) == ""
    assert snippet("   ", 50) == ""
    assert snippet(float("nan"), 50) == ""


def test_snippet_decodes_entities_and_keeps_comparisons():
    assert snippet("Fish &amp; Chips", 80) == "Fish & Chips"
    assert snippet("Hello&nbsp;there", 80) == "Hello there"
    assert snippet("5 < 10 and 20 > 15", 80) == "5 < 10 and 20 > 15"
    assert snippet("<p>5 &lt; 10 and 20 &gt; 15</p>", 80) == "5 < 10 and 20 > 15"
    assert snippet("<b>Fish &amp; Chips</b>", 80) == "Fish & Chips"


def test_row_from_hit_maps_jobspy_fields():
    row = row_from_hit(
        {
            "title": " Product Manager ",
            "company": "Prada",
            "location": "Milano, LOM, Italy",
            "date_posted": date(2026, 10, 20),
            "site": "indeed",
            "job_url": "https://it.indeed.com/viewjob?jk=abc",
            "description": "<p>Own the roadmap.</p>",
        },
        role="Product Manager",
        place="Milan, Italy",
        snippet_characters=80,
    )
    assert row["title"] == "Product Manager"
    assert row["company"] == "Prada"
    assert row["location"] == "Milano, LOM, Italy"
    assert row["date_posted"] == "2026-10-20"
    assert row["source"] == "indeed"
    assert row["url"] == "https://it.indeed.com/viewjob?jk=abc"
    assert row["description"] == "Own the roadmap."
    assert row["role_searched"] == "Product Manager"
    assert row["place_searched"] == "Milan, Italy"


def test_row_from_hit_drops_a_posting_with_no_title():
    assert row_from_hit({"company": "Prada"}, "PM", "Milan", 80) is None


def test_listing_key_ignores_fragments_and_tracking_but_keeps_path_case():
    left = listing_key(
        {"url": "HTTPS://IT.Indeed.com/ViewJob?jk=AbC&utm_source=email&refId=9/", "title": "A"}
    )
    right = listing_key(
        {"url": "https://it.indeed.com/ViewJob?jk=AbC&trackingId=1&trk=x#apply", "title": "B"}
    )
    other_case = listing_key({"url": "https://it.indeed.com/viewjob?jk=AbC", "title": "C"})
    other_query = listing_key({"url": "https://it.indeed.com/ViewJob?jk=abc", "title": "D"})
    assert left == right
    assert normalize_url(left.removeprefix("url:")) == "https://it.indeed.com/ViewJob?jk=AbC"
    assert left != other_case
    assert left != other_query


def test_listing_key_falls_back_to_title_company_and_location():
    key = listing_key(
        {"title": "PMO  Lead", "company": "ACME", "location": "Milan", "url": ""}
    )
    assert key == "text:pmo lead|acme|milan"


def test_dedupe_merges_the_same_link_found_by_two_roles():
    rows = [
        {
            "title": "PMO",
            "company": "ACME",
            "location": "Milan",
            "date_posted": "2026-10-01",
            "source": "indeed",
            "url": "https://it.indeed.com/viewjob?jk=1",
            "description": "Short.",
            "role_searched": "PMO",
            "place_searched": "Milan, Italy",
        },
        {
            "title": "PMO",
            "company": "ACME",
            "location": "Milan",
            "date_posted": "2026-10-01",
            "source": "indeed",
            "url": "https://it.indeed.com/viewjob?jk=1",
            "description": "A longer description of the same job.",
            "role_searched": "Project Manager",
            "place_searched": "Remote",
        },
    ]
    kept = dedupe(rows)
    assert len(kept) == 1
    assert kept[0]["role_searched"] == "PMO; Project Manager"
    assert kept[0]["place_searched"] == "Milan, Italy; Remote"
    assert kept[0]["description"] == "A longer description of the same job."


def test_dedupe_prefers_the_company_site_when_the_title_and_company_match():
    rows = [
        {
            "title": "Product Manager",
            "company": "Satispay S.r.l.",
            "location": "Milan",
            "date_posted": "2026-10-01",
            "source": "indeed",
            "source_type": "aggregator",
            "url": "https://it.indeed.com/viewjob?jk=1",
            "description": "From Indeed.",
            "role_searched": "Product Manager",
            "place_searched": "Milan, Italy",
        },
        {
            "title": "Product Manager",
            "company": "Satispay",
            "location": "Milan, Italy",
            "date_posted": "2026-10-02",
            "source": "Satispay careers",
            "source_type": "company site",
            "url": "https://jobs.ashbyhq.com/satispay/1",
            "description": "Apply on the company site.",
            "role_searched": "Product Manager",
            "place_searched": "Milan, Italy",
        },
    ]
    kept = dedupe(rows)
    assert len(kept) == 1
    assert kept[0]["url"] == "https://jobs.ashbyhq.com/satispay/1"
    assert kept[0]["source_type"] == "company site"
    assert "indeed" in kept[0]["source"]
    assert "Satispay careers" in kept[0]["source"]
    assert company_name_key("Satispay S.r.l.") == "satispay"


def test_same_title_and_company_is_not_new_if_another_link_was_saved():
    rows = dedupe(
        [
            {
                "title": "Product Manager",
                "company": "Satispay",
                "location": "Milan",
                "source": "indeed",
                "source_type": "aggregator",
                "url": "https://it.indeed.com/viewjob?jk=9",
                "description": "",
                "role_searched": "Product Manager",
                "place_searched": "Milan, Italy",
                "date_posted": "",
            },
            {
                "title": "Product Manager",
                "company": "Satispay",
                "location": "Milan",
                "source": "Satispay careers",
                "source_type": "company site",
                "url": "https://jobs.ashbyhq.com/satispay/9",
                "description": "",
                "role_searched": "Product Manager",
                "place_searched": "Milan, Italy",
                "date_posted": "",
            },
        ]
    )
    seen = {"url:https://it.indeed.com/viewjob?jk=9"}
    flagged = apply_new_flags(rows, seen)
    assert flagged[0]["is_new"] == "no"
    assert flagged[0]["source_type"] == "company site"


def _rules(country, places):
    built = [
        Place(
            name=name,
            indeed_location=indeed,
            linkedin_location=indeed,
            remote=remote,
        )
        for name, indeed, remote in places
    ]
    return build_location_rules(built, canonical_country(country), None)


_PROFILES = {
    "milan": (
        "italy",
        [
            ("Milan, Italy", "Milan, Lombardy, Italy", False),
            ("Remote", "Italy", True),
        ],
    ),
    "berlin": (
        "germany",
        [
            ("Berlin, Germany", "Berlin, Germany", False),
            ("Remote", "Germany", True),
        ],
    ),
    "austin": (
        "usa",
        [
            ("Austin, United States", "Austin, TX", False),
            ("Remote", "United States", True),
        ],
    ),
    "london": (
        "uk",
        [
            ("London, UK", "London, United Kingdom", False),
            ("Remote", "United Kingdom", True),
        ],
    ),
    "toronto": (
        "canada",
        [
            ("Toronto, Canada", "Toronto, ON", False),
            ("Remote", "Canada", True),
        ],
    ),
    "remote": ("germany", [("Remote", "Germany", True)]),
}


@pytest.mark.parametrize(
    ("user", "location", "is_remote", "expected"),
    [
        ("milan", "Milan, Italy", False, "Milan, Italy"),
        ("milan", "Milano, Milan, Italy", False, "Milan, Italy"),
        ("milan", "Paris", False, None),
        ("milan", "Paris", True, None),
        ("milan", "Remote - Italy", True, "Remote"),
        ("milan", "Remote - US only", True, None),
        ("milan", "Anywhere in the World", True, "Remote"),
        ("milan", "United States", True, None),
        ("milan", "London", True, None),
        ("milan", "Remote", False, "Remote"),
        ("milan", "Europe, USA", True, "Remote"),
        ("milan", "Finland", True, None),
        ("milan", "", True, "Remote"),
        ("berlin", "Berlin, Germany", False, "Berlin, Germany"),
        ("berlin", "Milan, Italy", False, None),
        ("berlin", "Remote - Germany", True, "Remote"),
        ("berlin", "Remote - US only", True, None),
        ("berlin", "Remote (EU only)", True, "Remote"),
        ("austin", "Austin, TX", False, "Austin, United States"),
        ("austin", "New York, NY", False, None),
        ("austin", "Remote - USA", True, "Remote"),
        ("austin", "Remote (EU only)", True, None),
        ("austin", "Remote - US only", True, "Remote"),
        ("london", "London, UK", False, "London, UK"),
        ("london", "Berlin, Germany", False, None),
        ("toronto", "Toronto, Canada", False, "Toronto, Canada"),
        ("toronto", "Vancouver, BC", False, None),
        ("remote", "Berlin, Germany", False, None),
        ("remote", "Remote - Germany", True, "Remote"),
        ("remote", "Remote - US only", True, None),
        ("remote", "", True, "Remote"),
    ],
)
def test_classify_place_follows_the_configured_places(user, location, is_remote, expected):
    rules = _rules(*_PROFILES[user])
    assert classify_place(location, is_remote, rules) == expected


@pytest.mark.parametrize(
    ("text", "excluded"),
    [
        ("Bonus only for top performers", False),
        ("remote user research", False),
        ("Remote - US only", True),
        ("Headquarters: Remote, US", True),
        ("based in the united states only", True),
        ("Remote - Germany", False),
        ("Remote (EU only)", False),
    ],
)
def test_excluded_region_uses_whole_phrases_for_a_germany_search(text, excluded):
    rules = _rules(*_PROFILES["berlin"])
    assert excluded_region(text, rules) is excluded


def test_excluded_region_keeps_the_users_own_country_limit():
    austin = _rules(*_PROFILES["austin"])
    assert excluded_region("Remote - US only", austin) is False
    assert excluded_region("Remote (EU only)", austin) is True


def test_dedupe_keeps_different_jobs_at_the_same_company():
    rows = [
        {
            "title": "Product Manager",
            "company": "Satispay",
            "location": "Milan",
            "source": "linkedin",
            "url": "https://www.linkedin.com/jobs/view/1",
            "description": "",
            "role_searched": "Product Manager",
            "place_searched": "Milan, Italy",
            "date_posted": "",
        },
        {
            "title": "Senior Product Manager",
            "company": "Satispay",
            "location": "Milan",
            "source": "linkedin",
            "url": "https://www.linkedin.com/jobs/view/2",
            "description": "",
            "role_searched": "Product Manager",
            "place_searched": "Milan, Italy",
            "date_posted": "",
        },
    ]
    assert len(dedupe(rows)) == 2


def test_apply_new_flags_marks_only_unseen_listings():
    rows = [
        {"url": "https://example.com/old", "title": "Old", "company": "", "location": ""},
        {"url": "https://example.com/new", "title": "New", "company": "", "location": ""},
    ]
    seen = {listing_key(rows[0])}
    flagged = apply_new_flags(rows, seen)
    assert flagged[0]["is_new"] == "no"
    assert flagged[1]["is_new"] == "yes"


def test_title_match_uses_whole_words_in_any_order():
    phrases = ["product manager", "product owner"]
    assert title_is_relevant("Senior Product Manager", phrases)
    assert title_is_relevant("PRODUCT OWNER (Milan)", phrases)
    assert not title_is_relevant("Strategy Associate", phrases)
    assert title_is_relevant("Anything", [])
    assert title_is_relevant("Three-shift Chef", ["hr"]) is False
    assert title_is_relevant("Waiter", ["it"]) is False
    assert title_is_relevant("Software-Entwickler (m/w/d)", ["software entwickler"]) is True
    assert title_is_relevant("Engineer, Software", ["software engineer"]) is True


def test_rejected_words_do_not_match_inside_longer_words():
    assert title_is_excluded("Senior Nurse", ["senior"]) is True
    assert title_is_excluded("Seniority Lead", ["senior"]) is False
    assert title_is_excluded("Internal Auditor", ["intern"]) is False
    assert title_is_excluded("Nurse Intern", ["senior", "intern"]) is True


def test_roles_for_title_keeps_every_role_when_the_filter_is_off():
    assert roles_for_title("Infermiere", ["Nurse"], {"Nurse": ["nurse"]}, False) == ["Nurse"]
    assert roles_for_title(
        "Chef",
        ["Nurse", "Chef"],
        {"Nurse": ["nurse"], "Chef": ["chef"]},
        False,
    ) == ["Nurse", "Chef"]
    assert roles_for_title("Infermiere", ["Nurse"], {"Nurse": ["nurse"]}, True) == []
    assert roles_for_title(
        "Infermiere",
        ["Nurse"],
        {"Nurse": ["nurse", "infermiere"]},
        True,
    ) == ["Nurse"]


def _job(title, company, location, url, source):
    return {
        "title": title,
        "company": company,
        "location": location,
        "source": source,
        "source_type": "aggregator",
        "url": url,
        "description": "",
        "role_searched": "Store Manager",
        "place_searched": location,
        "date_posted": "",
    }


def test_same_title_and_company_in_different_cities_stay_separate():
    rows = [
        _job("Store Manager", "Lidl", "Berlin, Germany", "https://example.com/berlin", "indeed"),
        _job("Store Manager", "Lidl", "Milan, Italy", "https://example.com/milan", "indeed"),
        _job("Store Manager", "Lidl", "Warsaw, Poland", "https://example.com/warsaw", "indeed"),
    ]
    assert len(dedupe(rows)) == 3


def test_accented_and_plain_spellings_of_one_city_merge():
    rows = [
        _job("Store Manager", "Lidl", "München, Germany", "https://example.com/a", "indeed"),
        _job("Store Manager", "Lidl", "Munchen", "https://example.com/b", "linkedin"),
    ]
    assert len(dedupe(rows)) == 1


def test_same_job_from_indeed_and_linkedin_in_one_city_merges():
    rows = [
        _job("Store Manager", "Lidl", "Milan, Italy", "https://it.indeed.com/viewjob?jk=1", "indeed"),
        _job("Store Manager", "Lidl", "Milan, Lombardy", "https://www.linkedin.com/jobs/view/9", "linkedin"),
    ]
    kept = dedupe(rows)
    assert len(kept) == 1
    assert "indeed" in kept[0]["source"]
    assert "linkedin" in kept[0]["source"]


def test_dedupe_keeps_distinct_urls_from_the_same_source():
    first = "https://de.indeed.com/viewjob?jk=1"
    second = "https://de.indeed.com/viewjob?jk=2"
    rows = [
        _job("Software Engineer", "Deutsche Bank", "Frankfurt, Germany", first, "indeed"),
        _job("Software Engineer", "Deutsche Bank", "Frankfurt, Germany", second, "indeed"),
    ]
    kept = dedupe(rows)
    assert len(kept) == 2
    assert {row["url"] for row in kept} == {first, second}


def test_dedupe_still_merges_the_same_role_across_sources():
    rows = [
        _job(
            "Software Engineer",
            "Deutsche Bank",
            "Frankfurt, Germany",
            "https://de.indeed.com/viewjob?jk=1",
            "indeed",
        ),
        _job(
            "Software Engineer",
            "Deutsche Bank",
            "Frankfurt",
            "https://www.linkedin.com/jobs/view/99",
            "linkedin",
        ),
    ]
    kept = dedupe(rows)
    assert len(kept) == 1
    assert "indeed" in kept[0]["source"]
    assert "linkedin" in kept[0]["source"]
    extra = _job(
        "Software Engineer",
        "Deutsche Bank",
        "Frankfurt, Germany",
        "https://de.indeed.com/viewjob?jk=3",
        "indeed",
    )
    assert len(dedupe(rows + [extra])) == 2


def test_job_board_beats_an_aggregator_for_the_same_role():
    indeed = _job(
        "Software Engineer",
        "Deutsche Bank",
        "Frankfurt, Germany",
        "https://de.indeed.com/viewjob?jk=1",
        "indeed",
    )
    board = _job(
        "Software Engineer",
        "Deutsche Bank",
        "Frankfurt, Germany",
        "https://www.arbeitnow.com/jobs/1",
        "arbeitnow",
    )
    board["source_type"] = "job board"
    kept = dedupe([indeed, board])
    assert len(kept) == 1
    assert kept[0]["source_type"] == "job board"
    assert kept[0]["url"] == board["url"]
    reversed_rows = dedupe([board, indeed])
    assert reversed_rows[0]["source_type"] == "job board"
    assert reversed_rows[0]["url"] == board["url"]


def test_sort_puts_new_jobs_first_and_newest_dates_above_older_ones():
    rows = [
        {"is_new": "no", "date_posted": "2026-10-20", "title": "old-recent"},
        {"is_new": "yes", "date_posted": "2026-10-01", "title": "new-older"},
        {"is_new": "yes", "date_posted": "2026-10-18", "title": "new-newer"},
        {"is_new": "yes", "date_posted": "", "title": "new-undated"},
    ]
    titles = [row["title"] for row in sort_rows(rows)]
    assert titles == ["new-newer", "new-older", "new-undated", "old-recent"]


def _memory(row, seen_on, sources):
    return SeenMemory(
        {key: {"seen_on": seen_on, "sources": sources} for key in identity_keys(row)}
    )


def test_same_source_with_a_new_url_stays_new_and_another_site_does_not():
    today = date(2026, 11, 26)
    seen_on = (today - timedelta(days=10)).isoformat()
    indeed = _job(
        "Software Engineer",
        "Deutsche Bank",
        "Frankfurt, Germany",
        "https://de.indeed.com/viewjob?jk=1",
        "indeed",
    )
    other_indeed = _job(
        "Software Engineer",
        "Deutsche Bank",
        "Frankfurt, Germany",
        "https://de.indeed.com/viewjob?jk=2",
        "indeed",
    )
    linkedin = _job(
        "Software Engineer",
        "Deutsche Bank",
        "Frankfurt, Germany",
        "https://www.linkedin.com/jobs/view/9",
        "linkedin",
    )
    memory = _memory(indeed, seen_on, ["indeed"])
    assert apply_new_flags([indeed], memory, today=today)[0]["is_new"] == "no"
    assert apply_new_flags([other_indeed], memory, today=today)[0]["is_new"] == "yes"
    assert apply_new_flags([linkedin], memory, today=today)[0]["is_new"] == "no"


def test_a_seen_job_is_new_again_after_the_expiry_window():
    today = date(2026, 11, 26)
    row = _job(
        "Software Engineer",
        "Deutsche Bank",
        "Frankfurt, Germany",
        "https://de.indeed.com/viewjob?jk=1",
        "indeed",
    )
    fresh = (today - timedelta(days=SEEN_TTL_DAYS)).isoformat()
    stale = (today - timedelta(days=SEEN_TTL_DAYS + 1)).isoformat()
    assert apply_new_flags([row], _memory(row, fresh, ["indeed"]), today=today)[0]["is_new"] == "no"
    assert apply_new_flags([row], _memory(row, stale, ["indeed"]), today=today)[0]["is_new"] == "yes"
