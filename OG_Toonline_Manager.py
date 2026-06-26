# -*- coding: utf-8 -*-
"""
OG Toonline Manager
===================
dx11Shader の MayaToonOutline.fx 相当の輪郭線を、Maya標準機能（実ジオメトリ）だけで再現。
inverted hull 方式（押し出し → 法線反転 → バックフェースカリング）。
元メッシュの変形に自動追従し、ライン単位で太さ・表示／非表示を管理できる。

主な機能:
    - 選択メッシュにアウトライン（ライン）を生成
    - ライン／グループをツリーで一覧表示
    - ライン単位の太さ調整（選択したラインだけに適用）
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

# ---- PySide6 / PySide2 両対応 ----
try:
    from PySide6 import QtWidgets, QtCore, QtGui
    from shiboken6 import wrapInstance
except ImportError:
    from PySide2 import QtWidgets, QtCore, QtGui
    from shiboken2 import wrapInstance

SHADER    = "toonOutline_SS"
SG        = "toonOutline_SG"
ROOT      = "toonOutlines_grp"      # 全ライン／グループの親
TAG       = "isToonOutline"         # ライン識別タグ
GROUP_TAG = "isToonOutlineGroup"    # グループ識別タグ
DEFAULT_GROUP = "Outline_Group1"


def _maya_main():
    return wrapInstance(int(omui.MQtUtil.mainWindow()), QtWidgets.QWidget)


def _short(name):
    return name.split("|")[-1] if name else name


def _ensure_shader(color):
    """全アウトライン共有のフラットマテリアルを確保。outColor が線の色。"""
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


class ToonOutlineUI(QtWidgets.QDialog):

    def __init__(self, parent=None):
        if parent is None:
            parent = _maya_main()
        super(ToonOutlineUI, self).__init__(parent)
        self.setWindowTitle("OG_Toonline_Manager")
        self.setMinimumWidth(360)
        self.setMinimumHeight(420)
        self._color = [0.0, 0.0, 0.0]
        self._populating = False       # ツリー再構築中のシグナル抑止フラグ
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
        crow.addWidget(QtWidgets.QLabel("→"))
        self.group_combo = QtWidgets.QComboBox()
        self.group_combo.setMinimumWidth(120)
        crow.addWidget(self.group_combo)
        lay.addLayout(crow)

        # ライン／グループ ツリー（チェックで表示・非表示）
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["名前 (チェック=表示)", "太さ"])
        self.tree.setColumnWidth(0, 240)
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.itemSelectionChanged.connect(self._on_tree_selection)
        lay.addWidget(self.tree, 1)

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
        self.spin.valueChanged.connect(self._on_spin)
        trow.addWidget(self.slider)
        trow.addWidget(self.spin)
        lay.addLayout(trow)
        self.lbl_hint = QtWidgets.QLabel("※ 太さはツリーで選択したラインにのみ適用されます")
        self.lbl_hint.setStyleSheet("color:#888;")
        lay.addWidget(self.lbl_hint)

        # 色（全ライン共有）
        clrow = QtWidgets.QHBoxLayout()
        clrow.addWidget(QtWidgets.QLabel("色 (全ライン共通)"))
        self.btn_color = QtWidgets.QPushButton()
        self.btn_color.setFixedHeight(22)
        self.btn_color.clicked.connect(self._pick_color)
        self._refresh_swatch()
        clrow.addWidget(self.btn_color, 1)
        lay.addLayout(clrow)

        # 管理ボタン
        mrow = QtWidgets.QHBoxLayout()
        b_grp = QtWidgets.QPushButton("新規グループ")
        b_mov = QtWidgets.QPushButton("選択を対象グループへ")
        b_del = QtWidgets.QPushButton("削除")
        b_ref = QtWidgets.QPushButton("再取得")
        b_grp.clicked.connect(self.new_group)
        b_mov.clicked.connect(self.move_selected_to_group)
        b_del.clicked.connect(self.delete_selected)
        b_ref.clicked.connect(self.refresh_tree)
        for b in (b_grp, b_mov, b_del, b_ref):
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

    def _pmv_for(self, line):
        """ラインの太さ駆動ノード（polyMoveVertex）を返す。"""
        pmvs = cmds.ls(cmds.listHistory(line) or [], type="polyMoveVertex")
        return pmvs[0] if pmvs else None

    def _thickness_of(self, line):
        pmv = self._pmv_for(line)
        if pmv and cmds.objExists(pmv):
            try:
                return cmds.getAttr(pmv + ".localTranslateZ")
            except Exception:
                pass
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

    # ========== ツリー ==========
    def _add_line_item(self, parent_item, line):
        it = QtWidgets.QTreeWidgetItem([_short(line), ""])
        it.setData(0, QtCore.Qt.UserRole, line)
        it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
        vis = True
        try:
            vis = bool(cmds.getAttr(line + ".visibility"))
        except Exception:
            pass
        it.setCheckState(0, QtCore.Qt.Checked if vis else QtCore.Qt.Unchecked)
        t = self._thickness_of(line)
        it.setText(1, "" if t is None else "{:.3f}".format(t))
        parent_item.addChild(it)
        return it

    def refresh_tree(self):
        self._populating = True
        self.tree.clear()
        # グループ
        for g in self._managed_groups():
            gi = QtWidgets.QTreeWidgetItem([_short(g) + "  (グループ)", ""])
            gi.setData(0, QtCore.Qt.UserRole, g)
            gi.setFlags(gi.flags() | QtCore.Qt.ItemIsUserCheckable)
            gvis = True
            try:
                gvis = bool(cmds.getAttr(g + ".visibility"))
            except Exception:
                pass
            gi.setCheckState(0, QtCore.Qt.Checked if gvis else QtCore.Qt.Unchecked)
            self.tree.addTopLevelItem(gi)
            for line in self._lines_in(g):
                self._add_line_item(gi, line)
            gi.setExpanded(True)
        # グループ未所属のライン
        loose = self._loose_lines()
        if loose:
            gi = QtWidgets.QTreeWidgetItem(["(未分類)", ""])
            gi.setFlags(gi.flags() & ~QtCore.Qt.ItemIsUserCheckable)
            self.tree.addTopLevelItem(gi)
            for line in loose:
                self._add_line_item(gi, line)
            gi.setExpanded(True)
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

    # ---- 表示・非表示（チェックボックス） ----
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

    def _set_thickness_widgets(self, val):
        self.slider.blockSignals(True); self.spin.blockSignals(True)
        self.spin.setValue(val)
        self.slider.setValue(int(val * 1000))
        self.slider.blockSignals(False); self.spin.blockSignals(False)

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
        for line in lines:
            pmv = self._pmv_for(line)
            if pmv and cmds.objExists(pmv):
                try:
                    cmds.setAttr(pmv + ".localTranslateZ", val)
                except Exception:
                    pass
        # ツリーの太さ表示を更新
        self._populating = True
        for it in self.tree.selectedItems():
            n = it.data(0, QtCore.Qt.UserRole)
            if n and cmds.attributeQuery(TAG, node=n, exists=True):
                it.setText(1, "{:.3f}".format(val))
        self._populating = False

    # ========== 色（全ライン共通） ==========
    def _refresh_swatch(self):
        r, g, b = [int(c * 255) for c in self._color]
        self.btn_color.setStyleSheet(
            "background-color: rgb({},{},{}); border:1px solid #222;".format(r, g, b))

    def _pick_color(self):
        c0 = QtGui.QColor.fromRgbF(*self._color)
        c = QtWidgets.QColorDialog.getColor(c0, self, "輪郭の色")
        if c.isValid():
            self._color = [c.redF(), c.greenF(), c.blueF()]
            self._refresh_swatch()
            if cmds.objExists(SHADER):
                cmds.setAttr(SHADER + ".outColor",
                             self._color[0], self._color[1], self._color[2],
                             type="double3")

    # ========== 生成 ==========
    def create_outlines(self):
        sel = cmds.ls(sl=True, long=True, type="transform")
        if not sel:
            cmds.warning("メッシュを選択してください"); return
        thick = self.spin.value()
        _, sg = _ensure_shader(self._color)
        grp = self._current_group()

        cmds.undoInfo(openChunk=True)
        made = []
        try:
            for obj in sel:
                shps = cmds.listRelatives(obj, shapes=True, type="mesh", ni=True, f=True)
                if not shps:
                    continue
                src = shps[0]

                # 複製 → 子トランスフォームを除去 → ワールドへ出して T0/R0/S1 に単位化
                dup = cmds.duplicate(obj, name=_short(obj) + "_outline", rr=True)[0]
                for k in cmds.listRelatives(dup, children=True, type="transform", f=True) or []:
                    cmds.delete(k)
                if cmds.listRelatives(dup, parent=True):
                    dup = cmds.parent(dup, world=True)[0]
                for ax in "xyz":
                    cmds.setAttr("{}.t{}".format(dup, ax), 0)
                    cmds.setAttr("{}.r{}".format(dup, ax), 0)
                    cmds.setAttr("{}.s{}".format(dup, ax), 1)

                dshape = cmds.listRelatives(dup, shapes=True, type="mesh", ni=True, f=True)[0]
                # 複製のヒストリを除去して inMesh を空ける
                cmds.delete(dup, constructionHistory=True)

                # --- inverted hull を構築 ---
                # 1) 先に 元.worldMesh[0] を inMesh に直結してライブ追従を確立。
                #    worldMesh はワールド空間、ライン側は T0/R0/S1 なので元に重なる
                #    （shape の inMesh を直接駆動するので静的キャッシュに落ちず原点バグも出ない）。
                cmds.connectAttr(src + ".worldMesh[0]", dshape + ".inMesh", f=True)
                # 2) コマンド形式で 押し出し→法線反転 を挿入。コマンドは頂点ごとの法線フレームを
                #    張るので localTranslateZ が法線方向の膨らみになる（createNode だと膨らまず
                #    z-fighting で表示が乱れる）。追加順に shape 側へ積まれるので
                #    最終チェーンは worldMesh → push → reverse → shape。
                cmds.polyMoveVertex(dshape + ".vtx[*]", localTranslateZ=thick, ch=True)
                cmds.polyNormal(dshape, normalMode=0, ch=True)   # 0 = 法線反転

                # バックフェースカリング + シェーダ + タグ
                cmds.setAttr(dshape + ".doubleSided", 0)
                cmds.sets(dshape, e=True, forceElement=sg)
                if not cmds.attributeQuery(TAG, node=dup, exists=True):
                    cmds.addAttr(dup, ln=TAG, at="bool", dv=True)

                dup = cmds.parent(dup, grp)[0]
                made.append(dup)
            if made:
                cmds.select(made, r=True)
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

    def move_selected_to_group(self):
        lines = self._selected_lines()
        if not lines:
            cmds.warning("移動するラインをツリーで選択してください"); return
        grp = self._current_group()
        cmds.undoInfo(openChunk=True)
        try:
            for ln in lines:
                if cmds.listRelatives(ln, parent=True, f=True) != [grp]:
                    cmds.parent(ln, grp)
        finally:
            cmds.undoInfo(closeChunk=True)
        self.refresh_tree()

    def delete_selected(self):
        nodes = self._selected_nodes()
        if not nodes:
            cmds.warning("削除する項目をツリーで選択してください"); return
        cmds.undoInfo(openChunk=True)
        try:
            cmds.delete(nodes)
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
