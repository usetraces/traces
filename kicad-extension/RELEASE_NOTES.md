First KiCad Plugin and Content Manager package for traces, targeting KiCad 10 on macOS.

This testing release provides component sourcing, schematic metadata editing,
datasheet lookup, symbol/footprint imports, and semantic netlist checks.

Download `traces-kicad-2.0.0.zip` and install it through KiCad's Plugin and Content
Manager using **Install from File**.

**The local backend is installed separately.** It requires Python 3.11+, uv,
Node.js 20+, and either local Ollama or an OpenRouter API key. Some supplier
features require supplier API keys. See the
[installation guide](https://github.com/usetraces/traces/blob/kicad-v2.0.0/kicad-extension/README.md).

This release is prepared for submission to KiCad's official addon repository;
publication here does not mean the package has been accepted there.
