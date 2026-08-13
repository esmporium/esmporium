"""
Facet query to native project language translation happens here.

`translate` lowers a query once into the canonical language and renders it out to each
requested project. The requested projects come from the query's own `project` field (a
CMIP5 skin with `project=("CMIP5", "CMIP6")` searches both), or from an explicit
`projects` override.
"""

from collections.abc import Collection

from esmporium.esgf.project_translation_maps import get_profile
from esmporium.esgf.query_models import _ESGFQueryBase


class NoTargetProjectError(ValueError):
    """Raised when a translate call has no project to render to.

    A target project is required: it selects which project(s')
    native params to render to. Every facet is optional; the target
    is not.
    """

    def __init__(self) -> None:
        super().__init__(
            "No project to translate to: set the query's `project` or pass `projects`."
        )


def translate(
    query: _ESGFQueryBase,
    projects: Collection[str] | None = None,
) -> dict[str, dict[str, str]]:
    """
    Render a query to native params for each requested project.

    Parameters
    ----------
    query
        Any dialect skin (unified language or project-specific). Its input vocabulary is
        independent of which projects are searched (e.g. a user may search for CMIP6
        data using CMIP5 facet language).

    projects
        The projects to render to. Defaults to the query's own `project` field.


    Returns
    -------
    :
        A mapping of project -> that project's native param dict.

    Raises
    ------
    ValueError
        If no projects are requested, or a project is unknown.

    FacetNotRepresentableError
        If any requested project cannot express a facet in the query. The call fails
        as a whole; no partial result is returned.
    """
    target_projects = tuple(query.project if projects is None else projects)
    if not target_projects:
        raise NoTargetProjectError

    canonical = query.to_canonical()

    return {
        project: get_profile(project).to_native_params(canonical)
        for project in target_projects
    }
