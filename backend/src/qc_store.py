"""Persistence for blind-sampling QC rounds.

All dispatch work runs in one transaction and takes a transaction-scoped advisory
lock, so concurrent or retried dispatch requests can never double-sample or
create duplicate dispatch records. Re-scoring of anomaly events never touches
rows already frozen here.
"""

import json
import uuid

from .operations_evidence import query
from .utils.db import get_conn

ADVISORY_KEY = 710_690_112  # fixed namespace key for the QC dispatch lock

SCHEMA_SQL = """
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
  UNIQUE(sample_id),
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
"""

# Shift windows: night shift 18:00-06:59 (dated by the evening's calendar day),
# day shift 07:00-17:59. Alert and control must share case_id and this key.
SHIFT_EXPR_SAMPLES = """
CASE WHEN EXTRACT(HOUR FROM s.sampled_at) >= 18
     THEN CONCAT('night:', TO_CHAR(s.sampled_at::date, 'YYYY-MM-DD'))
     WHEN EXTRACT(HOUR FROM s.sampled_at) < 7
     THEN CONCAT('night:', TO_CHAR((s.sampled_at - INTERVAL '1 day')::date, 'YYYY-MM-DD'))
     ELSE CONCAT('day:', TO_CHAR(s.sampled_at::date, 'YYYY-MM-DD')) END
"""

SHIFT_EXPR_SM = SHIFT_EXPR_SAMPLES.replace('s.sampled_at', 'sm.sampled_at').replace('s.', 'sm.')


def ensure_schema():
    query(SCHEMA_SQL)


def get_round(round_id):
    rows = query('SELECT * FROM qc_rounds WHERE id=%s', (round_id,))
    if not rows:
        return None
    cols = [
        'id', 'round_key', 'status', 'case_id', 'source_run_id', 'idempotency_key',
        'alert_candidate_count', 'matched_pair_count', 'gap_count', 'sample_count',
        'created_by', 'unblinded_by', 'unblinded_at', 'created_at',
    ]
    return dict(zip(cols, rows[0], strict=True))


def list_rounds():
    return query(
        """SELECT r.id, r.round_key, r.status, r.alert_candidate_count, r.matched_pair_count,
                  r.gap_count, r.sample_count, r.created_at, r.unblinded_at,
                  u.display_name AS created_by_name
           FROM qc_rounds r LEFT JOIN users u ON u.id=r.created_by
           ORDER BY r.id DESC LIMIT 100"""
    )


def dispatch_round(idempotency_key, case_id, actor_id):
    """Idempotent dispatch. Returns (round_row, reused: bool).

    A previously succeeded job returns its original round untouched. A failed or
    interrupted job is retried inside a fresh transaction without re-sampling any
    reading already consumed and without creating a second dispatch job/round.
    """
    ensure_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT pg_advisory_xact_lock(%s)', (ADVISORY_KEY,))
            cur.execute(
                'SELECT id, round_id, status FROM qc_dispatch_jobs WHERE idempotency_key=%s FOR UPDATE',
                (idempotency_key,),
            )
            existing = cur.fetchone()
            reused = False
            if existing:
                job_id, old_round_id, status = existing
                if status == 'succeeded':
                    cur.execute('SELECT * FROM qc_rounds WHERE id=%s', (old_round_id,))
                    cols = [d.name for d in cur.description]
                    return dict(zip(cols, cur.fetchone(), strict=True)), True
                # processing/failed -> retry: reuse the same job row, drop any
                # partial round and its samples (UNIQUE(sample_id) rows go with it),
                # then run sampling again in this same transaction.
                cur.execute('DELETE FROM qc_rounds WHERE id=%s', (old_round_id,))
                round_id = _sample(cur, case_id, actor_id, idempotency_key)
                cur.execute(
                    "UPDATE qc_dispatch_jobs SET round_id=%s, status='succeeded', error=NULL, finished_at=NOW() WHERE id=%s",
                    (round_id, job_id),
                )
                reused = True
            else:
                # Fail fast before creating a job row when there is nothing to sample.
                if not _available_alert_exists(cur, case_id):
                    raise LookupError('当前没有未进入过盲样的告警点可抽样')
                cur.execute(
                    "INSERT INTO qc_dispatch_jobs (idempotency_key, status) VALUES (%s,'processing') RETURNING id",
                    (idempotency_key,),
                )
                job_id = cur.fetchone()[0]
                try:
                    round_id = _sample(cur, case_id, actor_id, idempotency_key)
                except Exception:
                    cur.execute(
                        "UPDATE qc_dispatch_jobs SET status='failed', error=%s, finished_at=NOW() WHERE id=%s",
                        ('抽样事务回滚，等待幂等重试', job_id),
                    )
                    raise
                cur.execute(
                    "UPDATE qc_dispatch_jobs SET round_id=%s, status='succeeded', finished_at=NOW() WHERE id=%s",
                    (round_id, job_id),
                )
            cur.execute('SELECT * FROM qc_rounds WHERE id=%s', (round_id,))
            cols = [d.name for d in cur.description]
            round_row = dict(zip(cols, cur.fetchone(), strict=True))
            return round_row, reused


def _available_alert_exists(cur, case_id):
    cur.execute(
        """SELECT 1 FROM anomaly_events e
           JOIN display_cases dc ON dc.id=e.case_id
           WHERE dc.status<>'retired'
             AND NOT EXISTS (SELECT 1 FROM qc_samples q WHERE q.sample_id=e.sample_id)
             AND (%s IS NULL OR e.case_id=%s) LIMIT 1""",
        (case_id, case_id),
    )
    return cur.fetchone() is not None


def _sample(cur, case_id, actor_id, idempotency_key):
    case_filter = 'AND e.case_id=%s' if case_id else ''
    params = (case_id,) if case_id else ()

    # Latest succeeded scoring run per case, to keep the round tied to "同次评分".
    cur.execute(
        f"""
        SELECT e.id, e.case_id, e.sample_id, e.algorithm_run_id, {SHIFT_EXPR_SM} AS shift_key,
               sm.sampled_at, sm.temperature, sm.humidity, sm.co2_ppm, sm.door_open,
               COALESCE(e.algorithm_run_id,
                 (SELECT ar.id FROM algorithm_runs ar
                  WHERE ar.case_id=e.case_id AND ar.status='succeeded'
                  ORDER BY ar.updated_at DESC LIMIT 1)) AS source_run_id
        FROM anomaly_events e
        JOIN environment_samples sm ON sm.id=e.sample_id
        JOIN display_cases dc ON dc.id=e.case_id
        WHERE dc.status<>'retired'
          AND NOT EXISTS (SELECT 1 FROM qc_samples q WHERE q.sample_id=e.sample_id)
          {case_filter}
        ORDER BY e.case_id, e.id
        """,
        params,
    )
    alerts = [dict(zip([d.name for d in cur.description], row, strict=True)) for row in cur.fetchall()]
    if not alerts:
        raise LookupError('当前没有可抽样的告警点')

    cur.execute(
        """INSERT INTO qc_rounds
           (round_key, status, case_id, source_run_id, idempotency_key, alert_candidate_count, created_by)
           VALUES (%s,'dispatched',%s,%s,%s,%s,%s) RETURNING id""",
        (f'QCR-{uuid.uuid4().hex[:12]}', case_id, alerts[0]['source_run_id'],
         idempotency_key, len(alerts), actor_id),
    )
    round_id = cur.fetchone()[0]

    used_sample_ids = set()
    dispatch_no = 0
    matched_pairs = 0
    gap_count = 0

    for alert in alerts:
        # Same cabinet + same shift, never alerted (no anomaly_event row),
        # never consumed by any QC round globally, accepted samples only.
        cur.execute(
            f"""
            SELECT s.id, s.sampled_at, s.temperature, s.humidity, s.co2_ppm, s.door_open,
                   {SHIFT_EXPR_SAMPLES} AS shift_key
            FROM environment_samples s
            JOIN display_cases dc ON dc.id=s.case_id
            WHERE s.case_id=%s AND s.state='accepted' AND dc.status<>'retired'
              AND NOT EXISTS (SELECT 1 FROM anomaly_events e WHERE e.sample_id=s.id)
              AND NOT EXISTS (SELECT 1 FROM qc_samples q WHERE q.sample_id=s.id)
              AND {SHIFT_EXPR_SAMPLES}=%s
            ORDER BY ABS(EXTRACT(EPOCH FROM (s.sampled_at - %s))), s.id
            """,
            (alert['case_id'], alert['shift_key'], alert['sampled_at']),
        )
        control = cur.fetchone()
        if not control:
            cur.execute(
                """INSERT INTO qc_gaps (round_id, event_id, case_id, shift_key, reason)
                   VALUES (%s,%s,%s,%s,%s)""",
                (round_id, alert['id'], alert['case_id'], alert['shift_key'],
                 '同柜同班没有未告警且未被其他盲样占用的读数，记为配样缺口'),
            )
            gap_count += 1
            continue

        control_id, c_at, c_temp, c_rh, c_co2, c_door, c_shift = control
        if control_id in used_sample_ids:
            # defensive: the UNIQUE(sample_id) constraint already prevents this
            cur.execute(
                """INSERT INTO qc_gaps (round_id, event_id, case_id, shift_key, reason)
                   VALUES (%s,%s,%s,%s,%s)""",
                (round_id, alert['id'], alert['case_id'], alert['shift_key'],
                 '候选对照读数已在本轮配对中占用，记为配样缺口'),
            )
            gap_count += 1
            continue

        dispatch_no += 1
        cur.execute(
            """INSERT INTO qc_samples
               (round_id, sample_id, frozen_role, partner_sample_id, shift_key, dispatch_no,
                reading_sampled_at, reading_temperature, reading_humidity, reading_co2, reading_door_open,
                source_run_id)
               VALUES (%s,%s,'alert',%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (round_id, alert['sample_id'], control_id, alert['shift_key'], dispatch_no,
             alert['sampled_at'], alert['temperature'], alert['humidity'], alert['co2_ppm'], alert['door_open'],
             alert['source_run_id']),
        )
        used_sample_ids.add(alert['sample_id'])

        dispatch_no += 1
        cur.execute(
            """INSERT INTO qc_samples
               (round_id, sample_id, frozen_role, partner_sample_id, shift_key, dispatch_no,
                reading_sampled_at, reading_temperature, reading_humidity, reading_co2, reading_door_open,
                source_run_id)
               VALUES (%s,%s,'non_alert',%s,%s,%s,%s,%s,%s,%s,%s,NULL)""",
            (round_id, control_id, alert['sample_id'], c_shift, dispatch_no,
             c_at, c_temp, c_rh, c_co2, c_door),
        )
        used_sample_ids.add(control_id)
        matched_pairs += 1

    cur.execute(
        'UPDATE qc_rounds SET matched_pair_count=%s, gap_count=%s, sample_count=%s WHERE id=%s',
        (matched_pairs, gap_count, dispatch_no, round_id),
    )
    source_run_ids = sorted({a['source_run_id'] for a in alerts if a['source_run_id'] is not None})
    cur.execute(
        """INSERT INTO audit_logs (actor_id, action, entity_type, entity_id, detail)
           VALUES (%s,'qc_dispatch','qc_round',%s,%s::jsonb)""",
        (actor_id, round_id, json.dumps({
            'idempotency_key': idempotency_key, 'case_id': case_id,
            'alerts': len(alerts), 'pairs': matched_pairs, 'gaps': gap_count,
            'source_run_ids': source_run_ids,
        }, ensure_ascii=False)),
    )
    return round_id


def blind_queue(round_id):
    """Reviewer-facing material: frozen readings + case/inspection context only.

    Deliberately excludes anomaly_events, scores, duty_actions and frozen_role so
    the review role cannot see which samples were alerted before unblinding.
    """
    return query(
        """
        SELECT q.id, q.dispatch_no, q.shift_key, q.round_id,
               q.reading_sampled_at, q.reading_temperature, q.reading_humidity,
               q.reading_co2, q.reading_door_open,
               dc.code AS case_code, dc.gallery, dc.zone, dc.artifact_code, dc.artifact_title,
               dc.target_temp_min, dc.target_temp_max, dc.target_rh_min, dc.target_rh_max,
               (SELECT json_agg(k) FROM (
                  SELECT result, check_type, checked_at, notes
                  FROM artifact_checks WHERE case_id=dc.id ORDER BY checked_at DESC LIMIT 5
               ) k) AS recent_checks,
               (SELECT json_agg(g) FROM (
                  SELECT verdict, note, created_at, reviewer_id
                  FROM qc_review_signatures WHERE qc_sample_id=q.id ORDER BY created_at DESC LIMIT 3
               ) g) AS my_signatures
        FROM qc_samples q
        JOIN display_cases dc ON dc.id=(SELECT case_id FROM environment_samples WHERE id=q.sample_id)
        WHERE q.round_id=%s
        ORDER BY q.dispatch_no
        """,
        (round_id,),
    )


def add_signature(qc_sample_id, reviewer_id, verdict, note):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT pg_advisory_xact_lock(%s)', (ADVISORY_KEY + 1,))
            cur.execute('SELECT round_id FROM qc_samples WHERE id=%s FOR UPDATE', (qc_sample_id,))
            row = cur.fetchone()
            if not row:
                raise LookupError('盲样不存在')
            round_id = row[0]
            cur.execute('SELECT status FROM qc_rounds WHERE id=%s', (round_id,))
            status = cur.fetchone()[0]
            if status == 'unblinded':
                raise PermissionError('本轮已揭盲，复核签认已关闭')
            cur.execute(
                'INSERT INTO qc_review_signatures (qc_sample_id, round_id, reviewer_id, verdict, note) VALUES (%s,%s,%s,%s,%s)',
                (qc_sample_id, round_id, reviewer_id, verdict, note),
            )
            cur.execute(
                """UPDATE qc_rounds SET status=CASE WHEN status='dispatched' THEN 'reviewing' ELSE status END
                   WHERE id=%s""",
                (round_id,),
            )
            cur.execute(
                """INSERT INTO audit_logs (actor_id, action, entity_type, entity_id, detail)
                   VALUES (%s,'qc_review_sign','qc_sample',%s,%s::jsonb)""",
                (reviewer_id, qc_sample_id,
                 json.dumps({'verdict': verdict, 'round_id': round_id}, ensure_ascii=False)),
            )


def review_progress(round_id):
    rows = query(
        """SELECT COUNT(*) AS total,
                  (SELECT COUNT(DISTINCT qc_sample_id) FROM qc_review_signatures WHERE round_id=%s) AS signed
           FROM qc_samples WHERE round_id=%s""",
        (round_id, round_id),
    )
    total, signed = rows[0]
    return {'total': total, 'signed': signed, 'remaining': total - signed}


def gaps_for(round_id, blind=False):
    """Gap records. In blind mode the event id is withheld so it cannot be used
    to infer which readings were alerted before unblinding."""
    if blind:
        return query(
            """SELECT g.id, g.shift_key, g.reason, NULL AS event_id, dc.code AS case_code
               FROM qc_gaps g JOIN display_cases dc ON dc.id=g.case_id
               WHERE g.round_id=%s ORDER BY g.id""",
            (round_id,),
        )
    return query(
        """SELECT g.id, g.shift_key, g.reason, g.event_id, dc.code AS case_code
           FROM qc_gaps g JOIN display_cases dc ON dc.id=g.case_id
           WHERE g.round_id=%s ORDER BY g.id""",
        (round_id,),
    )


def unblind(round_id, actor_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT pg_advisory_xact_lock(%s)', (ADVISORY_KEY + 2,))
            cur.execute('SELECT status FROM qc_rounds WHERE id=%s FOR UPDATE', (round_id,))
            row = cur.fetchone()
            if not row:
                raise LookupError('巡检轮次不存在')
            if row[0] == 'unblinded':
                return False
            cur.execute(
                """SELECT (SELECT COUNT(*) FROM qc_samples WHERE round_id=%s),
                          (SELECT COUNT(DISTINCT qc_sample_id) FROM qc_review_signatures WHERE round_id=%s)""",
                (round_id, round_id),
            )
            total, signed = cur.fetchone()
            # A round whose alerts could not be paired at all has no samples to
            # sign; its gaps are the whole result, so it can be unblinded directly.
            if total > 0 and signed < total:
                raise PermissionError(f'还有 {total - signed} 份盲样未签认，不能揭盲')
            cur.execute(
                'UPDATE qc_rounds SET status=%s, unblinded_by=%s, unblinded_at=NOW() WHERE id=%s',
                ('unblinded', actor_id, round_id),
            )
            cur.execute(
                """INSERT INTO audit_logs (actor_id, action, entity_type, entity_id, detail)
                   VALUES (%s,'qc_unblind','qc_round',%s,'{}'::jsonb)""",
                (actor_id, round_id),
            )
            return True


def findings(round_id):
    """Compare frozen original roles against latest signed verdicts."""
    return query(
        """
        WITH latest_sig AS (
            SELECT DISTINCT ON (qc_sample_id) qc_sample_id, verdict, reviewer_id, created_at
            FROM qc_review_signatures WHERE round_id=%s
            ORDER BY qc_sample_id, created_at DESC
        )
        SELECT q.dispatch_no, q.frozen_role, ls.verdict,
               q.reading_sampled_at, q.reading_temperature, q.reading_humidity, q.reading_co2,
               q.reading_door_open, dc.code AS case_code, dc.artifact_title,
               u.display_name AS reviewer_name,
               CASE WHEN q.frozen_role='alert' AND ls.verdict='normal' THEN 'false_alert'
                    WHEN q.frozen_role='non_alert' AND ls.verdict='abnormal' THEN 'missed_alert'
                    WHEN q.frozen_role='alert' AND ls.verdict='abnormal' THEN 'alert_hit'
                    ELSE 'correct_clear' END AS finding
        FROM qc_samples q
        JOIN environment_samples s ON s.id=q.sample_id
        JOIN display_cases dc ON dc.id=s.case_id
        LEFT JOIN latest_sig ls ON ls.qc_sample_id=q.id
        LEFT JOIN users u ON u.id=ls.reviewer_id
        WHERE q.round_id=%s
        ORDER BY q.dispatch_no
        """,
        (round_id, round_id),
    )


def finding_summary(round_id):
    rows = findings(round_id)
    summary = {'false_alert': 0, 'missed_alert': 0, 'alert_hit': 0, 'correct_clear': 0}
    for row in rows:
        summary[row[-1]] += 1
    return summary
