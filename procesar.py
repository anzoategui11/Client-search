"""Fases 1-3 - Filtro, enriquecimiento, deduplicacion y export.

    python procesar.py                  # pipeline completo -> CSV
    python procesar.py --sin-enriquecimiento
    python procesar.py --limite 20
    python procesar.py --refrescar-sitios

Lee raw/*.csv que deja buscar.py, normaliza telefonos, visita la web de
cada taller para buscar celular / WhatsApp / correo, guarda todo en
data/contactos.db y exporta contactos_<fecha>.csv sin los ya contactados.

Flujo:
    leer_crudos -> filtro (en_caba, es_objetivo) -> enriquecer (web del
    taller, con cache en la tabla `sitios`) -> elegir_telefonos -> upsert
    -> consolidar (fusiona fichas con el mismo telefono) -> exportar
"""

from __future__ import annotations

import csv
import html
import json
import re
import sqlite3
import ssl
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import unquote, urljoin, urlparse
from urllib.robotparser import RobotFileParser
from urllib.request import Request, urlopen

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# El scraper guarda resenias y horarios como JSON dentro de una celda. El
# limite por defecto de csv (128 KB) puede quedar corto con fichas grandes.
# En Windows sys.maxsize no entra en un long de C, de ahi el tope de 32 bits.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

RAIZ = Path(__file__).resolve().parent
RAW = RAIZ / "raw"
DATA = RAIZ / "data"
DB = DATA / "contactos.db"

# Rectangulo que contiene a CABA. Es una aproximacion: toca bordes del
# conurbano. Si no hay coordenadas se decide por el texto de la direccion.
LAT_MIN, LAT_MAX = -34.71, -34.52
LON_MIN, LON_MAX = -58.54, -58.33

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
TIMEOUT = 8            # segundos por pedido HTTP (tambien para robots.txt)
MAX_PAGINAS = 2        # home + una pagina de contacto por sitio
PAUSA_DOMINIO = 1.0    # segundos minimos entre pedidos al mismo host
MAX_BYTES = 800_000    # se corta la descarga de paginas enormes

# Rubros que aparecen en las busquedas pero no son clientes.
EXCLUIR = [
    "gomeria", "lavadero", "concesionaria", "agencia de autos",
    "repuestos", "estacionamiento", "lubricentro", "taller de motos",
    "neumatic", "vidrieria", "playa de estacionamiento", "casa de repuestos",
    "distribuidor", "fabricante de autos", "servicio de remolque",
]
# Al menos una de estas palabras tiene que estar en nombre o categoria.
OBJETIVO = [
    "chapa", "carrocer", "pintura", "chapista", "enderez", "hojalater",
    "taller",
]

# Patrones de telefono, del mas especifico al mas generico. El orden importa:
# un tramo de texto que ya matcheo un patron no se vuelve a leer con los
# siguientes (ver buscar_telefonos).
TEL_PATRONES = [re.compile(p) for p in (
    r"\+?54[\s.\-]?9[\s.\-]?11[\s.\-]?\d{4}[\s.\-]?\d{4}",   # +54 9 11 XXXX-XXXX
    r"\+?54[\s.\-]?11[\s.\-]?\d{4}[\s.\-]?\d{4}",            # +54 11 XXXX-XXXX
    r"\(?0?11\)?[\s.\-]?15[\s.\-]?\d{4}[\s.\-]?\d{4}",       # (011) 15 XXXX-XXXX
    r"\b15[\s.\-]?\d{4}[\s.\-]?\d{4}\b",                     # 15 XXXX-XXXX
    r"\(?0?11\)?[\s.\-]?\d{4}[\s.\-]?\d{4}",                 # (011) XXXX-XXXX
    r"\b\d{4}[\s.\-]\d{4}\b",                                # XXXX-XXXX
)]
# "2015-2024" en el pie de cualquier web matchea XXXX-XXXX: no es un telefono.
RANGO_ANIOS = re.compile(r"^(19|20)\d\d[\s.\-](19|20)\d\d$")
# Palabras que, justo antes del numero, indican que es un celular.
ETIQUETA_CELULAR = re.compile(r"whats?app|wsp|wpp|celular|movil")
CONTEXTO_ETIQUETA = 45  # caracteres hacia atras donde se busca la etiqueta

WA_PATRONES = [re.compile(p, re.I) for p in (
    r"wa\.me/(\+?\d{8,15})",
    r"api\.whatsapp\.com/send\?phone=(\+?\d{8,15})",
    r"whatsapp\.com/send\?phone=(\+?\d{8,15})",
    r"whatsapp://send\?phone=(\+?\d{8,15})",
)]

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
EMAIL_BASURA = ("example.com", "sentry", "wixpress", "domain.com", ".png", ".jpg")

# Algunos talleres tienen el certificado vencido o mal armado. Para leer un
# telefono publico alcanza con reintentar sin validar.
_SSL_SIN_VALIDAR = ssl._create_unverified_context()

_robots_cache: dict[str, RobotFileParser | None] = {}   # base -> parser (None = sin robots)
_ultimo_acceso: dict[str, float] = {}                    # host -> time.time() del ultimo pedido


# --------------------------------------------------------------------------- #
# Telefonos
# --------------------------------------------------------------------------- #

def norm_texto(s: str) -> str:
    """Minusculas, sin tildes y sin signos: 'Carrocería S.A.' -> 'carroceria s a'."""
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def a_movil(e164: str) -> str:
    """+5411XXXXXXXX -> +54911XXXXXXXX. Un celular siempre va con el 9."""
    if e164.startswith("+5411") and len(e164) == 13:
        return "+54911" + e164[5:]
    return e164


def clasificar_tel(bruto: str, evidencia_wa: bool = False,
                   etiqueta_celular: bool = False) -> dict | None:
    """Normaliza un telefono y estima si es celular.

    Devuelve {'e164', 'estado', 'fuente'} o None si no parece un telefono.
      estado: 'celular' | 'probable_celular' | 'ambiguo'
      fuente: 'wa' (link de WhatsApp) | 'etiqueta' (texto cercano) | 'formato'
    """
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
        elif resto.startswith("1115") and len(resto) == 12:
            # +54 11 15 XXXX-XXXX: formato mezclado, el 15 local marca celular.
            e164, estado = "+54911" + resto[4:], "celular"
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

    # La evidencia (link wa.me o la palabra "WhatsApp" al lado) pesa mas que
    # el formato: en CABA fijo y movil tienen la misma cantidad de digitos.
    if estado != "celular" and (evidencia_wa or etiqueta_celular):
        estado = "celular"
    if estado == "celular":
        e164 = a_movil(e164)
    fuente = "wa" if evidencia_wa else ("etiqueta" if etiqueta_celular else "formato")
    return {"e164": e164, "estado": estado, "fuente": fuente}


def jerarquia(estado: str) -> int:
    """Orden de confianza: menor es mejor. Estados desconocidos o vacios van al final."""
    return {"celular": 0, "probable_celular": 1, "ambiguo": 2, "fijo": 3}.get(estado, 9)


def clave_linea(tel: str) -> str:
    """Misma linea telefonica en sus dos formas: +54 11 X y +54 9 11 X."""
    d = re.sub(r"\D", "", tel or "")
    if d.startswith("549"):
        d = "54" + d[3:]
    return d


def buscar_telefonos(texto: str) -> list[dict]:
    """Encuentra telefonos en texto plano.

    Los patrones van del mas especifico al mas generico y cada tramo de texto
    se usa una sola vez. Sin eso, "+54 9 11 4321-5678" tambien saldria como
    "4321-5678" (patron generico) y quedaria anotado como un segundo numero.
    """
    ocupados: list[tuple[int, int]] = []
    hallados: list[dict] = []
    for patron in TEL_PATRONES:
        for m in patron.finditer(texto):
            ini, fin = m.span()
            if any(ini < f and i < fin for i, f in ocupados):
                continue
            ocupados.append((ini, fin))
            if RANGO_ANIOS.match(m.group(0).strip()):
                continue
            antes = texto[max(0, ini - CONTEXTO_ETIQUETA):ini].lower()
            hallados.append({
                "bruto": m.group(0),
                "evidencia_wa": False,
                "etiqueta": bool(ETIQUETA_CELULAR.search(antes)),
            })
    return hallados


# --------------------------------------------------------------------------- #
# Descarga
# --------------------------------------------------------------------------- #

def _pedir(url: str, timeout: int, contexto: ssl.SSLContext | None = None) -> bytes | None:
    """GET crudo. Devuelve None si la respuesta no es HTML/texto (PDF, imagen...)."""
    req = Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "es-AR,es;q=0.9",
        "Accept-Encoding": "identity",   # sin gzip: urllib no lo descomprime solo
    })
    with urlopen(req, timeout=timeout, context=contexto) as r:
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype and "text" not in ctype:
            return None
        return r.read(MAX_BYTES)


def _decodificar(crudo: bytes) -> str:
    try:
        return crudo.decode("utf-8")
    except UnicodeDecodeError:
        return crudo.decode("latin-1")   # latin-1 acepta cualquier byte


def descargar(url: str, timeout: int = TIMEOUT) -> str | None:
    """Baja una pagina probando variantes (sin www, https) y, si el
    certificado falla, sin validarlo. Devuelve el HTML o None."""
    candidatos = [url]
    partes = urlparse(url)
    if partes.netloc.startswith("www."):
        candidatos.append(url.replace("www.", "", 1))
    if partes.scheme == "http":
        candidatos.append("https://" + url[len("http://"):])

    for candidato in candidatos:
        for contexto in (None, _SSL_SIN_VALIDAR):
            try:
                crudo = _pedir(candidato, timeout, contexto)
            except HTTPError:
                # El servidor contesto (404, 403, 500...): otra variante de la
                # misma URL no va a cambiar la respuesta. Se ahorran pedidos.
                return None
            except Exception as exc:
                if contexto is None and "CERTIFICATE_VERIFY_FAILED" in str(exc):
                    continue  # reintenta sin validar el certificado
                break         # DNS, timeout, conexion rechazada: proxima variante
            if crudo is None:
                return None
            return _decodificar(crudo)
    return None


def _leer_robots(base: str) -> RobotFileParser | None:
    """Baja robots.txt con timeout. RobotFileParser.read() no tiene timeout
    y un sitio colgado frenaba todo el procesamiento."""
    try:
        req = Request(urljoin(base, "/robots.txt"), headers={"User-Agent": UA})
        with urlopen(req, timeout=TIMEOUT) as r:
            texto = _decodificar(r.read(MAX_BYTES))
    except HTTPError as exc:
        if exc.code in (401, 403):
            # Mismo criterio que la libreria estandar: acceso restringido = todo prohibido.
            rp = RobotFileParser()
            rp.disallow_all = True
            return rp
        return None   # 404 y similares: no hay robots.txt, todo permitido
    except Exception:
        return None
    rp = RobotFileParser()
    rp.parse(texto.splitlines())
    return rp


def permitido(url: str) -> bool:
    """True si robots.txt deja leer esa URL. Ante cualquier duda, permite."""
    partes = urlparse(url)
    base = f"{partes.scheme}://{partes.netloc}"
    if base not in _robots_cache:
        _robots_cache[base] = _leer_robots(base)
    rp = _robots_cache[base]
    if rp is None:
        return True
    try:
        return rp.can_fetch(UA, url)
    except Exception:
        return True


def esperar_dominio(url: str) -> None:
    """Respeta PAUSA_DOMINIO entre dos pedidos al mismo host."""
    host = urlparse(url).netloc
    desde = _ultimo_acceso.get(host)
    if desde is not None:
        falta = PAUSA_DOMINIO - (time.time() - desde)
        if falta > 0:
            time.sleep(falta)
    _ultimo_acceso[host] = time.time()


def texto_visible(pagina: str) -> str:
    """HTML -> texto plano aproximado (sin scripts, estilos ni etiquetas)."""
    pagina = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", pagina)
    pagina = re.sub(r"(?s)<[^>]+>", " ", pagina)
    pagina = html.unescape(pagina)   # &nbsp; &amp; &#43; ... -> caracteres reales
    return re.sub(r"\s+", " ", pagina)


def extraer_sitio(pagina: str) -> dict:
    """Saca telefonos, numeros de WhatsApp y correos de una pagina HTML.

    Devuelve {'tels': [{'bruto', 'evidencia_wa', 'etiqueta'}], 'wa': [digitos], 'mails': [...]}
    """
    tels: list[dict] = []
    for m in re.findall(r'href=["\']tel:([^"\']+)', pagina, re.I):
        tels.append({"bruto": unquote(m), "evidencia_wa": False, "etiqueta": False})
    tels.extend(buscar_telefonos(texto_visible(pagina)))

    wa_nums: list[str] = []
    for patron in WA_PATRONES:
        wa_nums.extend(patron.findall(pagina))

    # Los mailto: van primero: son los que el taller puso a proposito. El
    # orden se conserva porque se exporta el primero de la lista.
    crudos = re.findall(r'mailto:([^"\'?>\s]+)', pagina, re.I) + EMAIL_RE.findall(pagina)
    mails = []
    for m in crudos:
        m = unquote(m).strip().lower()
        if any(b in m for b in EMAIL_BASURA):
            continue
        if re.search(r"\.(png|jpe?g|gif|svg|webp)$", m):
            continue
        mails.append(m)

    return {
        "tels": tels,
        "wa": [re.sub(r"\D", "", w) for w in wa_nums],
        "mails": list(dict.fromkeys(mails))[:5],   # sin repetidos, en orden
    }


def enriquecer(url: str) -> dict:
    """Visita home + una pagina de contacto. Devuelve telefonos/emails/wa."""
    if urlparse(url).scheme not in ("http", "https"):
        url = "https://" + url
    resultado = {"tels": [], "wa": [], "mails": [], "bloqueado": False}
    if not permitido(url):
        resultado["bloqueado"] = True
        return resultado

    partes = urlparse(url)
    base = f"{partes.scheme}://{partes.netloc}"
    # La home y, si alcanza el cupo, las rutas de contacto mas comunes.
    paginas = [url] + [urljoin(base, r) for r in ("/contacto", "/contact", "/contactanos")]

    visitadas: set[str] = set()
    for pagina in paginas:
        if len(visitadas) >= MAX_PAGINAS:
            break
        if pagina in visitadas or not permitido(pagina):
            continue
        visitadas.add(pagina)
        esperar_dominio(pagina)
        contenido = descargar(pagina)
        if not contenido:
            continue
        datos = extraer_sitio(contenido)
        resultado["tels"].extend(datos["tels"])
        resultado["wa"].extend(datos["wa"])
        resultado["mails"].extend(datos["mails"])
    return resultado


def intentar_instagram(url: str) -> dict:
    """Best effort: el og:description publico a veces trae el telefono."""
    vacio = {"tels": [], "wa": [], "mails": []}
    m = re.search(r"instagram\.com/([A-Za-z0-9_.]+)", url or "")
    usuario = m.group(1) if m else None
    if not usuario or usuario in {"p", "reel", "explore"}:
        return vacio

    esperar_dominio("https://instagram.com")
    contenido = descargar(f"https://www.instagram.com/{usuario}/", timeout=6)
    if not contenido:
        return vacio
    og = re.search(r'property=["\']og:description["\']\s+content=["\']([^"\']+)', contenido)
    return extraer_sitio(og.group(1) if og else "")


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
-- Cache de lo encontrado en cada sitio web (clave: ver clave_cache).
-- La columna se llama "dominio" por historia: hoy puede incluir la ruta.
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


def clave_cache(url: str) -> str:
    """Clave de la tabla `sitios` para una URL.

    Antes era solo el dominio, y eso mezclaba fichas: todos los talleres cuya
    "web" es un perfil de Instagram o Facebook compartian la misma entrada
    (www.instagram.com), asi que el telefono del primero se le asignaba a los
    demas. Ahora se incluye la ruta. Una URL sin ruta sigue dando solo el
    dominio, asi que la cache vieja se sigue aprovechando.
    """
    partes = urlparse(url if "://" in url else "https://" + url)
    dominio = partes.netloc.lower()
    ruta = partes.path.rstrip("/")
    return dominio + ruta if ruta else dominio


def cache_sitio(con: sqlite3.Connection, clave: str, refrescar: bool) -> dict | None:
    """Datos ya guardados para ese sitio, o None si hay que visitarlo.
    Los sitios que no dieron nada (ok=0) se vuelven a visitar cada corrida."""
    if refrescar:
        return None
    fila = con.execute("SELECT * FROM sitios WHERE dominio=?", (clave,)).fetchone()
    if not fila or not fila["ok"]:
        return None
    return {
        "tels": json.loads(fila["telefonos"]),
        "wa": json.loads(fila["wa"]),
        "mails": json.loads(fila["emails"]),
        "bloqueado": False,
    }


def guardar_sitio(con: sqlite3.Connection, clave: str, url: str, datos: dict) -> None:
    con.execute(
        """INSERT INTO sitios(dominio, url, fetched_at, telefonos, emails, wa, ok)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(dominio) DO UPDATE SET
             url=excluded.url, fetched_at=excluded.fetched_at,
             telefonos=excluded.telefonos, emails=excluded.emails,
             wa=excluded.wa, ok=excluded.ok""",
        (
            clave, url, datetime.now().isoformat(timespec="seconds"),
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
    """Todas las filas de raw/*.csv, en orden de archivo."""
    filas: list[dict] = []
    for archivo in sorted(RAW.glob("*.csv")):
        # utf-8-sig: tolera el BOM si algun CSV paso por Excel.
        with archivo.open(encoding="utf-8-sig", errors="replace", newline="") as f:
            filas.extend(csv.DictReader(f))
    return filas


def en_caba(fila: dict) -> bool:
    """Primero por coordenadas; si no hay, por el texto de la direccion."""
    try:
        lat = float(fila.get("latitude") or "nan")
        lon = float(fila.get("longitude") or "nan")
        # Con NaN las comparaciones dan False y se pasa al texto.
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
    """True si nombre+categoria suenan a chapa y pintura y no a un rubro excluido."""
    texto = norm_texto(f"{fila.get('title','')} {fila.get('category','')}")
    if any(p in texto for p in EXCLUIR):
        return False
    return any(p in texto for p in OBJETIVO)


def _rango(c: dict) -> tuple[int, int]:
    """Para ordenar telefonos clasificados: mejor estado y, a igualdad, el de wa.me."""
    return (jerarquia(c["estado"]), 0 if c["fuente"] == "wa" else 1)


def elegir_telefonos(tels: list[dict], wa: list[str]) -> dict:
    """De todos los telefonos de una ficha, elige los mejores.

    Devuelve {'celular', 'fijo', 'alt', 'estado', 'evidencia'}: un numero
    principal (en 'celular' si es movil, si no en 'fijo') y el resto en 'alt'.
    """
    clasificados = []
    for t in tels:
        c = clasificar_tel(t["bruto"], t.get("evidencia_wa", False), t.get("etiqueta", False))
        if c:
            clasificados.append(c)
    for w in wa:
        c = clasificar_tel(w, evidencia_wa=True)
        if c:
            clasificados.append(c)

    # Una entrada por linea telefonica: +54 11 X y +54 9 11 X son el mismo
    # numero, se queda la version con mejor evidencia.
    unicos: dict[str, dict] = {}
    for c in clasificados:
        clave = clave_linea(c["e164"])
        previo = unicos.get(clave)
        if previo is None or _rango(c) < _rango(previo):
            unicos[clave] = c

    ordenados = sorted(unicos.values(), key=lambda c: (*_rango(c), c["e164"]))
    if not ordenados:
        return {"celular": "", "fijo": "", "alt": "", "estado": "", "evidencia": ""}

    moviles = [c for c in ordenados if c["estado"] in ("celular", "probable_celular")]
    principal = moviles[0] if moviles else ordenados[0]
    resto = [c["e164"] for c in ordenados if c is not principal]

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


def upsert(con: sqlite3.Connection, negocio: dict) -> None:
    """Inserta o actualiza por place_id. Un dato vacio nunca pisa uno lleno.

    Ojo: si corregiste un celular a mano en la base, la proxima corrida lo
    pisa con lo que encuentre el scraper (si encuentra algo).
    """
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


def candidatos_desde_crudos(crudos: list[dict]) -> list[dict]:
    """Filtra a CABA + rubro y deja una ficha por place_id."""
    hoy = datetime.now().date().isoformat()
    vistos: set[str] = set()
    candidatos: list[dict] = []
    for fila in crudos:
        place_id = (fila.get("place_id") or "").strip()
        if not place_id:
            # Sin place_id: se arma una clave estable con nombre + direccion.
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
            "fecha": hoy,
        })
    return candidatos


def datos_de_la_web(con: sqlite3.Connection, sitio_web: str, refrescar: bool) -> dict:
    """Lo que hay en la web del taller, de la cache o visitandola."""
    clave = clave_cache(sitio_web)
    datos = cache_sitio(con, clave, refrescar)
    if datos is None:
        datos = enriquecer(sitio_web)
        guardar_sitio(con, clave, sitio_web, datos)
    return datos


def procesar(limite: int | None, sin_enriquecimiento: bool, refrescar: bool) -> int:
    crudos = leer_crudos()
    print(f"Filas crudas: {len(crudos)}")

    candidatos = candidatos_desde_crudos(crudos)
    print(f"Tras filtro CABA + rubro: {len(candidatos)} talleres unicos")
    if limite:
        candidatos = candidatos[:limite]

    con = abrir_db()
    total = len(candidatos)
    for i, neg in enumerate(candidatos, 1):
        # El telefono de Google Maps siempre entra; la web suma mas candidatos.
        tels = [{"bruto": neg["telefono"], "evidencia_wa": False, "etiqueta": False}] if neg["telefono"] else []
        wa: list[str] = []
        mails: list[str] = []

        if neg["sitio_web"] and not sin_enriquecimiento:
            datos = datos_de_la_web(con, neg["sitio_web"], refrescar)
            tels.extend(datos["tels"])
            wa.extend(datos["wa"])
            mails.extend(datos["mails"])
            # Instagram bloquea por robots.txt; si no hay ningun dato, se
            # prueba con la descripcion publica del perfil.
            if not tels and not mails and re.search(r"instagram\.com/[A-Za-z0-9_.]+", neg["sitio_web"]):
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
            "correo": mails[0] if mails else neg["correo"],
        })
        upsert(con, neg)

        if not sin_enriquecimiento and (i % 10 == 0 or i == total):
            print(f"  enriquecidos {i}/{total}")
    con.commit()

    consolidar(con)
    exportar(con)
    con.close()
    return 0


def _numeros(fila: dict) -> list[str]:
    """Todos los telefonos de una ficha: celular, fijo y alternativos."""
    numeros = [fila[c] for c in ("celular", "telefono_fijo") if fila.get(c)]
    numeros += [t.strip() for t in (fila.get("telefono_alt") or "").split("|") if t.strip()]
    return numeros


def _marcado(valor) -> bool:
    """Interpreta no_llame igual que enviar.js: '', '0', 'no', 'false' = no marcado."""
    return str(valor or "").strip().lower() not in ("", "0", "no", "false")


def consolidar(con: sqlite3.Connection) -> int:
    """Q9-B: fusiona solo fichas que comparten un telefono identico.

    La ficha que sobrevive es la ya contactada o, si no, la de id mas bajo.
    La otra se borra. Como su place_id vuelve a aparecer en raw/, la proxima
    corrida la reinserta y se vuelve a fusionar: por eso todo lo de aca tiene
    que poder repetirse sin acumular cosas.
    """
    filas = [dict(f) for f in con.execute(
        "SELECT * FROM negocios ORDER BY contactado DESC, id"
    ).fetchall()]

    canonicos: dict[str, int] = {}   # clave_linea -> id de la ficha que se queda
    fusiones = 0

    for fila in filas:
        claves = [k for k in (clave_linea(n) for n in _numeros(fila)) if k]
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

        # Completar lo que le falta a la ficha que se queda.
        nuevos = {}
        for campo in ("celular", "telefono_fijo", "correo", "sitio_web",
                      "direccion", "categoria", "fuente_url"):
            if not base[campo] and fila[campo]:
                nuevos[campo] = fila[campo]

        if fila["estado_celular"] and (
            not base["estado_celular"]
            or jerarquia(fila["estado_celular"]) < jerarquia(base["estado_celular"])
        ):
            nuevos["estado_celular"] = fila["estado_celular"]
            nuevos["evidencia"] = fila["evidencia"] or base["evidencia"]

        # Al fusionar no se puede perder un "no llamar" (ley 26.951) ni un
        # "ya contactado": si cualquiera de las dos lo tenia, queda.
        if "no_llame" in fila and _marcado(fila["no_llame"]) and not _marcado(base["no_llame"]):
            nuevos["no_llame"] = fila["no_llame"]
        if fila["contactado"] and not base["contactado"]:
            nuevos["contactado"] = 1
            nuevos["fecha_contacto"] = fila["fecha_contacto"]

        # Alternativos: todo lo que no es el principal, una vez por linea.
        usados = {
            clave_linea(nuevos.get("celular") or base["celular"]),
            clave_linea(nuevos.get("telefono_fijo") or base["telefono_fijo"]),
        } - {""}
        alternativos: dict[str, str] = {}
        for n in _numeros(base) + _numeros(fila):
            k = clave_linea(n)
            if k and k not in usados:
                alternativos.setdefault(k, n)
        nuevos["telefono_alt"] = " | ".join(alternativos.values())

        if norm_texto(fila["nombre"]) != norm_texto(base["nombre"]):
            nota = f"tambien: {fila['nombre']}"
            previas = [n for n in (base["notas"] or "").split(" / ") if n]
            if nota not in previas:   # sin esto la nota se repetia en cada corrida
                nuevos["notas"] = " / ".join(previas + [nota])

        # Los nombres de columna salen de la lista fija de arriba, no de datos
        # externos, asi que armar el SET con f-string es seguro.
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


def exportar(con: sqlite3.Connection) -> Path:
    """Escribe contactos_<fecha>.csv con las fichas no contactadas."""
    todas = [dict(f) for f in con.execute("SELECT * FROM negocios ORDER BY id").fetchall()]
    filas = [f for f in todas if not f["contactado"]]

    # Si un numero fue confirmado como celular en cualquier ficha (incluso una
    # ya contactada), se propaga a las fichas que comparten esa misma linea.
    # Esto solo cambia el CSV, no la base.
    moviles = {clave_linea(f["celular"]) for f in todas if f["celular"]}
    for f in filas:
        if f["celular"]:
            continue
        for t in _numeros(f):
            if clave_linea(t) in moviles:
                f["celular"] = a_movil(t)
                f["estado_celular"] = "celular"
                f["evidencia"] = "celular confirmado en otra ficha"
                break

    # Q9bis-B: nombre+direccion exactos no se fusionan, solo se marcan.
    canonicos: dict[tuple[str, str], int] = {}
    for f in filas:
        clave = (norm_texto(f["nombre"]), norm_texto(f["direccion"]))
        f["posible_duplicado_de"] = canonicos.setdefault(clave, f["id"])
        if f["posible_duplicado_de"] == f["id"]:
            f["posible_duplicado_de"] = ""

    # Primero los celulares confirmados, despues por nombre.
    filas.sort(key=lambda f: (
        jerarquia(f["estado_celular"]),
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
    # utf-8-sig: Excel en Windows reconoce las tildes solo con BOM.
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
                # enviar.js agrega la columna; si ya marcaste alguno, se ve aca.
                "no_llame": fila.get("no_llame") or "",
                "posible_duplicado_de": fila["posible_duplicado_de"],
                "notas": fila["notas"] or "",
            })

    con_movil = sum(1 for f in filas if f["celular"])
    print(f"\nCSV: {salida.name}")
    print(f"  filas: {len(filas)}")
    print(f"  con celular: {con_movil} ({con_movil * 100 // max(len(filas), 1)}%)")
    print(f"  con correo: {sum(1 for f in filas if f['correo'])}")
    return salida


def main() -> int:
    limite = None
    if "--limite" in sys.argv:
        try:
            limite = int(sys.argv[sys.argv.index("--limite") + 1])
        except (IndexError, ValueError):
            limite = 0
        if limite <= 0:
            print("--limite necesita un numero mayor que 0")
            return 2
    return procesar(
        limite=limite,
        sin_enriquecimiento="--sin-enriquecimiento" in sys.argv,
        refrescar="--refrescar-sitios" in sys.argv,
    )


if __name__ == "__main__":
    raise SystemExit(main())
