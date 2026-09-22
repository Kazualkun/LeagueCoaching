"""Desenha os ROIs sobre um print de partida, para conferencia visual.

Os valores em `vision/rois.py` foram medidos da HUD padrao em 1920x1080 com
escala 100%. Eles precisam ser conferidos contra captura REAL — do mesmo jeito
que as zonas do mapa so ficaram corretas depois de comparar com posicoes de
Baron de verdade, e foi assim que o raio da base apareceu errado.

    uv run python tools/calibrate_rois.py print.png
    uv run python tools/calibrate_rois.py print.png --scale 0.8
    uv run python tools/calibrate_rois.py video.mp4 --at 600

Gera `<nome>-rois.png` com os retangulos desenhados e rotulados, e tenta ler os
ROIs de OCR para mostrar o que sai de cada um. Se o relogio nao aparecer, o
recorte esta errado e TODO o modo video sairia deslocado no tempo.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw

from riftcoach.vision import rois
from riftcoach.vision.ocr import parse_clock, read_text


def seguro(texto: str) -> str:
    """Texto imprimivel no console atual.

    O OCR as vezes devolve CJK ao tentar ler um recorte que nao tem texto, e o
    console do Windows (cp1252) estoura com UnicodeEncodeError. Perder a
    ferramenta de calibracao por causa da codificacao do terminal seria
    ridiculo.
    """
    cod = sys.stdout.encoding or "utf-8"
    return texto.encode(cod, errors="replace").decode(cod, errors="replace")


CORES = {
    "game_clock": (255, 80, 80),
    "minimap": (80, 200, 255),
    "kda": (255, 200, 80),
    "cs": (160, 255, 120),
    "gold": (255, 220, 60),
    "self_health": (120, 255, 200),
    "abilities": (220, 140, 255),
}


def carregar(caminho: Path, at_s: float | None) -> Image.Image:
    if caminho.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp", ".webp"):
        return Image.open(caminho).convert("RGB")

    from riftcoach.vision.sampler import extract_at, probe

    info = probe(caminho)
    alvo = at_s if at_s is not None else info.duration_s / 2
    quadros = extract_at(caminho, [alvo])
    if not quadros:
        raise SystemExit(f"nao foi possivel extrair um quadro em {alvo:.0f}s")
    return quadros[0].image.convert("RGB")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("caminho", type=Path, help="print da partida ou arquivo de video")
    ap.add_argument("--scale", type=float, default=1.0, help="escala de HUD do jogo")
    ap.add_argument("--at", type=float, default=None, help="instante, se for video")
    ap.add_argument(
        "--profile", choices=sorted(rois.PROFILES), default="spectator",
        help="espectador (replay) ou jogador (gravacao da propria tela)",
    )
    args = ap.parse_args()

    img = carregar(args.caminho, args.at)
    w, h = img.size
    perfil = rois.PROFILES[args.profile]
    print(
        f"=== {args.caminho.name} · {w}x{h} · escala {args.scale} "
        f"· perfil {perfil.name} ==="
    )
    if rois.is_ultrawide(w, h):
        print("  [ultrawide] a HUD fica ancorada nos cantos; confira as bordas\n")

    marcado = img.copy()
    d = ImageDraw.Draw(marcado)

    for r in perfil.rois:
        caixa = r.pixels(w, h, args.scale)
        cor = CORES.get(r.name, (255, 255, 255))
        d.rectangle(caixa, outline=cor, width=2)
        d.text((caixa[0], max(0, caixa[1] - 12)), r.name, fill=cor)

        if r in perfil.ocr:
            leituras = read_text(img, r, scale=args.scale)
            texto = " | ".join(seguro(x.clean) for x in leituras) or "(nada)"
            extra = ""
            if r is perfil.clock:
                s = next(
                    (v for v in (parse_clock(x.clean) for x in leituras) if v is not None),
                    None,
                )
                extra = (
                    f"  -> {s // 60}:{s % 60:02d}"
                    if s is not None
                    else "  -> NAO RECONHECIDO como relogio"
                )
            print(f"  {r.name:<12} {caixa!s:<28} OCR: {texto}{extra}")
        else:
            print(f"  {r.name:<12} {caixa!s:<28} (sem OCR)")

    saida = args.caminho.with_name(f"{args.caminho.stem}-rois-{perfil.name}.png")
    marcado.save(saida)
    print(f"\nimagem marcada: {saida}")
    print(
        "Confira se cada retangulo cobre o elemento certo. O do relogio e o mais\n"
        "importante: se ele estiver errado, todo finding do modo video sai\n"
        "deslocado no tempo — e deslocado com cara de certeza."
    )


if __name__ == "__main__":
    main()
