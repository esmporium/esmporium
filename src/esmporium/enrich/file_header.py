"""
Parsing everything a file's global attributes say

For time and ancestry purposes that is the run's parent and where it branched from it:
`parent_*` attributes and the branch times, which CMIP6 and CMIP7 publish in full and
CMIP5 publishes in part. Which attributes carry them, and what an absent one means, is
each project's own convention, so the parsing is one function per project, collected in
[FILE_HEADER_PARSERS_BY_PROJECT][(m).FILE_HEADER_PARSERS_BY_PROJECT], and
[parse_file_header][(m).parse_file_header] picks one.

The input is the global attributes exactly as [esmporium.headers][] read them
([FileHeader.global_attributes][esmporium.headers.FileHeader.global_attributes]).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from esmporium.search.result_parsing import ParentInfo

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

NOT_SAID = frozenset({"", "n/a", "na", "none", "null", "no parent", "not applicable"})
"""
Attribute values which mean "this was not said", rather than being a value

Matched case-insensitively and after stripping whitespace.

Needed because the two projects whose headers we read both use a sentinel, and they use
different ones -- so neither a missing attribute nor a `None` is enough on its own.

- **CMIP5 writes `N/A`.** And it does so in two quite different situations, which is
  why this is a per-field rule rather than a whole-record one. A CMIP5 `piControl` says
  `parent_experiment_id = "N/A"` and carries nothing else, which is how it says it is a
  root. But `CMCC-CM`'s `rcp85` says `parent_experiment_rip = "N/A"` while naming
  `historical` as its parent experiment -- there, one field is unknown and the record
  still declares a parent.
- **CMIP6 writes `no parent`.** A literal `parent_experiment_id` of `"no parent"` is a
  CMIP6 convention, and does not occur in CMIP7, which announces a root by carrying
  none of the keys at all.

Checked live on 2026-11-05.
"""


def _said(attributes: Mapping[str, object], name: str) -> str | None:
    """
    Read one attribute as text, treating a sentinel as not having been said

    Parameters
    ----------
    attributes
        Every global attribute the file declared

    name
        The attribute to read

    Returns
    -------
    :
        Its value, or `None` if it is absent, empty or one of
        [NOT_SAID][(m).NOT_SAID]

    Examples
    --------
    >>> _said({"parent_experiment_id": "piControl"}, "parent_experiment_id")
    'piControl'

    A CMIP5 root run, which says so with a sentinel:

    >>> print(_said({"parent_experiment_id": "N/A"}, "parent_experiment_id"))
    None

    And a CMIP6 one, which says so with a different sentinel:

    >>> print(_said({"parent_experiment_id": "no parent"}, "parent_experiment_id"))
    None

    >>> print(_said({}, "parent_experiment_id"))
    None
    """
    raw = attributes.get(name)
    if raw is None:
        return None

    text = str(raw).strip()
    if text.casefold() in NOT_SAID:
        return None

    return text


def _number(attributes: Mapping[str, object], name: str) -> float | None:
    """
    Read one attribute as a number, if it is one

    Parameters
    ----------
    attributes
        Every global attribute the file declared

    name
        The attribute to read

    Returns
    -------
    :
        Its value as a `float`, or `None` if absent or not a number

        A value that is not a number is read as "not said" rather than raising: this
        is metadata written by dozens of modelling centres over twenty years, and one
        of them putting a word where a number belongs should cost us that field, not
        the whole header.

    Examples
    --------
    >>> _number({"branch_time": 240.0}, "branch_time")
    240.0

    A string that happens to be a number is still a number:

    >>> _number({"branch_time": "240"}, "branch_time")
    240.0

    >>> print(_number({"branch_time": "unknown"}, "branch_time"))
    None
    """
    raw = attributes.get(name)

    # `bool` first: it is an `int` subclass, so `True` would otherwise read as `1.0`.
    if isinstance(raw, bool):
        return None

    if isinstance(raw, (int, float)):
        return float(raw)

    if isinstance(raw, str):
        try:
            return float(raw)
        except ValueError:
            return None

    return None


class FileHeaderParser(Protocol):
    """
    Turns one file's global attributes into what it says about its parent

    One per project, collected in
    [FILE_HEADER_PARSERS_BY_PROJECT][(m).FILE_HEADER_PARSERS_BY_PROJECT].
    """

    def __call__(
        self, attributes: Mapping[str, object], *, project: str, model: str
    ) -> ParentInfo:
        """
        Parse what a file says about its parent

        Parameters
        ----------
        attributes
            Every global attribute the file declared, as
            [read_global_attributes][esmporium.headers.read_global_attributes]
            returned them

        project
            The project of the simulation whose file this is, as
            [Dataset.project][esmporium.db.schema.Dataset.project] spells it

        model
            The model of the simulation whose file this is, as
            [Dataset.model][esmporium.db.schema.Dataset.model] spells it

            For a project which publishes its parent completely, `project` and `model`
            are unused. They are here for CMIP5, which publishes no parent model and so
            has to take the child's -- and which must take it from **here** rather than
            from the file's own `model_id`, for the reason
            [parse_cmip5_file_header][(m).parse_cmip5_file_header] gives.

        Returns
        -------
        :
            What the file said

            An instance whose every field is `None` means the file was read and
            declared **no parent** -- which is how a root of the tree says so, and is
            a different statement from not having looked.
        """


def parse_cmip6_file_header(
    attributes: Mapping[str, object],
    *,
    project: str | None = None,
    model: str | None = None,
) -> ParentInfo:
    """
    Parse a CMIP6 or CMIP7 file's parent attributes

    These projects publish the parent completely, so nothing here is inferred: the
    file names the parent's era, activity, model, experiment and variant, and both
    branch times. Confirmed against live CMIP6 files on 2026-11-05.

    Parameters
    ----------
    attributes
        Every global attribute the file declared

    project
        The project of the simulation whose file this is. **Unused**, and accepted only
        so that this satisfies [FileHeaderParser][(m).FileHeaderParser] -- a project
        which publishes its parent in full has nothing to infer.

    model
        The model of the simulation whose file this is. **Unused**, for the same reason.

    Returns
    -------
    :
        What the file said about its parent

    Examples
    --------
    >>> parent = parse_cmip6_file_header(
    ...     {
    ...         "parent_mip_era": "CMIP6",
    ...         "parent_activity_id": "CMIP",
    ...         "parent_source_id": "TaiESM1",
    ...         "parent_experiment_id": "piControl",
    ...         "parent_variant_label": "r1i1p1f1",
    ...         "parent_time_units": "days since 0701-01-01",
    ...         "branch_time_in_parent": 438000.0,
    ...         "branch_time_in_child": 0.0,
    ...     }
    ... )
    >>> parent.parent_model, parent.parent_experiment
    ('TaiESM1', 'piControl')
    >>> parent.branch_time_in_parent
    438000.0

    A root run, which CMIP6 announces with a sentinel:

    >>> parse_cmip6_file_header({"parent_experiment_id": "no parent"}).declares_a_parent
    False
    """
    return ParentInfo(
        parent_project=_said(attributes, "parent_mip_era"),
        parent_activity=_said(attributes, "parent_activity_id"),
        parent_model=_said(attributes, "parent_source_id"),
        parent_experiment=_said(attributes, "parent_experiment_id"),
        parent_variant_label=_said(attributes, "parent_variant_label"),
        parent_time_units=_said(attributes, "parent_time_units"),
        branch_time_in_parent=_number(attributes, "branch_time_in_parent"),
        branch_time_in_child=_number(attributes, "branch_time_in_child"),
    )


def parse_cmip5_file_header(
    attributes: Mapping[str, object], *, project: str, model: str
) -> ParentInfo:
    """
    Parse a CMIP5 file's parent attributes, inferring the two it does not publish

    CMIP5 publishes far less than CMIP6 and spells what it does publish differently.
    Measured across 47 models on 2026-11-04, a CMIP5 header carries
    `parent_experiment_id`, `parent_experiment_rip` and `branch_time`, **and nothing
    else about the parent at all**.

    Parameters
    ----------
    attributes
        Every global attribute the file declared

    project
        The project of the simulation whose file this is, as the database holds it,
        which is inferred onto the parent

    model
        The model of the simulation whose file this is, as the database holds it,
        which is inferred onto the parent

    Returns
    -------
    :
        What the file said about its parent, plus the two inferred facts

    Examples
    --------
    >>> child = {"project": "CMIP5", "model": "BCC-CSM1.1(m)"}
    >>> parent = parse_cmip5_file_header(
    ...     {
    ...         "parent_experiment_id": "piControl",
    ...         "parent_experiment_rip": "r1i1p1",
    ...         "branch_time": 240.0,
    ...         "model_id": "bcc-csm1-1-m",
    ...     },
    ...     **child,
    ... )
    >>> parent.parent_experiment, parent.parent_variant_label
    ('piControl', 'r1i1p1')

    Note which spelling the parent's model gets: the child's, not the file's own
    `model_id`, because that is the one a search will match.

    >>> parent.parent_model, parent.parent_project
    ('BCC-CSM1.1(m)', 'CMIP5')

    `branch_time` is recorded verbatim, never interpreted:

    >>> parent.branch_time_in_parent
    240.0

    A root run declares no parent, and nothing is inferred onto it:

    >>> root = parse_cmip5_file_header({"parent_experiment_id": "N/A"}, **child)
    >>> root.declares_a_parent
    False
    >>> print(root.parent_model)
    None

    A model which publishes no usable variant label still declares its parent, and the
    gap is left as a gap for a fix to close:

    >>> partial = parse_cmip5_file_header(
    ...     {"parent_experiment_id": "historical", "parent_experiment_rip": "N/A"},
    ...     **child,
    ... )
    >>> partial.parent_experiment
    'historical'
    >>> print(partial.parent_variant_label)
    None
    """
    experiment = _said(attributes, "parent_experiment_id")
    variant_label = _said(attributes, "parent_experiment_rip")
    branch_time = _number(attributes, "branch_time")

    declares = experiment is not None or variant_label is not None

    return ParentInfo(
        # Inferred, and only once the file has declared a parent -- see above.
        parent_project=project if declares else None,
        parent_activity=None,
        parent_model=model if declares else None,
        parent_experiment=experiment,
        parent_variant_label=variant_label,
        # CMIP5 publishes no parent time units, so `branch_time` has no stated origin.
        parent_time_units=None,
        branch_time_in_parent=branch_time if declares else None,
        branch_time_in_child=None,
    )


FILE_HEADER_PARSERS_BY_PROJECT: Mapping[str, FileHeaderParser] = {
    "CMIP5": parse_cmip5_file_header,
    "CMIP6": parse_cmip6_file_header,
}
"""
Which parser turns each project's global attributes into a
[ParentInfo][esmporium.search.ParentInfo]

Keyed by [Dataset.project][esmporium.db.schema.Dataset.project]. **Keyed by project
and not by file format**, which is worth stating because the formats vary within a
project: CMIP5 is mostly classic netCDF but `EC-EARTH`'s rcp85 output is netCDF-4. The
format decides who reads the bytes ([esmporium.headers][] settles that from the bytes
themselves); the project decides what the attributes mean.

Pass a mapping of your own to override one, or to add a project, e.g.
`{**FILE_HEADER_PARSERS_BY_PROJECT, "CMIP5": my_parser}`.
"""


class UnknownProjectFileHeaderParserError(KeyError):
    """
    Raised when a project's file header has no parser
    """

    def __init__(self, project: str, known: Collection[str]) -> None:
        self.project = project
        """The project we have no parser for"""

        self.known = tuple(sorted(known))
        """The projects we do have one for"""

        super().__init__(
            f"No file header parser is registered for project {project!r}. "
            f"Known projects: {', '.join(repr(one) for one in self.known)}. "
            "Which global attributes carry a parent differs by project -- CMIP5 "
            "calls the parent's variant label `parent_experiment_rip` where CMIP6 "
            "calls it `parent_variant_label` -- so there is no sensible default to "
            "fall back on. Pass a parser of your own in `parsers`."
        )


def file_header_parser_for_project(
    project: str,
    parsers: Mapping[str, FileHeaderParser] = FILE_HEADER_PARSERS_BY_PROJECT,
) -> FileHeaderParser:
    """
    Get the file header parser for one project

    Parameters
    ----------
    project
        The project to parse for, as
        [Dataset.project][esmporium.db.schema.Dataset.project] spells it

    parsers
        The parsers to choose from

    Returns
    -------
    :
        The parser to use

    Raises
    ------
    UnknownProjectFileHeaderParserError
        `project` has no parser.
    """
    found = parsers.get(project)
    if found is None:
        raise UnknownProjectFileHeaderParserError(project, parsers)

    return found


def parse_file_header(
    attributes: Mapping[str, object],
    *,
    project: str,
    model: str,
    parsers: Mapping[str, FileHeaderParser] = FILE_HEADER_PARSERS_BY_PROJECT,
) -> ParentInfo:
    """
    Parse everything a file's global attributes say

    Parameters
    ----------
    attributes
        Every global attribute the file declared, as
        [FileHeader.global_attributes][esmporium.headers.FileHeader.global_attributes]
        holds them

    project
        The project of the simulation whose file this is, as
        [Dataset.project][esmporium.db.schema.Dataset.project] spells it, which picks
        the parser

    model
        The model of the simulation whose file this is, as
        [Dataset.model][esmporium.db.schema.Dataset.model] spells it

    parsers
        Which parser to use per project. See
        [FILE_HEADER_PARSERS_BY_PROJECT][(m).FILE_HEADER_PARSERS_BY_PROJECT].

    Returns
    -------
    :
        The parent the file declared, and the branch times as published

        An instance whose every field is `None` means the file declared **no parent**,
        which is how a root of the tree says so.

    Raises
    ------
    UnknownProjectFileHeaderParserError
        `project` has no parser

    Examples
    --------
    >>> parent = parse_file_header(
    ...     {
    ...         "parent_experiment_id": "piControl",
    ...         "parent_variant_label": "r1i1p1f1",
    ...         "parent_source_id": "TaiESM1",
    ...         "parent_time_units": "days since 0701-01-01",
    ...         "branch_time_in_parent": 438000.0,
    ...         "branch_time_in_child": 0.0,
    ...     },
    ...     project="CMIP6",
    ...     model="TaiESM1",
    ... )
    >>> parent.parent_experiment, parent.branch_time_in_parent
    ('piControl', 438000.0)
    """
    parser = file_header_parser_for_project(project, parsers)

    return parser(attributes, project=project, model=model)
