"""Refresh pinned renderer assets from verified npm archives, retaining their notices."""
from __future__ import annotations
import base64
import hashlib
import io
import json
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NATIVE = ROOT / 'companions/android/app/src/main/assets/vendor'
DESKTOP = ROOT / 'apps/assistant/ultimate_assistant/ui/vendor'
PINS = {
    'marked': ('18.1.0', '39a404686c55fc2d862216e0cbb70f6bedc456adaeb5fe57b642036455f9ffc0'),
    'dompurify': ('3.4.17', '056cbdae276897208b88c21d1d779adfa09ad7be488a2f510a28e900d3bbffe7'),
    'katex': ('0.19.0', 'd8e49f2fea6eeed7cdae2cf4fd48e2f1d348758aa9e5740715e4533c2c96d1ee'),
}

def selected(package: str, name: str) -> bool:
    if name.upper() in {'LICENSE', 'LICENSE.TXT', 'LICENSE.MD', 'NOTICE'}:
        return True
    if package == 'marked':
        return name == 'lib/marked.umd.js'
    if package == 'dompurify':
        return name == 'dist/purify.min.js'
    return name in {'dist/katex.min.js', 'dist/katex.min.css'} or (name.startswith('dist/fonts/') and name.endswith('.woff2'))


def main() -> None:
    manifest = []
    for package, (version, expected) in PINS.items():
        with urllib.request.urlopen(f'https://registry.npmjs.org/{package}/{version}', timeout=30) as response:
            metadata = json.load(response)
        url = metadata['dist']['tarball']
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read()
        algorithm, integrity = metadata['dist']['integrity'].split('-', 1)
        assert base64.b64encode(hashlib.new(algorithm, data).digest()).decode() == integrity
        assert hashlib.sha256(data).hexdigest() == expected, package
        files = []
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                path = Path(member.name).relative_to('package')
                if '..' in path.parts or not selected(package, path.as_posix()):
                    continue
                body = archive.extractfile(member).read()
                targets = [NATIVE / package / path]
                if package == 'katex':
                    targets.append(DESKTOP / f'katex@{version}' / path)
                for target in targets:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(body)
                files.append({'path': path.as_posix(), 'sha256': hashlib.sha256(body).hexdigest()})
        manifest.append({'package': package, 'version': version, 'source': url,
                         'sha256': expected, 'license': metadata['license'], 'files': sorted(files, key=lambda x:x['path'])})
    (NATIVE/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    (DESKTOP/'katex@0.19.0/vendor-manifest.json').write_text(json.dumps(manifest[-1], indent=2)+'\n')
    print('Verified and refreshed Marked, patched DOMPurify, and KaTeX assets and licenses.')


if __name__ == '__main__':
    main()
