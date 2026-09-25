"""Build a Python distribution ZIP and verify its extracted runtime."""
import hashlib
from pathlib import Path
import py_compile
import subprocess
import sys
import tempfile
import zipfile

VERSION = '0.2.0'
FILES = [
    'antilagphotoshop.py', 'antilagphotoshop_supervisor.pyw',
    'antilagphotoshop_winapi.py', 'antilagphotoshop_stability.py',
    'antilagphotoshop_diagnostics.py', 'run_antilagphotoshop.bat',
    'collect_diagnostics.bat', 'photoshop_path.txt',
    'antilagphotoshop.ico', 'antilagphotoshop.png', 'README.md',
    'TESTING.md', 'RELEASE_NOTES.md',
]


def main():
    root = Path(__file__).resolve().parent
    dist = root / 'dist'
    dist.mkdir(exist_ok=True)
    archive = dist / f'antilagphotoshop-v{VERSION}-windows-python.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for name in FILES:
            source = root / name
            if not source.is_file():
                raise RuntimeError('Missing runtime file: ' + name)
            bundle.write(source, 'antilagphotoshop/' + name)
    with tempfile.TemporaryDirectory(prefix='antilag-package-') as folder:
        destination = Path(folder) / 'extract space ! folder'
        with zipfile.ZipFile(archive) as bundle:
            expected = {'antilagphotoshop/' + name for name in FILES}
            if set(bundle.namelist()) != expected or bundle.testzip() is not None:
                raise RuntimeError('Archive integrity check failed')
            bundle.extractall(destination)
        app = destination / 'antilagphotoshop'
        for path in app.iterdir():
            if path.suffix in ('.py', '.pyw'):
                py_compile.compile(str(path), doraise=True)
        subprocess.run([sys.executable, 'antilagphotoshop.py', '--self-test'], cwd=app, check=True, timeout=30)
        subprocess.run([sys.executable, 'antilagphotoshop_diagnostics.py', '--help'], cwd=app, check=True, timeout=10)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    (dist / (archive.name + '.sha256')).write_text(checksum + '  ' + archive.name + '\n', encoding='ascii')
    print('Verified package: ' + str(archive))


if __name__ == '__main__':
    main()
