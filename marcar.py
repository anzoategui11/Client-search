"""Marca talleres como contactados para que no vuelvan a salir en el CSV.

    python marcar.py 12 15 22      # marcar contactados
    python marcar.py --desmarcar 15
    python marcar.py --estado      # cuantos quedan pendientes

El id es la primera columna del CSV.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB = Path(__file__).resolve().parent / "data" / "contactos.db"


def abrir() -> sqlite3.Connection:
    if not DB.exists():
        print("Todavia no hay base. Corre primero: python procesar.py")
        raise SystemExit(1)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def main() -> int:
    argumentos = [a for a in sys.argv[1:] if not a.startswith("--")]

    con = abrir()

    if "--estado" in sys.argv:
        pagados = con.execute("SELECT COUNT(*) FROM negocios WHERE contactado=0").fetchone()[0]
        hechos = con.execute("SELECT COUNT(*) FROM negocios WHERE contactado=1").fetchone()[0]
        print(f"Pendientes: {pagados}")
        print(f"Contactados: {hechos}")
        return 0

    if not argumentos:
        print(__doc__)
        return 2

    try:
        ids = [int(a) for a in argumentos]
    except ValueError:
        print("Los ids tienen que ser numeros. Ej: python marcar.py 12 15")
        return 2

    marca = 0 if "--desmarcar" in sys.argv else 1
    fecha = None if marca == 0 else datetime.now().date().isoformat()

    cambiados = 0
    for i in ids:
        cur = con.execute(
            "UPDATE negocios SET contactado=?, fecha_contacto=? WHERE id=?",
            (marca, fecha, i),
        )
        if cur.rowcount == 0:
            existe = con.execute("SELECT COUNT(*) FROM negocios WHERE id=?", (i,)).fetchone()[0]
            print(f"  id {i}: no existe en la base" if not existe else f"  id {i}: sin cambios")
        else:
            cambiados += 1

    con.commit()
    accion = "contactados" if marca else "vueltos a pendiente"
    print(f"{cambiados} talleres {accion}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
