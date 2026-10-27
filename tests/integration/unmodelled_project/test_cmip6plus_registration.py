"""
What a project costs before esmporium can search it, with CMIP6Plus as the cheap case

The companion to `test_cordex_other_terms_live.py`, and a deliberate comparison. Both
projects are ones esmporium cannot search as it stands, and what each one costs to
search anyway is the thing worth seeing side by side, because it separates two
questions which are easy to run together:

- *is this project registered?* -- which decides what a caller has to inject, and
- *does this project fit our dataset columns?* -- which decides whether the facade they
  build needs a query style of its own.

Neither project is registered, so both answer no to the first and the injections are
the same three. They differ on the second: a CMIP6Plus document is the same shape as a
CMIP6 document, so every piece CMIP6 already has is reusable, while CORDEX needs its
own. Hence:

                                CORDEX-CMIP6                  CMIP6Plus
    query class mapping         inject it                     inject it
    facade                      build it                      reuse CMIP6's, unchanged
    selector                    build it                      build it
    query style                 write a new one               reuse CMIP6's, unchanged
    columns with no equivalent  processing_id, grid_label      none
    `other_terms` needed        for domain and driving model  none at all

Put another way, this module shows the irreducible cost of a project not being
registered: three injections, and nothing else to write when the documents are a
shape we already read.

**Three of these tests need no network**, and carry no marker, so they run in the
normal suite: they pin the registration gap itself, which is ours and not the
archive's, and they are the tripwire for someone registering CMIP6Plus properly. The
three which do search are marked, and say so.

One thing this turned up and which the gap below is worth reading against: CMIP6Plus is
not short of data. NCI's Solr index held 177,090 CMIP6Plus datasets when this was
written, and the ESGF-NG mirrors 24,834, so this is a project esmporium cannot reach
rather than one with nothing to reach for. The two do not hold the *same* data: this
module's query answered by NCI and by ESGF-NG east returns four datasets each and they
have nothing in common (`EC-Earth3-ESM-1` against `IPSL-CM6-ESMCO2`). That is mirror
coverage, upstream of us, which is why nothing here asserts the two agree.
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
    UnknownProjectError,
    to_canonical,
    translate_canonical_to_projects,
)
from esmporium.requirements import leaf, requirement, solve
from esmporium.search import (
    DEFAULT_SELECTOR,
    ESGF1_CMIP6_FACADE_PARAMETERS,
    ESGFNG_CMIP6_FACADE_PARAMETERS,
    INBUILT_SEARCH_API_FACADE_STORE,
    AllSubQueriesFailedError,
    ESGFNGCMIP6ParametersQueryStyle,
    ESGFNGResultParser,
    ProjectPrefixMismatchError,
    SearchAPIESGFNGSTAC,
    SearchAPIFacade,
    STACFacadeParameters,
    build_default_retrying,
    build_project_list_selector,
    search,
)

TIMEOUT = 60.0
"""How long to wait for a node, in seconds"""

LIMIT = 50
"""Records per page, comfortably more than the query below matches"""

PROJECT = "CMIP6Plus"

STAC_PREFIX = "cmip6plus"
"""
The facet name prefix CMIP6Plus documents carry on the ESGF-NG APIs

`str.lower` of the project, which is why the `STACFacadeParameters` below needs
neither of its converters overridden.
"""

STAC_HOST = "search.east.esgf.io"
"""The ESGF-NG deployment this module searches for the STAC half"""

PROJECT_QUERY_MAP = {PROJECT: QueryCMIP6}
"""
The query class CMIP6Plus facets are expressed in: CMIP6's own

The first of the three injections, and the one the CORDEX module cannot make this
cheaply: CMIP6Plus documents are CMIP6 documents, so CMIP6's query class names every
facet one of them records and nothing project-specific has to be written.
"""

HOST = "esgf.nci.org.au"
"""
An ESGF1 Solr node which indexes CMIP6Plus

Solr rather than STAC for the main chain below, because a Solr facade needs no prefix
and so reuses CMIP6's pieces with nothing changed at all.
`test_the_cmip6_stac_facade_parameters_cannot_be_reused_for_cmip6plus` and
`test_a_cmip6plus_stac_facade_needs_only_a_new_prefix` are the STAC story: one line
more, and that line is a prefix.
"""

CMIP6PLUS_FACADE = INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
    "CMIP6", HOST
)
"""
The facade this module searches through: CMIP6's own, asked for by name

The second injection, and the whole point of this module in one line. There is nothing
to build, because a facade is a query style plus an API plus a result parser and
CMIP6's three serve CMIP6Plus unchanged -- so the store is asked for CMIP6's facade on
this host and a CMIP6Plus query is pointed at it. If CMIP6Plus ever needs pieces of its
own, this line is where that shows up.

Asking the store rather than assembling the pieces also couples this to the store's
host list on purpose: drop `esgf.nci.org.au` from CMIP6's hosts and this raises,
rather than quietly searching somewhere else.
"""

SELECTOR = build_project_list_selector({PROJECT: (CMIP6PLUS_FACADE,)})
"""The third injection: facade selection, which `DEFAULT_SELECTOR` has none of"""

LEAF_QUERY = Query(
    project=PROJECT,
    # Every facet modelled, and `other_terms` empty. There is nothing CMIP6Plus
    # records that esmporium does not already have a canonical name for, so the
    # `other_terms` escape hatch cannot be tested here. We test it with the CORDEX
    # integration tests instead, see `test_cordex_other_terms_live.py`.
    variable="tas",
    reporting_interval="mon",
    experiment="esm-hist",
)
"""
The query every search here works from

Tiny on purpose: this query gave four datasets when we searched: one model, four
variant labels, so grouping by model and variant gives one dataset per group.
`esm-hist` because CMIP6Plus runs its own experiments and has no `historical`.
"""

REQUIREMENT = requirement(
    name="cmip6plus-tas-monthly-live",
    tree=leaf(LEAF_QUERY, "tas"),
    group_by=("model", "variant_label"),
)
"""
The one requirement this module searches and solves

One object for both, deliberately: a requirement searched and then solved from the
same value is the chain this module is about, and two requirements differing only in
their name would leave open whether the solve was answering the search.
"""

EXPECTED_ROWS = 4
"""
How many datasets the query matched when this was written

Should be used as a floor, not an equality: the archive grows, and a test which
failed because more CMIP6Plus data was published would be telling us nothing.
"""

EXPECTED_ROWS_STAC = 4
"""
The same floor for the ESGF-NG mirror, which holds a different four datasets

Kept apart from `EXPECTED_ROWS` rather than shared, because the two agreeing on a
count is a coincidence: the mirrors hold different CMIP6Plus data (see the module
docstring), so one number standing for both would read as a claim we are not making.
"""

FACET_COLUMNS = (
    "model",
    "institution",
    "experiment",
    "variant_label",
    "variable",
    "reporting_interval",
    "processing_id",
    "grid_label",
)
"""
Every facet column a stored CMIP6Plus row should answer for

All of them, including the two CORDEX cannot answer for. Named rather than taken from
`DATASET_FACET_COLUMNS` for the same reason the CORDEX module names its own: this
should fail when a column is added, because that is the moment to decide whether
CMIP6Plus still needs nothing of its own.
"""


def saved(engine) -> list[Dataset]:
    """Every dataset row in the database"""
    with Session(engine) as session:
        return list(session.exec(select(Dataset)).all())


def migrated(database_path) -> object:
    """Build and migrate a database at `database_path`"""
    engine = configure_sqlite_for_concurrency(
        create_engine(f"sqlite:///{database_path}")
    )
    upgrade_to_head(engine)

    return engine


def search_live(facade, engine):
    """
    Search `REQUIREMENT` once through `facade`, or skip if no node answered

    Parameters
    ----------
    facade
        Facade to search through, which is what the two live routes vary

    engine
        Database to save into, already migrated

    Returns
    -------
    :
        The search outcomes
    """
    with httpx.Client(follow_redirects=True, timeout=TIMEOUT) as client:
        try:
            outcomes = search(
                REQUIREMENT,
                build_project_list_selector({PROJECT: (facade,)}),
                limit=LIMIT,
                client=client,
                api_call_observer=record_search_api_calls(engine),
                processor_factory=build_result_processor_factory(engine),
                # The first injection, in use. CMIP6Plus is not in
                # `PROJECT_QUERY_MAP_DEFAULT`, so without this the query does not
                # translate at all -- which is what
                # `test_cmip6plus_is_registered_nowhere` pins.
                project_query_map=PROJECT_QUERY_MAP,
            )
        except AllSubQueriesFailedError:
            pytest.skip("no node answered, so they are down or unwell")

    if not all(outcome.answered for outcome in outcomes):
        pytest.skip("a node did not answer, so it is down or unwell")

    return outcomes


def check_every_facet_column_was_read(rows) -> None:
    """
    Check each row in `rows` answers for every facet column, with nothing invented
    """
    for row in rows:
        assert row.project == PROJECT
        for column in FACET_COLUMNS:
            assert getattr(row, column), (
                f"{column!r} came back empty for {row.id_project_specific!r}. "
                "CMIP6Plus documents are CMIP6 documents, so every column should be "
                "readable: an empty one means that is no longer true."
            )


def stac_facade() -> SearchAPIFacade:
    """
    Build the CMIP6Plus ESGF-NG facade: CMIP6's pieces, with a prefix of its own

    A function rather than a module-level constant because it is what two tests are
    *about* rather than something they search through, and one of them asserts what
    goes into it.
    """
    return SearchAPIFacade(
        parameters=STACFacadeParameters(
            base_query_style=ESGFNGCMIP6ParametersQueryStyle,
            prefix=STAC_PREFIX,
        ),
        search_api=SearchAPIESGFNGSTAC(
            host=STAC_HOST, retrying=build_default_retrying()
        ),
        result_parser=ESGFNGResultParser(),
    )


@pytest.fixture(scope="module")
def searched(tmp_path_factory):
    """
    Search the query once, live, and hand back the database

    Module-scoped for the same reason as the CORDEX module's: every test reads the
    same rows, none writes, and one request is enough.
    """
    engine = migrated(tmp_path_factory.mktemp("cmip6plus") / "esmporium.db")

    outcomes = search_live(CMIP6PLUS_FACADE, engine)

    if not saved(engine):
        # A failure, not a skip: the node answered and answered empty, and the facets
        # above are all modelled ones, so the likeliest cause is that CMIP6Plus has
        # moved its experiment names. Worth being told either way.
        pytest.fail(
            f"{HOST} answered and returned no {PROJECT} dataset for monthly `tas` of "
            "`esm-hist`. Either the archive no longer holds matching datasets "
            "(CMIP6Plus runs its own experiments and they change) or the reused "
            "CMIP6 facade parameters no longer build the request we think they do.",
            pytrace=False,
        )

    return engine, outcomes


# --------------------------------------------------------------------------------
# The registration gap. No network: this is about our own wiring, so it runs in the
# normal suite.
# --------------------------------------------------------------------------------


def test_cmip6plus_is_registered_nowhere():
    """
    CMIP6Plus fails both gates, which is why this module injects all three pieces

    This pins a gap rather than a behaviour, and it is the tripwire for that gap being
    closed: if either gate stops raising, CMIP6Plus has been registered and the wiring
    at the top of this module can go.

    The two gates are deliberately consistent, and that is worth knowing because they
    were not. CMIP6Plus used to be in `PROJECT_QUERY_MAP_DEFAULT` and absent from
    `DEFAULT_SELECTOR`, so a CMIP6Plus query translated and then had nowhere to be
    searched -- half a project's support, which reads as a whole one. It was dropped
    from the mapping for that reason, and this test keeps the two in step.
    """
    # Gate 1: no query class, so a CMIP6Plus query does not translate on its own. The
    # error names the projects we do know, which is the actionable part.
    assert PROJECT not in PROJECT_QUERY_MAP_DEFAULT
    canonical = to_canonical(Query(project=PROJECT, variable="tas"))
    with pytest.raises(UnknownProjectError, match="CMIP5, CMIP6, CMIP7"):
        translate_canonical_to_projects(canonical)

    # Gate 2: no facade either, so the default selector cannot answer. The lookup is a
    # plain `KeyError`, carrying none of the guidance gate 1's error carries.
    with pytest.raises(KeyError):
        DEFAULT_SELECTOR(canonical, 0)

    # And what closing gate 1 costs, which is the cheapest part of this module: name
    # CMIP6's query class and a CMIP6Plus query translates into it.
    translated = translate_canonical_to_projects(
        canonical, project_query_map=PROJECT_QUERY_MAP
    )
    assert [type(query) for query in translated.values()] == [QueryCMIP6]


def test_the_cmip6_stac_facade_parameters_cannot_be_reused_for_cmip6plus():
    """
    The Solr facade parameters are reusable for CMIP6Plus and the STAC ones are not

    Worth pinning because it is the one place the "CMIP6Plus is just CMIP6" story
    breaks, and the reason is not the facet names -- those are identical -- but the
    prefix, which a STAC facade checks against the project's own name.
    `test_a_cmip6plus_stac_facade_needs_only_a_new_prefix` is what it costs to fix.
    """
    canonical = to_canonical(Query(project=PROJECT, variable="tas"))

    # Reusable: no prefixes in a Solr request, so identical facet names are enough.
    assert ESGF1_CMIP6_FACADE_PARAMETERS.get_search_request_facet_values(canonical)

    # Not reusable: `cmip6` against an expected `cmip6plus`.
    with pytest.raises(ProjectPrefixMismatchError):
        ESGFNG_CMIP6_FACADE_PARAMETERS.get_search_request_facet_values(canonical)


def test_a_cmip6plus_stac_facade_needs_only_a_new_prefix():
    """
    STAC support for CMIP6Plus is one more `STACFacadeParameters`, and nothing else

    The other half of the test above, and why the prefix mismatch is a cheap failure
    rather than an expensive one: the query style CMIP6 already has is what reads a
    CMIP6Plus document, so the only thing to change is the name the API wants on the
    front of each facet. Both converters keep their defaults, which is not luck --
    `str.lower` turns `CMIP6Plus` into exactly the prefix its documents use, and the
    collection is wanted at the project's own spelling, which is what east requires.
    """
    parameters = stac_facade().parameters

    # Reused by identity, so this says what it means: no CMIP6Plus query style was
    # written, CMIP6's own is doing the work.
    assert (
        parameters.base_query_style is ESGFNG_CMIP6_FACADE_PARAMETERS.base_query_style
    )

    canonical = to_canonical(
        Query(project=PROJECT, variable="tas", reporting_interval="mon")
    )

    # The collection is the project at its own spelling, which east is case-sensitive
    # about, and the facets are the names CMIP6 sends under a different prefix.
    assert parameters.get_collection(canonical) == PROJECT
    assert parameters.get_search_request_facet_values(canonical) == {
        "collection": (PROJECT,),
        f"{STAC_PREFIX}:variable_id": ("tas",),
        f"{STAC_PREFIX}:frequency": ("mon",),
    }


# --------------------------------------------------------------------------------
# The live half.
# --------------------------------------------------------------------------------


@pytest.mark.hits_esgf_search_api
def test_cmip6plus_needs_no_query_style_of_its_own(searched):
    """
    CMIP6's query style reads a live CMIP6Plus document with nothing invented

    The direct contrast with `test_our_cordex_query_style_still_reads_a_live_document`.
    There, two of the nine columns had to be decided rather than read, and `grid_label`
    came back `None` because CORDEX has no grid concept. Here every column is read off
    the document, `grid_label` among them, by a query style nobody wrote for this
    project.
    """
    engine, _ = searched
    rows = saved(engine)

    assert CMIP6PLUS_FACADE.parameters is ESGF1_CMIP6_FACADE_PARAMETERS, (
        "this test only means something while the facade parameters are CMIP6's "
        "own, reused"
    )
    assert len(rows) >= EXPECTED_ROWS

    check_every_facet_column_was_read(rows)


@pytest.mark.hits_esgf_search_api
def test_the_stac_route_finds_cmip6plus_data_live(tmp_path):
    """
    The one-prefix STAC facade really does search CMIP6Plus, on a live deployment

    `test_a_cmip6plus_stac_facade_needs_only_a_new_prefix` pins the request this
    builds; this pins that a deployment answers it with CMIP6Plus datasets. Worth
    asserting live because the failure it catches is a *successful empty answer*:
    these APIs report a prefix or a collection name they do not recognise as a search
    which matched nothing, not as an error, so nothing short of asking would notice.

    A second live request, and the reason it earns one: reusing CMIP6's query style is
    this module's claim, and a STAC document is a different shape from a Solr one, so
    the claim is only tested for both API families by searching both.

    Deliberately not compared against the Solr rows -- the two deployments hold
    different CMIP6Plus data, which is upstream of us (see the module docstring).
    """
    engine = migrated(tmp_path / "esmporium.db")

    search_live(stac_facade(), engine)
    rows = saved(engine)

    if not rows:
        pytest.fail(
            f"{STAC_HOST} answered and returned no {PROJECT} dataset for monthly "
            "`tas` of `esm-hist`. Either the deployment no longer holds matching "
            f"datasets or {STAC_PREFIX!r} is no longer the prefix its documents use, "
            "and a wrong prefix comes back as a successful empty answer rather than "
            "an error. Both are worth knowing, so this fails rather than skipping.",
            pytrace=False,
        )

    assert len(rows) >= EXPECTED_ROWS_STAC

    check_every_facet_column_was_read(rows)


@pytest.mark.hits_esgf_search_api
def test_a_cmip6plus_requirement_is_searched_saved_and_solved(searched):
    """
    The whole chain for an unregistered project, on modelled facets alone

    The mirror of the CORDEX module's solve. That one could only group through
    `other_terms`, because nothing it wanted to group by had a column. This one needs
    the escape hatch nowhere: `model` and `variant_label` are columns, the leaf is all
    canonical facets, and `other_terms` is empty from end to end.
    """
    engine, outcomes = searched

    assert [(outcome.roles, outcome.project) for outcome in outcomes] == [
        (("tas",), PROJECT)
    ]

    # The comparison, asserted rather than left to the docstring: this requirement
    # reaches the solver without the escape hatch anywhere in it.
    assert not LEAF_QUERY.other_terms
    assert not REQUIREMENT.group_by_other_terms
    assert not REQUIREMENT.prefer_other_terms

    # The injected mapping again, on the read side: without it the catalogue models
    # nothing for CMIP6Plus and every facet would be reachable only through
    # `other_terms`, which is the CORDEX module's situation and not this one.
    catalogue = DatabaseCatalogue(engine, project_query_map=PROJECT_QUERY_MAP)
    result = solve(REQUIREMENT, catalogue)

    discovered = set(result.satisfied) | set(result.unsatisfied) | set(result.ambiguous)
    assert discovered, "the search saved rows but the solver discovered no groups"
    assert len(discovered) == (
        len(result.satisfied) + len(result.unsatisfied) + len(result.ambiguous)
    )

    # One dataset per model and variant among the saved rows, so every group is
    # satisfied and the group keys are plain column values -- no `None` anywhere,
    # which is what an unanswerable facet would have left.
    assert result.satisfied, f"no group was satisfied: {result.explain()}"
    for key in result.satisfied:
        assert [name for name, _ in key] == ["model", "variant_label"]
        assert all(value is not None for _, value in key)

    assert result.explain()
