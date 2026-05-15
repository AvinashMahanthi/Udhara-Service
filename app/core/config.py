from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    mongodb_url: str = Field(alias="MONGODB_URL")
    mongodb_db: str = Field(alias="MONGODB_DB")
    jwt_secret_key: str = Field(alias="JWT_SECRET_KEY")
    jwt_algorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")
    access_token_expire_minutes: int = Field(default=120, alias="ACCESS_TOKEN_EXPIRE_MINUTES")
    api_port: int = Field(default=8080, alias="API_PORT")
    razorpay_key_id: str = Field(alias="RAZORPAY_KEY_ID")
    razorpay_key_secret: str = Field(alias="RAZORPAY_KEY_SECRET")
    # cors_origins: str = Field(default="http://localhost:8081,http://localhost:19006,http://localhost:3000", alias="CORS_ORIGINS")

    # @property
    # def cors_origin_list(self) -> list[str]:
    #     return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
