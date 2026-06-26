# -*- coding: utf-8 -*-
"""
OG Toonline Manager
===================
dx11Shader の MayaToonOutline.fx 相当の輪郭線を、Maya標準機能（実ジオメトリ）だけで再現。
inverted hull 方式（押し出し → 法線反転 → バックフェースカリング）。
元メッシュの変形に自動追従し、ライン単位で太さ・色・表示／非表示を管理できる。

主な機能:
    - 選択メッシュにアウトライン（ライン）を生成（生成ボタン／各グループ行の +追加）
    - ライン／グループをツリーで一覧表示（グループ行はバー表示）
    - ライン単位の太さ調整（選択したラインだけに適用）
    - カラー: 基本は共通ブラック。ラインごとに個別カラーも指定可
    - グループ単位／ライン単位の表示・非表示
    - グループの新規作成・ラインの移動・削除・再取得

使い方:
    Script Editor に貼り付けて実行、もしくは
        import OG_Toonline_Manager
        OG_Toonline_Manager.show()

詳細・残課題は toon_outline_handoff.md を参照。
"""
import maya.cmds as cmds
import maya.OpenMayaUI as omui
import maya.api.OpenMaya as om2

# ---- PySide6 / PySide2 両対応 ----
try:
    from PySide6 import QtWidgets, QtCore, QtGui
    from shiboken6 import wrapInstance
except ImportError:
    from PySide2 import QtWidgets, QtCore, QtGui
    from shiboken2 import wrapInstance

SHADER    = "toonOutline_SS"        # 共通（ブラック）サーフェスシェーダ
SG        = "toonOutline_SG"        # 共通シェーディンググループ
ROOT      = "toonOutlines_grp"      # 全ライン／グループの親
TAG       = "isToonOutline"         # ライン識別タグ
GROUP_TAG = "isToonOutlineGroup"    # グループ識別タグ
DEFAULT_GROUP = "Outline_Group1"
HANDLE_HOLDER = "toonOutline_handles"   # textureDeformerHandle 退避用の非表示ホルダー
CTRL_HOLDER   = "toonOutline_ctrls"     # 旧: ラインコントローラー格納グループ（互換掃除用）
LINE_HOLDER   = "Outline_grp"           # ライングループの格納グループ（ROOT 直下）
COL_PREFIX = "toonOutlineCol_"      # ライン個別カラーシェーダの接頭辞
THICK_TYPE = "textureDeformer"      # 太さ駆動ノードの型
THICK_ATTR = "offset"               # 現在法線方向への一定オフセット（太さ）

# ラインコントローラー（独立ノード）のアニメ用アトリビュート（全て英語）
CTRL_THICK  = "thickness"
CTRL_CURV   = "curvature"
CTRL_CAP    = "curvatureCap"
CTRL_SUFFIX = "_ctrl"                # コントローラー名 = <line>_ctrl
CTRL_LINK   = "toonCtrl"            # line 側の message 属性（→ controller）
GMULT       = "thicknessMult"        # グループ/全体コントローラーの太さ倍率アトリビュート
GLOBAL_CTRL = "toonOutline_globalCtrl"  # 全体コントローラー（コントローラー階層の親）
COL_GLOBAL  = (0.4, 0.8, 1.0)        # 全体=水色
COL_GROUP   = (0.55, 0.9, 0.2)       # グループ=黄緑
COL_LINE    = (1.0, 0.8, 0.0)        # ライン=黄

_CURV_CACHE = {}   # line名 -> 正規化曲率リスト（scriptJob 用・モジュールレベル）
_CURV_JOBS  = {}   # line名 -> [scriptJob id, ...]


def _maya_main():
    return wrapInstance(int(omui.MQtUtil.mainWindow()), QtWidgets.QWidget)


def _short(name):
    return name.split("|")[-1] if name else name


def _ensure_shader(color):
    """全アウトライン共有のフラットマテリアル（基本のブラック）を確保。"""
    if not cmds.objExists(SHADER):
        cmds.shadingNode("surfaceShader", asShader=True, name=SHADER)
        cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=SG)
        cmds.connectAttr(SHADER + ".outColor", SG + ".surfaceShader", f=True)
        cmds.setAttr(SHADER + ".outColor", color[0], color[1], color[2], type="double3")
    return SHADER, SG


def _ensure_root():
    if not cmds.objExists(ROOT):
        cmds.group(em=True, name=ROOT)
    return ROOT


def _compute_curvature(shape):
    """各頂点の符号付き曲率を [-1,1] に正規化して返す（凸 > 0 / 凹 < 0）。
    近傍平均との差（ラプラシアン/アンブレラ）を頂点法線へ投影して曲率とする。"""
    sl = om2.MSelectionList()
    sl.add(shape)
    dag = sl.getDagPath(0)
    mfn = om2.MFnMesh(dag)
    pts = mfn.getPoints(om2.MSpace.kObject)
    nrm = mfn.getVertexNormals(False, om2.MSpace.kObject)
    n = len(pts)
    curv = [0.0] * n
    itv = om2.MItMeshVertex(dag)
    while not itv.isDone():
        i = itv.index()
        conn = itv.getConnectedVertices()
        m = len(conn)
        if m > 0:
            ax = ay = az = 0.0
            for c in conn:
                p = pts[c]
                ax += p.x; ay += p.y; az += p.z
            ax /= m; ay /= m; az /= m
            pi = pts[i]
            lx = ax - pi.x; ly = ay - pi.y; lz = az - pi.z
            ni = nrm[i]
            # 凸面では近傍平均が法線の逆側 → 符号を反転して凸を正にする
            curv[i] = -(lx * ni.x + ly * ni.y + lz * ni.z)
        itv.next()
    mx = 0.0
    for c in curv:
        if abs(c) > mx:
            mx = abs(c)
    if mx > 1e-9:
        curv = [c / mx for c in curv]
    return curv


# ---- ラインコントローラー（アニメーション）まわり ----
def _line_deformer(line):
    nodes = cmds.ls(cmds.listHistory(line) or [], type=THICK_TYPE)
    return nodes[0] if nodes else None


def _line_src_shape(line):
    """曲率計算用の元メッシュ（deformer のベース入力）を返す。"""
    defm = _line_deformer(line)
    if defm:
        conn = cmds.listConnections(defm + ".input[0].inputGeometry",
                                    s=True, d=False, sh=True) or []
        if conn:
            return conn[0]
    sh = cmds.listRelatives(line, shapes=True, type="mesh", ni=True, f=True)
    return sh[0] if sh else None


def _line_curvature(line):
    curv = _CURV_CACHE.get(line)
    if curv is None:
        src = _line_src_shape(line)
        try:
            curv = _compute_curvature(src) if src else []
        except Exception:
            curv = []
        _CURV_CACHE[line] = curv
    return curv


def _attr_key_state(plug):
    """属性プラグのアニメ状態を返す: 'none' / 'anim'（アニメ有・キー上でない）/ 'key'（現フレームがキー）。"""
    try:
        kc = cmds.keyframe(plug, query=True, keyframeCount=True) or 0
    except Exception:
        kc = 0
    if kc <= 0:
        return "none"
    t = cmds.currentTime(query=True)
    try:
        at_t = cmds.keyframe(plug, query=True, time=(t, t)) or []
    except Exception:
        at_t = []
    return "key" if at_t else "anim"


def _ctrl_of(line):
    """ラインに紐づくコントローラー（独立ノード）を返す。無ければ None。"""
    if cmds.objExists(line) and cmds.attributeQuery(CTRL_LINK, node=line, exists=True):
        c = cmds.listConnections(line + "." + CTRL_LINK) or []
        if c and cmds.objExists(c[0]):
            return c[0]
    return None


def _set_outliner_color(node, rgb):
    try:
        cmds.setAttr(node + ".useOutlinerColor", 1)
        cmds.setAttr(node + ".outlinerColor", rgb[0], rgb[1], rgb[2], type="double3")
    except Exception:
        pass


def _lock_trs(node):
    for at in ("tx", "ty", "tz", "rx", "ry", "rz", "sx", "sy", "sz", "v"):
        try:
            cmds.setAttr(node + "." + at, lock=True, keyable=False, channelBox=False)
        except Exception:
            pass


def _ensure_mult_attr(node):
    if node and cmds.objExists(node) and not cmds.attributeQuery(GMULT, node=node, exists=True):
        try:
            cmds.addAttr(node, ln=GMULT, at="double", dv=1.0, min=0.0, keyable=True)
        except Exception:
            pass


def _line_group(line):
    par = cmds.listRelatives(line, parent=True, f=True) or []
    if par and cmds.attributeQuery(GROUP_TAG, node=par[0], exists=True):
        return par[0]
    return None


def _find_global_ctrl():
    if not cmds.objExists(ROOT):
        return None
    for c in cmds.listRelatives(ROOT, children=True, type="transform", f=True) or []:
        if _short(c) == GLOBAL_CTRL:
            return c
    return None


def _ensure_global_ctrl():
    """全体コントローラー（青・thicknessMult）を ROOT 直下に確保。コントローラー階層の親。"""
    _ensure_root()
    gc = None
    for c in cmds.listRelatives(ROOT, children=True, type="transform", f=True) or []:
        if _short(c) == GLOBAL_CTRL:
            gc = c
            break
    if gc is None:
        gc = cmds.createNode("transform", name=GLOBAL_CTRL)
        gc = cmds.parent(gc, ROOT)[0]
    _ensure_mult_attr(gc)
    _set_outliner_color(gc, COL_GLOBAL)
    _lock_trs(gc)
    return gc


def _ensure_group_ctrl(group):
    """グループコントローラー（黄緑・thicknessMult）を全体コントローラー配下に確保。"""
    gctrl_parent = _ensure_global_ctrl()
    gc = _ctrl_of(group)
    if gc is None or not cmds.objExists(gc):
        gc = cmds.createNode("transform", name=_short(group) + CTRL_SUFFIX)
        if not cmds.attributeQuery(CTRL_LINK, node=group, exists=True):
            cmds.addAttr(group, ln=CTRL_LINK, at="message")
        try:
            cmds.connectAttr(gc + ".message", group + "." + CTRL_LINK, f=True)
        except Exception:
            pass
    # 全体コントローラー配下へ
    par = cmds.listRelatives(gc, parent=True, f=True) or []
    if not par or _short(par[0]) != GLOBAL_CTRL:
        try:
            gc = cmds.parent(gc, gctrl_parent)[0]
        except Exception:
            pass
    _ensure_mult_attr(gc)
    _set_outliner_color(gc, COL_GROUP)
    _lock_trs(gc)
    return gc


def _create_line_ctrl(line, thick):
    """ライン用コントローラー（黄）を作成し、line と message で関連付ける（親子付けは呼び出し側）。"""
    ctrl = cmds.createNode("transform", name=_short(line) + CTRL_SUFFIX)
    for at, dv in ((CTRL_THICK, thick), (CTRL_CURV, 0.0), (CTRL_CAP, 3.0)):
        cmds.addAttr(ctrl, ln=at, at="double", dv=dv, keyable=True)
    if not cmds.attributeQuery(CTRL_LINK, node=line, exists=True):
        cmds.addAttr(line, ln=CTRL_LINK, at="message")
    try:
        cmds.connectAttr(ctrl + ".message", line + "." + CTRL_LINK, f=True)
    except Exception:
        pass
    return ctrl


def _ensure_line_anim(line, default_thick=0.05):
    """コントローラー階層（全体>グループ>ライン）を確保し、太さ乗算チェーンと追従ジョブを張る。"""
    defm = _line_deformer(line)
    ctrl = _ctrl_of(line)
    if ctrl is None:
        thick = default_thick
        if defm:
            try:
                thick = cmds.getAttr(defm + ".offset")
            except Exception:
                pass
        ctrl = _create_line_ctrl(line, thick)
    # ラインコントローラーを 所属グループのコントローラー（無ければ全体コントローラー）配下へ
    grp = _line_group(line)
    parent_ctrl = _ensure_group_ctrl(grp) if grp else _ensure_global_ctrl()
    par = cmds.listRelatives(ctrl, parent=True, f=True) or []
    if not par or _short(par[0]) != _short(parent_ctrl):
        try:
            ctrl = cmds.parent(ctrl, parent_ctrl)[0]
        except Exception:
            pass
    _set_outliner_color(ctrl, COL_LINE)
    _lock_trs(ctrl)
    _ensure_thickness_chain(line)
    _ensure_follow(line)
    _ensure_curv_jobs(line)
    return ctrl


def _ensure_follow(line):
    """元オブジェクトのトランスフォーム移動にラインを追従させる（parent/scale 拘束）。
    outMesh は変形のみ追従するので、移動/回転/スケールは拘束で合わせる。"""
    if cmds.listConnections(line + ".translateX", s=True, d=False, type="parentConstraint"):
        return
    srcsh = _line_src_shape(line)
    if not srcsh:
        return
    par = cmds.listRelatives(srcsh, parent=True, f=True) or []
    if not par:
        return
    src = par[0]
    try:
        cmds.parentConstraint(src, line, maintainOffset=False)
        cmds.scaleConstraint(src, line, maintainOffset=False)
    except Exception:
        pass


def _connect(src, dst):
    """未接続のときだけ接続（既接続時の警告を避ける）。"""
    try:
        if not cmds.isConnected(src, dst):
            cmds.connectAttr(src, dst, f=True)
    except Exception:
        pass


def _ensure_thickness_chain(line):
    """offset = lineCtrl.thickness * groupCtrl.thicknessMult * globalCtrl.thicknessMult を DG で構築。"""
    defm = _line_deformer(line)
    ctrl = _ctrl_of(line)
    if not defm or not ctrl:
        return
    gctrl = _ensure_global_ctrl()
    grp = _line_group(line)
    grpctrl = _ensure_group_ctrl(grp) if grp else None
    base = _short(ctrl)
    mA = base + "_thkA"
    mB = base + "_thkB"
    if not cmds.objExists(mA):
        mA = cmds.createNode("multDoubleLinear", name=mA)
    if not cmds.objExists(mB):
        mB = cmds.createNode("multDoubleLinear", name=mB)
    # mA = lineCtrl.thickness * groupCtrl.thicknessMult
    _connect(ctrl + "." + CTRL_THICK, mA + ".input1")
    if grpctrl:
        if not cmds.isConnected(grpctrl + "." + GMULT, mA + ".input2"):
            for p in (cmds.listConnections(mA + ".input2", s=True, d=False, p=True) or []):
                try:
                    cmds.disconnectAttr(p, mA + ".input2")
                except Exception:
                    pass
            _connect(grpctrl + "." + GMULT, mA + ".input2")
    else:
        for p in (cmds.listConnections(mA + ".input2", s=True, d=False, p=True) or []):
            try:
                cmds.disconnectAttr(p, mA + ".input2")
            except Exception:
                pass
        try:
            cmds.setAttr(mA + ".input2", 1.0)
        except Exception:
            pass
    # mB = mA * globalCtrl.thicknessMult → offset
    _connect(mA + ".output", mB + ".input1")
    _connect(gctrl + "." + GMULT, mB + ".input2")
    _connect(mB + ".output", defm + ".offset")


def _update_curv_weights(line):
    """コントローラーの curvature / curvatureCap から頂点ウェイトを再計算。
    scriptJob からも呼ばれる（アニメーション時の追従用）。"""
    if not cmds.objExists(line):
        return
    defm = _line_deformer(line)
    ctrl = _ctrl_of(line)
    if not defm or not ctrl:
        return
    try:
        influence = cmds.getAttr(ctrl + "." + CTRL_CURV)
        cap = cmds.getAttr(ctrl + "." + CTRL_CAP)
    except Exception:
        return
    curv = _line_curvature(line)
    n = len(curv)
    if n == 0:
        return
    weights = [min(cap, max(0.0, 1.0 + influence * abs(c))) for c in curv]
    try:
        cmds.setAttr(defm + ".weightList[0].weights[0:{}]".format(n - 1), *weights)
    except Exception:
        pass


def _ensure_curv_jobs(line):
    """controller の curvature / curvatureCap 変化で頂点ウェイトを再計算する scriptJob。
    （ノード削除時に自動 kill される attributeChange ジョブ）"""
    ctrl = _ctrl_of(line)
    if not ctrl:
        return
    old = _CURV_JOBS.get(line)
    if old:
        try:
            if all(cmds.scriptJob(exists=j) for j in old):
                return
        except Exception:
            pass
        for j in old:
            try:
                if cmds.scriptJob(exists=j):
                    cmds.scriptJob(kill=j, force=True)
            except Exception:
                pass
    jobs = []
    for at in (CTRL_CURV, CTRL_CAP):
        try:
            jid = cmds.scriptJob(attributeChange=[ctrl + "." + at,
                                                  lambda ln=line: _update_curv_weights(ln)])
            jobs.append(jid)
        except Exception:
            pass
    _CURV_JOBS[line] = jobs


class _OutlineTree(QtWidgets.QTreeWidget):
    """ライン→グループのドラッグ&ドロップ移動に対応したツリー。"""
    def __init__(self, ui, parent=None):
        super(_OutlineTree, self).__init__(parent)
        self.ui = ui

    def keyPressEvent(self, event):
        if event.key() in (QtCore.Qt.Key_Delete, QtCore.Qt.Key_Backspace):
            self.ui.delete_selected()
            event.accept()
            return
        super(_OutlineTree, self).keyPressEvent(event)

    def dropEvent(self, event):
        # ドロップ先のグループを解決し、選択ライン群を Maya 側で親子付けし直す。
        try:
            pos = event.position().toPoint()   # PySide6
        except AttributeError:
            pos = event.pos()                  # PySide2
        target = self.itemAt(pos)
        group = self.ui._group_of_item(target)
        lines = self.ui._selected_lines()
        if group and lines:
            self.ui._move_lines_to(lines, group)
        try:
            event.acceptProposedAction()
        except Exception:
            event.accept()


class ToonOutlineUI(QtWidgets.QDialog):

    def __init__(self, parent=None):
        if parent is None:
            parent = _maya_main()
        super(ToonOutlineUI, self).__init__(parent)
        self.setWindowTitle("OG_Toonline_Manager")
        self.setMinimumWidth(380)
        self.setMinimumHeight(460)
        self._color = [0.0, 0.0, 0.0]
        self._populating = False       # ツリー再構築中のシグナル抑止フラグ
        self._dragging = False         # スライダードラッグ中（undoチャンク制御）
        self._time_job = None          # timeChanged scriptJob
        self._build()
        self.refresh_tree()
        self._update_panels()      # 初期は選択無し → ライン/グループパネル非表示
        self._set_mult_widgets()
        try:
            self._time_job = cmds.scriptJob(event=["timeChanged", self._on_time_changed],
                                            protected=False)
        except Exception:
            self._time_job = None

    def closeEvent(self, event):
        try:
            if self._time_job and cmds.scriptJob(exists=self._time_job):
                cmds.scriptJob(kill=self._time_job, force=True)
        except Exception:
            pass
        super(ToonOutlineUI, self).closeEvent(event)

    # ========== UI 構築 ==========
    def _slider_spin_row(self, label, smin, smax, sval, dec, rmin, rmax, step, rval,
                         on_slider, on_spin, on_reset, on_key=None, tip=""):
        """ラベル+スライダー+スピン+[キー]+[リセット]の1行を作り (layout, slider, spin) を返す。"""
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel(label))
        sld = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        sld.setRange(smin, smax); sld.setValue(sval)
        spn = QtWidgets.QDoubleSpinBox()
        spn.setDecimals(dec); spn.setRange(rmin, rmax); spn.setSingleStep(step); spn.setValue(rval)
        if tip:
            spn.setToolTip(tip)
        sld.valueChanged.connect(on_slider)
        sld.sliderPressed.connect(self._begin_drag)
        sld.sliderReleased.connect(self._end_drag)
        spn.valueChanged.connect(on_spin)
        row.addWidget(sld); row.addWidget(spn)
        if on_key:
            bk = QtWidgets.QPushButton("K"); bk.setFixedWidth(22)
            bk.setToolTip("この値を現フレームにキー")
            bk.clicked.connect(on_key)
            row.addWidget(bk)
        b = QtWidgets.QPushButton("↺"); b.setFixedWidth(26); b.setToolTip("リセット")
        b.clicked.connect(on_reset)
        row.addWidget(b)
        return row, sld, spn

    def _build(self):
        lay = QtWidgets.QVBoxLayout(self)

        # 全体倍率（常に最上部）
        self.w_global = QtWidgets.QWidget()
        gl = QtWidgets.QVBoxLayout(self.w_global); gl.setContentsMargins(0, 0, 0, 0)
        arow, self.gslider, self.gspin = self._slider_spin_row(
            "全体倍率", 0, 500, 100, 2, 0.0, 5.0, 0.05, 1.0,
            self._on_gslider, self._on_gspin, self._reset_gmult,
            on_key=self._key_gmult, tip="全アウトラインの太さ倍率")
        gl.addLayout(arow)
        lay.addWidget(self.w_global)

        # 生成 / 新規グループ / 再取得（上部）
        crow = QtWidgets.QHBoxLayout()
        self.btn_create = QtWidgets.QPushButton("選択メッシュに輪郭を生成")
        self.btn_create.clicked.connect(self.create_outlines)
        crow.addWidget(self.btn_create, 1)
        b_grp = QtWidgets.QPushButton("新規グループ")
        b_grp.clicked.connect(self.new_group)
        crow.addWidget(b_grp)
        b_ref = QtWidgets.QPushButton("再取得")
        b_ref.clicked.connect(self.refresh_tree)
        crow.addWidget(b_ref)
        lay.addLayout(crow)

        # 対象グループ（生成先）
        crow2 = QtWidgets.QHBoxLayout()
        crow2.addWidget(QtWidgets.QLabel("対象グループ"))
        self.group_combo = QtWidgets.QComboBox()
        self.group_combo.setMinimumWidth(110)
        crow2.addWidget(self.group_combo, 1)
        lay.addLayout(crow2)

        # ライン／グループ ツリー（チェック=表示、色列=個別カラー、D&Dでグループ移動）
        self.tree = _OutlineTree(self)
        self.tree.setHeaderLabels(["名前 (チェック=表示)", "太さ", "色"])
        self.tree.setColumnWidth(0, 230)
        self.tree.setColumnWidth(1, 60)
        self.tree.setColumnWidth(2, 40)
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.tree.setDragEnabled(True)
        self.tree.viewport().setAcceptDrops(True)
        self.tree.setDropIndicatorShown(True)
        self.tree.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        self.tree.setExpandsOnDoubleClick(False)   # ダブルクリックはリネーム用
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.itemSelectionChanged.connect(self._on_tree_selection)
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        lay.addWidget(self.tree, 1)
        self.lbl_dd = QtWidgets.QLabel("※ ラインをグループへドラッグ&ドロップで移動。ダブルクリックで名前変更")
        self.lbl_dd.setStyleSheet("color:#888;")
        lay.addWidget(self.lbl_dd)

        # ライン用パネル（ライン選択時のみ表示）: 太さ / 曲率起伏 / 曲率上限
        self.w_line = QtWidgets.QWidget()
        lvl = QtWidgets.QVBoxLayout(self.w_line); lvl.setContentsMargins(0, 0, 0, 0)
        trow, self.slider, self.spin = self._slider_spin_row(
            "太さ", 0, 2000, 50, 3, 0.0, 2.0, 0.01, 0.05,
            self._on_slider, self._on_spin, self._reset_thickness,
            on_key=self._key_thickness)
        lvl.addLayout(trow)
        c2row, self.cslider, self.cspin = self._slider_spin_row(
            "曲率起伏", 0, 1000, 0, 2, 0.0, 10.0, 0.1, 0.0,
            self._on_cslider, self._on_cspin, self._reset_curv,
            on_key=self._key_curv)
        lvl.addLayout(c2row)
        c3row, self.cap_slider, self.cap_spin = self._slider_spin_row(
            "曲率上限", 100, 1000, 300, 1, 1.0, 10.0, 0.5, 3.0,
            self._on_cap_slider, self._on_cap_spin, self._reset_cap,
            on_key=self._key_cap, tip="起伏の最大倍率（角が太くなりすぎないよう上限を設定）")
        lvl.addLayout(c3row)
        lay.addWidget(self.w_line)

        # グループ用パネル（グループ選択時のみ表示）: グループ倍率
        self.w_group = QtWidgets.QWidget()
        gvl = QtWidgets.QVBoxLayout(self.w_group); gvl.setContentsMargins(0, 0, 0, 0)
        grow, self.grpslider, self.grpspin = self._slider_spin_row(
            "グループ倍率", 0, 500, 100, 2, 0.0, 5.0, 0.05, 1.0,
            self._on_grpslider, self._on_grpspin, self._reset_grpmult,
            on_key=self._key_grpmult, tip="選択グループの太さ倍率")
        gvl.addLayout(grow)
        lay.addWidget(self.w_group)

        self.lbl_hint = QtWidgets.QLabel("※ 値はツリーで選択したライン/グループに適用されます")
        self.lbl_hint.setStyleSheet("color:#888;")
        lay.addWidget(self.lbl_hint)

        # カラー
        clrow = QtWidgets.QHBoxLayout()
        clrow.addWidget(QtWidgets.QLabel("共通カラー"))
        self.btn_color = QtWidgets.QPushButton()
        self.btn_color.setFixedHeight(22)
        self.btn_color.setToolTip("共通カラー（基本のブラック）を変更。共通カラーのライン全部に効く")
        self.btn_color.clicked.connect(self._pick_color)
        self._refresh_swatch()
        clrow.addWidget(self.btn_color, 1)
        b_icol = QtWidgets.QPushButton("選択に個別カラー")
        b_scol = QtWidgets.QPushButton("選択を共通へ")
        b_icol.clicked.connect(self.set_individual_color)
        b_scol.clicked.connect(self.reset_to_shared_color)
        clrow.addWidget(b_icol)
        clrow.addWidget(b_scol)
        lay.addLayout(clrow)

        self.lbl_del = QtWidgets.QLabel("※ ライン/グループの削除は Delete キー")
        self.lbl_del.setStyleSheet("color:#888;")
        lay.addWidget(self.lbl_del)

    # ========== シーン走査ヘルパ ==========
    def _ensure_line_holder(self):
        """ライングループの格納グループ（ROOT 直下 Outline_grp）を確保。"""
        _ensure_root()
        for c in cmds.listRelatives(ROOT, children=True, type="transform", f=True) or []:
            if _short(c) == LINE_HOLDER:
                return c
        return cmds.group(em=True, name=LINE_HOLDER, parent=ROOT)

    def _find_line_holder(self):
        if not cmds.objExists(ROOT):
            return None
        for c in cmds.listRelatives(ROOT, children=True, type="transform", f=True) or []:
            if _short(c) == LINE_HOLDER:
                return c
        return None

    def _managed_groups(self):
        """ライングループ（GROUP_TAG 付き）。Outline_grp 配下。旧 ROOT 直下のものは移行する。
        ※ 空のときにホルダーを作らない（ツール起動だけでノードを作らないため）。"""
        if not cmds.objExists(ROOT):
            return []
        # 旧データ: ROOT 直下のグループがあれば Outline_grp へ移す
        direct = [t for t in (cmds.listRelatives(ROOT, children=True, type="transform", f=True) or [])
                  if cmds.attributeQuery(GROUP_TAG, node=t, exists=True)]
        if direct:
            holder = self._ensure_line_holder()
            for t in direct:
                try:
                    cmds.parent(t, holder)
                except Exception:
                    pass
        holder = self._find_line_holder()
        if not holder:
            return []
        out = []
        for t in cmds.listRelatives(holder, children=True, type="transform", f=True) or []:
            if cmds.attributeQuery(GROUP_TAG, node=t, exists=True):
                out.append(t)
        return out

    def _lines_in(self, group):
        """グループ直下のライン（TAG 付き）。"""
        out = []
        for t in cmds.listRelatives(group, children=True, type="transform", f=True) or []:
            if cmds.attributeQuery(TAG, node=t, exists=True):
                out.append(t)
        return out

    def _loose_lines(self):
        """Outline_grp 直下に直接ぶら下がっているライン（グループ未所属）。"""
        holder = self._find_line_holder()
        if not holder:
            return []
        out = []
        for t in cmds.listRelatives(holder, children=True, type="transform", f=True) or []:
            if cmds.attributeQuery(TAG, node=t, exists=True):
                out.append(t)
        return out

    def _shape_of(self, line):
        sh = cmds.listRelatives(line, shapes=True, type="mesh", ni=True, f=True)
        return sh[0] if sh else None

    def _thick_node_for(self, line):
        """ラインの太さ駆動ノード（textureDeformer）を返す。"""
        return _line_deformer(line)

    def _thickness_of(self, line):
        # 太さの真値はコントローラー属性 ctrl.thickness（offset を駆動）。
        ctrl = _ctrl_of(line)
        if ctrl:
            try:
                return cmds.getAttr(ctrl + "." + CTRL_THICK)
            except Exception:
                pass
        node = self._thick_node_for(line)
        if node and cmds.objExists(node):
            try:
                return cmds.getAttr(node + "." + THICK_ATTR)
            except Exception:
                pass
        return None

    def _line_color(self, line):
        """ラインに割り当てられた surfaceShader の outColor（[r,g,b]）。"""
        sh = self._shape_of(line)
        if not sh:
            return None
        sgs = cmds.listConnections(sh, type="shadingEngine") or []
        if not sgs:
            return None
        ss = cmds.listConnections(sgs[0] + ".surfaceShader") or []
        if not ss:
            return None
        try:
            c = cmds.getAttr(ss[0] + ".outColor")[0]
            return [c[0], c[1], c[2]]
        except Exception:
            return None

    def _ensure_group(self, name):
        """指定名のグループを Outline_grp 配下に確保して返す。"""
        for g in self._managed_groups():
            if _short(g) == name:
                return g
        grp = cmds.group(em=True, name=name, parent=self._ensure_line_holder())
        cmds.addAttr(grp, ln=GROUP_TAG, at="bool", dv=True)
        return grp

    def _current_group(self):
        name = self.group_combo.currentText().strip() if self.group_combo.count() else ""
        return self._ensure_group(name or DEFAULT_GROUP)

    def _tuck_handle(self, h):
        """ハンドルをアウトライナー＆ビューポートから隠す（グループは作らない・その場で隠す）。"""
        if not (h and cmds.objExists(h)):
            return
        for fn in (
            lambda: cmds.setAttr(h + ".hiddenInOutliner", 1),
            lambda: cmds.setAttr(h + ".visibility", 0),
        ):
            try:
                fn()
            except Exception:
                pass

    def _stash_loose_handles(self):
        """全 textureDeformerHandle をその場で隠す。旧ハンドルグループがあれば解体する。"""
        # 旧 toonOutline_handles グループを解体（中身をワールドへ出して削除）
        if cmds.objExists(HANDLE_HOLDER):
            for c in cmds.listRelatives(HANDLE_HOLDER, children=True, type="transform", f=True) or []:
                try:
                    cmds.parent(c, world=True)
                except Exception:
                    pass
            try:
                cmds.delete(HANDLE_HOLDER)
            except Exception:
                pass
        for h in cmds.ls("textureDeformerHandle*", type="transform") or []:
            self._tuck_handle(h)
        # アウトライナーを更新して hiddenInOutliner を反映
        try:
            import maya.mel as _mel
            _mel.eval("AEdagNodeCommonRefreshOutliners();")
        except Exception:
            pass

    def _cleanup_orphan_handles(self):
        """どの textureDeformer にも繋がっていないハンドルを削除する。"""
        for h in cmds.ls("textureDeformerHandle*", type="transform") or []:
            if not (cmds.listConnections(h, type="textureDeformer") or []):
                try:
                    cmds.delete(h)
                except Exception:
                    pass

    def _cleanup_orphan_ctrls(self):
        """リンク先（ライン/グループ）が消えたコントローラーと空の全体コントローラーを掃除。"""
        gc = _find_global_ctrl()
        if gc:
            orphans = []
            for c in cmds.listRelatives(gc, allDescendents=True, type="transform", f=True) or []:
                dest = cmds.listConnections(c + ".message", s=False, d=True) or []
                if not any(cmds.objExists(d) for d in dest):
                    orphans.append(c)
            for c in orphans:
                if cmds.objExists(c):
                    try:
                        cmds.delete(c)
                    except Exception:
                        pass
            if cmds.objExists(gc) and not (cmds.listRelatives(gc, children=True, type="transform") or []):
                try:
                    cmds.delete(gc)
                except Exception:
                    pass
        # 旧 toonOutline_ctrls が空で残っていれば掃除
        if cmds.objExists(CTRL_HOLDER) and not (cmds.listRelatives(CTRL_HOLDER, c=True) or []):
            try:
                cmds.delete(CTRL_HOLDER)
            except Exception:
                pass

    def _ensure_line_shader(self, line):
        """ラインの個別カラー用 surfaceShader/SG を確保。"""
        base = COL_PREFIX + _short(line)
        ss = base + "_SS"
        sg = base + "_SG"
        if not cmds.objExists(ss):
            ss = cmds.shadingNode("surfaceShader", asShader=True, name=ss)
        if not cmds.objExists(sg):
            sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=sg)
            cmds.connectAttr(ss + ".outColor", sg + ".surfaceShader", f=True)
        return ss, sg

    # ========== ツリー ==========
    def _add_line_item(self, parent_item, line):
        it = QtWidgets.QTreeWidgetItem([_short(line), "", ""])
        it.setData(0, QtCore.Qt.UserRole, line)
        # ライン: ドラッグ可・ドロップ不可（グループにのみ落とす）
        it.setFlags((it.flags() | QtCore.Qt.ItemIsUserCheckable
                     | QtCore.Qt.ItemIsDragEnabled) & ~QtCore.Qt.ItemIsDropEnabled)
        vis = True
        try:
            vis = bool(cmds.getAttr(line + ".visibility"))
        except Exception:
            pass
        it.setCheckState(0, QtCore.Qt.Checked if vis else QtCore.Qt.Unchecked)
        t = self._thickness_of(line)
        it.setText(1, "" if t is None else "{:.3f}".format(t))
        col = self._line_color(line)
        if col:
            r, g, b = [int(max(0.0, min(1.0, x)) * 255) for x in col]
            it.setBackground(2, QtGui.QBrush(QtGui.QColor(r, g, b)))
        parent_item.addChild(it)
        return it

    def _add_group_item(self, group_node, lines, visible):
        """展開式（プルダウン）のグループ項目を追加する。"""
        label = _short(group_node) if group_node else "(未分類)"
        gi = QtWidgets.QTreeWidgetItem([label, "", ""])
        if group_node:
            gi.setData(0, QtCore.Qt.UserRole, group_node)
            # グループ: チェック可・ドロップ可・自身はドラッグ不可
            gi.setFlags((gi.flags() | QtCore.Qt.ItemIsUserCheckable
                         | QtCore.Qt.ItemIsDropEnabled) & ~QtCore.Qt.ItemIsDragEnabled)
            gi.setCheckState(0, QtCore.Qt.Checked if visible else QtCore.Qt.Unchecked)
            # 太さ列にグループ倍率を表示
            grpctrl = _ensure_group_ctrl(group_node)
            gm = 1.0
            try:
                gm = cmds.getAttr(grpctrl + "." + GMULT)
            except Exception:
                pass
            gi.setText(1, "x{:.2f}".format(gm))
        else:
            gi.setFlags((gi.flags() | QtCore.Qt.ItemIsDropEnabled)
                        & ~QtCore.Qt.ItemIsUserCheckable & ~QtCore.Qt.ItemIsDragEnabled)
        f = gi.font(0); f.setBold(True); gi.setFont(0, f)
        gi.setBackground(0, QtGui.QBrush(QtGui.QColor(70, 78, 88)))
        self.tree.addTopLevelItem(gi)
        for line in lines:
            self._add_line_item(gi, line)
            # 既存ラインにもコントローラー・接続・追従ジョブを確保（旧データ/再開対応）
            _ensure_line_anim(line, self._thickness_of(line) or 0.05)
        gi.setExpanded(True)
        return gi

    def refresh_tree(self):
        self._populating = True
        self.tree.clear()
        for g in self._managed_groups():
            gvis = True
            try:
                gvis = bool(cmds.getAttr(g + ".visibility"))
            except Exception:
                pass
            self._add_group_item(g, self._lines_in(g), gvis)
        loose = self._loose_lines()
        if loose:
            self._add_group_item(None, loose, True)
        # 太さ列ヘッダーに全体倍率を表示
        gc = _find_global_ctrl()
        gv = 1.0
        if gc:
            try:
                gv = cmds.getAttr(gc + "." + GMULT)
            except Exception:
                pass
        self.tree.setHeaderLabels(["名前 (チェック=表示)", "太さ (全体x{:.2f})".format(gv), "色"])
        self._stash_loose_handles()   # はみ出したハンドルを退避
        self._populating = False
        self._refresh_group_combo()

    def _refresh_group_combo(self):
        cur = self.group_combo.currentText()
        self.group_combo.blockSignals(True)
        self.group_combo.clear()
        names = [_short(g) for g in self._managed_groups()]
        if not names:
            names = [DEFAULT_GROUP]
        self.group_combo.addItems(names)
        idx = self.group_combo.findText(cur)
        if idx >= 0:
            self.group_combo.setCurrentIndex(idx)
        self.group_combo.blockSignals(False)

    def _selected_nodes(self):
        out = []
        for it in self.tree.selectedItems():
            n = it.data(0, QtCore.Qt.UserRole)
            if n and cmds.objExists(n):
                out.append(n)
        return out

    def _selected_lines(self):
        return [n for n in self._selected_nodes()
                if cmds.attributeQuery(TAG, node=n, exists=True)]

    # ---- 表示・非表示（ライン項目のチェックボックス） ----
    def _on_item_changed(self, item, column):
        if self._populating or column != 0:
            return
        node = item.data(0, QtCore.Qt.UserRole)
        if node and cmds.objExists(node):
            vis = item.checkState(0) == QtCore.Qt.Checked
            try:
                cmds.setAttr(node + ".visibility", vis)
            except Exception:
                cmds.warning("{} の表示属性を変更できません（接続/ロック）".format(_short(node)))

    # ---- 選択変更 → 太さUIを同期 ----
    def _update_panels(self):
        """選択種別に応じてライン用/グループ用パネルの表示を切替（全体は常時表示）。"""
        nodes = self._selected_nodes()
        has_line = any(cmds.attributeQuery(TAG, node=n, exists=True) for n in nodes)
        has_group = any(cmds.attributeQuery(GROUP_TAG, node=n, exists=True) for n in nodes)
        self.w_line.setVisible(has_line)
        self.w_group.setVisible(has_group and not has_line)

    def _on_tree_selection(self):
        self._update_panels()
        self._set_mult_widgets()
        # 選択ノードのコントローラーを Maya 選択（タイムスライダにキー表示）
        ctrls = [_ctrl_of(n) for n in self._selected_nodes()]
        ctrls = [c for c in ctrls if c and cmds.objExists(c)]
        if ctrls:
            try:
                cmds.select(ctrls, r=True)
            except Exception:
                pass
        lines = self._selected_lines()
        if lines:
            ctrl = _ctrl_of(lines[0])
            t = self._thickness_of(lines[0])
            if t is not None:
                self._set_thickness_widgets(t)
            infl, cap = 0.0, None
            if ctrl:
                try:
                    infl = cmds.getAttr(ctrl + "." + CTRL_CURV)
                except Exception:
                    infl = 0.0
                try:
                    cap = cmds.getAttr(ctrl + "." + CTRL_CAP)
                except Exception:
                    cap = None
            self._set_curv_widgets(infl, cap)
        self._update_key_colors()

    def _set_thickness_widgets(self, val):
        self.slider.blockSignals(True); self.spin.blockSignals(True)
        self.spin.setValue(val)
        self.slider.setValue(int(val * 1000))
        self.slider.blockSignals(False); self.spin.blockSignals(False)

    # ========== undo 制御 ==========
    def _begin_drag(self):
        """スライダー押下：ドラッグ全体を1つの undo チャンクにまとめる。"""
        self._dragging = True
        cmds.undoInfo(openChunk=True)

    def _end_drag(self):
        cmds.undoInfo(closeChunk=True)
        self._dragging = False

    # ========== リセット ==========
    def _reset_thickness(self):
        self.spin.setValue(0.05)   # spin の valueChanged が適用＋slider同期する

    def _reset_curv(self):
        self.cspin.setValue(0.0)

    def _reset_cap(self):
        self.cap_spin.setValue(3.0)

    def _reset_grpmult(self):
        self.grpspin.setValue(1.0)

    def _reset_gmult(self):
        self.gspin.setValue(1.0)

    # ========== 太さ倍率（グループ / 全体） ==========
    def _target_group(self):
        """倍率の対象グループ: グループ選択中はそれ、ライン選択中はその所属グループ。"""
        for n in self._selected_nodes():
            if cmds.attributeQuery(GROUP_TAG, node=n, exists=True):
                return n
        for n in self._selected_nodes():
            if cmds.attributeQuery(TAG, node=n, exists=True):
                g = _line_group(n)
                if g:
                    return g
        return None

    def _on_grpslider(self, v):
        val = v / 100.0
        self.grpspin.blockSignals(True); self.grpspin.setValue(val); self.grpspin.blockSignals(False)
        self._apply_group_mult(val)

    def _on_grpspin(self, val):
        self.grpslider.blockSignals(True); self.grpslider.setValue(int(val * 100)); self.grpslider.blockSignals(False)
        self._apply_group_mult(val)

    def _apply_group_mult(self, val):
        grp = self._target_group()
        if not grp:
            return
        chunk = not self._dragging
        if chunk:
            cmds.undoInfo(openChunk=True)
        try:
            grpctrl = _ensure_group_ctrl(grp)
            try:
                cmds.setAttr(grpctrl + "." + GMULT, val)
            except Exception:
                pass
        finally:
            if chunk:
                cmds.undoInfo(closeChunk=True)
        self._update_mult_labels()

    def _on_gslider(self, v):
        val = v / 100.0
        self.gspin.blockSignals(True); self.gspin.setValue(val); self.gspin.blockSignals(False)
        self._apply_global_mult(val)

    def _on_gspin(self, val):
        self.gslider.blockSignals(True); self.gslider.setValue(int(val * 100)); self.gslider.blockSignals(False)
        self._apply_global_mult(val)

    def _apply_global_mult(self, val):
        chunk = not self._dragging
        if chunk:
            cmds.undoInfo(openChunk=True)
        try:
            gc = _ensure_global_ctrl()
            try:
                cmds.setAttr(gc + "." + GMULT, val)
            except Exception:
                pass
        finally:
            if chunk:
                cmds.undoInfo(closeChunk=True)
        self._update_mult_labels()

    def _update_mult_labels(self):
        """ツリーのグループ倍率（太さ列）と全体倍率（ヘッダー）を再描画（再構築せず）。"""
        self._populating = True
        gc = _find_global_ctrl()
        gv = 1.0
        if gc:
            try:
                gv = cmds.getAttr(gc + "." + GMULT)
            except Exception:
                pass
        self.tree.setHeaderLabels(["名前 (チェック=表示)", "太さ (全体x{:.2f})".format(gv), "色"])
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            gi = root.child(i)
            node = gi.data(0, QtCore.Qt.UserRole)
            if node and cmds.objExists(node) and cmds.attributeQuery(GROUP_TAG, node=node, exists=True):
                grpctrl = _ctrl_of(node)
                gm = 1.0
                if grpctrl:
                    try:
                        gm = cmds.getAttr(grpctrl + "." + GMULT)
                    except Exception:
                        pass
                gi.setText(1, "x{:.2f}".format(gm))
        self._populating = False

    def _set_mult_widgets(self):
        """選択に応じてグループ倍率・全体倍率スライダーを現在値へ同期。"""
        g = 1.0
        gc = _find_global_ctrl()
        if gc and cmds.attributeQuery(GMULT, node=gc, exists=True):
            try:
                g = cmds.getAttr(gc + "." + GMULT)
            except Exception:
                g = 1.0
        self.gspin.blockSignals(True); self.gslider.blockSignals(True)
        self.gspin.setValue(g); self.gslider.setValue(int(g * 100))
        self.gspin.blockSignals(False); self.gslider.blockSignals(False)
        grp = self._target_group()
        gm = 1.0
        grpctrl = _ctrl_of(grp) if grp else None
        if grpctrl and cmds.attributeQuery(GMULT, node=grpctrl, exists=True):
            try:
                gm = cmds.getAttr(grpctrl + "." + GMULT)
            except Exception:
                gm = 1.0
        self.grpspin.blockSignals(True); self.grpslider.blockSignals(True)
        self.grpspin.setValue(gm); self.grpslider.setValue(int(gm * 100))
        self.grpspin.blockSignals(False); self.grpslider.blockSignals(False)

    # ========== 太さ（選択ラインのみ） ==========
    def _on_slider(self, v):
        val = v / 1000.0
        self.spin.blockSignals(True); self.spin.setValue(val); self.spin.blockSignals(False)
        self._apply_thickness(val)

    def _on_spin(self, val):
        self.slider.blockSignals(True); self.slider.setValue(int(val * 1000)); self.slider.blockSignals(False)
        self._apply_thickness(val)

    def _apply_thickness(self, val):
        lines = self._selected_lines()
        if not lines:
            return
        chunk = not self._dragging   # ドラッグ中は _begin/_end_drag のチャンクに含める
        if chunk:
            cmds.undoInfo(openChunk=True)
        try:
            for line in lines:
                # コントローラー属性 thickness を設定（offset へ接続済みなので反映される）
                ctrl = _ensure_line_anim(line, val)
                try:
                    cmds.setAttr(ctrl + "." + CTRL_THICK, val)
                except Exception:
                    pass
        finally:
            if chunk:
                cmds.undoInfo(closeChunk=True)
        self._update_key_colors()
        # ツリーの太さ表示を更新
        self._populating = True
        for it in self.tree.selectedItems():
            n = it.data(0, QtCore.Qt.UserRole)
            if n and cmds.attributeQuery(TAG, node=n, exists=True):
                it.setText(1, "{:.3f}".format(val))
        self._populating = False

    # ========== 曲率起伏（選択ラインのみ） ==========
    def _on_cslider(self, v):
        val = v / 100.0
        self.cspin.blockSignals(True); self.cspin.setValue(val); self.cspin.blockSignals(False)
        self._apply_curvature()

    def _on_cspin(self, val):
        self.cslider.blockSignals(True); self.cslider.setValue(int(val * 100)); self.cslider.blockSignals(False)
        self._apply_curvature()

    def _on_cap_slider(self, v):
        val = v / 100.0
        self.cap_spin.blockSignals(True); self.cap_spin.setValue(val); self.cap_spin.blockSignals(False)
        self._apply_curvature()

    def _on_cap_spin(self, val):
        self.cap_slider.blockSignals(True); self.cap_slider.setValue(int(val * 100)); self.cap_slider.blockSignals(False)
        self._apply_curvature()

    def _set_curv_widgets(self, infl, cap=None):
        self.cslider.blockSignals(True); self.cspin.blockSignals(True)
        self.cspin.setValue(infl)
        self.cslider.setValue(int(infl * 100))
        self.cslider.blockSignals(False); self.cspin.blockSignals(False)
        if cap is not None:
            self.cap_spin.blockSignals(True); self.cap_slider.blockSignals(True)
            self.cap_spin.setValue(cap)
            self.cap_slider.setValue(int(cap * 100))
            self.cap_spin.blockSignals(False); self.cap_slider.blockSignals(False)

    def _apply_curvature(self):
        lines = self._selected_lines()
        if not lines:
            return
        influence = self.cspin.value()
        cap = self.cap_spin.value()
        chunk = not self._dragging   # ドラッグ中は _begin/_end_drag のチャンクに含める
        if chunk:
            cmds.undoInfo(openChunk=True)
        try:
            for line in lines:
                # コントローラー属性 curvature / curvatureCap を設定 → scriptJob で頂点
                #   ウェイトが再計算されるが、即時反映のため明示的にも更新する。
                ctrl = _ensure_line_anim(line, self.spin.value())
                try:
                    cmds.setAttr(ctrl + "." + CTRL_CURV, influence)
                    cmds.setAttr(ctrl + "." + CTRL_CAP, cap)
                except Exception:
                    pass
                _update_curv_weights(line)
        finally:
            if chunk:
                cmds.undoInfo(closeChunk=True)
        self._update_key_colors()

    # ========== キー（アニメーション） ==========
    def _key_line_attr(self, attr):
        lines = self._selected_lines()
        if not lines:
            cmds.warning("キーを打つラインをツリーで選択してください"); return
        cmds.undoInfo(openChunk=True)
        try:
            for line in lines:
                ctrl = _ensure_line_anim(line, self.spin.value())
                try:
                    cmds.setKeyframe(ctrl + "." + attr)
                except Exception:
                    pass
        finally:
            cmds.undoInfo(closeChunk=True)
        self._update_key_colors()

    def _key_thickness(self):
        self._key_line_attr(CTRL_THICK)

    def _key_curv(self):
        self._key_line_attr(CTRL_CURV)

    def _key_cap(self):
        self._key_line_attr(CTRL_CAP)

    def _key_grpmult(self):
        grp = self._target_group()
        if not grp:
            cmds.warning("キーを打つグループをツリーで選択してください"); return
        gctrl = _ensure_group_ctrl(grp)
        cmds.undoInfo(openChunk=True)
        try:
            try:
                cmds.setKeyframe(gctrl + "." + GMULT)
            except Exception:
                pass
        finally:
            cmds.undoInfo(closeChunk=True)
        self._update_key_colors()

    def _key_gmult(self):
        gc = _ensure_global_ctrl()
        cmds.undoInfo(openChunk=True)
        try:
            try:
                cmds.setKeyframe(gc + "." + GMULT)
            except Exception:
                pass
        finally:
            cmds.undoInfo(closeChunk=True)
        self._update_key_colors()

    # ---- 数値欄のキー色（アトリビュートエディター風） ----
    def _style_spin(self, spin, state):
        if state == "key":
            css = "QDoubleSpinBox{background-color:#c25450; color:#fff;}"        # 赤=現フレームがキー
        elif state == "anim":
            css = "QDoubleSpinBox{background-color:#e0a6a6; color:#000;}"        # ピンク=アニメ有り（黒文字）
        else:
            css = ""
        spin.setStyleSheet(css)

    def _update_key_colors(self):
        lines = self._selected_lines()
        lctrl = _ctrl_of(lines[0]) if lines else None
        for spin, at in ((self.spin, CTRL_THICK), (self.cspin, CTRL_CURV), (self.cap_spin, CTRL_CAP)):
            state = "none"
            if lctrl and cmds.attributeQuery(at, node=lctrl, exists=True):
                state = _attr_key_state(lctrl + "." + at)
            self._style_spin(spin, state)
        # グループ倍率
        grp = self._target_group()
        grpctrl = _ctrl_of(grp) if grp else None
        st = "none"
        if grpctrl and cmds.attributeQuery(GMULT, node=grpctrl, exists=True):
            st = _attr_key_state(grpctrl + "." + GMULT)
        self._style_spin(self.grpspin, st)
        # 全体倍率
        gc = _find_global_ctrl()
        st = "none"
        if gc and cmds.attributeQuery(GMULT, node=gc, exists=True):
            st = _attr_key_state(gc + "." + GMULT)
        self._style_spin(self.gspin, st)

    def _on_time_changed(self):
        """フレーム変更時：選択中の値をスライダーへ反映し、キー色を更新（ライン/グループ/全体）。"""
        lines = self._selected_lines()
        if lines:
            t = self._thickness_of(lines[0])
            if t is not None:
                self._set_thickness_widgets(t)   # blockSignals 済み → 適用は走らない
            ctrl = _ctrl_of(lines[0])
            if ctrl:
                infl = cap = None
                try:
                    infl = cmds.getAttr(ctrl + "." + CTRL_CURV)
                except Exception:
                    pass
                try:
                    cap = cmds.getAttr(ctrl + "." + CTRL_CAP)
                except Exception:
                    pass
                if infl is not None:
                    self._set_curv_widgets(infl, cap)
        # グループ/全体倍率の値・キー色もラインと同様に追従
        self._set_mult_widgets()
        self._update_mult_labels()
        self._update_key_colors()

    # ========== カラー ==========
    def _refresh_swatch(self):
        r, g, b = [int(c * 255) for c in self._color]
        self.btn_color.setStyleSheet(
            "background-color: rgb({},{},{}); border:1px solid #222;".format(r, g, b))

    def _pick_color(self):
        c0 = QtGui.QColor.fromRgbF(*self._color)
        c = QtWidgets.QColorDialog.getColor(c0, self, "共通カラー")
        if c.isValid():
            self._color = [c.redF(), c.greenF(), c.blueF()]
            self._refresh_swatch()
            if cmds.objExists(SHADER):
                cmds.undoInfo(openChunk=True)
                try:
                    cmds.setAttr(SHADER + ".outColor",
                                 self._color[0], self._color[1], self._color[2],
                                 type="double3")
                finally:
                    cmds.undoInfo(closeChunk=True)
            self.refresh_tree()

    def set_individual_color(self):
        """選択ラインに個別カラーを割り当てる（専用シェーダを生成）。"""
        lines = self._selected_lines()
        if not lines:
            cmds.warning("色を変えるラインをツリーで選択してください"); return
        init = self._line_color(lines[0]) or [0.0, 0.0, 0.0]
        c = QtWidgets.QColorDialog.getColor(QtGui.QColor.fromRgbF(*init), self, "ラインの個別カラー")
        if not c.isValid():
            return
        rgb = [c.redF(), c.greenF(), c.blueF()]
        cmds.undoInfo(openChunk=True)
        try:
            for line in lines:
                sh = self._shape_of(line)
                if not sh:
                    continue
                ss, sg = self._ensure_line_shader(line)
                cmds.setAttr(ss + ".outColor", rgb[0], rgb[1], rgb[2], type="double3")
                cmds.sets(sh, e=True, forceElement=sg)
        finally:
            cmds.undoInfo(closeChunk=True)
        self.refresh_tree()

    def reset_to_shared_color(self):
        """選択ラインを共通カラー（ブラック）に戻し、専用シェーダを片付ける。"""
        lines = self._selected_lines()
        if not lines:
            cmds.warning("共通カラーに戻すラインをツリーで選択してください"); return
        _, sg = _ensure_shader(self._color)
        cmds.undoInfo(openChunk=True)
        try:
            for line in lines:
                sh = self._shape_of(line)
                if sh:
                    cmds.sets(sh, e=True, forceElement=sg)
                base = COL_PREFIX + _short(line)
                for n in (base + "_SG", base + "_SS"):
                    if cmds.objExists(n):
                        try:
                            cmds.delete(n)
                        except Exception:
                            pass
        finally:
            cmds.undoInfo(closeChunk=True)
        self.refresh_tree()

    # ========== 生成 ==========
    def create_outlines(self, *args, **kwargs):
        # target_group が来ればそのグループへ、無ければコンボの対象グループへ
        target_group = kwargs.get("target_group", None)
        sel = cmds.ls(sl=True, long=True, type="transform")
        if not sel:
            cmds.warning("メッシュを選択してください"); return
        thick = self.spin.value()
        _, sg = _ensure_shader(self._color)
        if target_group and cmds.objExists(target_group):
            grp = target_group
        else:
            grp = self._current_group()

        cmds.undoInfo(openChunk=True)
        made = []
        try:
            for obj in sel:
                shps = cmds.listRelatives(obj, shapes=True, type="mesh", ni=True, f=True)
                if not shps:
                    continue
                src = shps[0]

                # 複製は元と同じ位置（同じ親・同じ TRS）に出る。トランスフォームは動かさない。
                # → 元と同じ変換空間に留まるので原点に飛ばない。
                dup = cmds.duplicate(obj, name=_short(obj) + "_outline", rr=True)[0]
                for k in cmds.listRelatives(dup, children=True, type="transform", f=True) or []:
                    cmds.delete(k)
                dshape = cmds.listRelatives(dup, shapes=True, type="mesh", ni=True, f=True)[0]

                # 複製のヒストリ／デフォーマを除去して inMesh を空ける
                cmds.delete(dup, constructionHistory=True)

                # --- inverted hull を構築 ---
                # 1) textureDeformer(direction="Normal") で各頂点を「現在のサーフェス法線」方向へ
                #    一定距離(offset)オフセット。direction 既定の "Handle" だとハンドル軸(Y)にしか
                #    動かないため必ず "Normal" を指定する。法線は変形後メッシュから毎フレーム
                #    再計算されるので、元メッシュを変形しても太さ(offset 距離)は一定に保たれる
                #    （blendShape はバインド時固定デルタで変形すると太さが変わるため不使用）。
                #    strength=0・テクスチャ無しで、純粋な法線方向の一定オフセットだけにする。
                #    まず静的な複製に付け、その後ベース入力へ outMesh を流して追従させる
                #    （先に inMesh へ直結すると評価が壊れて歪むので繋がない）。
                td = cmds.textureDeformer(dshape, strength=0, offset=thick, direction="Normal")
                defm = td[0]
                # ハンドルはデフォーマに接続された transform として特定する
                # （戻り値に含まれない版があるため接続から拾う。方向フィルタは付けない）。
                handle = None
                for c in (cmds.listConnections(defm, type="transform") or []):
                    if "textureDeformerHandle" in _short(c):
                        handle = c
                        break
                if handle is None and len(td) > 1:
                    handle = td[1]

                # 2) 変形追従: deformer のベース入力に 元の outMesh を流し込む。
                try:
                    cmds.connectAttr(src + ".outMesh", defm + ".input[0].inputGeometry", f=True)
                except Exception:
                    cmds.warning("変形追従の接続に失敗（静的な輪郭として生成）")

                # deformer ハンドルは direction="Normal" では不要だが削除すると offset が
                # 効かなくなるため、アウトライナーから隠してホルダーへ退避する。
                self._tuck_handle(handle)

                # 3) 法線反転は shape の opposite 属性で行う（ヒストリノードを足さない）。
                #    doubleSided=0 のバックフェースカリングと合わせて輪郭のリムだけ見せる。
                cmds.setAttr(dshape + ".doubleSided", 0)
                try:
                    cmds.setAttr(dshape + ".opposite", 1)
                except Exception:
                    pass
                cmds.sets(dshape, e=True, forceElement=sg)
                if not cmds.attributeQuery(TAG, node=dup, exists=True):
                    cmds.addAttr(dup, ln=TAG, at="bool", dv=True)

                # グループへ（ワールド位置を保持したまま移動 → 重なりは維持）
                dup = cmds.parent(dup, grp)[0]

                # ラインコントローラー（独立ノード）を作成。太さは offset へ直結（DGでアニメ可）、
                # 曲率/上限は scriptJob で頂点ウェイトを追従。
                ctrl = _ensure_line_anim(dup, thick)
                try:
                    cmds.setAttr(ctrl + "." + CTRL_THICK, thick)
                except Exception:
                    pass
                made.append(dup)
            if made:
                cmds.select(made, r=True)
            # 取りこぼしたハンドルがあればトップから退避（確実化）
            self._stash_loose_handles()
        finally:
            cmds.undoInfo(closeChunk=True)

        self.refresh_tree()

    # ========== リネーム ==========
    def rename_node(self, node):
        """ライン／グループのノードをリネームする。"""
        if not node or not cmds.objExists(node):
            return
        old = _short(node)
        new, ok = QtWidgets.QInputDialog.getText(self, "リネーム", "新しい名前:", text=old)
        new = (new or "").strip()
        if not ok or not new or new == old:
            return
        cmds.undoInfo(openChunk=True)
        try:
            cmds.rename(node, new)
        except Exception:
            cmds.warning("リネームに失敗しました: {}".format(new))
        finally:
            cmds.undoInfo(closeChunk=True)
        self.refresh_tree()

    def _on_double_click(self, item, column):
        """ライン・グループともダブルクリックでリネーム。"""
        node = item.data(0, QtCore.Qt.UserRole)
        if not node or not cmds.objExists(node):
            return
        if (cmds.attributeQuery(TAG, node=node, exists=True)
                or cmds.attributeQuery(GROUP_TAG, node=node, exists=True)):
            self.rename_node(node)

    # ========== ドラッグ&ドロップ移動 ==========
    def _group_of_item(self, item):
        """ツリー項目から移動先グループ（ノード）を解決する。"""
        if item is None:
            return None
        node = item.data(0, QtCore.Qt.UserRole)
        if not node or not cmds.objExists(node):
            return None
        if cmds.attributeQuery(GROUP_TAG, node=node, exists=True):
            return node
        if cmds.attributeQuery(TAG, node=node, exists=True):
            par = cmds.listRelatives(node, parent=True, f=True) or []
            if par and cmds.attributeQuery(GROUP_TAG, node=par[0], exists=True):
                return par[0]
        return None

    def _move_lines_to(self, lines, group):
        if not group or not cmds.objExists(group):
            return
        cmds.undoInfo(openChunk=True)
        try:
            for ln in lines:
                try:
                    cmds.parent(ln, group)
                except Exception:
                    pass
        finally:
            cmds.undoInfo(closeChunk=True)
        self.refresh_tree()

    # ========== グループ管理 ==========
    def new_group(self):
        name, ok = QtWidgets.QInputDialog.getText(self, "新規グループ", "グループ名:")
        name = (name or "").strip()
        if not ok or not name:
            return
        self._ensure_group(name)
        self.refresh_tree()
        idx = self.group_combo.findText(name)
        if idx >= 0:
            self.group_combo.setCurrentIndex(idx)

    def delete_selected(self):
        nodes = self._selected_nodes()
        if not nodes:
            cmds.warning("削除する項目をツリーで選択してください"); return
        # 削除対象ライン（選択ライン＋選択グループ配下ライン）のコントローラー・乗算ノードも巻き込む
        def _gather(line):
            c = _ctrl_of(line)
            if c:
                victims.add(c)
                for s in ("_thkA", "_thkB"):
                    n2 = _short(c) + s
                    if cmds.objExists(n2):
                        victims.add(n2)

        victims = set(nodes)
        for n in list(nodes):
            if cmds.attributeQuery(TAG, node=n, exists=True):
                _gather(n)
            if cmds.attributeQuery(GROUP_TAG, node=n, exists=True):
                gc = _ctrl_of(n)          # グループコントローラーも削除
                if gc:
                    victims.add(gc)
                for d in cmds.listRelatives(n, ad=True, type="transform", f=True) or []:
                    if cmds.attributeQuery(TAG, node=d, exists=True):
                        _gather(d)
        cmds.undoInfo(openChunk=True)
        try:
            cmds.delete(list(victims))
            # 孤立したハンドル／コントローラー／空ホルダーを掃除
            self._cleanup_orphan_handles()
            self._cleanup_orphan_ctrls()
            if cmds.objExists(LINE_HOLDER) and not (cmds.listRelatives(LINE_HOLDER, c=True) or []):
                cmds.delete(LINE_HOLDER)
            # 空になった ROOT は片付ける
            if cmds.objExists(ROOT) and not (cmds.listRelatives(ROOT, c=True) or []):
                cmds.delete(ROOT)
        finally:
            cmds.undoInfo(closeChunk=True)
        self.refresh_tree()


_toon_win = None


def show():
    """ツールウィンドウを起動。既存ウィンドウがあれば閉じてから開く。"""
    global _toon_win
    try:
        _toon_win.close(); _toon_win.deleteLater()
    except Exception:
        pass
    _toon_win = ToonOutlineUI()
    _toon_win.show()
    return _toon_win


if __name__ == "__main__":
    show()
