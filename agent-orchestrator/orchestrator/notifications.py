"""外部通知 Webhook（飞书/钉钉/Slack/通用 JSON）。

安全约束：
- Webhook URL 与签名密钥均只通过环境变量名引用，绝不写入配置文件或日志。
- 仅发送脱敏后的运行状态摘要，不包含源码、提示词或凭据。
- 发送失败或超时绝不抛出未捕获异常阻塞主流水线。
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .cli_telemetry import StreamRedactor
from .safety import environment_secrets, open_same_origin

DEFAULT_NOTIFY_EVENTS = ('done', 'failed', 'awaiting_approval', 'waiting_owner', 'waiting_external')
SUPPORTED_FORMATS = ('generic', 'feishu', 'dingtalk', 'slack')
_LOOPBACK_HOSTS = {'127.0.0.1', 'localhost', '::1'}


@dataclass(frozen=True)
class WebhookTarget:
    name: str
    url_env: str
    format: str = 'generic'
    events: tuple[str, ...] = DEFAULT_NOTIFY_EVENTS
    secret_env: str = ''
    timeout_s: float = 5.0

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError('Webhook name 必须为非空字符串')
        if not isinstance(self.url_env, str) or not self.url_env.strip():
            raise ValueError('Webhook url_env 必须为环境变量名')
        if self.format not in SUPPORTED_FORMATS:
            raise ValueError(f'Webhook format 必须为 {", ".join(SUPPORTED_FORMATS)} 之一')
        if not self.events or not all(isinstance(ev, str) and ev.strip() for ev in self.events):
            raise ValueError('Webhook events 必须为非空事件名元组')
        if self.timeout_s <= 0 or self.timeout_s > 30:
            raise ValueError('Webhook timeout_s 必须在 (0, 30] 秒之间')


@dataclass(frozen=True)
class NotificationReceipt:
    target: str
    ok: bool
    status_code: int = 0
    error: str = ''
    skipped: bool = False


def validate_webhook_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    if parsed.username or parsed.password or not parsed.hostname:
        raise ValueError('Webhook URL 必须包含有效主机且不含用户名/密码')
    if parsed.scheme == 'https':
        return url.strip()
    if parsed.scheme == 'http' and parsed.hostname.lower() in _LOOPBACK_HOSTS:
        return url.strip()
    raise ValueError('Webhook URL 仅允许 https://（或本机 loopback http:// 用于测试）')


def format_webhook_payload(
    fmt: str,
    *,
    run_id: str,
    event: str,
    stage: str = '',
    summary: str = '',
    usage: dict | None = None,
    timestamp: float | None = None,
) -> dict:
    ts = round(time.time() if timestamp is None else float(timestamp), 3)
    safe_usage = {}
    if isinstance(usage, dict):
        for key in ('tokens', 'duration_s', 'model_calls', 'unknown_calls'):
            if key in usage and isinstance(usage[key], (int, float)) and not isinstance(usage[key], bool):
                safe_usage[key] = usage[key]
    title = f'[ai-pipeline] {run_id} · {event}'
    lines = [title]
    if stage:
        lines.append(f'阶段：{stage}')
    if summary:
        lines.append(f'摘要：{summary[:400]}')
    if safe_usage:
        parts = [f'{k}={v}' for k, v in safe_usage.items()]
        lines.append('用量：' + ', '.join(parts))
    text = '\n'.join(lines)

    if fmt == 'feishu':
        return {'msg_type': 'text', 'content': {'text': text}}
    if fmt == 'dingtalk':
        return {'msgtype': 'text', 'text': {'content': text}}
    if fmt == 'slack':
        return {'text': text}
    return {
        'schema_version': 1,
        'run_id': run_id,
        'event': event,
        'stage': stage,
        'summary': summary[:400],
        'usage': safe_usage,
        'timestamp': ts,
    }


def sign_payload(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode('utf-8'), body, hashlib.sha256).hexdigest()
    return f'sha256={digest}'


class WebhookNotifier:
    """向一个或多个已配置 Webhook 目标发送脱敏状态通知。"""

    def __init__(self, targets: tuple[WebhookTarget, ...] = (), *, secrets: tuple[str, ...] = ()):
        self.targets = tuple(targets)
        self._secrets = tuple(secrets)

    def _scrub(self, text: str, extra_secrets: tuple[str, ...] = ()) -> str:
        redactor = StreamRedactor((*self._secrets, *extra_secrets, *environment_secrets()))
        return redactor.feed(text, final=True)

    def notify(
        self,
        *,
        run_id: str,
        event: str,
        stage: str = '',
        summary: str = '',
        usage: dict | None = None,
    ) -> list[NotificationReceipt]:
        receipts: list[NotificationReceipt] = []
        clean_stage = self._scrub(stage)
        clean_summary = self._scrub(summary)
        for target in self.targets:
            if event not in target.events:
                receipts.append(NotificationReceipt(target.name, True, skipped=True))
                continue
            raw_url = os.environ.get(target.url_env, '').strip()
            if not raw_url:
                receipts.append(NotificationReceipt(target.name, False, error=f'环境变量 {target.url_env} 未设置', skipped=True))
                continue
            try:
                url = validate_webhook_url(raw_url)
            except ValueError as error:
                receipts.append(NotificationReceipt(target.name, False, error=str(error)))
                continue
            payload = format_webhook_payload(
                target.format,
                run_id=run_id,
                event=event,
                stage=clean_stage,
                summary=clean_summary,
                usage=usage,
            )
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            headers = {'Content-Type': 'application/json; charset=utf-8'}
            if target.secret_env:
                secret = os.environ.get(target.secret_env, '')
                if secret:
                    headers['X-Pipeline-Signature-256'] = sign_payload(body, secret)
            req = urllib.request.Request(url, data=body, headers=headers, method='POST')
            try:
                with open_same_origin(req, timeout=target.timeout_s) as resp:
                    code = getattr(resp, 'status', 200)
                    receipts.append(NotificationReceipt(target.name, 200 <= code < 300, status_code=code))
            except urllib.error.HTTPError as error:
                receipts.append(NotificationReceipt(target.name, False, status_code=error.code, error=f'HTTP {error.code}'))
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                receipts.append(NotificationReceipt(target.name, False, error=type(error).__name__))
        return receipts


def notify_from_env(
    *,
    run_id: str,
    results: dict[str, str],
    usage: dict | None = None,
    url_env: str = 'PIPELINE_WEBHOOK_URL',
    format_env: str = 'PIPELINE_WEBHOOK_FORMAT',
    secret_env: str = 'PIPELINE_WEBHOOK_SECRET',
) -> list[NotificationReceipt]:
    """当环境变量配置了 Webhook 时发送脱敏状态摘要；未配置时直接返回空列表。"""
    if not os.environ.get(url_env, '').strip():
        return []
    fmt = os.environ.get(format_env, 'generic').strip().lower() or 'generic'
    if fmt not in SUPPORTED_FORMATS:
        fmt = 'generic'
    values = list(results.values())
    if any(v == 'failed' for v in values):
        event = 'failed'
    elif any(v == 'awaiting_approval' for v in values):
        event = 'awaiting_approval'
    elif any(v == 'waiting_owner' for v in values):
        event = 'waiting_owner'
    elif any(v == 'waiting_external' for v in values):
        event = 'waiting_external'
    elif values and all(v == 'done' for v in values):
        event = 'done'
    else:
        return []
    focus_stage = next((name for name, status in results.items() if status == event), '')
    summary = ', '.join(f'{k}={v}' for k, v in results.items())
    target = WebhookTarget(name='env_webhook', url_env=url_env, format=fmt, secret_env=secret_env)
    return WebhookNotifier((target,)).notify(
        run_id=run_id, event=event, stage=focus_stage, summary=summary, usage=usage
    )
