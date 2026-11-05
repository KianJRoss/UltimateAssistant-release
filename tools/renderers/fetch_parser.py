"""Retain the MIT-licensed parser source matching the selected Mermaid release."""
from pathlib import Path
import hashlib
import io
import json
import tarfile
import urllib.request

ROOT=Path(__file__).resolve().parent
COMMIT='dcb694ddb58dc5ad3502e7e903cac05fd812eac3'
URL=f'https://codeload.github.com/mermaid-js/mermaid/tar.gz/{COMMIT}'

def main():
    data=urllib.request.urlopen(URL,timeout=60).read()
    expected='bf7f92105e0270193900b3a910d2857207e997657b46d67eb876b637ac12d68e'
    if hashlib.sha256(data).hexdigest()!=expected:raise ValueError('Upstream parser source archive digest mismatch')
    target=ROOT/'upstream/parser'
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        prefix=archive.getnames()[0].split('/')[0]
        for member in archive.getmembers():
            if not member.isfile():continue
            name=member.name.removeprefix(prefix+'/')
            if name.startswith('packages/parser/src/') or name in {'packages/parser/package.json','packages/parser/langium-config.json'}:
                relative=Path(name).relative_to('packages/parser')
            elif name=='LICENSE':relative=Path('LICENSE')
            else:continue
            assert '..' not in relative.parts
            path=target/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(archive.extractfile(member).read())
    (target/'source-info.json').write_text(json.dumps({'repository':'https://github.com/mermaid-js/mermaid','commit':COMMIT,'source_archive':URL,'sha256':hashlib.sha256(data).hexdigest(),'license':'MIT','package':'@mermaid-js/parser','version':'1.2.1'},indent=2)+'\n')
    assert json.loads((target/'package.json').read_text())['version']=='1.2.1'
    print('Retained upstream parser source and license at commit '+COMMIT)

if __name__=='__main__':main()
