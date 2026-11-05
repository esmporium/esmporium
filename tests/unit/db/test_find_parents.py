"""
Planning parent searches, resolving them against stored datasets, and walking a chain

Every test here is offline. The walk only fires a search for a parent it cannot find
in the database, so a chain whose ancestors are already stored exercises the whole of
the climbing, the stopping and the failures without touching the network -- which is
also the fast path the design rests on.
"""

from __future__ import annotations

import itertools
import warnings

import pytest
from sqlmodel import Session, col, select

from esmporium.db import (
    Dataset,
    DatasetParentLink,
    DatasetVersion,
    DatasetVersionParent,
    IncompleteParentError,
    MatchOutcome,
    NoParentFactsProviderError,
    ParentInfoSource,
    ParentNotAncestor,
    ParentResolution,
    ParentResolutionError,
    ParentSimulation,
    ParentVersionChoice,
    ParentVersionDecidedWarning,
    TooManyParentHopsError,
    VersionMatchOutcome,
    find_parents,
    flushed_id,
    plan_parent_searches,
    preferred_parent_info,
    resolve_parent_links,
    save_parent_info,
)
from esmporium.search import ParentInfo

PICONTROL = ("piControl",)


def parent_dataset_of(session, link) -> Dataset:
    """Read the dataset a link points at, which is the certain half of the answer."""
    dataset = session.get(Dataset, link.parent_dataset_id)
    assert dataset is not None

    return dataset


def another_version(
    session, version_id: int, version: str, *, retracted: bool = False
) -> int:
    """Publish a second version of the dataset `version_id` belongs to."""
    existing = session.get(DatasetVersion, version_id)
    assert existing is not None
    added = DatasetVersion(
        dataset_id=existing.dataset_id,
        version=version,
        is_latest=False,
        retracted=retracted,
    )
    session.add(added)
    session.flush()

    return flushed_id(added)


def no_search(session, item) -> None:
    """
    Stand in for a search which finds nothing

    Every walk here is handed this, so no test in this file can reach the network. What
    is under test is what the walk *decides* -- which parents it plans, when it stops,
    what it records -- and none of that depends on what ESGF would have answered.
    """


def a_parent(experiment: str, variant_label: str = "r1i1p1f1") -> ParentInfo:
    """Build what a CMIP7 record declares, for a parent of the usual model."""
    return ParentInfo(
        parent_project="CMIP7",
        parent_activity="CMIP",
        parent_model="UKESM1-3-LL",
        parent_experiment=experiment,
        parent_variant_label=variant_label,
        parent_time_units="days since 1850-01-01",
    )


NO_PARENT = ParentInfo(
    parent_project=None,
    parent_activity=None,
    parent_model=None,
    parent_experiment=None,
    parent_variant_label=None,
    parent_time_units=None,
)


def store(session, a_dataset_version, experiment, *, parent, **facets) -> int:
    """Store one dataset version and what it declares about its parent."""
    version_id = a_dataset_version(session, experiment=experiment, **facets)
    save_parent_info(session, version_id, parent, ParentInfoSource.SEARCH_METADATA)

    return version_id


def test_many_children_of_one_control_run_are_one_search(engine, a_dataset_version):
    """
    The plan is per parent simulation, not per child

    This is the saving the whole step turns on: a Gregory calculation over three
    variants of four variables declares the same parent twelve times.
    """
    with Session(engine) as session:
        for variant in ("r1i1p1f1", "r2i1p1f1", "r3i1p1f1"):
            for variable in ("tas", "rsdt", "rlut", "rsut"):
                store(
                    session,
                    a_dataset_version,
                    "abrupt-4xCO2",
                    parent=a_parent("piControl"),
                    variable=variable,
                    variant_label=variant,
                )
        session.commit()

        plan = plan_parent_searches(session)

        assert len(plan) == 1
        assert plan[0].parent == ParentSimulation(
            project="CMIP7",
            model="UKESM1-3-LL",
            experiment="piControl",
            variant_label="r1i1p1f1",
        )
        # Scoped to what the children need, not to everything the control published.
        assert plan[0].variables == ("rlut", "rsdt", "rsut", "tas")
        assert len(plan[0].child_dataset_version_ids) == 12


def test_the_parent_query_names_only_the_simulation_and_the_variables(
    engine, a_dataset_version
):
    """
    Neither institution nor activity is constrained, and for different reasons

    The activity is published and recorded, but narrowing by it could only make a parent
    that exists unfindable if a publisher got it wrong. The institution is never
    published at all, and really does differ: a `UCSB` CMIP6 `historical` branches from
    an `E3SM-Project` `piControl`.

    Grid, interval and processing id are left out too -- they are
    [esmporium.db.parent_matching][]'s to compare, so that a parent published on another
    grid is still found rather than reading as absent.
    """
    with Session(engine) as session:
        store(session, a_dataset_version, "abrupt-4xCO2", parent=a_parent("piControl"))
        session.commit()

        canonical = plan_parent_searches(session)[0].to_canonical_query()

        assert canonical.experiment == ("piControl",)
        assert canonical.model == ("UKESM1-3-LL",)
        assert canonical.variant_label == ("r1i1p1f1",)
        assert canonical.variable == ("tas",)
        assert canonical.institution == ()
        assert canonical.activity == ()
        assert canonical.grid_label == ()
        assert canonical.reporting_interval == ()
        assert canonical.processing_id == ()


def test_the_parents_variant_is_not_assumed_to_be_the_childs(engine, a_dataset_version):
    """A simulation often branches from a variant other than its own."""
    with Session(engine) as session:
        store(
            session,
            a_dataset_version,
            "abrupt-4xCO2",
            parent=a_parent("piControl", "r1i1p2f1"),
            variant_label="r1i1p1f1",
        )
        session.commit()

        assert plan_parent_searches(session)[0].parent.variant_label == "r1i1p2f1"


def test_extra_variables_widen_the_search(engine, a_dataset_version):
    """Asking an ancestor for more than its children happen to need."""
    with Session(engine) as session:
        store(session, a_dataset_version, "abrupt-4xCO2", parent=a_parent("piControl"))
        session.commit()

        plan = plan_parent_searches(session, extra_variables=("areacella",))

        assert plan[0].variables == ("areacella", "tas")


def test_a_parent_missing_facets_is_refused(engine, a_dataset_version):
    """
    Filling the gaps in from the child would be guessing which run it came from

    Which is the kind of guess that silently links a dataset to the wrong control.
    """
    with Session(engine) as session:
        version_id = a_dataset_version(session)
        save_parent_info(
            session,
            version_id,
            ParentInfo(
                parent_project="CMIP7",
                parent_activity="CMIP",
                parent_model=None,
                parent_experiment="piControl",
                parent_variant_label=None,
                parent_time_units=None,
            ),
            ParentInfoSource.SEARCH_METADATA,
        )
        session.commit()

        with pytest.raises(IncompleteParentError, match="parent_model"):
            plan_parent_searches(session)


def test_resolving_links_a_child_to_the_parent_of_its_own_variable(
    engine, a_dataset_version
):
    """
    A simulation publishes many datasets; the child's variable picks one

    And the link points at the `Dataset`, not a version, so which version to use stays
    a question answered at read time.
    """
    with Session(engine) as session:
        child = store(
            session, a_dataset_version, "abrupt-4xCO2", parent=a_parent("piControl")
        )
        a_dataset_version(session, experiment="piControl", variable="tas")
        a_dataset_version(session, experiment="piControl", variable="pr")
        session.commit()

        result = resolve_parent_links(session)
        session.commit()

        assert len(result.linked) == 1
        link = result.linked[0]
        assert link.child_dataset_version_id == child
        parent_dataset = session.exec(
            select(DatasetVersionParent).where(
                DatasetVersionParent.dataset_version_id == child
            )
        ).one()
        assert parent_dataset.resolution == ParentResolution.RESOLVED
        assert parent_dataset.resolved_at is not None


def test_resolving_a_second_variable_needs_no_search(engine, a_dataset_version):
    """
    The cross-dataset fast path: a control run found for `tas` resolves `tos` for free

    A parent belongs to a simulation and not to a variable, so this works for a
    variable searched for long after the control run was fetched -- the answer is read
    out of the stored datasets rather than out of anything the earlier run remembered.
    """
    with Session(engine) as session:
        store(
            session,
            a_dataset_version,
            "abrupt-4xCO2",
            parent=a_parent("piControl"),
            variable="tas",
        )
        a_dataset_version(session, experiment="piControl", variable="tas")
        a_dataset_version(session, experiment="piControl", variable="tos")
        session.commit()
        resolve_parent_links(session)
        session.commit()

        # A new analysis turns up asking for `tos`, months later.
        store(
            session,
            a_dataset_version,
            "abrupt-4xCO2",
            parent=a_parent("piControl"),
            variable="tos",
        )
        session.commit()

        assert plan_parent_searches(session) != ()
        result = resolve_parent_links(session)
        session.commit()

        assert len(result.linked) == 1
        # Nothing is left wanting a search.
        assert plan_parent_searches(session) == ()


def test_a_parent_that_lacks_a_variable_is_a_gap_not_a_failure(
    engine, a_dataset_version
):
    """
    The lineage is intact and one variable of it has a hole

    Which is a different problem from the control run never being found, and
    collapsing the two would hide which it was.
    """
    with Session(engine) as session:
        store(
            session,
            a_dataset_version,
            "abrupt-4xCO2",
            parent=a_parent("piControl"),
            variable="rsdt",
        )
        a_dataset_version(session, experiment="piControl", variable="tas")
        session.commit()

        result = resolve_parent_links(session)
        session.commit()

        assert result.linked == ()
        assert len(result.variable_gaps) == 1
        assert result.variable_gaps[0].variable == "rsdt"
        assert session.exec(select(DatasetParentLink)).all() == []


def test_a_walk_stops_at_the_target_without_chasing_its_parent(
    engine, a_dataset_version
):
    """
    Reaching `piControl` ends the chain, which is what keeps `piControl-spinup` alone

    CMIP7 `piControl` declares a parent of `piControl-spinup`, and nobody publishes it.
    Stopping on the experiment we have *reached* is what stops that being searched for
    on every run.
    """
    with Session(engine) as session:
        child = store(
            session, a_dataset_version, "abrupt-4xCO2", parent=a_parent("piControl")
        )
        store(
            session,
            a_dataset_version,
            "piControl",
            parent=a_parent("piControl-spinup"),
        )
        session.commit()

        result = find_parents(
            session,
            [child],
            until_experiment=PICONTROL,
            check_until_exists=False,
            searcher=no_search,
        )

        assert result.searched == []
        assert len(result.linked) == 1
        assert [str(one) for one in result.terminals] == [
            "CMIP7 UKESM1-3-LL piControl r1i1p1f1"
        ]


def test_a_walk_climbs_as_far_as_it_needs_to(engine, a_dataset_version):
    """
    How deep a chain runs is a property of the science, not something a caller knows

    Which is why the stopping rule is an experiment name and not a number of hops.
    """
    with Session(engine) as session:
        child = store(
            session, a_dataset_version, "scen7-vl", parent=a_parent("historical")
        )
        store(session, a_dataset_version, "historical", parent=a_parent("piControl"))
        store(
            session,
            a_dataset_version,
            "piControl",
            parent=a_parent("piControl-spinup"),
        )
        session.commit()

        result = find_parents(
            session,
            [child],
            until_experiment=PICONTROL,
            check_until_exists=False,
            searcher=no_search,
        )

        assert len(result.linked) == 2
        assert [str(one) for one in result.terminals] == [
            "CMIP7 UKESM1-3-LL piControl r1i1p1f1"
        ]


def test_a_chain_that_never_reaches_the_target_raises(engine, a_dataset_version):
    """
    A lineage which is complete and does not go through the control run

    For an analysis that needs the control run, that is the same as not having the
    data -- which is what this becomes once a requirement can ask for ancestry.
    """
    with Session(engine) as session:
        child = store(
            session, a_dataset_version, "abrupt-4xCO2", parent=a_parent("amip")
        )
        store(session, a_dataset_version, "amip", parent=NO_PARENT)
        session.commit()

        with pytest.raises(ParentResolutionError) as caught:
            find_parents(
                session,
                [child],
                until_experiment=PICONTROL,
                check_until_exists=False,
                searcher=no_search,
            )

        assert len(caught.value.failures) == 1
        failure = caught.value.failures[0]
        assert isinstance(failure, ParentNotAncestor)
        assert failure.reached.experiment == "amip"
        assert failure.until_experiment == PICONTROL


def test_a_failed_walk_keeps_everything_it_learned(engine, a_dataset_version):
    """
    The raise comes after the commit, so running again does not start over

    The failures are also kept as objects rather than only rendered into the message,
    so a caller can act on them.
    """
    with Session(engine) as session:
        child = store(
            session, a_dataset_version, "abrupt-4xCO2", parent=a_parent("amip")
        )
        store(session, a_dataset_version, "amip", parent=NO_PARENT)
        session.commit()

        with pytest.raises(ParentResolutionError):
            find_parents(
                session,
                [child],
                until_experiment=PICONTROL,
                check_until_exists=False,
                searcher=no_search,
            )

        links = session.exec(select(DatasetParentLink)).all()
        assert len(links) == 1
        assert links[0].child_dataset_version_id == child


def test_a_walk_with_no_target_accepts_a_root(engine, a_dataset_version):
    """
    Walking to the top is allowed, it is just not a usable default for CMIP7

    With no target, reaching a record which declares no parent is an ordinary ending
    rather than a failure.
    """
    with Session(engine) as session:
        child = store(
            session, a_dataset_version, "abrupt-4xCO2", parent=a_parent("amip")
        )
        store(session, a_dataset_version, "amip", parent=NO_PARENT)
        session.commit()

        result = find_parents(
            session,
            [child],
            until_experiment=(),
            check_until_exists=False,
            searcher=no_search,
        )

        assert len(result.linked) == 1
        assert [one.experiment for one in result.terminals] == ["amip"]


def test_a_cycle_does_not_loop_forever(engine, a_dataset_version):
    """
    Published metadata which says two simulations are each other's parent

    Refusing to revisit a simulation is what bounds this, so it terminates rather than
    hitting the hop guard.
    """
    with Session(engine) as session:
        first = store(session, a_dataset_version, "exp-a", parent=a_parent("exp-b"))
        store(session, a_dataset_version, "exp-b", parent=a_parent("exp-a"))
        session.commit()

        result = find_parents(
            session,
            [first],
            until_experiment=(),
            check_until_exists=False,
            searcher=no_search,
        )

        assert len(result.linked) >= 1


def test_the_hop_guard_raises_rather_than_truncating(engine, a_dataset_version):
    """
    A partial lineage presented as a complete one would be the worse answer

    So the guard going off is an error, and it names what was still outstanding.
    """
    with Session(engine) as session:
        chain = [f"exp-{index}" for index in range(6)]
        first = store(session, a_dataset_version, chain[0], parent=a_parent(chain[1]))
        for experiment, parent in itertools.pairwise(chain[1:]):
            store(session, a_dataset_version, experiment, parent=a_parent(parent))
        store(session, a_dataset_version, chain[-1], parent=NO_PARENT)
        session.commit()

        with pytest.raises(TooManyParentHopsError, match="exp-"):
            find_parents(
                session,
                [first],
                until_experiment=(),
                check_until_exists=False,
                searcher=no_search,
                max_hops=1,
            )


def test_a_variable_a_parent_does_not_publish_is_settled_once(
    engine, a_dataset_version
):
    """
    A search that asks for a variable and does not get it is not fired again

    Leaving the row unresolved would make every later run ask the same control run for
    the same variable it has already said it does not have.
    """
    with Session(engine) as session:
        child = store(
            session,
            a_dataset_version,
            "abrupt-4xCO2",
            parent=a_parent("piControl"),
            variable="rsdt",
        )
        # The control run is here, and publishes something else.
        a_dataset_version(session, experiment="piControl", variable="tas")
        session.commit()

        result = find_parents(
            session,
            [child],
            until_experiment=PICONTROL,
            check_until_exists=False,
            searcher=no_search,
        )

        assert result.linked == []
        assert len(result.variable_gaps) == 1
        settled = session.exec(
            select(DatasetVersionParent).where(
                DatasetVersionParent.dataset_version_id == child
            )
        ).one()
        assert settled.resolution == ParentResolution.VARIABLE_NOT_PUBLISHED
        assert settled.resolved_at is not None

        # And it stays settled, so nothing is asked twice.
        again = find_parents(
            session,
            [child],
            until_experiment=PICONTROL,
            check_until_exists=False,
            searcher=no_search,
        )
        assert again.searched == []


def test_a_sibling_inherits_a_known_variable_gap(engine, a_dataset_version):
    """
    Which variables a simulation publishes is a fact about the simulation

    So a new dataset of a variable a control run has already been asked for, and did
    not have, inherits that answer rather than firing the same fruitless search.
    """
    with Session(engine) as session:
        first = store(
            session,
            a_dataset_version,
            "abrupt-4xCO2",
            parent=a_parent("piControl"),
            variable="rsdt",
        )
        a_dataset_version(session, experiment="piControl", variable="tas")
        session.commit()
        find_parents(
            session,
            [first],
            until_experiment=PICONTROL,
            check_until_exists=False,
            searcher=no_search,
        )

        # Another variant of the same experiment turns up wanting the same variable.
        sibling = store(
            session,
            a_dataset_version,
            "abrupt-4xCO2",
            parent=a_parent("piControl"),
            variable="rsdt",
            variant_label="r2i1p1f1",
        )
        session.commit()

        result = resolve_parent_links(session, [sibling])
        session.commit()

        assert len(result.variable_gaps) == 1
        assert result.still_unresolved == ()
        inherited = session.exec(
            select(DatasetVersionParent).where(
                DatasetVersionParent.dataset_version_id == sibling
            )
        ).one()
        assert inherited.resolution == ParentResolution.VARIABLE_NOT_PUBLISHED
        # Nothing is left for a search to do.
        assert plan_parent_searches(session, [sibling]) == ()


def test_a_broken_chain_keeps_being_reported(engine, a_dataset_version):
    """
    Recording that a parent is not published stops the *search*, not the failure

    A second run must still say the lineage is broken; going quiet because the answer
    was cached would be the worst of both.
    """
    with Session(engine) as session:
        child = store(
            session,
            a_dataset_version,
            "abrupt-4xCO2",
            parent=a_parent("piControl"),
        )
        # Nothing stored for the parent, and no search will find one offline either:
        # mark it the way a fruitless search would have.
        info = session.exec(
            select(DatasetVersionParent).where(
                DatasetVersionParent.dataset_version_id == child
            )
        ).one()
        info.resolution = ParentResolution.NOT_ON_ESGF
        session.add(info)
        session.commit()

        with pytest.raises(ParentResolutionError) as caught:
            find_parents(
                session,
                [child],
                until_experiment=PICONTROL,
                check_until_exists=False,
                searcher=no_search,
            )

        assert len(caught.value.failures) == 1
        assert caught.value.failures[0].parent.experiment == "piControl"
        # No search was fired to re-learn it.
        assert "piControl" in str(caught.value)


def header_facts(experiment: str, variant_label: str = "r1i1p1f1") -> ParentInfo:
    """What a header read will produce, once there is one to do the reading."""
    return ParentInfo(
        parent_project="CMIP6",
        parent_activity=None,
        parent_model="UKESM1-0-LL",
        parent_experiment=experiment,
        parent_variant_label=variant_label,
        parent_time_units="days since 1850-01-01",
    )


def build_facts_provider(chain: dict[str, str], project: str = "CMIP6"):
    """
    Stand in for the header reader a later PR adds

    Writes a `FILE_HEADER` assertion for every version it is given whose experiment has
    a parent in `chain`, which is exactly what reading a netCDF header will do -- so
    everything downstream of it is under test here, before any netCDF exists.
    """

    def provider(session, dataset_version_ids) -> None:
        for version_id in dataset_version_ids:
            dataset = session.exec(
                select(Dataset)
                .join(DatasetVersion, col(DatasetVersion.dataset_id) == col(Dataset.id))
                .where(DatasetVersion.id == version_id)
            ).one()
            parent_experiment = chain.get(dataset.experiment)
            if parent_experiment is None:
                continue

            save_parent_info(
                session,
                version_id,
                ParentInfo(
                    parent_project=project,
                    parent_activity=None,
                    parent_model=dataset.model,
                    parent_experiment=parent_experiment,
                    parent_variant_label=dataset.variant_label,
                    parent_time_units="days since 1850-01-01",
                ),
                ParentInfoSource.FILE_HEADER,
            )

    return provider


def a_cmip6_dataset(session, a_dataset_version, experiment: str, **facets) -> int:
    """A CMIP6 dataset version, with CMIP6-shaped facets."""
    cmip6 = {
        "project": "CMIP6",
        "model": "UKESM1-0-LL",
        "institution": "MOHC",
        "processing_id": "Amon",
        "grid_label": "gn",
    }

    return a_dataset_version(session, experiment=experiment, **{**cmip6, **facets})


def test_a_project_with_no_published_facts_refuses_rather_than_doing_nothing(
    engine, a_dataset_version
):
    """
    The silent failure this seam exists to remove

    CMIP6 publishes no parent facts on either API, so a CMIP6 version carries no
    assertion. Reading the frontier from existing rows meant the walk returned an empty
    result and no error -- indistinguishable from "this data has no ancestry".
    """
    with Session(engine) as session:
        child = a_cmip6_dataset(session, a_dataset_version, "abrupt-4xCO2")
        session.commit()

        with pytest.raises(NoParentFactsProviderError) as caught:
            find_parents(
                session,
                [child],
                until_experiment=PICONTROL,
                check_until_exists=False,
                searcher=no_search,
            )

        assert caught.value.missing == {"CMIP6": 1}
        assert "file header" in str(caught.value)


def test_cmip6_walks_a_whole_chain_once_facts_are_provided(engine, a_dataset_version):
    """
    The test that makes the header PR small

    Everything downstream of reading a header -- the parent query, the search, the
    matching, the linking, the hop reset -- is exercised here for CMIP6 with the facts
    injected. The only thing the next PR adds is the thing that produces them.
    """
    with Session(engine) as session:
        child = a_cmip6_dataset(session, a_dataset_version, "ssp585")
        a_cmip6_dataset(session, a_dataset_version, "historical")
        a_cmip6_dataset(session, a_dataset_version, "piControl")
        # a decoy at the control run: same variable, different table and interval
        a_cmip6_dataset(
            session,
            a_dataset_version,
            "piControl",
            processing_id="day",
            reporting_interval="day",
        )
        session.commit()

        result = find_parents(
            session,
            [child],
            until_experiment=PICONTROL,
            check_until_exists=False,
            searcher=no_search,
            facts_provider=build_facts_provider(
                {"ssp585": "historical", "historical": "piControl"}
            ),
        )

        assert result.hops == 2
        assert len(result.linked) == 2
        assert [str(one) for one in result.terminals] == [
            "CMIP6 UKESM1-0-LL piControl r1i1p1f1"
        ]
        assert result.ambiguous == []

        # The matcher, not the search, is what avoided the daily decoy.
        chosen = [parent_dataset_of(session, link) for link in result.linked]
        assert {one.processing_id for one in chosen} == {"Amon"}

        # The facts came from a header, and say so.
        sources = {
            one.source for one in session.exec(select(DatasetVersionParent)).all()
        }
        assert sources == {ParentInfoSource.FILE_HEADER}


def test_a_dataset_at_the_target_needs_no_facts(engine, a_dataset_version):
    """
    Reaching where you were asked to reach is not a failure

    A version whose own experiment is the target is never climbed, so its parent facts
    are never read. Demanding them would make a successful walk raise -- and for CMIP6
    would mean a header read nobody needs.
    """
    with Session(engine) as session:
        child = a_cmip6_dataset(session, a_dataset_version, "abrupt-4xCO2")
        a_cmip6_dataset(session, a_dataset_version, "piControl")
        session.commit()

        result = find_parents(
            session,
            [child],
            until_experiment=PICONTROL,
            check_until_exists=False,
            searcher=no_search,
            facts_provider=build_facts_provider({"abrupt-4xCO2": "piControl"}),
        )

        assert len(result.linked) == 1
        # piControl was reached and reported, with no assertion of its own.
        assert [str(one) for one in result.terminals] == [
            "CMIP6 UKESM1-0-LL piControl r1i1p1f1"
        ]


def test_a_header_fact_beats_what_the_index_published(engine, a_dataset_version):
    """
    Precedence, which is what holding a row per source is for

    Both rows are kept -- the point of `(dataset_version_id, source)` -- and the reader
    believes the header, because the header is what the data itself says.
    """
    with Session(engine) as session:
        child = a_cmip6_dataset(session, a_dataset_version, "abrupt-4xCO2")
        save_parent_info(
            session,
            child,
            ParentInfo(
                parent_project="CMIP6",
                parent_activity=None,
                parent_model="UKESM1-0-LL",
                parent_experiment="amip",
                parent_variant_label="r1i1p1f1",
                parent_time_units=None,
            ),
            ParentInfoSource.SEARCH_METADATA,
        )
        save_parent_info(
            session, child, header_facts("piControl"), ParentInfoSource.FILE_HEADER
        )
        a_cmip6_dataset(session, a_dataset_version, "piControl")
        session.commit()

        assert len(session.exec(select(DatasetVersionParent)).all()) == 2

        result = resolve_parent_links(session, [child])
        session.commit()

        assert len(result.linked) == 1
        parent = parent_dataset_of(session, result.linked[0])
        assert parent.experiment == "piControl"


def test_precedence_puts_a_fix_above_everything():
    """A correction exists because the published value is wrong, so it has to win."""
    rows = [
        DatasetVersionParent(
            id=index,
            dataset_version_id=1,
            source=source,
            resolution=ParentResolution.UNRESOLVED,
            parent_experiment=source.value,
        )
        for index, source in enumerate(
            (
                ParentInfoSource.SEARCH_METADATA,
                ParentInfoSource.FILE_HEADER,
                ParentInfoSource.FIX,
            ),
            start=1,
        )
    ]

    assert preferred_parent_info(rows).source == ParentInfoSource.FIX
    assert preferred_parent_info(rows[:2]).source == ParentInfoSource.FILE_HEADER
    assert preferred_parent_info([]) is None


def test_ambiguity_is_reported_and_left_unresolved(engine, a_dataset_version):
    """
    Two parents fitting equally well leaves the row for a better matcher

    Unresolved rather than settled, so injecting a matcher and running again can finish
    the job without anything having to be deleted first.
    """
    with Session(engine) as session:
        child = a_cmip6_dataset(session, a_dataset_version, "abrupt-4xCO2")
        save_parent_info(
            session, child, header_facts("piControl"), ParentInfoSource.FILE_HEADER
        )
        # the same simulation published by two institutions, neither the child's
        for institution in ("UCSB", "LLNL"):
            a_cmip6_dataset(
                session, a_dataset_version, "piControl", institution=institution
            )
        session.commit()

        result = resolve_parent_links(session, [child])
        session.commit()

        assert result.linked == ()
        assert len(result.ambiguous) == 1
        assert len(result.ambiguous[0].candidate_dataset_ids) == 2
        stored = session.exec(
            select(DatasetVersionParent).where(
                DatasetVersionParent.dataset_version_id == child
            )
        ).one()
        assert stored.resolution == ParentResolution.UNRESOLVED


def test_an_injected_matcher_resolves_an_ambiguity(engine, a_dataset_version):
    """Which is why the matchers are a parameter and not a constant."""
    with Session(engine) as session:
        child = a_cmip6_dataset(session, a_dataset_version, "abrupt-4xCO2")
        save_parent_info(
            session, child, header_facts("piControl"), ParentInfoSource.FILE_HEADER
        )
        for institution in ("UCSB", "LLNL"):
            a_cmip6_dataset(
                session, a_dataset_version, "piControl", institution=institution
            )
        session.commit()

        def prefer_llnl(child_dataset, candidates):
            for candidate in candidates:
                if candidate.institution == "LLNL":
                    return MatchOutcome(parent_dataset=candidate)

            return MatchOutcome(reason="no LLNL candidate")

        result = resolve_parent_links(session, [child], matchers={"CMIP6": prefer_llnl})
        session.commit()

        assert len(result.linked) == 1
        parent = parent_dataset_of(session, result.linked[0])
        assert parent.institution == "LLNL"


def test_one_version_of_a_parent_is_linked_without_a_word(engine, a_dataset_version):
    """A parent published once leaves nothing to decide, so nothing is reported."""
    with Session(engine) as session:
        child = store(
            session,
            a_dataset_version,
            "abrupt-4xCO2",
            parent=a_parent("piControl"),
            version="20261008",
        )
        only = a_dataset_version(session, experiment="piControl", version="20200101")
        session.commit()

        result = resolve_parent_links(session, [child])
        session.commit()

        assert result.version_decisions == ()
        assert len(result.linked) == 1
        assert result.linked[0].parent_dataset_version_id == only
        assert result.linked[0].version_choice == ParentVersionChoice.ONLY_VERSION


def test_a_republished_parent_is_linked_at_its_newest_and_reported(
    engine, a_dataset_version
):
    """
    The live CMIP6 case: every release of the parent is newer than the child

    So none of them carries the child's version string and nothing published says which
    was branched from. The chain is linked and climbs -- stopping here would stop at
    most CMIP6 parents -- and the link says the release was ours to pick.
    """
    with Session(engine) as session:
        child = store(
            session,
            a_dataset_version,
            "abrupt-4xCO2",
            parent=a_parent("piControl"),
            version="20190406",
        )
        first = a_dataset_version(session, experiment="piControl", version="20190410")
        middle = another_version(session, first, "20200630")
        newest = another_version(session, first, "20200828")
        session.commit()

        result = resolve_parent_links(session, [child])
        session.commit()

        assert len(result.linked) == 1
        link = result.linked[0]
        assert link.parent_dataset_version_id == newest
        assert link.version_choice == ParentVersionChoice.DEFAULTED

        (reported,) = result.version_decisions
        assert reported.child_dataset_version_id == child
        assert reported.chosen_dataset_version_id == newest
        assert reported.choice == ParentVersionChoice.DEFAULTED
        assert sorted(reported.passed_over_dataset_version_ids) == sorted(
            [first, middle]
        )
        assert str(reported.parent) == "CMIP7 UKESM1-3-LL piControl r1i1p1f1"

        (held,) = session.exec(select(DatasetVersionParent)).all()
        assert held.resolution == ParentResolution.RESOLVED


def test_a_parent_release_sharing_the_childs_version_is_the_one_linked(
    engine, a_dataset_version
):
    """
    A shared publication date is a publication batch, so that release belongs together

    Taken even though a newer release of the same parent is held, and still reported:
    it is better evidence than the newest-release default but it is a decision all the
    same.
    """
    with Session(engine) as session:
        child = store(
            session,
            a_dataset_version,
            "abrupt-4xCO2",
            parent=a_parent("piControl"),
            version="20190710",
        )
        matching = a_dataset_version(
            session, experiment="piControl", version="20190710"
        )
        newer = another_version(session, matching, "20210901")
        session.commit()

        result = resolve_parent_links(session, [child])
        session.commit()

        link = result.linked[0]
        assert link.parent_dataset_version_id == matching
        assert link.version_choice == ParentVersionChoice.CHILD_VERSION

        (reported,) = result.version_decisions
        assert reported.choice == ParentVersionChoice.CHILD_VERSION
        assert reported.chosen_dataset_version_id == matching
        assert reported.passed_over_dataset_version_ids == (newer,)


def test_a_retracted_matching_release_loses_to_a_live_newer_one(
    engine, a_dataset_version
):
    """Upstream saying do not use this outranks a shared publication date."""
    with Session(engine) as session:
        child = store(
            session,
            a_dataset_version,
            "abrupt-4xCO2",
            parent=a_parent("piControl"),
            version="20190710",
        )
        matching = a_dataset_version(
            session, experiment="piControl", version="20190710"
        )
        retracted = session.get(DatasetVersion, matching)
        assert retracted is not None
        retracted.retracted = True
        newer = another_version(session, matching, "20210901")
        session.commit()

        result = resolve_parent_links(session, [child])
        session.commit()

        link = result.linked[0]
        assert link.parent_dataset_version_id == newer
        assert link.version_choice == ParentVersionChoice.DEFAULTED


def test_the_link_holds_the_dataset_as_well_as_the_version(engine, a_dataset_version):
    """
    The dataset is a fact and the version is a choice, so both are stored

    The invariant that one belongs to the other holds by construction here, which is
    the only place links are written.
    """
    with Session(engine) as session:
        child = store(
            session,
            a_dataset_version,
            "abrupt-4xCO2",
            parent=a_parent("piControl"),
            version="20190406",
        )
        first = a_dataset_version(session, experiment="piControl", version="20190410")
        another_version(session, first, "20200828")
        session.commit()

        result = resolve_parent_links(session, [child])
        session.commit()

        link = result.linked[0]
        chosen = session.get(DatasetVersion, link.parent_dataset_version_id)
        assert chosen is not None
        assert chosen.dataset_id == link.parent_dataset_id


def test_an_injected_chooser_overrides_the_default(engine, a_dataset_version):
    """
    Which is why the chooser is a parameter: our rule is a rule, not a reading

    A caller who knows which release their analysis rests on says so, and the link is
    labelled as theirs rather than as a default.
    """
    with Session(engine) as session:
        child = store(
            session,
            a_dataset_version,
            "abrupt-4xCO2",
            parent=a_parent("piControl"),
            version="20261008",
        )
        first = a_dataset_version(session, experiment="piControl", version="20200101")
        another_version(session, first, "20300101")
        session.commit()

        def oldest(child_version, candidates):
            return VersionMatchOutcome(
                parent_version=min(candidates, key=lambda one: one.version)
            )

        result = resolve_parent_links(session, [child], version_chooser=oldest)
        session.commit()

        assert result.version_decisions == ()
        assert len(result.linked) == 1
        assert result.linked[0].parent_dataset_version_id == first
        assert result.linked[0].version_choice == ParentVersionChoice.CHOSEN


def test_a_chooser_which_declines_leaves_the_row_unresolved(engine, a_dataset_version):
    """
    A chooser of your own may refuse where ours will not

    If which release an analysis rests on matters, "I cannot tell" beats any rule. The
    chain stops and says why, with nothing written to undo.
    """
    with Session(engine) as session:
        child = store(
            session,
            a_dataset_version,
            "abrupt-4xCO2",
            parent=a_parent("piControl"),
            version="20261008",
        )
        first = a_dataset_version(session, experiment="piControl", version="20200101")
        second = another_version(session, first, "20300101")
        session.commit()

        def refuse(child_version, candidates):
            return VersionMatchOutcome(
                candidates=tuple(candidates), reason="I will not guess"
            )

        result = resolve_parent_links(session, [child], version_chooser=refuse)
        session.commit()

        assert result.linked == ()
        assert result.version_decisions == ()
        (reported,) = result.ambiguous_versions
        assert sorted(reported.candidate_dataset_version_ids) == sorted([first, second])
        assert "I will not guess" in reported.reason

        (held,) = session.exec(select(DatasetVersionParent)).all()
        assert held.resolution == ParentResolution.UNRESOLVED
        assert session.exec(select(DatasetParentLink)).all() == []


def test_a_walk_climbs_through_a_defaulted_version_and_warns_once(
    engine, a_dataset_version
):
    """
    The regression for the live CMIP6 failure: a republished parent stopped the walk

    It has to climb instead. And a walk over one simulation's many datasets has to say
    so once rather than once per link, or the warning gets silenced.
    """
    with Session(engine) as session:
        children = [
            store(
                session,
                a_dataset_version,
                "abrupt-4xCO2",
                parent=a_parent("piControl"),
                variable=variable,
                version="20190406",
            )
            for variable in ("tas", "rsdt")
        ]
        for variable in ("tas", "rsdt"):
            first = a_dataset_version(
                session,
                experiment="piControl",
                variable=variable,
                version="20190410",
            )
            another_version(session, first, "20200828")
        session.commit()

        with pytest.warns(ParentVersionDecidedWarning) as warned:
            result = find_parents(
                session,
                children,
                until_experiment=PICONTROL,
                check_until_exists=False,
                searcher=no_search,
            )

        assert result.hops >= 1
        assert len(result.linked) == 2
        assert len(warned) == 1
        message = str(warned[0].message)
        assert "piControl" in message
        # Both counts, and the sentence that stops the dates reading as nonsense.
        assert "0 matched the child's own version string" in message
        assert "2 fell back to the newest release" in message
        assert "publication date" in message

        assert len(result.version_decisions) == 2
        assert {one.child_dataset_version_id for one in result.version_decisions} == {
            *children
        }
        chosen = {
            session.get(DatasetVersion, link.parent_dataset_version_id).version
            for link in result.linked
        }
        assert chosen == {"20200828"}
        # One terminal per chain, and both chains stopped at the same simulation.
        assert {str(one) for one in result.terminals} == {
            "CMIP7 UKESM1-3-LL piControl r1i1p1f1"
        }
        assert len(result.terminals) == len(children)


def test_a_walk_with_nothing_to_choose_does_not_warn(engine, a_dataset_version):
    """The warning has to stay rare enough to mean something."""
    with Session(engine) as session:
        child = store(
            session,
            a_dataset_version,
            "abrupt-4xCO2",
            parent=a_parent("piControl"),
            version="20190406",
        )
        a_dataset_version(session, experiment="piControl", version="20200101")
        session.commit()

        with warnings.catch_warnings():
            warnings.simplefilter("error", ParentVersionDecidedWarning)
            result = find_parents(
                session,
                [child],
                until_experiment=PICONTROL,
                check_until_exists=False,
                searcher=no_search,
            )

        assert len(result.linked) == 1
        assert result.version_decisions == []
