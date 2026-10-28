"""
Writing a document's files and routes into the database

The end-to-end tests here go through a real recorded ESGF-NG response rather than a
hand-built payload, because the shape of that response is the thing most likely to
be misread.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest
from sqlmodel import Session, select

from esmporium.datasets import AccessService, ContainerFormat
from esmporium.db import (
    AmbiguousDataAccessError,
    DataAccess,
    DataNode,
    Dataset,
    File,
    FileFactsChangedWarning,
    UnknownRouteFileError,
    file_fact_differences,
    ingest_parsed_documents,
    save_data_access_payload,
)
from esmporium.search import (
    ESGFNG_CMIP6_FACADE_PARAMETERS,
    STAC_FORMAT_TAG,
    DataAccessInfo,
    DataAccessPayload,
    DataNodeInfo,
    DatasetFacets,
    ESGFNGResultParser,
    FileInfo,
    ParsedDocument,
    SearchAPIESGFNGSTAC,
    SearchAPIFacade,
    build_transient_retrying,
)

RECORDED_DIR = Path(__file__).parents[2] / "test-data" / "search"


def _west_cmip6_documents() -> tuple[ParsedDocument, ...]:
    """
    Parse the recorded west CMIP6 response

    West is the one recorded deployment that publishes whole-version routes
    alongside its files, so it exercises both branches of the model.
    """
    facade = SearchAPIFacade(
        parameters=ESGFNG_CMIP6_FACADE_PARAMETERS,
        search_api=SearchAPIESGFNGSTAC("recorded.example", build_transient_retrying(1)),
        result_parser=ESGFNGResultParser(),
    )
    raw = json.loads((RECORDED_DIR / "esgf-ng-stac-cmip6-west-search.json").read_text())

    return facade.parse_search_results(raw)


def _a_file_info(filename: str = "tas_Amon_185001-194912.nc") -> FileInfo:
    return FileInfo(
        filename=filename,
        size=123,
        checksum="ab" * 32,
        checksum_type="SHA256",
        tracking_id="hdl:21.14100/abc",
        local_path=f"CMIP6/x/{filename}",
    )


def _a_route(
    *,
    filename: str | None,
    url: str,
    container_format: ContainerFormat = ContainerFormat.NETCDF,
) -> DataAccessInfo:
    return DataAccessInfo(
        filename=filename,
        data_node="ceda.ac.uk",
        service=AccessService.HTTPS,
        container_format=container_format,
        url=url,
        raw_json="{}",
    )


def test_a_recorded_stac_document_becomes_files_and_both_kinds_of_route(engine):
    """
    The real west CMIP6 item: one file, three routes, two of them whole-version

    Two of the three routes serve the whole dataset version, which is exactly why a
    route cannot simply be a child of a file.
    """
    documents = _west_cmip6_documents()

    with Session(engine) as session:
        ingested = ingest_parsed_documents(session, documents)
        session.commit()

        files = session.exec(select(File)).all()
        routes = session.exec(select(DataAccess)).all()

        assert files
        assert routes
        # Every file is a file of the version its routes are for.
        assert all(stored.dataset_version_id for stored in files)

        whole_version = [route for route in routes if route.file_id is None]
        per_file = [route for route in routes if route.file_id is not None]
        assert whole_version
        assert per_file
        assert {route.container_format for route in whole_version} == {
            ContainerFormat.KERCHUNK,
            ContainerFormat.HTML,
        }
        # Each per-file route points at a file of its own version.
        by_id = {stored.id: stored for stored in files}
        for route in per_file:
            assert by_id[route.file_id].dataset_version_id == route.dataset_version_id

    # The handles come back for the next step (ancestry) to use.
    assert any(document.files for document in ingested)
    assert any(document.accesses for document in ingested)


def test_reingesting_a_recorded_document_writes_nothing_new(engine):
    """
    A second ingest reuses every file and route rather than duplicating them

    The whole-version routes are the interesting half: their `file_id` is NULL, and
    SQLite counts NULLs as distinct, so an identity which mentioned `file_id` would
    have re-inserted them here on every single ingest.
    """
    documents = _west_cmip6_documents()

    with Session(engine) as session:
        ingest_parsed_documents(session, documents)
        session.commit()
        first = (
            len(session.exec(select(File)).all()),
            len(session.exec(select(DataAccess)).all()),
        )

        ingest_parsed_documents(session, documents)
        session.commit()
        second = (
            len(session.exec(select(File)).all()),
            len(session.exec(select(DataAccess)).all()),
        )

    assert first == second


def test_ingested_versions_line_up_with_the_documents_datasets(engine):
    """
    `versions[i]` is the version of `parsed.datasets[i]`

    Pinned because the Solr file step leans on it to work out which variable each of
    a CMIP5 bundle's versions is for, without a database lookup. Checked by walking
    back from the stored version to its dataset and comparing the facet the
    alignment is used for.
    """
    documents = _west_cmip6_documents()

    with Session(engine) as session:
        ingested = ingest_parsed_documents(session, documents)
        session.commit()

        assert ingested
        for document in ingested:
            assert len(document.versions) == len(document.parsed.datasets)
            for facets, version in zip(
                document.parsed.datasets, document.versions, strict=True
            ):
                dataset = session.get(Dataset, version.dataset_id)
                assert dataset is not None
                assert dataset.variable == facets.variable
                assert dataset.id_project_specific == facets.id_project_specific


def test_two_nodes_serving_one_file_share_the_file_row(engine):
    """
    A file is node-independent, so two routes to it are two rows against one file

    This is the point of `File` not carrying a node: a filename and a checksum
    describe the bytes, which are the same wherever they are served from.
    """
    with Session(engine) as session:
        saved = save_data_access_payload(
            session,
            1,
            DataAccessPayload(
                files=(_a_file_info(),),
                accesses=(
                    _a_route(
                        filename="tas_Amon_185001-194912.nc",
                        url="https://ceda.example/tas.nc",
                    ),
                    DataAccessInfo(
                        filename="tas_Amon_185001-194912.nc",
                        data_node="esgf.nci.org.au",
                        service=AccessService.HTTPS,
                        container_format=ContainerFormat.NETCDF,
                        url="https://nci.example/tas.nc",
                        raw_json="{}",
                    ),
                ),
            ),
        )
        session.commit()

        assert len(saved.files) == 1
        assert len(saved.accesses) == 2
        assert {route.file_id for route in saved.accesses} == {saved.files[0].id}
        # A node seen only on a route still gets its own row.
        assert {node.data_node for node in session.exec(select(DataNode)).all()} == {
            "ceda.ac.uk",
            "esgf.nci.org.au",
        }


def test_a_route_cannot_be_given_another_versions_file(engine):
    """
    The invariant on `DataAccess.file_id`, which holds by construction

    A route's file is resolved only among the files of the version the route is for,
    so there is no path by which version 2 could be handed version 1's file. Version
    2 asking for a filename only version 1 has is therefore an unknown file, not a
    cross-version link -- and the error says so, because "the same filename under
    another version is a different file" is exactly the confusion to head off.

    Scoping the lookup is also required rather than merely tidy: that filename
    legitimately exists under both versions in general, so an unfiltered lookup by
    name would find two rows and raise.
    """
    with Session(engine) as session:
        save_data_access_payload(session, 1, DataAccessPayload(files=(_a_file_info(),)))
        session.commit()

        with pytest.raises(UnknownRouteFileError, match="dataset version 2"):
            save_data_access_payload(
                session,
                2,
                DataAccessPayload(
                    accesses=(
                        _a_route(
                            filename="tas_Amon_185001-194912.nc",
                            url="https://ceda.example/tas.nc",
                        ),
                    )
                ),
            )

        # Version 1's file is untouched, and no route was written for version 2.
        assert len(session.exec(select(File)).all()) == 1


def test_a_route_naming_a_file_nobody_has_is_refused(engine):
    """
    A route and its file are parsed together, so this means they got separated

    Loud rather than stored with a NULL `file_id`, because silently demoting a
    file-level route to a whole-version one would misreport what the node offers.
    """
    with Session(engine) as session:
        with pytest.raises(UnknownRouteFileError, match="has no such file"):
            save_data_access_payload(
                session,
                1,
                DataAccessPayload(
                    accesses=(
                        _a_route(
                            filename="tas_Amon_185001-194912.nc",
                            url="https://ceda.example/tas.nc",
                        ),
                    )
                ),
            )


def test_a_cmip5_bundle_carrying_files_is_refused(engine):
    """
    A document mapping to many versions cannot say which one a file belongs to

    This is the CMIP5 Solr case. Rather than guessing, we refuse, and the Solr file
    step fills these in by asking one variable at a time.
    """
    facets = [
        DatasetFacets(
            id_project_specific="cmip5.output1.BCC.bcc-csm1-1.rcp45.mon.atmos.Amon.r1i1p1",
            project="CMIP5",
            model="bcc-csm1-1",
            institution="BCC",
            experiment="rcp45",
            variant_label="r1i1p1",
            variable=variable,
            reporting_interval="mon",
            grid_label=None,
            processing_id="Amon",
        )
        for variable in ("tas", "pr")
    ]
    bundle = ParsedDocument(
        id_project_specific="cmip5.output1.BCC.bcc-csm1-1.rcp45.mon.atmos.Amon.r1i1p1",
        datasets=tuple(facets),
        version="20120710",
        is_latest=True,
        retracted=False,
        nodes=(DataNodeInfo("esgf.ceda.ac.uk"),),
        esgf_doc_id="a-bundle|esgf.ceda.ac.uk",
        raw_json="{}",
        raw_docs_format_tag=STAC_FORMAT_TAG,
        files=(_a_file_info(),),
    )

    with Session(engine) as session:
        with pytest.raises(AmbiguousDataAccessError, match=r"\['pr', 'tas'\]"):
            ingest_parsed_documents(session, [bundle])


def test_a_files_scalars_are_refreshed_on_a_later_ingest(engine):
    """
    The later answer wins, and we say so rather than swapping it in quietly

    Keeping the newer value is the right call -- holding a checksum we have been told
    is wrong is worse than holding the new one, and there is only one row to hold it
    in. But the same filename under the same version is supposed to describe the same
    bytes, so being told otherwise is a finding, and the value we drop cannot be
    recovered: a file document's own JSON is stored nowhere.
    """
    with Session(engine) as session:
        save_data_access_payload(session, 1, DataAccessPayload(files=(_a_file_info(),)))
        session.commit()

        corrected = FileInfo(
            filename="tas_Amon_185001-194912.nc",
            size=456,
            checksum="cd" * 32,
            checksum_type="SHA256",
            tracking_id="hdl:21.14100/abc",
            local_path="CMIP6/x/tas_Amon_185001-194912.nc",
        )
        with pytest.warns(FileFactsChangedWarning) as warned:
            save_data_access_payload(session, 1, DataAccessPayload(files=(corrected,)))
        session.commit()

        stored = session.exec(select(File)).one()
        assert (stored.size, stored.checksum) == (456, "cd" * 32)

    # Both values, so the one we dropped is at least in the log.
    said = str(warned[0].message)
    assert "we hold 123" in said
    assert "this document says 456" in said
    assert "ab" * 32 in said
    assert "cd" * 32 in said


def test_a_fact_a_document_is_silent_about_is_kept(engine):
    """
    Solr publishes no DRS path, and a Solr file step must not erase STAC's

    The two APIs are silent about different things, and CMIP6 is served by both, so
    this is an ordinary sequence rather than a corner case: record a version from
    STAC, fill in file access from Solr, and the `local_path` STAC taught us is still
    there. Reading Solr's silence as "the path is nothing" would have dropped it,
    with nothing to say it had.
    """
    with Session(engine) as session:
        save_data_access_payload(session, 1, DataAccessPayload(files=(_a_file_info(),)))
        session.commit()

        # Shaped the way a Solr file document arrives: no DRS path, everything else
        # asserted and agreeing.
        from_solr = FileInfo(
            filename="tas_Amon_185001-194912.nc",
            size=123,
            checksum="ab" * 32,
            checksum_type="SHA256",
            tracking_id="hdl:21.14100/abc",
            local_path=None,
        )
        save_data_access_payload(session, 1, DataAccessPayload(files=(from_solr,)))
        session.commit()

        stored = session.exec(select(File)).one()

    assert stored.local_path == "CMIP6/x/tas_Amon_185001-194912.nc"


def test_recording_the_same_facts_twice_says_nothing(engine):
    """
    Which is every re-run of a search, so a warning here would be noise, not news

    The check has to be quiet for the case that happens constantly, or nobody will
    read it on the day it fires.
    """
    with Session(engine) as session:
        save_data_access_payload(session, 1, DataAccessPayload(files=(_a_file_info(),)))
        session.commit()

        with warnings.catch_warnings():
            warnings.simplefilter("error", FileFactsChangedWarning)
            save_data_access_payload(
                session, 1, DataAccessPayload(files=(_a_file_info(),))
            )
            session.commit()


def test_a_disagreement_names_the_node_the_document_came_from(engine):
    """
    Read off the routes, because a `File` has no node of its own

    A `File` is node-independent by design -- the point of the table -- so the only
    way to say who told us this is to look at the routes travelling with it. Without
    that the warning says a file changed but not who says so, which is most of what
    somebody raising it upstream needs.
    """
    with Session(engine) as session:
        save_data_access_payload(
            session,
            1,
            DataAccessPayload(
                files=(_a_file_info(),),
                accesses=(
                    _a_route(
                        filename="tas_Amon_185001-194912.nc",
                        url="https://ceda.ac.uk/x/tas_Amon_185001-194912.nc",
                    ),
                ),
            ),
        )
        session.commit()

        disagreeing = FileInfo(
            filename="tas_Amon_185001-194912.nc",
            size=999,
            checksum="ab" * 32,
            checksum_type="SHA256",
            tracking_id="hdl:21.14100/abc",
            local_path="CMIP6/x/tas_Amon_185001-194912.nc",
        )
        other_node = DataAccessInfo(
            filename="tas_Amon_185001-194912.nc",
            data_node="esgf.nci.org.au",
            service=AccessService.HTTPS,
            container_format=ContainerFormat.NETCDF,
            url="https://esgf.nci.org.au/x/tas_Amon_185001-194912.nc",
            raw_json="{}",
        )
        with pytest.warns(FileFactsChangedWarning, match="esgf.nci.org.au"):
            save_data_access_payload(
                session,
                1,
                DataAccessPayload(files=(disagreeing,), accesses=(other_node,)),
            )


@pytest.mark.parametrize(
    ("held", "asserted", "expected", "why"),
    [
        pytest.param(
            123, 456, {"size": (123, 456)}, "two values, two answers", id="differs"
        ),
        pytest.param(123, 123, {}, "the same value asserted again", id="agrees"),
        pytest.param(
            123,
            None,
            {},
            "this document is silent, which asserts nothing",
            id="asserted-is-silent",
        ),
        pytest.param(
            None,
            456,
            {},
            "we held nothing, so there is nothing to disagree with",
            id="held-is-silent",
        ),
    ],
)
def test_what_counts_as_a_disagreement(held, asserted, expected, why):
    """
    Silence is not a disagreement, in either direction

    The rule the whole check rests on: a `None` means "this document does not say",
    not "the answer is nothing". Get it wrong in one direction and every Solr file
    warns about the path it never had; wrong in the other and Solr erases STAC's.
    """
    stored = File(dataset_version_id=1, filename="tas.nc", size=held)
    info = FileInfo(
        filename="tas.nc",
        size=asserted,
        checksum=None,
        checksum_type=None,
        tracking_id=None,
        local_path=None,
    )

    assert file_fact_differences(stored, info) == expected, why
