#!/usr/bin/env python3
"""Oficina de agentes: genera oficina.html a partir de la bitácora.

Lee (solo lectura) ../tablero.md y ../planes/<modulo>*.md y escribe
oficina.html al lado de este archivo. Nada se publica ni se descarga.

Uso:
    python3 oficina.py                         # genera una vez
    python3 oficina.py --vigilar 10            # abre http://localhost:8765 y la
                                               # actualiza sola, sin recargar
                                               # (servidor solo local; Ctrl+C sale)
    python3 oficina.py --tokens yunho=180000 --tokens san=95000

También lee, solo metadatos (título, horas, encargo de cada subagente,
tokens), las sesiones activas de Claude Code de este ambiente en
~/.claude/projects/<ambiente>/; nunca el texto de las conversaciones.
"""

import argparse
import base64
import datetime as dt
import hashlib
import html
import http.server
import json
import random
import shutil
import subprocess
import re
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

AQUI = Path(__file__).resolve().parent
BITACORA = AQUI.parent

def _cargar(nombre):
    """Lee config.json / textos.json de esta carpeta."""
    try:
        return json.loads((AQUI / nombre).read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        sys.exit(f"No se pudo leer {AQUI / nombre}: {err}")


CFG = _cargar("config.json")   # agentes, zonas, fases, sala, umbrales, tildes
T = _cargar("textos.json")     # todos los textos que se ven en la oficina
U = CFG["umbrales"]

MODULO = CFG["modulo"]
ACTIVA_MIN = U["sesion_activa_minutos"]
SUBAGENTE_VIVO_H = U["subagente_detenido_horas"]
AGENTES = [(a["clave"], a["nombre"], a["rol"], a["zona"]) for a in CFG["agentes"]]
FASES = [(f["clave"], f["nombre"], f["patron"]) for f in CFG["fases"]]

RE_DECISION = re.compile(
    r"decisi[oó]n(es)? pendiente|pendiente(s)? de (su|la usuaria)|"
    r"esperan? (su )?(decisi|respuesta|visto bueno)|esperando (decisi|respuesta|visto bueno|su)|"
    r"falta (su )?visto bueno|pregunta(r)? a la usuaria",
    re.I,
)


# --------------------------------------------------------------- lectura
def limpiar_md(texto):
    texto = re.sub(r"\*\*|__|`", "", texto)
    texto = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", texto)
    return re.sub(r"\s+", " ", texto).strip()


def recortar(texto, largo):
    texto = limpiar_md(texto)
    if len(texto) <= largo:
        return texto
    corte = texto[:largo].rsplit(" ", 1)[0]
    return corte.rstrip(" ,;:—-") + "…"


def leer(ruta):
    try:
        return Path(ruta).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def tablas_md(texto):
    """Devuelve las tablas Markdown como listas de dicts (encabezado -> celda)."""
    tablas, actual, cab = [], None, None
    for linea in texto.splitlines():
        lin = linea.strip()
        if lin.startswith("|") and lin.endswith("|"):
            celdas = [c.strip() for c in lin[1:-1].split("|")]
            if cab is None:
                cab, actual = [c.lower() for c in celdas], []
            elif all(re.fullmatch(r":?-{2,}:?", c) for c in celdas if c):
                continue
            else:
                celdas += [""] * (len(cab) - len(celdas))
                actual.append(dict(zip(cab, celdas)))
        elif cab is not None:
            tablas.append((cab, actual))
            cab, actual = None, None
    if cab is not None:
        tablas.append((cab, actual))
    return tablas


def fecha(texto):
    m = re.search(r"(\d{4}-\d{2}-\d{2})(?:[ T](\d{1,2}:\d{2}))?", texto or "")
    if not m:
        return None
    try:
        return dt.datetime.fromisoformat(f"{m.group(1)} {m.group(2) or '00:00'}")
    except ValueError:
        return None


def agente_de(texto):
    """Primer integrante nombrado en el texto (por nombre de pila)."""
    mejor = None
    for clave, nombre, _rol, _zona in AGENTES:
        m = re.search(rf"\b{nombre}\b", texto or "", re.I)
        if m and (mejor is None or m.start() < mejor[0]):
            mejor = (m.start(), clave)
    return mejor and mejor[1]


def estado_de(estado, nota=""):
    e = (estado or "").lower()
    n = (nota or "").lower()
    if "bloque" in e and "paus" in n:
        return "pausa"
    if "bloque" in e:
        return "bloqueado"
    if "curso" in e:
        return "trabajando"
    if "paus" in e or "diferid" in e or "pendiente" in e or "espera" in e:
        return "pausa"
    return "libre"


def leer_tablero(ruta, modulo):
    filas = []
    for cab, datos in tablas_md(leer(ruta)):
        if "tarea" not in cab or "agente" not in cab:
            continue
        for f in datos:
            tarea = f.get("tarea", "")
            if not (tarea == modulo or tarea.startswith(modulo + ":")):
                continue
            filas.append({
                "tarea": tarea[len(modulo):].lstrip(": ").strip() or T["agente"]["modulo_completo"],
                "etapa": f.get("etapa", ""),
                "agente": agente_de(f.get("agente", "")),
                "agente_txt": f.get("agente", ""),
                "estado_txt": f.get("estado", ""),
                "estado": estado_de(f.get("estado", ""), f.get("nota", "")),
                "nota": f.get("nota", ""),
                "fecha": fecha(f.get("actualizado", "")),
            })
    filas.sort(key=lambda f: f["fecha"] or dt.datetime.min, reverse=True)
    return filas


def secciones(texto):
    """{título de la sección ## : cuerpo} conservando el orden."""
    out, titulo, cuerpo = [], None, []
    for linea in texto.splitlines():
        if linea.startswith("## "):
            if titulo is not None:
                out.append((titulo, "\n".join(cuerpo)))
            titulo, cuerpo = linea[3:].strip(), []
        elif titulo is not None:
            cuerpo.append(linea)
    if titulo is not None:
        out.append((titulo, "\n".join(cuerpo)))
    return out


def leer_fichas(carpeta, modulo):
    principal = Path(carpeta) / f"{modulo}.md"
    otras = sorted(Path(carpeta).glob(f"{modulo}.*.md"))
    entradas, fases, etapas, decisiones = [], {}, [], []
    titulo_plan = ""
    for ruta in [principal, *otras]:
        texto = leer(ruta)
        if not texto:
            continue
        if ruta == principal:
            primera = texto.splitlines()[0] if texto else ""
            titulo_plan = limpiar_md(primera.lstrip("# "))
        for titulo, cuerpo in secciones(texto):
            t = titulo.lower()
            if t.startswith("dónde quedé") or t.startswith("donde quede"):
                entradas += entradas_donde_quede(cuerpo, ruta.name)
            if "avance" in t:
                for cab, datos in tablas_md(cuerpo):
                    if "fase" in cab and "estado" in cab:
                        for f in datos:
                            clave = fase_de(f.get("fase", ""))
                            if clave:
                                fases[clave] = {
                                    "estado_txt": f.get("estado", ""),
                                    "fecha": fecha(f.get("fecha", "")),
                                    "agente_txt": f.get("agente", ""),
                                    "fuente": ruta.name,
                                }
            if "etapas" in t and not etapas:
                m = re.search(r"^Etapas:\s*(.+)$", cuerpo, re.M)
                if m:
                    for num, desc in re.findall(r"(\d+)\s+(.+?)(?=;\s*\d+\s|$)", m.group(1).rstrip(".")):
                        etapas.append({"num": int(num), "desc": recortar(desc, 400)})
    entradas.sort(key=lambda e: e["fecha"] or dt.datetime.min, reverse=True)
    if entradas:
        # Solo la entrada más reciente de «Dónde quedé» cuenta como pendiente
        # vigente; las anteriores son historia.
        for frase in frases_decision(entradas[0]["completo"]):
            decisiones.append({"texto": frase, "fuente": T["agente"]["fuente_donde_quede"].format(ficha=entradas[0]['fuente']),
                               "fecha": entradas[0]["fecha"]})
    return {"titulo": titulo_plan, "entradas": entradas, "fases": fases,
            "etapas": etapas, "decisiones": decisiones}


def entradas_donde_quede(cuerpo, fuente):
    bloques, actual = [], []
    for linea in cuerpo.splitlines():
        lin = linea.strip()
        if re.match(r"^(\(Entrada anterior:\)|\(Estado previo)", lin) and actual:
            bloques.append(actual)
            actual = []
        if lin.startswith("(Entrada anterior:)") and lin == "(Entrada anterior:)":
            continue
        if lin:
            actual.append(lin)
    if actual:
        bloques.append(actual)
    out = []
    for bloque in bloques:
        texto = " ".join(bloque)
        texto = re.sub(r"^\((Entrada anterior|Estado previo)[^)]*\)\s*", "", texto)
        cabeza = re.match(r"\*\*(.+?)\*\*", texto)
        resumen = cabeza.group(1) if cabeza else texto
        out.append({
            "fecha": fecha(resumen) or fecha(texto),
            "agente": agente_de(resumen.split("—")[0]) or agente_de(resumen),
            "texto": recortar(re.sub(r"^\d{4}-\d{2}-\d{2}\s*(\([^)]*\)\s*[—-]\s*)?", "", resumen), 220),
            "completo": texto,
            "fuente": fuente,
        })
    return out


def frases_decision(texto):
    frases = []
    for trozo in re.split(r"(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚ¿*-])|\s+-\s+", limpiar_md(texto)):
        if RE_DECISION.search(trozo):
            frases.append(recortar(trozo, 260))
    return frases


def fase_de(texto):
    for clave, _nombre, patron in FASES:
        if re.search(patron, texto or "", re.I):
            return clave
    return None


# ----------------------------------------------------------- modelo final
def construir(tablero, fichas, tokens):
    agentes = []
    for clave, nombre, rol, zona in AGENTES:
        propias = [f for f in tablero if f["agente"] == clave]
        actual = propias[0] if propias else None
        historial = [{
            "fecha": f["fecha"],
            "texto": f"{f['tarea']} — {f['etapa']}".strip(" —"),
            "estado": f["estado_txt"],
            "nota": f["nota"],
            "fuente": "tablero",
        } for f in propias]
        historial += [{
            "fecha": e["fecha"], "texto": e["texto"], "estado": "",
            "nota": "", "fuente": T["agente"]["fuente_donde_quede"].format(ficha=e['fuente']),
        } for e in fichas["entradas"] if e["agente"] == clave]
        historial.sort(key=lambda h: h["fecha"] or dt.datetime.min, reverse=True)
        agentes.append({
            "clave": clave, "nombre": nombre, "rol": rol, "zona": zona,
            "estado": actual["estado"] if actual else "libre",
            "estado_txt": actual["estado_txt"] if actual else "",
            "tarea": actual["tarea"] if actual else "",
            "etapa": actual["etapa"] if actual else "",
            "nota": actual["nota"] if actual else "",
            "desde": actual["fecha"] if actual else None,
            "historial": historial[:6],
            "tokens": tokens.get(clave),
        })

    fases = []
    for clave, nombre, _patron in FASES:
        base = fichas["fases"].get(clave)
        reciente = next((f for f in tablero if fase_de(f["etapa"]) == clave), None)
        dato = None
        if base:
            dato = {"estado_txt": base["estado_txt"], "fecha": base["fecha"],
                    "agente": agente_de(base["agente_txt"]), "fuente": "ficha"}
        if reciente and (not dato or (reciente["fecha"] or dt.datetime.min) > (dato["fecha"] or dt.datetime.min)):
            dato = {"estado_txt": reciente["estado_txt"], "fecha": reciente["fecha"],
                    "agente": reciente["agente"], "fuente": "tablero"}
        fases.append({"clave": clave, "nombre": nombre,
                      "estado": estado_fase(dato["estado_txt"]) if dato else "pendiente",
                      "estado_txt": dato["estado_txt"] if dato else T["agente"]["sin_registro"],
                      "fecha": dato["fecha"] if dato else None,
                      "agente": dato["agente"] if dato else None})

    etapas = []
    for et in fichas["etapas"]:
        fila = next((f for f in tablero if re.search(rf"\bEtapa\s+{et['num']}\b", f["tarea"], re.I)), None)
        etapas.append({**et,
                       "estado": estado_fase(fila["estado_txt"]) if fila else "pendiente",
                       "estado_txt": f"{fila['etapa']} · {fila['estado_txt']}" if fila else T["agente"]["pendiente"],
                       "fecha": fila["fecha"] if fila else None})

    decisiones = list(fichas["decisiones"])
    ultimo_por_agente = {}
    for f in tablero:
        ultimo_por_agente.setdefault(f["agente"], f)
    for f in ultimo_por_agente.values():
        if RE_DECISION.search(f"{f['estado_txt']} {f['nota']}"):
            decisiones.append({"texto": recortar(f"{f['tarea']}: {f['nota'] or f['estado_txt']}", 260),
                               "fuente": f"tablero · {f['agente_txt']}", "fecha": f["fecha"]})
    bloqueos = [{"texto": f"{a['nombre']} {a['rol']}: {a['tarea']}" + (f" ({a['nota']})" if a["nota"] else ""),
                 "fuente": "tablero", "fecha": a["desde"]}
                for a in agentes if a["estado"] == "bloqueado"]
    return {"agentes": agentes, "fases": fases, "etapas": etapas,
            "decisiones": decisiones, "bloqueos": bloqueos}


def estado_fase(txt):
    t = (txt or "").lower()
    if "bloque" in t:
        return "bloqueado"
    if "curso" in t:
        return "trabajando"
    if "listo" in t or "entregad" in t or "hecho" in t:
        return "listo"
    if "diferid" in t or "paus" in t or "espera" in t:
        return "pausa"
    return "pendiente"


# ------------------------------------------------------------------ HTML
# --------------------------------------------------- pixel art propio
# Personaje de bloques 16x16, dibujado aquí (sin imágenes externas).
CUERPO = [
    "................",
    "...HHHHHHHHHH...",
    "...HHHHHHHHHH...",
    "...HSSSSSSSSH...",
    "...SSSSSSSSSS...",
    "...SWESSSSWES...",
    "...SSSSSSSSSS...",
    "...SSSSMMSSSS...",
    "...SSSSSSSSSS...",
    "......SSSS......",
    "..TTTTTTTTTTTT..",
    ".TTTTTTTTTTTTTT.",
    ".TTTTTTTTTTTTTT.",
    ".TTTTTTTTTTTTTT.",
    ".TTTTTTTTTTTTTT.",
    ".TTTTTTTTTTTTTT.",
]
# rol: (pelo, piel, camiseta, accesorio [(x, y, color), ...])
ROL_PIXEL = {
    "Orchestrator": ("#3b2416", "#e8b48a", "#6c4fd1",
                     [(2, 4, "#222"), (2, 5, "#222"), (2, 6, "#222"), (13, 4, "#222"), (13, 5, "#222"),
                      (13, 6, "#222"), (12, 7, "#222"), (11, 8, "#222"), (3, 0, "#222"), (12, 0, "#222")]
                     + [(x, 0, "#222") for x in range(4, 12)]),
    "Analyst": ("#1c1410", "#c98d62", "#2a8c80",
                [(11, 11, "#cfe9f7"), (12, 11, "#cfe9f7"), (10, 11, "#1d3b37"), (13, 11, "#1d3b37"),
                 (11, 10, "#1d3b37"), (12, 10, "#1d3b37"), (11, 12, "#1d3b37"), (12, 12, "#1d3b37"),
                 (13, 13, "#1d3b37"), (14, 14, "#1d3b37")]),
    "Designer": ("#8a4430", "#f0c49c", "#e0703f",
                 [(x, 0, "#c0392b") for x in range(3, 13)] + [(x, 1, "#c0392b") for x in range(2, 12)]
                 + [(12, 10, "#f1c40f"), (12, 11, "#f1c40f"), (12, 12, "#f1c40f"), (12, 13, "#e67e22")]),
    "Developer": ("#2a2a2a", "#e3a77c", "#2f6fd0",
                  [(x, 5, "#14213d") for x in (3, 6, 9, 12)] + [(7, 5, "#14213d"), (8, 5, "#14213d")]
                  + [(5, 12, "#ffffff"), (6, 12, "#7ee0a8"), (8, 12, "#7ee0a8"), (9, 12, "#ffffff")]),
    "Tester": ("#5a3418", "#f2c8a0", "#d9a52b",
               [(x, y, "#fff6dc") for x in range(10, 14) for y in range(11, 15)]
               + [(10, 12, "#2e9e3a"), (11, 13, "#2e9e3a"), (12, 12, "#2e9e3a"), (13, 11, "#2e9e3a")]),
    "Auditor": ("#121212", "#d9a074", "#4a5568",
                [(x, y, "#d4a017") for x in range(10, 14) for y in range(11, 14)]
                + [(11, 14, "#d4a017"), (12, 14, "#d4a017"), (11, 12, "#7a5c0a"), (12, 12, "#7a5c0a")]),
    "Reviewer": ("#6b3f26", "#f0c49c", "#c24f7a",
                 [(3, 5, "#3a1d2a"), (6, 5, "#3a1d2a"), (9, 5, "#3a1d2a"), (12, 5, "#3a1d2a"),
                  (10, 13, "#ffffff"), (11, 14, "#ffffff"), (12, 13, "#ffffff"), (13, 12, "#ffffff")]),
    "Documenter": ("#2c1d14", "#c98d62", "#4f8a35",
                   [(x, y, "#f4eee0") for x in range(9, 14) for y in range(11, 15)]
                   + [(11, y, "#6b4f2a") for y in range(11, 15)] + [(9, 12, "#9b8a6a"), (13, 12, "#9b8a6a")]),
}


def _rects(celdas):
    """Une píxeles contiguos del mismo color por fila para achicar el SVG."""
    out = []
    for y, fila in enumerate(celdas):
        x = 0
        while x < len(fila):
            c = fila[x]
            fin = x
            while fin + 1 < len(fila) and fila[fin + 1] == c:
                fin += 1
            if c:
                out.append(f'<rect x="{x}" y="{y}" width="{fin - x + 1}" height="1" fill="{c}"/>')
            x = fin + 1
    return "".join(out)


def svg_personaje(rol):
    pelo, piel, ropa, extra = ROL_PIXEL[rol]
    colores = {"H": pelo, "S": piel, "E": "#1b1b1b", "W": "#ffffff", "M": "#8a4a32", "T": ropa, ".": None}
    celdas = [[colores[ch] for ch in fila] for fila in CUERPO]
    for x, y, c in extra:
        celdas[y][x] = c
    return ('<svg viewBox="0 0 16 16" shape-rendering="crispEdges" role="img" '
            f'aria-label="{T["sala"]["personaje"].format(rol=rol)}">'
            '<rect width="16" height="16" fill="var(--avatar-bg)"/>' + _rects(celdas) + "</svg>")


def _tile(n, color_de, semilla):
    rnd = random.Random(semilla)
    celdas = [[color_de(x, y, rnd) for x in range(n)] for y in range(n)]
    return _uri(n, n, celdas)


def _uri(ancho, alto, celdas):
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {ancho} {alto}" '
           f'shape-rendering="crispEdges">{_rects(celdas)}</svg>')
    return "url(\"data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode() + "\")"


def _pix(filas, colores):
    celdas = [[colores.get(ch) for ch in fila] for fila in filas]
    return _uri(len(filas[0]), len(filas), celdas)


def _tono(hexa, f):
    r, g, b = (int(hexa[i:i + 2], 16) for i in (1, 3, 5))
    if f < 1:  # noche: más oscuro y algo violeta
        r, g, b = r * f + 14 * (1 - f), g * f, b * f + 30 * (1 - f)
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(v))) for v in (r, g, b))


def texturas(f):
    t = lambda pal: [_tono(c, f) for c in pal]  # noqa: E731
    menta = t(["#cdeedd", "#c4ead6", "#d5f2e3", "#bfe6d1"])
    petalo = t(["#f7bfd5", "#fbd3e3"])
    cerezo = t(["#f9d3e2", "#fbdbe8", "#f6c8da", "#fde3ee"])
    madera = t(["#f9e3da", "#f7ddd4", "#fbe9e2", "#f1d3c8"])
    piso = t(["#fdf0f4", "#fcebf0", "#fef4f7", "#f7e0e8"])
    lavanda = t(["#e8def6", "#e1d6f2", "#ede5f8", "#dacdee"])
    escritorio = t(["#f3c3d2", "#f0bccc", "#f6cad7", "#e6aec0"])
    papel = t(["#fdf0f5", "#fceaf1"])
    flor, centro = t(["#f6bcd2"])[0], t(["#ffe6a3"])[0]

    def ruido(pal):
        return lambda x, y, r: r.choice(pal)

    def con_petalos(pal):
        return lambda x, y, r: r.choice(petalo) if r.random() < .06 else r.choice(pal)

    def tablas(pal):
        def c(x, y, r):
            if y % 4 == 3 or (x == (0 if (y // 4) % 2 == 0 else 8) and r.random() < .5):
                return pal[3]
            return r.choice(pal[:3])
        return c

    def tapiz(x, y, r):
        for cx, cy in ((3, 3), (11, 11)):
            dx, dy = abs(x - cx), abs(y - cy)
            if dx == 0 and dy == 0:
                return centro
            if dx + dy == 1:
                return flor
        return r.choice(papel)

    return {
        "pasto": _tile(16, con_petalos(menta), 1),
        "cerezo": _tile(16, con_petalos(cerezo), 2),
        "madera": _tile(16, tablas(madera), 3),
        "lavanda": _tile(16, ruido(lavanda), 4),
        "piso": _tile(16, tablas(piso), 5),
        "escritorio": _tile(16, tablas(escritorio), 6),
        "tapiz": _tile(16, tapiz, 7),
    }


ROSA, ROSA2, AMAR, VERDE = "#f48fb1", "#e0679a", "#ffe08a", "#7cc49a"
DECOR = {
    "lazo": _pix(["XX.....XX", "XXX.K.XXX", "XXXXKXXXX", "XXX.K.XXX", "XX..K..XX"], {"X": ROSA, "K": ROSA2}),
    "flor": _pix(["..P..", ".PPP.", "PPYPP", ".PPP.", "..P.."], {"P": "#f7a8c4", "Y": AMAR}),
    "petalo": _pix([".PP.", "PPPP", "PPL.", ".P.."], {"P": "#f9b8cf", "L": "#fde1ec"}),
    "maceta": _pix(["..PP.PP..", ".PPYPPYP.", "..PP.PP..", "...G.G...", "....G....",
                    "..TTTTT..", ".TTTTTTT.", "..TWTWT..", "..TTTTT..", "...TTT..."],
                   {"P": "#f7a8c4", "Y": AMAR, "G": VERDE, "T": "#c9b6e8", "W": "#e6dcf6"}),
    "rama": _pix(["......B.........", ".....BPP..PP....", "..PP.B.PPPYP.PP.", ".PYPBB..PP..PYP.",
                  "..PBB.......BPP.", "BBBB..PP...BB...", "....BBPYPBBB.PP.", "..PP..BPPB...PYP",
                  ".PYP...BB.....P.", "..P.............", ],
                 {"B": "#8a5a4a", "P": "#f7b6cf", "Y": "#fff1a8"}),
    "planta": _pix(["...G.G..G...", "..GGG.GGG.G.", ".GGLGGGLGGG.", "GGLGG.GGLGGG", ".GGGGLGGGG..",
                    "..GGGGGGLGG.", ".GLGG.GGGG..", "..GGGGGG....", "....GG......", "....G.......",
                    "..TTTTTTTT..", "..TWTTWTTT..", "...TTTTTT...", "...TTTTTT...", "....TTTT...."],
                   {"G": "#86c9a0", "L": "#b5e6c6", "T": "#f2b8cc", "W": "#fbd9e5"}),
    "libros": _pix(["ab.cc.dd.ee.ff..", "ab.cc.dd.ee.ff.g", "ab.cc.dd.ee.ff.g", "ab.cc.dd.ee.ff.g", "ab.cc.dd.ee.ff.g"],
                   {"a": "#f4a7c3", "b": "#c9b6e8", "c": "#a8dcc1", "d": "#ffd59e", "e": "#f7b6cf", "f": "#9ec5f0", "g": "#e8c4b8"}),
    "nota": _pix(["NNNNNNN", "NLLLLLN", "NNNNNNN", "NLLLLNN", "NNNNNNN", "NLLLLLN", "NNNNNNN", "NLLLNNN"],
                 {"N": "#fffaf0", "L": "#e7c3d2"}),
}


def css_texturas():
    def bloque(f):
        return ";".join(f"--tx-{k}:{v}" for k, v in texturas(f).items())
    decor = ";".join(f"--px-{k}:{v}" for k, v in DECOR.items())
    dia, noche = bloque(1), bloque(0.42)
    return (f":root{{{dia};{decor}}}\n"
            f"@media (prefers-color-scheme:dark){{:root:not([data-theme=\"light\"]){{{noche}}}}}\n"
            f":root[data-theme=\"dark\"]{{{noche}}}")


def avatar_html(clave, nombre, rol, carpeta):
    """La foto va una sola vez en el CSS (clase av-<clave>); aquí solo se nombra."""
    ruta = Path(carpeta) / f"{clave}.png" if carpeta else None
    if ruta and ruta.is_file():
        return (f'<span class="foto av-{clave}" role="img" '
                f'aria-label="{html.escape(nombre)}"></span>'), True
    return svg_personaje(rol), False


def css_avatares(carpeta):
    reglas = []
    for clave, _n, _r, _z in AGENTES:
        ruta = Path(carpeta) / f"{clave}.png" if carpeta else None
        if ruta and ruta.is_file():
            datos = base64.b64encode(ruta.read_bytes()).decode()
            reglas.append(f".av-{clave}{{background-image:url(data:image/png;base64,{datos})}}")
    return "\n".join(reglas)


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M") if d else ""


def legible(d):
    if not d:
        return "—"
    return d.strftime("%d/%m/%Y" if (d.hour, d.minute) == (0, 0) else "%d/%m/%Y %H:%M")


def miles(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} M".replace(".", ",")
    if n >= 1000:
        return f"{round(n / 1000)} k"
    return str(n)


# ---------------------------------------------------- sesiones de Claude
def carpeta_sesiones():
    ambiente = str(BITACORA.parent)
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", ambiente)


def hora_local(ts):
    try:
        return (dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                .astimezone().replace(tzinfo=None, second=0, microsecond=0))
    except (AttributeError, TypeError, ValueError):
        return None


def modificado(ruta):
    return dt.datetime.fromtimestamp(ruta.stat().st_mtime)


def leer_transcripcion(ruta):
    """Solo metadatos: primera y última hora, título automático y tokens.

    Nunca se guarda el texto de la conversación."""
    inicio = fin = titulo = ultimo_msg = origen = ultimo_propio = None
    turno_trabajo = False   # el turno actual del coordinador usó herramientas (trabajó)
    usos = {}
    fichas = {}
    try:
        with open(ruta, encoding="utf-8") as fh:
            for linea in fh:
                if '"timestamp"' not in linea and '"aiTitle"' not in linea:
                    continue
                try:
                    d = json.loads(linea)
                except ValueError:
                    continue
                if d.get("type") == "ai-title" and d.get("aiTitle"):
                    titulo = d["aiTitle"]
                origen = origen or d.get("entrypoint")
                ts = hora_local(d.get("timestamp"))
                if ts:
                    inicio = inicio or ts
                    fin = ts
                msg = d.get("message") if d.get("type") == "assistant" else None
                if isinstance(msg, dict) and msg.get("id") and isinstance(msg.get("usage"), dict):
                    usos[msg["id"]] = msg["usage"]
                if d.get("type") == "assistant" and isinstance(d.get("message"), dict):
                    ultimo_propio = d["message"].get("stop_reason")   # vale también para subagentes
                elif d.get("type") == "user" and not d.get("isMeta"):
                    # La única lectura de contenido: la marca técnica que deja Claude Code
                    # cuando se detiene a un agente; el texto de la conversación no se usa.
                    m = d.get("message") if isinstance(d.get("message"), dict) else {}
                    c = m.get("content")
                    c = c if isinstance(c, str) else " ".join(x.get("text", "") for x in (c or []) if isinstance(x, dict))
                    ultimo_propio = "interrumpido" if c.strip().startswith("[Request interrupted") else None
                if d.get("type") in ("user", "assistant") and not d.get("isSidechain"):
                    m = d.get("message") if isinstance(d.get("message"), dict) else {}
                    cont = m.get("content") if isinstance(m.get("content"), list) else []
                    herramientas = [x.get("name") for x in cont if isinstance(x, dict) and x.get("type") == "tool_use"]
                    tipos_c = {x.get("type") for x in cont if isinstance(x, dict)}
                    if d["type"] == "user" and "tool_result" not in tipos_c and not d.get("isMeta"):
                        turno_trabajo = False          # mensaje nuevo de la usuaria: empieza otro turno
                    if herramientas:
                        turno_trabajo = True
                    ultimo_msg = {"tipo": d["type"], "stop": m.get("stop_reason"), "herramientas": herramientas, "ts": ts}
                    # qué ficha de la bitácora usa: solo la ruta en los parámetros de las herramientas
                    for x in cont:
                        if isinstance(x, dict) and x.get("type") == "tool_use":
                            entrada = x.get("input") if isinstance(x.get("input"), dict) else {}
                            ruta_txt = f'{entrada.get("file_path", "")} {entrada.get("command", "")}'
                            for f in RE_FICHA.findall(ruta_txt):
                                fichas[f] = fichas.get(f, 0) + 1
    except OSError:
        return None
    tokens = None
    if usos:
        ultimo = list(usos.values())[-1]
        tokens = {
            "contexto": sum(ultimo.get(k) or 0 for k in (
                "input_tokens", "cache_creation_input_tokens",
                "cache_read_input_tokens", "output_tokens")),
            "salida": sum(u.get("output_tokens") or 0 for u in usos.values()),
        }
    return {"inicio": inicio, "fin": fin, "titulo": titulo, "tokens": tokens,
            "ultimo_msg": ultimo_msg, "fichas": fichas, "origen": origen,
            "termino_turno": ultimo_propio in ("end_turn", "interrumpido"),
            "turno_trabajo": turno_trabajo}


RE_FICHA = re.compile(r"bitacora/planes/([A-Za-z0-9_]+)(?:\.[a-z]+)?\.md")


# Los títulos de sesión los genera Claude Code y a veces vienen sin tildes;
# se corrigen solo para mostrarlos (lista en config.json).
TILDES = CFG["tildes_en_titulos"]


def corrige_tildes(texto):
    def cambia(m):
        p = m.group(0)
        bien = TILDES.get(p.lower())
        if not bien:
            return p
        return bien.capitalize() if p[0].isupper() else bien
    return re.sub(r"[A-Za-zÁÉÍÓÚáéíóúñÑ]+", cambia, texto or "")


def leer_sesiones(carpeta, ahora):
    if not carpeta or not Path(carpeta).is_dir():
        return []
    activa = dt.timedelta(minutes=ACTIVA_MIN)
    viva = dt.timedelta(hours=SUBAGENTE_VIVO_H)
    sesiones = []
    for ruta in Path(carpeta).glob("*.jsonl"):
        sub = ruta.with_suffix("") / "subagents"
        ultima = modificado(ruta)
        subagentes, alguno_vivo = [], False
        for m in sorted(sub.glob("agent-*.meta.json")) if sub.is_dir() else []:
            jl = m.with_name(m.name.replace(".meta.json", ".jsonl"))
            try:
                meta = json.loads(m.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            mt = modificado(jl) if jl.exists() else modificado(m)
            ultima = max(ultima, mt)
            sin_terminar = not meta.get("finished")
            alguno_vivo = alguno_vivo or (sin_terminar and ahora - mt < viva)
            subagentes.append((meta, jl, mt, sin_terminar))
        if ahora - ultima > activa and not alguno_vivo:
            continue
        principal = leer_transcripcion(ruta) or {}
        encargos = {}
        fichas = dict(principal.get("fichas") or {})
        for meta, jl, mt, sin_terminar in subagentes:
            desc = meta.get("description", "")
            clave = agente_de(desc.split(":")[0]) or agente_de(desc)
            if not clave:
                continue
            nombre, rol = next((n, r) for c, n, r, _z in AGENTES if c == clave)
            tarea = re.sub(rf"^\s*{nombre}\s+(\d+\s+)?{rol}\s*:?\s*", "", desc, flags=re.I).strip()
            t = leer_transcripcion(jl) or {}
            if t.get("termino_turno"):
                sin_terminar = False   # entregó su informe final aunque falte la marca «finished»
            for f, n in (t.get("fichas") or {}).items():
                fichas[f] = fichas.get(f, 0) + n
            if not sin_terminar:
                estado = "libre"
            elif ahora - mt < viva:
                estado = "trabajando"
            else:
                estado = "pausa"
            encargos.setdefault(clave, []).append({
                "tarea": tarea or desc, "estado": estado, "inicio": t.get("inicio"),
                "fin": t.get("fin") or mt, "tokens": t.get("tokens"),
            })
        # ¿El coordinador necesita una respuesta de la usuaria para avanzar? Solo
        # cuando su último mensaje es una pregunta pendiente (con opciones) o un
        # plan esperando aprobación. Terminar un turno no cuenta.
        um = principal.get("ultimo_msg") or {}
        herr = um.get("herramientas") or []
        espera = None
        hay_agentes = any(x["estado"] == "trabajando" for lista in encargos.values() for x in lista)
        if um.get("tipo") == "assistant" and ("AskUserQuestion" in herr or "ExitPlanMode" in herr):
            espera = {"desde": um.get("ts"), "tipo": "pregunta" if "AskUserQuestion" in herr else "plan"}
        elif (um.get("tipo") == "assistant" and um.get("stop") == "end_turn"
              and principal.get("turno_trabajo") and not hay_agentes):
            # trabajó (usó herramientas), terminó su turno y no tiene agentes en marcha:
            # está parado hasta que le respondas. Una respuesta suelta, sin trabajo, no cuenta.
            espera = {"desde": um.get("ts"), "tipo": "turno"}
        ficha = max(fichas, key=fichas.get) if fichas else None
        donde = {"cli": T["sesion"]["en_terminal"], "claude-vscode": T["sesion"]["en_vscode"]}.get(principal.get("origen") or "", "")
        sin_titulo = f'{T["sesion"]["nueva"]} {donde}'.strip() + (f" ({principal['inicio']:%H:%M})" if principal.get("inicio") else "")
        sesiones.append({"id": ruta.stem, "titulo": corrige_tildes(principal.get("titulo")) or sin_titulo,
                         "espera": espera, "ficha": ficha,
                         "ultima": ultima, "inicio": principal.get("inicio"),
                         "principal": principal, "activa_ahora": ahora - modificado(ruta) < dt.timedelta(minutes=U["coordinador_activo_minutos"]),
                         "encargos": encargos, "n_subagentes": len(subagentes)})
    sesiones.sort(key=lambda s: s["ultima"], reverse=True)
    return sesiones


ESTADO_ENCARGO = T["estado_encargo"]


def agentes_de_sesion(ses):
    agentes = []
    for clave, nombre, rol, zona in AGENTES:
        base = {"clave": clave, "nombre": nombre, "rol": rol, "zona": zona, "etapa": "",
                "nota": "", "estado_txt": "", "prefijo": "",
                "vacio": T["sesion"]["sin_encargos"]}
        if clave == "hongjoong":
            p = ses["principal"]
            agentes.append({**base,
                            "estado": "pausa" if ses["espera"] else ("trabajando" if ses["activa_ahora"] else "pausa"),
                            "tarea": ses["titulo"],
                            "etapa": T["sesion"]["esperando_respuesta"] if ses["espera"] else T["sesion"]["esta_sesion"],
                            "desde": (ses["espera"]["desde"] if ses["espera"] else None) or ses["ultima"],
                            "prefijo": T["sesion"]["prefijo_esperando"] if ses["espera"] else T["sesion"]["prefijo_actividad"],
                            "tokens": p.get("tokens"),
                            "historial": [{"fecha": p.get("inicio"), "texto": T["sesion"]["inicio"].format(titulo=ses['titulo']),
                                           "estado": "", "nota": T["sesion"]["subagentes_total"].format(n=ses['n_subagentes']),
                                           "fuente": T["sesion"]["fuente"]}]})
            continue
        lista = sorted(ses["encargos"].get(clave, []),
                       key=lambda x: x["inicio"] or dt.datetime.min, reverse=True)
        actual = next((x for x in lista if x["estado"] != "libre"), lista[0] if lista else None)
        agentes.append({**base,
                        "estado": actual["estado"] if actual else "libre",
                        "tarea": actual["tarea"] if actual else "",
                        "desde": (actual["inicio"] if actual["estado"] == "trabajando" else actual["fin"]) if actual else None,
                        "tokens": actual["tokens"] if actual else None,
                        "historial": [{
                            "fecha": x["inicio"], "texto": x["tarea"], "estado": ESTADO_ENCARGO[x["estado"]],
                            "nota": (T["tokens"]["historial"].format(contexto=miles(x['tokens']['contexto']), salida=miles(x['tokens']['salida']))
                                     if x["tokens"] else ""),
                            "fuente": T["sesion"]["fuente_subagente"]} for x in lista[:6]]})
    return agentes


# ------------------------------------------------------------- vistas
SOL_LUNA = (
    '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="3.2" fill="#f6b73c"/>'
    '<g stroke="#f6b73c" stroke-width="1.6" stroke-linecap="round"><path d="M8 1.5v1.6M8 12.9v1.6M1.5 8h1.6M12.9 8h1.6'
    'M3.4 3.4l1.1 1.1M11.5 11.5l1.1 1.1M3.4 12.6l1.1-1.1M11.5 4.5l1.1-1.1"/></g></svg>'
    '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M10.8 2.2a6 6 0 1 0 3 9.6A5 5 0 0 1 10.8 2.2z" fill="#b39ddb"/></svg>'
    '<span class="perilla"></span>'
)


def petalos(n=16):
    rnd = random.Random(42)
    return ('<div class="petalos" aria-hidden="true">' + "".join(
        f'<i style="left:{rnd.uniform(0, 98):.1f}%;animation-duration:{rnd.uniform(11, 22):.1f}s;'
        f'animation-delay:-{rnd.uniform(0, 22):.1f}s;transform:scale({rnd.uniform(.7, 1.2):.2f})"></i>'
        for _ in range(n)) + "</div>")


ESTADO_TXT = T["estado"]

ZONAS = CFG["zonas"]


def html_tokens(t):
    if t is None:
        return ""
    if isinstance(t, int):
        return (f'<div class="tokens" title="{T["tokens"]["manual_tooltip"]}">'
                f'{T["tokens"]["manual"].format(n=f"{t:,}".replace(",", "."))}</div>')
    alto = " alto" if t["contexto"] >= U["tokens_alto"] else ""
    return (f'<div class="tokens{alto}" title="{T["tokens"]["sesion_tooltip"]}">'
            f'{T["tokens"]["sesion"].format(contexto=miles(t["contexto"]), salida=miles(t["salida"]))}</div>')


# ------------------------------------------------- sala isométrica (SVG)
M_SALA, N_SALA, ALTO_MURO, PAD = CFG["sala"]["ancho_baldosas"], CFG["sala"]["fondo_baldosas"], 132, 24
OX = N_SALA * 32 + PAD
OY = ALTO_MURO + 46
ANCHO_SVG = (M_SALA + N_SALA) * 32 + 2 * PAD
ALTO_SVG = OY + (M_SALA + N_SALA) * 16 + 18

# escritorio: esquina (x0, y0) de 2x1 baldosas; el agente se sienta detrás (menor y)
PUESTOS = {k: tuple(v) for k, v in CFG["sala"]["puestos"].items()}
MESA_REU = tuple(CFG["sala"]["mesa_reuniones"])   # la mesa de reuniones
ALFOMBRAS = {k: tuple(v) for k, v in CFG["sala"]["alfombras"].items()}  # x0, y0, x1, y1, rótulo


def P(x, y, z=0):
    return OX + (x - y) * 32, OY + (x + y) * 16 - z


def poli(puntos, clase, extra=""):
    pts = " ".join(f"{a:.1f},{b:.1f}" for a, b in puntos)
    return f'<polygon class="{clase}" points="{pts}"{extra}/>'


def caja(x0, y0, x1, y1, z0, h, clase):
    t = z0 + h
    return (poli([P(x0, y0, t), P(x1, y0, t), P(x1, y1, t), P(x0, y1, t)], f"{clase} cara-top")
            + poli([P(x1, y0, z0), P(x1, y1, z0), P(x1, y1, t), P(x1, y0, t)], f"{clase} cara-x")
            + poli([P(x0, y1, z0), P(x1, y1, z0), P(x1, y1, t), P(x0, y1, t)], f"{clase} cara-y"))


def corto(txt, n):
    txt = limpiar_md(txt or "")
    return txt if len(txt) <= n else txt[:n - 1].rstrip() + "…"


def etiqueta(cx, base, titulo, linea, estado):
    e = html.escape
    ancho = min(190, max(len(titulo) * 7.4 + 30, len(linea) * 5.9 + 20))
    alto = 34 if linea else 22
    x, y = cx - ancho / 2, base - alto
    return (f'<g class="tag">'
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{ancho:.1f}" height="{alto}" rx="9"/>'
            f'<path class="tag-pico" d="M{cx - 5:.1f} {base:.1f} L{cx:.1f} {base + 6:.1f} L{cx + 5:.1f} {base:.1f} Z"/>'
            f'<circle class="dot-{estado}" cx="{x + 12:.1f}" cy="{y + 11:.1f}" r="4"/>'
            f'<text class="tag-nombre" x="{x + 21:.1f}" y="{y + 15:.1f}">{e(titulo)}</text>'
            + (f'<text class="tag-linea" x="{cx:.1f}" y="{y + 28:.1f}" text-anchor="middle">{e(linea)}</text>' if linea else "")
            + "</g>")


def cilindro(cx, cy, rx, ry, alto, clase):
    """Cilindro isométrico: (cx, cy) es el centro de la base en pantalla."""
    t = cy - alto
    lado = (f'M{cx - rx:.1f} {t:.1f} L{cx - rx:.1f} {cy:.1f} A{rx} {ry} 0 0 0 {cx + rx:.1f} {cy:.1f} '
            f'L{cx + rx:.1f} {t:.1f} A{rx} {ry} 0 0 1 {cx - rx:.1f} {t:.1f} Z')
    return (f'<path class="{clase} cara-x" d="{lado}"/>'
            f'<ellipse class="{clase} cara-top" cx="{cx:.1f}" cy="{t:.1f}" rx="{rx}" ry="{ry}"/>')


def pizarra_plan(cx, base, etapas):
    """Pizarrón con el plan (etapas del cambio), colgado en la pared."""
    e = html.escape
    W = 176
    filas = etapas or []
    Hb = 28 + max(1, len(filas)) * 13
    x, y = cx - W / 2, base - Hb
    out = [f'<g class="pizarra"><rect class="piz-marco" x="{x - 5:.1f}" y="{y - 5:.1f}" width="{W + 10}" height="{Hb + 10}" rx="10"/>'
           f'<rect class="piz-fondo" x="{x:.1f}" y="{y:.1f}" width="{W}" height="{Hb}" rx="7"/>'
           f'<text class="piz-tit" x="{cx:.1f}" y="{y + 18:.1f}" text-anchor="middle">{T["sala"]["plan_titulo"]}</text>']
    if not filas:
        out.append(f'<text class="piz-txt" x="{cx:.1f}" y="{y + 38:.1f}" text-anchor="middle">{T["sala"]["sin_plan"]}</text>')
    for k, et in enumerate(filas):
        fy = y + 32 + k * 13
        cls = f"piz-fila f-{et['estado']}" + (" actual" if et["estado"] == "trabajando" else "")
        out.append(f'<g class="{cls}"><title>{T["sala"]["etapa"].format(n=et["num"])}: {e(et["desc"])} — {e(et["estado_txt"])}</title>'
                   f'<rect class="piz-resalte" x="{x + 5:.1f}" y="{fy - 9.5:.1f}" width="{W - 10}" height="12.5" rx="6"/>'
                   f'<circle class="piz-dot" cx="{x + 14:.1f}" cy="{fy - 3:.1f}" r="4"/>'
                   f'<text class="piz-num" x="{x + 23:.1f}" y="{fy:.1f}">E{et["num"]}</text>'
                   f'<text class="piz-txt" x="{x + 44:.1f}" y="{fy:.1f}">{e(corto(et["desc"], 22))}</text></g>')
    out.append("</g>")
    return "".join(out), y - 5


GAP_CARTEL = 10                          # mismo espacio (px) entre todos los carteles de la pared
PASO_CARTEL = (92 + GAP_CARTEL) / 32     # 92 = ancho de un cartel
INICIO_DER = 0.4
# el plan empieza un GAP_CARTEL después de «Entrega» (su marco mide 176 + 2·5 px)
PLAN_U = INICIO_DER + 3 * PASO_CARTEL + (92 + GAP_CARTEL + 5 + 176 / 2) / 32   # centro del pizarrón del plan sobre la pared derecha (en baldosas)


def mesa_reunion(sentados, etapas, n_dec, sufijo):
    """Mesa rectangular de tamaño fijo: 4 asientos por lado (uno por integrante)
    sobre cuatro patas; el plan está en la pared."""
    x0, y0, x1, y1 = MESA_REU
    ALTO = 24
    ym = (y0 + y1) / 2
    ZONA_PLAN = 0
    paso = (x1 - x0) / 4
    delante = [(x0 + paso * (i + .5), y1 + .55) for i in range(4)]
    detras = [(x0 + paso * (i + .5), y0 - .55) for i in range(4)]
    ocupados = sentados[:8]
    # se llenan primero los asientos de adelante (los que mejor se ven)
    asignados = list(zip(delante + detras, ocupados + [None] * (8 - len(ocupados))))

    def asiento(xt, yt, ag):
        sx, sy = P(xt, yt)
        g = [cilindro(sx, sy, 13, 6.5, 16, "banco")]
        if not ag:
            return "".join(g)
        ropa = ROL_PIXEL[ag["rol"]][2]
        by = sy - 16
        g.append(f'<g class="figura" data-fig="{ag["clave"]}">')
        g.append(cuerpo_svg(sx, by + 3, 30, ropa))
        g.append(f'<use href="#cab-{ag["clave"]}" x="{sx - 18:.1f}" y="{by - 62:.1f}" width="36" height="36"/>')
        g.append(f'<use href="#dec-lazo" x="{sx - 9:.1f}" y="{by - 68:.1f}" width="18" height="10"/>')
        if ag["estado"] == "bloqueado":
            g.append(f'<g class="marca-bloq"><circle cx="{sx + 17:.1f}" cy="{by - 58:.1f}" r="8"/>'
                     f'<text x="{sx + 17:.1f}" y="{by - 54:.1f}" text-anchor="middle">!</text></g>')
        elif ag["estado"] == "pausa":
            g.append(f'<text class="marca-zz" x="{sx + 14:.1f}" y="{by - 56:.1f}">z<tspan dy="-5">z</tspan></text>')
        g.append('</g>')
        nombre = html.escape(ag["nombre"])
        return (f'<g class="agente est-{ag["estado"]}" tabindex="0" role="button" data-dlg="hist-{ag["clave"]}-{sufijo}" '
                f'aria-label="{nombre} {html.escape(ag["rol"])}: {ESTADO_TXT[ag["estado"]]}">'
                f'<title>{nombre} {html.escape(ag["rol"])} — {ESTADO_TXT[ag["estado"]]}'
                f'{": " + html.escape(ag["tarea"]) if ag["tarea"] else ""}</title>' + "".join(g) + "</g>")

    sx0, sy0 = P(x0 - .3, y0 - .3)
    sx1, sy1 = P(x1 + .3, y1 + .3)
    partes = [poli([P(x0 - .25, y0 - .25), P(x1 + .25, y0 - .25), P(x1 + .25, y1 + .25), P(x0 - .25, y1 + .25)], "sombra-mesa")]
    partes += [asiento(xt, yt, ag) for (xt, yt), ag in asignados if yt < ym]          # lado de atrás
    GROSOR, PATA = 5, .22                           # tablero delgado sobre cuatro patas
    for lx, ly in ((x0 + .15, y0 + .15), (x1 - .15 - PATA, y0 + .15), (x0 + .15, y1 - .15 - PATA), (x1 - .15 - PATA, y1 - .15 - PATA)):
        partes.append(caja(lx, ly, lx + PATA, ly + PATA, 0, ALTO - GROSOR, "mesa pata"))
    partes.append(caja(x0, y0, x1, y1, ALTO - GROSOR, GROSOR, "mesa"))
    for k in range(min(n_dec, 6)):                                                     # una hoja por pendiente
        hx, hy = P(x0 + .6 + k * .8, ym + (.25 if k % 2 else -.25), ALTO)
        partes.append(f'<polygon class="hoja" points="{hx:.1f},{hy - 4:.1f} {hx + 11:.1f},{hy + 1.5:.1f} '
                      f'{hx + 1:.1f},{hy + 7:.1f} {hx - 10:.1f},{hy + 1.5:.1f}"/>')
    partes += [asiento(xt, yt, ag) for (xt, yt), ag in asignados if yt > ym]          # lado de adelante
    if n_dec:
        bx, by = P((x0 + x1) / 2, ym, ALTO + 70)
        texto = T["sala"]["aviso_mesa"] if n_dec == 1 else T["sala"]["aviso_mesa_varios"].format(n=n_dec)
        ancho = len(texto) * 6.4 + 34
        partes.append(
            f'<g class="aviso-mesa" data-dlg="dlg-mesa-{sufijo}" tabindex="0" role="button" aria-label="{T["sala"]["aviso_mesa_aria"].format(n=n_dec)}">'
            f'<title>{T["sala"]["aviso_mesa_tooltip"]}</title><g class="rebota">'
            f'<rect x="{bx - ancho / 2:.1f}" y="{by - 26:.1f}" width="{ancho:.1f}" height="26" rx="13"/>'
            f'<path class="aviso-pico" d="M{bx - 6:.1f} {by - 1:.1f} L{bx:.1f} {by + 8:.1f} L{bx + 6:.1f} {by - 1:.1f} Z"/>'
            f'<circle class="aviso-alerta" cx="{bx - ancho / 2 + 14:.1f}" cy="{by - 13:.1f}" r="8"/>'
            f'<text class="aviso-signo" x="{bx - ancho / 2 + 14:.1f}" y="{by - 9:.1f}" text-anchor="middle">!</text>'
            f'<text class="aviso-txt" x="{bx - ancho / 2 + 27:.1f}" y="{by - 9:.1f}">{html.escape(texto)}</text>'
            '</g></g>')
    # nombres justo debajo de los pies de cada agente (capa de arriba)
    nombres = []
    for (xt, yt), ag in asignados:
        if not ag:
            continue
        sx, sy = P(xt, yt)
        w = min(58, len(ag["nombre"]) * 6.2 + 12)
        ny = sy + 5
        nombres.append(f'<g class="nombre-mesa" data-fig="{ag["clave"]}" data-dlg="hist-{ag["clave"]}-{sufijo}">'
                       f'<rect x="{sx - w / 2:.1f}" y="{ny:.1f}" width="{w:.1f}" height="14" rx="7"/>'
                       f'<text x="{sx:.1f}" y="{ny + 10:.1f}" text-anchor="middle" textLength="{w - 10:.1f}" '
                       f'lengthAdjust="spacingAndGlyphs">{html.escape(ag["nombre"])}</text></g>')
    titulo = T["sala"]["mesa_con_pendientes"].format(n=n_dec) if n_dec else T["sala"]["mesa"]
    return f'<g class="mesa-reu" aria-label="{titulo}">' + "".join(partes) + "</g>", "".join(nombres)


def _mezcla(hexa, otro, t):
    a = [int(hexa[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(otro[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def cuerpo_svg(cx, base, alto, ropa):
    """Cuerpito chibi: hombros redondos, sombra de un lado, cuello con solapa,
    bracitos y manos. base = línea donde se apoya; alto = altura del torso."""
    oscuro, claro, piel = _mezcla(ropa, "#000000", .22), _mezcla(ropa, "#ffffff", .35), "#f6d3b0"
    w = alto * 0.55            # medio ancho del torso
    top = base - alto
    torso = (f'M{cx - w:.1f} {base:.1f} L{cx - w:.1f} {top + 10:.1f} '
             f'Q{cx - w:.1f} {top:.1f} {cx - w + 9:.1f} {top:.1f} L{cx + w - 9:.1f} {top:.1f} '
             f'Q{cx + w:.1f} {top:.1f} {cx + w:.1f} {top + 10:.1f} L{cx + w:.1f} {base:.1f} Z')
    sombra = (f'M{cx + w * .35:.1f} {top + 1:.1f} L{cx + w - 9:.1f} {top:.1f} '
              f'Q{cx + w:.1f} {top:.1f} {cx + w:.1f} {top + 10:.1f} L{cx + w:.1f} {base:.1f} L{cx + w * .35:.1f} {base:.1f} Z')
    cuello = f'M{cx - 6:.1f} {top:.1f} L{cx:.1f} {top + 8:.1f} L{cx + 6:.1f} {top:.1f} Z'
    brazo = lambda lado: (f'<rect x="{cx + lado * (w + 1) - (5 if lado > 0 else 1):.1f}" y="{top + 6:.1f}" width="6" '  # noqa: E731
                          f'height="{alto * .62:.1f}" rx="3" fill="{oscuro}"/>'
                          f'<circle cx="{cx + lado * (w + 1) + (-2 if lado > 0 else 2):.1f}" cy="{top + 6 + alto * .62:.1f}" r="3.4" fill="{piel}"/>')
    return (f'<g class="cuerpo">{brazo(-1)}{brazo(1)}'
            f'<path d="{torso}" fill="{ropa}"/><path d="{sombra}" fill="{oscuro}" opacity=".45"/>'
            f'<path d="{cuello}" fill="{claro}"/>'
            f'<circle cx="{cx:.1f}" cy="{top + 15:.1f}" r="1.6" fill="{claro}"/>'
            f'<circle cx="{cx:.1f}" cy="{top + 21:.1f}" r="1.6" fill="{claro}"/></g>')


def burbuja(cx, base):
    """Globo de diálogo «Trabajando…» sobre la cabeza (puntos animados)."""
    ancho, alto = 92, 24
    x, y = cx - ancho / 2, base - alto
    return (f'<g class="burbuja" aria-hidden="true"><g class="burbuja-mueve">'
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{ancho}" height="{alto}" rx="12"/>'
            f'<path class="burbuja-pico" d="M{cx - 6:.1f} {base - 1:.1f} L{cx - 1:.1f} {base + 7:.1f} L{cx + 5:.1f} {base - 1:.1f} Z"/>'
            f'<text x="{x + 12:.1f}" y="{y + 16:.1f}">{T["sala"]["burbuja"]}<tspan class="pt1">.</tspan>'
            '<tspan class="pt2">.</tspan><tspan class="pt3">.</tspan></text></g></g>')


def defs_svg(carpeta):
    """Cabezas y adornos definidos una sola vez; las salas los reutilizan con <use>."""
    partes = ['<clipPath id="clip-cab"><rect width="40" height="40" rx="11"/></clipPath>']
    for clave, nombre, rol, _z in AGENTES:
        ruta = Path(carpeta) / f"{clave}.png" if carpeta else None
        if ruta and ruta.is_file():
            datos = base64.b64encode(ruta.read_bytes()).decode()
            dentro = (f'<image href="data:image/png;base64,{datos}" width="40" height="44" y="-1" '
                      'preserveAspectRatio="xMidYMin slice" clip-path="url(#clip-cab)"/>')
        else:
            dentro = (f'<g clip-path="url(#clip-cab)">'
                      + svg_personaje(rol).replace("<svg ", '<svg width="40" height="40" ', 1) + "</g>")
        partes.append(f'<symbol id="cab-{clave}" viewBox="0 0 40 40">{dentro}'
                      '<rect class="cab-borde" width="40" height="40" rx="11"/></symbol>')
    for nombre in ("maceta", "planta", "lazo"):
        uri = DECOR[nombre][5:-2]  # url("…") -> …
        partes.append(f'<symbol id="dec-{nombre}" viewBox="0 0 10 10" preserveAspectRatio="none">'
                      f'<image href="{uri}" width="10" height="10" preserveAspectRatio="none" '
                      'style="image-rendering:pixelated"/></symbol>')
    return ('<svg class="defs" width="0" height="0" aria-hidden="true" focusable="false"><defs>'
            + "".join(partes) + "</defs></svg>")


def fase_actual(fases):
    """La fase en curso más reciente; si no hay, la más reciente en pausa o bloqueada."""
    for estados in (("trabajando",), ("bloqueado", "pausa")):
        cand = [f for f in fases if f["estado"] in estados]
        if cand:
            return max(cand, key=lambda f: f["fecha"] or dt.datetime.min)["clave"]
    return None


def carteles(datos):
    e = html.escape
    actual = fase_actual(datos["fases"])
    out = []
    W, Hc, Y = 92, 86, -116
    for i, f in enumerate(datos["fases"]):
        if i < 4:   # pared izquierda (x = 0), de adelante hacia la esquina
            u = -(N_SALA - 0.9 - i * PASO_CARTEL) * 32
            mat = f"matrix(1,-0.5,0,1,{OX},{OY})"
        else:       # pared derecha (y = 0), desde la esquina
            u = (INICIO_DER + (i - 4) * PASO_CARTEL) * 32
            mat = f"matrix(1,0.5,0,1,{OX},{OY})"
        es_actual = f["clave"] == actual
        cls = f'cartel-muro f-{f["estado"]}' + (" actual" if es_actual else "")
        estado = ESTADO_FASE.get(f["estado"], f["estado"])
        out.append(
            f'<g class="{cls}" transform="{mat}"><title>{i + 1}. {e(f["nombre"])}: {e(f["estado_txt"])}'
            f'{" · " + legible(f["fecha"]) if f["fecha"] else ""}</title>'
            f'<rect class="cartel-sombra" x="{u + 3:.1f}" y="{Y + 4}" width="{W}" height="{Hc}" rx="10"/>'
            f'<rect class="cartel-fondo" x="{u:.1f}" y="{Y}" width="{W}" height="{Hc}" rx="10"/>'
            f'<rect class="cartel-banda" x="{u:.1f}" y="{Y}" width="{W}" height="26" rx="10"/>'
            f'<rect class="cartel-banda" x="{u:.1f}" y="{Y + 14}" width="{W}" height="12"/>'
            f'<circle class="cartel-circulo" cx="{u + 18:.1f}" cy="{Y + 13}" r="9"/>'
            f'<text class="cartel-num" x="{u + 18:.1f}" y="{Y + 17.5}" text-anchor="middle">{i + 1}</text>'
            f'<text class="cartel-estado" x="{u + 32:.1f}" y="{Y + 17}">{e(estado)}</text>'
            f'<text class="cartel-nombre" x="{u + W / 2:.1f}" y="{Y + 50}" text-anchor="middle">{e(corto(f["nombre"], 14))}</text>'
            f'<text class="cartel-fecha" x="{u + W / 2:.1f}" y="{Y + 68}" text-anchor="middle">'
            f'{f["fecha"].strftime("%d/%m") if f["fecha"] else "—"}</text>'
            + (f'<g class="cartel-ahora"><rect x="{u + W / 2 - 26:.1f}" y="{Y + Hc - 9}" width="52" height="16" rx="8"/>'
               f'<text x="{u + W / 2:.1f}" y="{Y + Hc + 2.5}" text-anchor="middle">{T["sala"]["ahora"]}</text></g>' if es_actual else "")
            + "</g>")
    return "".join(out)


ESTADO_FASE = T["estado_fase"]


def sala_svg(agentes, datos, sufijo, n_dec):
    capas = []
    # piso
    for x in range(M_SALA):
        for y in range(N_SALA):
            capas.append(poli([P(x, y), P(x + 1, y), P(x + 1, y + 1), P(x, y + 1)], "piso-a" if (x + y) % 2 else "piso-b"))
    # paredes
    H = ALTO_MURO
    capas.append(poli([P(0, 0), P(0, N_SALA), P(0, N_SALA, H), P(0, 0, H)], "muro-l"))
    capas.append(poli([P(0, 0), P(M_SALA, 0), P(M_SALA, 0, H), P(0, 0, H)], "muro-r"))
    capas.append(poli([P(0, 0), P(0, N_SALA), P(0, N_SALA, 10), P(0, 0, 10)], "zocalo-l"))
    capas.append(poli([P(0, 0), P(M_SALA, 0), P(M_SALA, 0, 10), P(0, 0, 10)], "zocalo-r"))
    capas.append(poli([P(0, 0, H), P(0, N_SALA, H), P(-0.25, N_SALA, H + 6), P(-0.25, -0.25, H + 6)], "muro-tope"))
    capas.append(poli([P(0, 0, H), P(M_SALA, 0, H), P(M_SALA, -0.25, H + 6), P(-0.25, -0.25, H + 6)], "muro-tope"))
    # el plan del cambio, colgado en la pared derecha al lado de «Entrega»
    capas.append(f'<g class="plan-muro" data-dlg="dlg-plan-{sufijo}" tabindex="0" role="button" '
                 f'aria-label="{T["plan"]["abrir"]}"><title>{T["plan"]["abrir"]}</title>'
                 f'<g transform="matrix(1,0.5,0,1,{OX},{OY})">'
                 + pizarra_plan(PLAN_U * 32, -6, datos["etapas"])[0] + '</g></g>')
    capas.append(carteles(datos))
    # alfombras
    for zona, (x0, y0, x1, y1, _r) in ALFOMBRAS.items():
        capas.append(poli([P(x0, y0, 1), P(x1, y0, 1), P(x1, y1, 1), P(x0, y1, 1)], f"alfombra al-{zona}"))
    objetos = []
    for zona, (x0, y0, x1, y1, esquina) in ALFOMBRAS.items():
        # rótulo en el borde delantero de la alfombra, centrado en su lado más largo
        if (x1 - x0) >= (y1 - y0):
            cx, cy = P((x0 + x1) / 2, y1)
        else:
            cx, cy = P(x1, (y0 + y1) / 2)
        objetos.append((-1, f'<g class="rotulo"><rect x="{cx - 52:.1f}" y="{cy - 4:.1f}" width="104" height="19" rx="9.5"/>'
                        f'<text x="{cx:.1f}" y="{cy + 10:.1f}" text-anchor="middle">{ZONAS[zona]}</text></g>'))
    # plantas en las esquinas del fondo
    for (px, py) in ((0.5, 0.5), (0.6, 14.4)):
        sx, sy = P(px, py)
        objetos.append((px + py, f'<use href="#dec-planta" x="{sx - 22:.1f}" y="{sy - 62:.1f}" width="44" height="62"/>'))
    # mesa de reuniones redonda, con banquitos
    sentados = [a for a in agentes if a["estado"] != "trabajando"]
    mesa_svg, nombres_mesa = mesa_reunion(sentados, datos["etapas"], n_dec, sufijo)
    objetos.append((MESA_REU[1] + MESA_REU[3], mesa_svg))
    # puestos
    for a in agentes:
        x0, y0 = PUESTOS[a["clave"]]
        partes = []
        partes.append(caja(x0 + 0.62, y0 - 1.05, x0 + 1.38, y0 - 0.92, 16, 30, "silla"))  # respaldo
        partes.append(caja(x0 + 0.62, y0 - 1.0, x0 + 1.38, y0 - 0.3, 0, 16, "silla"))     # asiento
        cx, cy = P(x0 + 1.0, y0 - 0.62, 16)
        ropa = ROL_PIXEL[a["rol"]][2]
        en_mesa = a["estado"] != "trabajando"
        if not en_mesa:
            partes.append(f'<g class="figura" data-fig="{a["clave"]}">')
            partes.append(cuerpo_svg(cx, cy + 4, 32, ropa))
            partes.append(f'<use href="#cab-{a["clave"]}" x="{cx - 19:.1f}" y="{cy - 66:.1f}" width="38" height="38"/>')
            partes.append(f'<use href="#dec-lazo" x="{cx - 10:.1f}" y="{cy - 72:.1f}" width="20" height="11"/>')
            partes.append('</g>')
        partes.append(caja(x0, y0, x0 + 2, y0 + 1, 0, 26, "mesa"))
        px, py = P(x0, y0 + 1, 0)   # placa con el nombre en el frente del escritorio
        partes.append(f'<g class="placa" transform="matrix(1,0.5,0,1,{px:.1f},{py:.1f})">'
                      f'<rect x="5" y="-23" width="54" height="19" rx="3"/>'
                      f'<text class="placa-nombre" x="32" y="-13" text-anchor="middle">{html.escape(a["nombre"])}</text>'
                      f'<text class="placa-rol" x="32" y="-6.5" text-anchor="middle">{html.escape(a["rol"].upper())}</text></g>')
        partes.append(caja(x0 + 0.18, y0 + 0.12, x0 + 0.36, y0 + 0.82, 26, 4, "monitor"))   # base
        partes.append(caja(x0 + 0.24, y0 + 0.05, x0 + 0.32, y0 + 0.9, 30, 24, "monitor"))   # pantalla
        sx0, sy0 = P(x0 + 0.32, y0 + 0.12, 33)
        sx1, sy1 = P(x0 + 0.32, y0 + 0.83, 33)
        partes.append(f'<polygon class="pantalla-iso" points="{sx0:.1f},{sy0:.1f} {sx1:.1f},{sy1:.1f} '
                      f'{sx1:.1f},{sy1 - 18:.1f} {sx0:.1f},{sy0 - 18:.1f}"/>')
        mx, my = P(x0 + 1.65, y0 + 0.5, 26)
        partes.append(f'<use href="#dec-maceta" x="{mx - 9:.1f}" y="{my - 22:.1f}" width="18" height="24"/>')
        hx, hy = P(x0 + 1.0, y0 + 0.45, 26)
        partes.append(f'<polygon class="hoja" points="{hx:.1f},{hy - 3:.1f} {hx + 11:.1f},{hy + 2:.1f} {hx + 2:.1f},{hy + 7:.1f} {hx - 9:.1f},{hy + 2:.1f}"/>')
        if a["estado"] == "trabajando":
            partes.append(burbuja(cx, cy - 74))
        objetos.append((x0 + y0 + 1, f'<g class="agente {"escritorio-vacio est-libre" if en_mesa else "est-" + a["estado"]}" tabindex="0" role="button" '
                        f'data-dlg="hist-{a["clave"]}-{sufijo}" aria-label="{html.escape(a["nombre"])} {html.escape(a["rol"])}: '
                        f'{ESTADO_TXT[a["estado"]]}"><title>{html.escape(a["nombre"])} {html.escape(a["rol"])} — '
                        f'{ESTADO_TXT[a["estado"]]}{": " + html.escape(a["tarea"]) if a["tarea"] else ""}</title>'
                        + "".join(partes) + "</g>"))
    objetos.sort(key=lambda o: o[0])
    capas += [o[1] for o in objetos]
    capas.append(f'<g class="capa-nombres">{nombres_mesa}</g>')   # encima de todo: nunca se tapan
    return (f'<svg class="sala" viewBox="0 0 {ANCHO_SVG} {ALTO_SVG}" preserveAspectRatio="xMidYMid meet" '
            f'role="img" aria-label="{T["sala"]["aria"]}">' + "".join(capas) + "</svg>")


# ------------------------------------------------------- panel lateral
def mini_avatar(a, carpeta):
    ruta = Path(carpeta) / f"{a['clave']}.png" if carpeta else None
    if ruta and ruta.is_file():
        return f'<span class="mini-av foto av-{a["clave"]}"></span>'
    return f'<span class="mini-av">{svg_personaje(a["rol"])}</span>'


def fila_agente(a, carpeta, sufijo):
    e = html.escape
    prefijo = f' data-prefijo="{e(a["prefijo"])}"' if a.get("prefijo") else ""
    if a.get("vacio"):
        prefijo += f' data-vacio="{e(a["vacio"])}"'
    tarea = e(a["tarea"]) if a["tarea"] else T["agente"]["sin_tarea_asignada"]
    if a["tarea"] and a["estado"] == "libre":
        tarea = T["agente"]["ultima"] + " " + tarea
    return (f'<li><button type="button" class="fila est-{a["estado"]}" data-dlg="hist-{a["clave"]}-{sufijo}" '
            f'title="{e(a["tarea"]) if a["tarea"] else T["agente"]["sin_tarea"]}{T["agente"]["desde"].format(fecha=legible(a["desde"])) if a["desde"] else ""}">'
            f'{mini_avatar(a, carpeta)}<span class="fila-txt"><span class="fila-cab"><b>{e(a["nombre"])}</b>'
            f'<span class="rol">{e(a["rol"])}</span>'
            f'<span class="pildora p-{a["estado"]}"><span class="punto"></span>{ESTADO_TXT[a["estado"]]}</span></span>'
            f'<span class="fila-tarea">{tarea}</span></span></button></li>')


def dialogo_agente(a, sufijo, carpeta):
    e = html.escape
    hist = "".join(
        f'<li><time>{legible(h["fecha"])}</time><div>{e(h["texto"])}'
        + (f' <span class="mini">· {e(h["estado"])}</span>' if h["estado"] else "")
        + (f'<div class="mini">{e(recortar(h["nota"], 160))}</div>' if h["nota"] else "")
        + f'<div class="fuente">{e(h["fuente"])}</div></div></li>'
        for h in a["historial"]) or f'<li class="vacio">{T["agente"]["sin_historial"]}</li>'
    prefijo = f' data-prefijo="{e(a["prefijo"])}"' if a.get("prefijo") else ""
    if a.get("vacio"):
        prefijo += f' data-vacio="{e(a["vacio"])}"'
    ident = f"{a['clave']}-{sufijo}"
    return (f'<dialog id="hist-{ident}" class="historial" aria-labelledby="ht-{ident}">'
            f'<header>{mini_avatar(a, carpeta)}<h4 id="ht-{ident}">{e(a["nombre"])} <span class="rol">{e(a["rol"])}</span></h4>'
            f'<button type="button" class="cerrar" aria-label="{T["agente"]["cerrar"]}">×</button></header>'
            f'<div class="resumen"><span class="pildora p-{a["estado"]}"><span class="punto"></span>{ESTADO_TXT[a["estado"]]}</span>'
            f'<p class="tarea">{e(a["tarea"]) if a["tarea"] else T["agente"]["sin_tarea_asignada"]}</p>'
            + (f'<span class="etapa">{e(a["etapa"])}</span>' if a["etapa"] else "")
            + f'<span class="tiempo" data-desde="{iso(a["desde"])}" data-estado="{a["estado"]}"{prefijo}>—</span>'
            + html_tokens(a.get("tokens")) + "</div>"
            f'<h5>{T["agente"]["historial"]}</h5><ol>{hist}</ol></dialog>')


def trabajando_ahora(agentes, carpeta, sufijo):
    e = html.escape
    activos = [a for a in agentes if a["estado"] == "trabajando"]
    if not activos:
        return ""
    chips = "".join(
        f'<button type="button" class="ahora-chip" data-dlg="hist-{a["clave"]}-{sufijo}" title="{e(a["tarea"])}">'
        f'{mini_avatar(a, carpeta)}<span><b>{e(a["nombre"])}</b> <span class="rol">{e(a["rol"])}</span>'
        f'<small>{e(corto(a["tarea"], 34))}</small></span></button>' for a in activos)
    return f'<div class="ahora"><span class="ahora-tit"><i class="ahora-dot"></i>{T["sala"]["trabajando_ahora"].format(n=len(activos))}</span>{chips}</div>'


DIAS = T["limites"]["dias"]


def limites_html(ruta):
    """Medidores de la ventana de 5 horas y de la semana (de limites.json)."""
    try:
        lim = json.loads(Path(ruta).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return ""
    ahora = time.time()
    viejo = ahora - (lim.get("actualizado") or 0) > U["limite_viejo_minutos"] * 60

    def uno(nombre, t, con_dia):
        if not t or t.get("usado") is None:
            return ""
        pct = max(0.0, min(100.0, float(t["usado"])))
        nivel = ("rojo" if pct >= U["limite_rojo_porcentaje"] else
                 ("ambar" if pct >= U["limite_ambar_porcentaje"] else "ok"))
        reinicia = ""
        if t.get("reinicia"):
            try:
                r = dt.datetime.fromtimestamp(int(t["reinicia"]))
                if r.timestamp() < ahora:
                    reinicia = T["limites"]["ya_se_reinicio"]
                else:
                    reinicia = T["limites"]["se_reinicia"].format(
                        cuando=f"{DIAS[r.weekday()]} {r:%H:%M}" if con_dia else f"{r:%H:%M}")
            except (ValueError, OSError, OverflowError):
                pass
        return (f'<div class="limite l-{nivel}" title="{T["limites"]["tooltip"].format(nombre=nombre, pct=f"{pct:.0f}", reinicia=reinicia)}">'
                f'<span class="lim-nombre">{nombre}</span>'
                f'<span class="lim-barra"><i style="width:{pct:.1f}%"></i></span>'
                f'<span class="lim-pct">{pct:.0f}%</span>'
                f'<span class="lim-reinicia">{reinicia}</span></div>')
    cuerpo = (uno(T["limites"]["cinco_horas"], lim.get("cinco_horas"), False)
              + uno(T["limites"]["semana"], lim.get("semana"), True))
    if not cuerpo:
        return ""
    nota = f' title="{T["limites"]["viejo"].format(min=U["limite_viejo_minutos"]) if viejo else T["limites"]["actualizar"]}"'
    hora = ""
    if lim.get("actualizado"):
        hora = (f'<span class="lim-hora">'
                f'{T["limites"]["actualizado"].format(hora=dt.datetime.fromtimestamp(lim["actualizado"]).strftime("%H:%M"))}</span>')
    return f'<div class="limites{" viejo" if viejo else ""}"{nota}>{cuerpo}{hora}</div>'


def datos_vacios():
    return {"fases": [{"clave": c, "nombre": n, "estado": "pendiente", "estado_txt": T["agente"]["sin_ficha"], "fecha": None, "agente": None}
                      for c, n, _p in FASES], "etapas": [], "decisiones": [], "bloqueos": []}


def contador(agentes):
    r = {k: sum(1 for a in agentes if a["estado"] == k)
         for k in ("trabajando", "pausa", "bloqueado", "libre")}
    c = T["contador"]
    bloq = c["bloqueado_uno"] if r["bloqueado"] == 1 else c["bloqueado_varios"]
    return (f'<ul class="contador" aria-label="{c["aria"]}">'
            f'<li class="p-trabajando"><span class="punto"></span>{c["trabajando"].format(n=r["trabajando"])}</li>'
            f'<li class="p-pausa"><span class="punto"></span>{c["pausa"].format(n=r["pausa"])}</li>'
            f'<li class="p-bloqueado"><span class="punto"></span>{bloq.format(n=r["bloqueado"])}</li>'
            f'<li class="p-libre"><span class="punto"></span>{c["libre"].format(n=r["libre"])}</li></ul>')


def pagina(datos, fichas, sesiones, modulo, carpeta_avatares, cada, generado, version=""):
    e = html.escape

    def items(lista, clase):
        return "".join(
            f'<li class="{clase}"><p>{e(i["texto"])}</p><small>{e(i["fuente"])}'
            f'{" · " + legible(i["fecha"]) if i["fecha"] else ""}</small></li>' for i in lista)

    def espera_item(ses):
        desde = ses["espera"]["desde"]
        clave = {"pregunta": "espera_pregunta", "plan": "espera_plan"}.get(ses["espera"]["tipo"], "espera_turno")
        return {"texto": T["sesion"][clave].format(titulo=ses["titulo"]), "fuente": T["sesion"]["fuente"], "fecha": desde}

    def fases_html(dv):
        actual = fase_actual(dv["fases"])
        return "".join(
            f'<li class="chip f-{f["estado"]}{" actual" if f["clave"] == actual else ""}" title="{e(f["estado_txt"])}{" · " + legible(f["fecha"]) if f["fecha"] else ""}">'
            f'<b>{i}</b><span>{e(f["nombre"])}</span></li>'
            for i, f in enumerate(dv["fases"], 1))

    def etapas_html(dv):
        return "".join(
            f'<li class="chip f-{x["estado"]}" title="{e(x["desc"])} — {e(x["estado_txt"])}">'
            f'<b>E{x["num"]}</b><span>{e(corto(x["desc"], 28))}</span><small>{e(corto(x["estado_txt"], 26))}</small></li>'
            for x in dv["etapas"])

    esperando = [s for s in sesiones if s["espera"]]
    vistas = []

    def prioridad(s):
        trab = sum(1 for lista in s["encargos"].values() for x in lista if x["estado"] == "trabajando")
        return (0 if s["espera"] else 1, 0 if trab else 1, -s["ultima"].timestamp())
    for s in sorted(sesiones, key=prioridad):
        vistas.append((s["id"][:8], s, agentes_de_sesion(s),
                       T["sesion"]["aviso"].format(n=s["n_subagentes"], hora=s["ultima"].strftime("%H:%M")),
                       s.get("datos") or datos_vacios(), s.get("ficha"), [s] if s["espera"] else []))
    vistas.append(("bitacora", None, datos["agentes"], "", datos, modulo, esperando))
    inicio = vistas[0][0]   # se abre en la sesión más importante; sin sesiones, en el tablero

    opciones, cuerpos = [], []
    for ident, etiqueta_v, agentes, aviso, dv, ficha_v, esp in vistas:
        trab = sum(1 for a in agentes if a["estado"] == "trabajando")
        if etiqueta_v is None:
            if sesiones:
                opciones.append('<hr class="menu-sep">')
            opciones.append(
                f'<button type="button" class="pestana" role="option" data-ver="{ident}" data-nombre="{T["menu"]["tablero"]}">'
                f'<span class="pest-ico">▤</span><span class="pest-txt"><b>{T["menu"]["tablero"]}</b>'
                f'<small>{T["menu"]["tablero_detalle"]}</small></span>'
                + (f'<span class="pest-chip ok">{T["menu"]["trabajando"].format(n=trab)}</span>' if trab else "") + '</button>')
        else:
            ses = etiqueta_v
            if not opciones:
                opciones.append(f'<p class="menu-sec">{T["menu"]["sesiones_activas"].format(n=len(sesiones))}</p>')
            if esp:
                chip, ico = f'<span class="pest-chip espera">{T["menu"]["espera_tu_respuesta"]}</span>', '<span class="pest-ico espera">!</span>'
            elif trab:
                chip, ico = f'<span class="pest-chip ok">{T["menu"]["trabajando"].format(n=trab)}</span>', '<span class="pest-ico vivo">●</span>'
            else:
                chip, ico = f'<span class="pest-chip">{T["menu"]["sin_actividad"]}</span>', '<span class="pest-ico">●</span>'
            opciones.append(
                f'<button type="button" class="pestana" role="option" data-ver="{ident}" data-nombre="{e(ses["titulo"])}" '
                f'title="{e(T["menu"]["subagentes_tooltip"].format(titulo=ses["titulo"], n=ses["n_subagentes"]))}">'
                f'{ico}<span class="pest-txt"><b>{e(corto(ses["titulo"], 34))}</b>'
                f'<small>{ses["ultima"].strftime("%H:%M")}{" · " + e(ses["ficha"]) if ses["ficha"] else T["menu"]["sin_ficha"]}</small></span>'
                f'{chip}</button>')
        n_esp = len(esp)
        mesa = items([espera_item(x) for x in esp], "decision")
        if not mesa:
            mesa = f'<li class="vacio">{T["panel"]["nadie_espera"]}</li>'
        notas = items(dv["decisiones"], "nota") + items(dv["bloqueos"], "nota bloqueo")
        notas_html = (f'<h3 class="sub-decis">{T["panel"]["anotado"]}</h3><ul class="decis notas">{notas}</ul>' if notas else "")
        fases_v, etapas_v = fases_html(dv), etapas_html(dv)
        ficha_txt = (f'<p class="ficha-de">{T["panel"]["ficha"]} <code>{e(ficha_v)}</code></p>' if ficha_v
                     else f'<p class="ficha-de">{T["panel"]["sin_ficha"]}</p>')
        filas = "".join(fila_agente(a, carpeta_avatares, ident) for a in agentes)
        EST = {"listo": "trabajando", "trabajando": "pausa", "pausa": "pausa", "bloqueado": "bloqueado", "pendiente": "libre"}
        plan_items = "".join(
            f'<li class="{"actual" if x["estado"] == "trabajando" else ""}"><b>E{x["num"]}</b><span>{e(x["desc"])}</span>'
            f'<span class="pildora p-{EST.get(x["estado"], "libre")}"><span class="punto"></span>{e(x["estado_txt"])}</span></li>'
            for x in dv["etapas"]) or f'<li class="vacio">{T["sala"]["sin_plan"]}</li>'
        plan_dlg = (f'<dialog id="dlg-plan-{ident}" class="historial"><header><h4>{T["plan"]["titulo"]}</h4>'
                    f'<button type="button" class="cerrar" aria-label="{T["agente"]["cerrar"]}">×</button></header>'
                    + (f'<p class="ficha-de" style="margin:10px 16px 0">{T["panel"]["ficha"]} <code>{e(ficha_v)}</code></p>' if ficha_v else "")
                    + f'<ol class="plan-lista">{plan_items}</ol></dialog>')
        dialogos = "".join(dialogo_agente(a, ident, carpeta_avatares) for a in agentes)
        cuerpos.append(
            f'<div class="vista" data-vista="{ident}"{"" if ident == inicio else " hidden"}>'
            f'<section class="escena" aria-label="{T["sala"]["aria"]}">{trabajando_ahora(agentes, carpeta_avatares, ident)}'
            f'{sala_svg(agentes, dv, ident, n_esp)}</section>'
            f'<aside class="panel">'
            f'<div class="tarjeta"><div class="aviso-vista" title="{e(re.sub("<[^>]+>", "", aviso))}">{contador(agentes)}</div>'
            f'<h2>{T["panel"]["agentes"]}</h2><ul class="filas">{filas}</ul></div>'
            f'<div class="tarjeta"><h2>{T["panel"]["fases"]}</h2>{ficha_txt}<ol class="chips chips-fases">{fases_v}</ol>'
            + (f'<h2>{T["panel"]["etapas"]}</h2><ol class="chips">{etapas_v}</ol>' if etapas_v else "")
            + f'</div><div class="tarjeta"><h2>{T["panel"]["esperando"]} <span class="globo{"" if n_esp else " cero"}">{n_esp}</span></h2>'
            f'<ul class="decis">{mesa}</ul>{notas_html}</div></aside>'
            f'{dialogos}{plan_dlg}<dialog id="dlg-mesa-{ident}" class="historial"><header><h4>{T["panel"]["esperando"]}</h4>'
            f'<button type="button" class="cerrar" aria-label="{T["agente"]["cerrar"]}">×</button></header><ul class="decis">{mesa}</ul>{notas_html}</dialog>'
            '</div>')
    if not sesiones:
        opciones.insert(0, f'<p class="menu-sec">{T["menu"]["sin_sesiones"]}</p>')
    n_esperan = len(esperando)
    menu = (f'<div class="vistas-menu" data-inicio="{inicio}">'
            '<button type="button" id="vistas-btn" class="vistas-btn" aria-haspopup="listbox" aria-expanded="false" aria-controls="vistas">'
            f'<span class="vb-txt"><small>{T["menu"]["viendo"]}</small><b id="vistas-actual">—</b></span>'
            + (f'<span class="vb-alerta" title="{T["menu"]["esperan_tooltip"]}">{n_esperan}</span>' if n_esperan else "")
            + f'<span class="vb-total">{(T["menu"]["una_sesion"] if len(sesiones) == 1 else T["menu"]["varias_sesiones"]).format(n=len(sesiones))}</span><span class="vb-flecha" aria-hidden="true"></span></button>'
            f'<div class="vistas-pop" id="vistas" role="listbox" aria-label="{T["menu"]["elegir"]}" hidden>{"".join(opciones)}</div></div>')
    return f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{T["titulo_pagina"]}</title>
<style>{CSS.replace("/*TEXTURAS*/", css_texturas())}
{css_avatares(carpeta_avatares)}</style>
</head>
<body data-version="{version}" data-cada="{cada}" data-codigo="{CODIGO}">
{defs_svg(carpeta_avatares)}
{petalos()}
<header class="barra">
  <h1><span class="flor-titulo" aria-hidden="true"></span>{T["titulo_pagina"]}</h1>
  {limites_html(AQUI / "limites.json")}
  <div class="barra-der">
    {menu}
    <button type="button" id="tema" class="tema" aria-label="{T["cambiar_tema"]}">{SOL_LUNA}</button>
  </div>
</header>
<main class="planta">{''.join(cuerpos)}</main>
<script>var TXT={json.dumps(T["tiempo"], ensure_ascii=False)};</script>
<script>{JS}</script>
</body>
</html>"""


CSS = r"""
:root{
  --cielo1:#fde4ef; --cielo2:#fff7fb; --texto:#5b3a4b; --suave:#9a7487; --titulo:#c2517f;
  --panel:#fffafc; --panel2:#fdeef4; --borde:#f2bfd3; --borde2:#e8a6c1; --sombra:rgba(214,120,160,.18);
  --avatar-bg:#fde1ec; --pantalla:#4a3346;
  --ok-bg:#d9f4e6; --ok-tx:#2e6b4d; --ok-bd:#a8dcc1;
  --pa-bg:#fff1d3; --pa-tx:#8a5a08; --pa-bd:#f3d28c;
  --bl-bg:#ffe0e6; --bl-tx:#a32c47; --bl-bd:#f5a9b9;
  --li-bg:#f1eaf6; --li-tx:#6b5f78; --li-bd:#d9cce5;
  --az:#8fb8ef; --perilla-x:0px;
  /* sala */
  --piso-a:#fbe3ea; --piso-b:#f7d6e0; --muro-l:#f6cfdc; --muro-r:#fbe0e9; --muro-tope:#e9a9c0; --zocalo:#e9a9c0; --zocalo2:#efb9cc;
  --al-planificacion:#cdeedd; --al-centro:#f9c9db; --al-construccion:#fbe1d4; --al-control:#e3d9f4; --al-borde:#ffffff;
  --mesa-top:#f6cbd8; --mesa-x:#e7a9bd; --mesa-y:#d895ab;
  --silla-top:#d9c6f2; --silla-x:#c4ace9; --silla-y:#b197df;
  --mon-top:#fff4f8; --mon-x:#f2d9e3; --mon-y:#e6c3d1;
  --reu-top:#e9d8f7; --reu-borde:#c9a9ec; --reu-lado:#b896e0;
  --act-bg:#fff3d6; --act-bd:#f0a43c; --act-tx:#a85f08;
  --hoja:#fffaf0; --cartel:#fffafc; --ventana-cielo:#cbe8ff; --ventana-flor:#f7a8c4;
  --chevron:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 8'%3E%3Cpath d='M1 1.5l5 5 5-5' fill='none' stroke='%23c2517f' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
  color-scheme:light;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){/*NOCHE*/}}
:root[data-theme="dark"]{/*NOCHE*/}
/*TEXTURAS*/
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{color:var(--texto);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;overflow-x:hidden;
  background:linear-gradient(var(--cielo1),var(--cielo2));display:flex;flex-direction:column;min-height:100vh}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.92em}
h1,h2,h4,.rol{font-family:ui-monospace,"DejaVu Sans Mono",monospace;font-weight:800}
.defs{position:absolute}
.petalos{position:fixed;inset:0;pointer-events:none;overflow:hidden;z-index:0}
.petalos i{position:absolute;top:-24px;width:14px;height:14px;background:var(--px-petalo) center/contain no-repeat;image-rendering:pixelated;opacity:.8;animation:cae linear infinite}
@keyframes cae{0%{transform:translate(0,0) rotate(0)}50%{transform:translate(40px,55vh) rotate(140deg)}100%{transform:translate(-10px,110vh) rotate(300deg)}}
.barra,.planta,.pie-pagina{position:relative;z-index:1}
.barra{z-index:10}
/* barra */
.barra{display:flex;flex-wrap:wrap;align-items:center;gap:8px 18px;padding:10px 20px}
.barra h1{margin:0;font-size:1.3rem;color:var(--titulo);display:flex;align-items:center;gap:8px}
.flor-titulo{width:20px;height:20px;background:var(--px-flor) center/contain no-repeat;image-rendering:pixelated;animation:gira 8s linear infinite}
@keyframes gira{to{transform:rotate(360deg)}}
.modulo{margin:0;color:var(--suave);font-size:.82rem}
.barra-der{margin-left:auto;display:flex;gap:10px;align-items:center}
.selector{display:inline-flex;align-items:center;gap:8px;font-size:.82rem;font-weight:600;color:var(--suave)}
.selector select{appearance:none;-webkit-appearance:none;font:600 .86rem system-ui,sans-serif;color:var(--texto);cursor:pointer;
  background:var(--panel) var(--chevron) no-repeat right 12px center/11px 7px;padding:7px 34px 7px 14px;border:2px solid var(--borde);border-radius:999px;
  box-shadow:0 3px 10px var(--sombra);max-width:min(380px,calc(100vw - 150px));text-overflow:ellipsis}
.selector select:hover{border-color:var(--borde2)}
.selector select:focus-visible,.tema:focus-visible,.cerrar:focus-visible,.fila:focus-visible{outline:3px solid var(--titulo);outline-offset:2px}
.limites{display:flex;gap:8px;align-items:center;margin-left:12px}
.limites.viejo{opacity:.55}
.limites{cursor:pointer}
.limites.consultando{animation:espera 1s ease-in-out infinite}
.limite{display:flex;align-items:center;gap:7px;padding:5px 12px;background:var(--panel);border:2px solid var(--borde);border-radius:14px;box-shadow:0 3px 10px var(--sombra)}
.lim-nombre{font:800 .66rem ui-monospace,monospace;letter-spacing:.08em;text-transform:uppercase;color:var(--suave)}
.lim-barra{width:82px;height:9px;border-radius:999px;background:var(--panel2);overflow:hidden;box-shadow:inset 0 0 0 1px var(--borde)}
.lim-barra i{display:block;height:100%;border-radius:999px;background:#7fd3a6}
.l-ambar .lim-barra i{background:#f3c053} .l-rojo .lim-barra i{background:#f06d93}
.lim-pct{font-size:.8rem;font-weight:800;min-width:34px}
.l-rojo .lim-pct{color:#d2406c}
.lim-hora{font-size:.68rem;color:var(--suave);white-space:nowrap;padding:0 4px}
.lim-reinicia{font-size:.7rem;color:var(--suave);white-space:nowrap}
.vistas-menu{position:relative;z-index:30}
.menu-sep{border:none;border-top:1.5px dashed var(--borde);margin:6px 10px}
.vistas-btn{all:unset;box-sizing:border-box;cursor:pointer;display:flex;align-items:center;gap:10px;min-width:260px;max-width:min(420px,calc(100vw - 120px));
  padding:6px 14px 6px 16px;background:var(--panel);border:2px solid var(--borde);border-radius:16px;box-shadow:0 3px 10px var(--sombra);transition:border-color .15s}
.vistas-btn:hover,.vistas-btn[aria-expanded="true"]{border-color:var(--borde2)}
.vistas-btn:focus-visible{outline:3px solid var(--titulo);outline-offset:2px}
.vb-txt{display:flex;flex-direction:column;min-width:0;flex:1;line-height:1.15}
.vb-txt small{font-size:.62rem;letter-spacing:.08em;text-transform:uppercase;color:var(--suave);font-weight:700}
.vb-txt b{font-size:.88rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vb-alerta{min-width:22px;height:22px;padding:0 6px;border-radius:999px;display:grid;place-items:center;background:#f06d93;color:#fff;font-size:.72rem;font-weight:900;animation:late 1s ease-in-out infinite}
.vb-total{font-size:.7rem;color:var(--suave);white-space:nowrap}
.vb-flecha{width:11px;height:7px;background:var(--chevron) center/contain no-repeat;transition:transform .2s}
.vistas-btn[aria-expanded="true"] .vb-flecha{transform:rotate(180deg)}
.vistas-pop{position:absolute;right:0;top:calc(100% + 8px);z-index:20;width:min(400px,calc(100vw - 24px));max-height:min(62vh,520px);overflow:auto;
  background:var(--panel);border:2px solid var(--borde);border-radius:18px;box-shadow:0 18px 40px rgba(120,40,80,.25);padding:6px}
.menu-sec{margin:8px 10px 4px;font-size:.64rem;letter-spacing:.09em;text-transform:uppercase;color:var(--suave);font-weight:800}
.pestana{all:unset;box-sizing:border-box;width:100%;cursor:pointer;display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:12px;color:var(--texto)}
.pestana:hover,.pestana:focus-visible{background:var(--panel2);outline:none}
.pestana.activa{background:linear-gradient(135deg,#ffe1ec,#fbd0e0);color:#5b2240}
.pest-ico{flex:none;display:grid;place-items:center;width:26px;height:26px;border-radius:9px;background:var(--panel2);font-size:.72rem;color:var(--li-tx)}
.pest-ico.vivo{color:#4fbf86;animation:espera 1.4s ease-in-out infinite}
.pest-ico.espera{background:#f06d93;color:#fff;font-weight:900;font-size:.85rem;animation:late 1s ease-in-out infinite}
.pestana.activa .pest-ico{background:rgba(255,255,255,.7)}
.pestana.activa .pest-ico.espera{background:#f06d93;color:#fff}
.pest-txt{display:flex;flex-direction:column;min-width:0;flex:1;line-height:1.2}
.pest-txt b{font-size:.84rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pest-txt small{font-size:.68rem;color:var(--suave);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pestana.activa .pest-txt small{color:#8a5a70}
.pest-chip{flex:none;font-size:.66rem;font-weight:800;padding:2px 8px;border-radius:999px;background:var(--li-bg);color:var(--li-tx)}
.pest-chip.ok{background:var(--ok-bg);color:var(--ok-tx)}
.pest-chip.espera{background:#ffe0e8;color:#b0284f}
.pest-alerta{width:18px;height:18px;border-radius:50%;display:grid;place-items:center;background:#f06d93;color:#fff;font-size:.7rem;font-weight:900;
  animation:late 1s ease-in-out infinite}
.globo.cero{background:var(--li-bd)}
.ficha-de{margin:-2px 0 6px;font-size:.7rem;color:var(--suave)}
.sub-decis{margin:10px 0 5px;font-size:.66rem;letter-spacing:.08em;text-transform:uppercase;color:var(--suave)}
.decis.notas li{border-left-color:var(--li-bd);opacity:.85}
.pest-vacia{align-self:center;padding:0 10px;font-size:.72rem;color:var(--suave)}
.tema{position:relative;width:62px;height:34px;border:2px solid var(--borde);border-radius:999px;background:var(--panel);cursor:pointer;
  box-shadow:0 3px 10px var(--sombra);display:flex;align-items:center;justify-content:space-between;padding:0 8px;flex:none}
.tema svg{width:15px;height:15px;position:relative;z-index:1}
.tema .perilla{position:absolute;top:2px;left:3px;width:26px;height:26px;border-radius:50%;background:linear-gradient(135deg,#ffd1e2,#f7a8c8);
  box-shadow:0 2px 6px var(--sombra);transform:translateX(var(--perilla-x));transition:transform .25s ease}
/* una sola pantalla: sala + panel */
.planta{flex:1;min-height:0;padding:0 16px 8px;overflow:hidden}
.vista{height:calc(100vh - 62px);display:grid;grid-template-columns:minmax(0,1fr) 370px;gap:14px}
.escena{position:relative;min-width:0;min-height:0;border-radius:24px;
  background:radial-gradient(ellipse at 50% 60%,rgba(255,255,255,.55),transparent 70%)}
.sala{position:absolute;inset:0;width:100%;height:100%;display:block;overflow:visible}
.panel{min-height:0;max-height:calc(100vh - 66px);overflow:auto;display:flex;flex-direction:column;gap:8px;padding-right:2px}
.tarjeta{background:var(--panel);border:2px solid var(--borde);border-radius:18px;padding:10px 12px;box-shadow:0 4px 0 var(--sombra)}
.tarjeta h2{margin:6px 0 5px;font-size:.74rem;letter-spacing:.08em;text-transform:uppercase;color:var(--titulo);display:flex;align-items:center;gap:6px}
.tarjeta h2::before{content:"";width:12px;height:12px;background:var(--px-flor) center/contain no-repeat;image-rendering:pixelated}
.aviso-vista p{margin:6px 0 0;font-size:.74rem;color:var(--suave)}
.contador{display:flex;flex-wrap:wrap;gap:3px;list-style:none;margin:0;padding:0}
.contador li{display:inline-flex;align-items:center;gap:4px;padding:1px 7px;font-size:.68rem;font-weight:700;border-radius:999px;border:1.5px solid}
.filas{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:1px}
.fila{all:unset;box-sizing:border-box;width:100%;cursor:pointer;display:flex;gap:9px;align-items:center;padding:3px 6px;border-radius:12px;transition:background .15s}
.fila:hover{background:var(--panel2)}
.mini-av{flex:none;width:30px;height:30px;border-radius:10px;overflow:hidden;background:var(--avatar-bg) center 20%/cover no-repeat;box-shadow:0 0 0 2px var(--borde)}
.mini-av svg{display:block;width:100%;height:100%;image-rendering:pixelated}
.est-trabajando .mini-av{box-shadow:0 0 0 2px var(--ok-bd)} .est-pausa .mini-av{box-shadow:0 0 0 2px var(--pa-bd)} .est-bloqueado .mini-av{box-shadow:0 0 0 2px var(--bl-bd)}
.fila-txt{min-width:0;flex:1;display:flex;flex-direction:column}
.fila-cab{display:flex;align-items:center;gap:6px;min-width:0}
.fila-cab b{font-size:.84rem}
.rol{font-size:.62rem;color:var(--suave);text-transform:uppercase;letter-spacing:.08em}
.fila-cab .pildora{margin-left:auto}
.fila-tarea{font-size:.78rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tiempo{font-size:.7rem;color:var(--suave)}
.pildora{display:inline-flex;align-items:center;gap:5px;padding:1px 8px;font-size:.68rem;font-weight:700;border-radius:999px;border:1.5px solid;white-space:nowrap}
.punto{width:7px;height:7px;border-radius:50%;background:currentColor;flex:none}
.p-trabajando{background:var(--ok-bg);color:var(--ok-tx);border-color:var(--ok-bd)}
.p-pausa{background:var(--pa-bg);color:var(--pa-tx);border-color:var(--pa-bd)}
.p-bloqueado{background:var(--bl-bg);color:var(--bl-tx);border-color:var(--bl-bd)}
.p-libre{background:var(--li-bg);color:var(--li-tx);border-color:var(--li-bd)}
.chips{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:5px}
.chip{position:relative;background:var(--panel2);border-radius:10px;padding:7px 6px 4px;display:flex;flex-direction:column;min-width:0;overflow:hidden}
.chip::before{content:"";position:absolute;top:0;left:0;right:0;height:4px;background:var(--li-bd)}
.chip b{font-size:.72rem;font-family:ui-monospace,monospace}
.chip span{font-size:.7rem;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chip small{font-size:.62rem;color:var(--suave);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.f-listo::before{background:var(--ok-bd)} .f-trabajando::before{background:var(--az)} .f-pausa::before{background:var(--pa-bd)} .f-bloqueado::before{background:var(--bl-bd)}
.chip.f-pendiente{opacity:.7}
.chip.actual{background:var(--act-bg);box-shadow:inset 0 0 0 2px var(--act-bd)}.chip.actual::before{background:var(--act-bd)}
.chips-fases .chip{flex-direction:row;gap:5px;align-items:baseline;padding:6px 6px 3px}
.decis{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:6px}
.decis li{background:var(--panel2);border-left:5px solid var(--pa-bd);border-radius:8px;padding:6px 9px}
.decis li.bloqueo{border-left-color:var(--bl-bd)} .decis li.vacio{border-left-color:var(--ok-bd);color:var(--suave);font-size:.8rem}
.decis p{margin:0 0 2px;font-size:.8rem;overflow-wrap:break-word} .decis small{font-size:.68rem;color:var(--suave)}
.globo{display:inline-grid;place-items:center;min-width:18px;height:18px;padding:0 5px;border-radius:999px;background:var(--titulo);color:#fff;font-size:.66rem;letter-spacing:0}
.tokens{font-size:.74rem;color:var(--titulo);font-weight:700}
.tokens.alto{color:var(--bl-tx);background:var(--bl-bg);padding:1px 8px;border-radius:999px}
[hidden]{display:none!important}
/* sala isométrica */
.sala text{font-family:system-ui,sans-serif}
.piso-a{fill:var(--piso-a)} .piso-b{fill:var(--piso-b)}
.muro-l{fill:var(--muro-l)} .muro-r{fill:var(--muro-r)} .muro-tope{fill:var(--muro-tope)}
.zocalo-l{fill:var(--zocalo)} .zocalo-r{fill:var(--zocalo2)}
.alfombra{stroke:var(--al-borde);stroke-width:2.5;stroke-dasharray:6 4;opacity:.95}
.al-planificacion{fill:var(--al-planificacion)} .al-centro{fill:var(--al-centro)} .al-construccion{fill:var(--al-construccion)} .al-control{fill:var(--al-control)}
.mesa.cara-top{fill:var(--mesa-top)} .mesa.cara-x{fill:var(--mesa-x)} .mesa.cara-y{fill:var(--mesa-y)}
.silla.cara-top{fill:var(--silla-top)} .silla.cara-x{fill:var(--silla-x)} .silla.cara-y{fill:var(--silla-y)}
.monitor.cara-top{fill:var(--mon-top)} .monitor.cara-x{fill:var(--mon-x)} .monitor.cara-y{fill:var(--mon-y)}
.sala polygon{stroke-linejoin:round}
.hoja{fill:var(--hoja);stroke:var(--borde);stroke-width:.8}
.cab-borde{fill:none;stroke:#fff;stroke-width:3}
.pantalla-iso{fill:#3b2a3a}
.est-trabajando .pantalla-iso{fill:#86e3b4;animation:brilla 1.4s steps(3) infinite}
.est-pausa .pantalla-iso{fill:#ffd98a;animation:espera 3s ease-in-out infinite}
.est-bloqueado .pantalla-iso{fill:#ff9fb4;animation:alerta 1.2s ease-in-out infinite}
@keyframes brilla{0%,100%{fill:#86e3b4}50%{fill:#b8f5d6}}
@keyframes espera{0%,100%{opacity:1}50%{opacity:.5}}
@keyframes alerta{0%,100%{fill:#ff9fb4}50%{fill:#e8607e}}
@media (prefers-reduced-motion:reduce){.pantalla-iso,.flor-titulo,.petalos i,.punto,.burbuja-mueve,.burbuja tspan,.rebota,.aviso-alerta,.cartel-muro.actual,.fila.cambio,.agente.llega *{animation:none!important}.petalos{display:none}}
.agente,.mesa-reu{cursor:pointer;outline:none}
.agente:hover .tag rect,.agente:focus-visible .tag rect,.mesa-reu:hover .tag rect,.mesa-reu:focus-visible .tag rect{stroke:var(--titulo);stroke-width:2.5}
.agente:hover{filter:drop-shadow(0 4px 6px var(--sombra))}
.burbuja rect,.burbuja-pico{fill:#fff;stroke:#86d6ad;stroke-width:2}
.burbuja-pico{stroke-linejoin:round}
.burbuja text{font-size:11px;font-weight:800;fill:#2e6b4d}
.burbuja-mueve{animation:flota 2.4s ease-in-out infinite}
@keyframes flota{0%,100%{transform:translateY(0)}50%{transform:translateY(-3px)}}
.burbuja .pt1,.burbuja .pt2,.burbuja .pt3{animation:punto 1.2s infinite}
.burbuja .pt2{animation-delay:.2s} .burbuja .pt3{animation-delay:.4s}
@keyframes punto{0%,20%{fill-opacity:0}50%,100%{fill-opacity:1}}
.mesa-reu ellipse.mesa.cara-top,.mesa-reu polygon.mesa.cara-top{fill:var(--reu-top);stroke:var(--reu-borde);stroke-width:2}
.mesa-reu .mesa.cara-x,.mesa-reu .mesa.cara-y{fill:var(--reu-lado)}
.banco.cara-top{fill:var(--silla-top)} .banco.cara-x{fill:var(--silla-y)}
.mesa.cara-x{fill:var(--mesa-x)}
ellipse.mesa.cara-top{fill:var(--mesa-top);stroke:var(--mesa-x);stroke-width:1.5}
.sombra-mesa{fill:rgba(150,60,100,.14)}
.plan-muro{cursor:pointer;outline:none}
.plan-muro:hover .piz-marco,.plan-muro:focus-visible .piz-marco{stroke:var(--titulo);stroke-width:3}
.plan-lista{list-style:none;margin:0;padding:8px 16px 16px}
.plan-lista li,dialog.historial .plan-lista li{display:grid;grid-template-columns:42px 1fr;gap:4px 10px;padding:10px 0;border-bottom:1.5px dashed var(--borde)}
.plan-lista li:last-child{border-bottom:none}
.plan-lista b{font-family:ui-monospace,monospace;color:var(--titulo)}
.plan-lista .pildora{justify-self:start;grid-column:2}
.plan-lista li.actual{background:var(--act-bg);border-radius:12px;padding:10px 8px;border-bottom-color:transparent}
.aviso-mesa rect,.aviso-pico{fill:#fff;stroke:#f3a6be;stroke-width:2}
.aviso-txt{font-size:11px;font-weight:800;fill:#a32c47}
.aviso-alerta{fill:#f06d93;animation:late 1s ease-in-out infinite;transform-box:fill-box;transform-origin:center}
.aviso-signo{font-size:12px;font-weight:900;fill:#fff}
.rebota{animation:rebote 1.1s cubic-bezier(.3,0,.3,1) infinite}
@keyframes rebote{0%,100%{transform:translateY(0)}30%{transform:translateY(-10px)}50%{transform:translateY(0)}65%{transform:translateY(-4px)}80%{transform:translateY(0)}}
@keyframes late{0%,100%{transform:scale(1)}50%{transform:scale(1.25)}}
.mesa-reu:hover .aviso-mesa rect,.mesa-reu:focus-visible .aviso-mesa rect{stroke:var(--titulo)}
.placa rect{fill:var(--panel);stroke:var(--borde2);stroke-width:1.2}
.placa-nombre{font-size:9.5px;font-weight:800;fill:var(--texto)}
.placa-rol{font-size:5.6px;font-weight:700;fill:var(--titulo);letter-spacing:.06em}
.ahora{position:absolute;top:4px;left:4px;z-index:3;display:flex;flex-wrap:wrap;align-items:center;gap:6px;max-width:72%}
.ahora-tit{display:inline-flex;align-items:center;gap:6px;font:800 .7rem ui-monospace,monospace;letter-spacing:.08em;text-transform:uppercase;color:var(--ok-tx);
  background:var(--ok-bg);border:1.5px solid var(--ok-bd);border-radius:999px;padding:4px 10px}
.ahora-dot{width:8px;height:8px;border-radius:50%;background:#4fbf86;animation:espera 1.4s ease-in-out infinite}
.ahora-nadie{font-size:.78rem;color:var(--suave)}
.ahora-chip{all:unset;cursor:pointer;display:flex;align-items:center;gap:7px;background:var(--panel);border:1.5px solid var(--ok-bd);border-radius:14px;padding:3px 10px 3px 3px;
  box-shadow:0 3px 8px var(--sombra);transition:transform .15s}
.ahora-chip:hover{transform:translateY(-2px)}
.ahora-chip:focus-visible{outline:3px solid var(--titulo);outline-offset:2px}
.ahora-chip .mini-av{width:28px;height:28px;box-shadow:0 0 0 2px var(--ok-bd)}
.ahora-chip b{font-size:.8rem} .ahora-chip small{display:block;font-size:.7rem;color:var(--suave);white-space:nowrap}
.agente.caminando .burbuja,.agente.caminando .nombre-mesa{opacity:0}
.agente.llega .burbuja{animation:aparece .6s cubic-bezier(.3,1.6,.5,1)}
.agente.llega .nombre-mesa{animation:aparece .5s ease-out}
.agente.llega .pantalla-iso{animation:destello .9s ease-out}
@keyframes aparece{0%{opacity:0;transform:translateY(8px)}100%{opacity:1;transform:none}}
@keyframes destello{0%{fill:#ffffff}35%{fill:#fff6b0}100%{}}
.figura.en-camino{pointer-events:none}
.fila.cambio{animation:parpadeo .8s ease-in-out 3}
@keyframes parpadeo{0%,100%{background:transparent}50%{background:var(--act-bg);box-shadow:inset 0 0 0 2px var(--act-bd)}}
.tag rect{fill:var(--panel);stroke:var(--borde2);stroke-width:1.5;filter:drop-shadow(0 2px 2px var(--sombra))}
.tag-pico{fill:var(--panel);stroke:var(--borde2);stroke-width:1.5}
.tag-nombre{font-size:11px;font-weight:800;fill:var(--texto)}
.tag-linea{font-size:9.5px;fill:var(--suave)}
.dot-trabajando{fill:#4fbf86} .dot-pausa{fill:#f0b23c} .dot-bloqueado{fill:#e8607e} .dot-libre{fill:#b8a9c8}
.rotulo rect{fill:var(--panel);stroke:var(--borde);stroke-width:1.5}
.rotulo text{font-size:9.5px;font-weight:800;fill:var(--titulo);letter-spacing:.06em;text-transform:uppercase;font-family:ui-monospace,monospace}
.cartel-fondo{fill:var(--cartel);stroke:var(--borde2);stroke-width:1.5}
.cartel-sombra{fill:rgba(150,60,100,.12)}
.cartel-circulo{fill:#fff;opacity:.9}
.cartel-num{font-size:12px!important;font-weight:900;fill:var(--titulo)}
.cartel-estado{font-size:9px;font-weight:800;fill:#fff;letter-spacing:.04em}
.cartel-nombre{font-size:12px;font-weight:800;fill:var(--texto)}
.cartel-fecha{font-size:9px;fill:var(--suave)}
.cartel-muro.actual .cartel-fondo{fill:var(--act-bg);stroke:var(--act-bd);stroke-width:3}
.cartel-muro.actual .cartel-banda{fill:var(--act-bd)!important}
.cartel-muro.actual .cartel-num{fill:var(--act-tx)}
.cartel-muro.actual{animation:brillo 2s ease-in-out infinite}
@keyframes brillo{0%,100%{filter:drop-shadow(0 0 0 rgba(240,164,60,0))}50%{filter:drop-shadow(0 0 7px rgba(240,164,60,.9))}}
.cartel-ahora rect{fill:var(--act-bd);stroke:#fff;stroke-width:1.5} .cartel-ahora text{font-size:8.5px;font-weight:900;fill:#fff;letter-spacing:.1em}
.piz-marco{fill:var(--mesa-x)} .piz-fondo{fill:var(--panel)} .piz-pata{fill:var(--mesa-y)}
.piz-tit{font-size:10px;font-weight:900;fill:var(--titulo);letter-spacing:.12em;font-family:ui-monospace,monospace}
.piz-num{font-size:9.5px;font-weight:900;fill:var(--texto);font-family:ui-monospace,monospace}
.piz-txt{font-size:9.5px;fill:var(--texto)}
.piz-resalte{fill:transparent}
.piz-dot{fill:var(--li-bd)}
.piz-fila.f-listo .piz-dot{fill:#4fbf86} .piz-fila.f-pendiente{opacity:.6}
.piz-fila.actual .piz-resalte{fill:var(--act-bg);stroke:var(--act-bd);stroke-width:1.5} .piz-fila.actual .piz-dot{fill:var(--act-bd)}
.piz-fila.actual .piz-txt,.piz-fila.actual .piz-num{fill:var(--act-tx);font-weight:800}
.nombre-mesa{cursor:pointer}
.nombre-mesa.oculto{opacity:0}
.nombre-mesa rect{fill:var(--panel);stroke:var(--borde2);stroke-width:1.2}
.nombre-mesa text{font-size:8.5px;font-weight:800;fill:var(--texto)}
.marca-bloq circle{fill:#e8607e;stroke:#fff;stroke-width:1.5} .marca-bloq text{font-size:11px;font-weight:900;fill:#fff}
.marca-zz{font-size:12px;font-weight:900;fill:#b49ad8;font-family:ui-monospace,monospace}
.escritorio-vacio .placa{opacity:.75}
.cartel-banda{fill:var(--li-bd)}
.cartel-muro.f-listo .cartel-banda{fill:var(--ok-bd)} .cartel-muro.f-trabajando .cartel-banda{fill:var(--az)}
.cartel-muro.f-pausa .cartel-banda{fill:var(--pa-bd)} .cartel-muro.f-bloqueado .cartel-banda{fill:var(--bl-bd)}
.cartel-muro.f-pendiente{opacity:.7}
.cartel-num{font-size:17px;font-weight:800;fill:var(--titulo);font-family:ui-monospace,monospace}
.cartel-txt{font-size:8.5px;font-weight:700;fill:var(--suave)}
.ventana-marco{fill:#fff} .ventana-cielo{fill:var(--ventana-cielo)} .ventana-flor{fill:var(--ventana-flor)}
/* ficha del agente */
dialog.historial{width:min(540px,calc(100vw - 32px));max-height:82vh;padding:0;background:var(--panel);color:var(--texto);
  border:2px solid var(--borde);border-radius:22px;box-shadow:0 20px 50px rgba(120,40,80,.3)}
dialog.historial::backdrop{background:rgba(60,20,45,.35);backdrop-filter:blur(2px)}
dialog.historial header{display:flex;gap:10px;align-items:center;padding:12px 16px;position:sticky;top:0;z-index:1;background:var(--panel2);border-bottom:2px solid var(--borde)}
dialog.historial h4{margin:0;font-size:1rem;color:var(--titulo);flex:1;display:flex;align-items:baseline;gap:8px}
.cerrar{width:32px;height:32px;border:none;border-radius:50%;background:var(--panel);color:var(--titulo);font-size:1.25rem;line-height:1;cursor:pointer;
  box-shadow:0 2px 6px var(--sombra);transition:background .2s,transform .2s}
.cerrar:hover{background:var(--bl-bg);transform:rotate(90deg)}
.resumen{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center;padding:12px 16px;border-bottom:1.5px dashed var(--borde)}
.resumen .tarea{flex-basis:100%;margin:0;font-size:.92rem}
.etapa{font-size:.72rem;color:var(--suave);background:var(--panel2);padding:1px 9px;border-radius:999px}
dialog.historial h5{margin:12px 16px 0;font-size:.72rem;letter-spacing:.08em;text-transform:uppercase;color:var(--suave)}
dialog.historial ol{list-style:none;margin:0;padding:4px 16px 16px}
dialog.historial ol li{display:grid;grid-template-columns:112px 1fr;gap:10px;padding:10px 0;border-bottom:1.5px dashed var(--borde);font-size:.85rem}
dialog.historial ol li:last-child{border-bottom:none}
dialog.historial time{color:var(--suave);font-size:.78rem}
dialog.historial .decis{padding:12px 16px 16px}
.mini{color:var(--suave);font-size:.78rem} .fuente{color:var(--suave);font-size:.7rem;margin-top:2px} .vacio{color:var(--suave)}
.pie-pagina{padding:2px 20px 8px;color:var(--suave);font-size:.72rem}
.pie-pagina code{overflow-wrap:anywhere}
/* pantallas medianas y móviles: la sala arriba y el panel abajo */
@media (max-width:1400px){.regenerar{display:none}}
@media (max-width:1050px){
  .vista{grid-template-columns:1fr;height:auto}
  .planta{overflow:visible}
  .sala{position:static;height:auto}
  .ahora{position:static;max-width:none;margin:0 0 6px}
  .panel{max-height:none;overflow:visible}
}
@media (max-width:600px){
  .limites{order:5;flex-basis:100%;margin-left:0;flex-wrap:wrap}
  .vistas-menu{flex:1;min-width:0}
  .vistas-btn{min-width:0;width:100%;max-width:none}
  .vistas-pop{left:0;right:auto;width:calc(100vw - 28px)}
  .barra{padding:10px 14px}.planta{padding:0 10px 8px}
  .modulo{flex-basis:100%;order:3}
  .barra-der{margin-left:0}
  .chips{grid-template-columns:repeat(2,minmax(0,1fr))}
  dialog.historial ol li{grid-template-columns:1fr;gap:2px}
}
"""

NOCHE = """--cielo1:#231a2e; --cielo2:#352743; --texto:#fbeaf3; --suave:#cdb2c3; --titulo:#f7a8c8;
    --panel:#3a2b46; --panel2:#463554; --borde:#6f5181; --borde2:#8a6a9c; --sombra:rgba(0,0,0,.3);
    --avatar-bg:#4d3a5c; --pantalla:#1d1420;
    --ok-bg:#2c5444; --ok-tx:#b9f3d6; --ok-bd:#3f7560; --pa-bg:#57472a; --pa-tx:#ffe2a3; --pa-bd:#7a6338;
    --bl-bg:#5d2d3c; --bl-tx:#ffc4d1; --bl-bd:#874255; --li-bg:#463f52; --li-tx:#dcd2e8; --li-bd:#5f5670;
    --az:#a9c8f5; --perilla-x:28px;
    --piso-a:#4a3650; --piso-b:#433049; --muro-l:#3c2c4a; --muro-r:#46344f; --muro-tope:#6b4660; --zocalo:#6b4660; --zocalo2:#7a5270;
    --al-planificacion:#2f4a42; --al-centro:#5a3550; --al-construccion:#5a4440; --al-control:#3f3758; --al-borde:#8a6a9c;
    --mesa-top:#7d5068; --mesa-x:#6a4259; --mesa-y:#5a374c;
    --silla-top:#5f4d7c; --silla-x:#51416c; --silla-y:#45375e;
    --mon-top:#e8d8e2; --mon-x:#c9b3c2; --mon-y:#b39eae;
    --reu-top:#5d4a7c; --reu-borde:#8a6fb0; --reu-lado:#4a3a66;
    --act-bg:#5a4320; --act-bd:#f0b45a; --act-tx:#ffe2a3;
    --hoja:#efe3d6; --cartel:#4a3858; --ventana-cielo:#1f1d48; --ventana-flor:#c77a9b;
    --chevron:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 8'%3E%3Cpath d='M1 1.5l5 5 5-5' fill='none' stroke='%23f7a8c8' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
    color-scheme:dark;"""
CSS = CSS.replace("/*NOCHE*/", NOCHE)

JS = r"""
(function(){
  var raiz=document.documentElement, KEY='oficina-tema';
  try{var t=localStorage.getItem(KEY); if(t) raiz.setAttribute('data-theme',t);}catch(e){}
  document.getElementById('tema').addEventListener('click',function(){
    var oscuro=raiz.getAttribute('data-theme')?raiz.getAttribute('data-theme')==='dark'
      :matchMedia('(prefers-color-scheme: dark)').matches;
    var nuevo=oscuro?'light':'dark'; raiz.setAttribute('data-theme',nuevo);
    try{localStorage.setItem(KEY,nuevo);}catch(e){}
  });
  function abre(el){ var d=document.getElementById(el.getAttribute('data-dlg')); if(!d) return;
    if(d.showModal) d.showModal(); else d.setAttribute('open',''); }
  function enlaza(){
    document.querySelectorAll('[data-dlg]').forEach(function(el){
      el.addEventListener('click',function(){ abre(el); });
      el.addEventListener('keydown',function(ev){ if(ev.key==='Enter'||ev.key===' '){ ev.preventDefault(); abre(el); } });
    });
    document.querySelectorAll('dialog').forEach(function(d){
      var c=d.querySelector('.cerrar'); if(c) c.addEventListener('click',function(){ d.close(); });
      d.addEventListener('click',function(ev){ if(ev.target===d) d.close(); });
    });
  }
  enlaza();
  var VKEY='oficina-vista', vistaActual='bitacora';
  function muestra(v){
    var hay=false;
    document.querySelectorAll('.vista').forEach(function(x){ var si=x.getAttribute('data-vista')===v; x.hidden=!si; hay=hay||si; });
    if(!hay){ v='bitacora'; document.querySelector('[data-vista=bitacora]').hidden=false; }
    vistaActual=v;
    document.querySelectorAll('.pestana').forEach(function(b){
      var si=b.getAttribute('data-ver')===v; b.classList.toggle('activa',si); b.setAttribute('aria-selected',si?'true':'false');
      if(si){ var t=document.getElementById('vistas-actual'); if(t) t.textContent=b.getAttribute('data-nombre'); } });
  }
  function cierraMenu(){ var pop=document.getElementById('vistas'), b=document.getElementById('vistas-btn');
    if(pop){ pop.hidden=true; } if(b){ b.setAttribute('aria-expanded','false'); } }
  function pestanas(){
    var b=document.getElementById('vistas-btn'), pop=document.getElementById('vistas');
    b.addEventListener('click',function(ev){ ev.stopPropagation(); var abre=pop.hidden; pop.hidden=!abre; b.setAttribute('aria-expanded',abre?'true':'false');
      if(abre){ var act=pop.querySelector('.activa'); if(act) act.focus(); } });
    document.querySelectorAll('.pestana').forEach(function(o){
      o.addEventListener('click',function(){ var v=o.getAttribute('data-ver'); muestra(v); cierraMenu(); b.focus();
        try{localStorage.setItem(VKEY,v);}catch(e){} });
    });
  }
  document.addEventListener('click',function(ev){ if(!ev.target.closest('.vistas-menu')) cierraMenu(); });
  document.addEventListener('keydown',function(ev){ if(ev.key==='Escape') cierraMenu(); });
  pestanas(); muestra(document.querySelector('.vistas-menu').getAttribute('data-inicio')||'bitacora');
  try{ var guardada=localStorage.getItem(VKEY); if(guardada) muestra(guardada); }catch(e){}
  function lapso(min){
    function f(k,n){ return TXT[k].replace('{n}',n); }
    if(min<1) return TXT.menos_de_un_minuto;
    var d=Math.floor(min/1440), h=Math.floor(min%1440/60), m=Math.floor(min%60);
    if(d) return f('dias',d)+(h?' '+f('horas',h):'');
    if(h) return f('horas',h)+(m?' '+f('minutos',m):'');
    return f('minutos',m);
  }
  var PREF={trabajando:TXT.trabajando,pausa:TXT.pausa,bloqueado:TXT.bloqueado,libre:TXT.libre};
  function pinta(){
    var ahora=Date.now();
    document.querySelectorAll('.tiempo[data-desde]').forEach(function(p){
      var s=p.getAttribute('data-desde'); if(!s){p.textContent=p.getAttribute('data-vacio')||TXT.sin_registros;return;}
      var t=new Date(s).getTime(); if(isNaN(t)) return;
      p.textContent=(p.getAttribute('data-prefijo')||PREF[p.getAttribute('data-estado')]||TXT.otro)+lapso((ahora-t)/60000);
      p.title=s.replace('T',' ');
    });
  }
  pinta(); setInterval(pinta,60000);
  document.addEventListener('click',function(ev){
    var l=ev.target.closest('.limites'); if(!l || location.protocol.indexOf('http')!==0) return;
    l.classList.add('consultando'); fetch('/limites',{cache:'no-store'}).catch(function(){});
  });
  // Cuando un agente cambia entre su escritorio y la mesa, camina de un lugar al otro.
  var quieto=matchMedia('(prefers-reduced-motion: reduce)').matches;
  function camina(f, dx, dy){
    var ag=f.closest('.agente'), svg=f.ownerSVGElement, marca=document.createComment('');
    f.parentNode.insertBefore(marca,f); svg.appendChild(f); f.classList.add('en-camino');
    if(ag) ag.classList.add('caminando');
    var nom=svg.querySelector('.nombre-mesa[data-fig="'+f.getAttribute('data-fig')+'"]'); if(nom) nom.classList.add('oculto');
    var pasos=[], n=10;
    for(var i=0;i<=n;i++){ var t=i/n, salto=(i%2&&i<n)?-7:0;
      pasos.push({transform:'translate('+(dx*(1-t)).toFixed(1)+'px,'+(dy*(1-t)+salto).toFixed(1)+'px)'}); }
    var anim=f.animate(pasos,{duration:1600,easing:'linear'});
    anim.onfinish=function(){
      marca.replaceWith(f); f.classList.remove('en-camino');
      if(nom) nom.classList.remove('oculto');
      if(ag){ ag.classList.remove('caminando'); ag.classList.add('llega'); setTimeout(function(){ ag.classList.remove('llega'); },1200); }
    };
  }
  function anima(antes, estAntes){
    var vis=document.querySelector('.vista:not([hidden])'); if(!vis) return;
    vis.querySelectorAll('.fila').forEach(function(f){
      var k=f.getAttribute('data-dlg'); if(estAntes[k]!==undefined && estAntes[k]!==f.className){
        f.classList.add('cambio'); setTimeout(function(){ f.classList.remove('cambio'); },2600); }
    });
    if(quieto) return;
    vis.querySelectorAll('.figura').forEach(function(f){
      var a=antes[f.getAttribute('data-fig')]; if(!a) return;
      var b; try{ b=f.getBBox(); }catch(e){ return; }
      var dx=a.x-b.x, dy=a.y-b.y; if(Math.abs(dx)+Math.abs(dy)<20) return;
      camina(f,dx,dy);
    });
  }
  // Con --vigilar la página se sirve en localhost: pregunta si hubo cambios y
  // reemplaza solo el contenido (sin recargar, sin parpadeo).
  if(location.protocol.indexOf('http')===0 && document.body.getAttribute('data-version')){
    var version=document.body.getAttribute('data-version'), cada=(+document.body.getAttribute('data-cada')||10)*1000, ocupado=false;
    setInterval(function(){
      if(ocupado) return; ocupado=true;
      fetch('/version',{cache:'no-store'}).then(function(r){return r.text();}).then(function(v){
        v=v.trim(); if(!v||v===version) return;
        return fetch('/',{cache:'no-store'}).then(function(r){return r.text();}).then(function(h){
          var d=new DOMParser().parseFromString(h,'text/html');
          if(d.body.getAttribute('data-codigo')!==document.body.getAttribute('data-codigo')){ location.reload(); return; }
          var antes={}, estAntes={}, visA=document.querySelector('.vista:not([hidden])');
          if(visA){
            visA.querySelectorAll('.figura').forEach(function(f){ try{ antes[f.getAttribute('data-fig')]=f.getBBox(); }catch(e){} });
            visA.querySelectorAll('.fila').forEach(function(f){ estAntes[f.getAttribute('data-dlg')]=f.className; });
          }
          var abierto=document.querySelector('dialog[open]'), id=abierto&&abierto.id;
          if(abierto) abierto.close();
          var est=d.querySelector('style'); if(est) document.querySelector('style').textContent=est.textContent;
          var defs=d.querySelector('svg.defs'); if(defs) document.querySelector('svg.defs').replaceWith(defs);
          var actual=vistaActual; var abiertoMenu=!document.getElementById('vistas').hidden; document.querySelector('.vistas-menu').replaceWith(d.querySelector('.vistas-menu')); pestanas();
          if(abiertoMenu){ document.getElementById('vistas').hidden=false; document.getElementById('vistas-btn').setAttribute('aria-expanded','true'); }
          document.querySelector('main.planta').replaceWith(d.querySelector('main.planta'));
          version=v; enlaza(); muestra(actual); pinta(); anima(antes, estAntes);
          if(id){ var nd=document.getElementById(id); if(nd&&nd.showModal) nd.showModal(); }
        });
      }).catch(function(){}).then(function(){ ocupado=false; });
    },cada);
  }
})();
"""


PROGRAMA = [Path(__file__).resolve(), AQUI / "config.json", AQUI / "textos.json"]


def huella_programa():
    return tuple((str(r), r.stat().st_mtime) for r in PROGRAMA if r.exists())


# Cambia cuando cambia el programa (código, configuración o textos): la página abierta se recarga sola.
CODIGO = hashlib.sha1(b"".join(r.read_bytes() for r in PROGRAMA if r.exists())).hexdigest()[:10]


# ----------------------------------------------------------------- main
def abrir_ventana(url):
    """Abre la oficina en una ventana nueva del navegador predeterminado."""
    try:
        escritorio = subprocess.run(["xdg-settings", "get", "default-web-browser"], capture_output=True,
                                    text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        escritorio = ""
    nombre = escritorio.removesuffix(".desktop")
    candidatos = [nombre, nombre.split(".")[-1], "brave-browser", "google-chrome", "chromium", "firefox"]
    for cand in candidatos:
        ejecutable = cand and shutil.which(cand)
        if ejecutable:
            subprocess.Popen([ejecutable, "--new-window", url], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return
    webbrowser.open_new(url)


# ------------------------------------------------------- límites de uso
MESES = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun",
                                     "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _reinicio(texto):
    """'Nov 17, 6:39pm' -> epoch (hora local)."""
    m = re.match(r"([A-Za-z]{3})\w*\s+(\d{1,2}),?\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)", texto.strip(), re.I)
    if not m:
        return None
    mes, dia, h, mi, ap = MESES.get(m.group(1).lower()), int(m.group(2)), int(m.group(3)), int(m.group(4) or 0), m.group(5).lower()
    if not mes:
        return None
    h = h % 12 + (12 if ap == "pm" else 0)
    hoy = dt.datetime.now()
    r = dt.datetime(hoy.year, mes, dia, h, mi)
    if r < hoy - dt.timedelta(days=200):
        r = r.replace(year=hoy.year + 1)
    return int(r.timestamp())


def consultar_limites():
    """Pregunta a Claude Code su uso con /usage (comando interno: no usa el modelo
    ni gasta límite, y no guarda sesión) y lo deja en limites.json."""
    carpeta = Path.home() / ".cache" / "oficina-uso"
    carpeta.mkdir(parents=True, exist_ok=True)
    ejecutable = shutil.which("claude")
    if not ejecutable:
        return False
    try:
        r = subprocess.run([ejecutable, "-p", "/usage", "--no-session-persistence"], cwd=carpeta,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return False
    salida = r.stdout or ""

    def tramo(etiqueta):
        m = re.search(etiqueta + r"[^:]*:\s*(\d+(?:\.\d+)?)%\s*used\s*·\s*resets\s*([^(\n]+)", salida, re.I)
        return {"usado": float(m.group(1)), "reinicia": _reinicio(m.group(2))} if m else None
    cinco, semana = tramo("Current session"), tramo("Current week")
    if not (cinco or semana):
        return False
    datos = {"cinco_horas": cinco, "semana": semana, "actualizado": int(time.time())}
    destino = AQUI / "limites.json"
    temporal = destino.with_suffix(".tmp")
    temporal.write_text(json.dumps(datos), encoding="utf-8")
    os.replace(temporal, destino)
    return True


FASE_DE_ROL = CFG.get("fase_de_rol", {})


def completar_con_sesion(datos, ses):
    """Lo que hacen ahora los subagentes de la sesión manda sobre lo anotado en la
    bitácora, que el coordinador puede no haber actualizado todavía:
    - el rol de un subagente en marcha marca su fase como «En curso»;
    - «etapa N» en su encargo marca esa etapa del plan como en curso, y las etapas
      anteriores que ya tuvieron encargos terminados quedan como listas."""
    encargos = [(clave, x) for clave, lista in ses["encargos"].items() for x in lista]
    rol = {c: r for c, _n, r, _z in AGENTES}

    def etapas_de(tarea):
        m = re.search(r"etapas?\s+([\d\s,y-]+)", tarea or "", re.I)
        return {int(n) for n in re.findall(r"\d+", m.group(1))} if m else set()

    en_curso = {n for _c, x in encargos if x["estado"] == "trabajando" for n in etapas_de(x["tarea"])}
    con_encargo = {n for _c, x in encargos for n in etapas_de(x["tarea"])}
    tope = max(en_curso | con_encargo, default=0)
    for et in datos["etapas"]:
        n = et["num"]
        if n in en_curso:
            et.update(estado="trabajando", estado_txt=T["sesion"]["etapa_en_curso"])
        elif n in con_encargo and n < tope and et["estado"] != "listo":
            et.update(estado="listo", estado_txt=T["sesion"]["etapa_lista"])
    for clave, x in encargos:
        fase = FASE_DE_ROL.get(rol.get(clave, ""))
        if x["estado"] != "trabajando" or not fase:
            continue
        for f in datos["fases"]:
            if f["clave"] == fase and (f["fecha"] or dt.datetime.min) <= (x["inicio"] or dt.datetime.max):
                f.update(estado="trabajando", estado_txt=T["sesion"]["fase_en_curso"].format(tarea=x["tarea"]),
                         fecha=x["inicio"], agente=clave)
    # una fase «en curso» de la bitácora que ya no tiene a nadie trabajando en la sesión deja de ser la actual
    activas = {FASE_DE_ROL.get(rol.get(c, "")) for c, x in encargos if x["estado"] == "trabajando"}
    if activas - {None}:
        for f in datos["fases"]:
            if f["estado"] == "trabajando" and f["clave"] not in activas:
                f["estado"] = "listo"


def firma(h):
    # incluye la versión del programa: si cambia el diseño, la página abierta también se entera
    return hashlib.sha1((repr(h) + CODIGO).encode()).hexdigest()[:12]


def generar(args, version=""):
    tokens = {}
    for t in args.tokens or []:
        clave, _, valor = t.partition("=")
        try:
            tokens[clave.strip().lower()] = int(valor.replace(".", "").replace("_", ""))
        except ValueError:
            sys.exit(f"--tokens: valor inválido «{t}» (use nombre=número)")
    tablero = leer_tablero(args.tablero, args.modulo)
    fichas = leer_fichas(args.planes, args.modulo)
    datos = construir(tablero, fichas, tokens)
    ahora = dt.datetime.now().replace(second=0, microsecond=0)
    sesiones = leer_sesiones(args.sesiones, dt.datetime.now())
    for ses in sesiones:
        if ses["ficha"]:
            fic = ses["ficha"]
            ses["datos"] = construir(leer_tablero(args.tablero, fic), leer_fichas(args.planes, fic), {})
            completar_con_sesion(ses["datos"], ses)
    datos["sesiones"] = sesiones
    salida = pagina(datos, fichas, sesiones, args.modulo, args.avatares, args.vigilar, ahora, version)
    temporal = Path(args.salida).with_suffix(".tmp")
    temporal.write_text(salida, encoding="utf-8")
    os.replace(temporal, args.salida)  # nunca se sirve un archivo a medio escribir
    return datos


def huella(args):
    rutas = [Path(args.tablero), *Path(args.planes).glob(f"{args.modulo}*.md")]
    if args.avatares and Path(args.avatares).is_dir():
        rutas += list(Path(args.avatares).glob("*.png"))
    rutas.append(AQUI / "limites.json")
    if args.sesiones and Path(args.sesiones).is_dir():
        rutas += list(Path(args.sesiones).glob("*.jsonl"))
        rutas += list(Path(args.sesiones).glob("*/subagents/agent-*"))
    return tuple(sorted((str(r), r.stat().st_mtime) for r in rutas if r.exists()))


def main():
    p = argparse.ArgumentParser(description="Genera la oficina de agentes (HTML local).")
    p.add_argument("--modulo", default=MODULO)
    p.add_argument("--tablero", default=str(BITACORA / "tablero.md"))
    p.add_argument("--planes", default=str(BITACORA / "planes"))
    p.add_argument("--avatares", default=str(AQUI / "avatares"),
                   help="carpeta con <nombre>.png; vacío = solo personajes dibujados")
    p.add_argument("--salida", default=str(AQUI / "oficina.html"))
    p.add_argument("--sesiones", default=str(carpeta_sesiones()),
                   help="carpeta de sesiones de Claude Code de este ambiente; vacío = sin sesiones")
    p.add_argument("--tokens", action="append", metavar="NOMBRE=N",
                   help="tokens de un agente, anotados a mano (opcional, repetible)")
    p.add_argument("--vigilar", type=int, default=0, metavar="SEG",
                   help="sirve la página en http://localhost y la actualiza sin recargar cada SEG segundos")
    p.add_argument("--puerto", type=int, default=8765, help="puerto local para --vigilar (por defecto 8765)")
    p.add_argument("--sin-limites", action="store_true", help="no consultar los límites de uso con /usage")
    p.add_argument("--no-abrir", action="store_true", help="no abrir la oficina en una ventana nueva del navegador")
    args = p.parse_args()
    # sin bitácora al lado (por ejemplo, recién clonado el repo): usar los datos de ejemplo
    ejemplo = AQUI / "ejemplo"
    if not Path(args.tablero).exists() and (ejemplo / "tablero.md").exists():
        args.tablero, args.planes = str(ejemplo / "tablero.md"), str(ejemplo / "planes")
        print("No se encontró la bitácora: se usan los datos de ejemplo de la carpeta ejemplo/.")

    if not args.vigilar and not args.sin_limites:
        consultar_limites()
    anterior = huella(args)
    estado = {"version": firma(anterior)}
    datos = generar(args, estado["version"] if args.vigilar else "")
    activos = sum(1 for a in datos["agentes"] if a["estado"] != "libre")
    print(f"Oficina generada: {args.salida} ({activos} agentes con tarea, "
          f"{sum(1 for x in datos['sesiones'] if x['espera'])} esperando tu respuesta, "
          f"{len(datos['sesiones'])} sesiones activas)")
    if not args.vigilar:
        if not args.no_abrir:
            abrir_ventana(Path(args.salida).resolve().as_uri())
        return

    salida = Path(args.salida)

    class Manejador(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            ruta = self.path.split("?")[0]
            if ruta == "/limites":
                if estado.get("pedido"):
                    estado["pedido"].set()
                cuerpo, tipo = b"ok", "text/plain; charset=utf-8"
            elif ruta == "/version":
                cuerpo, tipo = estado["version"].encode(), "text/plain; charset=utf-8"
            elif ruta in ("/", "/oficina.html"):
                cuerpo, tipo = salida.read_bytes(), "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(cuerpo)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(cuerpo)

        def log_message(self, *a):
            pass

    programa_inicial = huella_programa()
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    try:
        servidor = http.server.ThreadingHTTPServer(("127.0.0.1", args.puerto), Manejador)
    except OSError:
        sys.exit(f"El puerto {args.puerto} está ocupado. Use otro con --puerto, por ejemplo --puerto 8766.")
    threading.Thread(target=servidor.serve_forever, daemon=True).start()

    pedido = threading.Event()   # clic en los medidores: consultar ya

    def cuenta_actual():
        """Solo el identificador de la cuenta conectada (no credenciales)."""
        try:
            d = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))
            return (d.get("oauthAccount") or {}).get("accountUuid")
        except (OSError, ValueError):
            return None

    def limites_periodicos():
        cuenta, proxima = None, 0.0
        while True:
            ahora_c = cuenta_actual()
            if pedido.is_set() or ahora_c != cuenta or time.time() >= proxima:
                pedido.clear()
                consultar_limites()
                cuenta, proxima = ahora_c, time.time() + max(1, U["limites_consulta_minutos"]) * 60
            pedido.wait(10)
    threading.Thread(target=limites_periodicos, daemon=True).start()
    estado["pedido"] = pedido
    url = f"http://localhost:{args.puerto}/"
    print(f"Oficina en vivo: {url} — revisa cambios cada {args.vigilar} s (Ctrl+C para salir)")
    if not args.no_abrir:
        abrir_ventana(url)
    try:
        while True:
            time.sleep(args.vigilar)
            actual = huella(args)
            if huella_programa() != programa_inicial:
                # cambió oficina.py, config.json o textos.json: reiniciar con el código nuevo
                print(time.strftime("%H:%M:%S"), "cambió el programa: reiniciando")
                servidor.shutdown()
                servidor.server_close()
                os.execv(sys.executable, [sys.executable, *sys.argv, "--no-abrir"] if "--no-abrir" not in sys.argv
                         else [sys.executable, *sys.argv])
            if actual != anterior:
                nueva = firma(actual)
                generar(args, nueva)
                estado["version"] = nueva
                anterior = actual
                print(time.strftime("%H:%M:%S"), "actualizada")
    except KeyboardInterrupt:
        pass
    finally:
        servidor.shutdown()


if __name__ == "__main__":
    main()
