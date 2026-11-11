"""Application configuration; secrets never enter graph state."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")
    model_base_url: str = "http://127.0.0.1:8000/v1"
    model_name: str = "qwen3.8-27b"
    model_api_key: str = "not-required"
    prefix_cache_layout: bool = False
    # Zero means deployment window is unverified: use an explicitly estimated guard.
    model_context_tokens: int = Field(default=0, ge=0)
    model_tokenizer_path: str = ""
    model_tokenizer_url: str = ""
    model_token_safety_margin: int = Field(default=512, ge=0)
    context_fallback_tokens: int = Field(default=32768, ge=1024)
    context_history_tokens: int = Field(default=8000, ge=256)
    app_host: str = "0.0.0.0"
    app_port: int = 18110
    data_dir: Path = ROOT / "data"
    search_proxy: str = ""
    search_provider: Literal["auto", "ddgs", "tavily"] = "auto"
    tavily_api_key: str = ""
    web_timeout: float = Field(default=25, ge=1, le=60)
    web_max_bytes: int = Field(default=2_000_000, ge=1000, le=10_000_000)
    image_api_base: str = ""
    image_model: str = ""
    image_api_key: str = ""
    mcp_config: Path = ROOT / "mcp_servers.json"
    memory_enabled: bool = False
    memory_mode: Literal["lite", "legacy"] = "lite"
    memory_lite_limit: int = Field(default=40, ge=1, le=200)
    memory_recent_turns: int = Field(default=6, ge=3, le=20)
    memory_lite_timeout_s: float = Field(default=30, gt=0)
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_cache: Path = ROOT / "data/embedding_cache"
    qdrant_url: str = ""
    rag_enabled: bool = False
    retrieval_port: int = 0
    retrieval_token: str = ""
    memory_score_threshold: float = 0.55
    memory_recall_limit: int = 5
    memory_retry_limit: int = 3
    memory_recall_timeout_s: float = Field(default=2.0, gt=0)
    memory_recall_max_pending: int = Field(default=2, ge=1, le=8)
    memory_rerank_timeout_s: float = Field(default=8.0, gt=0)
    memory_rerank_threshold: float = Field(default=0.3, ge=0, le=1)
    memory_recall_char_limit: int = Field(default=6000, ge=500)
    memory_index_dir: Path | None = None
    memory_embedding_url: str = ""
    memory_embedding_model: str = "BAAI/bge-m3"
    memory_embedding_dimensions: int = Field(default=1024, ge=1)
    memory_embedding_max_tokens: int = Field(default=1024, ge=128)
    memory_embedding_timeout_s: float = Field(default=8.0, gt=0)
    memory_dense_timeout_s: float = Field(default=0.5, gt=0)
    rerank_url: str = ""
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_api_key: str = ""
    document_embedding_url: str = ""
    document_embedding_model: str = "BAAI/bge-m3"
    document_embedding_dimensions: int = Field(default=1024, ge=1)
    document_embedding_tokenizer: str = ""
    document_embedding_max_tokens: int = Field(default=1024, ge=32, le=1024)
    retrieval_query_count: int = Field(default=1, ge=1, le=3)
    retrieval_dense_k: int = Field(default=40, ge=6, le=100)
    retrieval_lexical_k: int = Field(default=40, ge=6, le=100)
    retrieval_hybrid_k: int = Field(default=20, ge=6, le=100)
    retrieval_candidate_k: int = Field(default=20, ge=6, le=100)
    retrieval_coarse_weight: float = Field(default=0.5, ge=0, le=1)
    retrieval_rerank_threshold: float | None = Field(default=None, ge=0, le=1)
    retrieval_neighbors: bool = False
    retrieval_context_tokens: int = Field(default=4096, ge=512, le=16000)

    # --- A2A 多智能体 ---
    a2a_secret: str = "dev-insecure-secret-change-me"
    a2a_state_dir: Path = ROOT / "a2a_state"
    planner_url: str = "http://127.0.0.1:18211"
    worker_url: str = "http://127.0.0.1:18212"
    critic_url: str = "http://127.0.0.1:18213"
    synthesizer_url: str = "http://127.0.0.1:18214"
    host_url: str = "http://127.0.0.1:18210"
    judge_plan: bool = True
    max_parallel: int = Field(default=3, ge=1, le=8)
    run_wall_clock_s: int = Field(default=900, ge=30, le=3600)
    task_wall_clock_s: int = Field(default=180, ge=20, le=900)
    task_max_tool_calls: int = Field(default=12, ge=1, le=40)
    plan_max_revisions: int = Field(default=1, ge=0, le=3)
    task_max_retries: int = Field(default=2, ge=0, le=5)
    recovery_max_retries: int = Field(default=3, ge=0, le=10)
    recovery_retry_delay_s: float = Field(default=3, ge=0, le=60)
    review_excerpt_chars: int = Field(default=24000, ge=2000, le=80000)

    @property
    def database(self) -> Path:
        return self.data_dir / "agentgpt.sqlite3"

    def agent_urls(self) -> dict[str, str]:
        return {
            "planner": self.planner_url,
            "worker": self.worker_url,
            "critic": self.critic_url,
            "synthesizer": self.synthesizer_url,
        }


class ModelOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    temperature: float = Field(default=0.3, ge=0, le=1)
    max_tokens: int = Field(default=2000, ge=128, le=8192)
    language: str = Field(default="简体中文", max_length=40)
    enabled_tools: list[str] = Field(
        default_factory=lambda: [
            "builtin__web_search",
            "builtin__web_fetch",
            "builtin__private_search",
            "builtin__private_read",
        ]
    )
    # 旧字段：保留以兼容既有前端与已存会话，实际工具预算取
    # max(max_tool_rounds, settings.task_max_tool_calls)，不会回到 3 轮的旧行为。
    max_tool_rounds: int = Field(default=3, ge=1, le=8)
    # 旧字段：Plan 修订次数已由 settings.plan_max_revisions 接管。
    max_replans: int = Field(default=8, ge=1, le=32)
    judge_plan: bool = True
    memory_scope: str = Field(default="", max_length=80, pattern=r"^[\w\-\u4e00-\u9fff ]*$")
    knowledge_base: str = Field(default="", max_length=80, pattern=r"^[\w\-\u4e00-\u9fff ]*$")


settings = Settings()
