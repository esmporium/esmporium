"""
Tests of getting parent facts out of file headers
"""

from __future__ import annotations

import socket

import httpx
from sqlmodel import Session, select
from tests.conftest import CMIP5_FILE_SIZE

from esmporium.datasets import AccessService, ContainerFormat
from esmporium.db import (
    DEFAULT_MAX_FILE_CANDIDATES,
    SOURCE_PRECEDENCE,
    DataAccess,
    DataNode,
    DatasetVersionParent,
    File,
    ParentInfoSource,
    ParentSimulation,
    aggregate_header_read_health,
    build_header_facts_provider,
    plan_header_reads,
    preferred_parent_info,
)
from esmporium.db.results_to_database import flushed_id
from esmporium.fixes import PARENT_FIXES, ParentFix, fixes_from_mapping
from esmporium.search import ParentInfo

NODE = "esgf.example.org"


def served_range(prefix: bytes) -> str:
    """
    Render the `Content-Range` a node answers a range request with

    Parameters
    ----------
    prefix
        The bytes being served

    Returns
    -------
    :
        The header value
    """
    return f"bytes 0-{len(prefix) - 1}/{CMIP5_FILE_SIZE}"


def _a_file_with_a_route(
    session: Session,
    version_id: int,
    filename: str,
    *,
    size: int = 1000,
    node: str = NODE,
) -> int:
    """Store one file of a version, with an HTTPS route to it, and give back its id"""
    found = session.exec(select(DataNode).where(DataNode.data_node == node)).first()
    if found is None:
        found = DataNode(data_node=node)
        session.add(found)
        session.flush()

    each = File(dataset_version_id=version_id, filename=filename, size=size)
    session.add(each)
    session.flush()

    session.add(
        DataAccess(
            dataset_version_id=version_id,
            file_id=each.id,
            data_node_id=found.id,
            service=AccessService.HTTPS,
            container_format=ContainerFormat.NETCDF,
            url=f"https://{node}/thredds/fileServer/{filename}",
            raw_json="{}",
        )
    )
    session.flush()

    return flushed_id(each)


# The Solr facet spelling, which is what `Dataset.model` holds -- and deliberately not
# the `bcc-csm1-1-m` the recorded file's own `model_id` says, so that a test of the
# wiring would fail if the parent's model were taken from the file.
CMIP5_MODEL_AS_THE_INDEX_SPELLS_IT = "BCC-CSM1.1(m)"


def _cmip5_simulation(session, a_dataset_version, variables=("tas",), **facets):
    """Store one CMIP5 simulation's datasets, one per variable, each with a file"""
    version_ids = []
    for variable in variables:
        version_id = a_dataset_version(
            session,
            project="CMIP5",
            model=CMIP5_MODEL_AS_THE_INDEX_SPELLS_IT,
            experiment="abrupt4xCO2",
            variant_label="r1i1p1",
            variable=variable,
            grid_label=None,
            processing_id="Amon",
            **facets,
        )
        _a_file_with_a_route(session, version_id, f"{variable}_Amon.nc")
        version_ids.append(version_id)

    return version_ids


class TestPlanningWhatToRead:
    def test_one_simulation_needs_one_header_however_many_variables(
        self, engine, a_dataset_version
    ):
        """
        The death-spiral fix, as a plan

        A simulation is published as many datasets and they all have the same parent,
        so reading one file answers for all of them.
        """
        with Session(engine) as session:
            version_ids = _cmip5_simulation(
                session, a_dataset_version, variables=("tas", "rsut", "rlut", "rsdt")
            )
            session.commit()

            plan = plan_header_reads(session, version_ids)

        assert len(plan.to_read) == 1
        # And that one read is recorded against all four.
        assert set(plan.to_read[0].dataset_version_ids) == set(version_ids)

    def test_the_smallest_file_is_the_one_read(self, engine, a_dataset_version):
        """
        Its header is as good as any other's, and it costs least if a node floods us
        """
        with Session(engine) as session:
            version_id = a_dataset_version(session, project="CMIP5", grid_label=None)
            _a_file_with_a_route(session, version_id, "big.nc", size=900_000_000)
            small = _a_file_with_a_route(session, version_id, "small.nc", size=1_000)
            session.commit()

            plan = plan_header_reads(session, [version_id])

        assert plan.to_read[0].file_id == small
        assert plan.to_read[0].filename == "small.nc"

    def test_a_version_that_already_has_facts_is_left_alone(
        self, engine, a_dataset_version
    ):
        """
        Re-reading a header costs a request for an answer we hold
        """
        with Session(engine) as session:
            version_ids = _cmip5_simulation(session, a_dataset_version)
            session.add(
                DatasetVersionParent(
                    dataset_version_id=version_ids[0],
                    source=ParentInfoSource.FILE_HEADER,
                    resolution="unresolved",
                    parent_experiment="piControl",
                )
            )
            session.commit()

            plan = plan_header_reads(session, version_ids)

        assert plan.to_read == ()
        assert plan.reusable == {}

    def test_a_sibling_variable_reuses_a_stored_answer_for_free(
        self, engine, a_dataset_version
    ):
        """
        The fast path, and the thing that makes a whole archive affordable

        If the parent of a simulation's `tas` is stored, its `tos` has the same parent
        -- so the second variable needs no request at all.
        """
        with Session(engine) as session:
            tas, tos = _cmip5_simulation(
                session, a_dataset_version, variables=("tas", "tos")
            )
            session.add(
                DatasetVersionParent(
                    dataset_version_id=tas,
                    source=ParentInfoSource.FILE_HEADER,
                    resolution="unresolved",
                    parent_experiment="piControl",
                    parent_model="bcc-csm1-1-m",
                )
            )
            session.commit()

            plan = plan_header_reads(session, [tos])

        assert plan.to_read == ()
        assert set(plan.reusable) == {tos}

    def test_a_stored_answer_for_a_different_simulation_is_not_reused(
        self, engine, a_dataset_version
    ):
        """
        The jump is within one run, not across runs of the same model
        """
        with Session(engine) as session:
            (ours,) = _cmip5_simulation(session, a_dataset_version)
            other = a_dataset_version(
                session,
                project="CMIP5",
                model="bcc-csm1-1-m",
                experiment="abrupt4xCO2",
                # A different variant, so a different simulation.
                variant_label="r2i1p1",
                grid_label=None,
                processing_id="Amon",
            )
            session.add(
                DatasetVersionParent(
                    dataset_version_id=other,
                    source=ParentInfoSource.FILE_HEADER,
                    resolution="unresolved",
                    parent_experiment="piControl",
                )
            )
            session.commit()

            plan = plan_header_reads(session, [ours])

        assert plan.reusable == {}
        assert len(plan.to_read) == 1

    def test_a_simulation_with_no_fetchable_route_is_reported_not_raised(
        self, engine, a_dataset_version
    ):
        """
        One unreachable simulation must not stop the rest
        """
        with Session(engine) as session:
            version_id = a_dataset_version(
                session,
                project="CMIP5",
                model="bcc-csm1-1-m",
                grid_label=None,
            )
            # A file, but published only over OPeNDAP -- a landing page, not bytes.
            node = DataNode(data_node=NODE)
            session.add(node)
            session.flush()
            each = File(dataset_version_id=version_id, filename="tas.nc", size=10)
            session.add(each)
            session.flush()
            session.add(
                DataAccess(
                    dataset_version_id=version_id,
                    file_id=each.id,
                    data_node_id=node.id,
                    service=AccessService.OPENDAP,
                    container_format=ContainerFormat.HTML,
                    url=f"https://{NODE}/thredds/dodsC/tas.nc.html",
                    raw_json="{}",
                )
            )
            session.commit()

            plan = plan_header_reads(session, [version_id])

        assert plan.to_read == ()
        assert len(plan.unreadable) == 1
        assert plan.unreadable[0].model == "bcc-csm1-1-m"

    def test_nothing_asked_about_is_nothing_to_do(self, engine):
        with Session(engine) as session:
            plan = plan_header_reads(session, [])

        assert plan.to_read == ()
        assert plan.reusable == {}
        assert plan.unreadable == ()


class TestTheProvider:
    @staticmethod
    def _client(prefix: bytes, *, size: int, requests: list[str] | None = None):
        def handler(request: httpx.Request) -> httpx.Response:
            if requests is not None:
                requests.append(str(request.url))

            return httpx.Response(
                206,
                content=prefix,
                headers={"Content-Range": f"bytes 0-{len(prefix) - 1}/{size}"},
            )

        return httpx.Client(transport=httpx.MockTransport(handler))

    def test_one_read_answers_every_dataset_of_a_simulation(
        self, engine, a_dataset_version, cmip5_netcdf3_header
    ):
        """
        The headline claim, measured rather than asserted

        Four datasets, one HTTP request, four assertions -- every one of them pointing
        at the file the bytes really came from.
        """
        requests: list[str] = []

        with Session(engine) as session:
            version_ids = _cmip5_simulation(
                session, a_dataset_version, variables=("tas", "rsut", "rlut", "rsdt")
            )
            session.commit()

            with self._client(
                cmip5_netcdf3_header, size=CMIP5_FILE_SIZE, requests=requests
            ) as client:
                provider = build_header_facts_provider(
                    client=client, record_health=False
                )
                provider(session, version_ids)
                session.commit()

            rows = session.exec(select(DatasetVersionParent)).all()

        assert len(requests) == 1
        assert len(rows) == 4
        assert {row.parent_experiment for row in rows} == {"piControl"}
        assert {row.parent_variant_label for row in rows} == {"r1i1p1"}
        assert {row.source for row in rows} == {ParentInfoSource.FILE_HEADER}
        # Every row names the one file the bytes came from.
        assert len({row.file_id for row in rows}) == 1
        assert None not in {row.file_id for row in rows}

    def test_a_second_file_is_tried_when_the_first_ones_node_is_dead(
        self, engine, a_dataset_version, cmip5_netcdf3_header
    ):
        """
        The case that really happens, and the reason one file is not enough

        Live on 2026-11-05, `BCC-CSM1.1(m) abrupt4xCO2` publishes 110 files and the
        smallest is served only by a node that does not answer. A plan committed to
        that one file gave up on a simulation whose other 109 files read fine.
        """
        asked: list[str] = []

        with Session(engine) as session:
            version_id = a_dataset_version(
                session,
                project="CMIP5",
                model=CMIP5_MODEL_AS_THE_INDEX_SPELLS_IT,
                experiment="abrupt4xCO2",
                variant_label="r1i1p1",
                grid_label=None,
                processing_id="Amon",
            )
            # The smallest file is on a node that will not answer; a bigger one is on
            # a node that will.
            _a_file_with_a_route(
                session, version_id, "tiny_clim.nc", size=10, node="dead.example.org"
            )
            _a_file_with_a_route(
                session, version_id, "big.nc", size=900, node="alive.example.org"
            )
            session.commit()

            def handler(request: httpx.Request) -> httpx.Response:
                asked.append(str(request.url))
                if "dead" in str(request.url):
                    raise httpx.ConnectError(  # noqa: TRY003 - simulating a library error
                        "no route", request=request
                    ) from socket.gaierror(8)

                return httpx.Response(
                    206,
                    content=cmip5_netcdf3_header,
                    headers={"Content-Range": served_range(cmip5_netcdf3_header)},
                )

            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                build_header_facts_provider(client=client, record_health=False)(
                    session, [version_id]
                )
                session.commit()

            row = session.exec(select(DatasetVersionParent)).one()

        assert row.parent_experiment == "piControl"
        # The dead node was asked once, then the second file answered.
        assert sum("dead" in url for url in asked) == 1
        assert sum("alive" in url for url in asked) == 1
        # And the provenance names the file that really served the bytes.
        assert "big.nc" in next(url for url in asked if "alive" in url)

    def test_a_simulation_is_given_up_on_after_a_bounded_number_of_files(
        self, engine, a_dataset_version
    ):
        """
        The other half of the same judgement: bounded, or it is the death spiral
        """
        asked: list[str] = []

        with Session(engine) as session:
            version_id = a_dataset_version(
                session,
                project="CMIP5",
                model=CMIP5_MODEL_AS_THE_INDEX_SPELLS_IT,
                experiment="abrupt4xCO2",
                variant_label="r1i1p1",
                grid_label=None,
                processing_id="Amon",
            )
            for index in range(12):
                _a_file_with_a_route(
                    session,
                    version_id,
                    f"file{index}.nc",
                    size=index + 1,
                    node=f"dead{index}.example.org",
                )
            session.commit()

            def handler(request: httpx.Request) -> httpx.Response:
                asked.append(str(request.url))

                return httpx.Response(404)

            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                build_header_facts_provider(client=client, record_health=False)(
                    session, [version_id]
                )
                session.commit()

            assert session.exec(select(DatasetVersionParent)).all() == []

        assert len(asked) == DEFAULT_MAX_FILE_CANDIDATES

    def test_the_inferred_cmip5_model_is_the_one_a_search_will_match(
        self, engine, a_dataset_version, cmip5_netcdf3_header
    ):
        """
        End to end, with the two spellings deliberately different

        The recorded file says `model_id = "bcc-csm1-1-m"`; the stored dataset says
        `BCC-CSM1.1(m)`, which is what CMIP5's search index really calls that model.
        The parent has to get the second one, or the search built from it finds
        nothing and the failure reads as "this parent is not published".
        """
        with Session(engine) as session:
            version_ids = _cmip5_simulation(session, a_dataset_version)
            session.commit()

            with self._client(cmip5_netcdf3_header, size=CMIP5_FILE_SIZE) as client:
                build_header_facts_provider(client=client, record_health=False)(
                    session, version_ids
                )
                session.commit()

            row = session.exec(select(DatasetVersionParent)).one()

        assert row.parent_model == CMIP5_MODEL_AS_THE_INDEX_SPELLS_IT
        assert row.parent_project == "CMIP5"
        assert row.branch_time_in_parent == 240.0
        # CMIP5 publishes no units, so the branch time has no stated origin.
        assert row.parent_time_units is None

    def test_health_is_recorded_through_the_walks_own_session(
        self, engine, a_dataset_version, cmip5_netcdf3_header
    ):
        """
        Attempts land in `HeaderReadCallRecord`, written by the session we were handed

        Not by a session of the observer's own, and that is load-bearing rather than
        tidy. On SQLite a session takes the single write lock when its transaction
        opens -- `configure_sqlite_for_concurrency` uses `BEGIN IMMEDIATE` -- and sets
        a five-minute `busy_timeout`. A walk calls its facts provider while holding
        such a session, so an observer opening a second connection would not race it;
        it would stop dead for five minutes on every single attempt. This test passing
        quickly is the assertion.
        """
        with Session(engine) as session:
            version_ids = _cmip5_simulation(session, a_dataset_version)
            session.commit()

            with self._client(cmip5_netcdf3_header, size=CMIP5_FILE_SIZE) as client:
                build_header_facts_provider(client=client)(session, version_ids)
                session.commit()

        health = aggregate_header_read_health(engine)

        assert set(health) == {NODE}
        assert health[NODE].n_success == 1
        assert health[NODE].median_seconds is not None

    def test_what_the_nodes_did_survives_a_read_that_raises(
        self, engine, a_dataset_version
    ):
        """
        The evidence needed to work out *why* a read failed must not be lost with it

        A failure inside `read_headers` still leaves the attempts behind, because that
        is the only record of which nodes were tried and what they said.
        """
        with Session(engine) as session:
            version_ids = _cmip5_simulation(session, a_dataset_version)
            session.commit()

            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(503)

            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                build_header_facts_provider(
                    client=client,
                    # One attempt, so the test does not wait out a backoff.
                    max_workers=None,
                )(session, version_ids)
                session.commit()

        health = aggregate_header_read_health(engine)

        assert health[NODE].n_blocked >= 1
        assert health[NODE].n_success == 0

    def test_a_simulation_no_node_serves_leaves_the_others_recorded(
        self, engine, a_dataset_version, cmip5_netcdf3_header
    ):
        """
        One failure does not take the rest of the hop down with it

        The walk checks afterwards that everything it asked about got facts, and says
        what is missing per project -- so the failure surfaces there, with every other
        simulation's facts already stored.
        """
        with Session(engine) as session:
            good = _cmip5_simulation(session, a_dataset_version, variables=("tas",))
            bad = a_dataset_version(
                session,
                project="CMIP5",
                model="MIROC5",
                experiment="abrupt4xCO2",
                variant_label="r1i1p1",
                grid_label=None,
                processing_id="Amon",
            )
            _a_file_with_a_route(session, bad, "broken.nc")
            session.commit()

            served = f"bytes 0-{len(cmip5_netcdf3_header) - 1}/{CMIP5_FILE_SIZE}"

            def handler(request: httpx.Request) -> httpx.Response:
                if "broken" in str(request.url):
                    return httpx.Response(404)

                return httpx.Response(
                    206,
                    content=cmip5_netcdf3_header,
                    headers={"Content-Range": served},
                )

            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                build_header_facts_provider(client=client, record_health=False)(
                    session, [*good, bad]
                )
                session.commit()

            rows = session.exec(select(DatasetVersionParent)).all()

        assert [row.dataset_version_id for row in rows] == good


def _a_cmip5_dataset(session, a_dataset_version, model="INM-CM4"):
    """Store one CMIP5 dataset with a file and an HTTPS route, and give back its id"""
    version_id = a_dataset_version(
        session,
        project="CMIP5",
        model=model,
        experiment="abrupt4xCO2",
        variant_label="r1i1p1",
        variable="tas",
        grid_label=None,
        processing_id="Amon",
    )

    node = DataNode(data_node=NODE)
    session.add(node)
    session.flush()

    each = File(dataset_version_id=version_id, filename="tas_Amon.nc", size=1000)
    session.add(each)
    session.flush()

    session.add(
        DataAccess(
            dataset_version_id=version_id,
            file_id=each.id,
            data_node_id=node.id,
            service=AccessService.HTTPS,
            container_format=ContainerFormat.NETCDF,
            url=f"https://{NODE}/thredds/fileServer/tas_Amon.nc",
            raw_json="{}",
        )
    )
    session.flush()

    return version_id


def _serving(prefix: bytes, *, size: int = CMIP5_FILE_SIZE):
    """A client answering every range request with the given prefix"""
    return httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                206,
                content=prefix,
                headers={"Content-Range": f"bytes 0-{len(prefix) - 1}/{size}"},
            )
        )
    )


class TestApplyingAFixAlongsideAHeader:
    def test_both_rows_are_kept(self, engine, a_dataset_version, cmip5_netcdf3_header):
        """
        A fix does not replace the header; it sits beside it

        Which is the whole point of holding one row per source. What the file said
        stays visible, so a fix can be reviewed later against metadata that may since
        have been republished.
        """
        with Session(engine) as session:
            version_id = _a_cmip5_dataset(session, a_dataset_version)
            session.commit()

            with _serving(cmip5_netcdf3_header) as client:
                build_header_facts_provider(
                    client=client,
                    record_health=False,
                    fixes=fixes_from_mapping(PARENT_FIXES),
                )(session, [version_id])
                session.commit()

            rows = session.exec(select(DatasetVersionParent)).all()

        assert {row.source for row in rows} == {
            ParentInfoSource.FILE_HEADER,
            ParentInfoSource.FIX,
        }

    def test_the_reader_believes_the_fix(
        self, engine, a_dataset_version, cmip5_netcdf3_header
    ):
        """
        Precedence, which already put a fix above a header before this existed
        """
        with Session(engine) as session:
            version_id = _a_cmip5_dataset(session, a_dataset_version)
            session.commit()

            with _serving(cmip5_netcdf3_header) as client:
                build_header_facts_provider(
                    client=client,
                    record_health=False,
                    fixes=fixes_from_mapping(PARENT_FIXES),
                )(session, [version_id])
                session.commit()

            rows = session.exec(select(DatasetVersionParent)).all()
            preferred = preferred_parent_info(rows)

        assert preferred is not None
        assert preferred.source == ParentInfoSource.FIX
        assert preferred.parent_variant_label == "r1i1p1"
        assert SOURCE_PRECEDENCE.index(ParentInfoSource.FIX) < SOURCE_PRECEDENCE.index(
            ParentInfoSource.FILE_HEADER
        )

    def test_a_fix_lands_even_when_no_node_serves_the_header(
        self, engine, a_dataset_version
    ):
        """
        A simulation nothing serves a header for is one a fix may be the only answer for

        So the versions needing facts are worked out before the reads, not after them.
        """
        with Session(engine) as session:
            version_id = _a_cmip5_dataset(session, a_dataset_version)
            session.commit()

            dead = httpx.Client(
                transport=httpx.MockTransport(lambda request: httpx.Response(404))
            )
            with dead as client:
                build_header_facts_provider(
                    client=client,
                    record_health=False,
                    fixes=fixes_from_mapping(PARENT_FIXES),
                )(session, [version_id])
                session.commit()

            rows = session.exec(select(DatasetVersionParent)).all()

        assert [row.source for row in rows] == [ParentInfoSource.FIX]
        assert rows[0].parent_variant_label == "r1i1p1"

    def test_a_simulation_nobody_asked_about_is_not_touched(
        self, engine, a_dataset_version, cmip5_netcdf3_header
    ):
        """
        A fix for one model does not reach another
        """
        with Session(engine) as session:
            # A model with no fix registered.
            version_id = _a_cmip5_dataset(
                session, a_dataset_version, model="BCC-CSM1.1(m)"
            )
            session.commit()

            with _serving(cmip5_netcdf3_header) as client:
                build_header_facts_provider(
                    client=client,
                    record_health=False,
                    fixes=fixes_from_mapping(PARENT_FIXES),
                )(session, [version_id])
                session.commit()

            rows = session.exec(select(DatasetVersionParent)).all()

        assert [row.source for row in rows] == [ParentInfoSource.FILE_HEADER]

    def test_no_fixes_given_means_none_applied(
        self, engine, a_dataset_version, cmip5_netcdf3_header
    ):
        with Session(engine) as session:
            version_id = _a_cmip5_dataset(session, a_dataset_version)
            session.commit()

            with _serving(cmip5_netcdf3_header) as client:
                build_header_facts_provider(client=client, record_health=False)(
                    session, [version_id]
                )
                session.commit()

            rows = session.exec(select(DatasetVersionParent)).all()

        assert [row.source for row in rows] == [ParentInfoSource.FILE_HEADER]

    def test_a_fix_can_be_supplied_without_being_one_of_ours(
        self, engine, a_dataset_version, cmip5_netcdf3_header
    ):
        """
        The seam, used the way a user would use it

        Which is the reason it is a protocol and not a lookup in a module.
        """
        mine = ParentFix(
            info=ParentInfo(
                parent_project="CMIP5",
                parent_activity=None,
                parent_model="BCC-CSM1.1(m)",
                parent_experiment="piControl",
                parent_variant_label="r7i1p1",
                parent_time_units=None,
            ),
            reason="the published parent variant does not exist",
            evidence="confirmed by the modelling centre, 2026-11-05",
        )
        simulation = ParentSimulation(
            project="CMIP5",
            model="BCC-CSM1.1(m)",
            experiment="abrupt4xCO2",
            variant_label="r1i1p1",
        )

        with Session(engine) as session:
            version_id = _a_cmip5_dataset(
                session, a_dataset_version, model="BCC-CSM1.1(m)"
            )
            session.commit()

            with _serving(cmip5_netcdf3_header) as client:
                build_header_facts_provider(
                    client=client,
                    record_health=False,
                    fixes=fixes_from_mapping({simulation: mine}),
                )(session, [version_id])
                session.commit()

            rows = session.exec(select(DatasetVersionParent)).all()
            preferred = preferred_parent_info(rows)

        assert preferred is not None
        assert preferred.parent_variant_label == "r7i1p1"


def test_a_fix_is_not_reapplied_to_a_version_that_already_has_one(
    engine, a_dataset_version, cmip5_netcdf3_header
):
    """
    A second pass over the same datasets changes nothing and asks nothing

    Which is what makes a walk over a whole archive affordable to re-run.
    """
    with Session(engine) as session:
        version_id = _a_cmip5_dataset(session, a_dataset_version)
        session.commit()

        requests: list[str] = []

        served = f"bytes 0-{len(cmip5_netcdf3_header) - 1}/{CMIP5_FILE_SIZE}"

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(str(request.url))

            return httpx.Response(
                206,
                content=cmip5_netcdf3_header,
                headers={"Content-Range": served},
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            provider = build_header_facts_provider(
                client=client,
                record_health=False,
                fixes=fixes_from_mapping(PARENT_FIXES),
            )
            provider(session, [version_id])
            session.commit()
            after_first = len(requests)

            provider(session, [version_id])
            session.commit()

        rows = session.exec(select(DatasetVersionParent)).all()

    assert after_first == 1
    # The second pass asked for nothing at all.
    assert len(requests) == 1
    assert len(rows) == 2
