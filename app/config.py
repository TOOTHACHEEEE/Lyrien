"""应用配置，统一从 .env 加载。"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"

    # 应用
    database_url: str = "sqlite:///./data/lyrien.db"
    cards_per_day: int = 4
    explore_card_ratio: float = 0.2
    fetch_window_hours: int = 24
    daily_run_hour: int = 6
    prescreen_pool_size: int = 15
    explore_pool_size: int = 8

    @property
    def db_path(self) -> Path:
        """从 DATABASE_URL 解析出本地文件路径。"""
        url = self.database_url
        if url.startswith("sqlite:///"):
            path_str = url[len("sqlite:///") :]
            if path_str.startswith("./"):
                return PROJECT_ROOT / path_str[2:]
            return Path(path_str)
        raise ValueError(f"Unsupported DATABASE_URL: {url}")


settings = Settings()
