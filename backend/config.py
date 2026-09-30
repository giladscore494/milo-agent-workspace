from functools import lru_cache
from pydantic import AliasChoices, Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    supabase_url: HttpUrl = Field(alias="SUPABASE_URL")
    supabase_service_role_key: str = Field(
        validation_alias=AliasChoices("SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_ROLE_KEY"),
        min_length=1,
    )
    api_title: str = "MILO Agent Workspace API"
    allowed_cors_origins: str = Field(default="http://localhost:3000", alias="ALLOWED_CORS_ORIGINS")
    job_launcher: str = Field(default="disabled", alias="JOB_LAUNCHER")
    gcp_project_id: str = Field(default="big-cabinet-457321-t7", alias="GCP_PROJECT_ID")
    gcp_region: str = Field(default="us-central1", alias="GCP_REGION")
    cloud_run_worker_job: str = Field(default="milo-agent-worker", alias="CLOUD_RUN_WORKER_JOB")
    # E': the capture job the website's Prepare route executes. Empty (the
    # default) means this API has no capture job, so it can prepare nothing.
    cloud_run_capture_job: str = Field(default="", alias="CLOUD_RUN_CAPTURE_JOB")
    # PR-D3: the manufacturer normalisation job (the only one holding the
    # provider key). Empty means this API can normalise nothing.
    cloud_run_normalisation_job: str = Field(default="", alias="CLOUD_RUN_NORMALISATION_JOB")

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.allowed_cors_origins.split(",") if origin.strip()]

    model_config = SettingsConfigDict(env_file=None, extra="ignore", populate_by_name=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
