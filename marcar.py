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


OPCIONES = {"--estado", "--desmarcar"}


def abrir() -> sqlite3.Connection:
    if not DB.exists():
        print("Todavia no hay base. Corre primero: python procesar.py")
        raise SystemExit(1)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def main() -> int:
    opciones = {a for a in sys.argv[1:] if a.startswith("--")}
    argumentos = [a for a in sys.argv[1:] if not a.startswith("--")]

    desconocidas = opciones - OPCIONES
    if desconocidas:
        # Un "--desmarcr 15" mal escrito terminaba marcando el 15 como contactado.
        print(f"Opcion desconocida: {', '.join(sorted(desconocidas))}")
        print(__doc__)
        return 2

    con = abrir()
    try:
        if "--estado" in opciones:
            pendientes = con.execute("SELECT COUNT(*) FROM negocios WHERE contactado=0").fetchone()[0]
            contactados = con.execute("SELECT COUNT(*) FROM negocios WHERE contactado=1").fetchone()[0]
            print(f"Pendientes: {pendientes}")
            print(f"Contactados: {contactados}")
            return 0

        if not argumentos:
            print(__doc__)
            return 2

        try:
            ids = list(dict.fromkeys(int(a) for a in argumentos))   # sin repetidos, en orden
        except ValueError:
            print("Los ids tienen que ser numeros. Ej: python marcar.py 12 15")
            return 2

        marca = 0 if "--desmarcar" in opciones else 1
        fecha = None if marca == 0 else datetime.now().date().isoformat()

        cambiados = 0
        for i in ids:
            # "AND contactado<>?" evita pisar la fecha de contacto original
            # cuando se vuelve a marcar uno que ya estaba marcado. (SQLite
            # cuenta como modificada una fila aunque el valor no cambie.)
            cur = con.execute(
                "UPDATE negocios SET contactado=?, fecha_contacto=? WHERE id=? AND contactado<>?",
                (marca, fecha, i, marca),
            )
            if cur.rowcount:
                cambiados += 1
                continue
            existe = con.execute("SELECT 1 FROM negocios WHERE id=?", (i,)).fetchone()
            if not existe:
                print(f"  id {i}: no existe en la base")
            else:
                print(f"  id {i}: ya estaba {'contactado' if marca else 'pendiente'}")

        con.commit()
        accion = "contactados" if marca else "vueltos a pendiente"
        print(f"{cambiados} talleres {accion}.")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
