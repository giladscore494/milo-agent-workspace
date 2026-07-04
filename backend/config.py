from functools import lru_cache
from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    supabase_url: HttpUrl = Field(alias="SUPABASE_URL")
    supabase_service_role_key: str = Field(alias="SUPABASE_SERVICE_ROLE_KEY", min_length=1)
    api_title: str = "MILO Agent Workspace API"
    environment: str = Field(default="local", alias="ENVIRONMENT")

    model_config = SettingsConfigDict(env_file=None, extra="ignore", populate_by_name=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
