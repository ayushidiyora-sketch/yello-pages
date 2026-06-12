from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    MONGO_URI: str = "mongodb://localhost:27017"
    MONGO_DB: str = "yellowpages"

    # Optional paid US proxy gateway for yellowpages.com (US site geo-blocks non-US IPs).
    # Empty = use the rotating free US-proxy pool in yp_us.py.
    PROXY_URL: str = ""

    MAX_PAGES: int = 50
    MIN_DELAY: float = 1.0
    MAX_DELAY: float = 3.0
    REQUEST_TIMEOUT: int = 30


settings = Settings()
