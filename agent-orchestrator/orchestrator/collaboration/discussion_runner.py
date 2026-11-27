import os
import sys
from ..cli_telemetry import StreamRedactor
from ..safety import environment_secrets
from .storage import Store
from .discussion import execute


def _redact_text(text: str) -> str:
    return StreamRedactor(environment_secrets()).feed(text, final=True)


if __name__ == '__main__':
    store = Store(sys.argv[1])
    stream = ('--stream' in sys.argv[3:]) or os.environ.get('PIPELINE_STREAM_DISCUSSION') == '1'
    try:
        execute(store, sys.argv[2], stream_tokens=stream)
    except Exception as error:
        store.update_thread(sys.argv[2], 'failed', {'reason': _redact_text(str(error))[:1000]})
