"""
Tests of where a downloaded file goes on disk
"""

from pathlib import Path, PurePosixPath

import pytest

from esmporium.downloads import (
    FileTarget,
    UnsafeDownloadPathError,
    esmporium_layout,
    local_path,
)

CMIP7 = FileTarget(
    id_project_specific="CMIP7.CMIP.CCCma.CanESM5-1.1pctCO2.r1i1p2f1.Amon.tas.gn",
    version="20261018",
    filename="tas_mon_CanESM5-1_1pctCO2_r1i1p2f1_185001-190012.nc",
)

CMIP6 = FileTarget(
    id_project_specific="CMIP6.CMIP.MOHC.UKESM1-0-LL.historical.r15i1p1f2.Amon.tas.gn",
    version="20200623",
    filename="tas_Amon_UKESM1-0-LL_historical_r15i1p1f2_gn_185001-194912.nc",
)

# One CMIP5 `id_project_specific` covers several variables, which is the case that
# makes a single layout non-obvious.
CMIP5 = FileTarget(
    id_project_specific="cmip5.output1.NCAR.CCSM4.rcp45.mon.atmos.Amon.r1i1p1",
    version="20120413",
    filename="tas_Amon_CCSM4_rcp45_r1i1p1_200601-210012.nc",
)


@pytest.mark.parametrize(
    "target", (CMIP5, CMIP6, CMIP7), ids=("cmip5", "cmip6", "cmip7")
)
def test_the_layout_works_for_every_project(target):
    """One layout, no project-specific branching, no `unknown` directories"""
    result = esmporium_layout(target)

    assert not result.is_absolute()
    # The dotted id became directories, the version gained its `v`, and the file is
    # the leaf.
    assert result.parts[:-2] == tuple(target.id_project_specific.split("."))
    assert result.parts[-2] == f"v{target.version}"
    assert result.name == target.filename
    assert "unknown" not in result.parts


def test_two_cmip5_variables_share_a_directory_without_colliding():
    """
    The CMIP5 case: one id, several variables, and no overwriting

    Their `Dataset` rows differ by `variable`, but their `id_project_specific` and
    `version` are the same, so they land in one directory. Their filenames carry the
    variable, so they sit alongside one another -- which is what CMIP5's own DRS does
    above the variable level anyway.
    """
    tas = CMIP5
    rsdt = FileTarget(
        id_project_specific=CMIP5.id_project_specific,
        version=CMIP5.version,
        filename="rsdt_Amon_CCSM4_rcp45_r1i1p1_200601-210012.nc",
    )

    tas_path = esmporium_layout(tas)
    rsdt_path = esmporium_layout(rsdt)

    assert tas_path.parent == rsdt_path.parent
    assert tas_path != rsdt_path


def test_local_path_is_absolute_and_under_the_root(tmp_path):
    """The whole point of `root`: everything lands beneath it"""
    result = local_path(tmp_path, CMIP7)

    assert result.is_absolute()
    assert tmp_path.resolve() in result.parents


def test_local_path_does_not_create_anything(tmp_path):
    """
    Working out a path is not the same as deciding to write there

    Whatever writes the bytes makes the directory, because it is the thing that knows
    whether it is really going to.
    """
    result = local_path(tmp_path, CMIP7)

    assert not result.exists()
    assert not result.parent.exists()


@pytest.mark.parametrize(
    "field, value, blamed, culprit, why",
    (
        ("version", "..", "version", "..", "is not a name"),
        ("version", "/etc", "version", "/etc", "contains a path separator"),
        ("version", "a/b", "version", "a/b", "contains a path separator"),
        ("version", "", "version", "", "is not a name"),
        (
            "version",
            " 20261018",
            "version",
            " 20261018",
            "has leading or trailing whitespace",
        ),
        (
            "filename",
            "../../../etc/passwd",
            "filename",
            "../../../etc/passwd",
            "contains a path separator",
        ),
        ("filename", "a/b.nc", "filename", "a/b.nc", "contains a path separator"),
        ("filename", ".", "filename", ".", "is not a name"),
        ("filename", "", "filename", "", "is not a name"),
        ("filename", "tas.nc\x00", "filename", "tas.nc\x00", "contains a null byte"),
        # A dotted id is blamed one segment at a time, so the culprit is the segment
        # rather than the whole value: the dots are the layout's own separator.
        (
            "id_project_specific",
            "CMIP7./etc/passwd",
            "id_project_specific segment",
            "/etc/passwd",
            "contains a path separator",
        ),
        (
            "id_project_specific",
            "CMIP7...CMIP",
            "id_project_specific segment",
            "",
            "is not a name",
        ),
        (
            "id_project_specific",
            "CMIP7....",
            "id_project_specific segment",
            "",
            "is not a name",
        ),
    ),
)
def test_a_component_which_would_escape_is_refused(field, value, blamed, culprit, why):
    """
    Refused, not sanitised, and the refusal says which value and which check

    Quietly rewriting `../../etc` into something harmless would mean writing a file
    somewhere the record did not name and then recording that we had downloaded it,
    which is a quieter and worse failure than stopping.

    `blamed`, `culprit` and `why` are asserted rather than just the error type, because
    every case here raises the same error and the message is what someone acts on. A
    value refused for the wrong reason -- or a refusal blaming the wrong component --
    sends them looking at the wrong part of a published record, and would otherwise be
    invisible.
    """
    target = FileTarget(
        **{
            "id_project_specific": "CMIP7.CMIP.CCCma",
            "version": "20261018",
            "filename": "tas.nc",
            field: value,
        }
    )

    with pytest.raises(UnsafeDownloadPathError) as excinfo:
        esmporium_layout(target)

    assert excinfo.value.field == blamed
    assert excinfo.value.why == why
    assert excinfo.value.value == culprit


def test_a_dotted_id_is_checked_segment_by_segment():
    """
    A separator inside one segment is caught, not turned into two directories

    The dots are the layout's own separator, so `a.b` is two legitimate directories
    while `a/b` inside one segment is not.
    """
    with pytest.raises(UnsafeDownloadPathError) as caught:
        esmporium_layout(
            FileTarget(
                id_project_specific="CMIP7.CMIP/sneaky.CCCma",
                version="20261018",
                filename="tas.nc",
            )
        )

    assert caught.value.field == "id_project_specific segment"


def test_a_custom_layout_cannot_escape_the_root(tmp_path):
    """
    The containment check is on the *result*, so an injected builder cannot bypass it

    `PathBuilder` is a seam for reproducing a publisher's real DRS, and a builder
    supplied by a caller is not necessarily one that validates.
    """

    def escaping_layout(target):
        return PurePosixPath("..") / ".." / "somewhere-else" / target.filename

    with pytest.raises(UnsafeDownloadPathError) as caught:
        local_path(tmp_path, CMIP7, layout=escaping_layout)

    assert "does not stay under the download root" in str(caught.value)


def test_an_absolute_layout_result_is_refused(tmp_path):
    """An absolute path from a builder would ignore `root` entirely"""

    def absolute_layout(target):
        return PurePosixPath("/etc") / target.filename

    with pytest.raises(UnsafeDownloadPathError):
        local_path(tmp_path, CMIP7, layout=absolute_layout)


def test_a_custom_layout_that_stays_put_is_accepted(tmp_path):
    """The seam works for its intended use"""

    def flat_layout(target):
        return PurePosixPath(target.filename)

    result = local_path(tmp_path, CMIP7, layout=flat_layout)

    assert result == tmp_path.resolve() / CMIP7.filename


def test_the_root_may_be_relative_or_use_a_tilde():
    """`root` is resolved, so a relative or `~` root still gives an absolute path"""
    result = local_path(Path("downloads"), CMIP7)

    assert result.is_absolute()
    assert "downloads" in result.parts
