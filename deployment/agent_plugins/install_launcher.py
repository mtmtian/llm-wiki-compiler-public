"""Generate the stable dispatcher that validates and loads current bundles."""

from pathlib import Path

TEMPLATE = '''#!/usr/bin/env python3
"""Stable entry point for the currently installed llmwiki agent adapters."""
import hashlib, json, os, runpy, stat, sys
from pathlib import Path
root = Path(__INSTALL_ROOT__)
if root.is_symlink() or not root.is_dir() or stat.S_IMODE(root.stat().st_mode) != 0o700:
    raise SystemExit("llmwiki agent bundle root is invalid")
current = root / "current"
if not current.is_symlink():
    raise SystemExit("llmwiki agent bundle pointer is invalid")
name = os.readlink(current)
if Path(name).name != name or len(name) != 64 or any(c not in "0123456789abcdef" for c in name):
    raise SystemExit("llmwiki agent bundle pointer is invalid")
package = root / name
if package.is_symlink() or not package.is_dir() or package.resolve().parent != root.resolve():
    raise SystemExit("llmwiki agent bundle location is invalid")
if stat.S_IMODE(package.stat().st_mode) != 0o700:
    raise SystemExit("llmwiki agent bundle permissions are invalid")
manifest_path = package / "manifest.json"
if manifest_path.is_symlink() or stat.S_IMODE(manifest_path.stat().st_mode) != 0o600:
    raise SystemExit("llmwiki agent bundle manifest is invalid")
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
files = manifest.get("files", {})
expected = {"bridge.py", "claude_adapter.py", "claude_capture.py", "pi-extension.ts"}
if manifest.get("version") != 1 or set(files) != expected:
    raise SystemExit("llmwiki agent bundle manifest is invalid")
if {item.name for item in package.iterdir()} != expected | {"manifest.json"}:
    raise SystemExit("llmwiki agent bundle contents are invalid")
actual = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
if actual != manifest.get("bundleHash") or name != actual:
    raise SystemExit("llmwiki agent bundle address is invalid")
for filename, digest in files.items():
    target = package / filename
    if target.is_symlink() or stat.S_IMODE(target.stat().st_mode) != 0o600:
        raise SystemExit("llmwiki agent bundle file changed")
    if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        raise SystemExit("llmwiki agent bundle file changed")
sys.dont_write_bytecode = True
sys.path.insert(0, str(package))
runpy.run_path(str(package / "bridge.py"), run_name="__main__")
'''


def launcher_source(root: Path) -> str:
    """Render a stable dispatcher with an explicit, isolated install root."""
    return TEMPLATE.replace("__INSTALL_ROOT__", repr(str(root)))
