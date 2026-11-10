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

import json
import os
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
CONCURRENCIA = 1    # -c del scraper
DEPTH = 1
IDIOMA = "es"
PAUSA_MIN = 30      # segundos de jitter antes de cerrar cada tanda
IMAGEN = "gosom/google-maps-scraper"
CACHE_VOL = "gmaps-playwright-cache"
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
    DATA.mkdir(parents=True, exist_ok=True)
    if PROGRESO.exists():
        return json.loads(PROGRESO.read_text(encoding="utf-8"))
    return {"hechas": [], "tandas": 0}


def guardar_progreso(progreso: dict) -> None:
    PROGRESO.write_text(
        json.dumps(progreso, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def estado(consultas: list[str], progreso: dict) -> None:
    hechas = len(progreso["hechas"])
    total = len(consultas)
    lote = hechas // LOTE + 1
    en_lote = hechas % LOTE
    print(f"Consultas: {hechas}/{total} hechas")
    print(f"Lote {lote}: {en_lote}/{LOTE} ({en_lote // TANDA} tandas completas)")
    print(f"Tandas ejecutadas: {progreso['tandas']}")


def correr_tanda(tanda: list[str], numero: int) -> bool:
    """Corre una tanda de busquedas en Docker. Devuelve True si salio bien."""
    TANDAS_DIR.mkdir(parents=True, exist_ok=True)
    entrada = TANDAS_DIR / f"tanda_{numero:04d}.txt"
    entrada.write_text("\n".join(tanda) + "\n", encoding="utf-8")
    salida = RAW / f"tanda_{numero:04d}.csv"
    RAW.mkdir(parents=True, exist_ok=True)

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

    if resultado.returncode != 0 and not salida.exists():
        print(f"!! Docker salio con codigo {resultado.returncode}")
        return False

    if not salida.exists():
        print(f"!! No se genero {salida.name}. Se reintenta en la proxima corrida.")
        return False

    filas = max(salida.read_text(encoding="utf-8", errors="replace").count("\n") - 1, 0)
    if filas == 0:
        print(f"!! {salida.name} vino vacio (posible bloqueo). No se marca la tanda.")
        return False

    print(f">> {salida.name}: ~{filas} fichas en {minutos:.1f} min")
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

        progreso["tandas"] = numero
        progreso["hechas"].extend(tanda)
        guardar_progreso(progreso)
        pendientes = pendientes[TANDA:]

        print(f"\n[ {len(progreso['hechas'])}/{len(consultas)} busquedas ]")
        pausa = PAUSA_MIN + (numero * 37) % 60
        print(f"Pausa de {pausa}s antes de liberar la tanda...")
        time.sleep(pausa)

        if len(progreso["hechas"]) % LOTE == 0:
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
