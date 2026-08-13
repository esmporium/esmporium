"""
The multi-era fan-out — tying the skins to the era profiles.

`translate` lowers a query once into the canonical IR and renders it out to each
requested era. The requested eras come from the query's own `project` field (a
CMIP5 skin with `project=("CMIP5", "CMIP6")` searches both), or from an explicit
`projects` override.

Per the current design this is deliberately strict: if any one era cannot express
the query, the whole call raises (see
[`FacetNotRepresentableError`][esmporium.esgf.mip_translation.FacetNotRepresentableError])
so the user decides how to adjust it. Graceful per-era handling is future work.
"""

from collections.abc import Collection

from esmporium.esgf.mip_translation import get_profile
from esmporium.esgf.query_models import _ESGFQueryBase


class NoTargetErasError(ValueError):
    """Raised when a translate call has no eras to render to."""

    def __init__(self) -> None:
        super().__init__(
            "No eras to translate to: set the query's `project` or pass `projects`."
        )


def translate(
    query: _ESGFQueryBase,
    projects: Collection[str] | None = None,
) -> dict[str, dict[str, str]]:
    """
    Render a query to native params for each requested era.

    Parameters
    ----------
    query
        Any dialect skin (unified or era-specific). Its input vocabulary is
        independent of which eras are searched.

    projects
        The eras to render to. Defaults to the query's own `project` field.

    Returns
    -------
    :
        A mapping of era -> that era's native param dict.

    Raises
    ------
    ValueError
        If no eras are requested, or an era is unknown.

    FacetNotRepresentableError
        If any requested era cannot express a facet in the query. The call fails
        as a whole; no partial result is returned.
    """
    target_eras = tuple(query.project if projects is None else projects)
    if not target_eras:
        raise NoTargetErasError

    canonical = query.to_canonical()

    return {era: get_profile(era).to_native_params(canonical) for era in target_eras}
