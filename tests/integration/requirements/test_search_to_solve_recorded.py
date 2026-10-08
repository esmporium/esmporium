"""
A requirement searched, saved and solved, end to end, with no network

This is the point of R4, and the loop we actually want to run daily: write a
requirement, search for it, dump everything the search found into the database, and
ask [`solve`][esmporium.requirements.solve] what it adds up to. Run it again tomorrow
and the difference between the two
[`SolveResult`][esmporium.requirements.SolveResult]s is what changed.

It runs off a recorded ESGF-NG response (`tests/test-data/search/`) through a mock
transport, so it is deterministic and part of the ordinary test run. The live
counterpart is `test_search_to_solve_live.py`, which can only assert loosely because
what CMIP7 holds changes by the day.

The recorded CMIP7 response is a convenient fixture for this: it carries exactly two
datasets which differ *only* in `processing_id` (`tminavg-h2m-hxy-u` and
`tmaxavg-h2m-hxy-u`), so one requirement can be made to resolve, go ambiguous, or keep
both, by changing nothing but `prefer` and `cardinality`.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from sqlmodel import Session, select

from esmporium.db import (
    Availability,
    DatabaseCatalogue,
    Dataset,
    build_result_processor_factory,
)
from esmporium.db.migrate import upgrade_to_head
from esmporium.query import Query
from esmporium.requirements import Cardinality, all_of, leaf, requirement, solve
from esmporium.search import (
    INBUILT_SEARCH_API_FACADE_STORE,
    build_list_selector,
    search,
)

HOST = "search.east.esgf.io"
"""The host the recorded response came from"""

RECORDED = Path(__file__).parents[2] / "test-data" / "search"
"""Where the recorded responses live"""

TMIN = "tminavg-h2m-hxy-u"
TMAX = "tmaxavg-h2m-hxy-u"
"""The two datasets' `processing_id`s, which is all that tells them apart"""

LIMIT = 100
"""
Bigger than the recorded response's `numberMatched` (64)

Below it, `collect_all_pages` would emit a `PaginationWarning` for a search which is
not really paginating here.
"""


def recorded_body(*, retract: tuple[str, ...] = ()) -> dict:
    """
    Get the recorded CMIP7 response, with the named datasets reported as retracted

    `retract` holds `processing_id`s, so a test can retire one of the two datasets and
    leave the other, which is how a group survives a retraction instead of vanishing
    with it.

    The `next` link is stripped. `collect_all_pages` follows `next`, and a mock
    transport which answers every request with the same body would hand back a page it
    had already seen, which `PaginationLimitError` (rightly) refuses. Removing the link
    is how a one-page answer is expressed, not a convenience.
    """
    raw = json.loads((RECORDED / "esgf-ng-stac-cmip7-east-search.json").read_text())

    def retired(feature: dict) -> bool:
        return feature["properties"]["cmip7:variable_branding_suffix"] in retract

    body = dict(raw)
    body["links"] = [link for link in raw["links"] if link.get("rel") != "next"]
    body["features"] = [
        {
            **feature,
            "properties": {**feature["properties"], "retracted": retired(feature)},
        }
        for feature in raw["features"]
    ]

    return body


@pytest.fixture
def served():
    """
    Get a factory for a client which answers every search with a recorded body

    Takes the same keywords as [recorded_body][(m).recorded_body], so a test can serve
    today's answer and then tomorrow's.
    """

    def factory(**kwargs) -> httpx.Client:
        body = recorded_body(**kwargs)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body)

        return httpx.Client(transport=httpx.MockTransport(handler))

    return factory


@pytest.fixture
def selector():
    """The one facade the recorded response belongs to"""
    return build_list_selector(
        [
            INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
                "CMIP7", HOST
            )
        ]
    )


@pytest.fixture
def migrated(engine):
    """A migrated, file-backed database, which is what a real caller has"""
    upgrade_to_head(engine)
    return engine


def a_requirement(tree=None, **kwargs):
    """
    Build a requirement for the recorded CMIP7 `tas` datasets

    Grouped by model and variant, which is one run of an analysis, and narrowed to the
    experiment the recorded response holds so the group is the one we expect.
    """
    kwargs.setdefault("group_by", ("model", "variant_label"))
    kwargs.setdefault("where", Query(project="CMIP7", experiment="esm-scen7-vl"))
    return requirement(
        name="cmip7-tas-monthly",
        tree=tree
        if tree is not None
        else leaf(Query(variable="tas", reporting_interval="mon"), "tas"),
        **kwargs,
    )


def run_search(requirement_to_search, migrated, selector, client):
    """Search a requirement, saving everything it finds into `migrated`"""
    with client:
        return search(
            requirement_to_search,
            selector,
            limit=LIMIT,
            client=client,
            processor_factory=build_result_processor_factory(migrated),
        )


def saved_datasets(engine) -> list[str]:
    """The `processing_id` of every dataset saved, in the order they were saved"""
    with Session(engine) as session:
        return [
            row.processing_id
            for row in session.exec(select(Dataset).order_by(Dataset.id)).all()
        ]


def test_a_requirement_is_searched_saved_and_solved(migrated, selector, served):
    """
    The whole loop: a requirement in, a satisfied group out

    `prefer` is what makes this resolve rather than go ambiguous -- the two datasets
    are otherwise indistinguishable -- so this also pins that the tie-break survives
    the round trip through the database.
    """
    req = a_requirement(prefer={"processing_id": (TMIN,)})

    outcomes = run_search(req, migrated, selector, served())

    # One leaf, one project, so one sub-search, and it says which.
    assert [(outcome.roles, outcome.project) for outcome in outcomes] == [
        (("tas",), "CMIP7")
    ]
    # Everything the search found was saved, not just what the requirement needed.
    assert sorted(saved_datasets(migrated)) == sorted([TMAX, TMIN])

    result = solve(req, DatabaseCatalogue(migrated))

    assert not result.unsatisfied
    assert not result.ambiguous
    (group,) = result.satisfied.values()
    assert dict(group.key) == {"model": "UKESM1-3-LL", "variant_label": "r1i1p1f1"}
    assert group.one("tas").processing_id == TMIN


def test_two_candidates_with_nothing_to_choose_between_them_are_ambiguous(
    migrated, selector, served
):
    """
    Without `prefer`, the group is ambiguous and the explanation says what differed

    `processing_id` is the only facet the two datasets disagree on, so naming it is
    the whole of the useful answer.
    """
    req = a_requirement()

    run_search(req, migrated, selector, served())
    result = solve(req, DatabaseCatalogue(migrated))

    assert not result.satisfied
    (group,) = result.ambiguous.values()
    assert "processing_id" in group.explanation.render()


def test_cardinality_all_keeps_both(migrated, selector, served):
    """An analysis whose subject *is* the spread gets both datasets, not an error"""
    req = a_requirement(cardinality=Cardinality.ALL)

    run_search(req, migrated, selector, served())
    result = solve(req, DatabaseCatalogue(migrated))

    (group,) = result.satisfied.values()
    assert sorted(entry.processing_id for entry in group.roles["tas"]) == sorted(
        [TMAX, TMIN]
    )


def test_a_leaf_nothing_was_published_for_is_unsatisfied(migrated, selector, served):
    """
    A requirement asking for something that is not there says so, and names the role

    The recorded response holds `tas` and nothing else, so `rlut` cannot be satisfied
    however much `tas` is found.
    """
    req = a_requirement(
        tree=all_of(
            leaf(Query(variable="tas", reporting_interval="mon"), "tas"),
            leaf(Query(variable="rlut", reporting_interval="mon"), "radiation"),
        ),
        prefer={"processing_id": (TMIN,)},
    )

    run_search(req, migrated, selector, served())
    result = solve(req, DatabaseCatalogue(migrated))

    assert not result.satisfied
    (group,) = result.unsatisfied.values()
    assert "radiation" in group.explanation.render()


def test_everything_we_searched_for_is_findable_through_the_catalogue(
    migrated, selector, served
):
    """
    The write side and the read side agree about every row

    Closes the loop between `DatasetFacets` (what a parser produced) and
    `CatalogueEntry` (what the catalogue gives back): every dataset the search saved
    can be found again by the facets it was saved under.
    """
    req = a_requirement()
    run_search(req, migrated, selector, served())

    catalogue = DatabaseCatalogue(migrated)
    with Session(migrated) as session:
        rows = session.exec(select(Dataset)).all()

    assert rows
    for row in rows:
        found = catalogue.find(
            Query(
                project=row.project,
                model=row.model,
                variable=row.variable,
                processing_id=row.processing_id,
            )
        )
        assert [entry.id for entry in found] == [row.id]


def test_a_retracted_dataset_drops_its_group_from_the_solve(migrated, selector, served):
    """
    The day-to-day question: a dataset retracted overnight stops satisfying anything

    Worth being precise about what happens, because it is not quite "satisfied becomes
    unsatisfied". Groups are *discovered* from the datasets which are available, not
    listed up front, so when the only datasets in a group go away the group goes with
    them: it leaves `satisfied` and turns up in none of the three mappings. The change
    to watch for day to day is therefore the set of satisfied groups, not a group's
    status.

    (A group whose *other* leaf is retracted does become `unsatisfied` -- the group is
    still discovered, from the leaf which survived. That is the test below.)

    The rows are not deleted, and `Availability.ANY` still resolves the group, which
    is what makes this a statement about availability rather than about data loss.
    """
    req = a_requirement(prefer={"processing_id": (TMIN,)})
    catalogue = DatabaseCatalogue(migrated)

    run_search(req, migrated, selector, served())
    today = solve(req, catalogue)

    assert len(today.satisfied) == 1

    # Tomorrow: same requirement, same search, and ESGF now reports both retracted.
    # Re-searching is what updates the stored versions in place.
    run_search(req, migrated, selector, served(retract=(TMIN, TMAX)))
    tomorrow = solve(req, catalogue)

    assert not tomorrow.satisfied
    assert not tomorrow.unsatisfied
    assert not tomorrow.ambiguous
    # The question did not change, so the answer changing is about the data.
    assert (
        tomorrow.requirement.requirement_hash() == today.requirement.requirement_hash()
    )

    # Nothing was deleted: the rows are still there, they just no longer count.
    assert len(saved_datasets(migrated)) == 2
    on_record = solve(req, DatabaseCatalogue(migrated, availability=Availability.ANY))
    assert set(on_record.satisfied) == set(today.satisfied)


def test_a_group_whose_other_leaf_is_retracted_becomes_unsatisfied(
    migrated, selector, served
):
    """
    A retraction which leaves the group standing turns it `unsatisfied`, and says why

    Two leaves over the same `tas` data, told apart by `processing_id`. Retracting one
    of the two datasets leaves the other available, so the group is still discovered --
    and now one of its roles cannot be filled.

    Together with the test above this is the whole of the day-to-day story: a
    retraction either takes the group with it or leaves it unsatisfied, and either way
    the requirement's hash is untouched.
    """
    req = a_requirement(
        tree=all_of(
            leaf(Query(variable="tas", processing_id=TMIN), "minimum"),
            leaf(Query(variable="tas", processing_id=TMAX), "maximum"),
        )
    )
    catalogue = DatabaseCatalogue(migrated)

    run_search(req, migrated, selector, served())
    today = solve(req, catalogue)

    assert len(today.satisfied) == 1

    run_search(req, migrated, selector, served(retract=(TMAX,)))
    tomorrow = solve(req, catalogue)

    assert not tomorrow.satisfied
    (group,) = tomorrow.unsatisfied.values()
    # The same group, moved -- not a different group which happens to be unsatisfied.
    assert set(tomorrow.unsatisfied) == set(today.satisfied)
    # And the explanation names the role which can no longer be filled.
    assert "maximum" in group.explanation.render()
    assert (
        tomorrow.requirement.requirement_hash() == today.requirement.requirement_hash()
    )
