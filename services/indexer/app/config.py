from pydantic_settings import BaseSettings

CHUNKER_VERSION = "1"


class Settings(BaseSettings):
    env: str = "local"

    db_host: str
    db_port: int = 5432
    db_name: str = "vault"
    db_user: str
    db_password: str

    redis_url: str = "redis://localhost:6379/0"

    s3_endpoint_url: str | None = None
    s3_access_key: str
    s3_secret_key: str
    s3_bucket: str
    s3_region: str = "us-east-1"

    embedding_provider: str = "local"
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024
    embedding_base_url: str = ""
    openai_api_key: str = ""

    s3_rate_limit_rps: int = 50
    classifier_confidence_threshold: float = 0.6
    parser_prefer_docling: bool = True

    class Config:
        env_file = ".env.local"


settings = Settings()
