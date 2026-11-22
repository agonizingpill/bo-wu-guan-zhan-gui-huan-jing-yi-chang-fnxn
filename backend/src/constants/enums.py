CASE_STATUSES = ['active', 'maintenance', 'retired']
ANOMALY_STATUSES = ['open', 'triaged', 'escalated', 'resolved', 'closed']
ANOMALY_TYPES = ['environment_outlier', 'door_interference', 'continuous_drift']
DUTY_ACTION_TYPES = ['acknowledge', 'verify_door_sealed', 'adjust_climate', 'inspect_sensor', 'call_curator']
CHECK_RESULTS = ['pending', 'pass', 'fail']

# 盲样巡检
QC_ROUND_STATUSES = ['dispatched', 'reviewing', 'unblinded']
QC_FROZEN_ROLES = ['alert', 'non_alert']
QC_REVIEW_VERDICTS = ['abnormal', 'normal']
QC_FINDINGS = ['missed_alert', 'false_alert', 'alert_hit', 'correct_clear']
QC_DISPATCH_JOB_STATUSES = ['processing', 'succeeded', 'failed']
