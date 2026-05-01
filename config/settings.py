from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    TELEGRAM_BOT_TOKEN: str
    OWNER_TELEGRAM_ID: int = 0
    COMPANY_NAME: str = "Компания"
    WEB_PASSWORD: str = "mike123"
    API_KEY: str = ""

    # SaaS / tenant
    TENANT_ID: str = "default"
    BOT_NAME: str = "Ассистент"
    WELCOME_MESSAGE: str = "Здравствуйте! Я ассистент компании {COMPANY_NAME}. Чем могу помочь?"
    BOT_TONE: str = "professional"

    # Limits
    DAILY_REQUEST_LIMIT: int = 500
    MONTHLY_TOKEN_LIMIT: int = 500000
    RATE_LIMIT_SECONDS: int = 2
    MAX_UPLOAD_MB: int = 10
    MAX_FILE_SIZE_MB: int = 10
    MAX_FILES_PER_TENANT: int = 20
    MAX_CHUNKS_PER_TENANT: int = 5000
    MIN_TEXT_LENGTH: int = 200

    # RAG
    CHROMA_PERSIST_DIR: str = "./data/chroma_db"
    TOP_K_RESULTS: int = 4
    MIN_RAG_SCORE: float = 0.72
    RAG_CONFIDENCE_THRESHOLD: float = 0.72
    CHUNK_SIZE: int = 1500
    CHUNK_OVERLAP: int = 150
    EMBEDDING_MODEL: str = "intfloat/multilingual-e5-large"

    # LLM
    OPENROUTER_MODEL: str = "deepseek/deepseek-r1"
    OPENROUTER_API_KEY: str = ""
    MAX_LLM_TOKENS: int = 900
    LLM_TIMEOUT_SECONDS: int = 35

    NOTIFY_OWNER: bool = True

    # Telegram mode
    TELEGRAM_MODE: str = "polling"  # polling | webhook
    WEBHOOK_URL: str = ""
    WEBHOOK_PORT: int = 8443

    # Cache
    CACHE_TTL_HOURS: int = 24

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
