"""Role-based access matrix for every /qc endpoint."""
import sys

import httpx

BASE = __import__('os').environ.get('QC_BASE_URL', 'http://127.0.0.1:19268')
PASS = FAIL = 0


def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  PASS  {name}')
    else:
        FAIL += 1
        print(f'  FAIL  {name}  {detail}')


def login(u):
    r = httpx.post(f'{BASE}/login', json={'username': u, 'password': 'museum123'})
    return {'Authorization': f"Bearer {r.json()['access_token']}"}


def main():
    sup, op, vw = login('guo-supervisor'), login('lin-operator'), login('viewer')

    # 先由主管建一轮并签完揭盲，供 findings 端点测试；另留一轮未揭盲供 blind-queue 测试
    r = httpx.post(f'{BASE}/qc/rounds/dispatch', headers=sup, json={'idempotency_key': 'matrix-dispatch-1'})
    rid = r.json()['round']['id']
    q = httpx.get(f'{BASE}/qc/rounds/{rid}/blind-queue', headers=op).json()
    for item in q.get('items', []):
        httpx.post(f'{BASE}/qc/samples/{item["id"]}/reviews', headers=op, json={'verdict': 'normal', 'note': '矩阵测试'})
    httpx.post(f'{BASE}/qc/rounds/{rid}/unblind', headers=sup)
    # 确认揭盲成功（零样本全缺口轮也可直接揭盲）
    assert httpx.get(f'{BASE}/qc/rounds/{rid}/findings', headers=sup).status_code == 200

    r2 = httpx.post(f'{BASE}/qc/rounds/dispatch', headers=sup, json={'idempotency_key': 'matrix-blind-open'})
    rid_open = r2.json()['round']['id']

    matrix = [
        # (endpoint, method, body, allowed_roles)
        ('/qc/rounds', 'GET', None, {'supervisor', 'operator', 'viewer'}),
        ('/qc/rounds/dispatch', 'POST', {'idempotency_key': 'matrix-perm-check'}, {'supervisor'}),
        (f'/qc/rounds/{rid_open}/blind-queue', 'GET', None, {'supervisor', 'operator'}),
        (f'/qc/rounds/{rid}/findings', 'GET', None, {'supervisor', 'operator', 'viewer'}),
        (f'/qc/rounds/{rid_open}/unblind', 'POST', {}, {'supervisor'}),
    ]
    roles = {'supervisor': sup, 'operator': op, 'viewer': vw}
    for path, method, body, allowed in matrix:
        for role, headers in roles.items():
            resp = httpx.request(method, BASE + path, headers=headers, json=body)
            if role in allowed:
                check(f'{role} {method} {path.split("/")[-1]} 允许', resp.status_code < 400, f'{resp.status_code} {resp.text[:80]}')
            else:
                check(f'{role} {method} {path.split("/")[-1]} 拒绝 403', resp.status_code == 403, f'{resp.status_code}')

    # 签认端点单独测（用一轮新的、未揭盲的轮次）
    r = httpx.post(f'{BASE}/qc/rounds/dispatch', headers=sup, json={'idempotency_key': 'matrix-dispatch-2'})
    if r.status_code in (200, 201) and r.json()['round']['sample_count']:
        rid2 = r.json()['round']['id']
        item = httpx.get(f'{BASE}/qc/rounds/{rid2}/blind-queue', headers=op).json()['items'][0]
        for role, headers in roles.items():
            resp = httpx.post(f'{BASE}/qc/samples/{item["id"]}/reviews', headers=headers, json={'verdict': 'normal', 'note': '矩阵'})
            if role in {'supervisor', 'operator'}:
                check(f'{role} 签认允许', resp.status_code == 201, resp.text[:80])
            else:
                check(f'viewer 签认拒绝 403', resp.status_code == 403, str(resp.status_code))

    # 未认证请求
    resp = httpx.get(f'{BASE}/qc/rounds')
    check('无令牌访问被拒 401', resp.status_code == 401, str(resp.status_code))

    print(f'\n权限矩阵: {PASS} 通过 / {FAIL} 失败')
    sys.exit(1 if FAIL else 0)


if __name__ == '__main__':
    main()
