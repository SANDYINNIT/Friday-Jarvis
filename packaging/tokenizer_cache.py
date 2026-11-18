"""Explicit installer-only downloads; runtime verification never downloads."""
import hashlib
import os
from pathlib import Path
import sys
from urllib.request import urlopen

ASSETS = {
    'cl100k_base': '223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7',
    'o200k_base': '446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d',
}
def entries():
    for name,digest in ASSETS.items():
        url='https://openaipublic.blob.core.windows.net/encodings/'+name+'.tiktoken'
        yield hashlib.sha1(url.encode()).hexdigest(),url,digest

def cache_path(): return Path(sys.prefix)/'.friday-tokenizers'

def verify(cache=None):
    cache=Path(cache) if cache else cache_path()
    for key,url,digest in entries():
        path=cache/key
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
            raise ValueError('Tokenizer cache missing or invalid; rerun sh ./install-linux.sh. No runtime download attempted.')
    return cache

def install():
    cache=cache_path();cache.mkdir(mode=0o700,exist_ok=True)
    for key,url,digest in entries():
        path=cache/key
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()==digest:continue
        with urlopen(url,timeout=30) as response:data=response.read(8*1024*1024+1)
        if len(data)>8*1024*1024 or hashlib.sha256(data).hexdigest()!=digest:
            raise ValueError('Official tokenizer download failed hash/size verification; no cache published.')
        temporary=cache/(key+'.tmp')
        try:
            with temporary.open('wb') as out:out.write(data);out.flush();os.fsync(out.fileno())
            temporary.replace(path)
        finally:temporary.unlink(missing_ok=True)
    verify(cache)

if __name__=='__main__':
    try:
        if sys.argv[1:]!=['install']:raise ValueError('Expected installer-only install command.')
        install();print('Verified two official tokenizer assets; no model weights downloaded.')
    except Exception:
        print('Tokenizer installation failed. Check outbound HTTPS to the official OpenAI public blob host, then rerun installation.',file=sys.stderr);raise SystemExit(1)
