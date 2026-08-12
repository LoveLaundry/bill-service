from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ─── Backward-compatible legacy connection (historical) ───────────────
    mongo_uri: str = Field(
        default="mongodb://localhost:27017",
        validation_alias=AliasChoices("MONGO_URI", "DATABASE_URL"),
    )
    mongo_db_name: str = "bills"
    cors_origins: list[str] = ["*"]

    # ─── Three-database architecture (new) ─────────────────────────────────
    # MAIN        — production source of truth (all normal reads/writes).
    # SECONDARY   — verification/replica DB, written only by the sync worker.
    # LOCAL       — admin-triggered replica; never a read source for the API.
    mongodb_main_uri: str | None = Field(
        default=None, validation_alias=AliasChoices("MONGODB_MAIN_URI")
    )
    mongodb_main_db: str | None = Field(
        default=None, validation_alias=AliasChoices("MONGODB_MAIN_DB")
    )
    mongodb_secondary_uri: str | None = Field(
        default=None, validation_alias=AliasChoices("MONGODB_SECONDARY_URI")
    )
    mongodb_secondary_db: str | None = Field(
        default=None, validation_alias=AliasChoices("MONGODB_SECONDARY_DB")
    )
    mongodb_local_uri: str | None = Field(
        default=None, validation_alias=AliasChoices("MONGODB_LOCAL_URI")
    )
    mongodb_local_db: str | None = Field(
        default=None, validation_alias=AliasChoices("MONGODB_LOCAL_DB")
    )

    # ─── Sync worker tuning ────────────────────────────────────────────────
    sync_retry_max_attempts: int = Field(default=5, validation_alias=AliasChoices("SYNC_RETRY_MAX_ATTEMPTS"))
    sync_retry_base_delay_seconds: int = Field(default=3, validation_alias=AliasChoices("SYNC_RETRY_BASE_DELAY_SECONDS"))
    sync_worker_poll_seconds: float = Field(default=1.0, validation_alias=AliasChoices("SYNC_WORKER_POLL_SECONDS"))
    sync_enabled: bool = Field(default=True, validation_alias=AliasChoices("SYNC_ENABLED"))

    # ─── Resolved role databases ───────────────────────────────────────────
    def resolve_main_uri(self) -> str:
        return self.mongodb_main_uri or self.mongo_uri

    def resolve_main_db(self) -> str:
        return self.mongodb_main_db or self.mongo_db_name

    def resolve_secondary_uri(self) -> str:
        return self.mongodb_secondary_uri or self.mongo_uri

    def resolve_secondary_db(self) -> str:
        return self.mongodb_secondary_db or f"{self.resolve_main_db()}_secondary"

    def resolve_local_uri(self) -> str:
        return self.mongodb_local_uri or self.mongo_uri

    def resolve_local_db(self) -> str:
        return self.mongodb_local_db or f"{self.resolve_main_db()}_local"


settings = Settings()
