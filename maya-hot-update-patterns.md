# Maya Python ツール ─ ホットアップデート実装ノート

Stylized Paint Projectile Tool の開発中に、「install.py を Maya にドラッグするだけで、以降 Maya を再起動せずに GitHub 最新版に更新できる」仕組みを作る過程で **踏んだ落とし穴** と **それを回避する実装パターン** をまとめた社内ナレッジ。次に同種のツール
(Maya 用のアーティスト向け Python ツール) を作るときに、同じ調査を繰り返さないための備忘録。

対象は Maya 2023 (Python 3.9) / Windows。Autodesk のドラッグ&ドロップ + シェルフ + GitHub 配布という構成を前提にしている。

---

## 0. 目指すゴール

エンドユーザー (アニメーター) から見た体験:

1. GitHub から `install.py` を落として Maya のビューポートにドラッグ → セットアップ完了
2. シェルフに追加されたボタンを押すと UI が起動
3. 開発者側で GitHub に修正を push した後は、**Maya 再起動なし** で UI 内の「Update」ボタン一発で最新版が反映される

このシンプルな体験に至るまでに 6 個以上の非自明な問題を踏んだ。以下はその全記録。

---

## 1. 失敗パターン (症状 → 原因 → 対策)

### 1-1. `cmds.circle(ch=False)` の戻り値が unpack エラー

**症状**
```
ValueError: not enough values to unpack (expected 2, got 1)
```

**原因**
`cmds.circle` は construction history あり (`ch=True`, デフォルト) だと `[transform, makeNurbCircle]` の 2 要素、`ch=False` だと `[transform]` の 1 要素しか返さない。

**対策**
history を切るなら unpack しない:
```python
ctrl = cmds.circle(n="ctrl", ch=False)[0]      # OK
# NG: ctrl, _ = cmds.circle(n="ctrl", ch=False)
```

**教訓**
Maya コマンドは同じ関数でもフラグ次第で戻り値の shape が変わる。`ch`, `q`, `e` 系フラグを付けたら戻り値を必ず docs で確認。

---

### 1-2. `shutil.copytree` がソースの mtime を保持してしまう

**症状**
`install.py` を再ドラッグしても、コピー先の `.py` の更新日時が変わらない。「上書きが効いていない」と誤診する。

**原因**
`shutil.copytree` は内部で `shutil.copy2` を使い、`copystat` でソースの mtime / atime / mode をコピー先に転写する。GitHub から `git clone` した .py はチェックアウト時刻の mtime を持つので、複数回コピーしても全部同じ mtime になる。

**対策**
mtime = 「install 実行時刻」にしたいなら、`copy2` を使わず自前で書き直す:

```python
def _atomic_copy_file(src, dst):
    with open(src, "rb") as fh:
        data = fh.read()
    _atomic_write_bytes(dst, data)   # mtime は書き込み時刻になる
```

**教訓**
`.py` のタイムスタンプは「本当に上書きされたか」を目視確認するデバッグ手掛かりになる。デフォルト挙動が copystat だと、その手掛かりが失われる。ツールの updater を書くときは自前 write を推奨。

---

### 1-3. Windows で read-only 属性のせいで `rmtree` / `os.replace` が失敗

**症状**
インストール自体は成功するが、次のインストール時にサイレントに失敗するケースがある (特にネットワークドライブや同期フォルダ)。

**原因**
Windows のファイル属性 read-only が付いていると、`shutil.rmtree` が `PermissionError` で止まる。`os.replace` も同じ。

**対策**
削除の前に必ず writable にする + `onerror` でリトライ:

```python
import stat

def _force_writable(path):
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except Exception:
        pass

def _force_rmtree(path):
    def _on_error(func, p, exc_info):
        try:
            os.chmod(p, stat.S_IWRITE); func(p)
        except Exception:
            pass
    for _ in range(3):
        try:
            shutil.rmtree(path, onerror=_on_error)
            if not os.path.exists(path): return
        except Exception:
            time.sleep(0.2)
    if os.path.exists(path):
        raise RuntimeError(f"cannot remove {path!r}")
```

**教訓**
Windows ではファイル操作に必ず retry ループを噛ませる。特に read-only + アンチウイルスがロックしているケース。

---

### 1-4. 書き込みが原子的でない → 半端ファイルが残る

**症状**
インストール途中でエラー → 中途半端な .py が残り、以降 import で SyntaxError

**対策**
一時ファイルに書いて `os.replace` で原子スワップ:

```python
def _atomic_write_bytes(target, data):
    if os.path.exists(target):
        _force_writable(target)
    tmp = target + ".tmp_install"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        try: os.fsync(fh.fileno())
        except Exception: pass
    os.replace(tmp, target)      # 同一ドライブ上なら atomic
```

**教訓**
「書き込み中にキャンセル」「ネットワーク断」「ディスクフル」があってもファイルが常に「以前の完全な状態」か「新しい完全な状態」のどちらかであることを保証する。

---

### 1-5. Python が `.py` を上書きしても古い `.pyc` を使い続ける

**症状**
`.py` を新しくしたのに、実行される内容が古い。

**原因**
Python は `__pycache__/*.pyc` にバイトコードキャッシュを持ち、`.py` の mtime と `.pyc` に記録された「元 .py の mtime」が一致していると `.pyc` を使う。**mtime が偶然一致すれば古い .pyc を使い続けてしまう**。

**対策**
インストール時に `__pycache__` フォルダを毎回消す:

```python
def _clean_pycache(pkg_dir):
    _force_rmtree(os.path.join(pkg_dir, "__pycache__"))
```

**教訓**
Python の pyc キャッシュは mtime ベースで、自前 updater を作ると壊れやすい。install の最後に必ず `__pycache__` を消す。

---

### 1-6. `sys.modules` にキャッシュされたモジュールが更新されない

**症状**
ファイルは新しくなったのに、import しても古いコードが走る。

**原因**
Python は 1 度 import したモジュールを `sys.modules` にキャッシュし、同じ名前の 2 回目以降の import は no-op。

**対策**
入れ替え前に強制フラッシュ:

```python
def _flush_imports(pkg="my_tool"):
    for name in list(sys.modules):
        if name == pkg or name.startswith(pkg + "."):
            sys.modules.pop(name, None)
```

シェルフボタンの起動コマンド側でも毎回フラッシュしておくと、次に開くとき必ず disk から最新を読む:

```python
_SHELF_CMD = """
import sys
for _m in [k for k in sys.modules
           if k == 'my_tool' or k.startswith('my_tool.')]:
    sys.modules.pop(_m, None)
import my_tool
my_tool.show()
"""
```

**教訓**
「ファイルは更新されているのにコードが変わらない」時の第 1 容疑者は `sys.modules`。第 2 容疑者は `__pycache__`。

---

### 1-7. GitHub raw の CDN が **query string を無視して** キャッシュする

**症状**
`?_={time.time()}` を付けて「cache busting しているつもり」でも、実行のたびに古いコンテンツが返ってくる。ダウンロードログは正常に見えるのに `previous: 0.1.7 → current: 0.1.7` のように更新されない。

**原因**
`raw.githubusercontent.com` の CDN (Varnish/Fastly) は **URL のパス部分のみを cache key にする**。`?_=1234` を付けても cache key に反映されないので、初回に取ったレスポンスが TTL 内 (数分〜数十分) は使い回される。ここでハマると発覚しにくい。`curl` で外から叩くと最新が返ってくるので余計に混乱する (別ネットワーク / 別 UA だと別 edge に当たる)。

**対策 (決定版)**
**Commit SHA を含んだ immutable URL を使う**。SHA-scoped URL は commit ごとに一意なので、CDN が何をキャッシュしていようが「新しい SHA = 別 URL = 別 cache key」となり必ず最新が取れる。

```python
import json, urllib.request

def _latest_sha():
    api = "https://api.github.com/repos/OWNER/REPO/branches/main"
    return json.loads(urllib.request.urlopen(api, timeout=30).read())["commit"]["sha"]

def download_file(rel_path):
    sha = _latest_sha()
    url = f"https://raw.githubusercontent.com/OWNER/REPO/{sha}/{rel_path}"
    return urllib.request.urlopen(url, timeout=30).read()
```

**教訓**
- ブランチ名 URL (`/main/path`) は CDN のキャッシュに引っかかる。
- クエリストリング cache-buster は raw.githubusercontent.com に対しては効かない。
- `Cache-Control: no-cache` ヘッダも同上、CDN によっては無視される。
- **一度は必ず SHA-pinned URL を使う** のがベストプラクティス。API コールが 1 回増えるコストは、キャッシュに悩まされるコストより遥かに小さい。

---

### 1-8. Maya のドラッグ&ドロップは **同じファイルを 2 回目にドラッグしても何も起こらない**

**症状**
`install.py` を初回ドラッグしたら install.py が動いた。修正を GitHub に push した後、もう一度同じ install.py をドラッグしても、ダイアログもログも出ず何も起こらない。Maya の再起動が必要になる。

**原因**
Maya の `onMayaDroppedPythonFile` フックは、同一セッション内で同じファイルパスに対して 1 度しか呼ばれない (モジュールキャッシュに拾われて no-op 化する)。

**対策**
ドラッグ&ドロップを「再アップデート手段」として使わない。代わりに以下を用意:

1. **ツール UI 内の「Update from GitHub」ボタン**
   ```python
   cmds.button(l="Update from GitHub", c=_update_from_github)
   ```
2. **シェルフボタンの右クリックポップアップメニュー**
   ```python
   btn = cmds.shelfButton(...)
   popup = cmds.popupMenu(parent=btn, button=3)  # 3 = right click
   cmds.menuItem(parent=popup, label="Launch",  command=LAUNCH_CMD)
   cmds.menuItem(parent=popup, label="Update",  command=UPDATE_CMD)
   ```

どちらも「URL を毎回 fetch → exec install.py → 再オープン」を叩き直せるので、drag の caching に依存しない。

**教訓**
「初回インストール」と「アップデート」は別導線で用意する。ドラッグ&ドロップは初回のみ、アップデートは UI ボタン or シェルフメニュー。

---

### 1-10. install.py の `_REMOTE_FILES` に新規モジュールを追加し忘れる

**症状**
新しい `.py` をパッケージに追加して push、ユーザーが Update を叩くと `ModuleNotFoundError: No module named '<pkg>.<newmodule>'`。

**原因**
install.py はダウンロード対象ファイルをハードコードした `_REMOTE_FILES` タプルで管理している。パッケージに新規ファイルを追加しても、ここに書かないとダウンロードされない。ローカルの `__init__.py` は新規モジュールを import しようとするが、ディスクには存在しないので失敗する。

**対策**
新規 `.py` を追加したら、必ず同じ commit で `install.py` の `_REMOTE_FILES` にも追記する。CI があるならインストール後 import 全モジュール可能かの smoke test を通すと確実。

**教訓**
「配布リストはコードの真実 (package structure) と乖離しやすい」。手動同期を義務にするか、自動列挙 (GitHub API tree walk 等) で解決。

---

### 1-9. コールバック内から親ウィンドウを消すと再オープン後に UI が出ない

**症状**
Update ボタンのハンドラで `cmds.deleteUI(WINDOW)` → 再インストール → `show()` を同期実行すると、install() の `confirmDialog` は表示されるが、その後の `show()` で開いたウィンドウが見えないか、そもそも作られない。

**原因**
- Update ボタンのコールバックが実行中に、そのボタンをホストしている親ウィンドウを削除している
- install() の中で `confirmDialog` がモーダル表示され、それが閉じた直後に `show()` が走るが、Maya の UI スレッドがまだ落ち着いていない

**対策**
`cmds.evalDeferred` で **現在のコールバックを完全に抜けてから** 次のステージを実行する。 update フローを 3 段階に分割:

```python
def _update_from_github(*_):
    # Stage 1: ボタン callback は即 return
    cmds.evalDeferred(_run_update, lowestPriority=True)

def _run_update():
    # Stage 2: idle 時に fetch + exec + flush
    source = fetch()
    cmds.deleteUI(WINDOW)          # 自分のウィンドウを閉じる
    exec_install(source)           # ここで modal 出るが問題なし
    flush_sys_modules()
    cmds.evalDeferred(_reopen, lowestPriority=True)  # 更に defer

def _reopen():
    # Stage 3: 次の idle で reopen
    import my_tool
    my_tool.show()
```

**教訓**
Maya の UI コールバックの中で自分自身の親を消してはいけない。破壊系操作は必ず `evalDeferred` で defer する。

---

## 2. 成功パターン ─ 「ホットアップデート可能な Maya ツール」の標準構成

上の全ての落とし穴を回避した、再利用可能な最終構成:

### 2-1. ファイルレイアウト

```
repo-root/
├── install.py                        ← エンドユーザーが唯一手動で扱うファイル
├── <package>/                        ← 実装本体
│   ├── __init__.py                   ← __version__ を必ず持たせる
│   ├── ui.py                         ← Update ボタンを含む UI
│   └── ...
└── <launcher>.py                     ← import <launcher>; launcher.show() で開く
```

### 2-2. install.py がやること

1. `cmds.internalVar(userScriptDir=True)` で Maya ユーザースクリプトフォルダを取得
2. GitHub API で `main` の最新 commit SHA を取得
3. `raw.githubusercontent.com/OWNER/REPO/<SHA>/...` から各ファイルを atomic write でダウンロード
4. `__pycache__` を掃除
5. `sys.modules` から自パッケージを flush
6. `<user_scripts>` を `sys.path` に追加
7. シェルフボタンを追加 (左クリック=起動、右クリック=Update)
8. `confirmDialog` で `previous → current` バージョンを表示

### 2-3. UI が持つべき Update ボタンのロジック

```python
def _update_from_github(*_):
    cmds.evalDeferred(_run_update, lowestPriority=True)

def _run_update():
    # SHA を解決して install.py を SHA-pinned URL で fetch
    sha = _latest_sha()
    url = f"https://raw.githubusercontent.com/OWNER/REPO/{sha}/install.py"
    source = urllib.request.urlopen(url, timeout=30).read()

    # 自ウィンドウを閉じる → install.py 実行 → 再オープン (更に defer)
    if cmds.window(WIN, exists=True):
        cmds.deleteUI(WIN)
    exec(compile(source, "install.py", "exec"),
         {"__name__": "install", "__file__": "<github>"})
    for m in [k for k in list(sys.modules)
              if k == PKG or k.startswith(PKG + ".") or k == LAUNCHER]:
        sys.modules.pop(m, None)
    cmds.evalDeferred(_reopen, lowestPriority=True)

def _reopen():
    import <launcher> as l
    l.show()
```

### 2-4. シェルフボタン

- **左クリック** — 起動コマンド (先に `sys.modules` フラッシュしてから import + show)
- **右クリック** — ポップアップメニューで Launch / Update

Update コマンドは install.py と同じロジック (SHA fetch → exec) を **セルフコンテインな snippet** として書く。パッケージへの依存を持たせない (パッケージが壊れていても Update が走るように)。

### 2-5. UI にバージョン表示

タイトルバーと footer に `__version__` を出しておくと、動作しているコードのバージョンが目視で分かる。開発中もユーザーサポートでも便利。

```python
win = cmds.window(WIN, t=f"My Tool  —  v{__version__}", ...)
cmds.text(l=f"my_tool  v{__version__}", fn="smallObliqueLabelFont", al="right")
```

---

## 3. 検証チェックリスト

新規ツールを作ったら、以下のシナリオを 1 度は通す:

- [ ] 初回インストール — install.py をドラッグ、ダイアログが出る、シェルフボタンが増える
- [ ] 起動 — シェルフボタンをクリック、UI が開き、バージョン表示が正しい
- [ ] GitHub に version bump を push
- [ ] Update — UI 内 Update ボタンをクリック、`previous → current` ダイアログが正しく変わる
- [ ] Update 後 — UI が閉じて再オープンされ、新バージョンが表示される
- [ ] シェルフ右クリック → Update でも同様に動く
- [ ] `.py` の mtime が install 実行時刻に更新されている
- [ ] `__pycache__` が消えて再生成されている
- [ ] Maya を再起動しても最新版が読まれる (`__init__.py` を直接開いて `__version__` を目視)

---

## 4. デバッグ用 診断スニペット

「更新されているはずなのに古い」と言われたときは、まずこれを Script Editor で実行してもらう:

```python
import os, sys, datetime, hashlib
# 一旦 flush してから fresh import
for m in [k for k in list(sys.modules) if k.startswith("<pkg>")]:
    sys.modules.pop(m, None)

import <pkg>
p = <pkg>.__file__
size = os.path.getsize(p)
mtime = datetime.datetime.fromtimestamp(os.path.getmtime(p))
with open(p, "rb") as f:
    sha = hashlib.sha256(f.read()).hexdigest()[:16]

print(f"module   : {p}")
print(f"version  : {<pkg>.__version__}")
print(f"size     : {size} bytes")
print(f"mtime    : {mtime}")
print(f"sha256   : {sha}")
```

得られる情報:

- `mtime` が更新されていない → install が走っていない or copystat 系のバグ
- `mtime` は新しいが `version` が古い → CDN キャッシュ (SHA-pinned URL に切り替え)
- `sha256` が想定と違う → 意図しない別ファイルが混入

---

## 5. 完全テンプレ コード (この doc に埋め込み)

このセクションから先は **この doc 単体で** 新ツールを立ち上げられるように、
2 つの Python テンプレの全文をコードブロックで直接掲載する。
追加の URL や外部ファイル取得は不要 — **§5-A / §5-B のコードを丸ごとコピー**
→ §6 の CUSTOMIZE 定数を書き換え → GitHub に push、で完了。

### 配布時のファイル構成

新ツール リポジトリのルートに以下 2 ファイルだけ置く:

```
your-new-tool/
├── install.py       ← §5-A の内容をコピー
└── my_tool.py       ← §5-B の内容をコピー ・ ファイル名は §6-3 の規則で命名
```

エンドユーザーへの配布物 = `install.py` の GitHub raw URL のみ。
ブラウザで保存 → Maya ビューポートにドラッグで完結。

以降、ユーザーは UI 内の「GitHub から更新」ボタン (もしくはシェルフボタン
右クリック → Update from GitHub) で最新版を取り込める。

---

### §5-A. `install.py` の全文

```python
"""Generic Maya-tool installer template (single-file tool).

Ships alongside a single module file (``my_tool.py``). Two ways to
run this from inside Maya:

1) Drag ``install.py`` from your file browser into any Maya viewport.
2) From the Script Editor (Python tab)::

       exec(open(r"C:/path/to/install.py").read())

Either way:

* Fetches ``my_tool.py`` fresh from GitHub (SHA-pinned URL, so no CDN
  cache staleness) and writes it to your Maya user scripts folder.
* Force-overwrites the existing file (Windows read-only cleared,
  atomic tmp+os.replace).
* Wipes any ``__pycache__/my_tool.cpython-*.pyc`` so stale bytecode
  doesn't shadow the freshly copied source.
* Flushes ``my_tool`` from ``sys.modules`` so the next import reads
  from disk — no Maya restart needed.
* Adds / refreshes a shelf button on the active shelf. Left-click
  launches; right-click has an "Update from GitHub" menu that
  re-runs this installer without another drag.

============================================================================
CUSTOMIZE FOR YOUR TOOL — change these 4 constants:
============================================================================
"""

from __future__ import annotations

import os
import re
import shutil
import sys


# ─── CUSTOMIZE ────────────────────────────────────────────────────────────
_GITHUB_OWNER = "YOUR_GITHUB_USERNAME"
_GITHUB_REPO = "YOUR_REPO_NAME"
_GITHUB_BRANCH = "main"

_MODULE = "my_tool"                     # your tool's .py filename (without .py)
_SHELF_BUTTON_LABEL = "MyTool"          # short label on the shelf button
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
    iterate on the local checkout set ``<MODULE>_USE_LOCAL=1``."""
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
```

---

### §5-B. `my_tool.py` の全文

`_build_body()` の中を自分のツールの UI に書き換えて使う。それ以外は触らない。

```python
"""my_tool — single-file Maya tool template with Update-from-GitHub.

Everything the tool needs lives here:
    * __version__ for install.py's before/after dialog and the UI footer
    * show() to open the tool window
    * The Update-from-GitHub button flow (evalDeferred 3-stage, safe
      window teardown, SHA-pinned re-fetch of install.py)

Ship this file + install.py — that's it. Add your real controls and
tool logic in place of the placeholder ``_build_body``.

Shelf button command (auto-generated by install.py) is just:

    import my_tool; my_tool.show()

Rename the module to whatever your tool is (``rig_utils``,
``asset_browser``, etc.) — remember to update install.py's ``_MODULE``
to match.
"""

from __future__ import annotations

try:
    from maya import cmds  # type: ignore
except ImportError:  # pragma: no cover
    cmds = None  # type: ignore


__version__ = "0.1.0"


WINDOW = "myToolWin"  # match install.py's _close_existing_window() target

# ─── CUSTOMIZE ────────────────────────────────────────────────────────────
_GITHUB_OWNER = "YOUR_GITHUB_USERNAME"
_GITHUB_REPO = "YOUR_REPO_NAME"
_GITHUB_BRANCH = "main"
_PACKAGE = "my_tool"          # matches this file's module name
# ─── END CUSTOMIZE ────────────────────────────────────────────────────────

_GITHUB_API = f"https://api.github.com/repos/{_GITHUB_OWNER}/{_GITHUB_REPO}"
_GITHUB_RAW_BASE = f"https://raw.githubusercontent.com/{_GITHUB_OWNER}/{_GITHUB_REPO}"


# --------------------------------------------------------------------------- #
# Update-from-GitHub flow  (patterns doc §1-7, §1-8, §1-9)
# --------------------------------------------------------------------------- #

def _resolve_latest_sha() -> str:
    """SHA-pinned URLs are the only reliable cache-buster for
    raw.githubusercontent.com. Ask the API for main's tip commit."""
    import json
    import random
    import time
    import urllib.request

    salt = f"{time.time():.6f}_{random.randint(0, 2 ** 32)}"
    req = urllib.request.Request(
        f"{_GITHUB_API}/branches/{_GITHUB_BRANCH}?_={salt}",
        headers={
            "Accept": "application/vnd.github+json",
            "Cache-Control": "no-cache",
            "User-Agent": f"{_PACKAGE}-updater/{salt}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))["commit"]["sha"]
    except Exception as exc:
        print(f"[{_PACKAGE}] SHA lookup failed ({exc}); falling back to "
              f"{_GITHUB_BRANCH}")
        return _GITHUB_BRANCH


def update_from_github(*_args) -> None:
    """UI button callback. Immediately returns — the actual work runs
    on the next Maya idle so we don't tear down the window that owns
    this callback while the callback is still on the stack."""
    cmds.evalDeferred(_run_update, lowestPriority=True)


def _run_update() -> None:
    import sys
    import traceback
    import urllib.request

    sha = _resolve_latest_sha()
    url = f"{_GITHUB_RAW_BASE}/{sha}/install.py"
    print(f"[{_PACKAGE}] update: fetching {url}")
    try:
        req = urllib.request.Request(url, headers={
            "Cache-Control": "no-cache",
            "User-Agent": f"{_PACKAGE}-updater/{sha[:10]}",
        })
        source = urllib.request.urlopen(req, timeout=30).read()
    except Exception as exc:
        traceback.print_exc()
        cmds.confirmDialog(title="Update failed",
                           message=f"install.py fetch failed:\n{exc}",
                           button=["OK"])
        return

    if cmds.window(WINDOW, exists=True):
        try:
            cmds.deleteUI(WINDOW)
        except Exception:
            pass

    ns = {"__name__": "install", "__file__": "<github>"}
    try:
        exec(compile(source, "install.py (from GitHub)", "exec"), ns)
    except Exception as exc:
        traceback.print_exc()
        cmds.confirmDialog(
            title="Update failed",
            message=(f"install.py raised:\n{type(exc).__name__}: {exc}\n\n"
                     "See Script Editor for full traceback."),
            button=["OK"])
        return

    # Flush self so the next `import my_tool` re-reads the fresh copy.
    for m in [k for k in list(sys.modules) if k == _PACKAGE]:
        sys.modules.pop(m, None)

    # Defer reopen so install()'s modal is fully dismissed first.
    cmds.evalDeferred(_reopen_after_update, lowestPriority=True)


def _reopen_after_update() -> None:
    import importlib
    import sys
    import traceback
    try:
        if _PACKAGE in sys.modules:
            importlib.reload(sys.modules[_PACKAGE])
        mod = importlib.import_module(_PACKAGE)
        mod.show()
    except Exception as exc:
        traceback.print_exc()
        cmds.confirmDialog(
            title="Reopen failed",
            message=(f"Update finished but reopening the tool window "
                     f"failed:\n{type(exc).__name__}: {exc}\n\n"
                     "Click the shelf button to reopen manually."),
            button=["OK"])


# --------------------------------------------------------------------------- #
# Window  — replace _build_body with your real controls
# --------------------------------------------------------------------------- #

def _build_body() -> None:
    """Placeholder tool UI. Replace with your real controls."""
    cmds.separator(h=4, style="none")
    cmds.text(l="このウィンドウ本体は自由に組み替えて OK。",
              al="left")
    cmds.text(l="下の「GitHub から更新」だけそのまま置いておけば\n"
                "アップデートフローは動きます。",
              al="left")


def show() -> str:
    if cmds is None:
        raise RuntimeError("show() must be called inside Maya.")

    if cmds.window(WINDOW, exists=True):
        cmds.deleteUI(WINDOW)

    win = cmds.window(WINDOW,
                      t=f"My Tool  —  v{__version__}",
                      w=340, h=200, mnb=True, mxb=False, s=True)
    cmds.columnLayout(adj=True, rs=8, cat=("both", 10))

    _build_body()

    cmds.separator(h=10, style="in")
    cmds.rowLayout(nc=2, adj=1, cw2=(200, 130))
    cmds.text(l=f"{_PACKAGE}  v{__version__}",
              al="left", fn="smallObliqueLabelFont")
    cmds.button(l="GitHub から更新", h=24, c=update_from_github)
    cmds.setParent("..")

    cmds.showWindow(win)
    return win
```

---

## 6. CUSTOMIZE ブロック — 書き換え必須の定数

§5-A と §5-B の各コード冒頭に `# ─── CUSTOMIZE ───` で挟まれた定数ブロックが
ある。**書き換えが必要なのはこのブロックだけ**、他はそのままで動く。

### 6-1. `install.py` (5 定数)

```python
# ─── CUSTOMIZE ────────────────────────────────────────────────────────────
_GITHUB_OWNER = "YOUR_GITHUB_USERNAME"
_GITHUB_REPO = "YOUR_REPO_NAME"
_GITHUB_BRANCH = "main"

_MODULE = "my_tool"                     # your tool's .py filename (no .py)
_SHELF_BUTTON_LABEL = "MyTool"          # short label on the shelf button
# ─── END CUSTOMIZE ────────────────────────────────────────────────────────
```

| 定数 | 説明 | 例 |
|---|---|---|
| `_GITHUB_OWNER` | GitHub のアカウント名 / Organization 名 | `"acme_studio"` |
| `_GITHUB_REPO`  | リポジトリ名 (owner/repo の repo 部分) | `"rig-utils"` |
| `_GITHUB_BRANCH`| ダウンロード対象ブランチ (通常 `"main"`) | `"main"` |
| `_MODULE`       | ツール本体 `.py` のモジュール名 (`.py` 抜き) | `"rig_utils"` |
| `_SHELF_BUTTON_LABEL` | シェルフに表示する短い名前 (10 文字以下推奨) | `"RigUtils"` |

### 6-2. `my_tool.py` (4 定数 + WINDOW)

```python
WINDOW = "myToolWin"      # match install.py's _close_existing_window() target

# ─── CUSTOMIZE ────────────────────────────────────────────────────────────
_GITHUB_OWNER = "YOUR_GITHUB_USERNAME"
_GITHUB_REPO = "YOUR_REPO_NAME"
_GITHUB_BRANCH = "main"
_PACKAGE = "my_tool"        # matches this file's module name
# ─── END CUSTOMIZE ────────────────────────────────────────────────────────
```

| 定数 | 説明 |
|---|---|
| `_GITHUB_OWNER` / `_GITHUB_REPO` / `_GITHUB_BRANCH` | install.py と **同一値** にする |
| `_PACKAGE` | このファイル自身のモジュール名 (= install.py の `_MODULE`) |
| `WINDOW`   | ツールウィンドウの一意名。install.py の `_close_existing_window` は `f"{_MODULE}Win"` を探すので、**`_MODULE + "Win"` にしておくと自動で一致する** |

### 6-3. モジュール名 = ファイル名 = 3 か所同一

3 か所を一致させることが唯一の落とし穴:

```
実ファイル名:               <MODULE_NAME>.py
install.py の  _MODULE   :  <MODULE_NAME>
my_tool.py の  _PACKAGE  :  <MODULE_NAME>
```

例: 新ツール名を `rig_utils` にする場合:

```
rig_utils.py                       ← ファイル自体を this にリネーム
install.py:  _MODULE  = "rig_utils"
rig_utils.py: _PACKAGE = "rig_utils"
rig_utils.py: WINDOW   = "rig_utilsWin"   ← _MODULE + "Win"
```

これ以外 (`_GITHUB_OWNER`, `_GITHUB_REPO`, `_SHELF_BUTTON_LABEL`, `WINDOW`) は
好きに命名して OK。

### 6-4. コード側で書き換えるのは 1 か所だけ

`my_tool.py` の `_build_body()` を自分の UI に置き換える:

```python
def _build_body() -> None:
    """Placeholder tool UI. Replace with your real controls."""
    # ← ここに cmds.textFieldButtonGrp / cmds.floatSliderGrp などを並べる
```

update 関連の関数 (`_resolve_latest_sha`, `update_from_github`, `_run_update`,
`_reopen_after_update`) と `show()` の外枠 (window / footer / Update ボタン) は
**触らない**。

### 6-5. 追加モジュールを作る場合

`my_tool.py` が肥大化してきたら追加ファイルに分割することになる。
その場合の作業:

1. `my_tool/` フォルダを作って `__init__.py` + サブモジュール群にリファクタ
   (single-file → package 化)
2. `install.py` の `_fetch_module` を「単一ファイルダウンロード」から
   「ファイル一覧ダウンロード」に拡張 → `_REMOTE_FILES` タプルを追加
3. **新規モジュールを追加したら必ず `_REMOTE_FILES` にも追記** (§1-10 で
   踏んだ落とし穴の再発防止)
4. `_flush_imports` をパッケージ全体をポップするパターンに変更

拡張後の実装イメージ (パッケージ + 複数モジュール):

```python
# install.py の変更点だけ抜粋
_REMOTE_FILES = (
    f"{_PACKAGE}/__init__.py",
    f"{_PACKAGE}/ui.py",
    f"{_PACKAGE}/core.py",
    # 新規モジュール追加時はここに追記
)

def _fetch_module(dest_root):
    sha = _resolve_latest_sha()
    for rel in _REMOTE_FILES:
        url = f"{_GITHUB_RAW_BASE}/{sha}/{rel}"
        target = os.path.join(dest_root, rel.replace("/", os.sep))
        # ... _atomic_write_bytes(target, urllib.request.urlopen(url).read())

def _flush_imports():
    for name in list(sys.modules):
        if name == _PACKAGE or name.startswith(_PACKAGE + "."):
            sys.modules.pop(name, None)
```

---

## 7. Claude / LLM への依頼プロンプト テンプレ

新ツール開発を Claude (もしくは他の LLM) に頼む時のプロンプト:

```
新しい Maya ツール "<ツール名>" を作りたい。
配布とアップデートは以下の doc に記載されている「単一 .md 完結」テンプレを
使って:

  (この maya-hot-update-patterns.md ファイル全体をここに貼り付け)

指示:

1. §5-A の install.py の全文と §5-B の my_tool.py の全文をコピーし、
   新ツール リポジトリのルートに 2 ファイルとして書き出す。
   ファイル名は my_tool.py → <ツール名>.py にリネーム (§6-3 の規則)。

2. §6 の CUSTOMIZE ブロック 2 箇所を以下で埋める:
     GitHub owner: <あなたの GitHub アカウント>
     リポジトリ名: <新リポジトリ名>
     モジュール名: <スネークケース、ファイル名にもする>
     シェルフボタン ラベル: <10 文字以下>

3. <ツール名>.py の _build_body() に、以下の機能を実装:
     ・<機能 1>
     ・<機能 2>
     ...

4. 配布インフラ (update 関連関数、shelf button 部分) には手を加えない。
   §1-1 〜 §1-10 で解説されている失敗パターンを踏まないよう注意。

5. GitHub に push 後、install.py の raw URL をエンドユーザーに配布。
```

この doc 1 つを渡すだけで、Claude が §5-A / §5-B のコードを丸ごとコピー
→ §6 の書き換え → §7 の機能実装、を一気通貫でこなせる。

---

## 8. オプション拡張機能 (実装前に必ずユーザーに確認)

以下の 2 機能は §5-B の基本テンプレには含めていない **オプション拡張**。
利点と副作用のトレードオフがあるため、**LLM は絶対に勝手に実装しない**。
実装する前に必ずユーザーに方式を説明した上で判断を仰ぐこと。

### ⚠ LLM 向けの重要ルール

- ユーザーはこの doc の中身を読んでいないことが多い
- 「バージョン管理も欲しい」「新版通知も欲しい」といった要望が出た時、
  もしくは **要望が明示されなくても** §8 の機能が話題として関連しそうな
  場合は、以下を守ること:

1. **勝手に §8 の機能を組み込まない**。基本の §5-B テンプレだけで
   スタートし、§8 は必ずユーザーの明示的な OK を得てから追加する。
2. ユーザーへの質問時は **専門用語を避け** 、方式の利点/欠点を短く
   説明する。以下のような形式:

   > 「新バージョン公開時にツール起動時に通知ダイアログを出せます。方式は
   > 2 通りあります:
   >   ・A: 起動時に 1 回チェック (実装 30 行、シンプル、Maya セッション
   >     開始時のみ通知)
   >   ・B: 定期チェック (実装 60 行、セッション中に新版出ても検知、
   >     ネットワークトラフィック増)
   > A で始めるのが軽くて安全ですが、どちらにしますか? もしくは通知は
   > 不要ですか?」

3. ユーザーが選んだ方式だけを実装。両方入れたい / 全部入れて、と
   明示されない限りは複合させない。
4. 実装後に「バージョン選択機能も追加できますが試しますか?」など、
   §8 の他機能への誘導を必要に応じて行う。ただし押し付けない。

---

### 8-1. バージョンごとに保存・選択して Download / ロールバック

過去のバージョンを保存しておき、ユーザーが任意バージョンを Install /
ロールバックできる機能。

#### 方式 A: Git tags を使う (シンプル / 推奨)

- リポジトリで `git tag v0.1.0`, `v0.2.0` … を打ち push
- installer が GitHub API `/repos/OWNER/REPO/tags` で一覧取得
- 「バージョン選択」ダイアログで選択された tag の commit SHA から
  通常の SHA-pinned URL でダウンロード
- **利点**: リポジトリ構造は現状のまま、tag を打つ手間だけ追加
- **欠点**: リリースノートは無い、tag 打ち忘れやすい

#### 方式 B: GitHub Releases を使う (パブリック配布向け)

- リリースごとに tag + release notes + optional バイナリ を GitHub
  Releases に登録
- installer は `/repos/OWNER/REPO/releases` から一覧取得
- リリースノートを UI に表示可能
- **利点**: 自然な "配布物" 表現、Changelog 表示、統計取れる
- **欠点**: リリース作成の手順が少し増える、Releases UI に慣れる必要

#### 方式 C: リポジトリ内に `versions/` フォルダで実ファイル保存

- `versions/0.1.0/my_tool.py`, `versions/0.2.0/my_tool.py` を並べる
- installer は `versions/` を list → 選択
- **利点**: セルフホスト完結、tag / release 不要
- **欠点**: リポジトリが肥大、git 履歴と二重管理、diff が取りづらい

#### UI 側の追加要素

- シェルフ右クリックメニューに「Install specific version…」
- 選択ダイアログ: `cmds.optionMenu` / `cmds.textScrollList`
- よくある UI 構成: 「Latest」「Choose version」「Downgrade to previous」

---

### 8-2. 新バージョン配信時にダイアログでお知らせ

新バージョンが GitHub に push されたら、次回ツール起動時に「新版あります」
ダイアログを表示。

#### 方式 A: 起動時に軽く 1 回チェック (最軽量 / 推奨)

- `show()` の末尾で `evalDeferred` に `_check_for_update` を仕込む
- GitHub API から latest tag / commit SHA を取得
- ローカルの `__version__` と比較
- 新しければ In-View メッセージ or 小さいダイアログを表示:
  > 「新バージョン v0.3.0 が公開されました。今すぐ更新しますか?
  >   [はい] [あとで] [このバージョンを無視]」
- **利点**: 起動時 1 リクエストで完結、実装 ~30 行、副作用小
- **欠点**: Maya セッション中に新版が出ても、そのセッション中は検知しない

#### 方式 B: バックグラウンド定期チェック (Deep 統合)

- `cmds.scriptJob` の `idle` イベント + タイムスタンプで N 時間ごとチェック
- **利点**: ツール起動中に新版が出ても検知できる
- **欠点**: セッション中に不要なネットワークトラフィック、複雑化、
  Maya パフォーマンスに微影響

#### 通知の抑制機能 (推奨: A/B どちらでも追加可)

- ダイアログの「このバージョンを無視」で選ばれたバージョンを
  `~/maya/.<tool>_ignored_version` もしくは Maya の `optionVar` に記録
- 次回起動時、記録より新しいバージョンが出るまで再通知しない

---

### 8-3. 追加すると便利な小機能

| 機能 | 概要 | 追加規模 (概算) |
|---|---|---|
| Update available バッジ | 「GitHub から更新」ボタン脇に赤い「!」マーク | ~15 行 |
| Changelog 表示 | GitHub Releases の body を UI にレンダリング | ~40 行 |
| 自動チェック ON/OFF | `optionVar` で永続化、初回起動時に選択 | ~30 行 |
| 「N 日ごと」チェック間隔設定 | 前回チェック時刻を保存、以降 N 日は skip | ~20 行 |

---

### 8-4. コード追加規模の目安

| 機能 | 追加コード行数 |
|---|---|
| Git tag 一覧取得 + バージョン選択ダイアログ (方式 A) | ~80 行 |
| Releases 経由のバージョン管理 (方式 B) | ~100 行 |
| 起動時アップデートチェック + 通知ダイアログ (方式 A) | ~60 行 |
| バックグラウンド定期チェック (方式 B) | ~90 行 |
| 無視設定の永続化 (optionVar) | ~30 行 |
| Changelog 表示 | ~40 行 |

すべて既存 §5-B の `my_tool.py` に追加する形。install.py 側は基本
そのままで、UI 側にバージョン選択ダイアログ / 通知ダイアログを
足す構成になる。

---

### 8-5. ユーザーへの質問テンプレ (LLM 用)

新ツール開発中に §8 機能が話題になったら、以下のような形で聞く:

```
以下のオプション機能があります。実装しますか?

【機能 1: バージョン管理・ロールバック】
過去バージョンを保存して、ユーザーが任意バージョンにダウングレード
できるようにする機能。3 方式:
  ・A: Git tag ベース (シンプル、tag を打つだけ、リリースノートなし)
  ・B: GitHub Releases ベース (Changelog 表示可、UI 操作が少し増える)
  ・C: リポジトリに versions/ フォルダで実ファイル保存 (完全セルフホスト、
    リポジトリ肥大)
→ どれにしますか? もしくは今は不要ですか?

【機能 2: 新版公開時のお知らせダイアログ】
GitHub に新版を push した時、ユーザーのツール起動時に通知。2 方式:
  ・A: 起動時に 1 回チェック (軽量、~30 行、セッション開始時のみ)
  ・B: 定期チェック (~60 行、セッション中も検知、通信・実装コスト増)
→ どれにしますか? もしくは今は不要ですか?

【追加小機能】: Update バッジ、Changelog 表示、自動チェック ON/OFF、
チェック間隔設定、無視バージョン記憶 — どれも独立に追加可。
必要なものだけ選んでください。
```

ユーザーの選択が確定してから、選ばれた機能だけ実装する。
