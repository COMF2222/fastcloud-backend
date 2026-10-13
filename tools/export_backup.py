"""Create a checked SQLite/config bundle for an authenticated SSH pull."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import backup

def bundle(database, env, directory):
    database, env, directory = Path(database), Path(env), Path(directory)
    if not env.is_file(): raise ValueError('Private server configuration is missing')
    counts = backup.inspect(database)
    directory.mkdir(parents=True,exist_ok=True)
    if os.name != 'nt': directory.chmod(0o700)
    name = 'veirune-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + secrets.token_hex(4) + '.zip'
    target = directory / name
    manifest = {'schema':1,'created':int(datetime.now(timezone.utc).timestamp()),'counts':counts,
        'databaseSha256': hashlib.sha256(database.read_bytes()).hexdigest(),
        'contains':['approvals.sqlite3','.env']}
    fd = os.open(target,os.O_WRONLY | os.O_CREAT | os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'wb') as output, zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(database,'approvals.sqlite3')
            archive.write(env,'.env')
            archive.writestr('manifest.json',json.dumps(manifest))
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    files = sorted(directory.glob('veirune-*.zip'),key=lambda p:p.stat().st_mtime,reverse=True)
    for old in files[8:]:
        if old.is_file() and not old.is_symlink(): old.unlink()
    return {'file':name,'bytes':target.stat().st_size,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'counts':counts}

def main():
    compose = ['docker','compose','-f','compose.ip.yaml']
    result = subprocess.run(compose+['exec','-T','fastcloud','python','backup.py','create','--prune'],cwd=ROOT,check=True,capture_output=True,text=True)
    report = json.loads(result.stdout)
    filename = report['file']
    if not re.fullmatch(r'fastcloud-\d{8}T\d{6}-\d{6}\.sqlite3',filename): raise ValueError('Invalid snapshot name')
    with tempfile.TemporaryDirectory(prefix='veirune-export-') as folder:
        database = Path(folder)/'approvals.sqlite3'
        subprocess.run(compose+['cp','fastcloud:/backups/'+filename,str(database)],cwd=ROOT,check=True,capture_output=True)
        value = bundle(database,ROOT/'.env',Path.home()/'.local/share/veirune/exports')
    print(json.dumps(value))

if __name__ == '__main__':
    try: main()
    except Exception: sys.exit('Backup export failed. Check container health and the private configuration; no archive was sent.')
