"""SQLAlchemy Core 元数据：31 张表的单一事实来源，供 Alembic 自动生成迁移。

类型约定（与 SQLite 时代的语义对齐）：
- 所有字符串（含 UUID、ISO-8601 时间戳、JSON 文档）用 ``Text``；
- 版本/计数字段用 ``Integer``；附件内容用 ``LargeBinary``（BYTEA）；
- 原 SQLite ``rowid`` 排序改为显式 ``seq`` 自增列（PostgreSQL 无 rowid），
  仅加在历史上使用 ``ORDER BY rowid`` / ``MAX(rowid)`` 的表上，追加为末列。
"""

from sqlalchemy import (
    BigInteger,
    Column,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData()

# --- 根表 -------------------------------------------------------------------

projects = Table(
    "projects",
    metadata,
    Column("id", Text, primary_key=True),
    Column("document", Text, nullable=False),
)

pipeline_runs = Table(
    "pipeline_runs",
    metadata,
    Column("id", Text, primary_key=True),
    Column("project_id", Text, ForeignKey("projects.id"), nullable=False),
    Column("idempotency_key", Text, nullable=False),
    Column("input_hash", Text, nullable=False),
    Column("document", Text, nullable=False),
    UniqueConstraint("project_id", "idempotency_key"),
)

images = Table(
    "images",
    metadata,
    Column("id", Text, primary_key=True),
    Column("project_id", Text, ForeignKey("projects.id"), nullable=False),
    Column("idempotency_key", Text, nullable=False),
    Column("fingerprint", Text, nullable=False),
    Column("document", Text, nullable=False),
    UniqueConstraint("project_id", "idempotency_key"),
)

# --- 业务表（依赖 pipeline_runs / projects）----------------------------------

research_record_deletions = Table(
    "research_record_deletions",
    metadata,
    Column("run_id", Text, ForeignKey("pipeline_runs.id"), primary_key=True),
    Column("deleted_at", Text, nullable=False),
)

product_confirmations = Table(
    "product_confirmations",
    metadata,
    Column("run_id", Text, ForeignKey("pipeline_runs.id"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("document", Text, nullable=False),
    PrimaryKeyConstraint("run_id", "version"),
)

product_profiles = Table(
    "product_profiles",
    metadata,
    Column("run_id", Text, ForeignKey("pipeline_runs.id"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("document", Text, nullable=False),
    PrimaryKeyConstraint("run_id", "version"),
)

product_term_analyses = Table(
    "product_term_analyses",
    metadata,
    Column("id", Text, primary_key=True),
    Column("run_id", Text, ForeignKey("pipeline_runs.id"), nullable=False),
    Column("fingerprint", Text, nullable=False),
    Column("document", Text, nullable=False),
    Column("seq", BigInteger, Identity(), nullable=False),
    UniqueConstraint("run_id", "fingerprint"),
)

hot_search_core_analyses = Table(
    "hot_search_core_analyses",
    metadata,
    Column("id", Text, primary_key=True),
    Column("run_id", Text, ForeignKey("pipeline_runs.id"), nullable=False),
    Column("fingerprint", Text, nullable=False),
    Column("document", Text, nullable=False),
    Column("seq", BigInteger, Identity(), nullable=False),
    UniqueConstraint("run_id", "fingerprint"),
)

hot_search_core_current = Table(
    "hot_search_core_current",
    metadata,
    Column("run_id", Text, ForeignKey("pipeline_runs.id"), primary_key=True),
    Column(
        "analysis_id", Text, ForeignKey("hot_search_core_analyses.id"), nullable=False
    ),
)

hot_search_core_analysis_journal = Table(
    "hot_search_core_analysis_journal",
    metadata,
    Column(
        "analysis_id",
        Text,
        ForeignKey("hot_search_core_analyses.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("batch_id", Text, nullable=False),
    Column("candidate_id", Text, nullable=True),
    Column("kind", Text, nullable=False),
    Column("document", Text, nullable=False),
    Column("seq", BigInteger, Identity(), nullable=False),
    PrimaryKeyConstraint("analysis_id", "batch_id"),
    Index(
        "ix_hot_search_core_analysis_journal_candidate",
        "analysis_id",
        "candidate_id",
    ),
    Index(
        "ix_hot_search_core_analysis_journal_kind",
        "analysis_id",
        "kind",
    ),
)

hot_search_collection_jobs = Table(
    "hot_search_collection_jobs",
    metadata,
    Column("id", Text, primary_key=True),
    Column("run_id", Text, ForeignKey("pipeline_runs.id"), nullable=False),
    Column("fingerprint", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    Column("document", Text, nullable=False),
    Column("raw_document", Text, nullable=False),
    Column("seq", BigInteger, Identity(), nullable=False),
    UniqueConstraint("run_id", "fingerprint"),
)

keyword_library_drafts = Table(
    "keyword_library_drafts",
    metadata,
    Column("id", Text, primary_key=True),
    Column("run_id", Text, ForeignKey("pipeline_runs.id"), nullable=False),
    Column("project_id", Text, ForeignKey("projects.id"), nullable=False),
    Column("traffic_id", Text, nullable=False),
    Column("traffic_version", Integer, nullable=False),
    Column("document", Text, nullable=False),
    Column("revision_of", Text, nullable=False),
    Column("seq", BigInteger, Identity(), nullable=False),
    UniqueConstraint("run_id", "traffic_id", "traffic_version", "revision_of"),
)

keyword_library_publications = Table(
    "keyword_library_publications",
    metadata,
    Column("project_id", Text, ForeignKey("projects.id"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("draft_id", Text, ForeignKey("keyword_library_drafts.id"), nullable=False),
    Column("document", Text, nullable=False),
    PrimaryKeyConstraint("project_id", "version"),
    UniqueConstraint("draft_id"),
)

rule_sets = Table(
    "rule_sets",
    metadata,
    Column("id", Text, primary_key=True),
    Column("document", Text, nullable=False),
)

traffic_analyses = Table(
    "traffic_analyses",
    metadata,
    Column("id", Text, primary_key=True),
    Column("run_id", Text, ForeignKey("pipeline_runs.id"), nullable=False),
    Column("fingerprint", Text, nullable=False),
    Column("document", Text, nullable=False),
    Column("rows", Text, nullable=False),
    Column("seq", BigInteger, Identity(), nullable=False),
    UniqueConstraint("run_id", "fingerprint"),
)

traffic_reviews = Table(
    "traffic_reviews",
    metadata,
    Column("analysis_id", Text, ForeignKey("traffic_analyses.id"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("document", Text, nullable=False),
    PrimaryKeyConstraint("analysis_id", "version"),
)

cleaning_analyses = Table(
    "cleaning_analyses",
    metadata,
    Column("id", Text, primary_key=True),
    Column("run_id", Text, ForeignKey("pipeline_runs.id"), nullable=False),
    Column("fingerprint", Text, nullable=False),
    Column("document", Text, nullable=False),
    Column("phrases", Text, nullable=False),
    Column("frequencies", Text, nullable=False),
    Column("seq", BigInteger, Identity(), nullable=False),
    UniqueConstraint("run_id", "fingerprint"),
)

cleaning_reviews = Table(
    "cleaning_reviews",
    metadata,
    Column("analysis_id", Text, ForeignKey("cleaning_analyses.id"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("document", Text, nullable=False),
    PrimaryKeyConstraint("analysis_id", "version"),
)

cleaning_classifications = Table(
    "cleaning_classifications",
    metadata,
    Column("analysis_id", Text, ForeignKey("cleaning_analyses.id"), primary_key=True),
    Column("status", Text, nullable=False),
    Column("document", Text, nullable=False),
)

third_party_imports = Table(
    "third_party_imports",
    metadata,
    Column("id", Text, primary_key=True),
    Column("run_id", Text, ForeignKey("pipeline_runs.id"), nullable=False),
    Column("fingerprint", Text, nullable=False),
    Column("document", Text, nullable=False),
    Column("raw_document", Text, nullable=False),
    Column("results", Text, nullable=False),
    Column("seq", BigInteger, Identity(), nullable=False),
    UniqueConstraint("run_id", "fingerprint"),
)

third_party_import_files = Table(
    "third_party_import_files",
    metadata,
    Column("report_id", Text, ForeignKey("third_party_imports.id"), primary_key=True),
    Column("content", LargeBinary, nullable=False),
)

aba_expansions = Table(
    "aba_expansions",
    metadata,
    Column("id", Text, primary_key=True),
    Column("run_id", Text, ForeignKey("pipeline_runs.id"), nullable=False),
    Column("fingerprint", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("document", Text, nullable=False),
    Column("results", Text, nullable=False),
    Column("seq", BigInteger, Identity(), nullable=False),
    Index("aba_expansions_lookup", "run_id", "fingerprint", "created_at"),
)

competitor_versions = Table(
    "competitor_versions",
    metadata,
    Column("id", Text, primary_key=True),
    Column("run_id", Text, ForeignKey("pipeline_runs.id"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("document", Text, nullable=False),
    Column("seq", BigInteger, Identity(), nullable=False),
    UniqueConstraint("run_id", "version"),
)

aba_reports = Table(
    "aba_reports",
    metadata,
    Column("id", Text, primary_key=True),
    Column("run_id", Text, ForeignKey("pipeline_runs.id"), nullable=False),
    Column("fingerprint", Text, nullable=False),
    Column("context_hash", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("document", Text, nullable=False),
    Column("raw_document", Text, nullable=False),
    Column("seq", BigInteger, Identity(), nullable=False),
    Index("aba_reports_lookup", "run_id", "fingerprint", "created_at"),
)

core_analyses = Table(
    "core_analyses",
    metadata,
    Column("id", Text, primary_key=True),
    Column("run_id", Text, ForeignKey("pipeline_runs.id"), nullable=False),
    Column("fingerprint", Text, nullable=False),
    Column("report_id", Text, ForeignKey("aba_reports.id"), nullable=False),
    Column("document", Text, nullable=False),
    Column("seq", BigInteger, Identity(), nullable=False),
    UniqueConstraint("run_id", "fingerprint"),
)

core_prefilters = Table(
    "core_prefilters",
    metadata,
    Column("run_id", Text, ForeignKey("pipeline_runs.id"), nullable=False),
    Column("fingerprint", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("document", Text, nullable=False),
    PrimaryKeyConstraint("run_id", "fingerprint"),
)

core_selections = Table(
    "core_selections",
    metadata,
    Column("id", Text, primary_key=True),
    Column("run_id", Text, ForeignKey("pipeline_runs.id"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("document", Text, nullable=False),
    UniqueConstraint("run_id", "version"),
)

asin_products = Table(
    "asin_products",
    metadata,
    Column("run_id", Text, ForeignKey("pipeline_runs.id"), primary_key=True),
    Column("document", Text, nullable=False),
)

root_verifications = Table(
    "root_verifications",
    metadata,
    Column("id", Text, primary_key=True),
    Column("run_id", Text, ForeignKey("pipeline_runs.id"), nullable=False),
    Column("fingerprint", Text, nullable=False),
    Column("document", Text, nullable=False),
    UniqueConstraint("run_id", "fingerprint"),
)

root_candidates = Table(
    "root_candidates",
    metadata,
    Column("run_id", Text, ForeignKey("pipeline_runs.id"), primary_key=True),
    Column("status", Text, nullable=False),
    Column("document", Text),
)

root_selections = Table(
    "root_selections",
    metadata,
    Column("run_id", Text, ForeignKey("pipeline_runs.id"), primary_key=True),
    Column("document", Text, nullable=False),
)

product_understandings = Table(
    "product_understandings",
    metadata,
    Column("run_id", Text, ForeignKey("pipeline_runs.id"), primary_key=True),
    Column("status", Text, nullable=False),
    Column("document", Text),
)

# --- 工作流投影表（原独立 SQLite 文件，现同库同 schema）------------------------

workflow_runs = Table(
    "workflow_runs",
    metadata,
    Column("workflow_id", Text, primary_key=True),
    Column("run_id", Text, nullable=False),
    Column("project_id", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("state", Text, nullable=False),
    Column("view", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    Column("superseded_by", Text),
    Column("seq", BigInteger, Identity(), nullable=False),
    Index("workflow_runs_lookup", "run_id", "created_at"),
)

browser_operations = Table(
    "browser_operations",
    metadata,
    Column("operation_id", Text, primary_key=True),
    Column(
        "workflow_id", Text, ForeignKey("workflow_runs.workflow_id"), nullable=False
    ),
    Column("run_id", Text, ForeignKey("pipeline_runs.id"), nullable=False),
    Column("node_key", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("idempotency_key", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("auto_resume_remaining", Integer, nullable=False),
    Column("request_summary", Text, nullable=False),
    Column("result_ref", Text, nullable=False),
    Column("error_code", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    UniqueConstraint("run_id", "node_key", "source", "idempotency_key"),
    Index("browser_operations_waiting", "status", "updated_at"),
)


traffic_review_jobs = Table(
    "traffic_review_jobs", metadata,
    Column("analysis_id", Text, ForeignKey("traffic_analyses.id"), primary_key=True),
    Column("run_id", Text, ForeignKey("pipeline_runs.id"), nullable=False),
    Column("status", Text, nullable=False),
    Column("document", Text, nullable=False),
)
