"""
Corrected metadata, for the cases where what was published is wrong

Some of what ESGF publishes is simply incorrect, and no amount of care in reading it
helps. This is where a correction goes instead.

Kept as its own package rather than inside [esmporium.db][] because a fix is a
statement about the *data*, not about our database -- and because the intention is that
these can eventually be shared, which means they have to be a thing in their own right
rather than an implementation detail of how we happen to store facts.

Only parent information is corrected today. See [esmporium.fixes.parents][].
"""

from esmporium.fixes.parents import (
    PARENT_FIXES,
    ParentFix,
    ParentFixes,
    fixes_from_mapping,
    no_parent_fixes,
)

__all__ = [
    "PARENT_FIXES",
    "ParentFix",
    "ParentFixes",
    "fixes_from_mapping",
    "no_parent_fixes",
]
