"""name_matching tests. Directory rows are synthetic (made-up addresses and IDs)."""

import pandas as pd
import pytest

from restaurant_alts.name_matching import (
    ALIAS, EXACT, NO_MATCH, SUGGESTIONS, match_restaurant, normalize_address, normalize_name,
)


@pytest.fixture
def directory():
    return pd.DataFrame([
        ("1", "Don Angie", "1 Fake St", "10014"),
        ("2", "Café Lumière", "2 Sample Ave", "10003"),
        ("3", "Joe's Pizza", "3 Test Pl", "10012"),
        ("4", "Joe's Pizza", "44 Other St", "10036"),        # second branch
        ("5", "Sun & Moon Noodles", "5 Fake Blvd", "10002"),
        ("6", "Example Bistro", None, "10001"),
    ], columns=["nyc_id", "name", "address", "zip"])


@pytest.fixture
def aliases():
    return pd.DataFrame([("DA", "1", "manual")], columns=["alias", "nyc_id", "source"])


# ---------------------------------------------------------------- normalization

@pytest.mark.parametrize("raw, expected", [
    ("  Don   Angie ", "don angie"),
    ("Café Lumière", "cafe lumiere"),
    ("Joe’s Pizza", "joes pizza"),
    ("Sun & Moon", "sun and moon"),
    ("Pizza-Bar!", "pizza bar"),
    (None, ""),
])
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


def test_normalize_address():
    assert normalize_address("10 West 10th Street, New York, NY") == "10 w 10 st"


# ---------------------------------------------------------------- exact / alias

def test_exact_match_case_insensitive_single_is_preselected(directory):
    r = match_restaurant("don angie", directory)
    assert r.status == EXACT
    assert r.selected_id == "1"
    assert not r.requires_choice
    assert "confirm" in r.message


def test_accent_insensitive_exact(directory):
    r = match_restaurant("cafe lumiere", directory)
    assert (r.status, r.selected_id) == (EXACT, "2")


def test_multiple_branches_require_choice(directory):
    r = match_restaurant("joes pizza", directory)
    assert r.status == EXACT
    assert r.selected_id is None and r.requires_choice
    assert {c.nyc_id for c in r.candidates} == {"3", "4"}
    assert all(c.address for c in r.candidates)  # addresses shown so the user can choose


def test_alias_match(directory, aliases):
    r = match_restaurant("da", directory, aliases)
    assert (r.status, r.selected_id) == (ALIAS, "1")
    assert r.candidates[0].method == "alias"


def test_alias_to_multiple_restaurants_requires_choice(directory):
    al = pd.DataFrame([("joes", "3", "manual"), ("joes", "4", "manual")],
                      columns=["alias", "nyc_id", "source"])
    r = match_restaurant("Joes", directory, al)
    assert r.status == ALIAS and r.requires_choice


# ---------------------------------------------------------------- fuzzy never selects

@pytest.mark.parametrize("query", ["Don Angie NYC", "Don Angi", "Dn Angie", "don angie restaurant"])
def test_near_miss_gives_suggestions_not_selection(directory, query):
    r = match_restaurant(query, directory)
    assert r.status == SUGGESTIONS
    assert r.selected_id is None and r.requires_choice
    assert r.candidates[0].nyc_id == "1"
    assert all(c.method == "fuzzy" for c in r.candidates)


def test_fuzzy_returns_at_most_five(directory):
    many = pd.concat([directory] + [
        pd.DataFrame([(f"x{i}", f"Pizza Place {i}", f"{i} Fake St", "10001")],
                     columns=directory.columns) for i in range(10)
    ])
    r = match_restaurant("pizza plce", many)
    assert r.status == SUGGESTIONS and len(r.candidates) <= 5


def test_fuzzy_lists_both_branches(directory):
    r = match_restaurant("Joes Pizzza", directory)
    assert r.status == SUGGESTIONS
    assert {"3", "4"} <= {c.nyc_id for c in r.candidates}


# ---------------------------------------------------------------- empty states

@pytest.mark.parametrize("query", ["Zzyzx Qwerty", "", "   ", "!!!"])
def test_no_match_is_clear_empty_state(directory, query):
    r = match_restaurant(query, directory)
    assert r.status == NO_MATCH
    assert r.candidates == [] and r.selected_id is None
    assert not r.requires_choice
    assert r.message


def test_missing_address_is_none_not_error(directory):
    r = match_restaurant("Example Bistro", directory)
    assert r.selected_id == "6" and r.candidates[0].address is None


def test_empty_directory(directory):
    r = match_restaurant("Don Angie", directory.iloc[0:0])
    assert r.status == NO_MATCH


def test_duplicate_records_of_one_restaurant_show_once():
    d = pd.DataFrame([
        ("50104682", "SAMPLE THAI", "204 EAST   13 STREET", "10003", "Thai"),
        ("50156251", "Sample Thai", "204 East 13th St", "10003", None),     # re-permit duplicate
        ("70000001", "Sample Thai", "9 Other Ave", "10010", "Thai"),       # real second branch
    ], columns=["nyc_id", "name", "address", "zip", "cuisine"])
    r = match_restaurant("sample thai", d)
    assert sorted(c.nyc_id for c in r.candidates) == ["50104682", "70000001"]
    assert r.requires_choice  # two real branches still need a choice

    only_dupes = d.iloc[:2]
    r = match_restaurant("sample thai", only_dupes)
    assert r.selected_id == "50104682"  # one restaurant: pre-selected for confirmation
    # A record matched to Yelp wins even without a cuisine.
    r = match_restaurant("sample thai", only_dupes, preferred_ids={"50156251"})
    assert r.selected_id == "50156251"
