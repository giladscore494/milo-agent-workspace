from functools import lru_cache
from backend.config import get_settings
from backend.repository import Repository, SupabaseRepository


@lru_cache
def get_repository() -> Repository:
    return SupabaseRepository(get_settings())
