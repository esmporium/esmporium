"""
Where a downloaded file goes on our disk
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_SEPARATORS = ("/", "\\")
"""
Path separators a component may not contain

Both, whatever platform we are on: a value carrying a backslash is not something to
interpret differently on Linux than on Windows, it is something to refuse.
"""

_FORBIDDEN_COMPONENTS = frozenset({"", ".", ".."})
"""Components which are not names at all"""


class UnsafeDownloadPathError(ValueError):
    """
    Raised when a file's recorded identity will not make a safe path component

    Every part of a download's destination comes from a third party: ESGF publishes
    `id_project_specific`, `version` and `filename`, and we store what it published.
    A value containing `..`, an absolute path or a path separator would let a
    published record decide where on this machine we write, so the components are
    checked rather than cleaned.
    """

    def __init__(self, field: str, value: str, why: str) -> None:
        """
        Initialise the error

        Parameters
        ----------
        field
            Which part of the identity was unusable, e.g. `"filename"`

        value
            The value we were given

        why
            What is wrong with it, phrased to follow "because it "
        """
        self.field = field
        self.value = value
        self.why = why
        super().__init__(
            f"Cannot build a download path from {field}={value!r}, because it {why}. "
            "This value came from a search API and is stored as it was published, so "
            "either the API published something we did not expect or it has been "
            "edited since. If an ESGF API really publishes this, it is a bug in "
            "esmporium: please raise an issue at "
            "https://github.com/esmporium/esmporium/issues quoting the message above."
        )


@dataclass(frozen=True)
class FileTarget:
    """
    What a layout needs to know to decide where one file goes

    Deliberately not a [File][esmporium.db.schema.File] row. A layout is a pure
    function of the file's published identity, so it takes only that identity -- which
    keeps [esmporium.downloads][] free of any dependency on the database, and makes a
    layout trivial to test without one.
    """

    id_project_specific: str
    """
    The dataset's identifier in its project's own language

    See
    [Dataset.id_project_specific][esmporium.db.schema.Dataset.id_project_specific],
    e.g. `CMIP7.CMIP.CCCma.CanESM5-1.1pctCO2.r1i1p2f1.Amon.tas.gn`. Dotted, which is
    what makes it turn into a directory tree.
    """

    version: str
    """
    The dataset version string, as ESGF reports it

    See [DatasetVersion.version][esmporium.db.schema.DatasetVersion.version], e.g.
    `20261018`.
    """

    filename: str
    """
    The file's name

    See [File.filename][esmporium.db.schema.File.filename].
    """


PathBuilder = Callable[[FileTarget], PurePosixPath]
"""
Decides where one file goes, relative to the download root

Returns a *relative* path. Making it absolute is
[local_path][(m).local_path]'s job, which is also where the result is checked, so a
builder of your own cannot accidentally escape the root.
"""


def _check_component(field: str, value: str) -> str:
    """
    Check one published value is usable as a single path component

    Parameters
    ----------
    field
        Which part of the identity this is, for the error message

    value
        The value to check

    Returns
    -------
    :
        `value`, unchanged, if it is safe

    Raises
    ------
    UnsafeDownloadPathError
        `value` is not a single, relative, ordinary name
    """
    if value in _FORBIDDEN_COMPONENTS:
        return _refuse(field, value, "is not a name")

    if value.strip() != value:
        # Leading or trailing whitespace makes two paths that look identical in a
        # terminal but are not, which is a confusion worth refusing rather than
        # trimming: trimming would write to a path the record did not name.
        return _refuse(field, value, "has leading or trailing whitespace")

    if any(sep in value for sep in _SEPARATORS):
        return _refuse(field, value, "contains a path separator")

    if "\x00" in value:
        return _refuse(field, value, "contains a null byte")

    return value


def _refuse(field: str, value: str, why: str) -> str:
    """
    Raise for an unusable component

    Split out so that [_check_component][(m).] reads as a list of rules rather than a
    list of `raise` statements.

    Parameters
    ----------
    field
        Which part of the identity this is

    value
        The value we were given

    why
        What is wrong with it

    Raises
    ------
    UnsafeDownloadPathError
        Always
    """
    raise UnsafeDownloadPathError(field, value, why)


def esmporium_layout(target: FileTarget) -> PurePosixPath:
    """
    Get where a file goes under esmporium's own layout

    The layout is:

    ```text
    <id_project_specific, dots turned into slashes>/v<version>/<filename>
    ```

    Parameters
    ----------
    target
        The file to place

    Returns
    -------
    :
        Where the file goes, relative to the download root

    Raises
    ------
    UnsafeDownloadPathError
        Any part of `target` is not a usable path component. Note that
        `id_project_specific` is checked *per dotted segment*, so a single segment
        containing a separator is caught rather than quietly becoming two directories.

    Examples
    --------
    The dots become directories, and the version gains a `v`:

    >>> esmporium_layout(
    ...     FileTarget(
    ...         id_project_specific="CMIP7.CMIP.CCCma.CanESM5-1",
    ...         version="20261018",
    ...         filename="areacella.nc",
    ...     )
    ... ).as_posix()
    'CMIP7/CMIP/CCCma/CanESM5-1/v20261018/areacella.nc'

    Two CMIP5 variables share one `id_project_specific`, so they share a
    directory and are told apart by their filenames:

    >>> cmip5 = FileTarget(
    ...     id_project_specific="cmip5.output1.NCAR.CCSM4.rcp45.mon.atmos.Amon.r1i1p1",
    ...     version="20120413",
    ...     filename="tas_Amon_CCSM4_rcp45_r1i1p1_200601-210012.nc",
    ... )
    >>> esmporium_layout(cmip5).parent.as_posix()
    'cmip5/output1/NCAR/CCSM4/rcp45/mon/atmos/Amon/r1i1p1/v20120413'
    """
    # Split first, then check each segment: the dots are the layout's own separator,
    # so `a.b` is two legitimate directories, while `a/b` inside one segment is not.
    segments = tuple(
        _check_component("id_project_specific segment", segment)
        for segment in target.id_project_specific.split(".")
    )

    version = _check_component("version", target.version)
    filename = _check_component("filename", target.filename)

    return PurePosixPath(*segments, f"v{version}", filename)


def local_path(
    root: Path, target: FileTarget, *, layout: PathBuilder = esmporium_layout
) -> Path:
    """
    Get the absolute path a file should be downloaded to

    Parameters
    ----------
    root
        The directory to download into. Not required to exist yet.

    target
        The file to place

    layout
        How to decide where under `root` the file goes.
        See [PathBuilder][(m).]; the default is [esmporium_layout][(m).].

    Returns
    -------
    :
        The absolute path to write to

        Its parent directory is *not* created here; whatever writes the bytes does
        that, because it is the thing that knows whether it is really going to.

    Raises
    ------
    UnsafeDownloadPathError
        `layout` returned a path which does not stay under `root`, or `target` has a
        part which is not a usable path component.

        The second check is the important one even though `layout` is injected: a
        builder supplied by a caller is not necessarily one that validates, so the
        containment check is done here, on the result, where it cannot be bypassed.

    Examples
    --------
    >>> from pathlib import Path
    >>> local_path(
    ...     Path("/data/esgf"),
    ...     FileTarget(
    ...         id_project_specific="CMIP7.CMIP.CCCma.CanESM5-1",
    ...         version="20261018",
    ...         filename="areacella.nc",
    ...     ),
    ... ).as_posix()
    '/data/esgf/CMIP7/CMIP/CCCma/CanESM5-1/v20261018/areacella.nc'
    """
    relative = layout(target)

    if relative.is_absolute():
        _refuse("layout result", str(relative), "is an absolute path")

    resolved_root = root.expanduser().resolve()
    # `resolve()` on the joined path collapses any `..` the layout produced, so the
    # comparison below is against what the filesystem would really do rather than
    # against the string we were handed.
    resolved = (resolved_root / relative).resolve()

    if resolved != resolved_root and resolved_root not in resolved.parents:
        _refuse(
            "layout result",
            str(relative),
            f"does not stay under the download root ({resolved_root})",
        )

    return resolved
