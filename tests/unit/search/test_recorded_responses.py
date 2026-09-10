"""
Test our parsing against responses the live APIs really sent

The other unit tests parse responses we wrote ourselves,
which pins our behaviour but cannot tell us
whether we understood the API's shape correctly in the first place.
These parse recordings of real answers, and so can, without a network connection.

The recordings go stale.
That is the trade: they will not notice an API changing shape until they are
refreshed. Refresh them with `uv run python scripts/record_search_responses.py`
and read the diff.

Two kinds of recording are read here,
because a search API facade answers two kinds of question:
how to do searches
(the total a search matched, via `get_n_matches`,
and the datasets a search returned, via `parse_search_results`)
and which values a facet has (`parse_facet_values`).

Everything is read through the facade.
The count in particular is *not* keyed the same way by every endpoint --
ESGF-NG east and west speak the same format and still disagree about where it lives --
so it is the facade's result parser, picked per project and endpoint, which reads it.
The wiring is covered on its own, with mocked responses we wrote, in
`test_search.py` and `test_check_query_values.py`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from esmporium.query import facet_spec
from esmporium.search import (
    ESGF1_CMIP5_FACADE_PARAMETERS,
    ESGF1_CMIP6_FACADE_PARAMETERS,
    ESGFNG_CMIP6_FACADE_PARAMETERS,
    ESGFNG_CMIP7_FACADE_PARAMETERS,
    DatasetFacets,
    ESGFNGCMIP6ResultParser,
    ESGFNGCMIP7ResultParser,
    SearchAPIESGF1Solr,
    SearchAPIESGF15BridgeSolr,
    SearchAPIESGFNGSTAC,
    SearchAPIFacade,
    SolrSingleRowResultParser,
    SolrVariableBundleResultParser,
    build_transient_retrying,
    get_mapping_to_query_style_facet_names,
    stac_base_id,
    stac_east_n_matches,
    stac_id_without_version,
    stac_west_n_matches,
)

RECORDED_DIR = Path(__file__).parents[2] / "test-data" / "search"
"""Where the recorded responses live"""

PROBE_FACETS = {"variable", "reporting_interval", "model"}
"""
A few facets every project has, to check the values by eye against

`tas` is the probe because every project publishes it,
and it is what the recorded query asked for.
"""


def facade(
    parameters,
    search_api_cls,
    result_parser,
    host="recorded.example",
) -> SearchAPIFacade:
    """
    Build a facade for parsing a recording

    The host and retry policy are irrelevant here (nothing is sent),
    so any values will do.

    The result parser is not: it is picked for the project and the endpoint,
    so each case below has to name the one the real facade would use.
    """
    return SearchAPIFacade(
        parameters=parameters,
        search_api=search_api_cls(host, build_transient_retrying(1)),
        result_parser=result_parser,
    )


def every_facet(facade: SearchAPIFacade) -> set[str]:
    """
    Get every facet a facade's query style can express

    This has to match `facets_to_list` in `scripts/record_search_responses.py`:
    asking here for something the recording never asked the API about
    would only prove that it is not in the file.

    Parameters
    ----------
    facade
        The facade whose query style to read

    Returns
    -------
    :
        The facets it can express, named the way they are asked for
    """
    return set(facet_spec(facade.parameters.base_query_style).expressible_facets)


RECORDED_CASES = (
    pytest.param(
        "esgf1-solr-cmip5",
        facade(
            ESGF1_CMIP5_FACADE_PARAMETERS,
            SearchAPIESGF1Solr,
            SolrVariableBundleResultParser(),
        ),
        id="esgf1-solr-cmip5",
    ),
    pytest.param(
        "esgf1-solr-cmip6",
        facade(
            ESGF1_CMIP6_FACADE_PARAMETERS,
            SearchAPIESGF1Solr,
            SolrSingleRowResultParser(),
        ),
        id="esgf1-solr-cmip6",
    ),
    pytest.param(
        "esgf15-bridge-cmip6",
        facade(
            ESGF1_CMIP6_FACADE_PARAMETERS,
            SearchAPIESGF15BridgeSolr,
            SolrSingleRowResultParser(),
        ),
        id="esgf15-bridge-cmip6",
    ),
    # Each ESGF-NG case names the deployment its recording came from,
    # because that is what the parsers are told apart by:
    # the readers below are the ones the store gives a facade for that host.
    pytest.param(
        "esgf-ng-stac-cmip6-east",
        facade(
            ESGFNG_CMIP6_FACADE_PARAMETERS,
            SearchAPIESGFNGSTAC,
            ESGFNGCMIP6ResultParser(
                read_id_project_specific=stac_base_id,
                read_n_matches=stac_east_n_matches,
            ),
        ),
        id="esgf-ng-stac-cmip6-east",
    ),
    pytest.param(
        "esgf-ng-stac-cmip6-west",
        facade(
            ESGFNG_CMIP6_FACADE_PARAMETERS,
            SearchAPIESGFNGSTAC,
            ESGFNGCMIP6ResultParser(
                read_id_project_specific=stac_id_without_version,
                read_n_matches=stac_west_n_matches,
            ),
        ),
        id="esgf-ng-stac-cmip6-west",
    ),
    pytest.param(
        "esgf-ng-stac-cmip7-east",
        facade(
            ESGFNG_CMIP7_FACADE_PARAMETERS,
            SearchAPIESGFNGSTAC,
            ESGFNGCMIP7ResultParser(read_n_matches=stac_east_n_matches),
        ),
        id="esgf-ng-stac-cmip7-east",
    ),
    pytest.param(
        "esgf-ng-stac-cmip7-west",
        facade(
            ESGFNG_CMIP7_FACADE_PARAMETERS,
            SearchAPIESGFNGSTAC,
            ESGFNGCMIP7ResultParser(read_n_matches=stac_west_n_matches),
        ),
        id="esgf-ng-stac-cmip7-west",
    ),
)
"""Each recording, with the facade which asked for it"""


def load(name):
    """
    Load a recorded response

    Parameters
    ----------
    name
        The name it was recorded under

    Returns
    -------
    :
        The recorded response
    """
    path = RECORDED_DIR / f"{name}.json"
    assert path.exists(), (
        f"No recording at {path}. "
        "Record it with `uv run python scripts/record_search_responses.py`."
    )

    return json.loads(path.read_text())


@pytest.mark.parametrize("name, facade", RECORDED_CASES)
def test_result_count_of_a_recorded_search(name, facade):
    """Test that we can count the matches in a response an API really sent"""
    raw = load(f"{name}-search")

    assert facade.get_n_matches(raw) > 0


CMIP5_RECORDED_CASES = tuple(c for c in RECORDED_CASES if "cmip5" in str(c.id))
"""The recorded cases whose project bundles many variables into one document"""

NON_CMIP5_RECORDED_CASES = tuple(c for c in RECORDED_CASES if "cmip5" not in str(c.id))
"""The recorded cases whose documents are already one dataset each"""


@pytest.mark.parametrize("name, facade", RECORDED_CASES)
def test_parse_search_results_are_well_formed(name, facade):
    """Test the shape of the datasets we parse out of a response an API really sent"""
    raw = load(f"{name}-search")

    documents = facade.parse_search_results(raw)

    assert documents, "no documents were parsed from a non-empty search"
    for document in documents:
        assert document.id_project_specific
        assert document.version
        assert document.esgf_doc_id
        assert document.nodes, "a document was parsed with nowhere to fetch it from"
        assert document.datasets, "a document mapped to no dataset rows"
        for row in document.datasets:
            assert isinstance(row, DatasetFacets), (
                "a dataset row is not the typed DatasetFacets we parse into"
            )
            # grid_label is legitimately NULL (CMIP5); every other facet must be set.
            for column, value in row.model_dump().items():
                if column == "grid_label":
                    continue
                assert value, f"{column} was parsed empty"


@pytest.mark.parametrize("name, facade", CMIP5_RECORDED_CASES)
def test_cmip5_document_explodes_into_variables_sharing_one_edition(name, facade):
    """A CMIP5 record bundles many variables that share one bundle id and edition"""
    raw = load(f"{name}-search")

    documents = facade.parse_search_results(raw)

    for document in documents:
        variables = [row.variable for row in document.datasets]
        assert len(variables) > 1, "a CMIP5 bundle should carry many variables"
        assert len(set(variables)) == len(variables), "a variable was repeated"

        # Everything but the variable is shared across the bundle's rows...
        without_variable = [
            row.model_dump(exclude={"variable"}) for row in document.datasets
        ]
        assert all(row == without_variable[0] for row in without_variable)
        # ...and CMIP5 has no grid label.
        assert without_variable[0]["grid_label"] is None


@pytest.mark.parametrize("name, facade", NON_CMIP5_RECORDED_CASES)
def test_non_cmip5_document_is_a_single_dataset(name, facade):
    """A CMIP6/CMIP7 document maps to exactly one dataset row"""
    raw = load(f"{name}-search")

    documents = facade.parse_search_results(raw)

    for document in documents:
        assert len(document.datasets) == 1


STAC_RECORDED_CASES = tuple(case for case in RECORDED_CASES if "stac" in str(case.id))
"""The recorded cases whose API describes its facet values in a STAC collection"""


@pytest.mark.parametrize("name, facade", RECORDED_CASES)
def test_recorded_rows_carry_the_project_they_were_asked_for(name, facade):
    """Every dataset row says which project it belongs to, and says the right one

    Which field carries the project is the parser's business and it varies
    (a Solr `project` facet, CMIP6 STAC's `cmip6:mip_era`, CMIP7 STAC's `project`
    property), so it is worth pinning the value rather than only its shape.
    """
    raw = load(f"{name}-search")
    (expected,) = [p for p in ("CMIP5", "CMIP6", "CMIP7") if p.lower() in name]

    documents = facade.parse_search_results(raw)

    assert documents
    assert {row.project for doc in documents for row in doc.datasets} == {expected}


BASE_ID_RECORDED_CASES = tuple(
    case for case in STAC_RECORDED_CASES if str(case.id) == "esgf-ng-stac-cmip6-east"
)
"""
The recorded STAC cases whose features carry a `base_id`

Only east's CMIP6 collection publishes one: west's CMIP6 features do not, and neither
deployment publishes one for CMIP7.
"""

RECOVERED_ID_RECORDED_CASES = tuple(
    case for case in STAC_RECORDED_CASES if case not in BASE_ID_RECORDED_CASES
)
"""The recorded STAC cases whose bundle id has to be recovered from the feature id"""


@pytest.mark.parametrize("name, facade", BASE_ID_RECORDED_CASES)
def test_recorded_stac_bundle_id_is_read_from_base_id(name, facade):
    """Where a deployment carries the bundle id outright, we read it

    Recovering it from the feature id instead would work today, but only because a
    CMIP6 feature id happens to be the bundle id with a version token on the end.
    Reading `base_id` is not making that assumption.
    """
    raw = load(f"{name}-search")

    documents = facade.parse_search_results(raw)

    assert documents
    assert [doc.id_project_specific for doc in documents] == [
        feature["properties"]["base_id"] for feature in raw["features"]
    ]


@pytest.mark.parametrize("name, facade", RECOVERED_ID_RECORDED_CASES)
def test_recorded_stac_bundle_id_drops_the_version_token(name, facade):
    """Where it carries no `base_id`, the bundle id is recovered from the feature id

    The recording is checked for the absence first: the day one of these starts
    publishing a `base_id`, this says so rather than quietly carrying on stripping ids.
    """
    raw = load(f"{name}-search")

    documents = facade.parse_search_results(raw)

    assert documents
    for feature in raw["features"]:
        assert "base_id" not in feature["properties"], (
            "this recording carries a base_id, so it no longer has to be recovered "
            "from the feature id"
        )
        assert re.fullmatch(r"v\d+", feature["id"].rsplit(".", 1)[-1]), (
            "this feature id has no version token, so there is nothing to drop"
        )

    assert [doc.id_project_specific for doc in documents] == [
        feature["id"].rsplit(".", 1)[0] for feature in raw["features"]
    ]
    # The document still remembers which edition it came from.
    assert [doc.esgf_doc_id for doc in documents] == [
        feature["id"] for feature in raw["features"]
    ]


@pytest.mark.parametrize("name, facade", RECORDED_CASES)
def test_parse_facet_values_of_a_recorded_response(name, facade):
    """Test that we can read the facet values out of a response an API really sent"""
    raw = load(f"{name}-facets")

    res = facade.parse_facet_values(raw, PROBE_FACETS)

    assert set(res) <= PROBE_FACETS, "we were told about a facet we did not ask about"
    assert "tas" in res["variable"]
    assert res["model"]
    assert res["reporting_interval"]


@pytest.mark.parametrize("name, facade", RECORDED_CASES)
def test_recorded_facet_values_are_well_formed(name, facade):
    """
    Test the shape of what we hand back, on real data

    Every facet the query style can express is asked about, so this covers the
    facets which the APIs describe in ways that are not a list of values,
    as well as those they do. It also covers the query-style-specific names
    (`product` on CMIP5, and so on), which are the ones we guessed at.

    A facet we report has to have at least one value:
    reporting a facet with nothing in it would be read as
    "this facet has no valid values", which is never what we mean.
    """
    raw = load(f"{name}-facets")
    facets = every_facet(facade)

    res = facade.parse_facet_values(raw, facets)

    assert res, "no facet was reported at all"
    assert set(res) <= facets, "we were told about a facet we did not ask about"
    for facet, values in res.items():
        assert values, f"{facet} was reported with no values"
        assert all(isinstance(value, str) and value for value in values), (
            f"{facet} was reported with a value which is not a non-empty string"
        )


@pytest.mark.parametrize("name, facade", STAC_RECORDED_CASES)
def test_recorded_facets_which_are_not_enumerated_are_left_out(name, facade):
    """
    Test that a facet the API describes without listing its values is left out

    A STAC collection summarises some facets as a regular expression or as a
    range rather than as a list, because their values are generated rather than
    chosen from a vocabulary (`variant_label` is the standing example).

    What counts as "not enumerated" is read out of the recording itself rather
    than hard coded, so this keeps testing the right thing
    if the API starts listing something it used to describe as a pattern.
    """
    raw = load(f"{name}-facets")
    facets = every_facet(facade)

    res = facade.parse_facet_values(raw, facets)

    prefix = f"{facade.parameters.prefix}:"

    asked_for = {
        native: asked
        for asked, native in get_mapping_to_query_style_facet_names(
            facade.parameters.base_query_style, facets
        ).items()
    }
    not_enumerated = {
        asked
        for property_name, summary in raw["summaries"].items()
        if property_name.startswith(prefix)
        and (asked := asked_for.get(property_name[len(prefix) :])) is not None
        and not (
            isinstance(summary, list)
            and any(isinstance(value, str) for value in summary)
        )
    }

    assert not_enumerated, (
        "this recording enumerates every facet, so it cannot test that "
        "a non-enumerated one is left out"
    )
    assert not_enumerated.isdisjoint(res), (
        "a facet the collection did not enumerate was reported as having values"
    )


@pytest.mark.parametrize("name, facade", STAC_RECORDED_CASES)
def test_recorded_variant_label_is_summarised_as_a_pattern(name, facade):
    """
    Test that the generated identifier we build on really is described as a pattern

    `variant_label` being a pattern rather than a list is the reason the value
    checker treats it differently from a controlled vocabulary,
    so it is worth pinning against real data rather than only against
    a collection we wrote ourselves.
    """
    raw = load(f"{name}-facets")

    (native,) = get_mapping_to_query_style_facet_names(
        facade.parameters.base_query_style, {"variant_label"}
    ).values()
    summary = raw["summaries"][f"{facade.parameters.prefix}:{native}"]

    assert isinstance(summary, str), (
        f"{native} was summarised as a {type(summary).__name__}, not a pattern"
    )
    re.compile(summary)


@pytest.mark.parametrize("name, facade", STAC_RECORDED_CASES)
def test_summary_facet_keys_have_not_drifted_in_case(name, facade):
    """
    Test that no summary key differs from ours only by case

    We match summary keys exactly (see `stac_summary_values`): we build
    lowercase-prefixed names like `cmip7:variable_id` and look for that string.
    If a deployment renamed a key only by case (say `CMIP7:variable_id`), the
    exact match would miss it and the facet would be dropped silently, read
    downstream as "this facet has no enumerable values" rather than as an error.

    This catches that drift by finding a summary key that matches one of our
    facet names when case is ignored but not when it is respected, and only when
    the correctly-cased key is absent (so the facet really would be lost). It is
    the loud counterpart to the exact match the parser deliberately keeps.
    """
    raw = load(f"{name}-facets")
    facets = every_facet(facade)
    api_names = set(facade.parameters.get_mapping_to_api_facet_names(facets).values())

    summary_keys = set(raw["summaries"])
    summary_keys_by_lower = {key.lower(): key for key in summary_keys}

    drifted = {
        api_name: summary_keys_by_lower[api_name.lower()]
        for api_name in api_names
        # The exact key we look for is gone,
        if api_name not in summary_keys
        # but a differently-cased one is there, so its values would vanish.
        and api_name.lower() in summary_keys_by_lower
    }

    assert not drifted, (
        "these facet keys are present only under a different case, so the "
        f"parser's exact match would drop them silently: {drifted}. "
        "A deployment changed the case of a facet key; update the prefix or "
        "name mapping to match, or the facet's values disappear without an error."
    )
