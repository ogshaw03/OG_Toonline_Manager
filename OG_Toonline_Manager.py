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
COL_PREFIX = "toonOutlineCol_"      # ライン個別カラーシェーダの接頭辞
THICK_TYPE = "textureDeformer"      # 太さ駆動ノードの型
THICK_ATTR = "offset"               # 現在法線方向への一定オフセット（太さ）

# ラインコントローラー（line transform）のアニメ用アトリビュート（全て英語）
CTRL_THICK = "thickness"
CTRL_CURV  = "curvature"
CTRL_CAP   = "curvatureCap"

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


def _update_curv_weights(line):
    """line.curvature / line.curvatureCap から textureDeformer の頂点ウェイトを再計算。
    scriptJob からも呼ばれる（アニメーション時の追従用）。"""
    if not cmds.objExists(line):
        return
    defm = _line_deformer(line)
    if not defm:
        return
    try:
        influence = cmds.getAttr(line + "." + CTRL_CURV)
        cap = cmds.getAttr(line + "." + CTRL_CAP)
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


def _ensure_ctrl_attrs(line, thick):
    """ライン（コントローラー）にキーアブルな英語アトリビュートを追加。"""
    for at, dv in ((CTRL_THICK, thick), (CTRL_CURV, 0.0), (CTRL_CAP, 3.0)):
        if not cmds.attributeQuery(at, node=line, exists=True):
            cmds.addAttr(line, ln=at, at="double", dv=dv, keyable=True)


def _ensure_curv_jobs(line):
    """curvature / curvatureCap の変化で頂点ウェイトを再計算する scriptJob を確保。
    （ノード削除時に自動で kill される attributeChange ジョブ）"""
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
            jid = cmds.scriptJob(attributeChange=[line + "." + at,
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
        self._build()
        self.refresh_tree()

    # ========== UI 構築 ==========
    def _build(self):
        lay = QtWidgets.QVBoxLayout(self)

        # 生成 + 対象グループ
        crow = QtWidgets.QHBoxLayout()
        self.btn_create = QtWidgets.QPushButton("選択メッシュに輪郭を生成")
        self.btn_create.clicked.connect(self.create_outlines)
        crow.addWidget(self.btn_create, 1)
        crow.addWidget(QtWidgets.QLabel("グループ"))
        self.group_combo = QtWidgets.QComboBox()
        self.group_combo.setMinimumWidth(110)
        crow.addWidget(self.group_combo)
        lay.addLayout(crow)

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

        # 太さ（選択中ラインに適用）
        trow = QtWidgets.QHBoxLayout()
        trow.addWidget(QtWidgets.QLabel("太さ"))
        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.slider.setRange(0, 2000)
        self.slider.setValue(50)
        self.spin = QtWidgets.QDoubleSpinBox()
        self.spin.setDecimals(3)
        self.spin.setRange(0.0, 2.0)
        self.spin.setSingleStep(0.01)
        self.spin.setValue(0.05)
        self.slider.valueChanged.connect(self._on_slider)
        self.slider.sliderPressed.connect(self._begin_drag)
        self.slider.sliderReleased.connect(self._end_drag)
        self.spin.valueChanged.connect(self._on_spin)
        trow.addWidget(self.slider)
        trow.addWidget(self.spin)
        b_rt = QtWidgets.QPushButton("↺")
        b_rt.setFixedWidth(26); b_rt.setToolTip("太さをリセット (0.05)")
        b_rt.clicked.connect(self._reset_thickness)
        trow.addWidget(b_rt)
        lay.addLayout(trow)

        # 曲率起伏（元メッシュの曲率に応じて太さに起伏。0=一様 / 曲がる所ほど太く）
        c2row = QtWidgets.QHBoxLayout()
        c2row.addWidget(QtWidgets.QLabel("曲率起伏"))
        self.cslider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.cslider.setRange(0, 1000)
        self.cslider.setValue(0)
        self.cspin = QtWidgets.QDoubleSpinBox()
        self.cspin.setDecimals(2)
        self.cspin.setRange(0.0, 10.0)
        self.cspin.setSingleStep(0.1)
        self.cspin.setValue(0.0)
        self.cslider.valueChanged.connect(self._on_cslider)
        self.cslider.sliderPressed.connect(self._begin_drag)
        self.cslider.sliderReleased.connect(self._end_drag)
        self.cspin.valueChanged.connect(self._on_cspin)
        c2row.addWidget(self.cslider)
        c2row.addWidget(self.cspin)
        b_rc = QtWidgets.QPushButton("↺")
        b_rc.setFixedWidth(26); b_rc.setToolTip("曲率起伏をリセット (0.0)")
        b_rc.clicked.connect(self._reset_curv)
        c2row.addWidget(b_rc)
        lay.addLayout(c2row)

        # 曲率起伏の上限（最大倍率）。角(顎など)が太くなりすぎないよう制限。
        c3row = QtWidgets.QHBoxLayout()
        c3row.addWidget(QtWidgets.QLabel("曲率上限"))
        self.cap_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.cap_slider.setRange(100, 1000)   # /100 = 1.0〜10.0
        self.cap_slider.setValue(300)
        self.cap_spin = QtWidgets.QDoubleSpinBox()
        self.cap_spin.setDecimals(1)
        self.cap_spin.setRange(1.0, 10.0)
        self.cap_spin.setSingleStep(0.5)
        self.cap_spin.setValue(3.0)
        self.cap_spin.setToolTip("起伏の最大倍率（角が太くなりすぎないよう上限を設定）")
        self.cap_slider.valueChanged.connect(self._on_cap_slider)
        self.cap_slider.sliderPressed.connect(self._begin_drag)
        self.cap_slider.sliderReleased.connect(self._end_drag)
        self.cap_spin.valueChanged.connect(self._on_cap_spin)
        c3row.addWidget(self.cap_slider)
        c3row.addWidget(self.cap_spin)
        b_rcap = QtWidgets.QPushButton("↺")
        b_rcap.setFixedWidth(26); b_rcap.setToolTip("曲率上限をリセット (3.0)")
        b_rcap.clicked.connect(self._reset_cap)
        c3row.addWidget(b_rcap)
        lay.addLayout(c3row)

        self.lbl_hint = QtWidgets.QLabel("※ 太さ・曲率起伏・個別カラーはツリーで選択したラインに適用されます")
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

        # キー（選択ラインの thickness/curvature/curvatureCap を現フレームにキー）
        krow = QtWidgets.QHBoxLayout()
        self.btn_key = QtWidgets.QPushButton("現フレームにキー（選択ライン）")
        self.btn_key.setToolTip("選択ラインの太さ・曲率起伏・曲率上限を現在フレームにキー")
        self.btn_key.clicked.connect(self.key_selected)
        krow.addWidget(self.btn_key)
        lay.addLayout(krow)

        # 管理ボタン
        mrow = QtWidgets.QHBoxLayout()
        b_grp = QtWidgets.QPushButton("新規グループ")
        b_del = QtWidgets.QPushButton("削除")
        b_ref = QtWidgets.QPushButton("再取得")
        b_grp.clicked.connect(self.new_group)
        b_del.clicked.connect(self.delete_selected)
        b_ref.clicked.connect(self.refresh_tree)
        for b in (b_grp, b_del, b_ref):
            mrow.addWidget(b)
        lay.addLayout(mrow)

    # ========== シーン走査ヘルパ ==========
    def _managed_groups(self):
        """ROOT 直下のグループ（GROUP_TAG 付き）。"""
        if not cmds.objExists(ROOT):
            return []
        out = []
        for t in cmds.listRelatives(ROOT, children=True, type="transform", f=True) or []:
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
        """ROOT 直下に直接ぶら下がっているライン（グループ未所属）。"""
        if not cmds.objExists(ROOT):
            return []
        out = []
        for t in cmds.listRelatives(ROOT, children=True, type="transform", f=True) or []:
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
        # 太さの真値はコントローラー属性 line.thickness（offset を駆動）。
        if cmds.attributeQuery(CTRL_THICK, node=line, exists=True):
            try:
                return cmds.getAttr(line + "." + CTRL_THICK)
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
        """指定名のグループを ROOT 直下に確保して返す。"""
        _ensure_root()
        for g in self._managed_groups():
            if _short(g) == name:
                return g
        grp = cmds.group(em=True, name=name, parent=ROOT)
        cmds.addAttr(grp, ln=GROUP_TAG, at="bool", dv=True)
        return grp

    def _current_group(self):
        name = self.group_combo.currentText().strip() if self.group_combo.count() else ""
        return self._ensure_group(name or DEFAULT_GROUP)

    def _ensure_handle_holder(self):
        """textureDeformerHandle を格納する ROOT 直下の非表示ホルダー。
        ハンドルは変形対象メッシュの配下に親子付けできないため、ここへ退避する。"""
        _ensure_root()
        for c in cmds.listRelatives(ROOT, children=True, type="transform", f=True) or []:
            if _short(c) == HANDLE_HOLDER:
                return c
        h = cmds.group(em=True, name=HANDLE_HOLDER, parent=ROOT)
        try:
            cmds.setAttr(h + ".visibility", 0)
        except Exception:
            pass
        return h

    def _stash_loose_handles(self):
        """トップレベル（ワールド直下）に残った textureDeformerHandle をホルダーへ退避。"""
        loose = []
        for h in cmds.ls("textureDeformerHandle*", type="transform", long=True) or []:
            if not (cmds.listRelatives(h, parent=True) or []):
                loose.append(h)
        if not loose:
            return
        holder = self._ensure_handle_holder()
        for h in loose:
            try:
                cmds.setAttr(h + ".visibility", 0)
                cmds.parent(h, holder)
            except Exception:
                pass

    def _cleanup_orphan_handles(self):
        """どの textureDeformer にも繋がっていないハンドルとホルダーを掃除する。"""
        for h in cmds.ls("textureDeformerHandle*", type="transform") or []:
            if not (cmds.listConnections(h, type="textureDeformer") or []):
                try:
                    cmds.delete(h)
                except Exception:
                    pass
        if cmds.objExists(HANDLE_HOLDER) and not (cmds.listRelatives(HANDLE_HOLDER, c=True) or []):
            try:
                cmds.delete(HANDLE_HOLDER)
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
        else:
            gi.setFlags((gi.flags() | QtCore.Qt.ItemIsDropEnabled)
                        & ~QtCore.Qt.ItemIsUserCheckable & ~QtCore.Qt.ItemIsDragEnabled)
        f = gi.font(0); f.setBold(True); gi.setFont(0, f)
        gi.setBackground(0, QtGui.QBrush(QtGui.QColor(70, 78, 88)))
        self.tree.addTopLevelItem(gi)
        for line in lines:
            self._add_line_item(gi, line)
            # 既存ラインにもコントローラー属性・追従ジョブを確保（旧データ/再開対応）
            if not cmds.attributeQuery(CTRL_THICK, node=line, exists=True):
                _ensure_ctrl_attrs(line, self._thickness_of(line) or 0.05)
            _ensure_curv_jobs(line)
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
    def _on_tree_selection(self):
        lines = self._selected_lines()
        if not lines:
            return
        t = self._thickness_of(lines[0])
        if t is not None:
            self._set_thickness_widgets(t)
        infl = 0.0
        if cmds.attributeQuery(CTRL_CURV, node=lines[0], exists=True):
            try:
                infl = cmds.getAttr(lines[0] + "." + CTRL_CURV)
            except Exception:
                infl = 0.0
        cap = None
        if cmds.attributeQuery(CTRL_CAP, node=lines[0], exists=True):
            try:
                cap = cmds.getAttr(lines[0] + "." + CTRL_CAP)
            except Exception:
                cap = None
        self._set_curv_widgets(infl, cap)

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
                if cmds.attributeQuery(CTRL_THICK, node=line, exists=True):
                    try:
                        cmds.setAttr(line + "." + CTRL_THICK, val)
                    except Exception:
                        pass
                else:
                    node = self._thick_node_for(line)
                    if node and cmds.objExists(node):
                        try:
                            cmds.setAttr(node + "." + THICK_ATTR, val)
                        except Exception:
                            pass
        finally:
            if chunk:
                cmds.undoInfo(closeChunk=True)
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
                _ensure_ctrl_attrs(line, self.spin.value())
                try:
                    cmds.setAttr(line + "." + CTRL_CURV, influence)
                    cmds.setAttr(line + "." + CTRL_CAP, cap)
                except Exception:
                    pass
                _update_curv_weights(line)
        finally:
            if chunk:
                cmds.undoInfo(closeChunk=True)

    # ========== キー（アニメーション） ==========
    def key_selected(self):
        """選択ラインのコントローラー属性を現フレームにキーする。"""
        lines = self._selected_lines()
        if not lines:
            cmds.warning("キーを打つラインをツリーで選択してください"); return
        cmds.undoInfo(openChunk=True)
        try:
            for line in lines:
                _ensure_ctrl_attrs(line, self.spin.value())
                for at in (CTRL_THICK, CTRL_CURV, CTRL_CAP):
                    try:
                        cmds.setKeyframe(line + "." + at)
                    except Exception:
                        pass
        finally:
            cmds.undoInfo(closeChunk=True)

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

                # deformer ハンドルは direction="Normal" では見た目に不要だが、削除すると
                # offset(太さ) が効かなくなるため、非表示にして ROOT 直下のホルダーへ退避する
                # （変形対象メッシュ＝dup の配下には親子付けできないため）。
                if handle and cmds.objExists(handle):
                    try:
                        cmds.setAttr(handle + ".visibility", 0)
                        cmds.parent(handle, self._ensure_handle_holder())
                    except Exception:
                        pass

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

                # ラインコントローラー: キーアブル属性を作り、太さは offset へ直結（DGでアニメ可）。
                # 曲率/上限は scriptJob で頂点ウェイトを追従させる。
                _ensure_ctrl_attrs(dup, thick)
                cmds.setAttr(dup + "." + CTRL_THICK, thick)
                try:
                    cmds.connectAttr(dup + "." + CTRL_THICK, defm + ".offset", f=True)
                except Exception:
                    pass
                _ensure_curv_jobs(dup)
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
        cmds.undoInfo(openChunk=True)
        try:
            cmds.delete(nodes)
            # 変形ノードが消えて孤立したハンドル／空ホルダーを掃除
            self._cleanup_orphan_handles()
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
