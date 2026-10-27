"""
What a project costs before esmporium can search it, with CMIP6Plus as the cheap case

The companion to `test_cordex_other_terms_live.py`, and a deliberate comparison. Both
projects are ones esmporium cannot search as it stands, and what each one costs to
search anyway is the thing worth seeing side by side, because it separates two
questions which are easy to run together:

- *is this project registered?* -- which decides whether a caller has to inject a
  facade and a selector at all, and
- *does this project fit our dataset columns?* -- which decides whether the facade they
  build needs a naming table of its own.

CORDEX answers no to both. CMIP6Plus answers no to the first and **yes** to the second,
because a CMIP6Plus document is the same shape as a CMIP6 document. Hence:

                            CORDEX-CMIP6                 CMIP6Plus
    query class mapping     inject it                    already registered
    facade                  build it                     build it
    selector                build it                     build it
    naming table            write a new one              reuse CMIP6's, unchanged
    not applicable column values  processing_id, grid_label     none
    `other_terms` needed    for domain and driving model  none at all

Put another way, this module shows the irreducible cost of a project not being registered.

**Two of these tests need no network**, and carry no marker, so they run in the normal
suite: they pin the registration gap itself, which is ours and not the archive's, and
they are the tripwire for someone registering CMIP6Plus properly. The two which do
search are marked, and say so.

One thing this turned up and which the gap below is worth reading against: CMIP6Plus is
not short of data. NCI's Solr index held 177,090 CMIP6Plus datasets when this was
written, and the ESGF-NG mirrors 24,834, so this is a project esmporium cannot reach
rather than one with nothing to reach for.
"""

from __future__ import annotations

import httpx
import pytest
from sqlmodel import Session, create_engine, select

from esmporium.db import (
    DatabaseCatalogue,
    Dataset,
    build_result_processor_factory,
    configure_sqlite_for_concurrency,
    record_search_api_calls,
)
from esmporium.db.migrate import upgrade_to_head
from esmporium.query import (
    PROJECT_QUERY_MAP_DEFAULT,
    Query,
    QueryCMIP6,
    to_canonical,
    translate_canonical_to_projects,
)
from esmporium.requirements import leaf, requirement, solve
from esmporium.search import (
    DEFAULT_SELECTOR,
    ESGF1_CMIP6_FACADE_PARAMETERS,
    ESGFNG_CMIP6_FACADE_PARAMETERS,
    AllSubQueriesFailedError,
    ProjectPrefixMismatchError,
    SearchAPIESGF1Solr,
    SearchAPIFacade,
    SolrSingleRowResultParser,
    build_default_retrying,
    build_project_list_selector,
    search,
)

TIMEOUT = 60.0
"""How long to wait for a node, in seconds"""

LIMIT = 50
"""Records per page, comfortably more than the slice below matches"""

PROJECT = "CMIP6Plus"

HOST = "esgf.nci.org.au"
"""
An ESGF1 Solr node which indexes CMIP6Plus

Solr rather than STAC on purpose: `test_the_cmip6_stac_table_cannot_be_reused` shows
why the STAC table cannot be borrowed the way this one can.
"""

CMIP6PLUS_FACADE = SearchAPIFacade(
    # The whole point of this module. 
    # Nothing specific to CMIP6Plus, we just use CMIP6 pieces as they are.
    # If CMIP6Plus ever needs pieces of its own, this line is where that shows up.
    parameters=ESGF1_CMIP6_FACADE_PARAMETERS,
    search_api=SearchAPIESGF1Solr(host=HOST, retrying=build_default_retrying()),
    result_parser=SolrSingleRowResultParser(),
)
"""
The facade this module searches through

Two of the three injections a caller has to make. The third -- a project-to-query
mapping -- is not needed, because CMIP6Plus is already in
`PROJECT_QUERY_MAP_DEFAULT`; `search` below is called without one, which is the
difference from the CORDEX module in one line.
"""

SELECTOR = build_project_list_selector({PROJECT: (CMIP6PLUS_FACADE,)})
"""Facade selection for CMIP6Plus, which `DEFAULT_SELECTOR` has none of"""

LEAF_QUERY = Query(
    project=PROJECT,
    # Every facet modelled, and `other_terms` empty. There is nothing CMIP6Plus
    # records that esmporium does not already have a canonical name for, so the `other_terms` escape
    # hatch cannot be tested here. We test it with the CORDEX integration tests instead,
    # see [add cross-ref].
    variable="tas",
    reporting_interval="mon",
    experiment="esm-hist",
)
"""
The query every search here works from

Tiny on purpose: this query gave four datasets when we searched: one model, four variant labels, so grouping by model
and variant gives one dataset per group.
`esm-hist` because CMIP6Plus runs its own experiments and has no `historical`.
"""

EXPECTED_ROWS = 4
"""
How many datasets the query matched when this was written

Should be used as a floor, not an equality: the archive grows, and a test which failed because more
CMIP6Plus data was published would be telling us nothing.
"""


def saved(engine) -> list[Dataset]:
    """Every dataset row in the database"""
    with Session(engine) as session:
        return list(session.exec(select(Dataset)).all())


@pytest.fixture(scope="module")
def searched(tmp_path_factory):
    """
    Search the query once, live, and hand back the database

    Module-scoped for the same reason as the CORDEX module's: every test reads the
    same rows, none writes, and one request is enough.
    """
    database_path = tmp_path_factory.mktemp("cmip6plus") / "esmporium.db"
    engine = configure_sqlite_for_concurrency(
        create_engine(f"sqlite:///{database_path}")
    )
    upgrade_to_head(engine)

    with httpx.Client(follow_redirects=True, timeout=TIMEOUT) as client:
        try:
            outcomes = search(
                requirement(
                    name="cmip6plus-tas-monthly-live",
                    tree=leaf(LEAF_QUERY, "tas"),
                    group_by=("model", "variant_label"),
                ),
                SELECTOR,
                limit=LIMIT,
                client=client,
                api_call_observer=record_search_api_calls(engine),
                processor_factory=build_result_processor_factory(engine),
                # Deliberately no `project_query_map`: CMIP6Plus is already
                # registered for translation, so the default mapping answers.
            )
        except AllSubQueriesFailedError:
            pytest.skip("no node answered, so they are down or unwell")

    if not all(outcome.answered for outcome in outcomes):
        pytest.skip("a node did not answer, so it is down or unwell")

    if not saved(engine):
        # A failure, not a skip: the node answered and answered empty, and the facets
        # above are all modelled ones, so the likeliest cause is that CMIP6Plus has
        # moved its experiment names. Worth being told either way.
        pytest.fail(
            f"{HOST} answered and returned no {PROJECT} dataset for monthly `tas` of "
            "`esm-hist`. Either the slice has gone (CMIP6Plus runs its own "
            "experiments and they change) or the reused CMIP6 naming table no longer "
            "builds the request we think it does.",
            pytrace=False,
        )

    return engine, outcomes


# --------------------------------------------------------------------------------
# The registration gap. No network: this is about our own wiring, so it runs in the
# normal suite.
# --------------------------------------------------------------------------------


def test_cmip6plus_is_registered_for_translation_but_not_for_searching():
    """
    CMIP6Plus passes the first gate and fails the last, which is why this module wires

    This pins a gap rather than a behaviour, and it is the tripwire for that gap being
    closed: if the selector stops raising, CMIP6Plus has been registered properly and
    the wiring at the top of this module can go.
    """
    canonical = to_canonical(Query(project=PROJECT, variable="tas"))

    # Gate 1, and the one CMIP6Plus passes: it is in the mapping, pointed at CMIP6's
    # query class, so a CMIP6Plus query translates with nothing injected.
    assert PROJECT in PROJECT_QUERY_MAP_DEFAULT
    translated = translate_canonical_to_projects(canonical)
    assert [type(query) for query in translated.values()] == [QueryCMIP6]

    # Gate 3, which it fails: the default selector is built over a hard-coded
    # ("CMIP5", "CMIP6", "CMIP7"), so the lookup is a plain `KeyError` carrying none of
    # the guidance the other gates' errors carry.
    with pytest.raises(KeyError):
        DEFAULT_SELECTOR(canonical, 0)


def test_the_cmip6_stac_table_cannot_be_reused_for_cmip6plus():
    """
    The Solr table is reusable for CMIP6Plus and the STAC one is not

    Worth pinning because it is the one place the "CMIP6Plus is just CMIP6" story
    breaks, and the reason is not the facet names -- those are identical -- but the
    prefix, which a STAC facade checks against the project's own name.
    """
    canonical = to_canonical(Query(project=PROJECT, variable="tas"))

    # Reusable: no prefixes in a Solr request, so identical facet names are enough.
    assert ESGF1_CMIP6_FACADE_PARAMETERS.get_search_request_facet_values(canonical)

    # Not reusable: `cmip6` against an expected `cmip6plus`. A caller who wants the
    # STAC mirrors needs their own `STACFacadeParameters` around the same query style,
    # or a `project_to_prefix_converter` override.
    with pytest.raises(ProjectPrefixMismatchError):
        ESGFNG_CMIP6_FACADE_PARAMETERS.get_search_request_facet_values(canonical)


# --------------------------------------------------------------------------------
# The live half.
# --------------------------------------------------------------------------------


@pytest.mark.hits_esgf_search_api
def test_cmip6plus_needs_no_naming_table_of_its_own(searched):
    """
    CMIP6's naming table reads a live CMIP6Plus document with nothing invented

    The direct contrast with `test_our_cordex_naming_table_still_reads_a_live_document`.
    There, two of the nine columns had to be decided rather than read, and `grid_label`
    came back `None` because CORDEX has no grid concept. Here every column is read off
    the document, `grid_label` among them, by a table nobody wrote for this project.
    """
    engine, _ = searched
    rows = saved(engine)

    assert CMIP6PLUS_FACADE.parameters is ESGF1_CMIP6_FACADE_PARAMETERS, (
        "this test only means something while the table is CMIP6's own, reused"
    )
    assert len(rows) >= EXPECTED_ROWS

    # Every facet column, including the two CORDEX could not answer for. Nothing here
    # is `None` and nothing was decided by us.
    for row in rows:
        assert row.project == PROJECT
        for column in (
            "model",
            "institution",
            "experiment",
            "variant_label",
            "variable",
            "reporting_interval",
            "processing_id",
            "grid_label",
        ):
            assert getattr(row, column), (
                f"{column!r} came back empty for {row.id_project_specific!r}. "
                "CMIP6Plus documents are CMIP6 documents, so every column should be "
                "readable: an empty one means that is no longer true."
            )


@pytest.mark.hits_esgf_search_api
def test_a_cmip6plus_requirement_is_searched_saved_and_solved(searched):
    """
    The whole chain for a registered-but-unsearchable project, on modelled facets alone

    The mirror of the CORDEX module's solve. That one could only group through
    `other_terms`, because nothing it wanted to group by had a column. This one needs
    the escape hatch nowhere: `model` and `variant_label` are columns, the leaf is all
    canonical facets, and `other_terms` is empty from end to end.
    """
    engine, outcomes = searched

    assert [(outcome.roles, outcome.project) for outcome in outcomes] == [
        (("tas",), PROJECT)
    ]

    req = requirement(
        name="cmip6plus-tas-monthly-solve-live",
        tree=leaf(LEAF_QUERY, "tas"),
        group_by=("model", "variant_label"),
    )

    # The comparison, asserted rather than left to the docstring: this requirement
    # reaches the solver without the escape hatch anywhere in it.
    assert not LEAF_QUERY.other_terms
    assert not req.group_by_other_terms
    assert not req.prefer_other_terms

    result = solve(req, DatabaseCatalogue(engine))

    discovered = set(result.satisfied) | set(result.unsatisfied) | set(result.ambiguous)
    assert discovered, "the search saved rows but the solver discovered no groups"
    assert len(discovered) == (
        len(result.satisfied) + len(result.unsatisfied) + len(result.ambiguous)
    )

    # One dataset per model and variant in this slice, so every group is satisfied and
    # the group keys are plain column values -- no `None` anywhere, which is what an
    # unanswerable facet would have left.
    assert result.satisfied, f"no group was satisfied: {result.explain()}"
    for key in result.satisfied:
        assert [name for name, _ in key] == ["model", "variant_label"]
        assert all(value is not None for _, value in key)

    assert result.explain()
