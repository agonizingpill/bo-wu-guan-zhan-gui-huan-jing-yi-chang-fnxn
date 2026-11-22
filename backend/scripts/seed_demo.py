import hashlib
import os
from datetime import datetime, timedelta, timezone

from src.utils.db import execute, execute_returning, fetch_one, wait_for_db


def password(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def get_user(username, display_name, role):
    row = execute_returning(
        '''INSERT INTO users (username, display_name, role, password_hash)
           VALUES (%s,%s,%s,%s)
           ON CONFLICT (username) DO UPDATE SET display_name=EXCLUDED.display_name, role=EXCLUDED.role
           RETURNING id, username, display_name, role''',
        (username, display_name, role, password('museum123')),
    )
    return row or fetch_one('SELECT id, username, display_name, role FROM users WHERE username=%s', (username,))


def get_case(data):
    row = execute_returning(
        '''INSERT INTO display_cases
           (code, gallery, zone, artifact_code, artifact_title, target_temp_min, target_temp_max,
            target_rh_min, target_rh_max, status, door_sensor_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (code) DO UPDATE SET gallery=EXCLUDED.gallery, zone=EXCLUDED.zone,
             artifact_code=EXCLUDED.artifact_code, artifact_title=EXCLUDED.artifact_title
           RETURNING id''',
        data,
    )
    return row['id'] if row else fetch_one('SELECT id FROM display_cases WHERE code=%s', (data[0],))['id']


def sample(case_id, when, temp, rh, co2, door, note):
    row = execute_returning(
        '''INSERT INTO environment_samples
           (case_id, sampled_at, temperature, humidity, co2_ppm, door_open, source, state, note, created_by)
           VALUES (%s,%s,%s,%s,%s,%s,'seed','accepted',%s,NULL)
           ON CONFLICT (case_id, sampled_at) DO NOTHING RETURNING id''',
        (case_id, when, temp, rh, co2, door, note),
    )
    if row:
        return row['id']
    return fetch_one('SELECT id FROM environment_samples WHERE case_id=%s AND sampled_at=%s', (case_id, when))['id']


def seed_event(case_id, sample_id, score, threshold, anomaly_type, severity, status, door_context, consecutive, escalated, reason):
    existing = fetch_one('SELECT id FROM anomaly_events WHERE sample_id=%s', (sample_id,))
    if existing:
        return existing['id']
    row = execute_returning(
        '''INSERT INTO anomaly_events
           (case_id, sample_id, score, threshold, anomaly_type, severity, status, door_context,
            consecutive_count, escalated_to_curator, reason)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
        (case_id, sample_id, score, threshold, anomaly_type, severity, status, door_context, consecutive, escalated, reason),
    )
    return row['id']


def main():
    wait_for_db()
    operator = get_user('lin-operator', '林芮 · 夜班值守', 'operator')
    supervisor = get_user('guo-supervisor', '郭岚 · 藏品主管', 'supervisor')
    get_user('viewer', '顾言 · 研究阅览', 'viewer')

    cases = {}
    cases['MC-01'] = get_case(('MC-01', '青铜器展厅', '东侧 02 号展柜', 'BR-018', '错金银云纹铜犀尊', 19.0, 23.0, 45.0, 55.0, 'active', 'door-mc01'))
    cases['MC-02'] = get_case(('MC-02', '丝路文物展厅', '西侧 07 号展柜', 'SR-044', '唐三彩骆驼俑', 18.0, 22.0, 42.0, 52.0, 'active', 'door-mc02'))
    cases['MC-03'] = get_case(('MC-03', '玉器展厅', '北侧 03 号展柜', 'JA-006', '汉代龙纹玉璧', 20.0, 24.0, 40.0, 50.0, 'active', 'door-mc03'))
    cases['MC-04'] = get_case(('MC-04', '瓷器展厅', '南侧 05 号展柜', 'PO-071', '北宋汝窑天青釉盘', 18.5, 22.5, 44.0, 54.0, 'active', 'door-mc04'))
    cases['MC-05'] = get_case(('MC-05', '古籍善本展厅', '恒温库房 01 号展柜', 'BK-109', '明代地方志善本函套', 18.0, 20.0, 45.0, 50.0, 'active', 'door-mc05'))
    cases['MC-06'] = get_case(('MC-06', '陶瓷展厅', '中央独立展柜', 'CE-203', '元青花缠枝莲纹梅瓶', 19.0, 23.0, 46.0, 56.0, 'active', 'door-mc06'))

    base = datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc)
    for index, (temp, rh, co2) in enumerate([(21.0, 49.0, 620), (21.2, 49.5, 640), (20.9, 48.8, 610), (21.1, 49.2, 630), (21.0, 49.1, 625), (21.2, 49.3, 635), (21.1, 49.0, 615), (21.0, 49.2, 628)]):
        sample(cases['MC-01'], base + timedelta(minutes=index * 10), temp, rh, co2, False, '夜班稳态基线')

    door_ids = []
    for index, values in enumerate([(20.4, 47.2, 720, False), (20.3, 47.0, 715, True), (22.8, 60.0, 1200, True), (22.5, 59.0, 1150, True), (21.0, 50.0, 700, False), (21.1, 49.6, 680, False), (21.0, 49.5, 675, False)]):
        door_ids.append(sample(cases['MC-02'], base + timedelta(minutes=index * 10), *values, '门磁开启期间的光照调整'))
    seed_event(cases['MC-02'], door_ids[2], -0.14, -0.10, 'door_interference', 'watch', 'open', True, 1, False, '门磁开启段内温湿度突变，等待关门后复采')

    drift_ids = []
    for index, values in enumerate([(21.2, 45.0, 650), (21.4, 45.2, 665), (23.8, 57.0, 1100), (24.4, 59.0, 1240), (24.8, 61.0, 1370), (21.8, 47.0, 700), (21.4, 46.0, 680)]):
        drift_ids.append(sample(cases['MC-03'], base + timedelta(minutes=index * 10), *values, False, '连续偏移观察样本'))
    seed_event(cases['MC-03'], drift_ids[4], -0.31, -0.12, 'continuous_drift', 'critical', 'escalated', False, 3, True, '连续三个采样点超出稳态基线，已升级馆长查看')

    for index, values in enumerate([(20.1, 50.0, 590), (20.0, 50.2, 605), (20.2, 49.8, 600), (20.1, 50.1, 610), (20.0, 49.9, 600), (20.2, 50.0, 615)]):
        sample(cases['MC-04'], base + timedelta(minutes=index * 10), *values, False, '日常巡检基线')

    # MC-05：同班所有读数都已形成告警，没有未告警读数可配对 -> 盲样派单时必须记缺口
    gap_ids = []
    for index, values in enumerate([(24.6, 58.4, 1320), (24.9, 59.1, 1380), (25.2, 60.0, 1410),
                                    (25.0, 59.6, 1395), (24.8, 58.9, 1360), (24.7, 58.5, 1335)]):
        gap_ids.append(sample(cases['MC-05'], base + timedelta(minutes=index * 10), *values, False, '恒温库房连续越限，全部进入告警'))
    for index, sample_id in enumerate(gap_ids):
        seed_event(cases['MC-05'], sample_id, -0.22 - index * 0.01, -0.12,
                   'continuous_drift' if index >= 2 else 'environment_outlier',
                   'critical' if index >= 2 else 'high',
                   'escalated' if index >= 2 else 'open',
                   False, min(index + 1, 3), index >= 2,
                   '恒温库房温湿连续越限，同班无未告警样本可配对')

    # MC-06：12 条读数中 4 条告警、8 条未告警，可支撑多轮盲样配对
    vase_ids = []
    vase_series = [
        (20.4, 48.2, 640), (20.5, 48.0, 650), (20.3, 48.4, 635), (23.6, 56.8, 1180),
        (20.6, 48.1, 648), (23.9, 57.4, 1220), (20.4, 48.3, 642), (20.5, 48.0, 655),
        (24.2, 58.0, 1260), (20.6, 48.2, 646), (24.0, 57.6, 1235), (20.5, 48.1, 650),
    ]
    for index, values in enumerate(vase_series):
        vase_ids.append(sample(cases['MC-06'], base + timedelta(minutes=index * 10), *values, False, '独立展柜夜间序列'))
    for index in (3, 5, 8, 10):
        seed_event(cases['MC-06'], vase_ids[index], -0.18 - index * 0.01, -0.11,
                   'environment_outlier', 'high', 'open', False, 1, False,
                   '温湿与 CO2 联合偏离独立展柜稳态基线')

    # 巡检材料：供盲样复核角色在揭盲前查阅（不含告警/处置信息）
    execute(
        '''INSERT INTO artifact_checks (case_id, event_id, check_type, result, notes, inspector_id, checked_at)
           SELECT %s, NULL, 'routine_round', 'pass', '例行巡检：函套无霉斑，柜体玻璃与密封条正常', %s, %s
           WHERE NOT EXISTS (SELECT 1 FROM artifact_checks WHERE case_id=%s AND check_type='routine_round')''',
        (cases['MC-05'], operator['id'], base + timedelta(minutes=5), cases['MC-05']),
    )
    execute(
        '''INSERT INTO artifact_checks (case_id, event_id, check_type, result, notes, inspector_id, checked_at)
           SELECT %s, NULL, 'routine_round', 'pass', '例行巡检：玉璧托架稳固，展柜湿度记录连续', %s, %s
           WHERE NOT EXISTS (SELECT 1 FROM artifact_checks WHERE case_id=%s AND check_type='routine_round')''',
        (cases['MC-03'], operator['id'], base - timedelta(minutes=20), cases['MC-03']),
    )

    execute("UPDATE display_cases SET last_sample_at=(SELECT MAX(sampled_at) FROM environment_samples WHERE case_id=display_cases.id)")
    execute(
        '''INSERT INTO audit_logs (actor_id, action, entity_type, entity_id, detail)
           SELECT %s, 'seed_demo', 'batch', NULL, '{"source":"seed_demo","batch":"night-shift-2026-11-21"}'::jsonb
           WHERE NOT EXISTS (SELECT 1 FROM audit_logs WHERE action='seed_demo' AND detail->>'batch'='night-shift-2026-11-21')''',
        (operator['id'],),
    )
    print('seed_demo complete: 6 display cases, 52 samples, 12 seeded anomaly events, 2 routine checks, 3 users')


if __name__ == '__main__':
    main()
