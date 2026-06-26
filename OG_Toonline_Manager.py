# -*- coding: utf-8 -*-
"""
OG Toonline Manager
===================
dx11Shader の MayaToonOutline.fx 相当の輪郭線を、Maya標準機能（実ジオメトリ）だけで再現。
inverted hull 方式（押し出し → 法線反転 → バックフェースカリング）。
元メッシュの変形に自動追従し、ライン単位で太さ・色・表示／非表示を管理できる。

主な機能:
    - 選択メッシュにアウトライン（ライン）を生成（生成ボタン／グループ欄の + ボタン）
    - ライン／グループをツリーで一覧表示（グループは枠付きで強調）
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
COL_PREFIX = "toonOutlineCol_"      # ライン個別カラーシェーダの接頭辞

GROUP_ROLE = QtCore.Qt.UserRole + 1  # ツリー項目がグループかどうか


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


class _GroupBorderDelegate(QtWidgets.QStyledItemDelegate):
    """グループ行に枠を描いて視認性を上げるデリゲート。"""
    def paint(self, painter, option, index):
        super(_GroupBorderDelegate, self).paint(painter, option, index)
        if index.data(GROUP_ROLE):
            painter.save()
            pen = QtGui.QPen(QtGui.QColor("#8ab4d8"))
            pen.setWidth(1)
            painter.setPen(pen)
            r = option.rect.adjusted(1, 1, -2, -2)
            painter.drawRoundedRect(r, 3, 3)
            painter.restore()


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
        self._build()
        self.refresh_tree()

    # ========== UI 構築 ==========
    def _build(self):
        lay = QtWidgets.QVBoxLayout(self)

        # 生成 + 対象グループ（+ ボタンでも生成できる）
        crow = QtWidgets.QHBoxLayout()
        self.btn_create = QtWidgets.QPushButton("選択メッシュに輪郭を生成")
        self.btn_create.clicked.connect(self.create_outlines)
        crow.addWidget(self.btn_create, 1)
        crow.addWidget(QtWidgets.QLabel("グループ"))
        self.group_combo = QtWidgets.QComboBox()
        self.group_combo.setMinimumWidth(110)
        crow.addWidget(self.group_combo)
        self.btn_plus = QtWidgets.QPushButton("+")
        self.btn_plus.setFixedWidth(28)
        self.btn_plus.setToolTip("選択メッシュから、上のグループにラインを生成")
        self.btn_plus.clicked.connect(self.create_outlines)
        crow.addWidget(self.btn_plus)
        lay.addLayout(crow)

        # ライン／グループ ツリー（チェック=表示、色列=個別カラー）
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["名前 (チェック=表示)", "太さ", "色"])
        self.tree.setColumnWidth(0, 230)
        self.tree.setColumnWidth(1, 60)
        self.tree.setColumnWidth(2, 40)
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.tree.setItemDelegate(_GroupBorderDelegate(self.tree))
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
        self.lbl_hint = QtWidgets.QLabel("※ 太さ・個別カラーはツリーで選択したラインに適用されます")
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

    def _shape_of(self, line):
        sh = cmds.listRelatives(line, shapes=True, type="mesh", ni=True, f=True)
        return sh[0] if sh else None

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
        it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
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

    def refresh_tree(self):
        self._populating = True
        self.tree.clear()
        # グループ
        for g in self._managed_groups():
            gi = QtWidgets.QTreeWidgetItem(["▸ " + _short(g), "", ""])
            gi.setData(0, QtCore.Qt.UserRole, g)
            gi.setData(0, GROUP_ROLE, True)
            gi.setFlags(gi.flags() | QtCore.Qt.ItemIsUserCheckable)
            gi.setFirstColumnSpanned(True)
            f = gi.font(0); f.setBold(True); gi.setFont(0, f)
            gi.setBackground(0, QtGui.QBrush(QtGui.QColor(58, 68, 80)))
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
            gi = QtWidgets.QTreeWidgetItem(["▸ (未分類)", "", ""])
            gi.setData(0, GROUP_ROLE, True)
            gi.setFlags(gi.flags() & ~QtCore.Qt.ItemIsUserCheckable)
            gi.setFirstColumnSpanned(True)
            f = gi.font(0); f.setBold(True); gi.setFont(0, f)
            gi.setBackground(0, QtGui.QBrush(QtGui.QColor(58, 68, 80)))
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
                cmds.setAttr(SHADER + ".outColor",
                             self._color[0], self._color[1], self._color[2],
                             type="double3")
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

                # 複製は元と同じ位置（同じ親・同じ TRS）に出る。トランスフォームは動かさない。
                # → 元と同じ変換空間に留まるので原点に飛ばない。
                dup = cmds.duplicate(obj, name=_short(obj) + "_outline", rr=True)[0]
                for k in cmds.listRelatives(dup, children=True, type="transform", f=True) or []:
                    cmds.delete(k)
                dshape = cmds.listRelatives(dup, shapes=True, type="mesh", ni=True, f=True)[0]

                # 複製のヒストリ／デフォーマを除去して inMesh を空ける
                cmds.delete(dup, constructionHistory=True)

                # --- inverted hull を構築 ---
                # 1) 元の outMesh（オブジェクト空間の変形後メッシュ）を inMesh に直結。
                #    複製は元と同じトランスフォームなので、変形に追従しつつ元に重なる。
                cmds.connectAttr(src + ".outMesh", dshape + ".inMesh", f=True)
                # 2) コマンド形式で 押し出し→法線反転 を挿入（頂点ごとの法線フレームが張られ、
                #    localTranslateZ が法線方向の膨らみになる）。追加順に shape 側へ積まれ、
                #    最終チェーンは outMesh → push → reverse → shape。
                cmds.polyMoveVertex(dshape + ".vtx[*]", localTranslateZ=thick, ch=True)
                cmds.polyNormal(dshape, normalMode=0, ch=True)   # 0 = 法線反転

                # バックフェースカリング + 共通シェーダ + タグ
                cmds.setAttr(dshape + ".doubleSided", 0)
                cmds.sets(dshape, e=True, forceElement=sg)
                if not cmds.attributeQuery(TAG, node=dup, exists=True):
                    cmds.addAttr(dup, ln=TAG, at="bool", dv=True)

                # グループへ（ワールド位置を保持したまま移動 → 重なりは維持）
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
