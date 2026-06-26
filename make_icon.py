# -*- coding: utf-8 -*-
"""OG Toonline Manager のアイコンを生成する（透過背景PNG）。
太い輪郭線のついたセルシェードの球＝トゥーンアウトラインを表現。"""
from PIL import Image, ImageDraw

SS = 4  # スーパーサンプリング（アンチエイリアス用）


def _circle_mask(size, cx, cy, r):
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    return m


def make(size_out):
    S = size_out * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    cx = cy = S / 2.0
    r_sphere = S * 0.33          # 球の半径
    r_outline = r_sphere + S * 0.085  # 輪郭線（外側の太い黒）

    # トゥーンカラー（青系・3トーンのセルシェード）
    col_base = (58, 160, 214, 255)
    col_light = (130, 214, 245, 255)
    col_dark = (28, 96, 150, 255)
    outline = (18, 22, 28, 255)

    # 1) 太い輪郭線（黒シルエット）
    draw.ellipse([cx - r_outline, cy - r_outline, cx + r_outline, cy + r_outline],
                 fill=outline)

    # 2) 球のベース色
    draw.ellipse([cx - r_sphere, cy - r_sphere, cx + r_sphere, cy + r_sphere],
                 fill=col_base)

    sphere_mask = _circle_mask(S, cx, cy, r_sphere)

    # 3) 影（右下のクレセント）= 大きな円を右下にずらして暗色、球内だけに適用
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    off = S * 0.16
    sd.ellipse([cx - r_sphere + off, cy - r_sphere + off,
                cx + r_sphere + off, cy + r_sphere + off], fill=col_dark)
    img.paste(shadow, (0, 0), Image.composite(shadow.split()[3], Image.new("L", (S, S), 0), sphere_mask))

    # 4) ハイライト（左上のセル）= 小さめの円を左上に、ハードエッジで明色
    hi = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hi)
    rh = r_sphere * 0.78
    hx = cx - r_sphere * 0.30
    hy = cy - r_sphere * 0.30
    hd.ellipse([hx - rh, hy - rh, hx + rh, hy + rh], fill=col_light)
    img.paste(hi, (0, 0), Image.composite(hi.split()[3], Image.new("L", (S, S), 0), sphere_mask))

    # 5) スペキュラ（小さな白い点）
    sp_r = r_sphere * 0.16
    spx = cx - r_sphere * 0.42
    spy = cy - r_sphere * 0.44
    draw.ellipse([spx - sp_r, spy - sp_r, spx + sp_r, spy + sp_r],
                 fill=(255, 255, 255, 235))

    # ダウンサンプルでアンチエイリアス
    return img.resize((size_out, size_out), Image.LANCZOS)


if __name__ == "__main__":
    make(256).save("icon_OG_Toonline_Manager.png")
    make(64).save("icon_OG_Toonline_Manager_64.png")
    make(32).save("icon_OG_Toonline_Manager_32.png")
    print("saved")
