"""Runtime settings. Everything has a default so `make sim` works with no .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="DIALPASS_", extra="ignore")

    # HTTP server
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    # Detection loop (M3 tunes these)
    frame_ms: int = 20
    classifier_interval_ms: int = 500
    buffer_seconds: float = 12.0
    classify_window_ms: int = 1500
    # Wait this long after a menu is detected before waking Tier 2, so it hears
    # more of the prompt (fast-answering IVRs talk before the FSM catches up).
    # Kept short: a long delay misses brief menus that flip to hold music.
    menu_collect_s: float = 2.0

    # Telephony (M2+)
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    public_base_url: str = ""
    # If set, each call's decoded 8 kHz PCM is written to <record_dir>/<call_id>.wav.
    # Dev aid for M3 classifier tuning; leave blank in production.
    record_dir: str = ""

    # Realtime / Tier 2 (M4+)
    openai_api_key: str = ""
    realtime_model: str = "gpt-realtime-mini"
    # Menu decisions need strict JSON-format compliance; the mini model won't
    # hold format and keeps conversing, so the full model is used for that turn.
    realtime_menu_model: str = "gpt-realtime"

    @property
    def sample_rate(self) -> int:
        """Twilio Media Streams are 8 kHz mono G.711."""
        return 8000


@lru_cache
def get_settings() -> Settings:
    return Settings()
