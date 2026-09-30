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


@lru_cache
def get_capture_trigger():
    """E': the capture-job trigger this API is configured for, or None."""
    import os

    from backend.catalog.scope.prepare_trigger import build_capture_trigger
    return build_capture_trigger(get_settings(), os.environ)


@lru_cache
def get_normalisation_trigger():
    """PR-D3: the normalisation job's trigger (its own job: the only one holding
    the provider key), or None."""
    import os

    from backend.catalog.scope.prepare_trigger import build_capture_trigger
    return build_capture_trigger(get_settings(), os.environ, job_setting="cloud_run_normalisation_job")
