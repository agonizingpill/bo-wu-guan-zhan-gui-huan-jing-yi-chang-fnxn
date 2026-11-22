-- 盲样巡检（告警质量复核）模块
-- 一次评分产出的告警点与同柜同班未告警读数配对抽样；复核签认全部完成后才可揭盲。

CREATE TABLE IF NOT EXISTS qc_rounds (
  id SERIAL PRIMARY KEY,
  round_key VARCHAR(40) UNIQUE NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'dispatched'
    CHECK (status IN ('dispatched','reviewing','unblinded')),
  case_id INTEGER REFERENCES display_cases(id) ON DELETE SET NULL,
  source_run_id INTEGER REFERENCES algorithm_runs(id) ON DELETE SET NULL,
  idempotency_key VARCHAR(80) UNIQUE,
  alert_candidate_count INTEGER NOT NULL DEFAULT 0,
  matched_pair_count INTEGER NOT NULL DEFAULT 0,
  gap_count INTEGER NOT NULL DEFAULT 0,
  sample_count INTEGER NOT NULL DEFAULT 0,
  created_by INTEGER REFERENCES users(id),
  unblinded_by INTEGER REFERENCES users(id),
  unblinded_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS qc_samples (
  id SERIAL PRIMARY KEY,
  round_id INTEGER NOT NULL REFERENCES qc_rounds(id) ON DELETE CASCADE,
  sample_id INTEGER NOT NULL REFERENCES environment_samples(id) ON DELETE CASCADE,
  -- 全局不复用：同一条读数只能进入一次盲样（任何轮次），重试/重新评分都不可再抽
  UNIQUE(sample_id),
  -- 派单时刻冻结的角色与读数，重新评分不改变已派样本与原判
  frozen_role VARCHAR(20) NOT NULL CHECK (frozen_role IN ('alert','non_alert')),
  partner_sample_id INTEGER REFERENCES environment_samples(id) ON DELETE SET NULL,
  shift_key VARCHAR(20) NOT NULL,
  dispatch_no INTEGER NOT NULL,
  reading_sampled_at TIMESTAMPTZ NOT NULL,
  reading_temperature NUMERIC(5,2) NOT NULL,
  reading_humidity NUMERIC(5,2) NOT NULL,
  reading_co2 NUMERIC(7,2) NOT NULL,
  reading_door_open BOOLEAN NOT NULL,
  source_run_id INTEGER REFERENCES algorithm_runs(id) ON DELETE SET NULL,
  UNIQUE(round_id, dispatch_no)
);

CREATE TABLE IF NOT EXISTS qc_gaps (
  id SERIAL PRIMARY KEY,
  round_id INTEGER NOT NULL REFERENCES qc_rounds(id) ON DELETE CASCADE,
  event_id INTEGER NOT NULL REFERENCES anomaly_events(id) ON DELETE CASCADE,
  case_id INTEGER NOT NULL REFERENCES display_cases(id) ON DELETE CASCADE,
  shift_key VARCHAR(20) NOT NULL,
  reason VARCHAR(120) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(round_id, event_id)
);

CREATE TABLE IF NOT EXISTS qc_dispatch_jobs (
  id SERIAL PRIMARY KEY,
  idempotency_key VARCHAR(80) UNIQUE NOT NULL,
  round_id INTEGER REFERENCES qc_rounds(id) ON DELETE CASCADE,
  status VARCHAR(20) NOT NULL CHECK (status IN ('processing','succeeded','failed')),
  error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS qc_review_signatures (
  id SERIAL PRIMARY KEY,
  qc_sample_id INTEGER NOT NULL REFERENCES qc_samples(id) ON DELETE CASCADE,
  round_id INTEGER NOT NULL REFERENCES qc_rounds(id) ON DELETE CASCADE,
  reviewer_id INTEGER NOT NULL REFERENCES users(id),
  verdict VARCHAR(20) NOT NULL CHECK (verdict IN ('abnormal','normal')),
  note TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_qc_samples_round ON qc_samples(round_id, dispatch_no);
CREATE INDEX IF NOT EXISTS idx_qc_review_round ON qc_review_signatures(round_id, created_at);
CREATE INDEX IF NOT EXISTS idx_qc_gaps_round ON qc_gaps(round_id);
