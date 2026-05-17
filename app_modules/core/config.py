import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class ServiceConfig:
    app_name: str
    version: str
    api_key: str
    request_timeout_seconds: float


def _first_env(keys: tuple[str, ...]) -> str:
    for key in keys:
        value = os.getenv(key, "").strip()
        if value:
            return value
    return ""


@lru_cache(maxsize=1)
def get_config() -> ServiceConfig:
    return ServiceConfig(
        app_name=os.getenv("APP_NAME", "clean-rebuild-uid-checker"),
        version=os.getenv("APP_VERSION", "step10-uid-realtime-smoke"),
        api_key=_first_env(
            (
                "UID_CHECKER_API_KEY",
                "EXTERNAL_CHECKER_API_KEY",
                "BOT_NEW_CHECKER_API_KEY",
                "CHECKER_API_KEY",
                "FB_UID_API_KEY",
            )
        ),
        request_timeout_seconds=float(os.getenv("UID_CHECKER_TIMEOUT", "10")),
    )
