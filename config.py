from pydantic import BaseSettings
from dotenv import load_dotenv

load_dotenv()  # Optional if you use a .env file

class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+asyncpg://Dennis%20Evans%20Paul:ispbill001@lipay.store:5432/isp_billing"
    SECRET_KEY: str = "your-secret-key"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

settings = Settings()
