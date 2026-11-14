"""Local immutable research specifications/results and ignored Markdown notes."""
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from nhl_analytics.reconstructed import file_hash

PROJECT=Path(__file__).resolve().parents[2]
ROOT=PROJECT/'data/research_registry'
DECISIONS={'rejected','inconclusive','promising','awaiting_prospective_validation'}


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),default=str,allow_nan=False)


def write_once(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8') as stream:stream.write(json.dumps(value,indent=2,default=str,allow_nan=False)+'\n')


def register(identifier,spec,root=ROOT):
    if not re.fullmatch('[a-zA-Z0-9_-]{1,100}',identifier):raise ValueError('Invalid experiment ID')
    path=Path(root)/identifier/'spec.json'
    digest=hashlib.sha256(canonical(spec).encode()).hexdigest()
    if path.exists():
        old=json.loads(path.read_text())
        if old['spec_sha256']!=digest:raise ValueError('Experiment specification is immutable')
        return old
    result=dict(experiment_id=identifier,registered_at=datetime.now(timezone.utc).isoformat(),
        spec_sha256=digest,specification=spec)
    write_once(path,result);return result


def finish(identifier,result,root=ROOT):
    if result.get('decision') not in DECISIONS:raise ValueError('Explicit research decision required')
    spec=json.loads((Path(root)/identifier/'spec.json').read_text())
    value=dict(experiment_id=identifier,spec_sha256=spec['spec_sha256'],completed_at=datetime.now(timezone.utc).isoformat(),**result)
    write_once(Path(root)/identifier/'result.json',value)
    return value


def verify_registry(identifier,root=ROOT):
    folder=Path(root)/identifier;spec=json.loads((folder/'spec.json').read_text())
    if hashlib.sha256(canonical(spec['specification']).encode()).hexdigest()!=spec['spec_sha256']:
        raise ValueError('Registry spec hash mismatch')
    for path,sha in spec['specification'].get('input_sha256',{}).items():
        if file_hash(Path(path))!=sha:raise ValueError('Registry source hash mismatch')
    if (folder/'result.json').exists():
        result=json.loads((folder/'result.json').read_text())
        if result['spec_sha256']!=spec['spec_sha256']:raise ValueError('Registry result/spec mismatch')
        for path,sha in result.get('artifact_sha256',{}).items():
            if file_hash(Path(path))!=sha:raise ValueError('Registry artifact hash mismatch')
        for path in sorted(folder.glob('correction-*.json')):
            correction=json.loads(path.read_text())
            if correction['base_result_sha256']!=file_hash(folder/'result.json') or correction['spec_sha256']!=spec['spec_sha256']:
                raise ValueError('Registry correction provenance mismatch')
            for name,sha in correction.get('supplementary_artifact_sha256',{}).items():
                if file_hash(Path(name))!=sha:raise ValueError('Registry supplementary hash mismatch')
    return spec


def effective_result(identifier,root=ROOT):
    verify_registry(identifier,root);folder=Path(root)/identifier
    result=json.loads((folder/'result.json').read_text())
    for path in sorted(folder.glob('correction-*.json')):result.update(json.loads(path.read_text())['changes'])
    return result


def amend(identifier,changes,reason,supplementary=None,root=ROOT):
    """Append a disclosed derived-report correction; never rewrite a spec/result/forecast."""
    if not reason or set(changes)-{'decision','limitations','selection_metrics','joint_assessment'}:raise ValueError('Invalid derived correction')
    if 'decision' in changes and changes['decision'] not in DECISIONS:raise ValueError('Invalid corrected decision')
    spec=verify_registry(identifier,root);folder=Path(root)/identifier
    number=len(list(folder.glob('correction-*.json')))+1
    value=dict(spec_sha256=spec['spec_sha256'],base_result_sha256=file_hash(folder/'result.json'),
        corrected_at=datetime.now(timezone.utc).isoformat(),reason=reason,changes=changes,
        supplementary_artifact_sha256=supplementary or {})
    write_once(folder/f'correction-{number:03}.json',value);return value


def note(priority,text,name='report.md'):
    folder=PROJECT/'docs/phase2'/priority;folder.mkdir(parents=True,exist_ok=True)
    path=folder/name
    path.write_text(text,encoding='utf-8');return path


def run_note(output,text,kind):
    output=Path(output)
    suffix=hashlib.sha256(str(output.resolve()).encode()).hexdigest()[:8]
    path=PROJECT/'docs/experiments'/kind/f'{output.name}-{suffix}'/'report.md'
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text,encoding='utf-8')
    return path
