"""
traces.py — KiCad 10 schematic property editor plugin
Lets you select a schematic component, inspect/edit its metadata, and fetch
JLCPCB/LCSC stock and price into schematic properties.
"""

PLUGIN_VERSION = "2.0.0"
# Stamped with the installed PyPI package version by `usetraces install`.
# Left at "0.0.0" when running an unpackaged/dev copy (update check is skipped).
INSTALLED_VERSION = "0.0.0"
PYPI_PACKAGE = "usetraces"

import re
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
import webbrowser
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from urllib.error import URLError, HTTPError
from urllib.parse import quote_plus
from urllib.request import Request, urlopen, build_opener, HTTPSHandler

import pcbnew
import wx

# KiCad's bundled Python on macOS doesn't load system certs automatically.
# Install a global opener that loads them explicitly so HTTPS works.
_ssl_ctx = ssl.create_default_context()
try:
    import certifi
    _ssl_ctx = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    if os.path.exists("/etc/ssl/cert.pem"):
        _ssl_ctx = ssl.create_default_context(cafile="/etc/ssl/cert.pem")
urllib.request.install_opener(build_opener(HTTPSHandler(context=_ssl_ctx)))


# ---------------------------------------------------------------------------
# Update check (against the latest usetraces release on PyPI)
# ---------------------------------------------------------------------------

def _version_tuple(value):
    parts = []
    for chunk in str(value).split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _is_newer(latest, current):
    a, b = _version_tuple(latest), _version_tuple(current)
    length = max(len(a), len(b))
    a += (0,) * (length - len(a))
    b += (0,) * (length - len(b))
    return a > b


def fetch_latest_pypi_version(timeout=6):
    try:
        req = Request(
            f"https://pypi.org/pypi/{PYPI_PACKAGE}/json",
            headers={"User-Agent": f"traces-plugin/{PLUGIN_VERSION}"},
        )
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["info"]["version"]
    except Exception:
        return None


def _update_env():
    """Build a clean environment for spawning the external updater.

    KiCad's bundled Python exports PYTHONHOME/PYTHONPATH pointing at its own
    framework. If those leak into a subprocess, the tool's interpreter boots
    against KiCad's stdlib and dies with "No module named 'encodings'". Strip
    them, and augment PATH since GUI apps on macOS get a minimal one.
    """
    env = dict(os.environ)
    for var in (
        "PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "PYTHONSTARTUP",
        "PYTHONNOUSERSITE", "__PYVENV_LAUNCHER__", "PYTHONDONTWRITEBYTECODE",
    ):
        env.pop(var, None)
    parts = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
    for extra in (
        str(Path.home() / ".local" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        str(Path.home() / ".cargo" / "bin"),
    ):
        if extra not in parts:
            parts.append(extra)
    env["PATH"] = os.pathsep.join(parts)
    return env


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

BACKEND_URL = "http://127.0.0.1:8000"


def _server_alive(timeout=1.5):
    """True if the local traces server answers /health."""
    try:
        with urlopen(Request(f"{BACKEND_URL}/health"), timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def _find_mcp_dir():
    """Locate the traces `mcp/` dir (which holds pyproject + traces-serve).

    The installer writes its absolute path into a `traces_server_path` sidecar
    next to this plugin; fall back to a couple of common clone locations."""
    sidecar = Path(__file__).with_name("traces_server_path")
    try:
        p = Path(sidecar.read_text().strip())
        if (p / "pyproject.toml").exists():
            return p
    except Exception:
        pass
    for guess in (
        Path.home() / "Documents" / "git" / "traces" / "mcp",
        Path.home() / "traces" / "mcp",
        Path.home() / "src" / "traces" / "mcp",
    ):
        if (guess / "pyproject.toml").exists():
            return guess
    return None


def _ensure_server(wait=20):
    """Start the local traces server if it isn't already running.

    Spawns `uv run traces-serve` (detached) in the mcp dir using a clean env,
    then polls /health for up to `wait` seconds. Returns True if the server is
    reachable by the end, False otherwise (the caller can warn but still open).
    """
    if _server_alive():
        return True
    mcp_dir = _find_mcp_dir()
    if mcp_dir is None:
        return False
    env = _update_env()
    uv = shutil.which("uv", path=env["PATH"])
    if not uv:
        return False
    try:
        subprocess.Popen(
            [uv, "run", "traces-serve"],
            cwd=str(mcp_dir),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # survive KiCad / detach from the plugin
        )
    except Exception:
        return False
    deadline = time.time() + wait
    while time.time() < deadline:
        if _server_alive():
            return True
        time.sleep(0.5)
    return False


class AuthManager:
    """Local no-op stand-in for the old hosted auth.

    The open-source build talks only to a server running on your own machine,
    so there is no sign-in, token, account, or billing. This keeps the existing
    call sites working without any network or credentials.
    """

    def __init__(self):
        self.token = ""
        self.email = "local"
        self.plan = "local"
        self.welcome_seen = True

    def is_authenticated(self) -> bool:
        return True

    def auth_headers(self) -> dict:
        return {}

    def refresh_if_needed(self) -> bool:
        return True

    def save(self, *args, **kwargs) -> None:
        pass

    def clear(self) -> None:
        pass

    def mark_welcome_seen(self) -> None:
        pass

    def display_name(self) -> str:
        return "Local"

    def plan_label(self) -> str:
        return "Local"


# ---------------------------------------------------------------------------
# Inline schematic parser (kicad_sch.py logic embedded so the plugin is
# self-contained — or you can import from kicad_sch if it lives alongside)
# ---------------------------------------------------------------------------

def _tokenize(text):
    token_re = re.compile(
        r'\(|\)'
        r'|"(?:[^"\\]|\\.)*"'
        r'|[^\s()"]+'
    )
    return token_re.findall(text)


class _Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos]

    def consume(self):
        t = self.tokens[self.pos]
        self.pos += 1
        return t

    def parse_node(self):
        if self.peek() == '(':
            return self.parse_list()
        return self.consume()

    def parse_list(self):
        self.consume()  # '('
        items = []
        while self.peek() != ')':
            items.append(self.parse_node())
        self.consume()  # ')'
        return items

    def parse_root(self):
        return self.parse_list()


def _parse(text):
    return _Parser(_tokenize(text)).parse_root()


def _is_flat(node):
    return not any(isinstance(c, list) for c in node)


def _serialize(node, indent=0):
    if isinstance(node, str):
        return node
    tab = '\t' * indent
    if _is_flat(node):
        return '(' + ' '.join(node) + ')'
    head = node[0]
    rest = node[1:]
    inline_scalars = []
    child_nodes = []
    hit_list = False
    for item in rest:
        if not hit_list and isinstance(item, str):
            inline_scalars.append(item)
        else:
            hit_list = True
            child_nodes.append(item)
    first_line = '(' + ' '.join([head] + inline_scalars)
    if not child_nodes:
        return first_line + ')'
    lines = [first_line]
    for child in child_nodes:
        if isinstance(child, list):
            lines.append('\t' * (indent + 1) + _serialize(child, indent + 1))
        else:
            lines.append('\t' * (indent + 1) + child)
    lines.append(tab + ')')
    return '\n'.join(lines)


def _make_property_node(key, value, at=(0, 0, 0), hide=True):
    at_node = ['at', str(at[0]), str(at[1]), str(at[2])]
    show_name_node = ['show_name', 'no']
    dnap_node = ['do_not_autoplace', 'no']
    font_node = ['font', ['size', '1.27', '1.27']]
    effects_node = ['effects', font_node]
    node = ['property', f'"{key}"', f'"{value}"', at_node, show_name_node, dnap_node]
    if hide:
        node.append(['hide', 'yes'])
    node.append(effects_node)
    return node


class KiCadSchematic:
    def __init__(self, path):
        self.path = path
        with open(path, 'r', encoding='utf-8') as f:
            self.raw = f.read()
        self.tree = _parse(self.raw)
        self._index = self._build_index()

    def _build_index(self):
        index = {}
        for node in self.tree:
            if not isinstance(node, list) or node[0] != 'symbol':
                continue
            ref = self._get_prop_value(node, 'Reference')
            if ref:
                index[ref] = node
        return index

    def _rebuild_index(self):
        self._index = self._build_index()

    @staticmethod
    def _get_prop_node(symbol_node, key):
        for child in symbol_node:
            if (isinstance(child, list)
                    and child[0] == 'property'
                    and child[1] == f'"{key}"'):
                return child
        return None

    @staticmethod
    def _get_prop_value(symbol_node, key):
        node = KiCadSchematic._get_prop_node(symbol_node, key)
        return node[2].strip('"') if node else None

    def _require_ref(self, reference):
        if reference not in self._index:
            raise KeyError(f"Reference '{reference}' not found")
        return self._index[reference]

    def get_references(self):
        return sorted(self._index.keys())

    def get_properties(self, reference):
        sym = self._require_ref(reference)
        props = {}
        for child in sym:
            if isinstance(child, list) and child[0] == 'property':
                k = child[1].strip('"')
                v = child[2].strip('"')
                props[k] = v
        return props

    def get_property(self, reference, key):
        return self._get_prop_value(self._require_ref(reference), key)

    def add_property(self, reference, key, value, hide=True, at=(0, 0, 0)):
        sym = self._require_ref(reference)
        if self._get_prop_node(sym, key) is not None:
            raise ValueError(f"Property '{key}' already exists on '{reference}'")
        new_node = _make_property_node(key, value, at=at, hide=hide)
        insert_at = len(sym)
        for i, child in enumerate(sym):
            if isinstance(child, list) and child[0] == 'pin':
                insert_at = i
                break
        sym.insert(insert_at, new_node)

    def update_property(self, reference, key, value):
        sym = self._require_ref(reference)
        node = self._get_prop_node(sym, key)
        if node is None:
            raise KeyError(f"Property '{key}' not found on '{reference}'")
        node[2] = f'"{value}"'
        if key == 'Reference':
            self._rebuild_index()

    def set_property(self, reference, key, value, hide=True, at=(0, 0, 0)):
        sym = self._require_ref(reference)
        node = self._get_prop_node(sym, key)
        if node is not None:
            node[2] = f'"{value}"'
            if key == 'Reference':
                self._rebuild_index()
        else:
            self.add_property(reference, key, value, hide=hide, at=at)

    def delete_property(self, reference, key):
        if key in ('Reference', 'Value'):
            raise ValueError(f"Cannot delete required property '{key}'")
        sym = self._require_ref(reference)
        node = self._get_prop_node(sym, key)
        if node is None:
            raise KeyError(f"Property '{key}' not found on '{reference}'")
        sym.remove(node)

    def write(self, path=None):
        out = path or self.path
        with open(out, 'w', encoding='utf-8') as f:
            f.write(_serialize(self.tree, 0) + '\n')

    def set_lib_id(self, reference, lib_id_str):
        sym = self._require_ref(reference)
        for child in sym:
            if isinstance(child, list) and child[0] == 'lib_id':
                child[1] = f'"{lib_id_str}"'
                return
        sym.insert(1, ['lib_id', f'"{lib_id_str}"'])

    def embed_lib_symbol(self, lib_name, symbol_name, kicad_sym_content):
        """Embed a symbol definition into the schematic's lib_symbols cache.

        KiCad stores an inline copy of every used symbol in lib_symbols so the
        schematic is self-contained and renders without the external library.
        Without this entry the placed symbol shows as a broken '?' placeholder.
        """
        full_name = f"{lib_name}:{symbol_name}"

        # Parse fresh so we get an unshared copy we can mutate
        sym_lib = _parse(kicad_sym_content)
        sym_node = None
        for node in sym_lib:
            if (isinstance(node, list) and node[0] == 'symbol'
                    and len(node) > 1 and isinstance(node[1], str)
                    and node[1].strip('"') == symbol_name):
                sym_node = node
                break
        if sym_node is None:
            return

        # Rename top-level key from "SymbolName" → "lib:SymbolName"
        sym_node[1] = f'"{full_name}"'

        # Find or create the lib_symbols section
        for node in self.tree:
            if isinstance(node, list) and node[0] == 'lib_symbols':
                # Remove stale entry with the same full name
                node[:] = [n for n in node
                           if not (isinstance(n, list) and n[0] == 'symbol'
                                   and len(n) > 1 and isinstance(n[1], str)
                                   and n[1].strip('"') == full_name)]
                node.append(sym_node)
                return

        # No lib_symbols section yet — insert after uuid/paper nodes
        insert_at = 1
        for i, node in enumerate(self.tree):
            if isinstance(node, list) and node[0] in ('uuid', 'paper', 'generator'):
                insert_at = i + 1
        self.tree.insert(insert_at, ['lib_symbols', sym_node])

    def _get_schematic_uuid(self):
        for node in self.tree:
            if isinstance(node, list) and node[0] == 'uuid':
                return node[1].strip('"')
        return ""

    def _get_project_name(self):
        for node in self.tree:
            if isinstance(node, list) and node[0] == 'symbol':
                for child in node:
                    if isinstance(child, list) and child[0] == 'instances':
                        for sub in child:
                            if isinstance(sub, list) and sub[0] == 'project':
                                return sub[1].strip('"')
        return Path(self.path).stem

    def get_center(self):
        xs, ys = [], []
        for node in self.tree:
            if not isinstance(node, list) or node[0] != 'symbol':
                continue
            lib_id = ""
            for child in node:
                if isinstance(child, list) and child[0] == 'lib_id':
                    lib_id = child[1].strip('"')
                    break
            if lib_id.startswith('power:'):
                continue
            for child in node:
                if isinstance(child, list) and child[0] == 'at':
                    try:
                        xs.append(float(child[1]))
                        ys.append(float(child[2]))
                    except (IndexError, ValueError):
                        pass
        if not xs:
            return (150.0, 100.0)
        return (sum(xs) / len(xs), sum(ys) / len(ys))

    def place_symbol(self, lib_id_str, x, y, value, footprint, reference="U?",
                     datasheet="", extra_props=None):
        new_uuid = str(uuid.uuid4())
        sch_uuid = self._get_schematic_uuid()
        project_name = self._get_project_name()
        sx, sy = str(round(x, 2)), str(round(y, 2))

        ref_prop = _make_property_node('Reference', reference, at=(x, y - 2.54, 0), hide=False)
        val_prop = _make_property_node('Value', value, at=(x, y + 2.54, 0), hide=False)
        fp_prop  = _make_property_node('Footprint', footprint, at=(x, y, 0), hide=True)
        ds_prop  = _make_property_node('Datasheet', datasheet, at=(x, y, 0), hide=True)

        path_node    = ['path', f'"/{sch_uuid}"', ['reference', f'"{reference}"'], ['unit', '1']]
        project_node = ['project', f'"{project_name}"', path_node]
        instances    = ['instances', project_node]

        sym_node = [
            'symbol',
            ['lib_id', f'"{lib_id_str}"'],
            ['at', sx, sy, '0'],
            ['unit', '1'],
            ['in_bom', 'yes'],
            ['on_board', 'yes'],
            ['dnp', 'no'],
            ['uuid', new_uuid],
            ref_prop,
            val_prop,
            fp_prop,
            ds_prop,
        ]
        for key, val in (extra_props or {}).items():
            sym_node.append(_make_property_node(key, val, at=(x, y, 0), hide=True))
        sym_node.append(instances)
        self.tree.append(sym_node)
        self._rebuild_index()

    def next_reference(self, prefix):
        """Return the next free reference designator for a prefix (e.g. 'R' -> 'R5')."""
        prefix = prefix or "U"
        pat = re.compile(rf'^{re.escape(prefix)}(\d+)$')
        max_n = 0
        for ref in self._index.keys():
            m = pat.match(ref)
            if m:
                max_n = max(max_n, int(m.group(1)))
        return f"{prefix}{max_n + 1}"

    def symbol_ref_prefix(self, kicad_sym_content, symbol_name):
        """Extract the default reference prefix (R, C, D, L, U, ...) from a symbol def."""
        try:
            sym_lib = _parse(kicad_sym_content)
        except Exception:
            return "U"
        for node in sym_lib:
            if (isinstance(node, list) and node[0] == 'symbol'
                    and len(node) > 1 and isinstance(node[1], str)
                    and node[1].strip('"') == symbol_name):
                for child in node:
                    if (isinstance(child, list) and child[0] == 'property'
                            and len(child) > 2 and child[1].strip('"') == 'Reference'):
                        val = child[2].strip('"')
                        m = re.match(r'[A-Za-z]+', val)
                        if m:
                            return m.group(0)
        return "U"


# ---------------------------------------------------------------------------
# Property CRUD dialog
# ---------------------------------------------------------------------------

PROTECTED = {'Reference', 'Value'}
BACKEND_URL = "http://127.0.0.1:8000"
JLCPCB_SOURCE_URL = f"{BACKEND_URL}/jobs/jlcpcb/source"
JLCPCB_SEARCH_URL = f"{BACKEND_URL}/jobs/jlcpcb/search"
DIGIKEY_SOURCE_URL = f"{BACKEND_URL}/jobs/digikey/source"
DIGIKEY_SEARCH_URL = f"{BACKEND_URL}/jobs/digikey/search"
DIGIKEY_DATASHEET_URL = f"{BACKEND_URL}/jobs/digikey/datasheet"
MOUSER_DATASHEET_URL = f"{BACKEND_URL}/jobs/mouser/datasheet"
MOUSER_SOURCE_URL = f"{BACKEND_URL}/jobs/mouser/source"
MOUSER_SEARCH_URL = f"{BACKEND_URL}/jobs/mouser/search"
DATASHEET_FETCH_URL = f"{BACKEND_URL}/jobs/datasheet/fetch"
NETLIST_TYPO_URL = f"{BACKEND_URL}/jobs/netlist/typo"
NETLIST_CONSISTENCY_URL = f"{BACKEND_URL}/jobs/netlist/consistency"
NETLIST_ORPHAN_URL = f"{BACKEND_URL}/jobs/netlist/orphan"
NETLIST_NEARDUPLICATE_URL = f"{BACKEND_URL}/jobs/netlist/nearduplicate"
SRC_CHECKS = {
    "Typos": NETLIST_TYPO_URL,
    "Consistency": NETLIST_CONSISTENCY_URL,
    "Orphans": NETLIST_ORPHAN_URL,
    "Near dups": NETLIST_NEARDUPLICATE_URL,
}
LIBRARY_SYMBOL_URL = f"{BACKEND_URL}/library/symbol"
JLCPCB_FIELD_NAMES = ("LCSC", "LCSC Part", "JLCPCB Part", "Supplier Part")
DIGIKEY_FIELD_NAMES = ("Digikey Part Number", "Digi-Key Part Number", "DigiKey Part Number")
MOUSER_FIELD_NAMES = ("Mouser Part Number",)
NA_SUPPLIERS = {"N/A", "NA", "NONE"}
QUANTITY_FIELD = "Quantity Available"
PRICE_FIELD = "Price"
TIMESTAMP_FIELD = "Timestamp"
DATASHEET_URL_FIELD = "Datasheet"
DATASHEET_LOCAL_FIELD = "Datasheet Local"
DATASHEET_FIELD_NAMES = ("Datasheet", "Datasheet URL")


def _first_property(props, names):
    lowered = {key.lower(): value for key, value in props.items()}
    for name in names:
        value = lowered.get(name.lower(), "")
        if value:
            return value
    return ""


class CandidateSearchDialog(wx.Dialog):
    """Stage supplier search candidates before applying them to the schematic."""

    def __init__(self, owner, mode, row=None):
        title = "Add part" if mode == "add" else "Fill part data"
        super().__init__(
            owner,
            title=title,
            size=(760, 620),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.owner = owner
        self.mode = mode
        self.row = row
        self._build_ui()
        self._load_defaults()
        self.CentreOnParent()
        if self.mode == "fill" and (self.desc.GetValue().strip() or self.footprint.GetValue().strip()):
            wx.CallAfter(self._on_search, None)

    def _build_ui(self):
        root = wx.BoxSizer(wx.VERTICAL)

        form = wx.BoxSizer(wx.VERTICAL)
        sup_row = wx.BoxSizer(wx.HORIZONTAL)
        sup_row.Add(wx.StaticText(self, label="Supplier:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.supplier = wx.Choice(self, choices=["LCSC / JLCPCB", "DigiKey", "Mouser"])
        self.supplier.SetSelection(0)
        sup_row.Add(self.supplier, 1)
        form.Add(sup_row, 0, wx.EXPAND | wx.BOTTOM, 8)

        form.Add(wx.StaticText(self, label="Describe what you need:"), 0, wx.BOTTOM, 4)
        self.desc = wx.TextCtrl(self, size=(-1, 70), style=wx.TE_MULTILINE)
        self.desc.SetHint("e.g. 10k resistor, 0603, 1% or N-channel MOSFET Vds>100V")
        form.Add(self.desc, 0, wx.EXPAND | wx.BOTTOM, 8)

        grid = wx.FlexGridSizer(4, 2, 6, 8)
        grid.AddGrowableCol(1)
        grid.Add(wx.StaticText(self, label="Package:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.footprint = wx.TextCtrl(self)
        self.footprint.SetHint("e.g. 0603, SOT-23-5")
        grid.Add(self.footprint, 1, wx.EXPAND)
        grid.Add(wx.StaticText(self, label="Max $/unit:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.max_price = wx.TextCtrl(self)
        self.max_price.SetHint("optional")
        grid.Add(self.max_price, 1, wx.EXPAND)
        grid.Add(wx.StaticText(self, label="Min stock:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.min_qty = wx.TextCtrl(self)
        self.min_qty.SetHint("optional")
        grid.Add(self.min_qty, 1, wx.EXPAND)
        grid.Add(wx.StaticText(self, label="Results:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.count = wx.SpinCtrl(self, value="3", min=1, max=5, initial=3)
        grid.Add(self.count, 1, wx.EXPAND)
        form.Add(grid, 0, wx.EXPAND)

        root.Add(form, 0, wx.EXPAND | wx.ALL, 12)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        self.search_btn = wx.Button(self, label="Search")
        self.search_btn.Bind(wx.EVT_BUTTON, self._on_search)
        btn_row.Add(self.search_btn, 0, wx.RIGHT, 8)
        close_btn = wx.Button(self, wx.ID_CANCEL, label="Close")
        btn_row.Add(close_btn, 0)
        self.status = wx.StaticText(self, label="")
        btn_row.Add(self.status, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 12)
        root.Add(btn_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        root.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)
        self.results_scroll = wx.ScrolledWindow(self, style=wx.VSCROLL | wx.BORDER_SIMPLE)
        self.results_scroll.SetScrollRate(0, 12)
        self.results_sizer = wx.BoxSizer(wx.VERTICAL)
        self.results_scroll.SetSizer(self.results_sizer)
        root.Add(self.results_scroll, 1, wx.EXPAND | wx.ALL, 12)
        self.SetSizer(root)

    def _load_defaults(self):
        if self.mode != "fill" or not self.row:
            return
        supplier = (self.row.get("supplier") or "").strip().upper()
        if supplier == "LCSC":
            self.supplier.SetStringSelection("LCSC / JLCPCB")
        elif supplier in {"DIGIKEY", "DIGI-KEY"}:
            self.supplier.SetStringSelection("DigiKey")
        elif supplier == "MOUSER":
            self.supplier.SetStringSelection("Mouser")
        value = "" if self.row.get("value") == "Mixed" else self.row.get("value", "")
        footprint = "" if self.row.get("footprint") == "Mixed" else self.row.get("footprint", "")
        self.desc.SetValue(value)
        self.footprint.SetValue(footprint)

    def _on_search(self, _event):
        supplier = self.supplier.GetStringSelection().strip()
        desc = self.desc.GetValue().strip()
        footprint = self.footprint.GetValue().strip()
        if not desc and not footprint:
            self.status.SetLabel("Enter a description or package.")
            return

        self.search_btn.Disable()
        self.results_sizer.Clear(delete_windows=True)
        self.status.SetLabel(f"Searching for {self.count.GetValue()} result(s)...")
        wx.YieldIfNeeded()
        try:
            job_id = self.owner._submit_search_job(
                supplier.upper(),
                desc,
                footprint,
                self.max_price.GetValue().strip(),
                self.min_qty.GetValue().strip(),
                self.count.GetValue(),
            )
            result = self.owner._poll_job(
                job_id,
                f"{supplier} search",
                status_setter=self.status.SetLabel,
            )
        except HTTPError as exc:
            if self.owner._handle_http_error(exc):
                self.status.SetLabel("")
            else:
                self.status.SetLabel(f"Search failed: {exc}")
            self.search_btn.Enable()
            return
        except (URLError, TimeoutError, RuntimeError, KeyError, json.JSONDecodeError) as exc:
            self.status.SetLabel(f"Search failed: {exc}")
            self.search_btn.Enable()
            return

        self.search_btn.Enable()
        candidates = [c for c in (result.get("candidates", []) if result else []) if c.get("part_number")]
        if not candidates:
            self.status.SetLabel("No matches found.")
            empty = wx.StaticText(self.results_scroll, label="No results.")
            empty.SetForegroundColour(wx.Colour(150, 150, 150))
            self.results_sizer.Add(empty, 0, wx.ALL, 12)
        else:
            self.status.SetLabel(f"{len(candidates)} result(s) found.{self.owner._model_suffix(result)}")
            for candidate in candidates:
                candidate["_traces_supplier"] = self.owner._normalize_supplier_label(supplier)
                self._add_candidate_card(candidate)
        self.results_scroll.FitInside()
        self.results_scroll.Layout()

    def _add_candidate_card(self, candidate):
        supplier = candidate.get("_traces_supplier", "")
        card = wx.Panel(self.results_scroll)
        card.SetBackgroundColour(wx.Colour(42, 42, 48))
        sizer = wx.BoxSizer(wx.VERTICAL)

        part = candidate.get("part_number", "")
        name = candidate.get("name") or candidate.get("description", "") or part
        title = wx.StaticText(card, label=name)
        title_font = title.GetFont()
        title_font.SetWeight(wx.FONTWEIGHT_BOLD)
        title.SetFont(title_font)
        title.Wrap(640)
        sizer.Add(title, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)

        mfr = candidate.get("manufacturer", "")
        sub = f"{supplier}  {part}"
        if mfr:
            sub += f"  -  {mfr}"
        sub_lbl = wx.StaticText(card, label=sub)
        sub_lbl.SetForegroundColour(wx.Colour(150, 170, 210))
        sizer.Add(sub_lbl, 0, wx.LEFT | wx.RIGHT | wx.TOP, 3)

        detail_parts = []
        price = candidate.get("price")
        qty = candidate.get("qty")
        footprint = candidate.get("footprint", "")
        if price is not None:
            detail_parts.append(f"${price:.4f}" if price < 0.01 else f"${price:.2f}")
        if qty is not None:
            detail_parts.append(f"Stock: {qty:,}")
        if footprint:
            detail_parts.append(footprint)
        if detail_parts:
            detail = wx.StaticText(card, label="   ".join(detail_parts))
            detail.SetForegroundColour(wx.Colour(180, 180, 180))
            sizer.Add(detail, 0, wx.LEFT | wx.RIGHT | wx.TOP, 4)

        specs = candidate.get("specs", []) or []
        if specs:
            specs_lbl = wx.StaticText(card, label="  -  ".join(str(s) for s in specs[:6]))
            specs_lbl.SetForegroundColour(wx.Colour(140, 190, 150))
            specs_lbl.Wrap(640)
            sizer.Add(specs_lbl, 0, wx.LEFT | wx.RIGHT | wx.TOP, 4)

        just = candidate.get("justification", "")
        if just:
            just_lbl = wx.StaticText(card, label=just)
            just_lbl.SetForegroundColour(wx.Colour(130, 130, 130))
            just_lbl.Wrap(640)
            sizer.Add(just_lbl, 0, wx.LEFT | wx.RIGHT | wx.TOP, 4)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        apply_label = "Apply to selected" if self.mode == "fill" else "Apply: add part"
        apply_btn = wx.Button(card, label=apply_label)
        apply_btn.Bind(wx.EVT_BUTTON, lambda e, c=candidate: self._on_apply(c))
        btn_row.Add(apply_btn, 0, wx.RIGHT, 6)
        ds_btn = wx.Button(card, label="Datasheet")
        ds_btn.Bind(wx.EVT_BUTTON, lambda e, c=candidate: self.owner._open_candidate_datasheet(c))
        btn_row.Add(ds_btn, 0, wx.RIGHT, 6)
        view_btn = wx.Button(card, label=f"View on {supplier}")
        view_btn.Bind(wx.EVT_BUTTON, lambda e, c=candidate: self.owner._open_product_page(c))
        btn_row.Add(view_btn, 0)
        sizer.Add(btn_row, 0, wx.ALL, 8)

        card.SetSizer(sizer)
        self.results_sizer.Add(card, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

    def _on_apply(self, candidate):
        if self.mode == "fill":
            if not self.row:
                self.status.SetLabel("Select a component first.")
                return
            if self.owner._apply_candidate_to_refs(candidate, self.row["refs"], candidate.get("_traces_supplier", "")):
                selected = self.row["refs"][0]
                self.owner._populate_components(selected)
                refreshed = self.owner._row_for_ref(selected)
                if refreshed:
                    self.owner._load_detail_for_row(refreshed)
                self.owner._set_status(f"Applied {candidate.get('part_number', '')} to {len(self.row['refs'])} part(s).")
                self.EndModal(wx.ID_OK)
            return

        self.search_btn.Disable()
        self.status.SetLabel(f"Adding {candidate.get('part_number', '')}...")
        thread = threading.Thread(
            target=self.owner._place_candidate_as_new_part,
            args=(candidate, candidate.get("_traces_supplier", ""), self._set_status),
            daemon=True,
        )
        thread.start()

    def _set_status(self, message):
        self.owner._set_status(message)
        if not self.IsBeingDeleted():
            self.status.SetLabel(message)


class PropEditorDialog(wx.Dialog):
    """Main dialog: schematic list plus staged add/fill workflows."""

    def __init__(self, sch: KiCadSchematic, sch_path: str, board=None, auth: AuthManager = None):
        super().__init__(
            None,
            title="traces",
            size=(1280, 720),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.sch = sch
        self.sch_path = sch_path
        self.board = board
        self.auth = auth
        self.dirty = False
        self.component_rows = []
        self.sort_column = 0
        self.sort_reverse = False
        self.search_query = ""
        self.filter_mode = "Issues only"
        self.grouped_view = True
        self.row_call_status = {}
        self.src_logs = []
        self._detail_field_ctrls: dict = {}
        self._detail_refs: list = []
        self._search_job_id = ""
        self._search_poll_gen = 0
        self._search_result = None

        self._build_ui()
        self._populate_components()
        self.SetMinSize((1280, 720))
        self.SetSize((1280, 720))
        self.Layout()
        self.CentreOnScreen()
        self._latest_version = None
        threading.Thread(target=self._check_for_update, daemon=True).start()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        self._comp_page = wx.Panel(self)
        self._build_components_tab(self._comp_page)

        bottom = wx.BoxSizer(wx.HORIZONTAL)
        self.status_text = wx.StaticText(self, label="")
        bottom.Add(self.status_text, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 16)
        self.btn_update = wx.Button(self, label="Update")
        self.btn_update.Bind(wx.EVT_BUTTON, self._on_update_clicked)
        self.btn_update.Hide()
        bottom.Add(self.btn_update, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        btn_close = wx.Button(self, wx.ID_CANCEL, "Close")
        bottom.Add(btn_close, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 14)

        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(self._comp_page, 1, wx.EXPAND)
        root.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        root.Add(bottom, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 14)
        self.SetSizer(root)

    def _build_components_tab(self, parent):
        # Top bar
        top = wx.BoxSizer(wx.HORIZONTAL)
        self.search_field = wx.TextCtrl(parent, size=(200, -1), style=wx.TE_PROCESS_ENTER)
        self.search_field.SetHint("Search components...")
        self.search_field.Bind(wx.EVT_TEXT, self._on_search_changed)
        top.Add(self.search_field, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)

        self.filter_choice = wx.Choice(parent, choices=[
            "All components", "Issues only", "Needs footprint",
            "Missing supplier", "Needs sourcing", "Ready",
        ])
        self.filter_choice.SetStringSelection(self.filter_mode)
        self.filter_choice.Bind(wx.EVT_CHOICE, self._on_filter_changed)
        top.Add(self.filter_choice, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)

        self._view_mode_choice = wx.Choice(parent, choices=["Grouped", "Flat"])
        self._view_mode_choice.SetSelection(0)
        self._view_mode_choice.Bind(wx.EVT_CHOICE, self._on_view_mode_changed)
        top.Add(self._view_mode_choice, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)

        self.btn_add_part = wx.Button(parent, label="Add part")
        self.btn_add_part.Bind(wx.EVT_BUTTON, self._on_add_part)
        top.Add(self.btn_add_part, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)

        top.AddStretchSpacer()
        self.issue_status = wx.StaticText(parent, label="")
        f = self.issue_status.GetFont()
        f.SetWeight(wx.FONTWEIGHT_BOLD)
        self.issue_status.SetFont(f)
        top.Add(self.issue_status, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 14)
        self.footprint_status = wx.StaticText(parent, label="")
        self.supplier_status = wx.StaticText(parent, label="")
        self.part_status = wx.StaticText(parent, label="")
        for lbl in (self.footprint_status, self.supplier_status, self.part_status):
            top.Add(lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 14)

        # Splitter: left = list, right = detail
        splitter = wx.SplitterWindow(parent, style=wx.SP_LIVE_UPDATE | wx.SP_3DSASH)
        self._components_splitter = splitter
        splitter.Bind(wx.EVT_SIZE, self._on_components_splitter_size)

        left_panel = wx.Panel(splitter)
        self.component_grid = wx.ListCtrl(left_panel, style=wx.LC_REPORT | wx.BORDER_SUNKEN)
        self.component_grid.InsertColumn(0, "Status", width=80)
        self.component_grid.InsertColumn(1, "Component", width=240)
        self.component_grid.InsertColumn(2, "Refs", width=120)
        self.component_grid.InsertColumn(3, "Detail", width=240)
        self.component_grid.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_ref_select)
        self.component_grid.Bind(wx.EVT_LIST_COL_CLICK, self._on_component_column_click)

        left_sizer = wx.BoxSizer(wx.VERTICAL)
        left_sizer.Add(self.component_grid, 1, wx.EXPAND)
        left_panel.SetSizer(left_sizer)

        right_panel = self._build_detail_panel(splitter)

        splitter.SetSashGravity(0.5)
        splitter.SplitVertically(left_panel, right_panel, -1)
        splitter.SetMinimumPaneSize(300)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(top, 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(wx.StaticLine(parent), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(splitter, 1, wx.EXPAND | wx.ALL, 4)
        parent.SetSizer(sizer)
        self._update_reconcile_status()
        wx.CallAfter(self._set_components_splitter_half)

    def _on_components_splitter_size(self, event):
        event.Skip()
        wx.CallAfter(self._set_components_splitter_half)

    def _set_components_splitter_half(self):
        splitter = getattr(self, "_components_splitter", None)
        if not splitter or not splitter.IsSplit():
            return
        width = splitter.GetClientSize().GetWidth()
        if width > 0:
            splitter.SetSashPosition(width // 2)

    def _build_detail_panel(self, parent):
        panel = wx.Panel(parent)
        outer = wx.BoxSizer(wx.VERTICAL)

        self._detail_header = wx.StaticText(panel, label="Select a component")
        font = self._detail_header.GetFont()
        font.SetWeight(wx.FONTWEIGHT_BOLD)
        self._detail_header.SetFont(font)
        outer.Add(self._detail_header, 0, wx.ALL, 10)
        outer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        self._detail_scroll = wx.ScrolledWindow(panel, style=wx.VSCROLL)
        self._detail_scroll.SetScrollRate(0, 12)
        self._detail_scroll_sizer = wx.BoxSizer(wx.VERTICAL)
        self._detail_scroll.SetSizer(self._detail_scroll_sizer)
        outer.Add(self._detail_scroll, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 4)

        outer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        # Supplier + source action
        action_area = wx.BoxSizer(wx.VERTICAL)
        sup_row = wx.BoxSizer(wx.HORIZONTAL)
        sup_row.Add(wx.StaticText(panel, label="Supplier:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._detail_supplier = wx.Choice(panel, choices=["LCSC", "DigiKey", "Mouser", "N/A"])
        self._detail_supplier.SetSelection(0)
        sup_row.Add(self._detail_supplier, 1, wx.RIGHT, 8)
        self._btn_source_part = wx.Button(panel, label="Source")
        self._btn_source_part.Bind(wx.EVT_BUTTON, self._on_find_part_clicked)
        self._btn_source_part.Disable()
        sup_row.Add(self._btn_source_part, 0)
        action_area.Add(sup_row, 0, wx.EXPAND | wx.BOTTOM, 6)
        outer.Add(action_area, 0, wx.EXPAND | wx.ALL, 10)

        outer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        # Result area
        self._detail_result_panel = wx.Panel(panel)
        rs = wx.BoxSizer(wx.VERTICAL)
        self._detail_result_part = wx.StaticText(self._detail_result_panel, label="")
        f2 = self._detail_result_part.GetFont()
        f2.SetWeight(wx.FONTWEIGHT_BOLD)
        self._detail_result_part.SetFont(f2)
        rs.Add(self._detail_result_part, 0, wx.ALL, 8)
        self._detail_result_detail = wx.StaticText(self._detail_result_panel, label="")
        rs.Add(self._detail_result_detail, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self._detail_result_just = wx.StaticText(self._detail_result_panel, label="")
        self._detail_result_just.SetForegroundColour(wx.Colour(150, 150, 150))
        rs.Add(self._detail_result_just, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        rbtn_row = wx.BoxSizer(wx.HORIZONTAL)
        self._btn_result_datasheet = wx.Button(self._detail_result_panel, label="Datasheet")
        self._btn_result_datasheet.Bind(wx.EVT_BUTTON, lambda e: self._on_download_datasheet(e, self._current_row()))
        rbtn_row.Add(self._btn_result_datasheet, 0, wx.RIGHT, 6)
        self._btn_result_symbol = wx.Button(self._detail_result_panel, label="Get KiCad symbol")
        self._btn_result_symbol.Bind(wx.EVT_BUTTON, self._on_get_symbol_detail)
        rbtn_row.Add(self._btn_result_symbol, 0)
        rs.Add(rbtn_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self._detail_result_panel.SetSizer(rs)
        self._detail_result_panel.Hide()
        outer.Add(self._detail_result_panel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        panel.SetSizer(outer)
        return panel

    def _build_search_tab(self, parent):
        splitter = wx.SplitterWindow(parent, style=wx.SP_LIVE_UPDATE | wx.SP_3DSASH)

        # Left: form
        form = wx.Panel(splitter)
        fs = wx.BoxSizer(wx.VERTICAL)

        sup_row = wx.BoxSizer(wx.HORIZONTAL)
        sup_row.Add(wx.StaticText(form, label="Supplier:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._search_supplier = wx.Choice(form, choices=["LCSC / JLCPCB", "DigiKey", "Mouser"])
        self._search_supplier.SetSelection(0)
        sup_row.Add(self._search_supplier, 1)
        fs.Add(sup_row, 0, wx.EXPAND | wx.BOTTOM, 10)

        fs.Add(wx.StaticText(form, label="Describe what you need:"), 0, wx.BOTTOM, 4)
        self._search_desc = wx.TextCtrl(form, size=(-1, 80), style=wx.TE_MULTILINE)
        self._search_desc.SetHint("e.g. \"N-channel MOSFET Vds>100V Ids>5A low Rds_on\"")
        fs.Add(self._search_desc, 0, wx.EXPAND | wx.BOTTOM, 10)

        grid = wx.FlexGridSizer(4, 2, 6, 8)
        grid.AddGrowableCol(1)
        grid.Add(wx.StaticText(form, label="Package:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._search_footprint = wx.TextCtrl(form)
        self._search_footprint.SetHint("e.g. 0603, SOT-23-5")
        grid.Add(self._search_footprint, 1, wx.EXPAND)
        grid.Add(wx.StaticText(form, label="Max $/unit:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._search_max_price = wx.TextCtrl(form)
        self._search_max_price.SetHint("e.g. 0.50")
        grid.Add(self._search_max_price, 1, wx.EXPAND)
        grid.Add(wx.StaticText(form, label="Min stock:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._search_min_qty = wx.TextCtrl(form)
        self._search_min_qty.SetHint("e.g. 1000")
        grid.Add(self._search_min_qty, 1, wx.EXPAND)
        grid.Add(wx.StaticText(form, label="Results:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._search_count = wx.SpinCtrl(form, value="3", min=1, max=5, initial=3)
        grid.Add(self._search_count, 1, wx.EXPAND)
        fs.Add(grid, 0, wx.EXPAND | wx.BOTTOM, 10)

        self._search_btn = wx.Button(form, label="Search")
        self._search_btn.Bind(wx.EVT_BUTTON, self._on_search_tab_search)
        fs.Add(self._search_btn, 0, wx.BOTTOM, 6)
        self._search_status = wx.StaticText(form, label="")
        fs.Add(self._search_status, 0, wx.EXPAND)
        form.SetSizer(fs)

        # Right: scrollable results list
        results_outer = wx.Panel(splitter)
        ro_sizer = wx.BoxSizer(wx.VERTICAL)
        self._search_results_scroll = wx.ScrolledWindow(results_outer, style=wx.VSCROLL)
        self._search_results_scroll.SetScrollRate(0, 12)
        self._search_results_sizer = wx.BoxSizer(wx.VERTICAL)
        self._search_results_scroll.SetSizer(self._search_results_sizer)
        ro_sizer.Add(self._search_results_scroll, 1, wx.EXPAND)
        results_outer.SetSizer(ro_sizer)
        results_outer.Hide()
        self._search_result_panel = results_outer

        splitter.SplitVertically(form, results_outer, 380)
        splitter.SetMinimumPaneSize(200)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(splitter, 1, wx.EXPAND | wx.ALL, 12)
        parent.SetSizer(sizer)

    # ------------------------------------------------------------------
    # Populate / refresh helpers
    # ------------------------------------------------------------------

    def _component_record(self, ref):
        props = self.sch.get_properties(ref)
        return {
            "refs": [ref],
            "reference": ref,
            "value": props.get("Value", ""),
            "supplier": _first_property(props, ("Supplier",)),
            "footprint": props.get("Footprint", ""),
            "lcsc": _first_property(props, JLCPCB_FIELD_NAMES),
            "digikey": _first_property(props, DIGIKEY_FIELD_NAMES),
            "mouser": _first_property(props, MOUSER_FIELD_NAMES),
            "qty": props.get(QUANTITY_FIELD, ""),
            "price": props.get(PRICE_FIELD, ""),
            "timestamp": props.get(TIMESTAMP_FIELD, ""),
            "datasheet": _first_property(props, DATASHEET_FIELD_NAMES),
        }

    def _format_refs(self, refs):
        if len(refs) <= 3:
            return ", ".join(refs)
        return f"{', '.join(refs[:3])} +{len(refs) - 3}"

    def _same_or_mixed(self, values):
        real_values = [value for value in values if value]
        if not real_values:
            return ""
        first = real_values[0]
        if all(value == first for value in real_values):
            return first
        return "Mixed"

    def _part_number_for_row(self, row):
        supplier = row["supplier"].strip().upper()
        if supplier in NA_SUPPLIERS:
            return "N/A"
        if supplier == "LCSC":
            return row["lcsc"]
        if supplier in {"DIGIKEY", "DIGI-KEY"}:
            return row["digikey"]
        if supplier == "MOUSER":
            return row["mouser"]
        return row["lcsc"] or row["digikey"] or row["mouser"]

    def _row_records(self, row):
        """The underlying per-component records for a row (grouped or flat).

        Grouped rows carry their members under '_records'; a flat row is its
        own single record. Issues are always computed per-record so grouped
        and flat views never disagree, and 'Mixed' group values are never
        treated as issues."""
        return row.get("_records") or [row]

    def _record_issues(self, record):
        issues = []
        if not record["footprint"].strip():
            issues.append("missing footprint")
        supplier = record["supplier"].strip()
        if not supplier:
            issues.append("missing supplier")
        if supplier.upper() in NA_SUPPLIERS:
            return issues
        if not self._part_number_for_row(record):
            issues.append("missing part number")
        if not record.get("datasheet", ""):
            issues.append("missing datasheet")
        return issues

    def _row_issues(self, row):
        issues = []
        for record in self._row_records(row):
            for issue in self._record_issues(record):
                if issue not in issues:
                    issues.append(issue)
        return issues

    def _datasheet_state(self, row):
        if row["supplier"].strip().upper() in NA_SUPPLIERS:
            return "N/A"
        datasheet = row.get("datasheet", "")
        if datasheet == "Mixed":
            return "Mixed"
        return "Ready" if datasheet else "Missing"

    def _row_missing_label(self, row):
        issues = self._row_issues(row)
        if not issues:
            return "None"
        return ", ".join(issue.replace("missing ", "").replace("mixed ", "mixed ") for issue in issues)

    def _record_missing_fields(self, record):
        missing = []
        if not self._part_number_for_row(record):
            missing.append("part number")
        if not record.get("qty", ""):
            missing.append("quantity")
        if not record.get("price", ""):
            missing.append("price")
        if not record.get("timestamp", ""):
            missing.append("timestamp")
        return missing

    def _populate_missing_fields(self, row):
        missing = []
        for record in self._row_records(row):
            if record["supplier"].strip().upper() in NA_SUPPLIERS:
                continue
            for field in self._record_missing_fields(record):
                if field not in missing:
                    missing.append(field)
        return missing

    def _src_issue(self, severity, category, location, message):
        return {
            "severity": severity,
            "category": category,
            "location": location,
            "message": message,
        }

    def _src_checks(self, refs=None):
        refs = set(refs or [])
        issues = []
        records = [
            record for record in self._ungrouped_component_records()
            if not refs or record["reference"] in refs
        ]
        canonical_keys = {
            "Reference", "Value", "Footprint", "Supplier", "LCSC",
            "Digikey Part Number", QUANTITY_FIELD, PRICE_FIELD,
            TIMESTAMP_FIELD, DATASHEET_URL_FIELD, DATASHEET_LOCAL_FIELD,
        }
        alias_to_canonical = {
            "digikey part number": "Digikey Part Number",
            "digi-key part number": "Digikey Part Number",
            "digikey": "Digikey Part Number",
            "digi-key": "Digikey Part Number",
            "lcsc part": "LCSC",
            "jlcpcb part": "LCSC",
            "supplier part": "LCSC",
            "datasheet url": DATASHEET_URL_FIELD,
            "quantity": QUANTITY_FIELD,
            "qty": QUANTITY_FIELD,
        }

        for record in records:
            props = self.sch.get_properties(record["reference"])
            for key in props:
                stripped = key.strip()
                lowered = stripped.lower()
                if key != stripped:
                    issues.append(self._src_issue("warning", "Typos", record["reference"], f"Property key has leading/trailing whitespace: '{key}'."))
                canonical = alias_to_canonical.get(lowered)
                if canonical and key != canonical:
                    issues.append(self._src_issue("warning", "Typos", record["reference"], f"Property '{key}' should be '{canonical}' for consistency."))
                elif not canonical and key not in canonical_keys:
                    for expected in canonical_keys:
                        if SequenceMatcher(None, lowered, expected.lower()).ratio() >= 0.86:
                            issues.append(self._src_issue("warning", "Typos", record["reference"], f"Property '{key}' looks like '{expected}'."))
                            break

            supplier = record["supplier"].strip()
            if supplier and supplier.upper() not in {"LCSC", "DIGIKEY", "DIGI-KEY", "MOUSER"} | NA_SUPPLIERS:
                issues.append(self._src_issue("error", "Consistency", record["reference"], f"Unknown supplier '{supplier}'. Use LCSC, Digi-Key, Mouser, or N/A."))

        for row in self._build_component_rows():
            if refs and not any(ref in refs for ref in row["refs"]):
                continue
            mixed_fields = [
                label for label, key in (
                    ("supplier", "supplier"),
                    ("quantity", "qty"),
                    ("price", "price"),
                    ("datasheet", "datasheet"),
                )
                if row.get(key) == "Mixed"
            ]
            if mixed_fields:
                issues.append(self._src_issue("warning", "Consistency", row["reference"], f"Grouped parts disagree on {', '.join(mixed_fields)}."))

        board_refs = set(self._board_footprint_map())
        schematic_refs = set(self.sch.get_references()) - {ref for ref in self.sch.get_references() if ref.startswith("#")}
        if board_refs:
            for ref in sorted(board_refs - schematic_refs):
                if not refs or ref in refs:
                    issues.append(self._src_issue("error", "Orphans", ref, "PCB footprint reference has no schematic symbol."))
            for ref in sorted(schematic_refs - board_refs):
                if not refs or ref in refs:
                    issues.append(self._src_issue("warning", "Orphans", ref, "Schematic symbol has no matching PCB footprint reference."))

        near_dups = self._near_duplicate_src_issues(records)
        issues.extend(near_dups)
        return issues

    def _near_duplicate_src_issues(self, records):
        issues = []
        fields = [
            ("Value", "value"),
            ("Footprint", "footprint"),
            ("LCSC", "lcsc"),
            ("Digi-Key part", "digikey"),
        ]
        for label, key in fields:
            values = {}
            for record in records:
                value = record.get(key, "").strip()
                norm = re.sub(r"[^a-z0-9]+", "", value.lower())
                if len(norm) < 4:
                    continue
                values.setdefault(norm, {"value": value, "refs": []})["refs"].extend(record["refs"])
            norms = list(values)
            found = 0
            for i, left in enumerate(norms):
                for right in norms[i + 1:]:
                    if left == right:
                        continue
                    ratio = SequenceMatcher(None, left, right).ratio()
                    if ratio < 0.88:
                        continue
                    left_value = values[left]["value"]
                    right_value = values[right]["value"]
                    if left_value == right_value:
                        continue
                    location = f"{self._format_refs(values[left]['refs'])} / {self._format_refs(values[right]['refs'])}"
                    issues.append(self._src_issue("warning", "Near dups", location, f"{label} values are very similar: '{left_value}' vs '{right_value}'."))
                    found += 1
                    if found >= 8:
                        break
                if found >= 8:
                    break
        return issues

    def _row_key(self, row):
        return "|".join(row["refs"])

    def _row_call_label(self, row):
        return self.row_call_status.get(self._row_key(row), "Idle")

    def _set_row_call_status(self, row, label):
        if row:
            self.row_call_status[self._row_key(row)] = label

    def _mark_row_call(self, row, label):
        self._set_row_call_status(row, label)
        selected = row["refs"][0] if row and row["refs"] else self._current_ref()
        self._populate_components(selected)
        wx.YieldIfNeeded()

    def _row_for_ref(self, ref):
        for row in self._build_component_rows():
            if ref in row["refs"]:
                return row
        return None

    def _component_label(self, row):
        value = row["value"] or "(no value)"
        footprint = row["footprint"] or "(no footprint)"
        return f"{value}  {footprint}"

    def _build_component_rows(self):
        records = self._ungrouped_component_records()

        if not self.grouped_view:
            return records

        groups = {}
        for record in records:
            key = (record["value"], record["footprint"])
            groups.setdefault(key, []).append(record)

        rows = []
        for (value, footprint), group in groups.items():
            refs = []
            for record in group:
                refs.extend(record["refs"])
            rows.append(
                {
                    "refs": refs,
                    "reference": self._format_refs(refs),
                    "value": value,
                    "supplier": self._same_or_mixed([record["supplier"] for record in group]),
                    "footprint": footprint,
                    "lcsc": self._same_or_mixed([record["lcsc"] for record in group]),
                    "digikey": self._same_or_mixed([record["digikey"] for record in group]),
                    "mouser": self._same_or_mixed([record["mouser"] for record in group]),
                    "qty": self._same_or_mixed([record["qty"] for record in group]),
                    "price": self._same_or_mixed([record["price"] for record in group]),
                    "timestamp": max((record["timestamp"] for record in group if record["timestamp"]), default=""),
                    "datasheet": self._same_or_mixed([record["datasheet"] for record in group]),
                    "_records": group,
                }
            )
        return rows

    def _row_values(self, row):
        status = self._row_status(row)
        supplier = row["supplier"].strip().upper()
        if supplier in NA_SUPPLIERS:
            detail = "N/A"
        else:
            part = self._part_number_for_record(row)
            missing = self._populate_missing_fields(row)
            if missing:
                detail = ", ".join(missing)
            else:
                detail = part or "Ready"
        return [
            status,
            self._component_label(row),
            row["reference"],
            detail,
        ]

    def _row_status(self, row):
        issues = self._row_issues(row)
        if "missing footprint" in issues or "missing supplier" in issues:
            return "Blocked"
        suppliers = [record["supplier"].strip().upper() for record in self._row_records(row)]
        if suppliers and all(supplier in NA_SUPPLIERS for supplier in suppliers):
            return "N/A"
        if self._populate_missing_fields(row):
            return "Needs"
        return "Done"

    def _row_matches_search(self, row, query):
        if not query:
            return True
        haystack = " ".join(str(value) for value in [
            row["reference"],
            row["value"],
            row["supplier"],
            row["footprint"],
            row["lcsc"],
            row["digikey"],
            row["qty"],
            row["price"],
            row["timestamp"],
            row["datasheet"],
        ]).lower()
        return query.lower() in haystack

    def _row_matches_filter(self, row):
        mode = self.filter_mode
        issues = self._row_issues(row)
        if mode == "All components":
            return True
        if mode == "Issues only":
            return bool(issues)
        if mode == "Needs footprint":
            return any(issue in issues for issue in ("missing footprint", "mixed footprints"))
        if mode == "Missing supplier":
            return any(issue in issues for issue in ("missing supplier", "mixed suppliers"))
        if mode == "Needs sourcing":
            return bool(self._populate_missing_fields(row))
        if mode == "Ready":
            return not issues
        return True

    def _ungrouped_component_records(self):
        records = []
        for ref in self.sch.get_references():
            if ref.startswith("#"):
                continue
            records.append(self._component_record(ref))
        return records

    def _part_number_for_record(self, record):
        supplier = record["supplier"].strip().upper()
        if supplier in NA_SUPPLIERS:
            return "N/A"
        if supplier == "LCSC":
            return record["lcsc"]
        if supplier in {"DIGIKEY", "DIGI-KEY"}:
            return record["digikey"]
        return record["lcsc"] or record["digikey"] or record.get("mouser", "")

    def _update_reconcile_status(self):
        records = self._ungrouped_component_records()
        total = len(records)
        footprints = sum(1 for record in records if record["footprint"])
        suppliers = sum(1 for record in records if record["supplier"])
        part_numbers = sum(1 for record in records if self._part_number_for_record(record))
        self.footprint_status.SetLabel(f"Footprints {footprints}/{total}")
        self.supplier_status.SetLabel(f"Suppliers {suppliers}/{total}")
        self.part_status.SetLabel(f"Sourced {part_numbers}/{total}")
        issue_count = sum(1 for row in self.component_rows if self._row_issues(row))
        if hasattr(self, "btn_next_issue"):
            self.btn_next_issue.SetLabel(f"Next issue ({issue_count})")
        if hasattr(self, "issue_status"):
            if issue_count:
                self.issue_status.SetLabel(f"{issue_count} issue(s)")
                self.issue_status.SetForegroundColour(wx.Colour(230, 120, 110))
            else:
                self.issue_status.SetLabel("No issues")
                self.issue_status.SetForegroundColour(wx.Colour(110, 200, 140))

    def _update_step_summary(self):
        pass

    def _set_src_logs(self, issues):
        self.src_logs = issues
        self._render_src_logs()

    def _render_src_logs(self):
        if not hasattr(self, "_src_log_sizer"):
            return
        self._src_log_sizer.Clear(delete_windows=True)
        if not self.src_logs:
            ok = wx.StaticText(self._src_log_view, label="No review output yet. Run a check above (Typos, Consistency, Orphans, Near dups).")
            ok.SetForegroundColour(wx.Colour(150, 150, 150))
            self._src_log_sizer.Add(ok, 0, wx.EXPAND | wx.ALL, 8)
        else:
            for issue in self.src_logs[:40]:
                panel = wx.Panel(self._src_log_view)
                severity = issue.get("severity", "warning")
                if severity == "error":
                    panel.SetBackgroundColour(wx.Colour(58, 34, 34))
                    fg = wx.Colour(235, 120, 120)
                elif severity == "info":
                    panel.SetBackgroundColour(wx.Colour(34, 44, 58))
                    fg = wx.Colour(120, 175, 235)
                else:
                    panel.SetBackgroundColour(wx.Colour(58, 49, 32))
                    fg = wx.Colour(230, 185, 90)
                box = wx.BoxSizer(wx.VERTICAL)
                heading = wx.StaticText(panel, label=f"{issue.get('category', 'SRC')} - {issue.get('location', '')}")
                heading.SetForegroundColour(fg)
                body = wx.StaticText(panel, label=issue.get("message", ""))
                body.Wrap(210)
                body.SetForegroundColour(wx.Colour(220, 220, 220))
                box.Add(heading, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 6)
                box.Add(body, 0, wx.EXPAND | wx.ALL, 6)
                panel.SetSizer(box)
                self._src_log_sizer.Add(panel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        self._src_log_view.FitInside()
        self._src_log_view.Layout()

    def _populate_components(self, selected_ref=None):
        self.component_grid.DeleteAllItems()
        self.component_rows = [
            row for row in self._build_component_rows()
            if self._row_matches_search(row, self.search_query) and self._row_matches_filter(row)
        ]
        self.component_rows.sort(
            key=lambda row: (str(self._row_values(row)[self.sort_column]).lower(), row["reference"]),
            reverse=self.sort_reverse,
        )
        # Stable second pass: float rows with issues to the top regardless of sort.
        self.component_rows.sort(key=lambda row: 0 if self._row_issues(row) else 1)
        for row in self.component_rows:
            row_index = self.component_grid.InsertItem(self.component_grid.GetItemCount(), self._row_values(row)[0])
            for column, value in enumerate(self._row_values(row)[1:], start=1):
                self.component_grid.SetItem(row_index, column, value)
            self.component_grid.SetItemData(row_index, row_index)
            self._style_component_row(row_index, row)
            if selected_ref and selected_ref in row["refs"]:
                self.component_grid.Select(row_index)
                self.component_grid.Focus(row_index)
        self._update_reconcile_status()
        self._autosize_columns()

    _COL_MAX_WIDTHS = (120, 280, 160, 320)

    def _autosize_columns(self):
        g = self.component_grid
        for col, max_w in enumerate(self._COL_MAX_WIDTHS):
            g.SetColumnWidth(col, wx.LIST_AUTOSIZE_USEHEADER)
            header_w = g.GetColumnWidth(col)
            if g.GetItemCount():
                g.SetColumnWidth(col, wx.LIST_AUTOSIZE)
                content_w = g.GetColumnWidth(col)
            else:
                content_w = 0
            g.SetColumnWidth(col, min(max(header_w, content_w), max_w))

    def _style_component_row(self, row_index, row):
        status = self._row_status(row)
        if status in {"Done", "N/A"}:
            colour = wx.Colour(34, 48, 38)
        elif status == "Needs":
            colour = wx.Colour(58, 49, 32)
        else:
            colour = wx.Colour(58, 34, 34)
        self.component_grid.SetItemBackgroundColour(row_index, colour)

    def _reset_prop_grid(self):
        return

    def _append_prop_row(self, key, value, refs, editable=True):
        return

    def _refresh_current_selection(self, selected_ref=None):
        return

    def _load_group_props(self, row):
        return

    def _on_group_toggle(self, event):
        current_refs = self._current_refs()
        selected_ref = current_refs[0] if current_refs else None
        self._populate_components(selected_ref)

    def _on_search_changed(self, event):
        self.search_query = self.search_field.GetValue().strip()
        selected_refs = self._current_refs()
        self._populate_components(selected_refs[0] if selected_refs else None)

    def _on_filter_changed(self, event):
        self.filter_mode = self.filter_choice.GetStringSelection()
        selected_refs = self._current_refs()
        self._populate_components(selected_refs[0] if selected_refs else None)

    def _on_view_mode_changed(self, event):
        self.grouped_view = self._view_mode_choice.GetStringSelection() == "Grouped"
        self._on_group_toggle(event)

    def _run_src(self, category):
        for button in self._src_buttons:
            button.Disable()
        try:
            self._set_src_logs([
                self._src_issue("info", category, "Exporting", "Exporting schematic netlist XML...")
            ])
            self._set_status(f"Exporting netlist for SRC {category}...")
            wx.YieldIfNeeded()
            xml = self._export_kicad_xml_netlist()

            self._set_src_logs([
                self._src_issue("info", category, "Submitted", "Submitted to backend; waiting for result...")
            ])
            self._set_status(f"Running backend SRC {category}...")
            wx.YieldIfNeeded()
            job_id = self._submit_src_job(category, xml)
            result = self._poll_job(job_id, f"SRC {category}")
        except HTTPError as e:
            if self._handle_http_error(e):
                return
            self._set_src_logs([self._src_issue("error", category, "Backend", f"SRC {category} failed: {e}")])
            return self._set_status(f"SRC {category} failed: {e}", wx.RED)
        except (URLError, TimeoutError, RuntimeError, KeyError, json.JSONDecodeError, subprocess.SubprocessError) as e:
            self._set_src_logs([
                self._src_issue("error", category, "Backend", f"SRC {category} failed: {e}")
            ])
            return self._set_status(f"SRC {category} failed: {e}", wx.RED)
        finally:
            for button in self._src_buttons:
                button.Enable()

        issues = result.get("issues", [])
        normalized = []
        for issue in issues:
            normalized.append({
                "severity": issue.get("severity", "warning"),
                "category": category,
                "location": issue.get("location", ""),
                "message": issue.get("message", ""),
            })
        if not normalized:
            normalized.append(self._src_issue("info", category, "Complete", "No issues found."))
        self._set_src_logs(normalized)
        selected = self._current_ref()
        self._populate_components(selected)
        self._set_status(f"SRC {category}: {len(issues)} issue(s) found.{self._model_suffix(result)}")

    def _on_src_category(self, event, category):
        return self._run_src(category)

    def _on_next_issue(self, event):
        if not self.component_rows:
            return self._set_status("No components match the current view.", wx.RED)

        start = self.component_grid.GetFirstSelected()
        for offset in range(1, len(self.component_rows) + 1):
            index = ((start if start != -1 else -1) + offset) % len(self.component_rows)
            row = self.component_rows[index]
            if not self._row_issues(row):
                continue
            self.component_grid.Select(index)
            self.component_grid.Focus(index)
            self.component_grid.EnsureVisible(index)
            self._set_status(f"{row['reference']}: {', '.join(self._row_issues(row))}.")
            return
        self._set_status("No unresolved component metadata issues in this view.")

    def _on_component_column_click(self, event):
        column = event.GetColumn()
        if column == self.sort_column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = column
            self.sort_reverse = False
        selected_refs = self._current_refs()
        self._populate_components(selected_refs[0] if selected_refs else None)

    def _load_props(self, reference):
        return

    def _current_ref(self):
        refs = self._current_refs()
        return refs[0] if refs else None

    def _current_rows(self):
        rows = []
        idx = self.component_grid.GetFirstSelected()
        while idx != -1:
            if idx < len(self.component_rows):
                rows.append(self.component_rows[idx])
            idx = self.component_grid.GetNextSelected(idx)
        return rows

    def _current_refs(self):
        refs = []
        for row in self._current_rows():
            refs.extend(row["refs"])
        return refs

    def _current_row(self):
        rows = self._current_rows()
        return rows[0] if rows else None

    def _set_status(self, msg, colour=None):
        self.status_text.SetLabel(msg)
        if colour:
            self.status_text.SetForegroundColour(colour)
        else:
            self.status_text.SetForegroundColour(wx.NullColour)

    def _show_backend_error(self, summary: str, detail: str = "") -> None:
        body = summary
        if detail:
            body += f"\n\nDetails:\n{detail}"
        body += "\n\nIf this keeps happening, check that the traces server is running (uv run traces-serve)."
        wx.MessageBox(body, "Backend Error", wx.OK | wx.ICON_ERROR, self)
        self._set_status(summary, wx.RED)

    def _autosave(self, success_message=None):
        try:
            self.sch.write(self.sch_path)
            self.dirty = False
            if success_message:
                self._set_status(success_message)
            return True
        except Exception as e:
            self.dirty = True
            self._set_status(f"Autosave failed: {e}", wx.RED)
            return False

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_ref_select(self, event):
        rows = self._current_rows()
        if not rows:
            return
        if len(rows) > 1:
            return
        row = rows[0]
        self._load_detail_for_row(row)

    def _on_add_part(self, _event):
        dlg = CandidateSearchDialog(self, "add")
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()

    # ------------------------------------------------------------------
    # Update check / self-update
    # ------------------------------------------------------------------

    def _check_for_update(self):
        if INSTALLED_VERSION == "0.0.0":
            return  # unpackaged/dev copy — nothing to compare against
        latest = fetch_latest_pypi_version()
        if latest and _is_newer(latest, INSTALLED_VERSION):
            wx.CallAfter(self._show_update_available, latest)

    def _show_update_available(self, latest):
        self._latest_version = latest
        self.btn_update.SetLabel(f"Update to v{latest}")
        self.btn_update.Show()
        self.Layout()
        self._set_status(f"Update available: v{INSTALLED_VERSION} → v{latest}.")

    def _on_update_clicked(self, _event):
        if not self._latest_version:
            return
        self.btn_update.Disable()
        self._set_status("Updating traces…")
        threading.Thread(target=self._perform_update, daemon=True).start()

    def _perform_update(self):
        env = _update_env()
        uv = shutil.which("uv", path=env["PATH"])
        pipx = shutil.which("pipx", path=env["PATH"])
        attempts = []
        if uv:
            attempts.append([uv, "tool", "upgrade", PYPI_PACKAGE])
        if pipx:
            attempts.append([pipx, "upgrade", PYPI_PACKAGE])

        output = ""
        upgraded = False
        for cmd in attempts:
            try:
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   env=env, timeout=240)
                output = (r.stdout or "") + (r.stderr or "")
                if r.returncode == 0:
                    upgraded = True
                    break
            except Exception as e:
                output = str(e)

        if not upgraded:
            wx.CallAfter(self._update_failed, output)
            return

        usetraces = shutil.which("usetraces", path=env["PATH"])
        if not usetraces:
            wx.CallAfter(self._update_failed, output)
            return
        try:
            r = subprocess.run([usetraces, "install", "--force"],
                               capture_output=True, text=True, env=env, timeout=120)
            output = (r.stdout or "") + (r.stderr or "")
            if r.returncode != 0:
                wx.CallAfter(self._update_failed, output)
                return
        except Exception as e:
            wx.CallAfter(self._update_failed, str(e))
            return

        wx.CallAfter(self._update_succeeded)

    def _update_succeeded(self):
        self.btn_update.Hide()
        self.Layout()
        self._set_status(f"Updated to v{self._latest_version}. Restart KiCad to load it.")
        wx.MessageBox(
            f"traces updated to v{self._latest_version}.\n\n"
            "Restart KiCad to load the new version.",
            "Update complete", wx.OK | wx.ICON_INFORMATION, self)

    def _update_failed(self, detail):
        self.btn_update.Enable()
        self._set_status("Automatic update failed — run the command shown.", wx.RED)
        command = f"uv tool upgrade {PYPI_PACKAGE} && usetraces install"
        dlg = wx.Dialog(self, title="Update traces", size=(560, 280),
                        style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(dlg, label=(
            "Couldn't update automatically. Run this in a terminal, then "
            "restart KiCad:")), 0, wx.ALL, 12)
        cmd_ctrl = wx.TextCtrl(dlg, value=command,
                               style=wx.TE_READONLY | wx.TE_CENTER)
        sizer.Add(cmd_ctrl, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)
        sizer.Add(wx.StaticText(dlg, label="(Use `pip install -U usetraces` "
                  "instead if you installed with pip.)"),
                  0, wx.ALL, 12)
        if detail:
            log = wx.TextCtrl(dlg, value=detail.strip(),
                              style=wx.TE_READONLY | wx.TE_MULTILINE)
            log.SetMinSize((-1, 90))
            sizer.Add(log, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)
        btns = wx.BoxSizer(wx.HORIZONTAL)
        copy_btn = wx.Button(dlg, label="Copy command")

        def _copy(_evt):
            if wx.TheClipboard.Open():
                wx.TheClipboard.SetData(wx.TextDataObject(command))
                wx.TheClipboard.Close()
                copy_btn.SetLabel("Copied")
        copy_btn.Bind(wx.EVT_BUTTON, _copy)
        btns.Add(copy_btn, 0, wx.RIGHT, 8)
        btns.Add(wx.Button(dlg, wx.ID_OK, "Close"), 0)
        sizer.Add(btns, 0, wx.ALIGN_RIGHT | wx.ALL, 12)
        dlg.SetSizer(sizer)
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()

    def _pcb_footprint_id(self, footprint):
        try:
            fpid = footprint.GetFPID()
        except Exception:
            return ""
        for method_name in ("Format", "AsString"):
            method = getattr(fpid, method_name, None)
            if not method:
                continue
            try:
                value = method()
                if value:
                    return str(value)
            except TypeError:
                try:
                    value = method(True)
                    if value:
                        return str(value)
                except Exception:
                    pass
            except Exception:
                pass
        return str(fpid) if fpid else ""

    def _board_footprint_map(self):
        if not self.board:
            return {}
        try:
            footprints = list(self.board.GetFootprints())
        except Exception:
            return {}
        by_ref = {}
        for footprint in footprints:
            try:
                ref = footprint.GetReference()
            except Exception:
                continue
            fpid = self._pcb_footprint_id(footprint)
            if ref and fpid:
                by_ref[ref] = fpid
        return by_ref

    def _on_reconcile_footprints(self, event):
        return self._run_footprints(self._current_row())

    def _run_footprints(self, row=None):
        if row:
            return self._edit_footprint(row)
        return self._reconcile_footprints_from_pcb()

    def _edit_footprint(self, row):
        current_value = "" if row["value"] == "Mixed" else row["value"]
        current_fp = "" if row["footprint"] == "Mixed" else row["footprint"]

        dlg = wx.Dialog(self, title=f"Edit — {row['reference']}", size=(420, 180),
                        style=wx.DEFAULT_DIALOG_STYLE)
        sizer = wx.BoxSizer(wx.VERTICAL)
        grid = wx.FlexGridSizer(2, 2, 8, 8)
        grid.AddGrowableCol(1)
        grid.Add(wx.StaticText(dlg, label="Value:"), 0, wx.ALIGN_CENTER_VERTICAL)
        val_ctrl = wx.TextCtrl(dlg, value=current_value, size=(300, -1))
        grid.Add(val_ctrl, 1, wx.EXPAND)
        grid.Add(wx.StaticText(dlg, label="Footprint:"), 0, wx.ALIGN_CENTER_VERTICAL)
        fp_ctrl = wx.TextCtrl(dlg, value=current_fp, size=(300, -1))
        grid.Add(fp_ctrl, 1, wx.EXPAND)
        sizer.Add(grid, 0, wx.EXPAND | wx.ALL, 16)
        btn_sizer = dlg.CreateButtonSizer(wx.OK | wx.CANCEL)
        sizer.Add(btn_sizer, 0, wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, 12)
        dlg.SetSizer(sizer)
        val_ctrl.SetFocus()

        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            new_value = val_ctrl.GetValue().strip()
            new_fp = fp_ctrl.GetValue().strip()
        finally:
            dlg.Destroy()

        for ref in row["refs"]:
            if new_value:
                self.sch.set_property(ref, "Value", new_value)
            self.sch.set_property(ref, "Footprint", new_fp)
        self.dirty = True
        if not self._autosave():
            return
        self._set_row_call_status(row, "Updated")
        self._populate_components(row["refs"][0])
        self._set_status(f"Updated Value/Footprint on {len(row['refs'])} part(s).")

    def _reconcile_footprints_from_pcb(self):
        updated, missing_on_board = self._reconcile_missing_footprints(None)
        if not updated and not missing_on_board and not self._board_footprint_map():
            return self._set_status("No PCB footprints found to reconcile from.", wx.RED)
        self._populate_components(self._current_ref())
        if updated:
            detail = f"; {len(missing_on_board)} not found on PCB" if missing_on_board else ""
            self._set_status(f"Reconciled {updated} missing footprint(s){detail}.")
        else:
            self._set_status("No missing schematic footprints to reconcile.")

    def _reconcile_missing_footprints(self, refs=None):
        board_footprints = self._board_footprint_map()
        if not board_footprints:
            return 0, []

        refs = set(refs or [])
        updated = 0
        missing_on_board = []
        for record in self._ungrouped_component_records():
            if refs and record["reference"] not in refs:
                continue
            if record["footprint"].strip():
                continue
            ref = record["reference"]
            row = self._row_for_ref(ref)
            footprint = board_footprints.get(ref, "")
            if not footprint:
                missing_on_board.append(ref)
                self._set_row_call_status(row, "No PCB footprint")
                continue
            self.sch.set_property(ref, "Footprint", footprint)
            self._set_row_call_status(row, "Footprint copied from PCB")
            updated += 1

        self.dirty = self.dirty or updated > 0
        if updated:
            self._autosave()
        return updated, missing_on_board

    def _on_reconcile_supplier(self, event):
        return self._run_supplier(self._current_row())

    def _run_supplier(self, row=None):
        selected_refs = row["refs"] if row else []
        if row:
            refs = selected_refs
        else:
            refs = [
                record["reference"]
                for record in self._ungrouped_component_records()
                if not record["supplier"].strip()
            ]
        if not refs:
            return self._set_status("No components need supplier reconciliation.")
        missing_footprints = [
            ref for ref in refs
            if not self.sch.get_property(ref, "Footprint")
        ]
        if missing_footprints:
            return self._set_status(
                f"Reconcile footprints first: {self._format_refs(missing_footprints)}.",
                wx.RED,
            )

        dialog = wx.SingleChoiceDialog(
            self,
            f"Set supplier for {len(refs)} part(s):",
            "Reconcile supplier",
            ["LCSC", "Digi-Key", "Mouser", "N/A"],
        )
        try:
            dialog.SetSelection(0)
            if dialog.ShowModal() != wx.ID_OK:
                return
            supplier = dialog.GetStringSelection()
        finally:
            dialog.Destroy()

        cleared = 0
        for ref in refs:
            old_supplier = self.sch.get_property(ref, "Supplier") or ""
            self.sch.set_property(ref, "Supplier", supplier)
            row_ref = self._row_for_ref(ref)
            if old_supplier.strip() and old_supplier.strip().upper() != supplier.strip().upper():
                if row_ref:
                    self._clear_sourcing_data(row_ref)
                cleared += 1
            self._set_row_call_status(self._row_for_ref(ref), f"Supplier set: {supplier}")
        self.dirty = True
        if not self._autosave():
            return
        selected = refs[0]
        self._populate_components(selected)
        detail = f" (sourcing cleared for {cleared})" if cleared else ""
        self._set_status(f"Set Supplier={supplier} for {len(refs)} part(s){detail}.")

    def _run_populate(self, row=None, event=None):
        if row:
            return self._populate_row(row, event)
        return self._try_populate_rows(None, event)

    def _fix_all_plan(self):
        """Summarize what Fix All will do, without mutating anything."""
        records = self._ungrouped_component_records()
        board_map = self._board_footprint_map()
        missing_fp = [r for r in records if not r["footprint"].strip()]
        fillable_fp = [r for r in missing_fp if r["reference"] in board_map]
        unfillable_fp = [r for r in missing_fp if r["reference"] not in board_map]
        missing_sup = [
            r for r in records
            if r["footprint"].strip() and not r["supplier"].strip()
        ]
        need_source = [
            r for r in records
            if r["footprint"].strip()
            and r["supplier"].strip()
            and r["supplier"].strip().upper() not in NA_SUPPLIERS
            and not self._part_number_for_record(r)
        ]
        return {
            "fillable_fp": fillable_fp,
            "unfillable_fp": unfillable_fp,
            "missing_sup": missing_sup,
            "need_source": need_source,
        }

    def _confirm_fix_all(self):
        plan = self._fix_all_plan()
        lines = ["Fix All will run these steps:\n"]
        if plan["fillable_fp"]:
            lines.append(f"  • Copy {len(plan['fillable_fp'])} footprint(s) from the PCB.")
        if plan["unfillable_fp"]:
            lines.append(
                f"  • {len(plan['unfillable_fp'])} component(s) have no footprint and none on the PCB "
                f"— these will pause Fix All: {self._format_refs([r['reference'] for r in plan['unfillable_fp']])}."
            )
        if plan["missing_sup"]:
            lines.append(f"  • Auto-assign suppliers for {len(plan['missing_sup'])} component(s).")
        if plan["need_source"]:
            lines.append(
                f"  • Source part numbers for {len(plan['need_source'])} component(s) "
                f"(calls the backend; may take a moment)."
            )
        if len(lines) == 1:
            lines.append("  • Nothing to do — everything is already reconciled.")
        lines.append("\nThese changes are written to your schematic. Proceed?")
        dlg = wx.MessageDialog(
            self, "\n".join(lines), "Fix All — preview",
            wx.YES_NO | wx.ICON_QUESTION,
        )
        dlg.SetYesNoLabels("Apply", "Cancel")
        result = dlg.ShowModal()
        dlg.Destroy()
        return result == wx.ID_YES

    def _on_fix_all(self, event):
        if not self._confirm_fix_all():
            return self._set_status("Fix All cancelled.")
        if hasattr(self, "btn_fix_all"):
            self.btn_fix_all.Disable()
        try:
            self._set_status("Fix All: reconciling footprints...")
            updated, missing_on_board = self._reconcile_missing_footprints(None)
            self._populate_components(self._current_ref())
            wx.YieldIfNeeded()

            missing_footprints = [
                record["reference"]
                for record in self._ungrouped_component_records()
                if not record["footprint"].strip()
            ]
            if missing_footprints:
                detail = f" ({updated} copied from PCB)" if updated else ""
                return self._set_status(
                    f"Fix All paused: missing footprints for {self._format_refs(missing_footprints)}{detail}.",
                    wx.RED,
                )

            missing_suppliers = [
                record["reference"]
                for record in self._ungrouped_component_records()
                if not record["supplier"].strip()
            ]
            if missing_suppliers:
                self._set_status("Fix All: setting suppliers...")
                wx.YieldIfNeeded()
                self._run_supplier(None)
                missing_suppliers = [
                    record["reference"]
                    for record in self._ungrouped_component_records()
                    if not record["supplier"].strip()
                ]
                if missing_suppliers:
                    return self._set_status(
                        f"Fix All paused: missing suppliers for {self._format_refs(missing_suppliers)}.",
                        wx.RED,
                    )

            self._set_status("Fix All: sourcing parts...")
            wx.YieldIfNeeded()
            self._try_populate_rows(None, event)
        finally:
            if hasattr(self, "btn_fix_all"):
                self.btn_fix_all.Enable()

    def _run_datasheets(self, row=None, event=None):
        if row:
            return self._on_download_datasheet(event, row=row)
        rows = [
            row for row in self._build_component_rows()
            if self._row_status(row) == "Needs"
        ]
        attempted = 0
        for current in rows:
            self._on_download_datasheet(event, row=current)
            attempted += 1
        self._populate_components(self._current_ref())
        self._set_status(f"Datasheets: {attempted} attempt(s).")

    def _populate_row(self, row, event=None):
        if not row:
            return self._set_status("Select a component or group to source.", wx.RED)
        if not row["footprint"].strip() or row["footprint"] == "Mixed":
            self._mark_row_call(row, "Needs footprint")
            return self._set_status("Reconcile the footprint before populating.", wx.RED)
        supplier = row["supplier"].strip().upper()
        if not supplier or supplier == "MIXED":
            self._mark_row_call(row, "Needs supplier")
            return self._set_status("Reconcile the supplier before populating.", wx.RED)
        if supplier in NA_SUPPLIERS:
            self._mark_row_call(row, "Skipped: N/A")
            self._set_status(f"{row['reference']} marked Supplier=N/A; nothing to source.")
            return True
        if supplier == "LCSC":
            self._mark_row_call(row, "Looking up JLC")
            if row["lcsc"] and row["lcsc"] != "Mixed":
                return self._on_fetch_jlc(event, row=row)
            return self._on_find_jlc_from_value(event, row=row)
        if supplier in {"DIGIKEY", "DIGI-KEY"}:
            self._mark_row_call(row, "Looking up Digi-Key")
            return self._on_find_digikey_from_value(event, row=row)
        if supplier == "MOUSER":
            self._mark_row_call(row, "Looking up Mouser")
            return self._on_find_mouser_from_value(event, row=row)
        self._mark_row_call(row, "Unsupported supplier")
        return self._set_status(f"Unsupported supplier for populate: {row['supplier']}", wx.RED)

    def _on_populate_selected(self, event):
        return self._populate_row(self._current_row(), event)

    def _on_try_all(self, event):
        return self._try_populate_rows(self._current_row(), event)

    def _try_populate_rows(self, selected_row=None, event=None):
        target_refs = selected_row["refs"] if selected_row else None
        selected = target_refs[0] if target_refs else self._current_ref()
        self._populate_components(selected)

        pending_supplier = [
            record["reference"]
            for record in self._ungrouped_component_records()
            if (not target_refs or record["reference"] in target_refs)
            and record["footprint"].strip()
            and not record["supplier"].strip()
        ]
        if pending_supplier:
            if selected_row:
                self._set_row_call_status(selected_row, "Needs supplier")
                self._populate_components(selected)
            return self._set_status(
                f"Set suppliers before sourcing: {self._format_refs(pending_supplier)}.",
                wx.RED,
            )

        attempted = 0
        skipped = 0
        rows = [
            row for row in self._build_component_rows()
            if (not target_refs or any(ref in target_refs for ref in row["refs"]))
            and row["footprint"].strip()
            and row["footprint"] != "Mixed"
            and row["supplier"].strip()
            and row["supplier"] != "Mixed"
            and (
                self._populate_missing_fields(row)
                or row["supplier"].strip().upper() in NA_SUPPLIERS
            )
        ]
        for row in rows:
            supplier = row["supplier"].strip().upper()
            if supplier in NA_SUPPLIERS:
                skipped += 1
                continue
            self._populate_components(row["refs"][0])
            self._populate_row(row, event)
            attempted += 1

        self._populate_components(selected)
        scope = "selected" if target_refs else "all"
        self._set_status(
            f"Source {scope}: {attempted} attempt(s), {skipped} N/A skipped."
        )

    def _selected_supplier(self):
        row = self._current_row()
        if not row:
            return ""
        return row["supplier"].strip().upper()

    def _update_supplier_buttons(self):
        return


    def _clear_sourcing_data(self, row) -> None:
        clear_fields = (
            list(JLCPCB_FIELD_NAMES) + list(DIGIKEY_FIELD_NAMES) + list(MOUSER_FIELD_NAMES) +
            [QUANTITY_FIELD, PRICE_FIELD, TIMESTAMP_FIELD]
        )
        for ref in row["refs"]:
            props = self.sch.get_properties(ref)
            for field in clear_fields:
                if field in props:
                    self.sch.set_property(ref, field, "")
        self.dirty = True
        self._autosave()
        self._set_row_call_status(row, "Sourcing cleared")
        self._populate_components(row["refs"][0])
        self._set_status(f"Cleared sourcing data for {len(row['refs'])} part(s).")

    def _on_clear_sourcing(self, _event) -> None:
        row = self._current_row()
        if not row:
            return self._set_status("Select a row first.", wx.RED)
        self._clear_sourcing_data(row)

    # ------------------------------------------------------------------
    # Detail panel helpers
    # ------------------------------------------------------------------

    def _load_detail_for_row(self, row):
        self._detail_refs = list(row["refs"])
        self._detail_header.SetLabel(row["value"] or "(no value)")

        # Clear existing property controls
        self._detail_scroll_sizer.Clear(delete_windows=True)
        self._detail_field_ctrls = {}

        if not self._detail_refs:
            self._detail_scroll.FitInside()
            self._detail_scroll.Layout()
            return

        ref = self._detail_refs[0]
        props = self.sch.get_properties(ref)
        issues = self._row_issues(row)

        self._add_part_summary(row, issues)

        # Set supplier choice to match stored supplier
        supplier_val = (props.get("Supplier") or "").strip()
        supplier_choices = [self._detail_supplier.GetString(i) for i in range(self._detail_supplier.GetCount())]
        if supplier_val in supplier_choices:
            self._detail_supplier.SetStringSelection(supplier_val)
        else:
            self._detail_supplier.SetStringSelection("N/A")

        self._detail_result_panel.Hide()
        self._btn_source_part.Enable()
        self._detail_scroll.FitInside()
        self._detail_scroll.Layout()
        self._detail_scroll.GetParent().Layout()

    def _missing_property_keys(self, row):
        keys = set()
        for issue in self._row_issues(row):
            if issue == "missing footprint":
                keys.add("Footprint")
            elif issue == "missing supplier":
                keys.add("Supplier")
            elif issue == "missing part number":
                supplier = row["supplier"].strip().upper()
                if supplier == "LCSC":
                    keys.add("LCSC")
                elif supplier in {"DIGIKEY", "DIGI-KEY"}:
                    keys.add("Digikey Part Number")
                elif supplier == "MOUSER":
                    keys.add("Mouser Part Number")
                else:
                    keys.add("LCSC")
            elif issue == "missing datasheet":
                keys.add(DATASHEET_URL_FIELD)
        for field in self._populate_missing_fields(row):
            if field == "quantity":
                keys.add(QUANTITY_FIELD)
            elif field == "price":
                keys.add(PRICE_FIELD)
            elif field == "timestamp":
                keys.add(TIMESTAMP_FIELD)
        return keys

    def _add_detail_section_label(self, text):
        label = wx.StaticText(self._detail_scroll, label=text)
        font = label.GetFont()
        font.SetWeight(wx.FONTWEIGHT_BOLD)
        label.SetFont(font)
        label.SetForegroundColour(wx.Colour(190, 190, 190))
        self._detail_scroll_sizer.Add(label, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, 8)

    def _add_part_summary(self, row, issues):
        panel = wx.Panel(self._detail_scroll)
        panel.SetBackgroundColour(wx.Colour(40, 40, 44))
        sizer = wx.BoxSizer(wx.VERTICAL)

        headline = wx.StaticText(panel, label=row["reference"])
        headline_font = headline.GetFont()
        headline_font.SetWeight(wx.FONTWEIGHT_BOLD)
        headline.SetFont(headline_font)
        sizer.Add(headline, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)

        footprint = row["footprint"] or "(no footprint)"
        sub = wx.StaticText(panel, label=footprint)
        sub.SetForegroundColour(wx.Colour(165, 165, 165))
        sizer.Add(sub, 0, wx.LEFT | wx.RIGHT | wx.TOP, 3)

        facts = wx.FlexGridSizer(4, 2, 8, 14)
        facts.AddGrowableCol(1)
        part_number = self._part_number_for_row(row)
        for label, value, missing in (
            ("Supplier", row["supplier"] or "Missing", not row["supplier"]),
            ("Part", part_number or "Missing", not part_number),
            ("Stock", row.get("qty") or "Missing", not row.get("qty")),
            ("Price", row.get("price") or "Missing", not row.get("price")),
        ):
            lbl = wx.StaticText(panel, label=label)
            lbl.SetForegroundColour(wx.Colour(145, 145, 145))
            val = wx.StaticText(panel, label=str(value))
            val.SetForegroundColour(wx.Colour(235, 145, 120) if missing else wx.Colour(215, 215, 215))
            facts.Add(lbl, 0, wx.ALIGN_CENTER_VERTICAL)
            facts.Add(val, 1, wx.EXPAND)
        sizer.Add(facts, 0, wx.EXPAND | wx.ALL, 10)

        issue_text = "Missing: " + ", ".join(issue.replace("missing ", "") for issue in issues) if issues else "Ready"
        issue_lbl = wx.StaticText(panel, label=issue_text)
        issue_lbl.SetForegroundColour(wx.Colour(235, 145, 120) if issues else wx.Colour(120, 200, 140))
        sizer.Add(issue_lbl, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        link_row = wx.BoxSizer(wx.HORIZONTAL)
        ds_btn = wx.Button(panel, label="Datasheet")
        ds_btn.Bind(wx.EVT_BUTTON, lambda e, r=row: self._open_row_datasheet(r))
        ds_btn.Enable(bool(row.get("datasheet")))
        link_row.Add(ds_btn, 0, wx.RIGHT, 6)
        part_btn = wx.Button(panel, label="Supplier page")
        part_btn.Bind(wx.EVT_BUTTON, lambda e, r=row: self._open_row_product_page(r))
        part_btn.Enable(bool(part_number))
        link_row.Add(part_btn, 0)
        sizer.Add(link_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        panel.SetSizer(sizer)
        self._detail_scroll_sizer.Add(panel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, 8)

    def _add_property_editor_row(self, key, value, missing_keys):
        row_sizer = wx.BoxSizer(wx.HORIZONTAL)
        is_missing = key in missing_keys and not str(value).strip()
        lbl = wx.StaticText(self._detail_scroll, label=key, size=(145, -1))
        lbl.SetForegroundColour(wx.Colour(235, 145, 120) if is_missing else wx.Colour(160, 160, 160))
        ctrl = wx.TextCtrl(self._detail_scroll, value=value, size=(-1, -1))
        if is_missing:
            ctrl.SetBackgroundColour(wx.Colour(58, 38, 34))
            ctrl.SetForegroundColour(wx.Colour(245, 210, 200))
        ctrl.Bind(wx.EVT_TEXT, lambda e, k=key: self._on_detail_field_changed(k))
        del_btn = wx.Button(self._detail_scroll, label="x", size=(24, -1))
        del_btn.Bind(wx.EVT_BUTTON, lambda e, k=key: self._on_detail_delete_prop(k))
        del_btn.Enable(key not in PROTECTED)
        row_sizer.Add(lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        row_sizer.Add(ctrl, 1, wx.EXPAND | wx.RIGHT, 2)
        row_sizer.Add(del_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        self._detail_scroll_sizer.Add(row_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        self._detail_field_ctrls[key] = ctrl

    def _on_detail_field_changed(self, key):
        if not self._detail_refs or key not in self._detail_field_ctrls:
            return
        value = self._detail_field_ctrls[key].GetValue()
        for ref in self._detail_refs:
            self.sch.set_property(ref, key, value)
        self.dirty = True
        self._autosave(f"Saved {key}.")

    def _on_detail_delete_prop(self, key):
        if not self._detail_refs:
            return
        if key in PROTECTED:
            return self._set_status(f"'{key}' is required and cannot be deleted.", wx.RED)
        confirm = wx.MessageBox(
            f"Delete property '{key}' from {len(self._detail_refs)} part(s)?",
            "Confirm delete", wx.YES_NO | wx.ICON_WARNING, self,
        )
        if confirm != wx.YES:
            return
        for ref in self._detail_refs:
            self.sch.delete_property(ref, key)
        self.dirty = True
        if not self._autosave():
            return
        row = self._row_for_ref(self._detail_refs[0])
        if row:
            self._populate_components(self._detail_refs[0])
            self._load_detail_for_row(row)
        self._set_status(f"Deleted '{key}' from {len(self._detail_refs)} part(s).")

    def _on_find_part_clicked(self, event):
        row = self._current_row()
        if not row:
            return self._set_status("Select a component first.", wx.RED)
        supplier_sel = self._detail_supplier.GetStringSelection()
        if supplier_sel == "N/A":
            for ref in row["refs"]:
                self.sch.set_property(ref, "Supplier", "N/A")
            self.dirty = True
            self._autosave()
            self._populate_components(row["refs"][0])
            return self._set_status(f"Marked {row['reference']} as N/A.")
        for ref in row["refs"]:
            self.sch.set_property(ref, "Supplier", supplier_sel)
        self.dirty = True
        self._autosave()
        refreshed = self._row_for_ref(row["refs"][0]) or row
        dlg = CandidateSearchDialog(self, "fill", refreshed)
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()

    def _on_clear_sourcing_detail(self, event):
        row = self._current_row()
        if not row:
            return self._set_status("Select a component first.", wx.RED)
        self._clear_sourcing_data(row)
        self._load_detail_for_row(row)

    def _on_fetch_datasheet_detail(self, event):
        row = self._current_row()
        if not row:
            return self._set_status("Select a component first.", wx.RED)
        self._on_download_datasheet(event, row=row)
        if row:
            self._load_detail_for_row(row)

    def _show_detail_result(self, supplier, result):
        part = result.get("part_number", "")
        mfr = result.get("manufacturer", "")
        price = result.get("price")
        qty = result.get("qty")
        just = result.get("justification", "")
        detail_parts = []
        if mfr:
            detail_parts.append(mfr)
        if price is not None:
            detail_parts.append(f"${price:.4f}" if price < 0.01 else f"${price:.2f}")
        if qty is not None:
            detail_parts.append(f"Stock: {qty}")
        self._detail_result_part.SetLabel(f"{supplier}  {part}")
        self._detail_result_detail.SetLabel("  ".join(detail_parts))
        self._detail_result_just.SetLabel(just)
        self._detail_result_just.Wrap(260)
        self._btn_result_datasheet.Enable(bool(result.get("datasheet_url")))
        self._detail_result_panel.Show()
        self._detail_result_panel.GetParent().Layout()

    def _on_get_symbol_detail(self, event):
        row = self._current_row()
        if not row:
            return self._set_status("Select a component first.", wx.RED)
        supplier = (row.get("supplier") or "").strip().upper()
        if supplier == "LCSC":
            wx.MessageBox(
                "Symbol download for LCSC parts:\n\n"
                "Install easyeda2kicad and run:\n"
                "  easyeda2kicad --full --lcsc_id <LCSC_ID>\n\n"
                "This will download the KiCad symbol/footprint to your library.",
                "Get Symbol (LCSC)", wx.OK | wx.ICON_INFORMATION, self,
            )
        elif supplier in {"DIGIKEY", "DIGI-KEY"}:
            part = row.get("digikey", "")
            if part:
                webbrowser.open(self._supplier_product_url(supplier, part))
            else:
                self._set_status("No DigiKey part number found on this component.", wx.RED)
        elif supplier == "MOUSER":
            part = row.get("mouser", "")
            if part:
                webbrowser.open(self._supplier_product_url(supplier, part))
            else:
                self._set_status("No Mouser part number found on this component.", wx.RED)
        else:
            self._set_status("Set a supplier first.", wx.RED)

    # ------------------------------------------------------------------
    # Search helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _model_suffix(result):
        """' · <model label>' for a job result that reports which model answered."""
        info = (result or {}).get("model") or {}
        label = info.get("label")
        return f"  ·  {label}" if label else ""

    def _submit_search_job(self, supplier_upper, desc, footprint, max_price, min_qty, count):
        payload = {"description": desc, "footprint": footprint, "count": count}
        if max_price:
            try:
                payload["max_price"] = float(max_price)
            except ValueError:
                pass
        if min_qty:
            try:
                payload["min_qty"] = int(min_qty)
            except ValueError:
                pass
        body = json.dumps(payload).encode("utf-8")
        if supplier_upper in {"LCSC / JLCPCB", "LCSC"}:
            url = JLCPCB_SEARCH_URL
        elif supplier_upper in {"DIGIKEY", "DIGI-KEY"}:
            url = DIGIKEY_SEARCH_URL
        else:
            url = MOUSER_SEARCH_URL
        with urlopen(self._api_request(url, body), timeout=10) as resp:
            return json.loads(resp.read().decode())["job_id"]

    def _normalize_supplier_label(self, supplier):
        supplier = (supplier or "").strip()
        supplier_upper = supplier.upper()
        if supplier_upper in {"LCSC / JLCPCB", "LCSC"}:
            return "LCSC"
        if supplier_upper in {"DIGIKEY", "DIGI-KEY"}:
            return "DigiKey"
        if supplier_upper == "MOUSER":
            return "Mouser"
        return supplier

    def _on_search_tab_search(self, event):
        supplier = self._search_supplier.GetStringSelection().strip()
        desc = self._search_desc.GetValue().strip()
        footprint = self._search_footprint.GetValue().strip()
        max_price = self._search_max_price.GetValue().strip()
        min_qty = self._search_min_qty.GetValue().strip()
        count = self._search_count.GetValue()

        if not desc and not footprint:
            return self._set_status("Enter a description or footprint to search.", wx.RED)

        self._search_btn.Disable()
        self._search_status.SetLabel(f"Searching for {count} result(s)...")
        self._search_result_panel.Hide()
        wx.YieldIfNeeded()

        try:
            job_id = self._submit_search_job(supplier.upper(), desc, footprint, max_price, min_qty, count)
            self._search_job_id = job_id
            result = self._poll_job(job_id, f"{supplier} search", status_setter=self._search_status.SetLabel)
        except HTTPError as e:
            if self._handle_http_error(e):
                self._search_btn.Enable()
                self._search_status.SetLabel("")
                return
            self._search_status.SetLabel(f"Search failed: {e}")
            self._search_btn.Enable()
            return
        except (URLError, TimeoutError, RuntimeError, KeyError, json.JSONDecodeError) as e:
            self._search_status.SetLabel(f"Search failed: {e}")
            self._search_btn.Enable()
            return

        self._search_btn.Enable()
        self._show_search_results(result, supplier)

    def _show_search_results(self, result, supplier):
        candidates = result.get("candidates", []) if result else []
        candidates = [c for c in candidates if c.get("part_number")]

        self._search_results_sizer.Clear(delete_windows=True)
        self._search_result = None

        if not candidates:
            self._search_status.SetLabel("No matches found.")
            lbl = wx.StaticText(self._search_results_scroll, label="No results.")
            lbl.SetForegroundColour(wx.Colour(150, 150, 150))
            self._search_results_sizer.Add(lbl, 0, wx.ALL, 12)
            self._search_result_panel.Show()
            self._search_results_scroll.FitInside()
            self._search_result_panel.GetParent().Layout()
            return

        self._search_status.SetLabel(f"{len(candidates)} result(s) found.{self._model_suffix(result)}")
        for candidate in candidates:
            self._add_search_result_card(candidate, supplier)

        self._search_result_panel.Show()
        self._search_results_scroll.FitInside()
        self._search_result_panel.GetParent().Layout()

    def _add_search_result_card(self, candidate, supplier):
        scroll = self._search_results_scroll
        card = wx.Panel(scroll)
        card.SetBackgroundColour(wx.Colour(42, 42, 48))
        cs = wx.BoxSizer(wx.VERTICAL)

        part = candidate.get("part_number", "")
        price = candidate.get("price")
        qty = candidate.get("qty")
        just = candidate.get("justification", "")
        datasheet = candidate.get("datasheet_url", "")
        name = candidate.get("name") or candidate.get("description", "")
        mfr = candidate.get("manufacturer", "")
        footprint = candidate.get("footprint", "")
        specs = candidate.get("specs", []) or []

        # Headline: human-readable name, falling back to the part number.
        headline = name.strip() if name else part
        part_lbl = wx.StaticText(card, label=headline)
        font = part_lbl.GetFont()
        font.SetWeight(wx.FONTWEIGHT_BOLD)
        part_lbl.SetFont(font)
        part_lbl.Wrap(320)
        cs.Add(part_lbl, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)

        # Sub-line: supplier + part number (+ manufacturer)
        sub = f"{supplier}  {part}"
        if mfr:
            sub += f"  ·  {mfr}"
        sub_lbl = wx.StaticText(card, label=sub)
        sub_lbl.SetForegroundColour(wx.Colour(150, 170, 210))
        cs.Add(sub_lbl, 0, wx.LEFT | wx.RIGHT | wx.TOP, 3)

        detail_parts = []
        if price is not None:
            detail_parts.append(f"${price:.4f}" if price < 0.01 else f"${price:.2f}")
        if qty is not None:
            detail_parts.append(f"Stock: {qty:,}")
        if footprint:
            detail_parts.append(footprint)
        if detail_parts:
            detail_lbl = wx.StaticText(card, label="   ".join(detail_parts))
            detail_lbl.SetForegroundColour(wx.Colour(180, 180, 180))
            cs.Add(detail_lbl, 0, wx.LEFT | wx.RIGHT | wx.TOP, 4)

        if specs:
            specs_lbl = wx.StaticText(card, label="  •  ".join(str(s) for s in specs[:6]))
            specs_lbl.SetForegroundColour(wx.Colour(140, 190, 150))
            specs_lbl.Wrap(320)
            cs.Add(specs_lbl, 0, wx.LEFT | wx.RIGHT | wx.TOP, 4)

        if just:
            just_lbl = wx.StaticText(card, label=just)
            just_lbl.SetForegroundColour(wx.Colour(130, 130, 130))
            just_lbl.Wrap(320)
            cs.Add(just_lbl, 0, wx.LEFT | wx.RIGHT | wx.TOP, 4)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        btn_use = wx.Button(card, label="Use this")
        btn_use.Bind(wx.EVT_BUTTON, lambda e, c=candidate: self._on_search_use_candidate(c))
        btn_row.Add(btn_use, 0, wx.RIGHT, 6)
        btn_ds = wx.Button(card, label="Datasheet")
        btn_ds.Bind(wx.EVT_BUTTON, lambda e, c=candidate, u=datasheet: self._on_search_open_datasheet_for(c, u))
        btn_row.Add(btn_ds, 0, wx.RIGHT, 6)
        btn_view = wx.Button(card, label=f"View on {supplier}")
        btn_view.Bind(wx.EVT_BUTTON, lambda e, c=candidate: self._open_product_page(c))
        btn_row.Add(btn_view, 0, wx.RIGHT, 6)
        btn_sym = wx.Button(card, label="Symbol")
        btn_sym.Bind(wx.EVT_BUTTON, lambda e, c=candidate: self._on_search_get_symbol_for(c))
        btn_row.Add(btn_sym, 0)
        cs.Add(btn_row, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)

        # Assign-to: searchable dropdown of schematic components.
        assign_row = wx.BoxSizer(wx.HORIZONTAL)
        assign_row.Add(wx.StaticText(card, label="Assign to:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        combo = wx.ComboBox(card, choices=self._assignable_ref_choices(), style=wx.CB_DROPDOWN)
        combo.SetHint("type a reference, e.g. R4")
        assign_row.Add(combo, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        btn_assign = wx.Button(card, label="Assign")
        btn_assign.Bind(wx.EVT_BUTTON, lambda e, c=candidate, cb=combo: self._on_assign_candidate(c, cb))
        assign_row.Add(btn_assign, 0)
        cs.Add(assign_row, 0, wx.EXPAND | wx.ALL, 8)

        card.SetSizer(cs)
        self._search_results_sizer.Add(card, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

    def _open_product_page(self, candidate):
        part = candidate.get("part_number", "")
        if not part:
            return self._set_status("No part number to open.", wx.RED)
        supplier = (candidate.get("_traces_supplier") or "").strip().upper()
        if not supplier and hasattr(self, "_search_supplier"):
            supplier = self._search_supplier.GetStringSelection().strip().upper()
        url = self._supplier_product_url(supplier, part)
        if url:
            webbrowser.open(url)
        else:
            self._set_status("Unknown supplier for product page.", wx.RED)

    def _supplier_product_url(self, supplier, part):
        supplier = (supplier or "").strip().upper()
        part = (part or "").strip()
        if not part:
            return ""
        quoted = quote_plus(part)
        if supplier in {"LCSC / JLCPCB", "LCSC"}:
            return f"https://www.lcsc.com/product-detail/{quoted}.html"
        if supplier in {"DIGIKEY", "DIGI-KEY"}:
            return f"https://www.digikey.com/en/products?keywords={quoted}"
        if supplier == "MOUSER":
            return f"https://www.mouser.com/c/?q={quoted}"
        return ""

    def _row_candidate(self, row):
        supplier = self._normalize_supplier_label(row.get("supplier", ""))
        return {
            "part_number": self._part_number_for_row(row),
            "datasheet_url": row.get("datasheet", ""),
            "_traces_supplier": supplier,
        }

    def _open_row_product_page(self, row):
        candidate = self._row_candidate(row)
        if not candidate.get("part_number"):
            return self._set_status("No supplier part number to open.", wx.RED)
        self._open_product_page(candidate)

    def _open_row_datasheet(self, row):
        url = row.get("datasheet", "")
        if not url:
            return self._set_status("No datasheet link on this part.", wx.RED)
        webbrowser.open(url)

    def _open_candidate_datasheet(self, candidate):
        self._on_search_open_datasheet_for(candidate, candidate.get("datasheet_url", ""))

    def _on_search_use_candidate(self, candidate):
        self._search_result = candidate
        part = candidate.get("part_number", "")
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(part))
            wx.TheClipboard.Close()
        supplier_upper = self._search_supplier.GetStringSelection().strip().upper()
        candidate["_traces_supplier"] = self._normalize_supplier_label(supplier_upper)
        self._search_status.SetLabel(f"Selected {part}. Use Assign to fill an existing component, or Add part from the main screen.")

    def _on_search_add_candidate(self, candidate):
        supplier_upper = candidate.get("_traces_supplier") or self._search_supplier.GetStringSelection().strip()
        self._search_status.SetLabel(f"Adding {candidate.get('part_number', '')}...")
        t = threading.Thread(
            target=self._place_candidate_as_new_part,
            args=(candidate, supplier_upper, self._search_status.SetLabel),
            daemon=True,
        )
        t.start()

    def _on_search_copy_mpn(self, event):
        if not self._search_result:
            return
        part = self._search_result.get("part_number", "")
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(part))
            wx.TheClipboard.Close()
        self._search_status.SetLabel(f"Copied: {part}")

    def _on_search_open_datasheet(self, event):
        if self._search_result:
            self._on_search_open_datasheet_for(self._search_result, self._search_result.get("datasheet_url", ""))

    def _on_search_open_datasheet_for(self, candidate, datasheet_url):
        if datasheet_url:
            webbrowser.open(datasheet_url)
            return
        part = candidate.get("part_number", "")
        supplier = (candidate.get("_traces_supplier") or "").strip().upper()
        if not supplier and hasattr(self, "_search_supplier"):
            supplier = self._search_supplier.GetStringSelection().strip().upper()
        if supplier in {"LCSC / JLCPCB", "LCSC"} and part:
            webbrowser.open(f"https://www.lcsc.com/product-detail/{part}.html")
        elif supplier in {"DIGIKEY", "DIGI-KEY"} and part:
            webbrowser.open(self._supplier_product_url(supplier, part))
        elif supplier == "MOUSER" and part:
            webbrowser.open(self._supplier_product_url(supplier, part))
        elif hasattr(self, "_search_status"):
            self._search_status.SetLabel("No datasheet URL available.")
        else:
            self._set_status("No datasheet URL available.", wx.RED)

    def _on_search_get_symbol(self, event):
        if self._search_result:
            self._on_search_get_symbol_for(self._search_result)

    def _on_search_get_symbol_for(self, candidate):
        supplier_str = self._search_supplier.GetStringSelection().strip().upper()
        part = candidate.get("part_number", "")
        if supplier_str in {"LCSC / JLCPCB", "LCSC"}:
            wx.MessageBox(
                "Symbol download for LCSC parts:\n\n"
                "Install easyeda2kicad and run:\n"
                f"  easyeda2kicad --full --lcsc_id {part}\n\n"
                "This will download the KiCad symbol/footprint to your library.",
                "Get Symbol (LCSC)", wx.OK | wx.ICON_INFORMATION, self,
            )
        elif supplier_str in {"DIGIKEY", "DIGI-KEY"}:
            webbrowser.open(self._supplier_product_url(supplier_str, part))
        else:
            webbrowser.open(self._supplier_product_url(supplier_str, part))

    def _apply_candidate_to_refs(self, candidate, refs, supplier_str=None):
        """Write the chosen search candidate onto the given schematic references."""
        if supplier_str is None:
            supplier_str = candidate.get("_traces_supplier", "")
        if not supplier_str and hasattr(self, "_search_supplier"):
            supplier_str = self._search_supplier.GetStringSelection().strip()
        supplier_str = self._normalize_supplier_label(supplier_str)
        supplier_key = supplier_str.upper()
        part = candidate.get("part_number", "")
        datasheet = candidate.get("datasheet_url", "")
        qty = candidate.get("qty")
        price = candidate.get("price")
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        local_datasheet = self._download_candidate_datasheet(candidate, part)
        for ref in refs:
            self.sch.set_property(ref, "Supplier", supplier_str)
            if supplier_key == "LCSC":
                self._set_property_preserving_key(ref, "LCSC", part)
            elif supplier_key in {"DIGIKEY", "DIGI-KEY"}:
                self._set_property_preserving_key(ref, "Digikey Part Number", part)
            else:
                self._set_property_preserving_key(ref, "Mouser Part Number", part)
            self._set_property_preserving_key(ref, QUANTITY_FIELD, str(qty) if qty is not None else "")
            self._set_property_preserving_key(ref, PRICE_FIELD, "" if price is None else str(price))
            if datasheet:
                self._set_property_preserving_key(ref, DATASHEET_URL_FIELD, datasheet)
            if local_datasheet:
                self._set_property_preserving_key(ref, DATASHEET_LOCAL_FIELD, local_datasheet)
            self._set_property_preserving_key(ref, TIMESTAMP_FIELD, timestamp)
        self.dirty = True
        return self._autosave()

    def _download_candidate_datasheet(self, candidate, part, status_setter=None):
        url = candidate.get("datasheet_url", "")
        if not url:
            return ""
        datasheets_dir = Path(self.sch_path).parent / "datasheets"
        datasheets_dir.mkdir(exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", part or candidate.get("part_number", "") or "datasheet")
        dest = datasheets_dir / f"{safe_name}.pdf"
        try:
            self._download_pdf(url, dest)
            return str(dest)
        except Exception as exc:
            msg = f"Applied part data, but datasheet download failed: {exc}"
            if status_setter:
                wx.CallAfter(status_setter, msg)
            else:
                self._set_status(msg, wx.RED)
            return ""

    def _assignable_ref_choices(self):
        choices = []
        for ref in self.sch.get_references():
            if ref.startswith("#"):
                continue
            value = self.sch.get_property(ref, "Value") or ""
            choices.append(f"{ref}  ·  {value}" if value else ref)
        return sorted(choices)

    def _on_assign_candidate(self, candidate, combo):
        choice = combo.GetValue().strip()
        ref = choice.split()[0] if choice else ""
        if ref not in set(self.sch.get_references()):
            return self._search_status.SetLabel(f"Pick a component to assign to (got '{choice}').")
        if not self._apply_candidate_to_refs(candidate, [ref]):
            return
        self._populate_components(ref)
        row = self._row_for_ref(ref)
        if row:
            self._load_detail_for_row(row)
        part = candidate.get("part_number", "")
        self._search_status.SetLabel(f"Assigned {part} → {ref}.")

    def _on_search_apply_to_selected(self, event):
        if not self._search_result:
            return self._set_status("Click 'Use this' on a result first.", wx.RED)
        row = self._current_row()
        if not row:
            return self._set_status("Select a component first.", wx.RED)
        if not self._apply_candidate_to_refs(self._search_result, row["refs"]):
            return
        self._populate_components(row["refs"][0])
        self._load_detail_for_row(row)
        part = self._search_result.get("part_number", "")
        self._set_status(f"Applied {part} to {len(row['refs'])} part(s).")

    # ------------------------------------------------------------------
    # Symbol / footprint installation (EasyEDA → KiCad)
    # ------------------------------------------------------------------

    @staticmethod
    def _version_dirs(base):
        """Return subdirs of base whose names look like version numbers, sorted newest-first."""
        version_re = re.compile(r'^\d+(\.\d+)*$')
        def _ver(d):
            try: return tuple(int(p) for p in d.name.split('.'))
            except ValueError: return (0,)
        dirs = [d for d in base.iterdir() if d.is_dir() and version_re.match(d.name)] if base.exists() else []
        return sorted(dirs, key=_ver, reverse=True)

    def _kicad_user_path(self):
        """Data dir — where 3rdparty symbol/footprint files are stored."""
        import platform
        system = platform.system()
        if system == "Darwin":
            base = Path.home() / "Documents" / "KiCad"
        elif system == "Windows":
            base = Path(os.environ.get("APPDATA", str(Path.home()))) / "kicad"
        else:
            base = Path.home() / ".local" / "share" / "kicad"
        dirs = self._version_dirs(base)
        return dirs[0] if dirs else None

    def _kicad_config_path(self):
        """Config dir — where fp-lib-table and sym-lib-table live."""
        import platform
        system = platform.system()
        if system == "Darwin":
            base = Path.home() / "Library" / "Preferences" / "kicad"
        elif system == "Windows":
            base = Path(os.environ.get("APPDATA", str(Path.home()))) / "kicad"
        else:
            base = Path.home() / ".config" / "kicad"
        dirs = self._version_dirs(base)
        return dirs[0] if dirs else None

    def _lookup_lcsc_for_mpn(self, mpn):
        """Search JLCPCB for an MPN and return the first LCSC C-number, or None."""
        body = json.dumps({
            "currentPage": 1, "pageSize": 5,
            "keyword": mpn, "searchType": 1,
        }).encode("utf-8")
        req = Request(
            "https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList/v2",
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None
        components = data.get("data", {}).get("componentPageInfo", {}).get("list", [])
        if not components:
            return None
        lcsc_id = str(components[0].get("componentCode") or "")
        return lcsc_id if re.match(r'^C\d+$', lcsc_id, re.IGNORECASE) else None

    def _fetch_symbol_data(self, lcsc_id):
        url = f"{LIBRARY_SYMBOL_URL}/{lcsc_id}"
        with urlopen(self._api_request(url), timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _merge_kicad_sym(self, sym_lib_path, new_content, symbol_name):
        if not sym_lib_path.exists():
            sym_lib_path.write_text(new_content, encoding="utf-8")
            return
        existing = _parse(sym_lib_path.read_text(encoding="utf-8"))
        incoming = _parse(new_content)
        new_sym = next(
            (n for n in incoming
             if isinstance(n, list) and n[0] == 'symbol'
             and len(n) > 1 and isinstance(n[1], str)
             and n[1].strip('"') == symbol_name),
            None,
        )
        if new_sym is None:
            return
        existing[:] = [
            n for n in existing
            if not (isinstance(n, list) and n[0] == 'symbol'
                    and len(n) > 1 and isinstance(n[1], str)
                    and n[1].strip('"') == symbol_name)
        ]
        existing.append(new_sym)
        sym_lib_path.write_text(_serialize(existing, 0) + '\n', encoding="utf-8")

    def _ensure_sym_lib_table(self, lib_dir, lib_name):
        kicad_path = self._kicad_config_path()
        if not kicad_path:
            return
        table_path = kicad_path / "sym-lib-table"
        sym_path = str(lib_dir / f"{lib_name}.kicad_sym")
        if table_path.exists():
            tree = _parse(table_path.read_text(encoding="utf-8"))
            for node in tree:
                if isinstance(node, list) and node[0] == 'lib':
                    for child in node:
                        if (isinstance(child, list) and child[0] == 'name'
                                and child[1].strip('"') == lib_name):
                            return
        else:
            tree = ['sym_lib_table']
        tree.append(['lib',
            ['name', f'"{lib_name}"'],
            ['type', '"KiCad"'],
            ['uri', f'"{sym_path}"'],
            ['options', '""'],
            ['descr', '"EasyEDA / LCSC components"'],
        ])
        table_path.write_text(_serialize(tree, 0) + '\n', encoding="utf-8")

    def _ensure_fp_lib_table(self, lib_dir, lib_name):
        kicad_path = self._kicad_config_path()
        if not kicad_path:
            return
        table_path = kicad_path / "fp-lib-table"
        fp_path = str(lib_dir / f"{lib_name}.pretty")
        if table_path.exists():
            tree = _parse(table_path.read_text(encoding="utf-8"))
            for node in tree:
                if isinstance(node, list) and node[0] == 'lib':
                    for child in node:
                        if (isinstance(child, list) and child[0] == 'name'
                                and child[1].strip('"') == lib_name):
                            return
        else:
            tree = ['fp_lib_table']
        tree.append(['lib',
            ['name', f'"{lib_name}"'],
            ['type', '"KiCad"'],
            ['uri', f'"{fp_path}"'],
            ['options', '""'],
            ['descr', '"EasyEDA / LCSC footprints"'],
        ])
        table_path.write_text(_serialize(tree, 0) + '\n', encoding="utf-8")

    def _place_candidate_as_new_part(self, candidate, supplier_upper, status_setter=None):
        """Install the candidate symbol and place one new schematic part."""
        status_setter = status_setter or self._set_status
        part = candidate.get("part_number", "")
        supplier_upper = self._normalize_supplier_label(supplier_upper).upper()

        # Step 1: resolve LCSC C-number
        if supplier_upper == "LCSC":
            lcsc_id = part.upper()
            if not re.match(r'^C\d+$', lcsc_id):
                wx.CallAfter(status_setter,
                             f"{part!r} is not a valid LCSC C-number — symbol not installed.")
                return
        else:
            wx.CallAfter(status_setter, f"Looking up LCSC ID for {part}...")
            lcsc_id = self._lookup_lcsc_for_mpn(part)
            if not lcsc_id:
                wx.CallAfter(status_setter,
                             f"No LCSC equivalent found for {part!r} — symbol not installed.")
                return

        # Step 2: fetch symbol/footprint content from backend
        wx.CallAfter(status_setter, f"Downloading symbol for {lcsc_id}...")
        try:
            data = self._fetch_symbol_data(lcsc_id)
        except HTTPError as e:
            msg = f"Symbol fetch failed ({e.code})"
            try:
                msg += ": " + json.loads(e.read().decode()).get("detail", "")
            except Exception:
                pass
            wx.CallAfter(status_setter, msg)
            return
        except Exception as e:
            wx.CallAfter(status_setter, f"Symbol fetch failed: {e}")
            return

        sym_name = data["symbol_name"]
        fp_name  = data.get("footprint_name", "")
        lib_name = data["lib_name"]
        lib_id_str = f"{lib_name}:{sym_name}"
        fp_ref     = f"{lib_name}:{fp_name}" if fp_name else ""

        # Step 3: write files to KiCad library dir
        wx.CallAfter(status_setter, f"Installing {sym_name}...")
        try:
            kicad_path = self._kicad_user_path()
            if kicad_path is None:
                raise RuntimeError("KiCad user path not found (~/Documents/KiCad/ missing)")
            lib_dir = kicad_path / "3rdparty" / "traces"
            lib_dir.mkdir(parents=True, exist_ok=True)
            self._merge_kicad_sym(lib_dir / f"{lib_name}.kicad_sym", data["symbol_content"], sym_name)
            if fp_name and data.get("footprint_content"):
                fp_dir = lib_dir / f"{lib_name}.pretty"
                fp_dir.mkdir(exist_ok=True)
                (fp_dir / f"{fp_name}.kicad_mod").write_text(data["footprint_content"], encoding="utf-8")
            self._ensure_sym_lib_table(lib_dir, lib_name)
            if fp_name:
                self._ensure_fp_lib_table(lib_dir, lib_name)
        except Exception as e:
            wx.CallAfter(status_setter, f"Install failed: {e}")
            return

        # Build sourcing property set from candidate + lcsc_id
        price = candidate.get("price")
        qty   = candidate.get("qty")
        datasheet_url = candidate.get("datasheet_url", "")
        local_datasheet = self._download_candidate_datasheet(candidate, part, status_setter)
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")

        # Map supplier string back to the key used for the part-number field
        if supplier_upper == "LCSC":
            supplier_label = "LCSC"
            part_field     = "LCSC"
            part_value     = lcsc_id
        elif supplier_upper in {"DIGIKEY", "DIGI-KEY"}:
            supplier_label = "DigiKey"
            part_field     = "Digikey Part Number"
            part_value     = part
        else:
            supplier_label = "Mouser"
            part_field     = "Mouser Part Number"
            part_value     = part

        # Schematic edits run on the main thread so wxPython and self.sch are safe.
        def _apply_on_main():
            # Embed symbol definition into lib_symbols so KiCad can render it
            # without needing the external library file loaded yet.
            self.sch.embed_lib_symbol(lib_name, sym_name, data["symbol_content"])

            cx, cy = self.sch.get_center()
            cx = round(cx / 1.27) * 1.27
            cy = round(cy / 1.27) * 1.27
            extra = {
                "Supplier": supplier_label,
                part_field: part_value,
                QUANTITY_FIELD: str(qty) if qty is not None else "",
                PRICE_FIELD: "" if price is None else str(price),
                TIMESTAMP_FIELD: timestamp,
            }
            if local_datasheet:
                extra[DATASHEET_LOCAL_FIELD] = local_datasheet
            if supplier_upper != "LCSC":
                extra["LCSC"] = lcsc_id
            prefix = self.sch.symbol_ref_prefix(data["symbol_content"], sym_name)
            new_ref = self.sch.next_reference(prefix)
            self.sch.place_symbol(lib_id_str, cx, cy, sym_name, fp_ref,
                                  reference=new_ref,
                                  datasheet=datasheet_url, extra_props=extra)
            self.dirty = True

            if self._autosave():
                self._populate_components(new_ref)
                status_setter(
                    f"Installed {sym_name} - placed at center. "
                    "Reload schematic (File → Revert) to see symbol; "
                    "restart KiCad to pick up footprint library.")

        wx.CallAfter(_apply_on_main)

    def _install_and_apply_symbol(self, candidate, supplier_upper):
        """Backward-compatible wrapper for old search-tab callers."""
        return self._place_candidate_as_new_part(candidate, supplier_upper)

    # ------------------------------------------------------------------
    # SRC tab
    # ------------------------------------------------------------------

    def _on_src_run_all(self, event):
        for category in SRC_CHECKS:
            self._run_src(category)

    def _api_request(self, url: str, data: bytes = None, method: str = None) -> Request:
        headers = {}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.auth:
            headers.update(self.auth.auth_headers())
        return Request(url, data=data, headers=headers,
                       method=method or ("POST" if data is not None else "GET"))

    def _handle_http_error(self, exc: HTTPError) -> bool:
        if exc.code == 422:
            try:
                body = json.loads(exc.read().decode())
                detail = body.get("detail", [])
                if isinstance(detail, list):
                    msgs = "; ".join(d.get("msg", str(d)) for d in detail)
                else:
                    msgs = str(detail)
            except Exception:
                msgs = str(exc)
            self._set_status(f"Validation error: {msgs}", wx.RED)
            return True
        if exc.code == 401:
            self._set_status("Session expired — please restart KiCad to sign in again.", wx.RED)
            if self.auth:
                self.auth.clear()
            return True
        return False

    def _submit_jlc_job(self, *, lcsc="", mpn="", footprint=""):
        body = json.dumps({"lcsc": lcsc, "mpn": mpn, "footprint": footprint}).encode("utf-8")
        with urlopen(self._api_request(JLCPCB_SOURCE_URL, body), timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))["job_id"]

    def _submit_digikey_job(self, *, description="", mpn="", footprint=""):
        payload = {"footprint": footprint}
        if description:
            payload["description"] = description
        if mpn:
            payload["mpn"] = mpn
        body = json.dumps(payload).encode("utf-8")
        with urlopen(self._api_request(DIGIKEY_SOURCE_URL, body), timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))["job_id"]

    def _submit_digikey_datasheet_job(self, part_number):
        body = json.dumps({"part_number": part_number}).encode("utf-8")
        with urlopen(self._api_request(DIGIKEY_DATASHEET_URL, body), timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))["job_id"]

    def _submit_mouser_datasheet_job(self, part_number):
        body = json.dumps({"part_number": part_number}).encode("utf-8")
        with urlopen(self._api_request(MOUSER_DATASHEET_URL, body), timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))["job_id"]

    def _submit_mouser_job(self, *, description="", mpn="", footprint=""):
        payload = {"footprint": footprint}
        if description:
            payload["description"] = description
        if mpn:
            payload["mpn"] = mpn
        body = json.dumps(payload).encode("utf-8")
        with urlopen(self._api_request(MOUSER_SOURCE_URL, body), timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))["job_id"]

    def _poll_job(self, job_id, label, status_setter=None):
        status_setter = status_setter or self._set_status
        status_url = f"{BACKEND_URL}/jobs/{job_id}"
        phase = 0
        for _ in range(90):
            with urlopen(self._api_request(status_url), timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))
            status = data.get("status")
            if status == "complete":
                return data.get("result") or {}
            if status == "failed":
                self._show_backend_error(f"{label} job failed on the server.", json.dumps(data, indent=2))
                raise RuntimeError(f"{label} job failed")
            if status == "not_found":
                self._show_backend_error(f"{label} job not found.", f"Job ID: {job_id}")
                raise RuntimeError(f"{label} job not found")
            # Animate plain-text dots while the job is in progress (~1s/poll).
            for _ in range(3):
                phase = phase % 3 + 1
                status_setter(f"{label} running{'.' * phase}")
                wx.YieldIfNeeded()
                time.sleep(0.34)
        self._show_backend_error(f"{label} lookup timed out.", f"Job ID: {job_id}")
        raise TimeoutError(f"{label} lookup timed out")

    def _poll_jlc_job(self, job_id):
        return self._poll_job(job_id, "JLC")

    def _poll_digikey_job(self, job_id):
        return self._poll_job(job_id, "Digi-Key")

    def _poll_mouser_job(self, job_id):
        return self._poll_job(job_id, "Mouser")

    def _export_kicad_xml_netlist(self):
        cli = shutil.which("kicad-cli")
        if not cli:
            for candidate in [
                "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
                "/usr/local/bin/kicad-cli",
                "/usr/bin/kicad-cli",
            ]:
                if Path(candidate).exists():
                    cli = candidate
                    break
        if not cli:
            raise RuntimeError("kicad-cli not found. Is KiCad installed and in your PATH?")

        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as output:
            out_path = output.name
        try:
            subprocess.run(
                [cli, "sch", "export", "netlist", "--output", out_path, self.sch_path],
                check=True,
                capture_output=True,
                timeout=30,
            )
            return Path(out_path).read_text(encoding="utf-8")
        finally:
            Path(out_path).unlink(missing_ok=True)

    def _submit_src_job(self, category, xml):
        body = json.dumps({"xml": xml}).encode("utf-8")
        with urlopen(self._api_request(SRC_CHECKS[category], body), timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))["job_id"]

    def _set_property_preserving_key(self, ref, preferred_key, value):
        props = self.sch.get_properties(ref)
        key = preferred_key
        for existing_key in props:
            if existing_key.lower() == preferred_key.lower():
                key = existing_key
                break
        self.sch.set_property(ref, key, value)

    def _write_jlc_result(self, ref, result):
        return self._write_jlc_result_to_refs([ref], result)

    def _write_jlc_result_to_refs(self, refs, result, row=None):
        row = row or self._row_for_ref(refs[0])
        qty = result.get("qty", 0)
        price = result.get("price", None)
        part_number = result.get("part_number", "")
        if not part_number:
            self._set_row_call_status(row, "No JLC match")
            return False

        datasheet_url = result.get("datasheet_url", "")
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        for ref in refs:
            self._set_property_preserving_key(ref, "LCSC", part_number)
            self._set_property_preserving_key(ref, QUANTITY_FIELD, str(qty))
            self._set_property_preserving_key(ref, PRICE_FIELD, "" if price is None else str(price))
            if datasheet_url:
                self._set_property_preserving_key(ref, DATASHEET_URL_FIELD, datasheet_url)
            self._set_property_preserving_key(ref, TIMESTAMP_FIELD, timestamp)
        if not self._autosave():
            self._set_row_call_status(row, "Updated, save failed")
            self._populate_components(refs[0])
            return True

        self._set_row_call_status(row, f"Updated JLC {part_number}")
        self._populate_components(refs[0])
        self._show_detail_result("LCSC", result)
        self._set_status(
            f"Updated and saved {len(refs)} part(s): LCSC={part_number}, {QUANTITY_FIELD}={qty}, {PRICE_FIELD}={price}."
        )
        return True

    def _write_digikey_result(self, ref, result):
        return self._write_digikey_result_to_refs([ref], result)

    def _write_digikey_result_to_refs(self, refs, result, row=None):
        row = row or self._row_for_ref(refs[0])
        qty = result.get("qty", 0)
        price = result.get("price", None)
        part_number = result.get("part_number", "")
        if not part_number:
            self._set_row_call_status(row, "No Digi-Key match")
            return False

        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        for ref in refs:
            self._set_property_preserving_key(ref, "Digikey Part Number", part_number)
            self._set_property_preserving_key(ref, QUANTITY_FIELD, str(qty))
            self._set_property_preserving_key(ref, PRICE_FIELD, "" if price is None else str(price))
            self._set_property_preserving_key(ref, TIMESTAMP_FIELD, timestamp)
        if not self._autosave():
            self._set_row_call_status(row, "Updated, save failed")
            self._populate_components(refs[0])
            return True

        self._set_row_call_status(row, f"Updated Digi-Key {part_number}")
        self._populate_components(refs[0])
        self._show_detail_result("DigiKey", result)
        self._set_status(
            f"Updated and saved {len(refs)} part(s): Digikey Part Number={part_number}, {QUANTITY_FIELD}={qty}, {PRICE_FIELD}={price}."
        )
        return True

    def _write_mouser_result(self, ref, result):
        return self._write_mouser_result_to_refs([ref], result)

    def _write_mouser_result_to_refs(self, refs, result, row=None):
        row = row or self._row_for_ref(refs[0])
        qty = result.get("qty", 0)
        price = result.get("price", None)
        part_number = result.get("part_number", "")
        if not part_number:
            self._set_row_call_status(row, "No Mouser match")
            return False

        datasheet_url = result.get("datasheet_url", "")
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        for ref in refs:
            self._set_property_preserving_key(ref, "Mouser Part Number", part_number)
            self._set_property_preserving_key(ref, QUANTITY_FIELD, str(qty))
            self._set_property_preserving_key(ref, PRICE_FIELD, "" if price is None else str(price))
            if datasheet_url:
                self._set_property_preserving_key(ref, DATASHEET_URL_FIELD, datasheet_url)
            self._set_property_preserving_key(ref, TIMESTAMP_FIELD, timestamp)
        if not self._autosave():
            self._set_row_call_status(row, "Updated, save failed")
            self._populate_components(refs[0])
            return True

        self._set_row_call_status(row, f"Updated Mouser {part_number}")
        self._populate_components(refs[0])
        self._show_detail_result("Mouser", result)
        self._set_status(
            f"Updated and saved {len(refs)} part(s): Mouser Part Number={part_number}, {QUANTITY_FIELD}={qty}, {PRICE_FIELD}={price}."
        )
        return True

    def _download_pdf(self, url, dest, referer=None):
        opener = build_opener(HTTPSHandler(context=_ssl_ctx))
        opener.addheaders = [
            ("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        ]
        headers = {"Accept": "application/pdf,text/html,*/*"}
        if referer:
            headers["Referer"] = referer
        with opener.open(Request(url, headers=headers), timeout=30) as resp:
            content_type = resp.headers.get("Content-Type", "")
            response_bytes = resp.read()
        if response_bytes.startswith(b"%PDF") or "application/pdf" in content_type:
            dest.write_bytes(response_bytes)
            return

        html = response_bytes.decode("utf-8", errors="replace")
        pdf_urls = re.findall(
            r"https://(?:datasheet|wmsc)\.lcsc\.com/[^\\\"']+?\.pdf",
            html,
        )
        pdf_urls.extend(re.findall(r"https://[^\\\"']+?\.pdf", html))
        if not pdf_urls:
            raise RuntimeError("Could not find a PDF URL in the datasheet page")

        pdf_url = pdf_urls[0]
        with opener.open(Request(pdf_url, headers={"Referer": url, "Accept": "application/pdf,*/*"}), timeout=30) as resp:
            pdf_bytes = resp.read()
        if not pdf_bytes.startswith(b"%PDF"):
            raise RuntimeError(f"Datasheet server did not return a PDF ({len(pdf_bytes)} bytes, starts: {pdf_bytes[:40]})")
        dest.write_bytes(pdf_bytes)

    def _on_fetch_jlc(self, event, row=None):
        row = row or self._current_row()
        if not row:
            return self._set_status("Select a component first.", wx.RED)

        if row["supplier"].strip().upper() != "LCSC":
            self._mark_row_call(row, "Supplier is not LCSC")
            return self._set_status(f"{row['reference']} supplier is not LCSC.", wx.RED)
        lcsc = row["lcsc"]
        footprint = row["footprint"]
        if not lcsc:
            self._mark_row_call(row, "Missing LCSC")
            return self._set_status(f"{row['reference']} does not have an LCSC field populated.", wx.RED)

        try:
            self._mark_row_call(row, "Submitting JLC")
            self._set_status(f"Submitting JLC lookup for {row['reference']} ({lcsc})...")
            job_id = self._submit_jlc_job(lcsc=lcsc, footprint=footprint)
            self._mark_row_call(row, "JLC running")
            result = self._poll_jlc_job(job_id)
        except HTTPError as e:
            self._mark_row_call(row, "JLC failed")
            if self._handle_http_error(e):
                return
            self._show_backend_error("JLC lookup failed.", str(e))
            return
        except (URLError, TimeoutError, RuntimeError, KeyError, json.JSONDecodeError) as e:
            self._mark_row_call(row, "JLC failed")
            self._show_backend_error("JLC lookup failed.", str(e))
            return

        if not result.get("part_number", ""):
            self._mark_row_call(row, "No JLC match")
            return self._set_status(f"No credible JLC match found for {lcsc}.", wx.RED)
        self._write_jlc_result_to_refs(row["refs"], result, row=row)
        self._set_status(f"Updated {len(row['refs'])} part(s) from JLC: {lcsc}.")

    def _on_find_jlc_from_value(self, event, row=None):
        row = row or self._current_row()
        if not row:
            return self._set_status("Select a component first.", wx.RED)

        if row["supplier"].strip().upper() != "LCSC":
            self._mark_row_call(row, "Supplier is not LCSC")
            return self._set_status(f"{row['reference']} supplier is not LCSC.", wx.RED)
        value = row["value"].strip()
        footprint = row["footprint"].strip()
        if not value:
            self._mark_row_call(row, "Missing Value")
            return self._set_status(f"{row['reference']} does not have a Value populated.", wx.RED)
        if not footprint:
            self._mark_row_call(row, "Missing Footprint")
            return self._set_status(f"{row['reference']} does not have a Footprint populated.", wx.RED)

        try:
            self._mark_row_call(row, "Submitting JLC")
            self._set_status(f"Finding JLC part for {row['reference']} from Value={value}, Footprint={footprint}...")
            job_id = self._submit_jlc_job(mpn=value, footprint=footprint)
            self._mark_row_call(row, "JLC running")
            result = self._poll_jlc_job(job_id)
        except HTTPError as e:
            self._mark_row_call(row, "JLC failed")
            if self._handle_http_error(e):
                return
            self._show_backend_error("JLC lookup failed.", str(e))
            return
        except (URLError, TimeoutError, RuntimeError, KeyError, json.JSONDecodeError) as e:
            self._mark_row_call(row, "JLC failed")
            self._show_backend_error("JLC lookup failed.", str(e))
            return

        if not result.get("part_number", ""):
            self._mark_row_call(row, "No JLC match")
            return self._set_status(f"No credible JLC match found for {value} / {footprint}.", wx.RED)
        self._write_jlc_result_to_refs(row["refs"], result, row=row)
        self._set_status(f"Updated {len(row['refs'])} part(s) from JLC: {result.get('part_number', '')}.")

    def _on_find_digikey_from_value(self, event, row=None):
        row = row or self._current_row()
        if not row:
            return self._set_status("Select a component first.", wx.RED)

        if row["supplier"].strip().upper() not in {"DIGIKEY", "DIGI-KEY"}:
            self._mark_row_call(row, "Supplier is not Digi-Key")
            return self._set_status(f"{row['reference']} supplier is not Digi-Key.", wx.RED)

        value = row["value"].strip()
        footprint = row["footprint"].strip()
        digikey_part = "" if row["digikey"] == "Mixed" else row["digikey"]
        if not value and not digikey_part:
            self._mark_row_call(row, "Missing Value/part")
            return self._set_status(f"{row['reference']} does not have a Value or Digikey Part Number populated.", wx.RED)
        if not footprint:
            self._mark_row_call(row, "Missing Footprint")
            return self._set_status(f"{row['reference']} does not have a Footprint populated.", wx.RED)

        try:
            if digikey_part:
                self._mark_row_call(row, "Submitting Digi-Key")
                self._set_status(f"Updating Digi-Key availability for {row['reference']} ({digikey_part})...")
                job_id = self._submit_digikey_job(mpn=digikey_part, footprint=footprint)
            else:
                self._mark_row_call(row, "Submitting Digi-Key")
                self._set_status(f"Finding Digi-Key part for {row['reference']} from Value={value}, Footprint={footprint}...")
                job_id = self._submit_digikey_job(description=value, footprint=footprint)
            self._mark_row_call(row, "Digi-Key running")
            result = self._poll_digikey_job(job_id)
        except HTTPError as e:
            self._mark_row_call(row, "Digi-Key failed")
            if self._handle_http_error(e):
                return
            self._show_backend_error("Digi-Key lookup failed.", str(e))
            return
        except (URLError, TimeoutError, RuntimeError, KeyError, json.JSONDecodeError) as e:
            self._mark_row_call(row, "Digi-Key failed")
            self._show_backend_error("Digi-Key lookup failed.", str(e))
            return

        search_label = digikey_part or value
        if not result.get("part_number", ""):
            self._mark_row_call(row, "No Digi-Key match")
            return self._set_status(f"No credible Digi-Key match found for {search_label} / {footprint}.", wx.RED)
        self._write_digikey_result_to_refs(row["refs"], result, row=row)
        self._set_status(f"Updated {len(row['refs'])} part(s) from Digi-Key: {result.get('part_number', '')}.")

    def _on_find_mouser_from_value(self, event, row=None):
        row = row or self._current_row()
        if not row:
            return self._set_status("Select a component first.", wx.RED)

        if row["supplier"].strip().upper() != "MOUSER":
            self._mark_row_call(row, "Supplier is not Mouser")
            return self._set_status(f"{row['reference']} supplier is not Mouser.", wx.RED)

        value = row["value"].strip()
        footprint = row["footprint"].strip()
        mouser_part = "" if row["mouser"] == "Mixed" else row["mouser"]
        if not value and not mouser_part:
            self._mark_row_call(row, "Missing Value/part")
            return self._set_status(f"{row['reference']} does not have a Value or Mouser Part Number populated.", wx.RED)
        if not footprint:
            self._mark_row_call(row, "Missing Footprint")
            return self._set_status(f"{row['reference']} does not have a Footprint populated.", wx.RED)

        try:
            if mouser_part:
                self._mark_row_call(row, "Submitting Mouser")
                self._set_status(f"Updating Mouser availability for {row['reference']} ({mouser_part})...")
                job_id = self._submit_mouser_job(mpn=mouser_part, footprint=footprint)
            else:
                self._mark_row_call(row, "Submitting Mouser")
                self._set_status(f"Finding Mouser part for {row['reference']} from Value={value}, Footprint={footprint}...")
                job_id = self._submit_mouser_job(description=value, footprint=footprint)
            self._mark_row_call(row, "Mouser running")
            result = self._poll_mouser_job(job_id)
        except HTTPError as e:
            self._mark_row_call(row, "Mouser failed")
            if self._handle_http_error(e):
                return
            self._show_backend_error("Mouser lookup failed.", str(e))
            return
        except (URLError, TimeoutError, RuntimeError, KeyError, json.JSONDecodeError) as e:
            self._mark_row_call(row, "Mouser failed")
            self._show_backend_error("Mouser lookup failed.", str(e))
            return

        search_label = mouser_part or value
        if not result.get("part_number", ""):
            self._mark_row_call(row, "No Mouser match")
            return self._set_status(f"No credible Mouser match found for {search_label} / {footprint}.", wx.RED)
        self._write_mouser_result_to_refs(row["refs"], result, row=row)
        self._set_status(f"Updated {len(row['refs'])} part(s) from Mouser: {result.get('part_number', '')}.")

    def _on_download_datasheet(self, event, row=None):
        row = row or self._current_row()
        if not row:
            return self._set_status("Select a component first.", wx.RED)

        url = row.get("datasheet", "")
        lcsc = row["lcsc"]
        if not url or url == "Mixed":
            supplier = row["supplier"].strip().upper()
            if supplier == "LCSC":
                if not lcsc:
                    return self._set_status(f"{row['reference']} does not have an LCSC field populated.", wx.RED)
                try:
                    self._set_status(f"Fetching datasheet URL for {row['reference']} ({lcsc})...")
                    body = json.dumps({"lcsc": lcsc}).encode("utf-8")
                    with urlopen(self._api_request(DATASHEET_FETCH_URL, body), timeout=10) as response:
                        job_id = json.loads(response.read().decode("utf-8"))["job_id"]
                    result = self._poll_jlc_job(job_id)
                except HTTPError as e:
                    if self._handle_http_error(e):
                        return
                    return self._set_status(f"Datasheet fetch failed: {e}", wx.RED)
                except (URLError, TimeoutError, RuntimeError, KeyError, json.JSONDecodeError) as e:
                    return self._set_status(f"Datasheet fetch failed: {e}", wx.RED)
                url = (result or {}).get("url", "")
            elif supplier in {"DIGIKEY", "DIGI-KEY"}:
                digikey_part = "" if row["digikey"] == "Mixed" else row["digikey"]
                if not digikey_part:
                    return self._set_status(f"{row['reference']} does not have a Digi-Key part number.", wx.RED)
                try:
                    self._set_status(f"Fetching Digi-Key datasheet URL for {row['reference']} ({digikey_part})...")
                    job_id = self._submit_digikey_datasheet_job(digikey_part)
                    result = self._poll_digikey_job(job_id)
                except (URLError, TimeoutError, RuntimeError, KeyError, json.JSONDecodeError) as e:
                    return self._set_status(f"Digi-Key datasheet fetch failed: {e}", wx.RED)
                url = (result or {}).get("url", "")
            elif supplier == "MOUSER":
                mouser_part = "" if row["mouser"] == "Mixed" else row["mouser"]
                if not mouser_part:
                    return self._set_status(f"{row['reference']} does not have a Mouser part number.", wx.RED)
                try:
                    self._set_status(f"Fetching Mouser datasheet URL for {row['reference']} ({mouser_part})...")
                    job_id = self._submit_mouser_datasheet_job(mouser_part)
                    result = self._poll_mouser_job(job_id)
                except (URLError, TimeoutError, RuntimeError, KeyError, json.JSONDecodeError) as e:
                    return self._set_status(f"Mouser datasheet fetch failed: {e}", wx.RED)
                url = (result or {}).get("url", "")
            else:
                return self._set_status(f"{row['reference']} does not have a usable Datasheet URL.", wx.RED)
        if not url:
            label = lcsc or row.get("digikey") or row.get("mouser") or row["reference"]
            return self._set_status(f"No datasheet URL returned for {label}.", wx.RED)

        for ref in row["refs"]:
            self.sch.set_property(ref, DATASHEET_URL_FIELD, url)
        if len(row["refs"]) == 1:
            self._load_props(row["refs"][0])
        else:
            self._load_group_props(row)
        self._populate_components(row["refs"][0])
        self._update_supplier_buttons()
        self.dirty = True
        if not self._autosave():
            return

        datasheets_dir = Path(self.sch_path).parent / "datasheets"
        datasheets_dir.mkdir(exist_ok=True)
        safe_name = lcsc or row["refs"][0]
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", safe_name)
        dest = datasheets_dir / f"{safe_name}.pdf"

        try:
            self._set_status(f"Downloading datasheet for {row['reference']}...")
            self._download_pdf(url, dest)
        except Exception as e:
            return self._set_status(f"Download failed: {e}", wx.RED)

        for ref in row["refs"]:
            self.sch.set_property(ref, DATASHEET_LOCAL_FIELD, str(dest))
        if len(row["refs"]) == 1:
            self._load_props(row["refs"][0])
        else:
            self._load_group_props(row)
        self._populate_components(row["refs"][0])
        self._update_supplier_buttons()
        self.dirty = True
        if not self._autosave():
            return

        self._set_status(f"Datasheet saved for {len(row['refs'])} part(s): {dest}")

# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

class SchematicPropertyEditor(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "traces"
        self.category = "Schematic"
        self.description = "Reconcile schematic metadata, suppliers, datasheets, and SRC netlists."
        self.show_toolbar_button = True
        self.icon_file_name = str(Path(__file__).with_name("traces.png"))

    def Run(self):
        board = pcbnew.GetBoard()
        if not board or not board.GetFileName():
            wx.MessageBox(
                "Open this from a saved PCB inside a KiCad project.",
                "traces",
            )
            return

        # Start the local sourcing server if it isn't already up.
        if not _server_alive():
            busy = wx.BusyCursor()
            started = _ensure_server()
            del busy
            if not started:
                wx.MessageBox(
                    "Could not reach or start the local traces server at "
                    f"{BACKEND_URL}.\n\nStart it manually with:\n"
                    "    cd traces/mcp && uv run traces-serve",
                    "traces",
                )

        project_folder = Path(board.GetFileName()).parent
        sch_files = list(project_folder.glob("*.kicad_sch"))

        if not sch_files:
            wx.MessageBox(
                f"No .kicad_sch files found in:\n{project_folder}",
                "traces",
            )
            return

        # If multiple schematics, let the user pick
        if len(sch_files) == 1:
            sch_path = str(sch_files[0])
        else:
            choices = [f.name for f in sch_files]
            dlg = wx.SingleChoiceDialog(
                None,
                "Multiple schematic files found. Pick one:",
                "Select schematic",
                choices,
            )
            if dlg.ShowModal() != wx.ID_OK:
                dlg.Destroy()
                return
            sch_path = str(sch_files[dlg.GetSelection()])
            dlg.Destroy()

        auth = AuthManager()

        try:
            sch = KiCadSchematic(sch_path)
        except Exception as e:
            wx.MessageBox(f"Failed to parse schematic:\n\n{e}", "traces")
            return

        if not sch.get_references():
            wx.MessageBox("No placed components found in schematic.", "traces")
            return

        dialog = PropEditorDialog(sch, sch_path, board, auth=auth)
        dialog.ShowModal()
        dialog.Destroy()


SchematicPropertyEditor().register()
