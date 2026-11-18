"""Standard-library environment identity; no application imports or writes."""
import hashlib
from pathlib import Path
import sys

def key(root=None):
    if sys.version_info[:2] not in ((3,11),(3,12)):
        raise ValueError('Use Python3.11 or3.12: PYTHON=python3.12 ./install-linux.sh')
    root=Path(root) if root else Path(__file__).resolve().parents[1]
    digest=hashlib.sha256((root/'requirements-linux.lock').read_bytes()).hexdigest()[:20]
    return f'py{sys.version_info.major}{sys.version_info.minor}-{digest}'

if __name__=='__main__':
    try:
        if sys.argv[1:]!=['key']:raise ValueError('Expected key')
        print(key())
    except (ValueError,OSError) as e:
        print(str(e),file=sys.stderr);raise SystemExit(2)