#!/usr/bin/env python3
"""Build a deterministic KiCad ZIP and the corresponding GitLab submission."""

import argparse
import ast
import copy
import hashlib
import json
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "kicad-extension"


def encode_json(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "pcm")
    parser.add_argument("--schema", type=Path, help="Validate with jsonschema against a downloaded KiCad schema")
    args = parser.parse_args()
    metadata = json.loads((PLUGIN / "metadata.json").read_text())
    if len(metadata["versions"]) != 1:
        raise ValueError("Archive metadata must describe exactly one release")
    version = metadata["versions"][0]["version"]
    if any(key.startswith("download_") for key in metadata["versions"][0]):
        raise ValueError("Archive metadata must not contain download_* fields")
    module = ast.parse((PLUGIN / "traces.py").read_text())
    constants = {
        target.id: ast.literal_eval(node.value)
        for node in module.body if isinstance(node, ast.Assign)
        for target in node.targets if isinstance(target, ast.Name)
        and target.id in {"PLUGIN_VERSION", "INSTALLED_VERSION"}
    }
    if constants["PLUGIN_VERSION"] != version:
        raise ValueError("Plugin and package versions must match")
    if constants["INSTALLED_VERSION"] != "0.0.0":
        raise ValueError("PCM packages must not enable the legacy PyPI updater")

    files = {
        "metadata.json": encode_json(metadata),
        "plugins/__init__.py": (PLUGIN / "__init__.py").read_bytes(),
        "plugins/traces.py": (PLUGIN / "traces.py").read_bytes(),
        "plugins/traces.png": (PLUGIN / "traces.png").read_bytes(),
        "plugins/LICENSE": (ROOT / "LICENSE").read_bytes(),
        "plugins/README.md": (PLUGIN / "README.md").read_bytes(),
    }
    for name, data in files.items():
        if name.endswith(".py"):
            compile(data, name, "exec")
    args.output.mkdir(parents=True, exist_ok=True)
    archive = args.output / f"traces-kicad-{version}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            package.writestr(info, data)

    submission = copy.deepcopy(metadata)
    release = submission["versions"][0]
    release.update(
        download_url=f"https://github.com/usetraces/traces/releases/download/kicad-v{version}/{archive.name}",
        download_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        download_size=archive.stat().st_size,
        install_size=sum(len(data) for data in files.values()),
    )
    if args.schema:
        import jsonschema
        schema = json.loads(args.schema.read_text())
        for document in (metadata, submission):
            jsonschema.validate(document, schema)
    target = args.output / "submission" / "packages" / metadata["identifier"] / "metadata.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(encode_json(submission))
    with zipfile.ZipFile(archive) as package:
        if package.testzip() is not None:
            raise ValueError("ZIP integrity check failed")
    print(f"Archive: {archive}")
    print(f"Submission: {target}")
    print(f"SHA-256: {release['download_sha256']}")
    print(f"Download: {release['download_size']} bytes; installed: {release['install_size']} bytes")


if __name__ == "__main__":
    main()
