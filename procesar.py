"""Fases 1-3 - Filtro, enriquecimiento, deduplicacion y export.

    python procesar.py                  # pipeline completo -> CSV
    python procesar.py --sin-enriquecimiento
    python procesar.py --limite 20
    python procesar.py --refrescar-sitios

Lee raw/*.csv que deja buscar.py, normaliza telefonos, visita la web de
cada taller para buscar celular / WhatsApp / correo, guarda todo en
data/contactos.db y exporta contactos_<fecha>.csv sin los ya contactados.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
from urllib.request import Request, urlopen

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RAIZ = Path(__file__).resolve().parent
RAW = RAIZ / "raw"
DATA = RAIZ / "data"
DB = DATA / "contactos.db"

# CABA
LAT_MIN, LAT_MAX = -34.71, -34.52
LON_MIN, LON_MAX = -58.54, -58.33

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
TIMEOUT = 8
MAX_PAGINAS = 2
PAUSA_DOMINIO = 1.0

EXCLUIR = [
    "gomeria", "lavadero", "concesionaria", "agencia de autos",
    "repuestos", "estacionamiento", "lubricentro", "taller de motos",
    "neumatic", "vidrieria", "playa de estacionamiento", "casa de repuestos",
    "distribuidor", "fabricante de autos", "servicio de remolque",
]
OBJETIVO = [
    "chapa", "carrocer", "pintura", "chapista", "enderez", "hojalater",
    "taller",
]

TEL_PATRONES = [
    r"\+?54[\s.\-]?9[\s.\-]?11[\s.\-]?\d{4}[\s.\-]?\d{4}",
    r"\+?54[\s.\-]?11[\s.\-]?\d{4}[\s.\-]?\d{4}",
    r"\(?0?11\)?[\s.\-]?15[\s.\-]?\d{4}[\s.\-]?\d{4}",
    r"\b15[\s.\-]?\d{4}[\s.\-]?\d{4}\b",
    r"\(?0?11\)?[\s.\-]?\d{4}[\s.\-]?\d{4}",
    r"\b\d{4}[\s.\-]\d{4}\b",
]
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
EMAIL_BASURA = ("example.com", "sentry", "wixpress", "domain.com", ".png", ".jpg")

_robots_cache: dict[str, RobotFileParser | None] = {}
_ultimo_acceso: dict[str, float] = {}


# --------------------------------------------------------------------------- #
# Telefonos
# --------------------------------------------------------------------------- #

def norm_texto(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def clasificar_tel(bruto: str, evidencia_wa: bool = False,
                   etiqueta_celular: bool = False) -> dict | None:
    """Devuelve {'e164', 'estado'} o None si no parece un telefono."""
    d = re.sub(r"\D", "", bruto or "")
    if d.startswith("0054"):
        d = "54" + d[4:]
    if d.startswith("0"):
        d = d[1:]

    e164 = None
    estado = None

    if d.startswith("54") and len(d) >= 10:
        resto = d[2:]
        if resto.startswith("9") and len(resto) >= 11:
            e164, estado = "+54" + resto, "celular"
        elif len(resto) >= 10:
            e164, estado = "+54" + resto, "ambiguo"
    elif d.startswith("911") and len(d) == 11:
        e164, estado = "+54" + d, "celular"
    elif d.startswith("11") and len(d) >= 10:
        resto = d[2:]
        if resto.startswith("15") and len(resto) >= 10:
            e164, estado = "+54911" + resto[2:], "celular"
        else:
            e164, estado = "+5411" + resto, "ambiguo"
    elif d.startswith("15") and len(d) == 10:
        e164, estado = "+54911" + d[2:], "probable_celular"
    elif len(d) == 8:
        e164, estado = "+5411" + d, "ambiguo"

    if not e164 or not (8 <= len(re.sub(r"\D", "", e164)) <= 15):
        return None

    if estado != "celular" and (evidencia_wa or etiqueta_celular):
        estado = "celular"
    fuente = "wa" if evidencia_wa else ("etiqueta" if etiqueta_celular else "formato")
    return {"e164": e164, "estado": estado, "fuente": fuente}


def jerarquia(estado: str) -> int:
    return {"celular": 0, "probable_celular": 1, "ambiguo": 2, "fijo": 3}.get(estado, 9)


def clave_linea(tel: str) -> str:
    """Misma linea telefonica en sus dos formas: +54 11 X y +54 9 11 X."""
    d = re.sub(r"\D", "", tel or "")
    if d.startswith("549"):
        d = "54" + d[3:]
    return d


# --------------------------------------------------------------------------- #
# Descarga
# --------------------------------------------------------------------------- #

def _pedir(url: str, timeout: int, contexto=None) -> bytes | str | None:
    req = Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "es-AR,es;q=0.9",
        "Accept-Encoding": "identity",
    })
    with urlopen(req, timeout=timeout, context=contexto) as r:
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype and "text" not in ctype:
            return None
        return r.read(800_000)


def descargar(url: str, timeout: int = TIMEOUT) -> str | None:
    candidatos = [url]
    partes = urlparse(url)
    if partes.netloc.startswith("www."):
        candidatos.append(url.replace("www.", "", 1))
    if partes.scheme == "http":
        candidatos.append("https://" + url[len("http://"):])

    import ssl

    for candidato in candidatos:
        for contexto in (None, ssl._create_unverified_context()):
            try:
                crudo = _pedir(candidato, timeout, contexto)
            except Exception as exc:
                if contexto is None and "CERTIFICATE_VERIFY_FAILED" in str(exc):
                    continue  # reintenta sin validar el certificado
                break
            if crudo is None:
                return None
            for enc in ("utf-8", "latin-1"):
                try:
                    return crudo.decode(enc)
                except UnicodeDecodeError:
                    continue
            return crudo.decode("utf-8", "ignore")
    return None


def permitido(url: str) -> bool:
    partes = urlparse(url)
    base = f"{partes.scheme}://{partes.netloc}"
    if base not in _robots_cache:
        try:
            rp = RobotFileParser()
            rp.set_url(urljoin(base, "/robots.txt"))
            rp.read()
            _robots_cache[base] = rp
        except Exception:
            _robots_cache[base] = None
    rp = _robots_cache[base]
    if rp is None:
        return True
    try:
        return rp.can_fetch(UA, url)
    except Exception:
        return True


def esperar_dominio(url: str) -> None:
    host = urlparse(url).netloc
    desde = _ultimo_acceso.get(host)
    if desde is not None:
        falta = PAUSA_DOMINIO - (time.time() - desde)
        if falta > 0:
            time.sleep(falta)
    _ultimo_acceso[host] = time.time()


def texto_visible(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = html.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", html)


def extraer_sitio(html: str, url: str) -> dict:
    tels: list[dict] = []
    wa_nums: list[str] = []
    mails: list[str] = []

    for m in re.findall(r'href=["\']tel:([^"\']+)', html, re.I):
        tels.append({"bruto": m, "evidencia_wa": False, "etiqueta": False})

    for patron in (
        r"wa\.me/(\+?\d{8,15})",
        r"api\.whatsapp\.com/send\?phone=(\+?\d{8,15})",
        r"whatsapp\.com/send\?phone=(\+?\d{8,15})",
        r"whatsapp://send\?phone=(\+?\d{8,15})",
    ):
        wa_nums.extend(re.findall(patron, html, re.I))

    vis = texto_visible(html)

    for patron in TEL_PATRONES:
        for m in re.finditer(patron, vis):
            contexto = vis[max(0, m.start() - 45): m.start()].lower()
            etiqueta = bool(re.search(r"whats?app|wsp|wpp|celular|movil", contexto))
            tels.append({"bruto": m.group(0), "evidencia_wa": False, "etiqueta": etiqueta})

    for m in re.findall(r'mailto:([^"\'?>\s]+)', html, re.I):
        mails.append(m)
    mails.extend(EMAIL_RE.findall(html))

    mails = [
        m.strip().lower() for m in mails
        if not any(b in m.lower() for b in EMAIL_BASURA)
        and not re.search(r"\.(png|jpe?g|gif|svg|webp)$", m.lower())
    ]

    return {
        "tels": tels,
        "wa": [re.sub(r"\D", "", w) for w in wa_nums],
        "mails": sorted(set(mails))[:5],
    }


def enriquecer(url: str) -> dict:
    """Visita home + una pagina de contacto. Devuelve telefonos/emails/wa."""
    partes = urlparse(url)
    if partes.scheme not in ("http", "https"):
        url = "https://" + url
    if not permitido(url):
        return {"tels": [], "wa": [], "mails": [], "bloqueado": True}

    vistos: set[str] = set()
    resultado = {"tels": [], "wa": [], "mails": [], "bloqueado": False}
    paginas = [url]
    base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"

    for candidato in ("/contacto", "/contact", "/contactanos"):
        if len(paginas) >= MAX_PAGINAS + 1:
            break
        paginas.append(urljoin(base, candidato))

    for pagina in paginas:
        if len(vistos) >= MAX_PAGINAS:
            break
        if pagina in vistos:
            continue
        vistos.add(pagina)
        esperar_dominio(pagina)
        html = descargar(pagina)
        if not html:
            continue
        datos = extraer_sitio(html, pagina)
        resultado["tels"].extend(datos["tels"])
        resultado["wa"].extend(datos["wa"])
        resultado["mails"].extend(datos["mails"])
    return resultado


def intentar_instagram(url: str) -> dict:
    """Best effort: el og:description publico a veces trae el telefono."""
    usuario = None
    m = re.search(r"instagram\.com/([A-Za-z0-9_.]+)", url or "")
    if m:
        usuario = m.group(1)
    if not usuario or usuario in {"p", "reel", "explore"}:
        return {"tels": [], "wa": [], "mails": []}

    esperar_dominio("https://instagram.com")
    html = descargar(f"https://www.instagram.com/{usuario}/", timeout=6)
    if not html:
        return {"tels": [], "wa": [], "mails": []}
    og = re.search(r'property=["\']og:description["\']\s+content=["\']([^"\']+)', html)
    texto = og.group(1) if og else ""
    datos = extraer_sitio(texto, url)
    return datos


# --------------------------------------------------------------------------- #
# Base de datos
# --------------------------------------------------------------------------- #

ESQUEMA = """
CREATE TABLE IF NOT EXISTS negocios(
    id INTEGER PRIMARY KEY,
    place_id TEXT UNIQUE,
    nombre TEXT, direccion TEXT, categoria TEXT, sitio_web TEXT,
    celular TEXT, telefono_fijo TEXT, telefono_alt TEXT,
    estado_celular TEXT, evidencia TEXT, correo TEXT,
    fuente_url TEXT, lat REAL, lon REAL,
    fecha_extraccion TEXT,
    contactado INTEGER DEFAULT 0, fecha_contacto TEXT, notas TEXT
);
CREATE TABLE IF NOT EXISTS sitios(
    dominio TEXT PRIMARY KEY, url TEXT, fetched_at TEXT,
    telefonos TEXT, emails TEXT, wa TEXT, ok INTEGER
);
"""


def abrir_db() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.executescript(ESQUEMA)
    return con


def cache_sitio(con, dominio: str, refrescar: bool) -> dict | None:
    if refrescar:
        return None
    fila = con.execute("SELECT * FROM sitios WHERE dominio=?", (dominio,)).fetchone()
    if not fila:
        return None
    if not fila["ok"]:
        return None
    return {
        "tels": json.loads(fila["telefonos"]),
        "wa": json.loads(fila["wa"]),
        "mails": json.loads(fila["emails"]),
        "bloqueado": False,
    }


def guardar_sitio(con, dominio: str, url: str, datos: dict) -> None:
    con.execute(
        """INSERT INTO sitios(dominio, url, fetched_at, telefonos, emails, wa, ok)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(dominio) DO UPDATE SET
             url=excluded.url, fetched_at=excluded.fetched_at,
             telefonos=excluded.telefonos, emails=excluded.emails,
             wa=excluded.wa, ok=excluded.ok""",
        (
            dominio, url, datetime.now().isoformat(timespec="seconds"),
            json.dumps(datos["tels"], ensure_ascii=False),
            json.dumps(datos["mails"], ensure_ascii=False),
            json.dumps(datos["wa"], ensure_ascii=False),
            1 if (datos["tels"] or datos["mails"] or datos["wa"]) else 0,
        ),
    )
    con.commit()


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #

def leer_crudos() -> list[dict]:
    filas: list[dict] = []
    for archivo in sorted(RAW.glob("*.csv")):
        with archivo.open(encoding="utf-8", errors="replace", newline="") as f:
            for fila in csv.DictReader(f):
                filas.append(fila)
    return filas


def en_caba(fila: dict) -> bool:
    try:
        lat = float(fila.get("latitude") or "nan")
        lon = float(fila.get("longitude") or "nan")
        if LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX:
            return True
    except ValueError:
        pass
    direccion = norm_texto(fila.get("complete_address") or fila.get("address") or "")
    return any(x in direccion for x in (
        "caba", "capital federal", "ciudad autonoma de buenos aires",
        "autonoma de buenos aires", "cdad autonoma de buenos aires",
    ))


def es_objetivo(fila: dict) -> bool:
    texto = norm_texto(f"{fila.get('title','')} {fila.get('category','')}")
    if any(p in texto for p in EXCLUIR):
        return False
    return any(p in texto for p in OBJETIVO)


def elegir_telefonos(tels: list[dict], wa: list[str]) -> dict:
    """De todos los telefonos de una ficha, elige los mejores."""
    wa_set = {w[-10:] for w in wa if len(w) >= 8}
    clasificados = []
    for t in tels:
        c = clasificar_tel(t["bruto"], t.get("evidencia_wa", False), t.get("etiqueta", False))
        if c:
            clasificados.append(c)
    for w in wa:
        c = clasificar_tel(w, evidencia_wa=True)
        if c:
            clasificados.append(c)

    unicos: dict[str, dict] = {}
    for c in clasificados:
        clave = c["e164"]
        previo = unicos.get(clave)
        if previo is None or (
            jerarquia(c["estado"]), 0 if c["fuente"] == "wa" else 1
        ) < (jerarquia(previo["estado"]), 0 if previo["fuente"] == "wa" else 1):
            unicos[clave] = c

    ordenados = sorted(
        unicos.values(),
        key=lambda c: (jerarquia(c["estado"]), 0 if c["fuente"] == "wa" else 1, c["e164"]),
    )
    if not ordenados:
        return {"celular": "", "fijo": "", "alt": "", "estado": "", "evidencia": ""}

    moviles = [c for c in ordenados if c["estado"] in ("celular", "probable_celular")]
    fijos = [c for c in ordenados if c["estado"] not in ("celular", "probable_celular")]

    principal = moviles[0] if moviles else ordenados[0]
    resto = [c["e164"] for c in ordenados if c["e164"] != principal["e164"]]

    evidencia = ""
    if principal["fuente"] == "wa":
        evidencia = "link wa.me en el sitio"
    elif principal["fuente"] == "etiqueta":
        evidencia = "rotulado WhatsApp/celular en el sitio"

    return {
        "celular": principal["e164"] if moviles else "",
        "fijo": principal["e164"] if not moviles else "",
        "alt": " | ".join(resto),
        "estado": principal["estado"],
        "evidencia": evidencia,
    }


def upsert(con, negocio: dict) -> None:
    con.execute(
        """INSERT INTO negocios(
              place_id, nombre, direccion, categoria, sitio_web,
              celular, telefono_fijo, telefono_alt, estado_celular,
              evidencia, correo, fuente_url, lat, lon, fecha_extraccion)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(place_id) DO UPDATE SET
              nombre=excluded.nombre, direccion=excluded.direccion,
              categoria=excluded.categoria,
              sitio_web=COALESCE(NULLIF(excluded.sitio_web,''), negocios.sitio_web),
              celular=COALESCE(NULLIF(excluded.celular,''), negocios.celular),
              telefono_fijo=COALESCE(NULLIF(excluded.telefono_fijo,''), negocios.telefono_fijo),
              telefono_alt=COALESCE(NULLIF(excluded.telefono_alt,''), negocios.telefono_alt),
              estado_celular=COALESCE(NULLIF(excluded.estado_celular,''), negocios.estado_celular),
              evidencia=COALESCE(NULLIF(excluded.evidencia,''), negocios.evidencia),
              correo=COALESCE(NULLIF(excluded.correo,''), negocios.correo),
              fuente_url=COALESCE(NULLIF(excluded.fuente_url,''), negocios.fuente_url),
              lat=excluded.lat, lon=excluded.lon,
              fecha_extraccion=excluded.fecha_extraccion""",
        (
            negocio["place_id"], negocio["nombre"], negocio["direccion"],
            negocio["categoria"], negocio["sitio_web"], negocio["celular"],
            negocio["telefono_fijo"], negocio["telefono_alt"], negocio["estado"],
            negocio["evidencia"], negocio["correo"], negocio["fuente_url"],
            negocio["lat"], negocio["lon"], negocio["fecha"],
        ),
    )


def procesar(limite: int | None, sin_enriquecimiento: bool, refrescar: bool) -> int:
    crudos = leer_crudos()
    print(f"Filas crudas: {len(crudos)}")

    vistos: set[str] = set()
    candidatos: list[dict] = []
    for fila in crudos:
        place_id = (fila.get("place_id") or "").strip()
        if not place_id:
            place_id = "local:" + norm_texto(f"{fila.get('title')}|{fila.get('address')}")
        if place_id in vistos:
            continue
        if not en_caba(fila) or not es_objetivo(fila):
            continue
        vistos.add(place_id)
        candidatos.append({
            "place_id": place_id,
            "nombre": (fila.get("title") or "").strip(),
            "direccion": (fila.get("complete_address") or fila.get("address") or "").strip(),
            "categoria": (fila.get("category") or "").strip(),
            "sitio_web": (fila.get("website") or "").strip(),
            "telefono": (fila.get("phone") or "").strip(),
            "correo": "",
            "fuente_url": (fila.get("link") or "").strip(),
            "lat": fila.get("latitude") or "",
            "lon": fila.get("longitude") or "",
            "fecha": datetime.now().date().isoformat(),
        })

    print(f"Tras filtro CABA + rubro: {len(candidatos)} talleres unicos")
    if limite:
        candidatos = candidatos[:limite]

    con = abrir_db()

    if not sin_enriquecimiento:
        total = len(candidatos)
        for i, neg in enumerate(candidatos, 1):
            tels = [{"bruto": neg["telefono"], "evidencia_wa": False, "etiqueta": False}] if neg["telefono"] else []
            wa: list[str] = []
            mails: list[str] = []

            if neg["sitio_web"]:
                dominio = urlparse(neg["sitio_web"] if "://" in neg["sitio_web"] else "https://" + neg["sitio_web"]).netloc.lower()
                datos = cache_sitio(con, dominio, refrescar)
                if datos is None:
                    datos = enriquecer(neg["sitio_web"])
                    guardar_sitio(con, dominio, neg["sitio_web"], datos)
                tels.extend(datos["tels"])
                wa.extend(datos["wa"])
                mails.extend(datos["mails"])
                if not tels and not mails:
                    ig = re.search(r"instagram\.com/[A-Za-z0-9_.]+", neg["sitio_web"])
                    if ig:
                        extra = intentar_instagram(neg["sitio_web"])
                        tels.extend(extra["tels"])
                        wa.extend(extra["wa"])
                        mails.extend(extra["mails"])

            elegido = elegir_telefonos(tels, wa)
            neg.update({
                "celular": elegido["celular"],
                "telefono_fijo": elegido["fijo"],
                "telefono_alt": elegido["alt"],
                "estado": elegido["estado"],
                "evidencia": elegido["evidencia"],
                "correo": (mails[0] if mails else neg["correo"]),
            })
            upsert(con, neg)

            if i % 10 == 0 or i == total:
                print(f"  enriquecidos {i}/{total}")
        con.commit()
    else:
        for neg in candidatos:
            elegido = elegir_telefonos(
                [{"bruto": neg["telefono"], "evidencia_wa": False, "etiqueta": False}]
                if neg["telefono"] else [], []
            )
            neg.update({
                "celular": elegido["celular"],
                "telefono_fijo": elegido["fijo"],
                "telefono_alt": elegido["alt"],
                "estado": elegido["estado"],
                "evidencia": elegido["evidencia"],
                "correo": neg["correo"],
            })
            upsert(con, neg)
        con.commit()

    consolidar(con)
    exportar(con)
    return 0


def _numeros(fila: dict) -> list[str]:
    numeros = []
    for campo in ("celular", "telefono_fijo"):
        if fila.get(campo):
            numeros.append(fila[campo])
    for t in (fila.get("telefono_alt") or "").split("|"):
        if t.strip():
            numeros.append(t.strip())
    return numeros


def consolidar(con) -> int:
    """Q9-B: fusiona solo fichas que comparten un telefono identico."""
    filas = [dict(f) for f in con.execute(
        "SELECT * FROM negocios ORDER BY contactado DESC, id"
    ).fetchall()]

    canonicos: dict[str, int] = {}
    fusiones = 0

    for fila in filas:
        claves = [clave_linea(n) for n in _numeros(fila) if clave_linea(n)]
        if not claves:
            continue

        destino_id = next((canonicos[c] for c in claves if c in canonicos), None)
        if destino_id is None:
            for c in claves:
                canonicos[c] = fila["id"]
            continue

        base = dict(con.execute(
            "SELECT * FROM negocios WHERE id=?", (destino_id,)
        ).fetchone())

        nuevos = {}
        if not base["celular"] and fila["celular"]:
            nuevos["celular"] = fila["celular"]
        if not base["telefono_fijo"] and fila["telefono_fijo"]:
            nuevos["telefono_fijo"] = fila["telefono_fijo"]
        for campo in ("correo", "sitio_web", "direccion", "categoria", "fuente_url"):
            if not base[campo] and fila[campo]:
                nuevos[campo] = fila[campo]

        if fila["estado_celular"] and (
            not base["estado_celular"]
            or jerarquia(fila["estado_celular"]) < jerarquia(base["estado_celular"])
        ):
            nuevos["estado_celular"] = fila["estado_celular"]
            nuevos["evidencia"] = fila["evidencia"] or base["evidencia"]

        usados = {
            clave_linea(nuevos.get("celular") or base["celular"]),
            clave_linea(nuevos.get("telefono_fijo") or base["telefono_fijo"]),
        } - {""}
        alternativos = [n for n in _numeros(base) if clave_linea(n) not in usados]
        for n in _numeros(fila):
            if n and clave_linea(n) not in usados and n not in alternativos:
                alternativos.append(n)
        nuevos["telefono_alt"] = " | ".join(dict.fromkeys(alternativos))

        if norm_texto(fila["nombre"]) != norm_texto(base["nombre"]):
            nota = f"tambien: {fila['nombre']}"
            nuevos["notas"] = ((base["notas"] or "") + " / " + nota).strip(" /")

        asignaciones = ", ".join(f"{c}=?" for c in nuevos)
        con.execute(
            f"UPDATE negocios SET {asignaciones} WHERE id=?",
            list(nuevos.values()) + [destino_id],
        )
        con.execute("DELETE FROM negocios WHERE id=?", (fila["id"],))
        fusiones += 1

        for c in claves:
            canonicos.setdefault(c, destino_id)

    con.commit()
    if fusiones:
        print(f"Fusiones por telefono identico: {fusiones}")
    return fusiones


def exportar(con) -> None:
    filas = [dict(f) for f in con.execute(
        "SELECT * FROM negocios WHERE contactado=0 ORDER BY id"
    ).fetchall()]

    # Si un numero fue confirmado como celular en cualquier fuente, se
    # propaga a todas las fichas que comparten esa misma linea.
    moviles = {clave_linea(f["celular"]) for f in filas if f["celular"]}
    for f in filas:
        if f["celular"]:
            continue
        for tel in (f["telefono_fijo"], f["telefono_alt"]):
            for t in (tel or "").split("|"):
                if t.strip() and clave_linea(t) in moviles:
                    f["celular"] = t.strip()
                    f["estado_celular"] = "celular"
                    f["evidencia"] = "celular confirmado en otra ficha"
                    break
            if f["celular"]:
                break

    # Q9bis-B: nombre+direccion exactos no se fusionan, solo se marcan.
    canonicos: dict[tuple[str, str], int] = {}
    for f in filas:
        clave = (norm_texto(f["nombre"]), norm_texto(f["direccion"]))
        if clave in canonicos and canonicos[clave] != f["id"]:
            f["posible_duplicado_de"] = canonicos[clave]
        else:
            canonicos.setdefault(clave, f["id"])
            f["posible_duplicado_de"] = ""

    filas.sort(key=lambda f: (
        jerarquia(f["estado_celular"]) if f["estado_celular"] else 9,
        0 if f["celular"] else 1,
        norm_texto(f["nombre"]),
    ))

    fecha = datetime.now().strftime("%Y-%m-%d_%H%M")
    salida = RAIZ / f"contactos_{fecha}.csv"
    columnas = [
        "id", "fecha_extraccion", "nombre_negocio", "celular", "sitio_web",
        "correo", "telefono_fijo", "telefono_alternativo", "estado_celular",
        "evidencia", "direccion", "categoria", "fuente_url", "no_llame",
        "posible_duplicado_de", "notas",
    ]
    with salida.open("w", encoding="utf-8-sig", newline="") as f:
        escritor = csv.DictWriter(f, fieldnames=columnas, extrasaction="ignore")
        escritor.writeheader()
        for fila in filas:
            escritor.writerow({
                "id": fila["id"],
                "fecha_extraccion": fila["fecha_extraccion"],
                "nombre_negocio": fila["nombre"],
                "celular": fila["celular"],
                "sitio_web": fila["sitio_web"],
                "correo": fila["correo"],
                "telefono_fijo": fila["telefono_fijo"],
                "telefono_alternativo": fila["telefono_alt"],
                "estado_celular": fila["estado_celular"],
                "evidencia": fila["evidencia"],
                "direccion": fila["direccion"],
                "categoria": fila["categoria"],
                "fuente_url": fila["fuente_url"],
                "no_llame": "",
                "posible_duplicado_de": fila["posible_duplicado_de"],
                "notas": fila["notas"] or "",
            })

    con_movil = sum(1 for f in filas if f["celular"])
    print(f"\nCSV: {salida.name}")
    print(f"  filas: {len(filas)}")
    print(f"  con celular: {con_movil} ({con_movil * 100 // max(len(filas), 1)}%)")
    print(f"  con correo: {sum(1 for f in filas if f['correo'])}")


def main() -> int:
    limite = None
    if "--limite" in sys.argv:
        try:
            limite = int(sys.argv[sys.argv.index("--limite") + 1])
        except (IndexError, ValueError):
            print("--limite necesita un numero")
            return 2
    return procesar(
        limite=limite,
        sin_enriquecimiento="--sin-enriquecimiento" in sys.argv,
        refrescar="--refrescar-sitios" in sys.argv,
    )


if __name__ == "__main__":
    raise SystemExit(main())
