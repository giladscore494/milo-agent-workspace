"""Compatibility helpers for the pinned PostgREST Python client.

postgrest 2.27.x returns mutation builders that do not expose ``select`` even
though the repository historically chained ``.select("*")`` after mutations.
Those mutation methods already default to ``returning=representation``, so a
select-all compatibility method can safely preserve the existing repository
behavior until the dependency is upgraded.
"""

from typing import Any

from postgrest import SyncFilterRequestBuilder, SyncQueryRequestBuilder


def _select_all(builder: Any, *columns: str, **kwargs: Any) -> Any:
    if kwargs or columns not in ((), ("*",)):
        raise ValueError("postgrest 2.27 compatibility only supports select-all after mutations")
    return builder


def install_postgrest_select_all_compatibility() -> None:
    for builder_type in (SyncQueryRequestBuilder, SyncFilterRequestBuilder):
        if not hasattr(builder_type, "select"):
            setattr(builder_type, "select", _select_all)
