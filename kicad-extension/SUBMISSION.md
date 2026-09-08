# KiCad PCM release and submission

Package: `com.github.usetraces.traces`

First release: `2.0.0` / Git tag `kicad-v2.0.0`

Target: KiCad 10.0, macOS, SWIG runtime, testing status

## Build

From the repository root:

```sh
python3 scripts/build_pcm.py
```

This creates:

- `dist/pcm/traces-kicad-2.0.0.zip` — plugin archive with a package entry point,
  source, icon, MIT license, setup guide, and archive metadata.
- `dist/pcm/submission/packages/com.github.usetraces.traces/metadata.json` —
  GitLab metadata with the final ZIP hash, URL, compressed size, and extracted size.

The ZIP is built from an explicit file list; it excludes `.env`, design files,
Python caches, and the server's dependencies. Builds with the same inputs and
Python/zlib toolchain produce identical bytes. Only the GitLab metadata has
`download_*` fields. `INSTALLED_VERSION` remains `0.0.0` so the old PyPI updater
does not run in PCM installations.

Optional schema validation:

```sh
curl -fL https://go.kicad.org/pcm/schemas/v2 -o /tmp/traces-pcm-v2.schema.json
uv run --no-project --with jsonschema python scripts/build_pcm.py \
  --schema /tmp/traces-pcm-v2.schema.json
```

## Verify and publish

1. Install the ZIP using PCM's **Install from File** and confirm the installed
   version. Avoid loading a duplicate manually installed copy.
2. Follow `README.md` in this directory to start the server and open the plugin
   from a saved PCB. Use a disposable project copy for write operations.
3. Commit the packaging files and documentation. Create the release tag on that
   commit; the source at the tag must match the ZIP's source.
4. Upload the ZIP to a public GitHub release named `traces for KiCad 2.0.0`, with
   tag `kicad-v2.0.0`. Mark the release as a prerelease while PCM status is testing.
5. Download the public release asset and check its SHA-256 against the generated
   submission metadata. Do not change the archive after this check.

## Submit to GitLab

1. Sign in with `glab auth login --hostname gitlab.com`.
2. Fork `https://gitlab.com/kicad/addons/metadata` and create a branch such as
   `add-traces` from its current `main`.
3. Copy the generated `packages/com.github.usetraces.traces/metadata.json` into
   the fork. Commit and push that branch.
4. Check the fork's validation pipeline. Its build job produces a temporary PCM
   repository URL; add that repository in KiCad and test installation.
5. Open a merge request targeting `kicad/addons/metadata:main` with the package
   purpose, release link, backend requirement, and exact validation performed.
   Do not claim supplier-policy approval or tests that have not happened.

Updates use additional merge requests adding release entries to the upstream
metadata. Keep prior entries in the upstream file; the builder emits only the
current version and must not replace upstream release history on later updates.

Official instructions: https://dev-docs.kicad.org/en/addons/
