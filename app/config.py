"""Settings, read from environment variables (see .env.example)."""
import os
from dataclasses import dataclass, field


def _load_dotenv(path=None):
    """Load KEY=value lines from .env into the environment (never overriding real env vars)."""
    from pathlib import Path
    p = Path(path) if path else Path(__file__).resolve().parents[1] / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


if not os.environ.get("LL_SKIP_DOTENV"):
    _load_dotenv()


def _env(name, default=None, cast=str):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    if cast is bool:
        return v.lower() in ("1", "true", "yes", "on")
    return cast(v)


@dataclass
class Settings:
    env: str = field(default_factory=lambda: _env("LL_ENV", "development"))
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "sqlite:///ledgerlight.db"))
    secret_key: str = field(default_factory=lambda: _env("LL_SECRET_KEY", "dev-only-change-me"))
    token_ttl_hours: int = field(default_factory=lambda: _env("LL_TOKEN_TTL_HOURS", 24 * 7, int))
    # On Render the web service gets RENDER_EXTERNAL_URL; the worker reads it from the database (see worker.py).
    public_base_url: str = field(default_factory=lambda: (_env("LL_PUBLIC_BASE_URL") or _env("RENDER_EXTERNAL_URL")
                                                          or "http://localhost:8000").rstrip("/"))
    # Comma-separated list of website origins allowed to call the in-house API (the 9 brand sites).
    allowed_origins: str = field(default_factory=lambda: _env("LL_ALLOWED_ORIGINS", "http://localhost:8080"))

    # Data sources
    companies_house_api_key: str = field(default_factory=lambda: _env("COMPANIES_HOUSE_API_KEY", ""))
    companies_house_stream_key: str = field(default_factory=lambda: _env("COMPANIES_HOUSE_STREAM_KEY", ""))
    ch_rate_limit: int = field(default_factory=lambda: _env("CH_RATE_LIMIT", 550, int))  # per 5 minutes; CH allows 600
    gazette_poll_minutes: int = field(default_factory=lambda: _env("GAZETTE_POLL_MINUTES", 5, int))
    ccod_path: str = field(default_factory=lambda: _env("HMLR_CCOD_PATH", ""))
    ccj_provider_url: str = field(default_factory=lambda: _env("CCJ_PROVIDER_URL", ""))
    ccj_provider_key: str = field(default_factory=lambda: _env("CCJ_PROVIDER_KEY", ""))

    # AI briefs
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY", ""))
    anthropic_model: str = field(default_factory=lambda: _env("ANTHROPIC_MODEL", "claude-sonnet-5-5"))

    # Delivery
    smtp_host: str = field(default_factory=lambda: _env("SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: _env("SMTP_PORT", 587, int))
    smtp_user: str = field(default_factory=lambda: _env("SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: _env("SMTP_PASSWORD", ""))
    mail_from: str = field(default_factory=lambda: _env("MAIL_FROM", "alerts@ledgerlight.example"))
    twilio_sid: str = field(default_factory=lambda: _env("TWILIO_ACCOUNT_SID", ""))
    twilio_token: str = field(default_factory=lambda: _env("TWILIO_AUTH_TOKEN", ""))
    twilio_from: str = field(default_factory=lambda: _env("TWILIO_FROM", ""))
    export_dir: str = field(default_factory=lambda: _env("LL_EXPORT_DIR", "./exports"))

    # Billing
    stripe_secret_key: str = field(default_factory=lambda: _env("STRIPE_SECRET_KEY", ""))
    stripe_webhook_secret: str = field(default_factory=lambda: _env("STRIPE_WEBHOOK_SECRET", ""))

    # Behaviour
    dry_run_delivery: bool = field(default_factory=lambda: _env("LL_DRY_RUN_DELIVERY", True, bool))
    trust_proxy: bool = field(default_factory=lambda: _env("LL_TRUST_PROXY", False, bool))
    probate_cooling_off_days: int = field(default_factory=lambda: _env("LL_PROBATE_COOLING_OFF_DAYS", 14, int))

    @property
    def origins(self):
        out = [o.strip() for o in self.allowed_origins.split(",") if o.strip()]
        return out + ([self.public_base_url] if self.public_base_url not in out else [])


settings = Settings()


def reload():
    """Re-read the environment (used by tests)."""
    global settings
    settings = Settings()
    return settings
