"""Application settings.

Values come from environment variables, then from ROOT/.env (loaded regardless of the
current working directory), then from the defaults below, which equal .env.example.
"""
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# greencharge/ (this file is greencharge/backend/app/config.py)
ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    database_url: str = "postgresql://greencharge:greencharge@localhost:5435/greencharge"
    redis_url: str = "redis://localhost:6379/0"
    electricity_maps_token: str = ""
    electricity_maps_zone: str = "IN-WE"
    openchargemap_key: str = ""
    llm_provider: str = "gemini"
    llm_api_key: str = ""
    grid_provider: Literal["electricitymaps", "synthetic"] = "synthetic"
    ocpp_port: int = 9000
    site_lat: float = 23.1866
    site_lon: float = 72.6291

    # Phase 1: site-local time zone, slot grid and seed-data directory.
    site_timezone: str = "Asia/Kolkata"
    slot_minutes: int = 15
    horizon_slots: int = 96
    data_dir: Path = Path(__file__).resolve().parent / "data"

    # Phase 2: simulated-time compression (see app/clock.py) and the OCPP heartbeat interval.
    time_scale: float = 60.0          # TIME_SCALE env var
    heartbeat_interval_s: int = 30    # spec: BootNotification interval=30

    # Phase 4: the orchestration loop (app/orchestrator/loop.py) and on-time accounting.
    tick_minutes: int = 5            # spec: "The tick — runs every 5 minutes" (SIMULATED minutes)
    soc_tolerance: float = 1e-4      # SoC travels as a 3-decimal percent string; completed-on-time check tolerance

    # Phase 7: the model app/llm/client.py asks when llm_provider == "gemini" (env LLM_MODEL).
    # Checked against GET /v1beta/models for this project's key: "gemini-2.0-flash" is not offered
    # to it, so the default is the stable GA name that is. A pinned name, not an alias such as
    # "gemini-flash-latest", so the demo cannot shift under us. Deliberately absent from
    # .env.example, which stays exactly as the build spec pins it.
    llm_model: str = "gemini-2.5-flash"


settings = Settings()
