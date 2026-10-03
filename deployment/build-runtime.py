#!/usr/bin/env python3
"""Build a reproducible runtime from a clean committed checkout.

Runtime directories are immutable and private to this machine. A failed build
never switches the active compiler or hooks; install.py performs that step.
"""
import hashlib
import json
import shutil
import subprocess
from pathlib import Path


def run(args, cwd):
    """Stop on any failed dependency or build step."""
    subprocess.run(args, cwd=cwd, check=True)


def revision(repo):
    """Only clean committed code receives an immutable release name."""
    dirty = subprocess.check_output(['git', 'status', '--porcelain'], cwd=repo, text=True)
    if dirty.strip():
        raise RuntimeError('Commit or isolate local changes before building a release')
    return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()


def verify_existing(release, sha):
    """Reuse only the requested commit with intact recorded files."""
    manifest = json.loads((release / 'build-manifest.json').read_text())
    if manifest['commit'] != sha or not manifest.get('files'):
        raise RuntimeError('Runtime manifest does not match the checkout')
    for name, expected in manifest['files'].items():
        if hashlib.sha256((release / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError('Existing runtime has changed: ' + name)


def build(repo, staging):
    """Compile with locked dependencies and copy only runtime artifacts."""
    run(['npm', 'ci', '--ignore-scripts'], repo)
    run(['npm', 'run', 'build'], repo)
    for name in ('package.json', 'package-lock.json', 'LICENSE', 'deployment/alma-session.py'):
        destination = staging / Path(name).name
        shutil.copy2(repo / name, destination)
    shutil.copytree(repo / 'dist', staging / 'dist')
    run(['npm', 'ci', '--omit=dev', '--ignore-scripts'], staging)
    run(['node', str(repo / 'extensions/knowledge-flow/build.mjs'),
         str(staging / 'knowledge-flow')], repo)


def main():
    """Print the path to pass to install.py after an atomic promotion."""
    repo = Path(__file__).resolve().parent.parent
    sha = revision(repo)
    base = Path.home() / '.local/share/llm-wiki-compiler/releases'
    release = base / sha[:12]
    if release.exists():
        verify_existing(release, sha)
        print(release)
        return
    base.mkdir(parents=True, exist_ok=True)
    staging = base / (sha[:12] + '.building')
    staging.mkdir()
    try:
        build(repo, staging)
        files = list((staging / 'knowledge-flow').glob('*')) + [staging / 'dist/cli.js', staging / 'alma-session.py', staging / 'package-lock.json']
        manifest = {'commit': sha, 'capabilities': ['semantic-topic-revisions-v1', 'knowledge-ledger-v1'],
                    'files': {str(p.relative_to(staging)): hashlib.sha256(p.read_bytes()).hexdigest()
                                           for p in files if p.is_file()}}
        (staging / 'build-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        staging.rename(release)
    except Exception:
        shutil.rmtree(staging)
        raise
    print(release)


if __name__ == '__main__':
    main()
