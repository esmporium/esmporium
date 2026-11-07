"""
Walking CMIP7 ancestry end to end against the live ESGF-NG search API
"""

from __future__ import annotations

import warnings

import pytest
from sqlmodel import Session, select

from esmporium.db import (
    DatasetParentLink,
    DatasetVersionParent,
    NoParentFactsProviderError,
    ParentResolution,
    UnknownUntilExperimentError,
    build_result_processor,
    check_until_experiments_exist,
    find_parents,
    plan_parent_searches,
)
from esmporium.db.migrate import upgrade_to_head
from esmporium.db.schema import Dataset, DatasetVersion
from esmporium.query import QueryCMIP6, QueryCMIP7
from esmporium.search import search_single_project

pytestmark = pytest.mark.hits_esgf_search_api

GREGORY_VARIABLES = ("tas", "rsdt", "rlut", "rsut")
"""
The variables a Gregory-method ECS needs
"""

CMIP6_GREGORY_CHILD = QueryCMIP6(
    experiment_id="abrupt-4xCO2",
    variable_id="tas",
    frequency="mon",
    source_id="UKESM1-0-LL",
)
"""
One CMIP6 simulation to walk, kept small on purpose

`UKESM1-0-LL abrupt-4xCO2` branches from a `piControl` with a *different* variant
label, which is the case that makes reading the parent necessary rather than
guessable.
"""

UNTIL_PICONTROL = ("piControl", "esm-piControl")
"""
Where a CMIP7 chain should stop

Both spellings, because an Earth-system run branches from `esm-piControl`. Note that
neither is the top of the tree: CMIP7 `piControl` declares a parent of
`piControl-spinup`, which is not published, so a walk with no target would fail.
"""


def _cmip6_version_ids(session) -> list[int]:
    """
    Get the id of every stored CMIP6 dataset version

    Parameters
    ----------
    session
        The session to read with

    Returns
    -------
    :
        The version ids
    """
    return [
        one.id
        for one in session.exec(
            select(DatasetVersion)
            .join(Dataset, Dataset.id == DatasetVersion.dataset_id)
            .where(Dataset.project == "CMIP6")
        ).all()
    ]


@pytest.fixture
def migrated_engine(engine):
    """Get an engine whose schema was built by the migrations, not by `create_all`."""
    upgrade_to_head(engine)

    return engine


def test_gregory_children_reach_their_control_runs(migrated_engine):
    """
    The whole step: search children, read their parents for free, search the parents

    This is the shape every ancestry use case has. The number worth watching is how
    many searches it takes: one per *distinct* parent simulation, which is far fewer
    than one per child.
    """
    with Session(migrated_engine) as session:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            search_single_project(
                QueryCMIP7(experiment_id="abrupt-4xCO2", variable_id=GREGORY_VARIABLES),
                processor=build_result_processor(session),
                warn_on_pagination=False,
            )
        session.commit()

        children = session.exec(
            select(Dataset).where(Dataset.experiment == "abrupt-4xCO2")
        ).all()
        assert children, "ESGF published no CMIP7 abrupt-4xCO2 for these variables"

        # Reading the parent costs nothing: it arrived on the documents already fetched.
        assertions = session.exec(select(DatasetVersionParent)).all()
        assert len(assertions) >= len(children)
        assert all(one.parent_experiment is not None for one in assertions)
        assert all(one.raw_doc_id is not None for one in assertions)

        plan = plan_parent_searches(session)
        assert plan, "every child resolved before any search, which cannot be right"
        assert len(plan) < len(assertions), (
            "children were not deduplicated into parent simulations"
        )
        for item in plan:
            assert set(item.variables) <= set(GREGORY_VARIABLES)

        result = find_parents(session, until_experiment=UNTIL_PICONTROL)

        assert len(result.searched) == len(plan), (
            "a search was fired for something that was not planned"
        )
        assert result.linked
        assert result.terminals
        assert all(one.experiment in UNTIL_PICONTROL for one in result.terminals), (
            f"a chain stopped somewhere unexpected: {result.terminals}"
        )

        # The control runs are now datasets of their own, with links pointing at them.
        links = session.exec(select(DatasetParentLink)).all()
        assert len(links) == len(result.linked)
        for link in links:
            parent_version = session.get(DatasetVersion, link.parent_dataset_version_id)
            assert parent_version is not None
            parent = session.get(Dataset, parent_version.dataset_id)
            assert parent is not None
            assert parent.experiment in UNTIL_PICONTROL


def test_walking_the_same_datasets_again_fires_no_searches(migrated_engine):
    """
    Once a control run is held, asking again costs nothing

    Resolving reads the stored datasets rather than anything the first walk
    remembered, so this holds across processes and across days, not just within one
    session.
    """
    with Session(migrated_engine) as session:
        # No `api_call_observer`, deliberately: it writes through sessions of its own,
        # and SQLite lets one transaction write at a time, so an observer on the same
        # file database as the session doing the walking deadlocks the two against each
        # other. What is being measured is how many searches the walk decided to fire,
        # which the result says directly.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            search_single_project(
                QueryCMIP7(experiment_id="abrupt-4xCO2", variable_id="tas"),
                processor=build_result_processor(session),
                warn_on_pagination=False,
            )
        session.commit()

        first = find_parents(
            session, until_experiment=UNTIL_PICONTROL, check_until_exists=False
        )
        assert first.searched, "no parent search was fired at all"
        assert first.linked

        again = find_parents(
            session, until_experiment=UNTIL_PICONTROL, check_until_exists=False
        )

        assert again.searched == [], (
            f"asking again fired {len(again.searched)} search(es) for control runs "
            "already in the database"
        )
        assert again.linked == []


def test_a_control_run_fetched_for_one_variable_serves_another(migrated_engine):
    """
    The cross-dataset fast path, which is what makes attaching parents at scale cheap

    The reuse is of the parent *datasets*, so it helps for every variable the control
    run was actually fetched for -- which is why the parent search is variable-scoped
    and `extra_variables` exists to widen it. Here the first walk is widened to bring
    back the control runs' `rlut` as well, and a later analysis asking for `rlut` then
    resolves against those instead of searching again.
    """
    with Session(migrated_engine) as session:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            search_single_project(
                QueryCMIP7(experiment_id="abrupt-4xCO2", variable_id="tas"),
                processor=build_result_processor(session),
                warn_on_pagination=False,
            )
        session.commit()

        first = find_parents(
            session,
            until_experiment=UNTIL_PICONTROL,
            check_until_exists=False,
            extra_variables=("rlut",),
        )
        assert first.searched

        # Months later, a second analysis wants `rlut` of the same children.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            search_single_project(
                QueryCMIP7(experiment_id="abrupt-4xCO2", variable_id="rlut"),
                processor=build_result_processor(session),
                warn_on_pagination=False,
            )
        session.commit()

        second = find_parents(
            session, until_experiment=UNTIL_PICONTROL, check_until_exists=False
        )

        assert second.linked, "the second variable linked to nothing"
        assert len(second.searched) < len(first.searched), (
            f"the second variable fired {len(second.searched)} search(es) where the "
            f"first fired {len(first.searched)}; the control runs already held should "
            "have cost nothing"
        )

        # Everything is either linked or recorded as settled, except the control runs
        # themselves: the walk stopped at those, so their own declared parent
        # (`piControl-spinup`, which nobody publishes) was deliberately never looked
        # for. `UNRESOLVED` there means exactly that, and it is why a third walk still
        # fires nothing.
        unresolved = session.exec(
            select(DatasetVersionParent, Dataset)
            .join(
                DatasetVersion,
                DatasetVersion.id == DatasetVersionParent.dataset_version_id,
            )
            .join(Dataset, Dataset.id == DatasetVersion.dataset_id)
            .where(DatasetVersionParent.resolution == ParentResolution.UNRESOLVED)
        ).all()
        assert all(dataset.experiment in UNTIL_PICONTROL for _, dataset in unresolved)

        third = find_parents(
            session, until_experiment=UNTIL_PICONTROL, check_until_exists=False
        )
        assert third.searched == []


def test_a_misspelled_target_is_caught_before_anything_is_searched(migrated_engine):
    """
    A target that does not exist can never be reached
    """
    with pytest.raises(UnknownUntilExperimentError) as caught:
        check_until_experiments_exist(("piContorl",), "CMIP7")

    assert "piControl" in caught.value.suggestions


def test_the_live_target_experiments_are_real(migrated_engine):
    """Both spellings the walks here stop at are values CMIP7 publishes."""
    check_until_experiments_exist(UNTIL_PICONTROL, "CMIP7")


def test_cmip6_still_publishes_no_parent_facts_in_either_index(migrated_engine):
    """
    Why a header has to be read at all, pinned against the live API

    Two things, and the first is the premise of the second: CMIP6 publishes nothing
    about its parents where a search can see it, so a real CMIP6 search leaves its
    datasets with no assertions at all. A walk given no provider then refuses, which is
    the seam saying "nobody has looked" rather than "this data has no ancestry".

    If ESGF ever starts publishing them, this fails -- which is the right way round,
    because reading a header would then be an avoidable request.
    """
    with Session(migrated_engine) as session:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            search_single_project(
                # Narrowed to one model: the point is the shape of the gap, and every
                # CMIP6 dataset in existence would demonstrate it equally well at a
                # hundred times the cost.
                CMIP6_GREGORY_CHILD,
                processor=build_result_processor(session),
                warn_on_pagination=False,
            )
        session.commit()

        children = session.exec(select(Dataset).where(Dataset.project == "CMIP6")).all()
        assert children, "ESGF published no CMIP6 abrupt-4xCO2 tas"

        # Nothing was read off the documents, because CMIP6 publishes nothing to read.
        assert session.exec(select(DatasetVersionParent)).all() == []

        version_ids = _cmip6_version_ids(session)
        with pytest.raises(NoParentFactsProviderError) as caught:
            find_parents(
                session,
                version_ids,
                until_experiment=("piControl",),
                check_until_exists=False,
            )

        assert set(caught.value.missing) == {"CMIP6"}
        assert caught.value.missing["CMIP6"] == len(version_ids)
        # The message sends someone to the seam rather than to a dead end.
        assert not caught.value.provider_was_given
        assert "build_header_facts_provider" in str(caught.value)


def test_a_walk_given_a_provider_that_delivers_nothing_says_so(migrated_engine):
    """
    The other half of that error: a provider was given and still delivered nothing

    A node down, or a simulation with no HTTPS route. Reported as its own situation
    rather than as "no facts provider was given", which would send someone to fix
    something that is not broken.

    No network beyond the search: the provider here is one that deliberately does
    nothing, which is exactly what a totally unreachable archive looks like.
    """
    with Session(migrated_engine) as session:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            search_single_project(
                CMIP6_GREGORY_CHILD,
                processor=build_result_processor(session),
                warn_on_pagination=False,
            )
        session.commit()

        version_ids = _cmip6_version_ids(session)
        assert version_ids, "ESGF published no CMIP6 abrupt-4xCO2 tas"

        def delivers_nothing(session, dataset_version_ids):
            """Stand in for an archive where no node served a single header."""

        with pytest.raises(NoParentFactsProviderError) as caught:
            find_parents(
                session,
                version_ids,
                until_experiment=("piControl",),
                check_until_exists=False,
                facts_provider=delivers_nothing,
            )

        assert caught.value.provider_was_given
        assert "could not read a header" in str(caught.value)
        assert "HeaderReadCallRecord" in str(caught.value)
