"""Serialize a store-period financial decision across API and maintenance jobs."""
from functools import wraps
from hashlib import sha256
from contextlib import contextmanager
from pathlib import Path
import threading
from .model.transaction import _file_lock,_file_unlock

_mutex=threading.Lock()
_locks={}
_local=threading.local()


@contextmanager
def guard(root,store_id,period):
    key=sha256((store_id+'\0'+period).encode()).hexdigest()
    path=Path(root).resolve()/'financial-decisions'/(key+'.lock')
    identity=str(path)
    with _mutex:lock=_locks.setdefault(identity,threading.RLock())
    with lock:
        held=getattr(_local,'held',set())
        if identity in held:
            yield
            return
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('a+b') as file:
            _file_lock(file)
            _local.held=held|{identity}
            try:yield
            finally:
                _local.held=held
                _file_unlock(file)


def locked(method):
    @wraps(method)
    def run(self,store_id,period,*args,**kwargs):
        with guard(self.root,store_id,period):return method(self,store_id,period,*args,**kwargs)
    return run
