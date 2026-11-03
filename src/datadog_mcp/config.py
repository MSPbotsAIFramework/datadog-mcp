"""Non-credential configuration.

Credentials never live here -- they are read per-request from headers
(see server.py). This module only holds process-level settings, and it
must tolerate unknown environment variables (the runtime injects some
that this project never declares) or the container fails to start.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    mcp_http_host: str = "0.0.0.0"
    mcp_http_port: int = 8080


settings = Settings()
