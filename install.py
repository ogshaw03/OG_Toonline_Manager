"""OG Toonline Manager — installer / hot-updater (single-file tool).

Ships alongside a single module file (``OG_Toonline_Manager.py``). Two ways
to run this from inside Maya:

1) Drag ``install.py`` from your file browser into any Maya viewport.
2) From the Script Editor (Python tab)::

       exec(open(r"C:/path/to/install.py").read())

Either way:

* Fetches ``OG_Toonline_Manager.py`` fresh from GitHub (SHA-pinned URL, so
  no CDN cache staleness) and writes it to your Maya user scripts folder.
* Force-overwrites the existing file (Windows read-only cleared,
  atomic tmp+os.replace).
* Wipes any ``__pycache__/OG_Toonline_Manager.cpython-*.pyc`` so stale
  bytecode doesn't shadow the freshly copied source.
* Flushes ``OG_Toonline_Manager`` from ``sys.modules`` so the next import
  reads from disk — no Maya restart needed.
* Adds / refreshes a shelf button on the active shelf. Left-click
  launches; right-click has an "Update from GitHub" menu that
  re-runs this installer without another drag.

Target: Maya 2022+ (Python 3). See maya-hot-update-patterns.md for the
design notes behind each step (§1-3/§1-4/§1-5/§1-6/§1-7/§1-8/§1-9).
"""

from __future__ import annotations

import os
import re
import sys


# ─── CUSTOMIZE ────────────────────────────────────────────────────────────
_GITHUB_OWNER = "ogshaw03"
_GITHUB_REPO = "OG_Toonline_Manager"
_GITHUB_BRANCH = "claude/repository-handoff-review-k54kwk"

_MODULE = "OG_Toonline_Manager"          # tool's .py filename (without .py)
_SHELF_BUTTON_LABEL = "OGToon"           # short label on the shelf button
# ─── END CUSTOMIZE ────────────────────────────────────────────────────────


_MODULE_FILE = f"{_MODULE}.py"
_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
_GITHUB_API = f"https://api.github.com/repos/{_GITHUB_OWNER}/{_GITHUB_REPO}"
_GITHUB_RAW_BASE = f"https://raw.githubusercontent.com/{_GITHUB_OWNER}/{_GITHUB_REPO}"


# --------------------------------------------------------------------------- #
# Force-overwrite helpers (Windows-safe)  — patterns doc §1-3, §1-4
# --------------------------------------------------------------------------- #

def _force_writable(path: str) -> None:
    import stat
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except Exception:
        pass


def _atomic_write_bytes(target: str, data: bytes) -> None:
    """Overwrite ``target`` atomically. Either the previous complete
    file OR the new complete file exists on disk — no half-written
    garbage on cancel / power loss / disk full."""
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    if os.path.exists(target):
        _force_writable(target)
    tmp = target + ".tmp_install"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except Exception:
            pass
    os.replace(tmp, target)


# --------------------------------------------------------------------------- #
# Module acquisition
# --------------------------------------------------------------------------- #

def _resolve_latest_sha() -> str:
    """SHA-pinned raw URLs are the only reliable cache-buster for
    raw.githubusercontent.com — its CDN caches by path only."""
    import json
    import random
    import time
    from urllib.request import Request, urlopen

    salt = f"{time.time():.6f}_{random.randint(0, 2 ** 32)}"
    req = Request(f"{_GITHUB_API}/branches/{_GITHUB_BRANCH}?_={salt}",
                  headers={
                      "Accept": "application/vnd.github+json",
                      "Cache-Control": "no-cache",
                      "User-Agent": f"{_MODULE}-installer/{salt}",
                  })
    try:
        with urlopen(req, timeout=30) as resp:
            sha = json.loads(resp.read().decode("utf-8"))["commit"]["sha"]
        print(f"[{_MODULE}] resolved {_GITHUB_BRANCH} → {sha[:10]}")
        return sha
    except Exception as exc:
        print(f"[{_MODULE}] SHA lookup failed ({exc}); falling back to "
              f"branch name (may hit CDN cache)")
        return _GITHUB_BRANCH


def _fetch_module(dest_root: str) -> None:
    """Default: always pull from GitHub. Developers who want to
    iterate on the local checkout set ``OG_TOONLINE_MANAGER_USE_LOCAL=1``."""
    from urllib.request import Request, urlopen

    env_flag = f"{_MODULE.upper()}_USE_LOCAL"
    use_local = os.environ.get(env_flag) == "1"
    src_local = os.path.join(_REPO_ROOT, _MODULE_FILE)
    target = os.path.join(dest_root, _MODULE_FILE)

    if use_local and os.path.isfile(src_local):
        print(f"[{_MODULE}] {env_flag}=1 → copying local {src_local}")
        with open(src_local, "rb") as fh:
            data = fh.read()
    else:
        sha = _resolve_latest_sha()
        url = f"{_GITHUB_RAW_BASE}/{sha}/{_MODULE_FILE}"
        print(f"[{_MODULE}] downloading {url}")
        req = Request(url, headers={
            "Cache-Control": "no-cache",
            "User-Agent": f"{_MODULE}-installer/{sha[:10]}",
        })
        try:
            data = urlopen(req, timeout=30).read()
        except Exception as exc:
            raise RuntimeError(f"Failed to download {url}: {exc}")

    _atomic_write_bytes(target, data)
    print(f"[{_MODULE}]   → {target} ({len(data)} bytes)")


# --------------------------------------------------------------------------- #
# Post-install: verify, clean pycache, flush imports, shelf button
# --------------------------------------------------------------------------- #

def _verify_install(dest_root: str) -> None:
    p = os.path.join(dest_root, _MODULE_FILE)
    if not os.path.isfile(p) or os.path.getsize(p) == 0:
        raise RuntimeError(f"Install verification failed — {p} missing/empty")


def _clean_pycache(dest_root: str) -> None:
    """Remove any stale .pyc for this module — patterns doc §1-5."""
    pycache = os.path.join(dest_root, "__pycache__")
    if not os.path.isdir(pycache):
        return
    for name in os.listdir(pycache):
        if name.startswith(f"{_MODULE}.") and name.endswith(".pyc"):
            try:
                _force_writable(os.path.join(pycache, name))
                os.remove(os.path.join(pycache, name))
            except Exception:
                pass


def _flush_imports() -> None:
    """Drop the module from sys.modules — patterns doc §1-6."""
    sys.modules.pop(_MODULE, None)


def _read_installed_version(dest_root: str) -> str:
    p = os.path.join(dest_root, _MODULE_FILE)
    try:
        with open(p, "r", encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r'\s*__version__\s*=\s*[\'"]([^\'"]+)[\'"]', line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return "(unknown)"


def _close_existing_window() -> None:
    """Best-effort: close a Maya cmds window by name. The tool's own
    show() also closes any duplicate PySide window by objectName, so
    this is just a harmless extra guard."""
    try:
        from maya import cmds
        window_name = f"{_MODULE}Win"
        if cmds.window(window_name, exists=True):
            cmds.deleteUI(window_name)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Shelf button (with right-click Update popup — patterns doc §1-8)
# --------------------------------------------------------------------------- #

_SHELF_LAUNCH_CMD = (
    f"# Auto-generated by {_MODULE} install.py\n"
    "import sys\n"
    f"sys.modules.pop({_MODULE!r}, None)\n"
    f"import {_MODULE} as _t; _t.show()\n"
)

_SHELF_UPDATE_CMD = (
    f"# Auto-generated by {_MODULE} install.py\n"
    "import json, urllib.request\n"
    f"_api = 'https://api.github.com/repos/{_GITHUB_OWNER}/{_GITHUB_REPO}"
    f"/branches/{_GITHUB_BRANCH}'\n"
    "_sha = json.loads(urllib.request.urlopen(_api, timeout=30)"
    ".read())['commit']['sha']\n"
    f"_u = 'https://raw.githubusercontent.com/{_GITHUB_OWNER}/{_GITHUB_REPO}"
    "/' + _sha + '/install.py'\n"
    f"print('[{_MODULE}] update via SHA', _sha[:10])\n"
    "exec(compile(urllib.request.urlopen(_u, timeout=30).read(),\n"
    "             'install.py (from GitHub)', 'exec'),\n"
    "     {'__name__': 'install', '__file__': '<github>'})\n"
)


def _add_shelf_button() -> None:
    from maya import cmds, mel

    top_shelf = mel.eval("$tmp = $gShelfTopLevel")
    if not top_shelf or not cmds.tabLayout(top_shelf, exists=True):
        return
    current = cmds.tabLayout(top_shelf, q=True, selectTab=True)
    if not current:
        return

    for child in cmds.shelfLayout(current, q=True, ca=True) or []:
        try:
            if cmds.shelfButton(child, q=True, label=True) == _SHELF_BUTTON_LABEL:
                cmds.deleteUI(child)
        except Exception:
            pass

    button = cmds.shelfButton(
        parent=current,
        label=_SHELF_BUTTON_LABEL,
        annotation="Left-click: launch.  Right-click: update from GitHub.",
        image="pythonFamily.png",
        imageOverlayLabel=_SHELF_BUTTON_LABEL[:5],
        command=_SHELF_LAUNCH_CMD,
        sourceType="python",
    )
    popup = cmds.popupMenu(parent=button, button=3)
    cmds.menuItem(parent=popup, label="Launch Tool",
                  command=_SHELF_LAUNCH_CMD, sourceType="python")
    cmds.menuItem(parent=popup, divider=True)
    cmds.menuItem(parent=popup, label="Update from GitHub",
                  command=_SHELF_UPDATE_CMD, sourceType="python")


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #

def install() -> str:
    from maya import cmds

    user_scripts = cmds.internalVar(userScriptDir=True).rstrip("/\\")
    if not os.path.isdir(user_scripts):
        os.makedirs(user_scripts)

    prev_version = _read_installed_version(user_scripts)

    _close_existing_window()
    _fetch_module(user_scripts)
    _clean_pycache(user_scripts)
    _verify_install(user_scripts)
    _flush_imports()

    if user_scripts not in sys.path:
        sys.path.insert(0, user_scripts)

    _add_shelf_button()
    new_version = _read_installed_version(user_scripts)

    print(f"[{_MODULE}] " + "=" * 55)
    print(f"[{_MODULE}] installed to:      {user_scripts}")
    print(f"[{_MODULE}] previous version:  {prev_version}")
    print(f"[{_MODULE}] current  version:  {new_version}")
    print(f"[{_MODULE}] " + "=" * 55)

    try:
        cmds.confirmDialog(
            title=_SHELF_BUTTON_LABEL,
            message=(f"Installed to:\n{user_scripts}\n\n"
                     f"Version: {prev_version} → {new_version}\n\n"
                     f"The '{_SHELF_BUTTON_LABEL}' shelf button has "
                     "been refreshed. Left-click to launch, right-click "
                     "for Update-from-GitHub."),
            button=["OK"])
    except Exception:
        pass
    return user_scripts


def onMayaDroppedPythonFile(*_args) -> None:
    install()


# ``exec(open(...).read())`` from Script Editor bypasses
# ``__name__ == '__main__'`` and onMayaDroppedPythonFile — auto-run
# install here so both entry points work. install() is idempotent
# (existing file wiped before write), so drag-drop's double-run is
# harmless.
try:
    from maya import cmds as _cmds  # noqa: F401
    install()
except ImportError:
    pass
