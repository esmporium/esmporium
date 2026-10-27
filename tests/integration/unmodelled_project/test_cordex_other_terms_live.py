"""
Testing CORDEX, an unmodelled project, run end to end against the live ESGF APIs

CORDEX is the test because it is a project that doesn't match our `Dataset` columns
and because it has facets no query class of ours names. There are **two ways to reach
such a facet**, and this module runs the same live data through both:

- `other_terms`, the escape hatch: nothing is translated or checked, and the facet is
  spelled exactly as a search document spells it (`cordex-cmip6:driving_source_id`).
  Nothing is registered for CORDEX, so a document models nothing
  (`CatalogueEntry.project_facets` is empty) and this is the only path available.
- a **query class of your own**, `QueryCORDEX` below: the facet is declared, so it is
  named without a prefix (`driving_source_id`), it is checked, and it arrives through
  `project_facets` like any facet we model.

The second is more convenient and more work, and which is right depends on whether a
project is worth writing a class for. The comparison is the point of this module, and
`test_both_routes_build_the_same_search_request` is the headline: the two differ only
in how the facet is *named*, going in and coming back out. The archive is asked
exactly the same question either way.

**Everything below is a test-local claim, not esmporium's position on how CORDEX is
modelled.** CORDEX is not a project esmporium supports: it is absent from
`PROJECT_QUERY_MAP_DEFAULT`, from `get_default_facade_definition` and from
`DEFAULT_SELECTOR`. Searching it at all means injecting a facade, a selector and a
project-to-query mapping, which is what a user would have to do today and what the
docstring of `PROJECT_QUERY_MAP_DEFAULT` points at ("to search a project we do not
know about yet"). So these tests also check, deliberately, that those injection seams
work as advertised -- and one of them does not reach far enough, which
`test_group_by_refuses_a_facet_only_our_own_query_class_models` pins.

The companion module is `test_cmip6plus_registration.py`, where a project needs no
query style of its own at all.
"""

from __future__ import annotations

from typing import Annotated

import httpx
import pytest
from pydantic import BaseModel, ConfigDict
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
    FacetValues,
    FacetValuesByName,
    Query,
    QueryFacet,
    SourceQuery,
    facet_spec,
    modelled_facet_names,
    to_canonical,
)
from esmporium.requirements import (
    UnmodelledFacetError,
    leaf,
    requirement,
    solve,
)
from esmporium.search import (
    AllSubQueriesFailedError,
    ESGFNGResultParser,
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
"""
Records per page

Comfortably more than the query below matches, so one page holds all of it.
"""

PROJECT = "CORDEX-CMIP6"
"""
The project CORDEX-CMIP6 data comes back under

Also the STAC collection name, and `str.lower` of it is the facet name prefix, which
is why `STACFacadeParameters` needs neither of its converters overridden here.
"""

PREFIX = PROJECT.lower()
"""
The prefix CORDEX-CMIP6 facets carry on the ESGF-NG APIs

Named rather than written twice, because the whole difference between this module's
two routes is which side of it a facet is spelled on.
"""

HOST = "search.east.esgf.io"
"""
The one ESGF-NG deployment with CORDEX-CMIP6 in quantity
"""


class ESGFNGCordexCMIP6ParametersQueryStyle(BaseModel):
    """CORDEX-CMIP6 facet values under their ESGF-NG parameter names"""

    model_config = ConfigDict(extra="forbid")

    # One declaration per `DatasetFacets` column, because that is what a row needs:
    # a column with no parameter name here is left unset and then fails
    # `DatasetFacets` validation, while a facet which is not a column (`activity`,
    # say) is never read by the parser at all. `project` is absent for the same
    # reason it is absent from the CMIP6 and CMIP7 STAC styles: a STAC parser reads
    # the project from the feature's `collection`.
    #
    # Plus the two facets which are no column at all, declared at the end so the
    # query-class route can express them. Declaring them costs the `other_terms`
    # route nothing: a `Query` cannot set them, so they stay empty and are dropped
    # before `merge_other_terms` adds them back under the document's own spelling.
    # That is why one query style serves both routes -- and why both routes build the
    # same request.
    source_id: Annotated[FacetValues, QueryFacet("model")] = ()
    """The regional model. See [Dataset.model][esmporium.db.schema.Dataset.model]."""

    institution_id: Annotated[FacetValues, QueryFacet("institution")] = ()
    """See [Dataset.institution][esmporium.db.schema.Dataset.institution]."""

    driving_experiment_id: Annotated[FacetValues, QueryFacet("experiment")] = ()
    """See [Dataset.experiment][esmporium.db.schema.Dataset.experiment]."""

    driving_variant_label: Annotated[FacetValues, QueryFacet("variant_label")] = ()
    """See [Dataset.variant_label][esmporium.db.schema.Dataset.variant_label]."""
    # This is an example of a mapping that might not be correct if you tried to
    # use this in practice.

    variable_id: Annotated[FacetValues, QueryFacet("variable")] = ()
    """See [Dataset.variable][esmporium.db.schema.Dataset.variable]."""

    frequency: Annotated[FacetValues, QueryFacet("reporting_interval")] = ()
    """See [Dataset.reporting_interval][esmporium.db.schema.Dataset.reporting_interval]."""  # noqa: E501

    # CORDEX-CMIP6 has no concept of CMOR table, and `processing_id` is required
    # non-null, so something has to answer for it. `version_realization` is the
    # nearest (another example of an imprecise mapping that you'd want to think
    # through more if you were using this for real).
    version_realization: Annotated[FacetValues, QueryFacet("processing_id")] = ()
    """See [Dataset.processing_id][esmporium.db.schema.Dataset.processing_id]."""

    # No CORDEX-CMIP6 document carries a `grid_label`, but
    # `DatasetFacets.grid_label` is required-with-no-default so that a parser has to
    # *state* the absence rather than forget the facet.
    grid_label: Annotated[FacetValues, QueryFacet("grid_label")] = ()
    """See [Dataset.grid_label][esmporium.db.schema.Dataset.grid_label]."""

    domain_id: Annotated[FacetValues, QueryFacet(None)] = ()
    """The CORDEX region. No `Dataset` column holds it, so it has no canonical name."""

    driving_source_id: Annotated[FacetValues, QueryFacet(None)] = ()
    """The driving global model, likewise no column and so no canonical name."""

    other_terms: FacetValuesByName = {}
    """See [Query.other_terms][esmporium.query.known_queries.Query.other_terms]."""

    source_query: SourceQuery = None
    """See [Query.source_query][esmporium.query.known_queries.Query.source_query]."""


class QueryCORDEX(BaseModel):
    """
    A query under CORDEX-CMIP6's own parameter names

    The alternative to `other_terms`, and the second half of this module. A facet
    declared here is a facet we model: it is named without the API's prefix, a typo in
    it is refused rather than silently matching nothing, and it comes back through
    [CatalogueEntry.project_facets][esmporium.requirements.CatalogueEntry.project_facets]
    rather than through the escape hatch.

    Deliberately a sibling of
    [QueryCMIP6][esmporium.query.QueryCMIP6] in shape rather than of the query style
    above: a query class is what a *user* writes queries in, and a query style is what
    a facade renders them with. The two happen to agree on every name here because
    CORDEX's own names are what its API uses, which is not true in general.
    """

    model_config = ConfigDict(extra="forbid")

    project: Annotated[FacetValues, QueryFacet("project")] = (PROJECT,)
    """See [Dataset.project][esmporium.db.schema.Dataset.project]."""

    source_id: Annotated[FacetValues, QueryFacet("model")] = ()
    """The regional model. See [Dataset.model][esmporium.db.schema.Dataset.model]."""

    institution_id: Annotated[FacetValues, QueryFacet("institution")] = ()
    """See [Dataset.institution][esmporium.db.schema.Dataset.institution]."""

    driving_experiment_id: Annotated[FacetValues, QueryFacet("experiment")] = ()
    """See [Dataset.experiment][esmporium.db.schema.Dataset.experiment]."""

    driving_variant_label: Annotated[FacetValues, QueryFacet("variant_label")] = ()
    """See [Dataset.variant_label][esmporium.db.schema.Dataset.variant_label]."""

    variable_id: Annotated[FacetValues, QueryFacet("variable")] = ()
    """See [Dataset.variable][esmporium.db.schema.Dataset.variable]."""

    frequency: Annotated[FacetValues, QueryFacet("reporting_interval")] = ()
    """See [Dataset.reporting_interval][esmporium.db.schema.Dataset.reporting_interval]."""  # noqa: E501

    version_realization: Annotated[FacetValues, QueryFacet("processing_id")] = ()
    """See [Dataset.processing_id][esmporium.db.schema.Dataset.processing_id]."""

    grid_label: Annotated[FacetValues, QueryFacet("grid_label")] = ()
    """See [Dataset.grid_label][esmporium.db.schema.Dataset.grid_label]."""

    domain_id: Annotated[FacetValues, QueryFacet(None)] = ()
    """
    The CORDEX region

    Project-specific, i.e. no canonical name, which is what makes it the interesting
    one: through `other_terms` this is reachable only as
    `cordex-cmip6:domain_id`, and declaring it here is what buys the plain spelling.
    """

    driving_source_id: Annotated[FacetValues, QueryFacet(None)] = ()
    """The driving global model, project-specific for the same reason"""

    other_terms: FacetValuesByName = {}
    """See [Query.other_terms][esmporium.query.known_queries.Query.other_terms]."""

    source_query: SourceQuery = None
    """See [Query.source_query][esmporium.query.known_queries.Query.source_query]."""


CORDEX_FACADE = SearchAPIFacade(
    parameters=STACFacadeParameters(
        base_query_style=ESGFNGCordexCMIP6ParametersQueryStyle,
        # `STACFacadeParameters` checks this against the query's project for us, via
        # its default `project_to_prefix_converter` of `str.lower`.
        prefix=PREFIX,
    ),
    search_api=SearchAPIESGFNGSTAC(host=HOST, retrying=build_default_retrying()),
    result_parser=ESGFNGResultParser(),
)
"""
The facade this module searches through
"""

SELECTOR = build_project_list_selector({PROJECT: (CORDEX_FACADE,)})
"""
Facade selection for CORDEX
"""

PROJECT_QUERY_MAP = {PROJECT: Query}
"""
The query class CORDEX facets are expressed in for the `other_terms` route

The third injected seam. `Query` does the job because the leaf below names only
canonical facets and `other_terms`, so on this route there is nothing a
CORDEX-specific query class would have to express.
"""

PROJECT_QUERY_MAP_CORDEX = {PROJECT: QueryCORDEX}
"""
The same seam for the query-class route

The one difference between the two routes, and the whole of it: the same facade and
the same selector serve both, because what changes is only which class names the
facets.
"""

DRIVING_SOURCE_ID = "cordex-cmip6:driving_source_id"
"""
The driving global model, spelled as a stored search document spells it

No `Dataset` column holds this and no query class names it, so it is reachable only
through `other_terms`.
"""

VERSION_REALIZATION = "cordex-cmip6:version_realization"
"""Which processing of a run this is, again as a document spells it"""

DOMAIN_ID = "cordex-cmip6:domain_id"
"""The CORDEX region, again as a document spells it"""

DOMAIN_ID_DECLARED = "domain_id"
"""
The same facet as `DOMAIN_ID`, spelled as `QueryCORDEX` declares it

`DOMAIN_ID` without the `cordex-cmip6:` prefix, and the two are the two routes in
miniature: the escape hatch has to use the API's spelling because nothing translates
it, while a declared facet is named without the API's prefix and the facade puts it
back on.
"""

DRIVING_SOURCE_ID_DECLARED = "driving_source_id"
"""The same facet as `DRIVING_SOURCE_ID`, spelled as `QueryCORDEX` declares it"""

LEAF_QUERY = Query(
    project=PROJECT,
    # Each facet in exactly one home. `variable` and `reporting_interval` are
    # modelled because the query style above can express them, so writing them in
    # `other_terms` would be using the escape hatch for something that is not
    # escaping. `other_terms` carries the two facets which have nowhere else to go.
    variable="tas",
    reporting_interval="mon",
    other_terms={
        DOMAIN_ID: ("EUR-12",),
        DRIVING_SOURCE_ID: ("ERA5",),
    },
)
"""
The one query every test here works from

Narrow on purpose, and narrow in a way that earns its keep: the ERA5-driven European
runs include two regional models published as both `v1-r1` and `v1-r2`, which is the
genuine tie `test_prefer_other_terms_breaks_a_tie` needs. Asking for ERA5 and then
grouping by driving model is also what a real analysis looks like.
"""

LEAF_QUERY_CORDEX = QueryCORDEX(
    # The same constraint as `LEAF_QUERY`, in CORDEX's own names. Every facet is
    # declared, so `other_terms` is empty: nothing here needs the escape hatch, which
    # is exactly what writing a query class buys.
    variable_id=("tas",),
    frequency=("mon",),
    domain_id=("EUR-12",),
    driving_source_id=("ERA5",),
)
"""
The same query as `LEAF_QUERY`, written in CORDEX's own parameter names

Note the prefixes are gone: `domain_id`, not `cordex-cmip6:domain_id`. The prefix is
the API's, and a query class is not where the API's names belong --
`test_both_routes_build_the_same_search_request` is what shows the prefix is still
put back on before the request goes out.
"""

EXPECTED_ROWS = 6
"""
How many datasets the query matched when this test was written

Should be used as a floor rather than an equality: the archive grows, and a test which
fails because more CORDEX data was published would be telling us nothing.
"""


def a_requirement(**kwargs):
    """
    Build a requirement around `LEAF_QUERY` above, overriding whatever is passed

    `query` overrides the leaf, which is how the query-class route reuses this: the
    two leaves say the same thing in different names, so everything else can stay.
    """
    return requirement(
        name=kwargs.pop("name", "cordex-tas-monthly-live"),
        tree=leaf(kwargs.pop("query", LEAF_QUERY), "tas"),
        group_by=kwargs.pop("group_by", ("model",)),
        **kwargs,
    )


def describe(query) -> str:
    """Name a query's facets in a message, `other_terms` included"""
    facets = {
        "variable": query.variable,
        "reporting_interval": query.reporting_interval,
    }

    return ", ".join(
        f"{name}={values}"
        for name, values in (*facets.items(), *query.other_terms.items())
    )


def saved(engine) -> list[Dataset]:
    """Every dataset row in the database"""
    with Session(engine) as session:
        return list(session.exec(select(Dataset)).all())


@pytest.fixture(scope="module")
def searched(tmp_path_factory):
    """
    Search the query once, live, and hand back the database and the outcomes

    Module-scoped because every test below reads the same rows and none of them
    writes, so one search serves all of them. That also keeps this module to a single
    request against somebody else's server.
    """
    database_path = tmp_path_factory.mktemp("cordex") / "esmporium.db"
    engine = configure_sqlite_for_concurrency(
        create_engine(f"sqlite:///{database_path}")
    )
    upgrade_to_head(engine)

    with httpx.Client(follow_redirects=True, timeout=TIMEOUT) as client:
        try:
            outcomes = search(
                a_requirement(),
                SELECTOR,
                limit=LIMIT,
                client=client,
                api_call_observer=record_search_api_calls(engine),
                processor_factory=build_result_processor_factory(engine),
                project_query_map=PROJECT_QUERY_MAP,
            )
        except AllSubQueriesFailedError:
            pytest.skip("no node answered, so they are down or unwell")

    # A node which could not be reached says nothing about the behaviour under test.
    # A node which answers and returns *nothing* is different: these APIs report a
    # mis-spelled facet name or value as a successful empty result, so that is a
    # finding, and it is asserted on below rather than skipped over.
    if not all(outcome.answered for outcome in outcomes):
        pytest.skip("a node did not answer, so it is down or unwell")

    if not saved(engine):
        pytest.fail(
            f"{HOST} answered and returned no CORDEX-CMIP6 dataset for "
            f"{describe(LEAF_QUERY)}. Either an `other_terms` key is wrong (they are "
            "never translated or checked, and these APIs answer a mis-spelled facet "
            "name with an empty result rather than an error) or the archive no "
            "longer holds matching datasets. Both are worth knowing, so this "
            "fails rather than skipping.",
            pytrace=False,
        )

    return engine, outcomes


@pytest.fixture(scope="module")
def catalogue(searched):
    """
    The catalogue the solver reads, with nothing overridden

    CORDEX is absent from the default project-to-query mapping, so the catalogue
    models nothing for it and everything a CORDEX document records is reachable only
    through `other_terms`. That is the point of using CORDEX, and
    `test_our_cordex_query_style_still_reads_a_live_document` asserts it.
    """
    engine, _ = searched

    return DatabaseCatalogue(engine)


@pytest.fixture(scope="module")
def catalogue_cordex(searched):
    """
    The same rows, read through `QueryCORDEX` instead

    The one difference from `catalogue` above, and the whole of the query-class route
    on the read side: told which class names CORDEX's facets, the catalogue models
    them, so they arrive in `project_facets` under their own unprefixed names instead
    of being reachable only through `other_terms`.
    """
    engine, _ = searched

    return DatabaseCatalogue(engine, project_query_map=PROJECT_QUERY_MAP_CORDEX)


@pytest.mark.hits_esgf_search_api
def test_our_cordex_query_style_still_reads_a_live_document(searched, catalogue):
    """
    Every column a stored CORDEX row needs was read from a live document

    This is here to keep two failure modes apart -- the query style at the top of
    this module is ours, the archive is not -- and it comes first for that reason.
    """
    engine, _ = searched
    rows = saved(engine)
    blame = (
        "Our CORDEX query style disagrees with the archive. That query style is "
        "this module's own (CORDEX is not a project esmporium supports), so look "
        "there first and not at the `other_terms` path."
    )

    assert len(rows) >= EXPECTED_ROWS, (
        f"{blame} Expected at least {EXPECTED_ROWS} rows, got {len(rows)}."
    )

    # Named rather than taken from `DATASET_FACET_COLUMNS` so that this test fails
    # when a column is added, which is the moment to decide what CORDEX calls it.
    required = (
        "model",
        "institution",
        "experiment",
        "variant_label",
        "variable",
        "reporting_interval",
        "processing_id",
    )
    for row in rows:
        assert row.project == PROJECT, f"{blame} Got project {row.project!r}."
        for column in required:
            assert getattr(row, column), (
                f"{blame} {column!r} came back empty for {row.id_project_specific!r}."
            )
        # The one column allowed to be null, and it has to be *stated* as null
        assert row.grid_label is None, (
            f"{blame} CORDEX-CMIP6 has no grid label, yet {row.grid_label!r} was "
            f"stored for {row.id_project_specific!r}."
        )

    # One stored row per dataset: nothing was duplicated or folded together on the
    # way in. For CORDEX-CMIP6 the project-specific id is the document's `title`,
    # which carries the version realization, so the two versions of a run are two
    # distinct ids here rather than relying on `processing_id` to separate them.
    assert len({row.id_project_specific for row in rows}) == len(rows), (
        f"{blame} Two rows share a project-specific id."
    )

    # The read side of "CORDEX is a project we model nothing for": with no CORDEX
    # query class, no document facet is canonical, so `project_facets` stays empty and
    # everything a document records is reachable only through `other_terms`. This is
    # what makes the rest of this module a test of the escape hatch and nothing else.
    entries = catalogue.find(LEAF_QUERY)
    assert entries, f"{blame} The rows are saved but the catalogue cannot find them."
    assert all(entry.project_facets == {} for entry in entries), (
        "CORDEX is not in the catalogue's project-to-query mapping, so it should "
        "model nothing and every document facet should be reachable only through "
        "`other_terms`. Something now models CORDEX facets: "
        f"{[dict(entry.project_facets) for entry in entries if entry.project_facets]}"
    )


@pytest.mark.hits_esgf_search_api
def test_other_terms_constrain_a_live_search_and_reach_the_catalogue(
    searched, catalogue
):
    """
    `other_terms` filter a live search, and come back keyed as the documents key them
    """
    _, outcomes = searched

    assert [(outcome.roles, outcome.project) for outcome in outcomes] == [
        (("tas",), PROJECT)
    ]

    entries = catalogue.find(LEAF_QUERY)

    # Keyed exactly as a document keys it, prefix included, because nothing
    # translated it on the way in or on the way out.
    assert all(isinstance(entry.facet(DRIVING_SOURCE_ID), str) for entry in entries), (
        f"every entry should record {DRIVING_SOURCE_ID!r}, got "
        f"{[entry.facet(DRIVING_SOURCE_ID) for entry in entries]}"
    )

    # `find` honoured both legs of the query at once: `variable` and
    # `reporting_interval` are columns and became SQL, while the two `other_terms`
    # were matched in memory against each entry's own keys. A leaf written this way
    # exercises both, and a failure here means one of the two legs was dropped.
    assert all(entry.variable == "tas" for entry in entries)
    assert all(entry.reporting_interval == "mon" for entry in entries)
    assert all(entry.facet(DOMAIN_ID) == "EUR-12" for entry in entries)
    assert all(entry.facet(DRIVING_SOURCE_ID) == "ERA5" for entry in entries)


@pytest.mark.hits_esgf_search_api
def test_an_other_terms_facet_reaches_the_solver_group_key(searched, catalogue):
    """
    A facet only a search document knows defines a group
    """
    req = a_requirement(
        group_by=("model",),
        group_by_other_terms=(DRIVING_SOURCE_ID,),
    )

    result = solve(req, catalogue)

    discovered = set(result.satisfied) | set(result.unsatisfied) | set(result.ambiguous)
    assert discovered, "rows were saved but the solver discovered no groups"

    # Every group lands in exactly one of the three, so none is double-counted.
    assert len(discovered) == (
        len(result.satisfied) + len(result.unsatisfied) + len(result.ambiguous)
    )

    for key in discovered:
        assert [name for name, _ in key] == ["model", DRIVING_SOURCE_ID], (
            f"group key {key} does not have the shape `group_by` then "
            "`group_by_other_terms` asked for"
        )
        driving_model = dict(key)[DRIVING_SOURCE_ID]
        # The whole point. `None` here is what an unanswered escape-hatch facet
        # looks like, and the typo test below asserts that deliberately -- so seeing
        # it here would mean the value never made it out of the stored document.
        assert driving_model == "ERA5", (
            f"{DRIVING_SOURCE_ID!r} reached the group key as {driving_model!r}. "
            "`None` means the solver could not read it back out of a stored search "
            "document, which is the failure this test exists to catch."
        )

    assert result.explain()


@pytest.mark.hits_esgf_search_api
def test_an_other_terms_facet_nothing_records_groups_as_none(catalogue):
    """
    A `group_by_other_terms` name nothing records is one group, not an error

    The negative control, and the other half of the bargain: `group_by_other_terms` is
    never checked, so a name no document carries -- a typo, here -- cannot be refused
    the way the same mistake in `group_by` is. Every dataset answers `None` for it and
    they all land in one group. Asserting it live matters because it is what the
    `None` in the test above would have meant.
    """
    typo = "cordex-cmip6:drivng_source_id"
    req = a_requirement(
        name="cordex-tas-monthly-typo-live",
        group_by=("model",),
        group_by_other_terms=(typo,),
    )

    result = solve(req, catalogue)

    discovered = set(result.satisfied) | set(result.unsatisfied) | set(result.ambiguous)
    assert discovered, "rows were saved but the solver discovered no groups"

    answers = {dict(key)[typo] for key in discovered}
    assert answers == {None}, (
        "a mis-spelled `group_by_other_terms` name should answer `None` for every "
        f"dataset, got {answers}"
    )


@pytest.mark.hits_esgf_search_api
def test_prefer_other_terms_breaks_a_tie(catalogue):
    """
    A tie between candidates is broken on a facet only a search document knows

    Solved twice over the same rows, once with no preferences and once preferring one
    version realization. The proof is the difference: if `prefer_other_terms` were
    ignored -- the failure this exists to catch, since nothing checks these names --
    the ambiguous groups would be identical both times.
    """
    preferred = "v1-r1"
    group_by_other_terms = (DRIVING_SOURCE_ID,)

    without = solve(
        a_requirement(
            name="cordex-tas-monthly-no-prefer-live",
            group_by=("model",),
            group_by_other_terms=group_by_other_terms,
        ),
        catalogue,
    )

    if not without.ambiguous:
        # Not a finding, unlike an empty search: whether two regional models publish
        # more than one version realization of the same run is a property of the
        # archive, not of our code. With nothing to disambiguate, there is no
        # tie-break to exercise today.
        pytest.skip(
            "no group had several candidates, so there is no tie for "
            "`prefer_other_terms` to break"
        )

    with_prefer = solve(
        a_requirement(
            name="cordex-tas-monthly-prefer-live",
            group_by=("model",),
            group_by_other_terms=group_by_other_terms,
            prefer_other_terms={VERSION_REALIZATION: (preferred,)},
        ),
        catalogue,
    )

    # Preferring narrows or leaves alone; it can never make a group ambiguous that
    # was not. A value not listed is ranked last, never refused.
    assert set(with_prefer.ambiguous) <= set(without.ambiguous), (
        "preferring made a group ambiguous which was not: "
        f"{set(with_prefer.ambiguous) - set(without.ambiguous)}"
    )

    resolved = set(without.ambiguous) - set(with_prefer.ambiguous)
    assert resolved, (
        f"{len(without.ambiguous)} group(s) were ambiguous and preferring "
        f"{VERSION_REALIZATION}={preferred!r} resolved none of them. Nothing checks "
        "the names in `prefer_other_terms`, so a name the documents do not carry "
        "ranks every candidate equally and silently changes nothing -- which is what "
        "this looks like."
    )

    for key in resolved:
        # Note: use of `one` ensures there is only one match
        chosen = with_prefer.satisfied[key].one("tas")
        # Read back under the document's own key, which is how it was asked for.
        # That the same value also happens to fill the `processing_id` column under
        # this module's query style is beside the point: `prefer` was never given
        # that column, so the only thing that can have narrowed this group is
        # `prefer_other_terms`.
        assert chosen.facet(VERSION_REALIZATION) == preferred, (
            f"group {key} was resolved by preferring {preferred!r}, but the chosen "
            f"dataset records {chosen.facet(VERSION_REALIZATION)!r}"
        )


# --------------------------------------------------------------------------------
# The query-class route: the same facets, declared instead of escaped.
#
# The first two need no network -- they are about our own naming and our own checks --
# so they carry no marker and run in the normal suite.
# --------------------------------------------------------------------------------


def test_both_routes_build_the_same_search_request():
    """
    Naming a facet in a query class, or escaping it, asks the archive the same question

    The headline of this module, and the reason the two halves are worth having side
    by side: a facet declared in `QueryCORDEX` and the same facet written into
    `other_terms` arrive at the API as the same parameter, with the same prefix and
    the same value. The choice between the routes is about what *we* have to write and
    what we can check, not about what gets searched.

    No network: this is the request we would send, which is the part we decide.
    """
    via_other_terms = CORDEX_FACADE.parameters.get_search_request_facet_values(
        to_canonical(LEAF_QUERY)
    )
    via_query_class = CORDEX_FACADE.parameters.get_search_request_facet_values(
        to_canonical(LEAF_QUERY_CORDEX)
    )

    assert via_other_terms == via_query_class

    # Spelled out, because "they are equal" would also pass if both were empty or if
    # both had quietly dropped the two facets this module is about.
    assert via_query_class == {
        "collection": (PROJECT,),
        f"{PREFIX}:variable_id": ("tas",),
        f"{PREFIX}:frequency": ("mon",),
        f"{PREFIX}:domain_id": ("EUR-12",),
        f"{PREFIX}:driving_source_id": ("ERA5",),
    }


def test_group_by_refuses_a_facet_only_our_own_query_class_models():
    """
    A query class of your own does not reach `group_by`, so grouping still escapes

    The limit of the query-class route, pinned rather than worked around, because it
    is the one place injecting a query class is not enough. `requirement` checks
    `group_by` and `prefer` against
    [modelled_facet_names][esmporium.query.modelled_facet_names] called with **no
    argument**, i.e. against `PROJECT_QUERY_MAP_DEFAULT` -- so a facet `QueryCORDEX`
    declares is unknown to it however thoroughly the mapping is injected everywhere
    else (`search` and `DatabaseCatalogue` both take one).

    The consequence is a split worth knowing about: on this route you query with
    `domain_id` and still group with `group_by_other_terms`. What declaring the facet
    does buy is the *spelling* -- `driving_source_id` rather than
    `cordex-cmip6:driving_source_id` -- which
    `test_a_query_class_facet_groups_under_its_unprefixed_name` is what shows.

    This is a tripwire. If it stops raising, `requirement` has learned to take a
    project-to-query mapping, and then `group_by=("model", "driving_source_id")` is
    what this module should be doing instead.
    """
    assert "driving_source_id" in facet_spec(QueryCORDEX).query_specific_facets
    assert "driving_source_id" not in modelled_facet_names()
    assert "driving_source_id" in modelled_facet_names(PROJECT_QUERY_MAP_CORDEX)

    with pytest.raises(UnmodelledFacetError, match="driving_source_id"):
        a_requirement(
            query=LEAF_QUERY_CORDEX,
            group_by=("model", DRIVING_SOURCE_ID_DECLARED),
        )


@pytest.mark.hits_esgf_search_api
def test_a_cordex_query_class_models_what_other_terms_had_to_carry(catalogue_cordex):
    """
    The same live rows, with the two facets modelled instead of escaped

    The inverse of the assertion in
    `test_our_cordex_query_style_still_reads_a_live_document`, which pins
    `project_facets == {}` for the route where nothing is declared. Told about
    `QueryCORDEX`, the catalogue models both facets and they arrive under their own
    names.
    """
    entries = catalogue_cordex.find(LEAF_QUERY_CORDEX)
    assert entries, "the rows are saved but the query-class route cannot find them"

    for entry in entries:
        # Modelled, which is the claim. `modelled_facet` deliberately does not fall
        # through to `other_terms`, so an answer here cannot have come from the
        # escape hatch -- that is what makes this the inverse assertion and not just
        # a second way of reading the same value.
        assert entry.modelled_facet(DOMAIN_ID_DECLARED) == "EUR-12"
        assert entry.modelled_facet(DRIVING_SOURCE_ID_DECLARED) == "ERA5"

        # And both spellings still read, because `other_terms` is filled verbatim from
        # the document whatever the query class says. Worth pinning: it means moving
        # to a query class does not break anything already written against the
        # prefixed names.
        assert entry.facet(DRIVING_SOURCE_ID) == "ERA5"

    # Nothing on this route went through the escape hatch on the way in.
    assert not LEAF_QUERY_CORDEX.other_terms


@pytest.mark.hits_esgf_search_api
def test_a_query_class_facet_groups_under_its_unprefixed_name(catalogue_cordex):
    """
    What the query-class route buys at the solver: the prefix is gone

    The pair of `test_an_other_terms_facet_reaches_the_solver_group_key`, which groups
    the same live rows on the same facet under `cordex-cmip6:driving_source_id`. Here
    the name is `driving_source_id`, and it resolves because the facet is modelled --
    not because `group_by_other_terms` is unchecked and happened to match.

    Still `group_by_other_terms` rather than `group_by`, which is not a choice:
    `test_group_by_refuses_a_facet_only_our_own_query_class_models` is why.
    """
    req = a_requirement(
        name="cordex-tas-monthly-query-class-live",
        query=LEAF_QUERY_CORDEX,
        group_by=("model",),
        group_by_other_terms=(DRIVING_SOURCE_ID_DECLARED,),
    )

    result = solve(req, catalogue_cordex)

    discovered = set(result.satisfied) | set(result.unsatisfied) | set(result.ambiguous)
    assert discovered, "rows were saved but the solver discovered no groups"

    for key in discovered:
        assert [name for name, _ in key] == ["model", DRIVING_SOURCE_ID_DECLARED]
        driving_model = dict(key)[DRIVING_SOURCE_ID_DECLARED]
        # `None` is what an unanswered name looks like, and on this route it would
        # mean the catalogue did not model the facet after all -- the failure this
        # test exists to catch.
        assert driving_model == "ERA5", (
            f"{DRIVING_SOURCE_ID_DECLARED!r} reached the group key as "
            f"{driving_model!r}. `None` means the facet was not modelled, so the "
            "query class is not doing what this test says it does."
        )

    assert result.explain()
