-- ranking_facts 数仓结构
-- 原始落地层（raw_*）：与 sources/*.csv 一一对应，只追加、不更新，完整保留源记录。
-- 双时间策展层（dim_*/fact_*/governance_*）：从原始层重建，携带系统记录时间
--   recorded_at（何时被系统获知）与业务时间（何时生效/发生），支持历史时点查询。

-- =====================================================================
-- 原始落地层
-- =====================================================================

CREATE TABLE IF NOT EXISTS raw_policy_scope (
    record_id       TEXT PRIMARY KEY,
    scope_id        TEXT NOT NULL,
    content_id      TEXT NOT NULL,
    policy_version  TEXT NOT NULL,
    effective_from  TEXT NOT NULL,
    effective_to    TEXT NOT NULL,
    scope_state     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_binding (
    record_id        TEXT PRIMARY KEY,
    binding_id       TEXT NOT NULL,
    policy_version   TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    channel_id       TEXT NOT NULL,
    valid_from       TEXT NOT NULL,
    valid_to         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_deployment (
    record_id        TEXT PRIMARY KEY,
    deployment_id    TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    channel_id       TEXT NOT NULL,
    started_at       TEXT NOT NULL,
    ended_at         TEXT NOT NULL,
    deploy_state     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_exposure (
    record_id        TEXT PRIMARY KEY,
    exposure_id      TEXT NOT NULL,
    content_id       TEXT NOT NULL,
    deployment_id    TEXT NOT NULL,
    occurred_at      TEXT NOT NULL,
    impression_count INTEGER NOT NULL,
    source_state     TEXT NOT NULL
);

-- 系统事件日志：唯一提供 recorded_at（记录时间）的来源，只追加。
CREATE TABLE IF NOT EXISTS raw_ingestion_log (
    log_id      TEXT PRIMARY KEY,
    source_file TEXT NOT NULL,
    record_id   TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    event_kind  TEXT NOT NULL,
    details     TEXT NOT NULL
);

-- =====================================================================
-- 双时间策展层（rebuild 时整体重建）
-- =====================================================================

-- 政策允许范围（双时间断言表）：
-- 每一行是系统在 recorded_at 对「scope_id 在业务区间 [valid_from, valid_to)
-- 内处于 scope_state」的一次断言。初始断言保留登记时认知的完整业务区间；
-- 处置撤销/收窄不修改旧行，而是追加一条仅覆盖变更后业务区间的新断言。
-- 历史时点查询在 recorded_at <= 认知时点 且业务区间覆盖该时点的行中，
-- 取 recorded_at 最大的断言——事后撤销既不会抹掉系统此前的认知，
-- 也不会把撤销前的业务时点改写成新状态。
CREATE TABLE IF NOT EXISTS dim_policy_scope (
    scope_id         TEXT NOT NULL,
    content_id       TEXT NOT NULL,
    policy_version   TEXT NOT NULL,
    valid_from       TEXT NOT NULL,
    valid_to         TEXT NOT NULL,
    scope_state      TEXT NOT NULL,
    recorded_at      TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    PRIMARY KEY (scope_id, recorded_at, valid_from)
);

-- 配置计划层：政策版本对「策略版本 × 渠道」的授权绑定。
CREATE TABLE IF NOT EXISTS dim_binding (
    binding_id       TEXT PRIMARY KEY,
    policy_version   TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    channel_id       TEXT NOT NULL,
    valid_from       TEXT NOT NULL,
    valid_to         TEXT NOT NULL,
    recorded_at      TEXT NOT NULL,
    source_record_id TEXT NOT NULL
);

-- 真实投放记录：部署是实际发生的运行事实，deploy_state 反映真实结局
-- （completed/partial/rollback 失败等），不按当前配置改写。
CREATE TABLE IF NOT EXISTS fact_deployment (
    deployment_id    TEXT PRIMARY KEY,
    strategy_version TEXT NOT NULL,
    channel_id       TEXT NOT NULL,
    started_at       TEXT NOT NULL,
    ended_at         TEXT NOT NULL,
    deploy_state     TEXT NOT NULL,
    recorded_at      TEXT NOT NULL,
    source_record_id TEXT NOT NULL
);

-- 真实曝光事实：只追加、永不更新或删除。is_verified 仅由新的核对事件推进，
-- 迟到记录在核实前始终为 0。
CREATE TABLE IF NOT EXISTS fact_exposure (
    exposure_id       TEXT PRIMARY KEY,
    content_id        TEXT NOT NULL,
    deployment_id     TEXT NOT NULL,
    occurred_at       TEXT NOT NULL,
    impression_count  INTEGER NOT NULL,
    source_state      TEXT NOT NULL,
    is_verified       INTEGER NOT NULL,
    recorded_at       TEXT NOT NULL,
    source_record_id  TEXT NOT NULL
);

-- 恢复/回滚动作：独立记录尝试与结局，失败的回滚同样保留。
CREATE TABLE IF NOT EXISTS fact_recovery_action (
    recovery_id      TEXT PRIMARY KEY,
    deployment_id    TEXT NOT NULL,
    action_type      TEXT NOT NULL,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    result           TEXT NOT NULL,
    recorded_at      TEXT NOT NULL,
    source_record_id TEXT NOT NULL
);

-- 治理任务：处置变更、迟到配置、待核实投放与回滚失败触发的核查项。
CREATE TABLE IF NOT EXISTS governance_task (
    task_id        TEXT PRIMARY KEY,
    task_type      TEXT NOT NULL,
    status         TEXT NOT NULL,
    opened_at      TEXT NOT NULL,
    resolved_at    TEXT,
    related_id     TEXT NOT NULL,
    evidence       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_exposure_occurred ON fact_exposure (occurred_at);
CREATE INDEX IF NOT EXISTS idx_exposure_deployment ON fact_exposure (deployment_id);
CREATE INDEX IF NOT EXISTS idx_scope_content ON dim_policy_scope (content_id);
CREATE INDEX IF NOT EXISTS idx_binding_strategy ON dim_binding (strategy_version, channel_id);
