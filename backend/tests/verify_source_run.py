"""Verify source_run_id freezes the scoring run that produced each alert."""
from src.services.algorithm import run_case_analysis
from src.utils.db import execute, fetch_all, fetch_one
from src import qc_store

# Run real scoring on MC-06 (12 samples >= 6 required)
case = fetch_one("SELECT id FROM display_cases WHERE code='MC-06'")
run_row = execute_returning_run = None
from src.utils.db import execute_returning
run_row = execute_returning(
    "INSERT INTO algorithm_runs (run_key, case_id, status, input_summary, parameters) VALUES (%s,%s,'queued','{}'::jsonb,'{}'::jsonb) RETURNING *",
    (f'verify-run-{case["id"]}', case['id']),
)
run_case_analysis(case['id'], run_row['id'])
events = fetch_all(
    "SELECT id, algorithm_run_id FROM anomaly_events WHERE case_id=%s AND algorithm_run_id IS NOT NULL ORDER BY id",
    (case['id'],),
)
print('events with run id:', [(e['id'], e['algorithm_run_id']) for e in events])
assert events, 'algorithm run should link events to the run'

row, reused = qc_store.dispatch_round('verify-source-run-1', case['id'], 2)
print('dispatch:', row['id'], 'pairs', row['matched_pair_count'])
frozen = fetch_all(
    """SELECT frozen_role, source_run_id FROM qc_samples
       WHERE round_id=%s ORDER BY dispatch_no""",
    (row['id'],),
)
print('frozen rows:', frozen)
alert_rows = [f for f in frozen if f['frozen_role'] == 'alert']
control_rows = [f for f in frozen if f['frozen_role'] == 'non_alert']
assert all(f['source_run_id'] == run_row['id'] for f in alert_rows), 'alert rows must freeze the source run'
assert all(f['source_run_id'] is None for f in control_rows), 'control rows have no source run'
print('PASS: alert samples freeze the producing algorithm run; controls leave it NULL')
