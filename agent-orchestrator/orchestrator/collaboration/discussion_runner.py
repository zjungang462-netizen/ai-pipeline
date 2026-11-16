import sys
from .storage import Store
from .discussion import execute

if __name__ == '__main__':
    store = Store(sys.argv[1])
    try:
        execute(store, sys.argv[2])
    except Exception as error:
        store.update_thread(sys.argv[2], 'failed', {'reason': str(error)})
