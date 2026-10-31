"""Settings: where things live (environment) and how the vault behaves (.cortex/config.toml)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_REDIRECTS = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "http://localhost:*",
    "http://127.0.0.1:*",
    "cursor://anysphere.cursor-mcp/oauth/callback",
]


@dataclass
class Settings:
    vault: Path
    auth_db: Path
    public_url: str = "http://127.0.0.1:8765"
    host: str = "127.0.0.1"
    mcp_port: int = 8765
    ui_port: int = 8766
    cookie_secure: bool = True

    embedding_provider: str = "fastembed"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    context_tokens: int = 2000
    duplicate_threshold: float = 0.96
    quiet_seconds: float = 5.0
    push_minutes: int = 15
    allowed_folders: list[str] = field(
        default_factory=lambda: ["core", "memory", "entities", "docs", "logs"]
    )
    redirect_allowlist: list[str] = field(default_factory=lambda: list(DEFAULT_REDIRECTS))

    @property
    def state_dir(self) -> Path:
        return self.vault / ".cortex"

    @property
    def index_path(self) -> Path:
        return self.state_dir / "index.sqlite"


def load_settings(vault: str | Path | None = None) -> Settings:
    vault_path = Path(vault or os.environ.get("CORTEX_VAULT", "~/cortex-memory")).expanduser()
    auth_db = Path(os.environ.get("CORTEX_AUTH_DB", "~/.local/state/cortex/auth.sqlite")).expanduser()
    settings = Settings(
        vault=vault_path.resolve(),
        auth_db=auth_db,
        public_url=os.environ.get("CORTEX_PUBLIC_URL", "http://127.0.0.1:8765").rstrip("/"),
        mcp_port=int(os.environ.get("CORTEX_MCP_PORT", 8765)),
        ui_port=int(os.environ.get("CORTEX_UI_PORT", 8766)),
    )
    config_file = settings.state_dir / "config.toml"
    if config_file.exists():
        config = tomllib.loads(config_file.read_text())
        embedding = config.get("embedding", {})
        settings.embedding_provider = embedding.get("provider", settings.embedding_provider)
        settings.embedding_model = embedding.get("model", settings.embedding_model)
        for key in (
            "context_tokens",
            "duplicate_threshold",
            "quiet_seconds",
            "push_minutes",
            "allowed_folders",
            "redirect_allowlist",
        ):
            if key in config:
                setattr(settings, key, config[key])
    return settings
