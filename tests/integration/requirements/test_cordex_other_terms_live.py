"""
The `other_terms` escape hatch, run end to end against the live ESGF APIs

`other_terms` is the one path where a facet travels from the user to the search API
with no translation and no checking, and since the requirement and search packages
were joined it is also a path through grouping (`group_by_other_terms`),
tie-breaking (`prefer_other_terms`) and matching. Each hop is unit-tested on its own.
Nothing else runs the whole length of it against live data, which is what this does.

CORDEX is the test case because it is the honest one: `cordex-cmip6:domain_id` and
`cordex-cmip6:driving_source_id` are real facets that no `Dataset` column holds and
no query class names, which is exactly the situation `other_terms` exists for. Since
esmporium registers no CORDEX query class, a CORDEX document models nothing
(`CatalogueEntry.project_facets` is empty), so the facets grouped and ranked on below
cannot be answered by a modelled path and these tests cannot pass by accident.

Two things to know before reading on.

**The naming table below is a test-local claim, not esmporium's position on how CORDEX
is modelled.** CORDEX is not a project esmporium supports: it is absent from
`PROJECT_QUERY_MAP_DEFAULT`, from `get_default_facade_definition` and from
`DEFAULT_SELECTOR`. Searching it at all means injecting a facade, a selector and a
project-to-query mapping, which is what a user would have to do today and what the
docstring of `PROJECT_QUERY_MAP_DEFAULT` points at ("to search a project we do not
know about yet"). So these tests also check, incidentally, that those injection seams
work as advertised. Two lines of the table had to be decided rather than read off the
documents, and are commented where they appear; if CORDEX ever becomes a project we
support, that is where the decision belongs and this module should be reconciled with
it rather than treated as precedent.

**One test is here to keep the failure modes apart.**
`test_our_cordex_naming_table_still_reads_a_live_document` asserts the stored row
directly and comes first, so that a red run can be read: if it is the one which failed,
our table disagrees with the archive; if it passed and a later one failed, the
`other_terms` path is at fault. Its own docstring says which half of that it covers and
which half the parser already announces for us.
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
)
from esmporium.requirements import leaf, requirement, solve
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

pytestmark = pytest.mark.hits_esgf_search_api

TIMEOUT = 60.0
"""How long to wait for a node, in seconds"""

LIMIT = 50
"""
Records per page

Comfortably more than the slice below matches, so one page holds all of it.
"""

PROJECT = "CORDEX-CMIP6"
"""
The project CORDEX-CMIP6 data comes back under

Also the STAC collection name, and `str.lower` of it is the facet name prefix, which
is why `STACFacadeParameters` needs neither of its converters overridden here.
"""

HOST = "search.east.esgf.io"
"""
The one ESGF-NG deployment with CORDEX-CMIP6 in quantity

`search.west.esgf.io` carries some too, so adding it as a fallback is one more facade
if we ever want it. NCI's Solr has none: it serves only the older, CMIP5-driven
`CORDEX`, whose facet names carry no prefix.
"""


class ESGFNGCordexCMIP6ParametersQueryStyle(BaseModel):
    """CORDEX-CMIP6 facet values under their ESGF-NG parameter names"""

    model_config = ConfigDict(extra="forbid")

    # One declaration per `DatasetFacets` column and nothing else, because that is
    # all a row needs: a column with no API name here is left unset and then fails
    # `DatasetFacets` validation, while a facet which is not a column (`activity`,
    # say) is never read by the parser at all. `project` is absent for the same
    # reason it is absent from the CMIP6 and CMIP7 STAC styles: a STAC parser reads
    # the project from the feature's `collection`.
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

    # Decided, not read off the documents. CORDEX-CMIP6 has no CMOR table concept and
    # `processing_id` is required and non-null, so something has to answer for it.
    # `version_realization` is the nearest "which processing of this data is this"
    # label: its sibling field `version_realization_info` reads e.g. "CanRCM5-SN with
    # runtime bias correction".
    #
    # Note what this choice does *not* affect, because it is easy to assume otherwise:
    # whether two rows are the same dataset. `DATASET_IDENTITY_INDEX` includes
    # `id_project_specific`, which for a STAC document is `properties.title` and
    # already carries the version realization, so a model's `v1-r1` and `v1-r2` stay
    # two rows however this is mapped. The choice is therefore about whether the
    # column means anything, not about identity.
    version_realization: Annotated[FacetValues, QueryFacet("processing_id")] = ()
    """See [Dataset.processing_id][esmporium.db.schema.Dataset.processing_id]."""

    # Also decided. No CORDEX-CMIP6 document carries a `grid_label`, but
    # `DatasetFacets.grid_label` is required-with-no-default so that a parser has to
    # *state* the absence rather than forget the facet. `ESGFNGResultParser` has no
    # CMIP5-style `{"grid_label": None, ...}` base to state it in, so naming the facet
    # here lets `read_facet` answer `None` for the missing field, which is the one
    # column allowed to be null.
    grid_label: Annotated[FacetValues, QueryFacet("grid_label")] = ()
    """See [Dataset.grid_label][esmporium.db.schema.Dataset.grid_label]."""

    other_terms: FacetValuesByName = {}
    """See [Query.other_terms][esmporium.query.known_queries.Query.other_terms]."""

    source_query: SourceQuery = None
    """See [Query.source_query][esmporium.query.known_queries.Query.source_query]."""


CORDEX_FACADE = SearchAPIFacade(
    parameters=STACFacadeParameters(
        base_query_style=ESGFNGCordexCMIP6ParametersQueryStyle,
        # `STACFacadeParameters` checks this against the query's project for us, via
        # its default `project_to_prefix_converter` of `str.lower`.
        prefix=PROJECT.lower(),
    ),
    search_api=SearchAPIESGFNGSTAC(host=HOST, retrying=build_default_retrying()),
    result_parser=ESGFNGResultParser(),
)
"""
The facade this module searches through

Built by hand rather than from `get_default_facade_definition`, which raises
`NotImplementedError` for any project but CMIP5, CMIP6 and CMIP7.
"""

SELECTOR = build_project_list_selector({PROJECT: (CORDEX_FACADE,)})
"""
Facade selection for CORDEX

`DEFAULT_SELECTOR` raises `KeyError` for a project it has no facade list for, so a
selector is the second of the three things a caller has to inject.
"""

PROJECT_QUERY_MAP = {PROJECT: Query}
"""
The query class CORDEX facets are expressed in: the canonical one

The third injected seam. `Query` does the job because the leaf below names only
canonical facets and `other_terms`, so there is nothing a CORDEX-specific query class
would have to express.
"""

DRIVING_SOURCE_ID = "cordex-cmip6:driving_source_id"
"""
The driving global model, spelled as a stored search document spells it

No `Dataset` column holds this and no query class names it, so it is reachable only
through `other_terms`. The prefix is part of the name: nothing translates these, and
the ESGF-NG deployments prefix their facets.
"""

VERSION_REALIZATION = "cordex-cmip6:version_realization"
"""Which processing of a run this is, again as a document spells it"""

DOMAIN_ID = "cordex-cmip6:domain_id"
"""The CORDEX region, again as a document spells it"""

LEAF_QUERY = Query(
    project=PROJECT,
    # Each facet in exactly one home. `variable` and `reporting_interval` are modelled
    # because the table above can express them, so writing them in `other_terms` would
    # be using the escape hatch for something that is not escaping. `other_terms`
    # carries the two facets which have nowhere else to go.
    variable="tas",
    reporting_interval="mon",
    other_terms={
        DOMAIN_ID: ("EUR-12",),
        DRIVING_SOURCE_ID: ("ERA5",),
    },
)
"""
The one slice every test here works from

Narrow on purpose, and narrow in a way that earns its keep: the ERA5-driven European
runs include two regional models published as both `v1-r1` and `v1-r2`, which is the
genuine tie `test_prefer_other_terms_breaks_a_tie` needs. Asking for ERA5 and then
grouping by driving model is also what a real analysis looks like.
"""

EXPECTED_ROWS = 6
"""
How many datasets the slice held when this test was written

Asserted as a floor rather than an equality: the archive grows, and a test which
fails because more CORDEX data was published would be telling us nothing.
"""


def a_requirement(**kwargs):
    """Build a requirement for the slice above, overriding whatever is passed"""
    return requirement(
        name=kwargs.pop("name", "cordex-tas-monthly-live"),
        tree=leaf(LEAF_QUERY, "tas"),
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
    Search the slice once, live, and hand back the database and the outcomes

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
        # A failure rather than a skip, and the check above is why that is safe:
        # reaching here means the node answered and answered *empty*. The likeliest
        # reason is ours, and it is the thing this module exists to catch -- an
        # `other_terms` key spelled wrongly, which is never checked and which an API
        # reports as a well-formed empty result rather than an error. Dropping the
        # prefix would do it. If it is not ours, then CORDEX-CMIP6 no longer
        # publishes monthly `tas` over Europe driven by ERA5, which is also worth
        # being told rather than passing over.
        pytest.fail(
            f"{HOST} answered and returned no CORDEX-CMIP6 dataset for "
            f"{describe(LEAF_QUERY)}. Either an `other_terms` key is wrong (they are "
            "never translated or checked, and these APIs answer a mis-spelled facet "
            "name with an empty result rather than an error) or the archive no "
            "longer holds this slice. Both are worth knowing, so this fails rather "
            "than skipping.",
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
    `test_our_cordex_naming_table_still_reads_a_live_document` asserts it.
    """
    engine, _ = searched

    return DatabaseCatalogue(engine)


def test_our_cordex_naming_table_still_reads_a_live_document(searched, catalogue):
    """
    Every column a stored CORDEX row needs was read from a live document

    This is here to keep two failure modes apart -- the naming table at the top of
    this module is ours, the archive is not -- and it comes first for that reason.

    It covers the half of that which does not announce itself. A field the table names
    and the documents have *dropped* needs no help from us: the parser raises a
    `ValidationError` naming the column before anything is stored, so the whole module
    errors out with the answer already in the message. What has no such alarm is a
    field which still exists and no longer means what we assumed -- a `processing_id`
    that stopped discriminating, a `grid_label` that appeared -- or a CORDEX query
    class arriving in `src` and quietly moving facets out of `other_terms`. Those
    would otherwise surface as a puzzling failure in a grouping assertion three tests
    down.
    """
    engine, _ = searched
    rows = saved(engine)
    blame = (
        "Our CORDEX naming table disagrees with the archive. That table is this "
        "module's own (CORDEX is not a project esmporium supports), so look there "
        "first and not at the `other_terms` path."
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
        # The one column allowed to be null, and it has to be *stated* as null:
        # see the comment on `grid_label` in the table above.
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


def test_other_terms_constrain_a_live_search_and_reach_the_catalogue(
    searched, catalogue
):
    """
    `other_terms` filter a live search, and come back keyed as the documents key them

    The request half and the read half of the escape hatch, in one test. The request
    half is proven by the search having been constrained at all: `domain_id` and
    `driving_source_id` are the only thing narrowing this slice beyond monthly `tas`,
    and the fixture fails loudly if they came back empty. The read half is that the
    values are in `other_terms`, under the document's own prefixed spelling.
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


def test_an_other_terms_facet_reaches_the_solver_group_key(searched, catalogue):
    """
    A facet only a search document knows defines a group

    The assertion this module was written for. `model` is a column, so grouping on it
    proves nothing new; `cordex-cmip6:driving_source_id` is held by no column and
    named by no query class, so for it to appear in a group key with a real value it
    has to have been read back out of a stored search document.
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


def test_prefer_other_terms_breaks_a_tie(catalogue):
    """
    A tie between candidates is broken on a facet only a search document knows

    Solved twice over the same rows, once with no preferences and once preferring one
    version realization. The proof is the difference: if `prefer_other_terms` were
    ignored -- the failure this exists to catch, since nothing checks these names --
    the ambiguous groups would be identical both times.

    Grouped by regional model, which is what makes the ties real rather than
    contrived: two of the regional models here publish the same ERA5-driven run as
    both `v1-r1` and `v1-r2`, so their groups hold two candidates and nothing in
    `group_by` can separate them.
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
        chosen = with_prefer.satisfied[key].one("tas")
        # Read back under the document's own key, which is how it was asked for.
        # That the same value also happens to fill the `processing_id` column under
        # this module's naming table is beside the point: `prefer` was never given
        # that column, so the only thing that can have narrowed this group is
        # `prefer_other_terms`.
        assert chosen.facet(VERSION_REALIZATION) == preferred, (
            f"group {key} was resolved by preferring {preferred!r}, but the chosen "
            f"dataset records {chosen.facet(VERSION_REALIZATION)!r}"
        )
