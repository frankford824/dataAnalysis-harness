"""Package committed static UI files without changing the accounting engine stamp.

The baseline is a read-only production inventory. Backend/model hashes are checked
by the deployment script; no backend, model, or VERSION file enters the payload.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import zipfile


# Render-function style literals only; numeric formatting and financial code must
# still match the engine baseline byte-for-byte after these reviewed substitutions.
UI_RENDER_STYLES = {
    'ledger/web/src/views/CommissionReports.vue': {
        'color:#a16207': 'color:var(--sk-color-warning)',
        'color:#475569;font-size:12.5px': 'color:var(--sk-color-muted);font-size:var(--sk-type-caption)',
        'color:#64748b;font-size:13px': 'color:var(--sk-color-muted);font-size:var(--sk-type-body)',
        'color:#94a3b8;font-size:12px': 'color:var(--sk-color-muted);font-size:var(--sk-type-caption)',
        "color:isPos?'#16a34a':'#d97706',fontWeight:600,fontSize:'12.5px'": "color:isPos?'var(--sk-color-success)':'var(--sk-color-warning)',fontWeight:600,fontSize:'var(--sk-type-caption)'",
    },
    'ledger/web/src/components/CommissionDetailDrawer.vue': {
        'color:#64748b': 'color:var(--sk-color-muted)',
        'color:#94a3b8': 'color:var(--sk-color-muted)',
        "color:pos?'#16a34a':'#d97706'": "color:pos?'var(--sk-color-success)':'var(--sk-color-warning)'",
    },
    'ledger/web/src/components/ProfitCompositionDrawer.vue': {
        'color:#a16207': 'color:var(--sk-color-warning)',
        'color:#15803d': 'color:var(--sk-color-success)',
    },
}


def reviewed_render_styles(path, text):
    for before, after in UI_RENDER_STYLES.get(path, {}).items():
        text = text.replace(before, after)
    return text


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', required=True)
    parser.add_argument('--base', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--baseline', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9a-f]{7,40}', args.version):
        raise SystemExit('Invalid UI release revision')
    repo = Path(args.repo).resolve()
    def git(*command):
        return subprocess.check_output(['git', '-c', 'core.filemode=false', '-C', str(repo), *command])
    git('rev-parse', '--verify', args.version + '^{commit}')
    git('diff', '--exit-code', args.version, '--', 'ledger/web', 'ledger/ledger/static')
    baseline = json.loads(Path(args.baseline).read_text(encoding='utf-8-sig'))
    publisher = git('show', args.version + ':tools/deploy/win/deploy-static-ui.ps1').decode()
    if re.search(r'\$(?:home|host|pid|input|args|error|pwd|profile|PSVersionTable|ExecutionContext)\s*=', publisher, re.I):
        raise SystemExit('UI publisher assigns a PowerShell automatic variable')
    if git('rev-parse', '--short', args.base).decode().strip() != baseline['version']:
        raise SystemExit('The reviewed UI base does not match the production engine stamp')
    allowed_tools = {'tools/deploy/package-ui-release.py', 'tools/deploy/win/deploy-static-ui.ps1'}
    changed_paths = git('diff', '--name-only', '--no-renames', args.base, args.version).decode().splitlines()
    for path in changed_paths:
        if not path.startswith(('ledger/web/', 'ledger/ledger/static/')) and path not in allowed_tools:
            raise SystemExit(f'UI-only release includes an unrelated file: {path}')
    # These views contain reviewed UI draft/cancellation and selection changes.
    # Calculation/state/API modules and every deployed backend/model file remain protected.
    ui_script_changes = {'src/App.vue', 'src/components/AppBody.vue', 'src/components/GapList.vue', 'src/components/FilterBar.vue', 'src/components/PeriodStrip.vue', 'src/components/OrgColumnView.vue', 'src/views/CommissionOrgTree.vue', 'src/views/FeesView.vue', 'src/views/FreightReview.vue', 'src/views/BoardView.vue', 'src/views/LaborView.vue', 'src/views/PeriodView.vue'}
    for path in git('ls-tree', '-r', '--name-only', args.base, '--', 'ledger/web/src').decode().splitlines():
        before = git('show', args.base + ':' + path)
        after = git('show', args.version + ':' + path)
        if path.endswith('.js') and path not in {'ledger/web/src/main.js', 'ledger/web/src/router.js', 'ledger/web/src/theme.js'} and before != after:
            raise SystemExit(f'Existing financial/state/API module changed: {path}')
        if path.endswith('.vue') and path.removeprefix('ledger/web/') not in ui_script_changes:
            scripts = lambda text: re.findall(r'<script\b[^>]*>([\s\S]*?)</script>', text.decode().replace('\r\n', '\n').replace('\r', '\n'))
            if [reviewed_render_styles(path, script) for script in scripts(before)] != scripts(after):
                raise SystemExit(f'An existing business view script changed outside the reviewed UI scope: {path}')
    prefix = 'ledger/ledger/static/'
    changed = git('diff', '--name-only', '--no-renames', '--diff-filter=AM', args.base, args.version, '--', prefix).decode().splitlines()
    changed = [path for path in changed if path != prefix + 'ui-version.json']
    if prefix + 'index.html' not in changed:
        raise SystemExit('The UI entry point must be part of the release')
    before = baseline['static_files']
    files = []
    with zipfile.ZipFile(args.output, 'x', zipfile.ZIP_DEFLATED) as archive:
        for path in changed:
            relative = PurePosixPath(path)
            if not path.startswith(prefix) or '..' in relative.parts or relative.suffix not in {'.html', '.js', '.css', '.json', '.svg', '.woff', '.woff2', '.ttf', '.png', '.webp', '.jpg'}:
                raise SystemExit(f'Not a UI asset: {path}')
            content = git('show', args.version + ':' + path)
            archive.writestr(path, content)
            files.append({'path': path, 'sha256': hashlib.sha256(content).hexdigest(), 'before_sha256': before.get(path, '')})
        metadata_path = prefix + 'ui-version.json'
        metadata = json.dumps({'ui_version': args.version, 'engine_version': baseline['version'], 'base_revision': args.base}, separators=(',', ':')).encode()
        archive.writestr(metadata_path, metadata)
        files.append({'path': metadata_path, 'sha256': hashlib.sha256(metadata).hexdigest(), 'before_sha256': before.get(metadata_path, '')})
        manifest = {'schema': 'ledger-static-ui-v1', 'version': args.version, 'engine_version': baseline['version'], 'model_revision': baseline['model_revision'], 'expected_pid': baseline['pid'], 'protected_files': baseline['protected_files'], 'files': files}
        archive.writestr('manifest.json', json.dumps(manifest, separators=(',', ':')))
    print(json.dumps({'ui_version': args.version, 'engine_version': baseline['version'], 'files': len(files), 'bytes': Path(args.output).stat().st_size}))


if __name__ == '__main__':
    main()
