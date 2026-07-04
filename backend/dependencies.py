from functools import lru_cache
from backend.config import get_settings
from backend.repository import Repository, SupabaseRepository


@lru_cache
def get_repository() -> Repository:
    return SupabaseRepository(get_settings())


@lru_cache
def get_job_launcher():
    from backend.job_launcher import build_job_launcher
    return build_job_launcher(get_settings())
