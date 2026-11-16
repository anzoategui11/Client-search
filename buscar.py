"""Fase 0 - Descubrimiento de talleres en Google Maps.

Corre una tanda de N busquedas con el scraper no oficial (Docker), espera
un Enter, y sigue con la proxima tanda hasta completar el lote.

    python buscar.py            # corre la siguiente tanda / lote
    python buscar.py --generar  # solo genera queries/queries.txt
    python buscar.py --estado   # muestra progreso

El progreso vive en data/progreso.json, asi que cortar con Ctrl+C no
pierde nada: la proxima corrida retoma exactamente donde quedo.
"""

from __future__ import annotations

import csv
import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RAIZ = Path(__file__).resolve().parent
QUERIES_DIR = RAIZ / "queries"
QUERIES = QUERIES_DIR / "queries.txt"
TANDAS_DIR = QUERIES_DIR / "tandas"
RAW = RAIZ / "raw"
DATA = RAIZ / "data"
PROGRESO = DATA / "progreso.json"

# --- perillas ---------------------------------------------------------------
LOTE = 50           # busquedas por sesion de trabajo
TANDA = 10          # busquedas por tanda, con pausa manual entre tandas
CONCURRENCIA = 1    # -c del scraper: una pestania, ritmo humano
DEPTH = 1           # -depth del scraper: cuanto scrollea la lista de resultados
IDIOMA = "es"
PAUSA_MIN = 30      # la pausa entre tandas es aleatoria, entre PAUSA_MIN
PAUSA_RANGO = 60    # y PAUSA_MIN + PAUSA_RANGO segundos
IMAGEN = "gosom/google-maps-scraper"
CACHE_VOL = "gmaps-playwright-cache"   # volumen Docker con el navegador ya bajado
# ----------------------------------------------------------------------------

TERMINOS = [
    "taller de chapa y pintura",
    "chapista",
    "carroceria",
    "pintura automotor",
    "taller de chapa",
]

BARRIOS = [
    "Agronomia", "Almagro", "Balvanera", "Barracas", "Belgrano", "Boedo",
    "Caballito", "Chacarita", "Coghlan", "Colegiales", "Constitucion",
    "Flores", "Floresta", "La Boca", "La Paternal", "Liniers", "Mataderos",
    "Monte Castro", "Montserrat", "Nueva Pompeya", "Nunez", "Palermo",
    "Parque Avellaneda", "Parque Chacabuco", "Parque Chas",
    "Parque Patricios", "Puerto Madero", "Recoleta", "Retiro", "Saavedra",
    "San Cristobal", "San Nicolas", "San Telmo", "Velez Sarsfield",
    "Versalles", "Villa Crespo", "Villa del Parque", "Villa Devoto",
    "Villa General Mitre", "Villa Lugano", "Villa Luro", "Villa Ortuzar",
    "Villa Pueyrredon", "Villa Real", "Villa Riachuelo",
    "Villa Santa Rita", "Villa Soldati", "Villa Urquiza",
]


def generar_consultas() -> list[str]:
    """Genera queries.txt (barrio x termino) si no existe y lo devuelve."""
    QUERIES_DIR.mkdir(parents=True, exist_ok=True)
    if not QUERIES.exists():
        lineas = [
            f"{termino} en {barrio}, CABA"
            for barrio in BARRIOS
            for termino in TERMINOS
        ]
        QUERIES.write_text("\n".join(lineas) + "\n", encoding="utf-8")
        print(f"Generadas {len(lineas)} busquedas en {QUERIES}")
    return [
        linea.strip()
        for linea in QUERIES.read_text(encoding="utf-8").splitlines()
        if linea.strip()
    ]


def cargar_progreso() -> dict:
    """Lee data/progreso.json: {'hechas': [consultas], 'tandas': N}."""
    DATA.mkdir(parents=True, exist_ok=True)
    if not PROGRESO.exists():
        return {"hechas": [], "tandas": 0}
    try:
        progreso = json.loads(PROGRESO.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # No se arranca de cero en silencio: eso repetiria busquedas ya hechas.
        print(f"{PROGRESO} esta danado. Revisalo a mano (o borralo para empezar de cero).")
        raise SystemExit(1)
    progreso.setdefault("hechas", [])
    progreso.setdefault("tandas", 0)
    return progreso


def guardar_progreso(progreso: dict) -> None:
    """Escribe a un temporal y lo renombra: un Ctrl+C a mitad de la escritura
    no puede dejar progreso.json cortado."""
    temporal = PROGRESO.with_suffix(".json.tmp")
    temporal.write_text(
        json.dumps(progreso, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporal, PROGRESO)


def estado(consultas: list[str], progreso: dict) -> None:
    """Imprime cuanto se busco y en que lote va."""
    hechas = len(progreso["hechas"])
    total = len(consultas)
    lote = hechas // LOTE + 1
    en_lote = hechas % LOTE
    print(f"Consultas: {hechas}/{total} hechas")
    print(f"Lote {lote}: {en_lote}/{LOTE} ({en_lote // TANDA} tandas completas)")
    print(f"Tandas ejecutadas: {progreso['tandas']}")


def contar_filas(archivo: Path) -> int:
    """Filas de datos de un CSV. Contar saltos de linea no sirve: las celdas
    con resenias o direcciones traen saltos de linea adentro."""
    csv.field_size_limit(min(sys.maxsize, 2**31 - 1))   # celdas JSON grandes
    with archivo.open(encoding="utf-8", errors="replace", newline="") as f:
        return sum(1 for _ in csv.DictReader(f))


def correr_tanda(tanda: list[str], numero: int) -> bool:
    """Corre una tanda de busquedas en Docker. Devuelve True si salio bien."""
    TANDAS_DIR.mkdir(parents=True, exist_ok=True)
    entrada = TANDAS_DIR / f"tanda_{numero:04d}.txt"
    entrada.write_text("\n".join(tanda) + "\n", encoding="utf-8")
    salida = RAW / f"tanda_{numero:04d}.csv"
    RAW.mkdir(parents=True, exist_ok=True)
    # Una tanda fallida no avanza el numero, asi que el reintento usa el mismo
    # nombre de archivo. Se borra lo viejo para no confundirlo con el resultado.
    salida.unlink(missing_ok=True)

    # El contenedor ve queries/ en /queries (solo lectura) y raw/ en /out.
    # Docker Desktop acepta rutas de Windows con barras normales (as_posix).

    comando = [
        "docker", "run", "--rm",
        "-v", f"{CACHE_VOL}:/opt",
        "-v", f"{QUERIES_DIR.as_posix()}:/queries:ro",
        "-v", f"{RAW.as_posix()}:/out",
        IMAGEN,
        "-input", f"/queries/tandas/{entrada.name}",
        "-results", f"/out/{salida.name}",
        "-depth", str(DEPTH),
        "-lang", IDIOMA,
        "-c", str(CONCURRENCIA),
        "-exit-on-inactivity", "2m",
    ]

    entorno = dict(os.environ, DISABLE_TELEMETRY="1")
    print(f"\n>> tanda {numero} ({len(tanda)} busquedas), esto puede tardar unos minutos...")
    inicio = time.time()
    resultado = subprocess.run(comando, env=entorno)
    minutos = (time.time() - inicio) / 60

    # Un codigo != 0 con CSV generado se acepta: el scraper puede salir con
    # error al cortar por inactividad despues de haber escrito todo.
    if not salida.exists():
        if resultado.returncode != 0:
            print(f"!! Docker salio con codigo {resultado.returncode}")
        print(f"!! No se genero {salida.name}. Se reintenta en la proxima corrida.")
        return False

    filas = contar_filas(salida)
    if filas == 0:
        print(f"!! {salida.name} vino vacio (posible bloqueo). No se marca la tanda.")
        return False

    print(f">> {salida.name}: {filas} fichas en {minutos:.1f} min")
    return True


def main() -> int:
    if "--generar" in sys.argv:
        generar_consultas()
        return 0

    consultas = generar_consultas()
    progreso = cargar_progreso()

    if "--estado" in sys.argv:
        estado(consultas, progreso)
        return 0

    if not shutil.which("docker"):
        print("Docker no esta en el PATH. Abrilo y volve a intentar.")
        return 1

    hechas = set(progreso["hechas"])
    pendientes = [q for q in consultas if q not in hechas]

    if not pendientes:
        print("No queda nada por buscar. Todas las consultas estan hechas.")
        return 0

    hechas_en_lote = len(hechas) % LOTE
    if hechas_en_lote == 0 and len(hechas) > 0:
        print("\nLote anterior completo. Arrancando lote nuevo.")

    while pendientes:
        tanda = pendientes[:TANDA]
        numero = progreso["tandas"] + 1

        if not correr_tanda(tanda, numero):
            print("\nTanda fallida: no se marca como hecha. Revisa Docker y volve a correr.")
            print("Si el problema es bloqueo de Google, espera un rato antes de reintentar.")
            return 1

        lote_antes = len(progreso["hechas"]) // LOTE
        progreso["tandas"] = numero
        progreso["hechas"].extend(tanda)
        guardar_progreso(progreso)
        pendientes = pendientes[TANDA:]

        print(f"\n[ {len(progreso['hechas'])}/{len(consultas)} busquedas ]")
        if not pendientes:
            break  # era la ultima tanda: sin pausa y con el mensaje final

        pausa = random.randint(PAUSA_MIN, PAUSA_MIN + PAUSA_RANGO)
        print(f"Pausa de {pausa}s antes de liberar la tanda...")
        time.sleep(pausa)

        # Se mira si se cruzo de un lote al siguiente. Con "hechas % LOTE == 0"
        # el corte nunca llegaba si TANDA no divide a LOTE o la cuenta venia
        # desfasada (por ejemplo, despues de editar queries.txt).
        if len(progreso["hechas"]) // LOTE > lote_antes:
            print(f"\nLote completo ({LOTE} busquedas).")
            print("Volve a correr `python buscar.py` para el lote siguiente,")
            print("o `python procesar.py` para armar el CSV con lo que ya hay.")
            return 0

        try:
            entrada_usuario = input(
                "Enter para la proxima tanda / Ctrl+C para cortar: "
            )
        except EOFError:
            print("\nSin entrada: corto aca. El progreso quedo guardado.")
            return 0
        if entrada_usuario.strip().lower() in {"q", "salir", "exit"}:
            return 0

    print("\nTodas las consultas estan hechas.")
    print("Ahora: python procesar.py")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCortado. El progreso quedo guardado.")
        raise SystemExit(130)
