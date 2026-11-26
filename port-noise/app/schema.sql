-- 声级仪噪声峰复核系统 —— 单季数据结构
PRAGMA foreign_keys = ON;

-- 设备：声级仪一台 + 至多 12 台吊机 + 可能出现的中间设备间
CREATE TABLE IF NOT EXISTS devices (
    id        TEXT PRIMARY KEY,
    role      TEXT NOT NULL CHECK (role IN ('meter', 'crane', 'room'))
);

-- 声级仪记录的夜间噪声峰
CREATE TABLE IF NOT EXISTS peaks (
    peak_id   TEXT PRIMARY KEY,
    peak_sec  INTEGER NOT NULL
);

-- 吊机作业区间 [start_sec, end_sec) —— 含首不含尾
CREATE TABLE IF NOT EXISTS work_intervals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    crane_id   TEXT NOT NULL REFERENCES devices(id),
    start_sec  INTEGER NOT NULL,
    end_sec    INTEGER NOT NULL CHECK (end_sec > start_sec)
);
-- 复核单峰/验单时按吊机 + 时刻走索引区间查询，读取量与全季区间总数无关
CREATE INDEX IF NOT EXISTS idx_work_crane_time
    ON work_intervals(crane_id, start_sec, end_sec);

-- 设备间钟差记录：(device_b 时钟读数) - (device_a 时钟读数) ∈ [low, high]，整秒。
-- rec_id 是用户侧记录编号；逐条更正会插入同 rec_id 的新版本，旧版本置 active=0，
-- 行主键用隐式 rowid，版本先后由 created_event（事件序号）区分。
CREATE TABLE IF NOT EXISTS clock_records (
    rec_id  INTEGER NOT NULL,
    device_a TEXT NOT NULL,
    device_b TEXT NOT NULL,
    low_sec  INTEGER NOT NULL,
    high_sec INTEGER NOT NULL CHECK (high_sec >= low_sec),
    active   INTEGER NOT NULL DEFAULT 1,
    superseded_by INTEGER,
    created_event INTEGER,
    CHECK (device_a <> device_b)
);
CREATE INDEX IF NOT EXISTS idx_clock_rec_id ON clock_records(rec_id, active);
CREATE INDEX IF NOT EXISTS idx_clock_active ON clock_records(active);

-- 事件流水：首传、更正、签发、撤销一律按入库先后取号，核定顺序即事件序号
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    kind    TEXT NOT NULL CHECK (kind IN ('upload', 'correction', 'issue', 'revoke')),
    detail  TEXT NOT NULL DEFAULT '{}'
);

-- 复核单。同一峰同时只允许一张有效单；旧单撤销后保留，改回原归属也不恢复
CREATE TABLE IF NOT EXISTS forms (
    form_no        TEXT PRIMARY KEY,
    peak_id        TEXT NOT NULL REFERENCES peaks(peak_id),
    crane_id       TEXT NOT NULL REFERENCES devices(id),
    status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
    created_event  INTEGER NOT NULL REFERENCES events(id),
    revoked_event  INTEGER REFERENCES events(id),
    revoke_reason  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_form_active_per_peak
    ON forms(peak_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_form_status ON forms(status);

-- --------------------------------------------------------------------------
-- 班组交接统计：按事件流水（events.id 即流水号）记录每台吊机在用复核单数
--
-- 层级化块汇总，块大小为 8。level=0 的叶子块覆盖 8 条流水，level=l 的块覆盖
-- 8^(l+1) 条；block_idx 为块在该层内的序号（从 0 开始）。
--   base_end_count：块起点前（不含本块）该吊机的有效单数
--   child_max      ：8 个子块各自“块内最高有效单数”（JSON 数组，稀疏可含 null）
--   child_end      ：8 个子块各自“块末有效单数”（同上）
--   leaf_deltas    ：仅叶子块，块内 8 条流水各自造成的单数增减（JSON 数组）
--
-- 任意区间核算只读 O(log8 N) 行汇总（百万流水下不超过几十行，硬上限 64 行），
-- 绝不逐条扫描 events/forms。
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS crane_count_stats (
    crane_id       TEXT NOT NULL,
    level          INTEGER NOT NULL,
    block_idx      INTEGER NOT NULL,
    base_end_count INTEGER NOT NULL,
    child_max      TEXT NOT NULL DEFAULT '[]',
    child_end      TEXT NOT NULL DEFAULT '[]',
    leaf_deltas    TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (crane_id, level, block_idx)
) WITHOUT ROWID;

-- 应用级元数据：记录统计汇总是否已对旧卷历史流水补齐（backfill）
CREATE TABLE IF NOT EXISTS app_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
