"""End-to-end verification of blind-sampling QC rules against the running backend."""
import json
import sys
import threading

import httpx
import psycopg2

BASE = __import__('os').environ.get('QC_BASE_URL', 'http://127.0.0.1:19268')
DB = __import__('os').environ.get('DATABASE_URL', 'postgresql://museum@127.0.0.1:17768/museum_case')
PASS = 0
FAIL = 0


def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  PASS  {name}')
    else:
        FAIL += 1
        print(f'  FAIL  {name}  {detail}')


def sql(statement, params=()):
    conn = psycopg2.connect(DB)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(statement, params)
    rows = cur.fetchall() if cur.description else []
    conn.close()
    return rows


def login(username):
    r = httpx.post(f'{BASE}/login', json={'username': username, 'password': 'museum123'})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['access_token']}"}


def dispatch(headers, key, case_id=None):
    body = {'idempotency_key': key}
    if case_id is not None:
        body['case_id'] = case_id
    return httpx.post(f'{BASE}/qc/rounds/dispatch', headers=headers, json=body)


def main():
    sup_h = login('guo-supervisor')
    op_h = login('lin-operator')
    vw_h = login('viewer')

    print('1) 角色权限：派单仅主管，盲样仅复核角色')
    r = dispatch(op_h, 'perm-op-0001')
    check('operator 派单被拒 403', r.status_code == 403, r.text)
    r = dispatch(vw_h, 'perm-vw-0001')
    check('viewer 派单被拒 403', r.status_code == 403, r.text)

    print('2) 主管派单：同柜同班配对、配不齐记缺口、样本全局不复用')
    r = dispatch(sup_h, 'e2e-key-0001')
    check('首次派单 201', r.status_code == 201, r.text)
    round1 = r.json()['round']
    rid1 = round1['id']
    check('告警候选覆盖全部 12 个种子事件', round1['alert_candidate_count'] == 12, str(round1))
    check('配成 6 对（4 MC-06 + 2 MC-02/MC-03）', round1['matched_pair_count'] == 6, str(round1))
    check('MC-05 六个告警全部记缺口', round1['gap_count'] == 6, str(round1))
    check('样本数=配对数*2', round1['sample_count'] == 12, str(round1))

    gaps = sql('SELECT g.event_id, g.shift_key, g.reason, c.code FROM qc_gaps g JOIN display_cases c ON c.id=g.case_id WHERE g.round_id=%s', (rid1,))
    check('缺口均有原因、班次、展柜', all(g[2] and g[1].startswith(('night:', 'day:')) and g[3] for g in gaps))
    check('缺口全部来自 MC-05', {g[3] for g in gaps} == {'MC-05'}, str(gaps))

    pair_rows = sql(
        """SELECT q1.shift_key, s1.case_id, s2.case_id,
                  (SELECT shift_key FROM qc_samples q2 WHERE q2.sample_id=s2.id) AS partner_shift
           FROM qc_samples q1
           JOIN environment_samples s1 ON s1.id=q1.sample_id
           JOIN environment_samples s2 ON s2.id=q1.partner_sample_id
           WHERE q1.round_id=%s AND q1.frozen_role='alert'""",
        (rid1,),
    )
    check('每对同柜', all(a == b for _, a, b, _ in pair_rows), str(pair_rows))
    check('每对同班', all(sh == psh for sh, _, _, psh in pair_rows), str(pair_rows))
    check('全局无样本复用', not sql('SELECT sample_id FROM qc_samples GROUP BY sample_id HAVING COUNT(*)>1'))

    print('3) 幂等重试：同键不重复抽样、不重复派单')
    r = dispatch(sup_h, 'e2e-key-0001')
    check('同键返回复用标记', r.status_code == 200 and r.json().get('reused_existing_round') is True, f'{r.status_code} {r.text}')
    check('复用同一轮次', r.json()['round']['id'] == rid1)
    check('只有一条派单任务', sql('SELECT COUNT(*) FROM qc_dispatch_jobs WHERE idempotency_key=%s', ('e2e-key-0001',))[0][0] == 1)
    check('只有一个轮次', sql('SELECT COUNT(*) FROM qc_rounds WHERE idempotency_key=%s', ('e2e-key-0001',))[0][0] == 1)

    print('4) 并发派单同一幂等键：只落一轮')
    results = []
    errors = []

    def fire():
        try:
            resp = dispatch(sup_h, 'e2e-concurrent-key')
            if resp.status_code == 200 or resp.status_code == 201:
                results.append(resp.json())
            else:
                errors.append(resp.status_code)
        except Exception as exc:  # pragma: no cover - diagnostic only
            errors.append(str(exc))

    threads = [threading.Thread(target=fire) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ids = {x['round']['id'] for x in results}
    check('并发请求收敛到同一轮', len(ids) == 1, f'ids={ids} errors={errors}')
    check('并发轮与既有轮无样本冲突', not sql('SELECT sample_id FROM qc_samples GROUP BY sample_id HAVING COUNT(*)>1'))

    print('5) 失败任务重试：不重复抽样或派单')
    sql("INSERT INTO qc_dispatch_jobs (idempotency_key, status, error, finished_at) VALUES (%s,'failed','模拟派发失败',NOW())",
        ('e2e-failed-job',))
    r = dispatch(sup_h, 'e2e-failed-job')
    check('失败后重试成功', r.status_code == 200, r.text)
    rid_retry = r.json()['round']['id']
    check('任务仍只有一条', sql('SELECT COUNT(*) FROM qc_dispatch_jobs WHERE idempotency_key=%s', ('e2e-failed-job',))[0][0] == 1)
    check('任务状态翻为 succeeded', sql("SELECT status FROM qc_dispatch_jobs WHERE idempotency_key=%s", ('e2e-failed-job',))[0][0] == 'succeeded')
    check('重试轮与既有轮样本零交集',
          not sql('SELECT sample_id FROM qc_samples WHERE round_id=%s INTERSECT SELECT sample_id FROM qc_samples WHERE round_id=%s', (rid_retry, rid1)))

    print('6) 盲样队列：权限隔离 + 揭盲前看不到告警信息')
    check('viewer 看不到盲样 403', httpx.get(f'{BASE}/qc/rounds/{rid1}/blind-queue', headers=vw_h).status_code == 403)
    r = httpx.get(f'{BASE}/qc/rounds/{rid1}/blind-queue', headers=op_h)
    check('operator 可读盲样', r.status_code == 200, r.text)
    payload = r.json()
    check('材料范围声明存在', '采样与巡检材料' in payload['material_scope'])
    raw = json.dumps(payload, ensure_ascii=False)
    forbidden = ('frozen_role', 'anomaly', 'score', 'threshold', 'duty', 'event_id', 'severity', 'consecutive')
    check('载荷不含告警/分数/处置字段', not any(tok in raw for tok in forbidden), raw[:400])
    check('每条含冻结读数与展柜窗口', all(
        it['reading_temperature'] is not None and it['target_temp_min'] is not None and it['case_code']
        for it in payload['items']))
    check('盲样缺口不暴露告警 id', all('event_id' not in g for g in payload['gaps']))
    check('进度统计正确', payload['progress'] == {'total': 12, 'signed': 0, 'remaining': 12})

    print('7) 复核签认：未签完不能揭盲；揭盲后签认关闭')
    item_ids = [it['id'] for it in payload['items']]
    role_by_item = dict(sql('SELECT id, frozen_role FROM qc_samples WHERE round_id=%s', (rid1,)))
    false_alert_item = next(i for i in item_ids if role_by_item[i] == 'alert')
    missed_item = next(i for i in item_ids if role_by_item[i] == 'non_alert')

    def expected_verdict(i):
        if i == false_alert_item:
            return 'normal'      # 告警 -> 复核正常 = 误报
        if i == missed_item:
            return 'abnormal'    # 未告警 -> 复核异常 = 漏报
        return {'alert': 'abnormal', 'non_alert': 'normal'}[role_by_item[i]]

    check('operator 揭盲被拒 403', httpx.post(f'{BASE}/qc/rounds/{rid1}/unblind', headers=op_h).status_code == 403)
    check('未签完揭盲被拒 409', httpx.post(f'{BASE}/qc/rounds/{rid1}/unblind', headers=sup_h).status_code == 409)
    for i in item_ids[:-1]:
        rr = httpx.post(f'{BASE}/qc/samples/{i}/reviews', headers=op_h, json={'verdict': expected_verdict(i), 'note': '复核签认'})
        assert rr.status_code == 201, rr.text
    check('差一份未签仍拒绝揭盲', httpx.post(f'{BASE}/qc/rounds/{rid1}/unblind', headers=sup_h).status_code == 409)
    rr = httpx.post(f'{BASE}/qc/samples/{item_ids[-1]}/reviews', headers=op_h,
                    json={'verdict': expected_verdict(item_ids[-1]), 'note': '末份签认'})
    check('末份签认 201', rr.status_code == 201, rr.text)
    check('轮次进入 reviewing', sql("SELECT status FROM qc_rounds WHERE id=%s", (rid1,))[0][0] == 'reviewing')
    rr = httpx.post(f'{BASE}/qc/samples/{item_ids[0]}/reviews', headers=op_h, json={'verdict': 'normal', 'note': '揭盲前允许补签留痕'})
    check('揭盲前可补签', rr.status_code == 201, rr.text)
    r = httpx.post(f'{BASE}/qc/rounds/{rid1}/unblind', headers=sup_h)
    check('全部签完揭盲成功', r.status_code == 200 and r.json()['unblinded'] is True, r.text)
    check('重复揭盲幂等返回', httpx.post(f'{BASE}/qc/rounds/{rid1}/unblind', headers=sup_h).json()['unblinded'] is False)
    check('揭盲后签认关闭 409',
          httpx.post(f'{BASE}/qc/samples/{item_ids[0]}/reviews', headers=op_h,
                     json={'verdict': 'normal', 'note': '揭盲后应拒绝'}).status_code == 409)
    check('揭盲后盲样队列关闭', httpx.get(f'{BASE}/qc/rounds/{rid1}/blind-queue', headers=op_h).status_code == 409)

    print('8) 抽检发现：漏报/误报统计与逐项结果')
    r = httpx.get(f'{BASE}/qc/rounds/{rid1}/findings', headers=vw_h)
    check('viewer 揭盲后可看发现', r.status_code == 200, r.text)
    findings = r.json()
    check('误报计数=1', findings['summary']['false_alert'] == 1, str(findings['summary']))
    check('漏报计数=1', findings['summary']['missed_alert'] == 1, str(findings['summary']))
    check('其余判定自洽', findings['summary']['alert_hit'] == 5 and findings['summary']['correct_clear'] == 5, str(findings['summary']))
    check('发现条数=样本数', len(findings['items']) == 12)
    check('缺口随发现一并展示且含事件 id', len(findings['gaps']) == 6 and all(g['event_id'] for g in findings['gaps']))
    check('漏报/误报有中文口径', '漏报' in findings['finding_labels']['missed_alert'] and '误报' in findings['finding_labels']['false_alert'])

    print('9) 重新评分不改已派样本与原判')
    before = sql('SELECT frozen_role, reading_temperature, reading_humidity, reading_co2, partner_sample_id FROM qc_samples WHERE round_id=%s ORDER BY dispatch_no', (rid1,))
    sql("""UPDATE anomaly_events SET score=9.99, threshold=0.0, status='closed', reason='重新评分覆盖测试'
           WHERE sample_id IN (SELECT sample_id FROM qc_samples WHERE round_id=%s AND frozen_role='alert')""", (rid1,))
    after = sql('SELECT frozen_role, reading_temperature, reading_humidity, reading_co2, partner_sample_id FROM qc_samples WHERE round_id=%s ORDER BY dispatch_no', (rid1,))
    check('已派样本冻结字段不变', before == after)
    r = httpx.get(f'{BASE}/qc/rounds/{rid1}/findings', headers=sup_h)
    check('原判口径仍来自冻结角色', r.json()['summary'] == findings['summary'], r.text)

    print('10) 第二轮派单：样本不复用，配不齐继续记缺口')
    r = dispatch(sup_h, 'e2e-key-0002')
    check('第二轮派单成功（新建轮 201）', r.status_code == 201, r.text)
    rid2 = r.json()['round']['id']
    check('两轮样本零交集',
          not sql('SELECT sample_id FROM qc_samples WHERE round_id=%s INTERSECT SELECT sample_id FROM qc_samples WHERE round_id=%s', (rid1, rid2)))
    check('MC-05 缺口在第二轮继续记录', r.json()['round']['gap_count'] >= 6, str(r.json()['round']))
    check('第二轮无可配对告警时配对数为 0', r.json()['round']['matched_pair_count'] == 0, str(r.json()['round']))

    print('11) 指定展柜派单、无候选报错与审计留痕')
    # 正向：选一个告警尚未被任何轮次消耗的展柜单独派单（MC-02 的告警在全量轮已抽，改用未消耗展柜）
    available = sql(
        """SELECT c.id, c.code FROM display_cases c
           WHERE EXISTS (SELECT 1 FROM anomaly_events e WHERE e.case_id=c.id
                         AND NOT EXISTS (SELECT 1 FROM qc_samples q WHERE q.sample_id=e.sample_id))
           ORDER BY c.code LIMIT 1"""
    )
    if available:
        case_pk, case_code = available[0]
        r = dispatch(sup_h, f'e2e-case-{case_code.lower()}', case_pk)
        check('指定展柜派单成功', r.status_code in (200, 201), r.text)
        rid3 = r.json()['round']['id']
        check('轮次只含指定展柜样本', not sql(
            """SELECT 1 FROM qc_samples q JOIN environment_samples s ON s.id=q.sample_id
               WHERE q.round_id=%s AND s.case_id<>%s LIMIT 1""", (rid3, case_pk)))
    else:
        check('存在未消耗告警的展柜可供定向派单', False, '所有告警均已入盲样')
    mc04 = sql("SELECT id FROM display_cases WHERE code='MC-04'")[0][0]
    r = dispatch(sup_h, 'e2e-mc04-0001', mc04)
    check('无未抽样告警的展柜返回 409', r.status_code == 409, r.text)
    check('失败时不留任务行', sql("SELECT COUNT(*) FROM qc_dispatch_jobs WHERE idempotency_key='e2e-mc04-0001'")[0][0] == 0)
    audit_rows = dict(sql("SELECT action, COUNT(*) FROM audit_logs WHERE action LIKE 'qc_%%' GROUP BY action"))
    check('派单/签认/揭盲均有审计', {'qc_dispatch', 'qc_review_sign', 'qc_unblind'} <= set(audit_rows), str(audit_rows))

    print(f'\n结果: {PASS} 通过 / {FAIL} 失败')
    sys.exit(1 if FAIL else 0)


if __name__ == '__main__':
    main()
