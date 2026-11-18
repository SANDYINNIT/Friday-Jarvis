"""Bounded interprocess lock for explicit local JSON writes, never passive reads."""
from contextlib import contextmanager
import os
from pathlib import Path
from threading import RLock
import time

_threads=RLock()


@contextmanager
def file_transaction(path, timeout=2.0):
    with _threads:
        target=Path(path);target.parent.mkdir(parents=True,exist_ok=True)
        with (target.parent/(target.name+'.lock')).open('a+b') as stream:
            stream.seek(0,2)
            if stream.tell()==0:stream.write(b'0');stream.flush()
            deadline=time.monotonic()+timeout
            while True:
                try:
                    stream.seek(0)
                    if os.name=='nt':
                        import msvcrt
                        msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic()>=deadline:raise OSError('Another settings write is still active') from None
                    time.sleep(.03)
            try:yield
            finally:
                stream.seek(0)
                if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
                else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
