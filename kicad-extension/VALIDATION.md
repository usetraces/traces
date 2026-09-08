# Initial PCM package validation

Validated on macOS with KiCad 10.0 GUI and KiCad 10.0.1 Python bindings.

- Archive metadata and submission metadata pass the official PCM v2 JSON schema.
- ZIP integrity passes, and rebuilding the same inputs produces the same SHA-256.
- Packaged Python files compile. The packaged schematic parser reads the demo.
- KiCad PCM's **Install from File** installs the archive and lists traces under
  Installed.
- Opening traces from **Tools → External Plugins** in the PCB Editor displays
  its component dialog on a disposable copy of the committed demo project.
- KiCad's scripting console confirms the loaded module lives under
  `10.0/3rdparty/plugins/com_github_usetraces_traces/`.
- The separate local server responds to `/health` with `status: online`.

This is a package installation and launch smoke check. Supplier searches, model
inference, write operations, and a fresh backend dependency installation have
not been re-tested for this packaging change. The GitLab fork pipeline and its
temporary PCM repository must be checked during submission.
