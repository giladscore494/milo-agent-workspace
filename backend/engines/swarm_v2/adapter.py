"""Explicit adapter name for registry construction in a later routing PR."""

from .engine import SwarmV2Engine


class SwarmV2Adapter(SwarmV2Engine):
    pass
