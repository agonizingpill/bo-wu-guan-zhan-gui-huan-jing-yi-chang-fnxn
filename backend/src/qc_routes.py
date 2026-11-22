"""Blind-sampling QC routes: dispatch, blind review, unblind, findings.

Permission model (before unblinding the review role only sees sampling and
inspection material):
- supervisor: dispatch / retry dispatch / unblind, and may also review.
- operator:   blind review against frozen readings + inspection material only.
- viewer:     no blind material; may read post-unblinding findings.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from . import qc_store
from .middlewares.auth import current_user
from .utils.db import clean

router = APIRouter(prefix='/qc', tags=['盲样巡检'])

BLIND_QUEUE_COLS = (
    'id', 'dispatch_no', 'shift_key', 'round_id', 'reading_sampled_at',
    'reading_temperature', 'reading_humidity', 'reading_co2', 'reading_door_open',
    'case_code', 'gallery', 'zone', 'artifact_code', 'artifact_title',
    'target_temp_min', 'target_temp_max', 'target_rh_min', 'target_rh_max',
    'recent_checks', 'my_signatures',
)
ROUND_COLS = (
    'id', 'round_key', 'status', 'case_id', 'source_run_id', 'idempotency_key',
    'alert_candidate_count', 'matched_pair_count', 'gap_count', 'sample_count',
    'created_by', 'unblinded_by', 'unblinded_at', 'created_at',
)
GAP_COLS = ('id', 'shift_key', 'reason', 'event_id', 'case_code')
BLIND_GAP_COLS = ('id', 'shift_key', 'reason', 'case_code')
FINDING_COLS = (
    'dispatch_no', 'frozen_role', 'verdict', 'reading_sampled_at', 'reading_temperature',
    'reading_humidity', 'reading_co2', 'reading_door_open', 'case_code', 'artifact_title',
    'reviewer_name', 'finding',
)


class DispatchPayload(BaseModel):
    case_id: int | None = None
    idempotency_key: str = Field(default_factory=lambda: uuid.uuid4().hex, min_length=8, max_length=80)


class ReviewPayload(BaseModel):
    verdict: str = Field(pattern='^(abnormal|normal)$')
    note: str = Field(default='', max_length=500)


def _round(round_id):
    qc_store.ensure_schema()
    row = qc_store.get_round(round_id)
    if not row:
        raise HTTPException(404, '巡检轮次不存在')
    return row


@router.get('/rounds')
def rounds(user=Depends(current_user)):
    qc_store.ensure_schema()
    rows = qc_store.list_rounds()
    cols = ('id', 'round_key', 'status', 'alert_candidate_count', 'matched_pair_count',
            'gap_count', 'sample_count', 'created_at', 'unblinded_at', 'created_by_name')
    return clean([dict(zip(cols, row, strict=True)) for row in rows])


@router.post('/rounds/dispatch', status_code=201)
def dispatch(payload: DispatchPayload, response: Response, user=Depends(current_user)):
    if user['role'] != 'supervisor':
        raise HTTPException(403, '只有主管可以发起盲样派单')
    try:
        round_row, reused = qc_store.dispatch_round(payload.idempotency_key, payload.case_id, user['id'])
    except LookupError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:  # transaction rolled back; job stays failed and can be retried
        raise HTTPException(500, f'派单失败，可使用同一幂等键重试，不会重复抽样: {exc}') from exc
    if reused:
        response.status_code = 200
    return {'round': clean(dict(zip(ROUND_COLS, [round_row[c] for c in ROUND_COLS], strict=True))),
            'reused_existing_round': reused}


@router.get('/rounds/{round_id}/blind-queue')
def blind_queue(round_id: int, user=Depends(current_user)):
    if user['role'] not in {'operator', 'supervisor'}:
        raise HTTPException(403, '复核角色仅开放采样与巡检材料，观察员无盲样权限')
    round_row = _round(round_id)
    if round_row['status'] == 'unblinded':
        raise HTTPException(409, '本轮已揭盲，盲样队列已关闭，请查看抽检发现')
    rows = qc_store.blind_queue(round_id)
    items = []
    for row in rows:
        item = dict(zip(BLIND_QUEUE_COLS, row, strict=True))
        # signatures carry reviewer ids; strip them for the worklist, keep counts only
        sigs = item.pop('my_signatures') or []
        item['signature_count'] = len(sigs)
        item['latest_verdict'] = sigs[0]['verdict'] if sigs else None
        items.append(item)
    blind_gaps = []
    for g in qc_store.gaps_for(round_id, blind=True):
        # drop the null event slot entirely so no alert-identifying key leaks
        blind_gaps.append(dict(zip(BLIND_GAP_COLS, (g[0], g[1], g[2], g[4]), strict=True)))
    return {
        'round_id': round_id,
        'status': round_row['status'],
        'progress': qc_store.review_progress(round_id),
        'gaps': clean(blind_gaps),
        'items': clean(items),
        'material_scope': '仅开放采样与巡检材料：展柜档案、冻结读数与文物巡检记录；不含告警、分数与处置信息',
    }


@router.post('/samples/{qc_sample_id}/reviews', status_code=201)
def sign_review(qc_sample_id: int, payload: ReviewPayload, user=Depends(current_user)):
    if user['role'] not in {'operator', 'supervisor'}:
        raise HTTPException(403, '只有复核角色可以签认盲样')
    note = payload.note.strip()
    try:
        qc_store.add_signature(qc_sample_id, user['id'], payload.verdict, note)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {'qc_sample_id': qc_sample_id, 'verdict': payload.verdict, 'reviewer': user['display_name']}


@router.post('/rounds/{round_id}/unblind', status_code=200)
def unblind(round_id: int, user=Depends(current_user)):
    if user['role'] != 'supervisor':
        raise HTTPException(403, '只有主管可以揭盲')
    _round(round_id)
    try:
        changed = qc_store.unblind(round_id, user['id'])
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {'round_id': round_id, 'unblinded': changed}


@router.get('/rounds/{round_id}/findings')
def findings(round_id: int, user=Depends(current_user)):
    round_row = _round(round_id)
    if round_row['status'] != 'unblinded':
        raise HTTPException(409, '本轮尚未揭盲，抽检发现暂不可见')
    rows = qc_store.findings(round_id)
    items = clean([dict(zip(FINDING_COLS, row, strict=True)) for row in rows])
    summary = qc_store.finding_summary(round_id)
    gaps = clean([dict(zip(GAP_COLS, g, strict=True)) for g in qc_store.gaps_for(round_id)])
    return {
        'round': clean(round_row),
        'summary': summary,
        'gaps': gaps,
        'items': items,
        'finding_labels': {
            'missed_alert': '漏报：未告警读数被复核判为异常',
            'false_alert': '误报：告警读数被复核判为正常',
            'alert_hit': '告警命中：告警且复核判异常',
            'correct_clear': '判正常正确：未告警且复核判正常',
        },
        'generated_at': datetime.utcnow().isoformat(),
    }
