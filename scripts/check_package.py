"""Validate the distributable source without importing Home Assistant."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "power_fingerprint"


def main() -> None:
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    hacs = json.loads((ROOT / "hacs.json").read_text())
    assert manifest["domain"] == "power_fingerprint"
    assert all(manifest.get(key) for key in (
        "name", "documentation", "issue_tracker", "codeowners", "version"
    ))
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
    assert hacs["name"] == manifest["name"]
    assert hacs.get("render_readme") is True
    assert hacs.get("homeassistant")
    assert not hacs.get("content_in_root", False)
    assert not hacs.get("zip_release", False)
    assert [p.name for p in (ROOT / "custom_components").iterdir() if p.is_dir()] == [
        "power_fingerprint"
    ]
    constants = ast.parse((COMPONENT / "const.py").read_text())
    version = next(
        ast.literal_eval(node.value)
        for node in constants.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "VERSION"
                for target in node.targets)
    )
    assert version == manifest["version"]
    for path in COMPONENT.rglob("*.py"):
        compile(path.read_text(), str(path), "exec")
    for path in COMPONENT.rglob("*.json"):
        json.loads(path.read_text())
    for name in ("README.md", "LICENSE", "docs/MIGRATION.md",
                 f"docs/RELEASE_{version}.md"):
        assert (ROOT / name).is_file(), name
    if (ROOT / ".git").exists():
        tracked = subprocess.check_output(
            ["git", "ls-files", "-z"], cwd=ROOT
        ).decode().split("\0")
        forbidden = [name for name in tracked if name and (
            ".storage/" in name or "__pycache__/" in name
            or Path(name).name.startswith(("config_entry-", "pf-diagnostics"))
            or name.endswith((".pyc", ".zip"))
        )]
        assert not forbidden, forbidden
    print(f"Package metadata, source compilation, JSON, and layout verified: {version}")


if __name__ == "__main__":
    main()
