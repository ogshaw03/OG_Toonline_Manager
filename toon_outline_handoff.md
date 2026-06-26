# Toon Outline Tool — 引き継ぎドキュメント

## 1. 概要

`dx11Shader` の `MayaToonOutline.fx` 相当の輪郭線を、**Maya標準機能（実ジオメトリ）だけ**で再現するツール。VP2 表示でも Arnold / V-Ray / Redshift のどのレンダラーでも同じ見た目で出る、レンダラー非依存の輪郭。

手法は **inverted hull**（押し出し → 法線反転 → バックフェースカリング）。元メッシュの変形に自動追従し、スライダーで太さをライブ調整できる PySide6/PySide2 UI 付き。

---

## 2. 仕組み（最重要）

各アウトラインは元メッシュの複製で、評価チェーンは次の順で組む：

```
元メッシュ.worldMesh[0]  (skinCluster等で変形後)
      │  connectAttr → polyMoveVertex.inputPolymesh
      ▼
polyMoveVertex   (localTranslateZ = 頂点法線方向へ押し出し / 太さ)
      ▼
polyNormal       (normalMode=0 で法線反転)
      ▼
アウトライン shape   (doubleSided=0 でバックフェースカリング)
```

- **追従の仕組み**: 複製を skinCluster で縛らない。代わりに元メッシュの `worldMesh[0]`（＝デフォーム後の形状）を `polyMoveVertex.inputPolymesh` にライブ接続。入力が毎フレーム変形 → 後段ヒストリが再評価されて追従する。`.fx` が頂点シェーダで毎フレーム膨らませているのを、ヒストリノードで等価再現している。
- **太さの仕組み**: 各アウトラインの `polyMoveVertex.localTranslateZ` を UI から直接 `setAttr`。輪郭を作り直さずに即反映され、追従も維持される。
- **トランスフォーム**: 複製は `parent -w` でワールドへ出し、T=0 / R=0 / S=1 に単位化。形状データがワールド空間 worldMesh なので、トランスフォームは単位でちょうど元に重なる。

---

## 3. ⚠️ ハマりどころ（既知の落とし穴）

**`worldMesh` の接続と `polyMoveVertex` の順序を間違えると、ラインが原点に生成される。**

- NG: 先に `worldMesh[0] → inMesh` を繋いでから `polyMoveVertex` をヒストリ追加する。→ ライブ接続が押し出しノードに正しくチェーンされず、表示が複製の静的メッシュ（トランスフォーム単位化済みなのでローカル原点）に戻り、**原点に出る**。
- OK: **先に `polyMoveVertex`（押し出し）+ `polyNormal`（反転）のヒストリを積み**、そのうえで `元.worldMesh[0]` を **`polyMoveVertex.inputPolymesh`** に差し込む。

現行コードはこの正しい順序に修正済み。`create_outlines()` 内のコメント `# 1) 先に … # 2) … 差し替え` の流れを崩さないこと。

---

## 4. ノード命名 / タグ

| 名前 | 種別 | 役割 |
|---|---|---|
| `toonOutline_SS` | surfaceShader | 全アウトライン共有のフラット（unlitな）輪郭マテリアル。`outColor` が線の色 |
| `toonOutline_SG` | shadingEngine | 上記の SG |
| `toonOutlines_grp` | transform | 生成アウトラインの格納グループ |
| `<元名>_outline` | transform | 各アウトライン本体 |
| `.isToonOutline` | bool attr | アウトライン識別タグ。選択/削除/再取得のスキャンに使用 |

色はサーフェスシェーダ共有なので**全アウトライン一括**。個別色が必要なら shader を分離する改修が要る。

---

## 5. UI 機能

- **生成**: 選択メッシュ（複数可）にアウトラインを作成。
- **太さ**: スライダー(0.000–2.000) ⇔ スピンボックス双方向同期。`localTranslateZ` をライブ駆動。
- **色**: `QColorDialog` で選択 → `toonOutline_SS.outColor` に反映。
- **輪郭を選択 / 削除**: `.isToonOutline` タグでスキャンして一括処理。
- **再取得**: 別セッションで作った既存アウトラインを `polyMoveVertex` キャッシュに取り込み、スライダー制御下に戻す。ウィンドウ開き直し後はこれを押す。

---

## 6. 残課題 / 改善余地

1. **太さがワールドスケール依存**: `localTranslateZ` の絶対値はメッシュのスケールに依存。レンジ上限 2.0 で足りないキャラスケールなら `spin.setRange` 上限を上げる。画面上で一定px幅にしたい（スケール非依存）場合は別アプローチが必要。
2. **押し出し方向の精度**: `polyMoveVertex` は入力法線フレームで押し出すため、関節が極端に回転する箇所で方向が僅かにズレて見える場合がある。完全に正確にするなら「事前に膨らませたハルを元メッシュに `wrap` で追従させる」方式に差し替え。
3. **インスタンスメッシュ**: 元がインスタンス化されていると `worldMesh[0]` のインデックスが実体パスと食い違い、位置がズレる可能性。実体パスを解決して正しい `worldMesh[n]` を選ぶ改修が要る。
4. **色の個別化**: 現状は一括のみ（前述）。

---

## 7. 標準機能での代替案（参考）

| 方式 | 特徴 | mixed renderer 適性 |
|---|---|---|
| **inverted hull（本ツール）** | `.fx` と最も等価。VP2/全レンダラー統一。要管理 | ◎ 唯一統一できる |
| pfxToon (`Toon > Assign Outline`) | 標準トゥーン。手軽だが Paint Effects ベース。最終レンダーは要ポリ/カーブ変換 | △ |
| レンダラー純正 (aiToon+contour / VRayToon / Redshift Contour) | ジオメトリ不要・高品質。レンダラーごとに別設定 | × 統一不可 |

「VP2 上で `.fx` と同じ見た目を全レンダラー共通で」が要件なら inverted hull が唯一解。
