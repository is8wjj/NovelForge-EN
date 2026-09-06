"""Application configuration management

Unified management of all configuration items, supporting environment variables and default values.
"""

import os
import sys
from pathlib import Path
from typing import ClassVar, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class DatabaseSettings(BaseSettings):
    """Database configuration"""
    
    # Database path
    db_path: Optional[str] = Field(default=None, alias="NOVELFORGE_DB_PATH")
    
    # Whether to print SQL logs
    echo: bool = Field(default=False, alias="DB_ECHO")
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )
    
    def get_database_url(self) -> str:
        """Get database URL
        
        Strategy:
        1) Packaged (onefile/onedir): prefer placing alongside the executable
        2) Development: place in the source backend directory
        3) Support overriding the absolute path via the NOVELFORGE_DB_PATH environment variable (compatible with the legacy variable AIAUTHOR_DB_PATH)
        
        Returns:
            Database URL
        """
        override_path = self.db_path or os.getenv("AIAUTHOR_DB_PATH")
        if override_path:
            db_file = Path(override_path)
        else:
            if getattr(sys, "frozen", False):
                base_dir = Path(sys.executable).resolve().parent
            else:
                # Go up 2 levels from app/core/config.py to backend/
                # config.py -> core/ -> app/ -> backend/
                base_dir = Path(__file__).resolve().parents[2]
            db_file = base_dir / 'novelforge.db'
        
        return f"sqlite:///{db_file.as_posix()}"


class KnowledgeGraphSettings(BaseSettings):
    """Knowledge graph configuration"""
    
    # Knowledge graph provider
    provider: str = Field(default="sqlmodel", alias="KNOWLEDGE_GRAPH_PROVIDER")
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


class Neo4jSettings(BaseSettings):
    """Neo4j graph database configuration"""
    
    uri: str = Field(default="neo4j://127.0.0.1:7687", alias="NEO4J_URI")
    user: str = Field(default="neo4j", alias="NEO4J_USER")
    password: str = Field(default="neo4j", alias="NEO4J_PASSWORD")
    
    # Compatible with legacy environment variables
    graph_db_uri: Optional[str] = Field(default=None, alias="GRAPH_DB_URI")
    graph_db_user: Optional[str] = Field(default=None, alias="GRAPH_DB_USER")
    graph_db_password: Optional[str] = Field(default=None, alias="GRAPH_DB_PASSWORD")
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )
    
    def get_uri(self) -> str:
        """Get URI (compatible with legacy environment variables)"""
        return self.graph_db_uri or self.uri
    
    def get_user(self) -> str:
        """Get username (compatible with legacy environment variables)"""
        return self.graph_db_user or self.user
    
    def get_password(self) -> str:
        """Get password (compatible with legacy environment variables)"""
        return self.graph_db_password or self.password


class BootstrapSettings(BaseSettings):
    """Startup initialization configuration"""
    
    # Whether to overwrite built-in data (prompts, knowledge bases, etc.)
    overwrite: bool = Field(default=False, alias="BOOTSTRAP_OVERWRITE")
    # Whether to overwrite built-in card type schemas
    overwrite_card_schemas: bool = Field(default=False, alias="BOOTSTRAP_OVERWRITE_CARD_SCHEMAS")
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )
    
    @property
    def should_overwrite(self) -> bool:
        """Whether to overwrite updates
        
        Supports multiple formats: true/false, 1/0, yes/no, on/off
        
        Returns:
            Whether to overwrite
        """
        if isinstance(self.overwrite, bool):
            return self.overwrite
        return str(self.overwrite).lower() in ('1', 'true', 'yes', 'on')

    @property
    def should_overwrite_card_schemas(self) -> bool:
        """Whether to overwrite built-in card type schemas."""
        if isinstance(self.overwrite_card_schemas, bool):
            return self.overwrite_card_schemas
        return str(self.overwrite_card_schemas).lower() in ('1', 'true', 'yes', 'on')


class AISettings(BaseSettings):
    """AI-related configuration"""
    
    # Maximum retry count on model call failure
    max_tool_call_retries: int = Field(default=3, alias="MAX_TOOL_CALL_RETRIES")
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


class AppSettings(BaseSettings):
    """Application configuration"""
    
    # Application name
    app_name: str = Field(default="NovelForge", alias="APP_NAME")
    
    # Application version
    app_version: str = Field(default="0.10.0", alias="APP_VERSION")
    
    # Whether to enable debug mode
    debug: bool = Field(default=False, alias="DEBUG")
    
    # API prefix
    api_prefix: str = Field(default="/api", alias="API_PREFIX")
    
    # Server bind address. NovelForge has no authentication or per-user authorization:
    # it is a local, single-user application. The default binds to loopback only;
    # exposing it on other interfaces (HOST=0.0.0.0) is unsupported and unsafe.
    host: str = Field(default="127.0.0.1", alias="HOST")
    port: int = Field(default=54321, alias="PORT")
    
    # CORS allowed origins (comma separated). The default is the local-only policy:
    # loopback dev-server origins on any port plus the "null" origin Electron sends for
    # file:// pages. Set CORS_ORIGINS="*" only when the backend is unreachable from other hosts.
    cors_origins: str = Field(default="local", alias="CORS_ORIGINS")
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )
    
    LOCAL_ORIGIN_REGEX: ClassVar[str] = r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"

    def get_cors_origins_list(self) -> list:
        """Explicit origin list; ``local`` adds only the Electron ``null`` origin (loopback origins come from the regex)."""
        if self.cors_origins == "*":
            return ["*"]
        if self.cors_origins.strip() == "local":
            return ["null"]
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    def get_cors_origin_regex(self) -> Optional[str]:
        return self.LOCAL_ORIGIN_REGEX if self.cors_origins.strip() == "local" else None

    def is_loopback_host(self) -> bool:
        return self.host.strip() in ("127.0.0.1", "localhost", "::1")


class ContextSettings(BaseSettings):
    """Context assembly budgets (characters) and graph traversal defaults."""

    facts_quota_chars: int = Field(default=5000, alias="CONTEXT_FACTS_QUOTA_CHARS")
    bible_quota_chars: int = Field(default=6000, alias="CONTEXT_BIBLE_QUOTA_CHARS")
    relation_radius: int = Field(default=1, alias="CONTEXT_RELATION_RADIUS")
    recent_chapters_window: int = Field(default=3, alias="CONTEXT_RECENT_CHAPTERS_WINDOW")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


class WorkflowSettings(BaseSettings):
    """Workflow configuration"""
    
    # Persistence record retention period (days)
    retention_persistent_days: int = Field(default=30, alias="WORKFLOW_RETENTION_PERSISTENT_DAYS")
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


class AutonomousSettings(BaseSettings):
    """Autonomous novel job durability and budget settings."""

    # Lease held by a worker on a job; must exceed the longest single provider call.
    lease_seconds: int = Field(default=300, alias="AUTONOMOUS_LEASE_SECONDS")
    # Independent heartbeat renews the lease this often (30-60 s recommended).
    heartbeat_seconds: int = Field(default=45, alias="AUTONOMOUS_HEARTBEAT_SECONDS")
    # Default job budgets (0 = unlimited); a job's own ``budget`` overrides these.
    default_max_calls: int = Field(default=0, alias="AUTONOMOUS_DEFAULT_MAX_CALLS")
    default_max_total_tokens: int = Field(default=0, alias="AUTONOMOUS_DEFAULT_MAX_TOTAL_TOKENS")
    default_max_repair_calls: int = Field(default=0, alias="AUTONOMOUS_DEFAULT_MAX_REPAIR_CALLS")
    # Test-only failpoints (never enabled in production); comma-separated failpoint names.
    failpoints: str = Field(default="", alias="AUTONOMOUS_FAILPOINTS")
    # Upload hardening.
    max_upload_bytes: int = Field(default=60 * 1024 * 1024, alias="AUTONOMOUS_MAX_UPLOAD_BYTES")
    max_zip_entries: int = Field(default=5000, alias="AUTONOMOUS_MAX_ZIP_ENTRIES")
    max_expanded_bytes: int = Field(default=400 * 1024 * 1024, alias="AUTONOMOUS_MAX_EXPANDED_BYTES")
    max_compression_ratio: int = Field(default=200, alias="AUTONOMOUS_MAX_COMPRESSION_RATIO")
    max_entry_bytes: int = Field(default=20 * 1024 * 1024, alias="AUTONOMOUS_MAX_ENTRY_BYTES")
    max_chapters: int = Field(default=2000, alias="AUTONOMOUS_MAX_CHAPTERS")
    max_text_chars: int = Field(default=30_000_000, alias="AUTONOMOUS_MAX_TEXT_CHARS")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


class Settings:
    """Global configuration object"""
    
    def __init__(self):
        self.database = DatabaseSettings()
        self.kg = KnowledgeGraphSettings()
        self.neo4j = Neo4jSettings()
        self.ai = AISettings()
        self.bootstrap = BootstrapSettings()
        self.workflow = WorkflowSettings()
        self.autonomous = AutonomousSettings()
        self.context = ContextSettings()
        self.app = AppSettings()
    
    def __repr__(self) -> str:
        return (
            f"Settings(\n"
            f"  database_url={self.database.get_database_url()},\n"
            f"  kg_provider={self.kg.provider},\n"
            f"  neo4j_uri={self.neo4j.get_uri()},\n"
            f"  max_retries={self.ai.max_tool_call_retries},\n"
            f"  bootstrap_overwrite={self.bootstrap.should_overwrite},\n"
            f"  bootstrap_overwrite_card_schemas={self.bootstrap.should_overwrite_card_schemas},\n"
            f"  app_name={self.app.app_name}\n"
            f")"
        )


# Global configuration instance
settings = Settings()