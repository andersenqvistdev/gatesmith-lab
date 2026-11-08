#!/usr/bin/env python3
"""Import a sibling module that the public export may leave out.

The public Gatesmith export ships the core without the experimental company
layer and the retired discovery thread (launch roadmap K2a). Core modules
import those siblings through ``import_optional``: a missing module reads as
``None``, while a present module behaves exactly as a plain import would.
Only the absence of that very module is swallowed; an ImportError raised
inside a module that exists propagates unchanged.
"""

from __future__ import annotations

import importlib
from types import ModuleType


def import_optional(name: str, package: str | None) -> ModuleType | None:
    """Return sibling module ``name``, or None when it is not installed.

    ``package`` is the caller's ``__package__``: package mode tries the
    qualified name first, then (like the callers' script-mode fallback) the
    bare name.
    """
    candidates = [f"{package}.{name}", name] if package else [name]
    for qualified in candidates:
        try:
            return importlib.import_module(qualified)
        except ModuleNotFoundError as exc:
            if exc.name not in (qualified, package):
                raise
    return None
