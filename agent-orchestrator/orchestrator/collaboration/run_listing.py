"""工作台运行列表摘要、游标编解码与全历史筛选分页。"""
from __future__ import annotations

import base64
import json
import math

CURSOR_VERSION = 3
CURSOR_MAX_LEN = 8192  # 容纳 200 个 Unicode 字符编码后的合法查询

# 合法 status 值及其对应的底层 lifecycle 集合
STATUS_GROUPS: dict[str, set[str] | None] = {
    'all': None,
    'running': {'pending', 'running', 'recovering'},
    'paused': {'paused'},
    'waiting_approval': {'waiting_approval'},
    'waiting_external': {'waiting_external'},
    'waiting_owner': {'waiting_owner'},
    'done': {'done'},
    'failed': {'failed', 'timeout', 'cancelled'},
    'archived': None,
}


def encode_cursor(
    created: float,
    run_id: str,
    before: float | None,
    q: str = '',
    project_id: str = '',
    status: str = 'all',
    sort: str = 'newest',
) -> str:
    payload = json.dumps({
        'v': CURSOR_VERSION,
        'ch': float(created).hex(),
        'id': run_id,
        'bh': float(before).hex() if before is not None else None,
        'qh': q if q else None,
        'pi': project_id if project_id else None,
        'st': status,
        'sort': sort,
    }, separators=(',', ':')).encode('utf-8')
    return base64.urlsafe_b64encode(payload).decode('ascii')


def decode_cursor(cursor: str) -> tuple[float, str, float | None, str, str, str, str]:
    """游标绑定筛选和时间方向；旧版本保持最新在前。"""
    if not isinstance(cursor, str) or not 1 <= len(cursor) <= CURSOR_MAX_LEN:
        raise ValueError('游标格式无效')
    try:
        payload = base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True)
        obj = json.loads(payload.decode('utf-8'))
    except (ValueError, UnicodeError):
        raise ValueError('游标格式无效')
    if not isinstance(obj, dict) or type(obj.get('v')) is not int:
        raise ValueError('游标版本不支持')
    if obj['v'] == 1:
        if set(obj) != {'v', 'ch', 'id', 'bh'}:
            raise ValueError('游标版本不支持')
        try:
            if not isinstance(obj['ch'], str) or not isinstance(obj['id'], str):
                raise ValueError('游标字段类型错误')
            created = float.fromhex(obj['ch'])
            run_id = obj['id']
            bh = obj.get('bh')
            if bh is not None and not isinstance(bh, str):
                raise ValueError('游标边界类型错误')
            before = float.fromhex(bh) if bh is not None else None
        except (KeyError, ValueError, TypeError, OverflowError):
            raise ValueError('游标字段无效')
        if not isinstance(run_id, str) or not run_id:
            raise ValueError('游标 id 无效')
        if not math.isfinite(created) or (before is not None and not math.isfinite(before)):
            raise ValueError('游标时间戳无效')
        return created, run_id, before, '', '', 'all', 'newest'
    if obj['v'] in (2, CURSOR_VERSION):
        fields = {'v', 'ch', 'id', 'bh', 'qh', 'pi', 'st'}
        if obj['v'] == CURSOR_VERSION:
            fields.add('sort')
        if set(obj) != fields:
            raise ValueError('游标版本不支持')
        try:
            if not isinstance(obj['ch'], str) or not isinstance(obj['id'], str):
                raise ValueError('游标字段类型错误')
            created = float.fromhex(obj['ch'])
            run_id = obj['id']
            bh = obj.get('bh')
            if bh is not None and not isinstance(bh, str):
                raise ValueError('游标边界类型错误')
            before = float.fromhex(bh) if bh is not None else None
            qh = obj.get('qh')
            if qh is not None and (not isinstance(qh, str) or len(qh) > 600):
                raise ValueError('游标关键词类型或长度错误')
            q = qh if isinstance(qh, str) else ''
            pi = obj.get('pi')
            if pi is not None and (not isinstance(pi, str) or len(pi) > 512):
                raise ValueError('游标项目类型或长度错误')
            project_id = pi if isinstance(pi, str) else ''
            st = obj.get('st', 'all')
            if not isinstance(st, str) or st not in STATUS_GROUPS:
                raise ValueError('游标状态无效')
            sort = obj.get('sort', 'newest')
            if not isinstance(sort, str) or sort not in ('newest', 'oldest'):
                raise ValueError('游标排序无效')
        except (KeyError, ValueError, TypeError, OverflowError):
            raise ValueError('游标字段无效')
        if not isinstance(run_id, str) or not run_id:
            raise ValueError('游标 id 无效')
        if not math.isfinite(created) or (before is not None and not math.isfinite(before)):
            raise ValueError('游标时间戳无效')
        return created, run_id, before, q, project_id, st, sort
    raise ValueError('游标版本不支持')


def _summary_row(full_run: dict, task_text: str, lifecycle: str, *, archived: bool = False) -> dict:
    return {
        'id': full_run['id'],
        'run_id': full_run['run_id'],
        'project_id': full_run['project_id'],
        'created': full_run['created'],
        'task': task_text,
        'lifecycle': lifecycle,
        'managed': bool(full_run['managed']),
        'archived': archived,
    }


def _build_where(before: float | None, project_id: str,
                 scan_created: float | None, scan_id: str | None,
                 comparison: str, *, archived_only: bool = False) -> tuple[str, list]:
    parts: list[str] = []
    params: list = []
    if archived_only:
        parts.append('id IN (SELECT run_id FROM archived_runs)')
    else:
        parts.append('id NOT IN (SELECT run_id FROM archived_runs)')
    if before is not None:
        parts.append('created < ?')
        params.append(before)
    if project_id:
        parts.append('project_id = ?')
        params.append(project_id)
    if scan_created is not None and scan_id is not None:
        parts.append(f'(created {comparison} ? OR (created = ? AND id {comparison} ?))')
        params.extend([scan_created, scan_created, scan_id])
    return ('WHERE ' + ' AND '.join(parts)) if parts else '', params


def list_runs(store, summary_cache, redact, limit=100, before=None):
    """列表只读摘要；产物、会诊和长记录在进入任务房间后加载。"""
    result = []
    with store.session():
        for run in store.runs(limit, before):
            task, status = summary_cache.read(run)
            result.append(_summary_row(run, task, status))
    return redact(result)


def list_runs_page(
    store,
    summary_cache,
    redact,
    *,
    limit: int = 50,
    before: float | None = None,
    cursor: str | None = None,
    q: str | None = None,
    project_id: str | None = None,
    status: str | None = None,
    sort: str | None = None,
):
    """稳定游标分页，支持全历史筛选；返回 {runs, has_more, next_cursor}。"""
    supplied_q, supplied_project, supplied_status = q is not None, project_id is not None, status is not None
    q = '' if q is None else q
    project_id = '' if project_id is None else project_id
    status = 'all' if status is None else status
    supplied_sort = sort is not None
    sort = 'newest' if sort is None else sort
    if not isinstance(sort, str) or sort not in ('newest', 'oldest'):
        raise ValueError('sort 必须为 newest/oldest 之一')

    if not isinstance(q, str):
        raise ValueError('q 必须为字符串')
    q_norm = q.strip().casefold()
    if len(q) > 200:
        raise ValueError('q 最多 200 字符')
    if not isinstance(project_id, str):
        raise ValueError('project_id 必须为字符串')
    if len(project_id) > 512:
        raise ValueError('project_id 过长')
    if not isinstance(status, str) or status not in STATUS_GROUPS:
        raise ValueError(f'status 必须为 {"/".join(STATUS_GROUPS)} 之一')

    limit = max(1, min(100, limit))
    if before is not None and not math.isfinite(before):
        raise ValueError('分页时间边界必须为有限数')

    cur_created: float | None = None
    cur_id: str | None = None
    if cursor is not None:
        cur_created, cur_id, cursor_before, cursor_q, cursor_project_id, cursor_status, cursor_sort = decode_cursor(cursor)
        if before is not None and before != cursor_before:
            raise ValueError('游标与当前时间范围不一致，请重置分页')
        before = cursor_before
        cursor_q_norm = cursor_q.strip().casefold() if cursor_q else ''
        if ((supplied_q and q_norm != cursor_q_norm)
                or (supplied_project and project_id != cursor_project_id)
                or (supplied_status and status != cursor_status)
                or (supplied_sort and sort != cursor_sort)):
            raise ValueError('游标与当前筛选条件不一致，请重置分页')
        q_norm = cursor_q_norm
        project_id = cursor_project_id
        status = cursor_status
        sort = cursor_sort

    # 排序方向仅来自已验证枚举，禁止把请求值直接拼进 SQL。
    direction, comparison = ('ASC', '>') if sort == 'oldest' else ('DESC', '<')
    archived_only = (status == 'archived')
    status_set = STATUS_GROUPS.get(status)
    need_filter = bool(q_norm) or status_set is not None
    result: list[dict] = []

    with store.session():
        if not need_filter:
            where_sql, params = _build_where(before, project_id, cur_created, cur_id, comparison, archived_only=archived_only)
            fetch_sql = f"""
                SELECT *
                FROM runs
                {where_sql}
                ORDER BY created {direction}, id {direction}
                LIMIT ?
            """
            with store.connect() as db:
                rows = db.execute(fetch_sql, params + [limit + 1]).fetchall()
            has_more = len(rows) > limit
            for row in rows[:limit]:
                full_run = dict(row)
                task_text, lc = summary_cache.read(full_run)
                result.append(_summary_row(full_run, task_text, lc, archived=archived_only))
        else:
            batch_size = 200
            scan_created = cur_created
            scan_id = cur_id
            exhausted = False
            found_count = 0
            has_more = False
            while not exhausted and found_count <= limit:
                where_sql, params = _build_where(before, project_id, scan_created, scan_id, comparison, archived_only=archived_only)
                fetch_sql = f"""
                    SELECT *
                    FROM runs
                    {where_sql}
                    ORDER BY created {direction}, id {direction}
                    LIMIT ?
                """
                with store.connect() as db:
                    batch_rows = db.execute(fetch_sql, params + [batch_size]).fetchall()
                if len(batch_rows) < batch_size:
                    exhausted = True
                for row in batch_rows:
                    full_run = dict(row)
                    task_text, lc = summary_cache.read(full_run)
                    if status_set is not None and lc not in status_set:
                        continue
                    if q_norm:
                        if q_norm not in task_text.casefold() and q_norm not in full_run['run_id'].casefold():
                            continue
                    if found_count < limit:
                        result.append(_summary_row(full_run, task_text, lc, archived=archived_only))
                    found_count += 1
                    if found_count > limit:
                        has_more = True
                        break
                if found_count > limit:
                    break
                if batch_rows:
                    last_row = dict(batch_rows[-1])
                    scan_created = last_row['created']
                    scan_id = last_row['id']

    next_cursor: str | None = None
    if has_more and result:
        last = result[-1]
        next_cursor = encode_cursor(last['created'], last['id'], before,
                                    q_norm, project_id, status, sort)
    return redact({'runs': result, 'has_more': has_more, 'next_cursor': next_cursor})
