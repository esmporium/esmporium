"""
Walking CMIP5 and CMIP6 ancestry end to end, reading real file headers

Opt-in: `--run-reads-file-headers`, or `make test-headers`. These hit the search API
*and* the data nodes, and they carry the data-node marker because that is the part that
is new and the part that breaks -- a search that fails here fails in
`test_find_parents_live.py` too.

What the unit tests cannot cover is whether the two ends actually meet. The planning,
the stopping, the matching and the linking are pinned offline in
`tests/unit/db/test_find_parents.py` with facts injected, and the parsers are pinned
against recorded bytes in `tests/unit/headers/`. These tests are the join: that a
header really read off a live node produces facts a live search can then find the
parent from.

**The CMIP5 case is the hard one**, and the reason it is worth the minutes it takes:
its headers name the parent's experiment and variant but no model, so the model is
inferred from the child -- and it has to be inferred in the *index's* spelling, which
differs from the file's. Nothing offline can catch that, because only a live search
rejects the wrong one.
"""

from __future__ import annotations

import warnings

import pytest
from sqlmodel import Session, select

from esmporium.datasets import HeaderReadOutcome
from esmporium.db import (
    DatasetParentLink,
    DatasetVersionParent,
    HeaderReadCallRecord,
    ParentInfoSource,
    ParentResolution,
    ParentVersionChoice,
    build_header_facts_provider,
    build_parent_searcher_with_file_access,
    build_result_processor_with_file_access,
    find_parents,
    plan_header_reads,
)
from esmporium.db.migrate import upgrade_to_head
from esmporium.db.schema import Dataset, DatasetVersion, File
from esmporium.fixes import PARENT_FIXES, fixes_from_mapping
from esmporium.query import QueryCMIP5, QueryCMIP6
from esmporium.search import search_single_project
from esmporium.search.search.errors import NoFacadeAnsweredError

pytestmark = pytest.mark.reads_file_headers

# One variable each, which is the smallest thing that still proves a walk. Two
# variables would show the one-header-per-simulation saving better, and the unit tests
# already prove that without a network.
CMIP6_CHILD = QueryCMIP6(
    experiment_id="abrupt-4xCO2",
    variable_id="tas",
    frequency="mon",
    source_id="UKESM1-0-LL",
)
"""
A CMIP6 child whose parent has to be read rather than guessed

`r1i1p1f2`, and its parent is `piControl r1i1p1f2` with `branch_time_in_parent`
39600.0 -- measured 2026-11-05. A reader assuming `r1i1p1f1` would be wrong.
"""

CMIP5_CHILD = QueryCMIP5(
    experiment="abrupt4xCO2",
    variable="tas",
    time_frequency="mon",
    # Spelled as CMIP5's search index spells it. The file's own `model_id` says
    # `bcc-csm1-1-m`, and getting those two confused is the mistake this test exists
    # to catch.
    model="BCC-CSM1.1(m)",
    ensemble="r1i1p1",
)
"""A CMIP5 child, whose parent facts are the least complete of any project"""


@pytest.fixture
def migrated_engine(engine):
    """Get an engine whose schema was built by the migrations, not by `create_all`."""
    upgrade_to_head(engine)

    return engine


def search_with_files_or_skip(session, query) -> list[int]:
    """
    Search for a query's datasets *and* their files, or skip

    The files matter: on Solr a dataset document carries no file URLs, so a walk given
    the plain result processor finds datasets with nothing to read.

    Parameters
    ----------
    session
        The session to write into

    query
        What to search for

    Returns
    -------
    :
        The id of every stored dataset version
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            search_single_project(
                query,
                processor=build_result_processor_with_file_access(session),
                warn_on_pagination=False,
            )
    except NoFacadeAnsweredError:
        # Every endpoint failing is somebody else's server being down rather than a
        # change in our code, so this is a skip.
        pytest.skip("no search endpoint answered")

    session.commit()

    if not session.exec(select(File)).all():
        pytest.skip("the search found no files, so there is no header to read")

    return [one.id for one in session.exec(select(DatasetVersion)).all()]


def walk(session, version_ids, *, until):
    """
    Walk the ancestry of some versions, reading headers for their parent facts

    Parameters
    ----------
    session
        The session to walk in

    version_ids
        The versions to start from

    until
        Which experiments to stop at

    Returns
    -------
    :
        The walk's result
    """
    return find_parents(
        session,
        version_ids,
        until_experiment=until,
        searcher=build_parent_searcher_with_file_access(),
        facts_provider=build_header_facts_provider(
            fixes=fixes_from_mapping(PARENT_FIXES)
        ),
        # The targets are real; `test_find_parents_live.py` checks that separately, and
        # checking it again here is a request per run for nothing.
        check_until_exists=False,
    )


def test_a_cmip6_chain_is_walked_from_a_header(migrated_engine):
    """
    The whole path, for real: search, read a header, search the parent, link it
    """
    with Session(migrated_engine) as session:
        version_ids = search_with_files_or_skip(session, CMIP6_CHILD)

        plan = plan_header_reads(session, version_ids)
        assert plan.to_read, "nothing planned, so the files were not usable"

        result = walk(session, version_ids, until=("piControl",))
        session.commit()

        assert result.hops >= 1
        assert result.linked, "a parent was found but never linked to a dataset"

        facts = session.exec(select(DatasetVersionParent)).all()
        header_facts = [
            row for row in facts if row.source == ParentInfoSource.FILE_HEADER
        ]
        assert header_facts, "no fact came from a header"

        for row in header_facts:
            assert row.parent_experiment == "piControl"
            # Read, not assumed: CMIP6 names the parent's variant and it need not be
            # the child's.
            assert row.parent_variant_label is not None
            assert row.parent_model == "UKESM1-0-LL"
            # The column that only a header fills in.
            assert row.branch_time_in_parent is not None
            # And the provenance: a fact read out of bytes names the bytes.
            assert row.file_id is not None

        links = session.exec(select(DatasetParentLink)).all()
        assert links

        # UKESM1-0-LL `piControl` is published several times over, which is why this
        # test is the one that pins the version choice: offline the candidates are
        # whatever a fixture made, and here they are whatever ESGF really holds. The
        # child predates every release of its parent, so the choice is a rule of ours
        # and the link has to say so.
        for link in links:
            parent_version = session.get(DatasetVersion, link.parent_dataset_version_id)
            assert parent_version is not None
            # The invariant `DatasetParentLink` says holds by construction, checked
            # where real data flows through it: nothing in the database enforces that
            # the version belongs to the dataset beside it.
            assert parent_version.dataset_id == link.parent_dataset_id

        decided = [
            one
            for one in result.version_decisions
            if one.choice != ParentVersionChoice.ONLY_VERSION
        ]
        assert decided, (
            "no version decision was reported, so either UKESM1-0-LL piControl is "
            "down to one release or the choice stopped being reported"
        )

        for decision in decided:
            # Not a hardcoded datestamp: UKESM republishes, so what is pinned is the
            # rule -- the release used is the newest of the ones considered -- rather
            # than today's answer to it.
            considered = (
                decision.chosen_dataset_version_id,
                *decision.passed_over_dataset_version_ids,
            )
            versions = [session.get(DatasetVersion, one) for one in considered]
            assert all(one is not None for one in versions)
            chosen = session.get(DatasetVersion, decision.chosen_dataset_version_id)
            assert chosen is not None

            if decision.choice == ParentVersionChoice.DEFAULTED:
                assert chosen.version == max(
                    one.version
                    for one in versions
                    if one is not None and not one.retracted
                )

            # The link carries the same verdict the report does; a reader querying the
            # column and a reader reading the walk's return value get one answer.
            matching = [
                one
                for one in links
                if one.child_dataset_version_id == decision.child_dataset_version_id
            ]
            assert matching
            assert all(one.version_choice == decision.choice for one in matching)


def test_a_cmip5_chain_is_walked_from_a_header(migrated_engine):
    """
    The hardest case in this step, end to end

    Two things can only be checked here. The parent's model is **inferred from the
    child** -- CMIP5 publishes none -- and it has to be inferred in the index's
    spelling (`BCC-CSM1.1(m)`) rather than the file's (`bcc-csm1-1-m`), or the parent
    search finds nothing. And `branch_time` is stored exactly as published, because
    CMIP5 gives no units to interpret it with.
    """
    with Session(migrated_engine) as session:
        version_ids = search_with_files_or_skip(session, CMIP5_CHILD)

        result = walk(session, version_ids, until=("piControl",))
        session.commit()

        assert result.hops >= 1
        assert result.linked, (
            "a CMIP5 parent was declared but never linked -- if the model spelling is "
            "the file's rather than the index's, this is what that looks like"
        )

        facts = session.exec(select(DatasetVersionParent)).all()
        header_facts = [
            row for row in facts if row.source == ParentInfoSource.FILE_HEADER
        ]
        assert header_facts

        for row in header_facts:
            assert row.parent_experiment == "piControl"
            assert row.parent_model == "BCC-CSM1.1(m)"
            assert row.parent_project == "CMIP5"
            # CMIP5 has neither of these, so they must not be invented.
            assert row.parent_activity is None
            assert row.parent_time_units is None
            # Recorded verbatim. `240.0` for this model, which is a count of years --
            # one of at least five encodings CMIP5 uses for this attribute.
            assert row.branch_time_in_parent is not None
            assert row.file_id is not None

        assert any(
            row.resolution == ParentResolution.RESOLVED for row in header_facts
        ), "the declared CMIP5 parent was never resolved to a dataset we hold"


def test_one_header_answers_a_whole_simulation(migrated_engine):
    """
    The saving, measured against a live archive rather than asserted

    `BCC-CSM1.1(m) abrupt4xCO2 r1i1p1` publishes 55 datasets. Reading one header for
    all of them is the difference between this being usable and not, and the count of
    recorded attempts is the only honest way to show it.
    """
    with Session(migrated_engine) as session:
        # Every variable of one simulation, which is where the ratio shows.
        version_ids = search_with_files_or_skip(
            session,
            QueryCMIP5(
                experiment="abrupt4xCO2",
                time_frequency="mon",
                model="BCC-CSM1.1(m)",
                ensemble="r1i1p1",
            ),
        )
        n_datasets = len(session.exec(select(Dataset)).all())
        assert n_datasets > 5, "too few datasets for the ratio to mean anything"

        plan = plan_header_reads(session, version_ids)
        assert len(plan.to_read) == 1, (
            f"{n_datasets} datasets of one simulation should need one header, "
            f"not {len(plan.to_read)}"
        )
        assert len(plan.to_read[0].dataset_version_ids) == len(version_ids)

        walk(session, version_ids, until=("piControl",))
        session.commit()

        successes = [
            row
            for row in session.exec(select(HeaderReadCallRecord)).all()
            if row.outcome == HeaderReadOutcome.SUCCESS
        ]

    # One successful read per simulation reached: the child's, and its parent's once
    # the parent's own datasets arrive needing facts of their own.
    assert len(successes) <= 3, (
        f"{len(successes)} headers read successfully for {n_datasets} datasets; "
        "one per simulation is the whole point"
    )
    assert successes, "no header was read at all"
