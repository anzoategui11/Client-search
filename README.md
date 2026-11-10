# Buscador de chapistas — CABA

Sistema para relevar talleres de chapa y pintura en las 15 comunas de CABA y
entregar un CSV con `fecha de extracción · nombre · celular · sitio web · correo`.

```
python buscar.py      # 1) descubre talleres (Google Maps, tandas de 10)
python procesar.py    # 2) enriquece con la web de cada taller y exporta el CSV
python marcar.py 12 15    # 3) marca los que ya llamaste
```

## Requisitos

- Python 3.11+ (probado con 3.15, sin dependencias externas)
- Docker Desktop corriendo

## Cómo se usa

**1. Descubrimiento.** `python buscar.py` corre **una tanda de 10 búsquedas**,
hace una pausa de 30–90 s y te pide Enter para la siguiente. Cuando completa
**50 búsquedas (un lote)** se detiene y te dice que vuelvas a correrlo.

- `-c 1` fijo: una búsqueda por vez, ritmo humano.
- Ctrl+C en cualquier momento: el progreso queda en `data/progreso.json` y la
  próxima corrida retoma exactamente donde quedó.
- `python buscar.py --estado` muestra cuánto falta.

Son 240 búsquedas (48 barrios × 5 términos). A 50 por lote, son ~5 lotes.

**Rendimiento medido** (corrida real del 2026-11-09, 10 búsquedas = Agronomía +
Almagro): 3,8 min de scraping, **91 talleres únicos, 16 celulares, 11 correos**.
Extrapolado a las 240 búsquedas: ~90 min de scraping repartidos en 5 sesiones.

**2. Procesamiento.** `python procesar.py`:

1. lee `raw/*.csv`, recorta a CABA por coordenadas y filtra rubros que no son
   tu cliente (gomería, lavadero, concesionaria, repuestos, etc.);
2. visita la web de cada taller (home + una página de contacto, 1 req/s por
   dominio) buscando `tel:`, links `wa.me`, `mailto:` y teléfonos en el texto;
3. clasifica cada número y guarda todo en `data/contactos.db`;
4. exporta `contactos_AAAA-MM-DD_HHMM.csv` **sin los ya contactados**.

Opciones: `--limite 20` (prueba), `--sin-enriquecimiento`, `--refrescar-sitios`
(ignora la caché de dominios).

**3. Marcado.** El CSV trae `id` en la primera columna.
`python marcar.py 12 15 22` los saca de las próximas exportaciones.
`python marcar.py --estado` dice cuántos quedan pendientes.

## Columnas del CSV

| Columna | Qué es |
|---|---|
| `id` | identificador estable (para `marcar.py`) |
| `fecha_extraccion` | fecha de la corrida |
| `nombre_negocio` | nombre en Google Maps |
| `celular` | móvil en formato `+549...`, si se encontró |
| `sitio_web` | web del taller |
| `correo` | primer email encontrado en su web |
| `telefono_fijo` | teléfono del local cuando no hay celular |
| `telefono_alternativo` | otros números, separados por ` \| ` |
| `estado_celular` | `celular` / `probable_celular` / `ambiguo` |
| `evidencia` | por qué se marcó celular (ej. link `wa.me`) |
| `direccion`, `categoria`, `fuente_url` | contexto y trazabilidad |
| `no_llame` | vacía: la llenás vos antes de la campaña |
| `posible_duplicado_de` | id de otra fila con nombre+dirección idénticos |

## Cómo se decide que un número es celular

En CABA fijo y móvil tienen la misma cantidad de dígitos (11 + 8), así que el
largo no dice nada. Se usa, en orden:

1. formato con marcador: `+54 9 11 ...` o `0 11 15 ...` → **celular**;
2. `15 xxxx-xxxx` sin código de área → **probable_celular**;
3. un link `wa.me` / `api.whatsapp.com` en la web del taller, o la palabra
   "WhatsApp/celular" junto al número → **celular** (la evidencia manda);
4. todo lo demás (`11 xxxxxxxx`) → **ambiguo**.

Los números confirmados como celular en cualquier fuente se propagan a las
demás fichas que comparten ese número.

## Qué se rompe y cómo se arregla

| Síntoma | Arreglo |
|---|---|
| Una tanda falla o no genera CSV | no se marca como hecha; corré de nuevo y la reintenta |
| Google empieza a bloquear | esperá 30–60 min, bajá `CONCURRENCIA` a 1 (ya está), acortá `TANDA` |
| Menos de 100 talleres | sumar Cylex como segunda fuente en `procesar.py` |
| Menos de 50 celulares | normal a esta altura: hay talleres que solo publican fijo |
| Un sitio tarda o cuelga | timeout 8 s, máximo 2 páginas por dominio, se sigue |
| Un número quedó mal clasificado | corregilo en la base: `UPDATE negocios SET celular=... WHERE id=...` |

## Límites conocidos

- **Instagram**: sin sesión logueada solo se lee el `og:description` público.
  Muchos perfiles no exponen teléfono ahí. No se usa tu cuenta.
- **WhatsApp**: no se valida que un número tenga WhatsApp. `wa.me` devuelve la
  misma página para un número inexistente. Se usa el link publicado por el
  taller como evidencia de que **ese** número es su canal.
- **Google Maps** no es una API: el scraper puede ser bloqueado. Es el costo de
  operar con 0 USD.
- **Datos y ley 25.326**: al ser una base de contactos, corresponde inscribirla
  en el RNBD (gratis, con CUIT) si alguna vez deja de ser de uso exclusivamente
  personal. La columna `no_llame` existe para eso: la ley 26.951 sanciona cada
  contacto a un número inscripto.
