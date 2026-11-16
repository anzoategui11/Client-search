#!/usr/bin/env node
/**
 * Envio por WhatsApp de la campana de Giroscopio 3D.
 *
 *   node enviar.js --estado          # cupo, rampa, pendientes
 *   node enviar.js --simular --limite 5
 *   node enviar.js                   # corrida real
 *
 * Lee data/contactos.db (fuente de verdad), verifica cada numero contra el
 * servidor de WhatsApp, muestra el mensaje ya armado y espera Enter.
 * La sesion vive fuera del repo, en %LOCALAPPDATA%\giroscopio-wa\auth.
 */
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const http = require("node:http");
const crypto = require("node:crypto");
const readline = require("node:readline");
const { spawn, execSync } = require("node:child_process");
const { DatabaseSync } = require("node:sqlite");
const pino = require("pino");
const qrcode = require("qrcode");
const {
  default: makeWASocket,
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
  Browsers,
  DisconnectReason,
} = require("@whiskeysockets/baileys");

// --------------------------------------------------------------------------- #
// Constantes
// --------------------------------------------------------------------------- #

const RAIZ = __dirname;
const DB = path.join(RAIZ, "data", "contactos.db");
const ASSETS = path.join(RAIZ, "assets");
const PLANTILLA_POR_DEFECTO = path.join(ASSETS, "mensaje.txt");
const FOTO_POR_DEFECTO = path.join(ASSETS, "foto.jpg");
const AUTH = process.env.GIROSCOPIO_WA_AUTH
  ? path.resolve(process.env.GIROSCOPIO_WA_AUTH)
  : path.join(process.env.LOCALAPPDATA || os.homedir(), "giroscopio-wa", "auth");

const TOPE_DIA = 30; // techo de la rampa
const PAUSA_SEG = 60; // minimo entre envios
const JITTER_SEG = 15; // aleatorio que se suma
const ESPERA_HISTORIAL_MS = 15000;
const LARGO_NOMBRE = 40;

// --------------------------------------------------------------------------- #
// Argumentos
// --------------------------------------------------------------------------- #

function leerArgs(argv) {
  const opts = {
    estado: false,
    simular: false,
    limite: null,
    pasada: null,
    tope: TOPE_DIA,
    plantilla: PLANTILLA_POR_DEFECTO,
    foto: FOTO_POR_DEFECTO,
    reintentar: false,
    forzar: false,
    // Pedir el historial completo deja la sesion "sincronizando" y WhatsApp
    // acepta los envios sin entregarlos (probado en esta cuenta). Solo se
    // activa a mano, y no mientras se manda.
    historialCompleto: false,
    probar: null,
    sinValidar: false,
    numero: null,
    codigo: false,
    diagnostico: false,
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const valor = () => {
      const v = argv[++i];
      if (v === undefined) throw new Error(`Falta el valor de ${a}`);
      return v;
    };
    switch (a) {
      case "--estado": opts.estado = true; break;
      case "--simular": opts.simular = true; break;
      case "--limite": opts.limite = Number(valor()); break;
      case "--pasada": opts.pasada = Number(valor()); break;
      case "--tope": opts.tope = Number(valor()); break;
      case "--plantilla": opts.plantilla = path.resolve(valor()); break;
      case "--foto": opts.foto = path.resolve(valor()); break;
      case "--reintentar": opts.reintentar = true; break;
      case "--forzar": opts.forzar = true; break;
      case "--historial-completo": opts.historialCompleto = true; break;
      case "--probar": opts.probar = valor(); break;
      case "--sin-validar": opts.sinValidar = true; break;
      case "--numero": opts.numero = valor(); break;
      case "--codigo": opts.codigo = true; break;
      case "--diagnostico": opts.diagnostico = true; break;
      case "-h":
      case "--ayuda":
        imprimirAyuda();
        process.exit(0);
        break;
      default:
        throw new Error(`Opcion desconocida: ${a}`);
    }
  }
  if (opts.pasada !== null && ![1, 2].includes(opts.pasada)) {
    throw new Error("--pasada solo acepta 1 o 2");
  }
  if (opts.limite !== null && !(opts.limite > 0)) throw new Error("--limite tiene que ser > 0");
  if (!(opts.tope > 0)) throw new Error("--tope tiene que ser > 0");
  return opts;
}

function imprimirAyuda() {
  console.log(`Uso: node enviar.js [opciones]

  --estado            Resumen de la base y del cupo. No abre sesion.
  --simular           Recorre contactos sin mandar nada.
  --limite N          Corta despues de N mensajes.
  --pasada 1|2        Solo celulares (1) o solo fijos (2).
  --tope N            Pisa el techo diario de la rampa (default ${TOPE_DIA}).
  --plantilla ARCHIVO Plantilla del mensaje (default assets/mensaje.txt).
  --foto ARCHIVO      Foto adjunta (default assets/foto.jpg).
  --reintentar        Incluye numeros ya marcados como inexistentes.
  --forzar            No espera el historial de chats al conectar.
  --historial-completo
                      Pide todo el historial al conectar. WhatsApp acepta los
                      envios pero no los entrega: sirve solo para leer chats.
  --probar NUMERO     Manda el mensaje a ese numero y termina.
  --sin-validar       No exige que el numero de --probar sea de CABA.
  --numero NUMERO     Vincula con codigo de 8 caracteres para ese numero.
  --codigo            Igual que --numero, pero pide el numero al vuelo.
                      Sin --numero ni --codigo, vincula mostrando un QR.
  --diagnostico       Escribe wa-diag.log con todo el handshake.

En el preview: Enter envia, e edita el nombre, a edita el mensaje, s saltea, q sale.`);
}

// --------------------------------------------------------------------------- #
// Base de datos
// --------------------------------------------------------------------------- #

const ESQUEMA = `
CREATE TABLE IF NOT EXISTS envios(
    id INTEGER PRIMARY KEY,
    negocio_id INTEGER,
    numero TEXT,
    jid TEXT,
    resultado TEXT,
    motivo TEXT,
    fecha TEXT,
    msg_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_envios_numero ON envios(numero);
CREATE INDEX IF NOT EXISTS idx_envios_resultado ON envios(resultado);
CREATE TABLE IF NOT EXISTS wa_chats(
    jid TEXT PRIMARY KEY,
    nombre TEXT,
    ultimo_ts INTEGER,
    ultimo_texto TEXT,
    origen TEXT
);
`;

function abrirDb() {
  if (!fs.existsSync(DB)) {
    throw new Error(`No existe ${DB}. Corre primero: python procesar.py`);
  }
  const db = new DatabaseSync(DB);
  db.exec("PRAGMA busy_timeout=8000");
  db.exec(ESQUEMA);
  // El CSV exporta no_llame pero la base no la tenia. Se agrega sin tocar lo demas.
  const columnas = db.prepare("PRAGMA table_info(negocios)").all().map((c) => c.name);
  if (!columnas.includes("no_llame")) {
    db.exec("ALTER TABLE negocios ADD COLUMN no_llame TEXT DEFAULT ''");
  }
  return db;
}

function hoyIso() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/**
 * Agrega una fila a `envios`. resultado: enviado | no_existe |
 * conversacion_previa | duplicado | salteado | error | simulado.
 * Devuelve el id de la fila, para poder completarla despues (ver actualizarMotivo).
 */
function registrar(db, negocio, numero, jid, resultado, motivo, msgId) {
  const { lastInsertRowid } = db.prepare(
    "INSERT INTO envios(negocio_id, numero, jid, resultado, motivo, fecha, msg_id) VALUES(?,?,?,?,?,?,?)"
  ).run(
    negocio ? negocio.id : null,
    numero || null,
    jid || null,
    resultado,
    motivo || null,
    hoyIso(),
    msgId || null
  );
  return lastInsertRowid;
}

function actualizarMotivo(db, envioId, motivo) {
  db.prepare("UPDATE envios SET motivo=? WHERE id=?").run(motivo, envioId);
}

function marcarContactado(db, negocio) {
  db.prepare("UPDATE negocios SET contactado=1, fecha_contacto=? WHERE id=?").run(
    hoyIso().slice(0, 10),
    negocio.id
  );
}

function guardarChat(db, chat, origen) {
  const jid = chat.id || chat.jid;
  if (!jid || !jid.endsWith("@s.whatsapp.net")) return;
  const ts = tsNum(chat.conversationTimestamp);
  db.prepare(
    `INSERT INTO wa_chats(jid, nombre, ultimo_ts, ultimo_texto, origen)
     VALUES(?,?,?,?,?)
     ON CONFLICT(jid) DO UPDATE SET
       nombre=COALESCE(excluded.nombre, wa_chats.nombre),
       ultimo_ts=MAX(COALESCE(excluded.ultimo_ts,0), COALESCE(wa_chats.ultimo_ts,0)),
       ultimo_texto=COALESCE(excluded.ultimo_texto, wa_chats.ultimo_texto)`
  ).run(
    jid,
    chat.name || chat.verifiedName || null,
    ts,
    null,
    origen
  );
}

// Baileys entrega los timestamps como number, bigint o Long (protobuf).
// Devuelve segundos como number, o null si no se puede leer.
function tsNum(v) {
  if (v === null || v === undefined) return null;
  if (typeof v === "number") return Math.trunc(v);
  if (typeof v === "bigint") return Number(v);
  if (typeof v.toNumber === "function") return v.toNumber();
  if (typeof v.low === "number" && typeof v.high === "number") return v.high * 4294967296 + (v.low >>> 0);
  const n = Number(v);
  return Number.isFinite(n) ? Math.trunc(n) : null;
}

// --------------------------------------------------------------------------- #
// Numeros
// --------------------------------------------------------------------------- #

function soloDigitos(s) {
  return String(s || "").replace(/\D/g, "");
}

/**
 * Devuelve { num: '54911XXXXXXXX', ult8 } o null si no es un numero
 * de CABA reconocible. Acepta +54911..., +5411..., 011..., 11 15 4321 5678.
 */
function normalizar(bruto) {
  let d = soloDigitos(bruto);
  if (!d) return null;
  if (d.startsWith("0054")) d = d.slice(4);
  // Solo es codigo de pais si sobra largo: un 10 digitos que empieza con 54 no lo tiene.
  else if (d.startsWith("54") && d.length > 10) d = d.slice(2);
  if (d.startsWith("0")) d = d.slice(1); // 011... -> 11...
  if (d.length === 11 && d.startsWith("9")) d = d.slice(1); // 9 11 XXXX XXXX -> 11 XXXX XXXX
  // 11 15 XXXX XXXX -> 11 XXXX XXXX (el 15 es el prefijo local de celular)
  if (d.length === 12 && d.startsWith("11") && d.slice(2, 4) === "15") d = d.slice(0, 2) + d.slice(4);
  if (d.length !== 10 || !d.startsWith("11")) return null;
  // WhatsApp identifica los celulares argentinos como 549 + area + numero.
  return { num: "549" + d, ult8: d.slice(2) };
}

function candidatos(negocio) {
  const out = [];
  const agregar = (bruto, origen) => {
    const n = normalizar(bruto);
    if (!n) return;
    if (out.some((c) => c.num === n.num)) return;
    out.push({ num: n.num, ult8: n.ult8, origen });
  };
  agregar(negocio.celular, "celular");
  agregar(negocio.telefono_fijo, "fijo");
  for (const alt of String(negocio.telefono_alt || "").split("|")) {
    agregar(alt.trim(), "alternativo");
  }
  return out;
}

// --------------------------------------------------------------------------- #
// Nombre y mensaje
// --------------------------------------------------------------------------- #

function limpiarNombre(bruto) {
  let s = String(bruto || "").replace(/\s+/g, " ").trim();
  s = s.split(/\s+[-\u2013\u2014]\s+/)[0];
  s = s.replace(
    /\s*[|,]\s*(CABA|C\.?A\.?B\.?A\.?|capital federal|ciudad (aut[oó]noma )?de buenos aires|comuna \d+)\s*$/i,
    ""
  );
  s = s.replace(/[\s.,;:|]+$/, "").trim();
  if (s.length > LARGO_NOMBRE) {
    s = s.slice(0, LARGO_NOMBRE);
    const corte = s.lastIndexOf(" ");
    if (corte > 15) s = s.slice(0, corte);
    s = s.replace(/[\s.,;:|]+$/, "").trim();
  }
  return s;
}

function leerPlantilla(archivo) {
  if (!fs.existsSync(archivo)) {
    throw new Error(`No existe la plantilla ${archivo}. Creala con el texto del mensaje y {nombre}.`);
  }
  const txt = fs.readFileSync(archivo, "utf8").replace(/\r\n/g, "\n").trim();
  if (!txt) throw new Error(`La plantilla ${archivo} esta vacia.`);
  return txt;
}

function renderMensaje(plantilla, nombre) {
  const n = String(nombre || "").trim();
  if (n) return plantilla.split("{nombre}").join(n);
  return plantilla
    .replace(/Hola\s*\{nombre\}\s*,\s*/gi, "Hola, ")
    .split("{nombre}")
    .join("")
    .trim();
}

// --------------------------------------------------------------------------- #
// Terminal
// --------------------------------------------------------------------------- #

function prepararConsola() {
  if (process.platform === "win32" && process.stdout.isTTY) {
    try {
      execSync("chcp 65001 > nul", { stdio: "ignore" });
    } catch {
      /* sin consola extendida, se sigue igual */
    }
  }
  // libsignal imprime la sesion completa cada vez que renueva claves, justo
  // en medio del preview. Es ruido interno y parece un error.
  for (const nivel of ["info", "warn"]) {
    const original = console[nivel];
    console[nivel] = (...args) => {
      const primero = typeof args[0] === "string" ? args[0] : "";
      if (/^(Closing|Opening) session|^Session already (open|closed)/.test(primero)) return;
      original.apply(console, args);
    };
  }
}

function linea() {
  console.log("\u2500".repeat(62));
}

// Cuando la entrada es una tuberia (pruebas, scripts) se lee todo de una vez
// y se reparte por lineas: crear un readline por pregunta pierde lo que ya
// habia en el buffer.
let lineasEntrada = null;
function siguienteLinea() {
  if (!lineasEntrada) {
    lineasEntrada = new Promise((resolve) => {
      let datos = "";
      process.stdin.setEncoding("utf8");
      process.stdin.on("data", (c) => {
        datos += c;
      });
      process.stdin.on("end", () => {
        const ls = datos.split(/\r?\n/);
        if (ls.length && ls[ls.length - 1] === "") ls.pop();
        resolve(ls);
      });
    });
  }
  return lineasEntrada.then((ls) => (ls.length ? ls.shift() : ""));
}

function esperarTecla(permitidas) {
  return new Promise((resolve) => {
    if (!process.stdin.isTTY) {
      siguienteLinea().then((resp) => {
        const k = String(resp || "").trim().toLowerCase();
        resolve(k === "" ? "enter" : k.slice(0, 1));
      });
      return;
    }
    const emitir = readline.emitKeypressEvents;
    emitir(process.stdin);
    process.stdin.setRawMode(true);
    process.stdin.resume();
    const alPresionar = (str, key) => {
      if (key && key.ctrl && key.name === "c") {
        limpiar();
        resolve("q");
        return;
      }
      const k = key && key.name === "return" ? "enter" : String(str || "").toLowerCase();
      if (permitidas.includes(k)) {
        limpiar();
        resolve(k);
      }
    };
    function limpiar() {
      process.stdin.removeListener("keypress", alPresionar);
      process.stdin.setRawMode(false);
      process.stdin.pause();
    }
    process.stdin.on("keypress", alPresionar);
  });
}

// Edicion en la misma terminal: muestra el mensaje numerado y deja cambiar,
// agregar o borrar lineas sin abrir otro programa.
function leerLinea(pregunta, inicial = "") {
  if (!process.stdin.isTTY) {
    if (pregunta) process.stdout.write(pregunta);
    return siguienteLinea().then((v) => (String(v).trim() === "" ? inicial : String(v)));
  }
  return new Promise((resolve) => {
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
    rl.setPrompt(pregunta || "");
    rl.prompt();
    // Deja el valor actual escrito en la linea, listo para borrar o completar.
    if (inicial) rl.write(inicial);
    rl.once("line", (resp) => {
      rl.close();
      resolve(resp.trim() === "" ? inicial : resp);
    });
  });
}

async function editarNombre(nombre) {
  const nuevo = (await leerLinea("Nombre: ", nombre)).trim();
  return nuevo || nombre;
}

async function editar(texto) {
  const lineas = texto.split("\n");
  for (;;) {
    linea();
    lineas.forEach((l, i) => console.log(`${String(i + 1).padStart(2)} | ${l}`));
    linea();
    const resp = (
      await leerLinea("Linea a cambiar (Enter termina, + agrega, bN borra): ")
    )
      .trim()
      .toLowerCase();
    if (!resp) {
      const salida = lineas.join("\n").trim();
      return salida || texto;
    }
    if (resp === "+") {
      lineas.push(await leerLinea("Nueva linea: "));
      continue;
    }
    const borrar = /^b(\d+)$/.exec(resp);
    const n = borrar ? Number(borrar[1]) : Number(resp);
    if (!Number.isInteger(n) || n < 1 || n > lineas.length) {
      console.log("No existe esa linea.");
      continue;
    }
    if (borrar) {
      lineas.splice(n - 1, 1);
      continue;
    }
    const nuevo = await leerLinea(`Linea ${n}: `);
    if (nuevo !== "") lineas[n - 1] = nuevo;
  }
}

function dormir(segundos, motivo) {
  if (segundos < 1) {
    return new Promise((resolve) => setTimeout(resolve, segundos * 1000));
  }
  return new Promise((resolve) => {
    let resto = Math.ceil(segundos);
    const tic = () => {
      if (resto <= 0) return resolve();
      process.stdout.write(`\r${motivo} en ${resto} s...   `);
      resto -= 1;
      setTimeout(tic, 1000);
    };
    tic();
  }).then(() => process.stdout.write("\r" + " ".repeat(50) + "\r"));
}

// --------------------------------------------------------------------------- #
// QR de vinculacion (imagen en el navegador)
// --------------------------------------------------------------------------- #

let qrActual = null;
let servidorQr = null;
let qrVinculado = false;

function guardarQr(qr) {
  qrActual = qr;
  qrcode
    .toFile(path.join(os.tmpdir(), "wa_qr.png"), qr, { margin: 2, width: 420 })
    .catch(() => {});
  if (servidorQr) return;

  servidorQr = http.createServer(async (req, res) => {
    try {
      if (qrVinculado) {
        res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
        res.end(paginaListo());
        return;
      }
      if (!qrActual) {
        res.writeHead(503, { "content-type": "text/plain; charset=utf-8" });
        res.end("Esperando QR...");
        return;
      }
      const dataUrl = await qrcode.toDataURL(qrActual, { margin: 2, width: 460 });
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      res.end(paginaQr(dataUrl));
    } catch (e) {
      res.writeHead(500, { "content-type": "text/plain; charset=utf-8" });
      res.end(String(e && e.message ? e.message : e));
    }
  });

  servidorQr.listen(0, "127.0.0.1", () => {
    const url = `http://127.0.0.1:${servidorQr.address().port}/`;
    console.log(`\nAbriendo el QR en el navegador: ${url}`);
    console.log(`PNG de respaldo: ${path.join(os.tmpdir(), "wa_qr.png")}\n`);
    if (!process.env.GIROSCOPIO_WA_NO_ABRIR) {
      // spawn avisa un ejecutable faltante con un evento "error", no con una
      // excepcion: sin el listener, eso tiraba abajo todo el proceso.
      spawn("cmd", ["/c", "start", "", url], { stdio: "ignore", detached: true })
        .on("error", () => {
          /* si no abre, la URL queda impresa */
        })
        .unref();
    }
  });
  servidorQr.on("error", (e) => {
    console.log(`No pude abrir la pagina del QR (${e.message}). Usa el PNG: ${path.join(os.tmpdir(), "wa_qr.png")}`);
  });
}

function marcarVinculado() {
  qrVinculado = true;
  qrActual = null;
  if (servidorQr) setTimeout(cerrarQr, 15000);
}

function cerrarQr() {
  if (!servidorQr) return;
  try {
    servidorQr.close();
  } catch {
    /* ya cerrado */
  }
  servidorQr = null;
}

function paginaQr(dataUrl) {
  return `<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="2">
<title>Vincular WhatsApp</title>
<style>
  body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;
       justify-content:center;gap:14px;background:#f7f7f7;
       font-family:system-ui,-apple-system,"Segoe UI",sans-serif;color:#111}
  img{width:min(70vw,460px);background:#fff;padding:14px;border-radius:12px;
      box-shadow:0 4px 24px rgba(0,0,0,.12)}
  h1{font-size:20px;margin:0;font-weight:600}
  p{margin:0;color:#555;font-size:14px;max-width:520px;text-align:center;line-height:1.5}
</style></head>
<body>
  <h1>Escanea con WhatsApp</h1>
  <img src="${dataUrl}" alt="QR de vinculacion">
  <p>WhatsApp &gt; Dispositivos vinculados &gt; Vincular dispositivo</p>
  <p>La pagina se actualiza sola: cuando el QR se renueva, la imagen cambia.</p>
</body></html>`;
}

function paginaListo() {
  return `<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<title>WhatsApp vinculado</title>
<style>
  body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;
       justify-content:center;gap:12px;background:#f7f7f7;
       font-family:system-ui,-apple-system,"Segoe UI",sans-serif;color:#111}
  h1{font-size:24px;margin:0;font-weight:600}
  p{margin:0;color:#555;font-size:15px;text-align:center;max-width:460px;line-height:1.5}
</style></head>
<body>
  <h1>Listo, WhatsApp quedo vinculado</h1>
  <p>Podes cerrar esta pestana y volver a la terminal.</p>
</body></html>`;
}

// --------------------------------------------------------------------------- #
// WhatsApp
// --------------------------------------------------------------------------- #

// Una vinculacion que no llego a terminar deja creds con identidad y cuenta
// asignadas pero registered:false. Baileys entonces intenta loguear un
// dispositivo inexistente y WhatsApp contesta 401. Se descarta esa carpeta.
function limpiarSesionIncompleta() {
  const archivo = path.join(AUTH, "creds.json");
  if (!fs.existsSync(archivo)) return false;
  let creds = null;
  try {
    creds = JSON.parse(fs.readFileSync(archivo, "utf8"));
  } catch {
    creds = null;
  }
  if (creds && creds.registered) return false;
  // Baileys 7 no marca registered:true al vincular por QR, aunque la
  // vinculacion haya salido bien. Si hay identidad y cuenta asignadas, la
  // sesion sirve: se intenta login. Borrarla seria tirar un dispositivo ya
  // vinculado y WhatsApp contestaria 408/401 al reintentar registrar.
  if (creds && creds.me && creds.me.id && creds.account) return false;
  fs.rmSync(AUTH, { recursive: true, force: true });
  return true;
}

function diagnosticoAuth(dir) {
  try {
    const c = JSON.parse(fs.readFileSync(path.join(dir, "creds.json"), "utf8"));
    return { registrado: c.registered, me: c.me && c.me.id, account: Boolean(c.account) };
  } catch {
    return { registrado: false };
  }
}

// Log detallado del handshake, para diagnosticar vinculaciones que no cierran.
let diagIniciado = false;
function crearDiag(activo) {
  if (!activo) return () => {};
  const archivo = path.join(__dirname, "wa-diag.log");
  if (!diagIniciado) {
    diagIniciado = true;
    try {
      fs.writeFileSync(archivo, `=== ${new Date().toISOString()} ===\n`);
    } catch {
      /* sin permisos: se ignora */
    }
  }
  return (dato) => {
    const linea = `${new Date().toISOString()} ${JSON.stringify(dato)}\n`;
    try {
      fs.appendFileSync(archivo, linea);
    } catch {
      /* sin permisos: se ignora */
    }
  };
}

async function conectar(opts, db, estado) {
  const diag = crearDiag(opts.diagnostico);
  diag({ evento: "conectar", auth: AUTH, registrado: diagnosticoAuth(AUTH) });
  if (limpiarSesionIncompleta()) {
    console.log("Sesion a medio vincular detectada: la descarto y empiezo de cero.");
    diag({ evento: "sesion-descartada" });
  }
  fs.mkdirSync(AUTH, { recursive: true });
  const { state, saveCreds } = await useMultiFileAuthState(AUTH);
  let version;
  try {
    ({ version } = await fetchLatestBaileysVersion());
  } catch {
    version = undefined;
  }

  const pedirHistorialCompleto =
    Boolean(opts.historialCompleto) && state.creds.registered && !estado.sinHistorialCompleto;

  const logger = opts.diagnostico
    ? pino(
        { level: "debug" },
        pino.destination({ dest: path.join(__dirname, "wa-diag.log"), sync: true })
      )
    : pino({ level: "silent" });

  const sock = makeWASocket({
    version,
    auth: state,
    // Identidad coherente: un navegador normal anuncia webSubPlatform
    // WEB_BROWSER. Con "Mac OS/Desktop" + historial completo apagado,
    // WhatsApp recibe un cliente que dice ser Desktop y a la vez navegador,
    // y el emparejamiento se cuelga despues del escaneo.
    browser: Browsers.ubuntu("Chrome"),
    // Con true, WhatsApp acepta los envios y no los entrega (probado en esta
    // cuenta), y ademas puede cortar el handshake con 428. Por eso va apagado
    // salvo que se pida con --historial-completo.
    syncFullHistory: pedirHistorialCompleto,
    markOnlineOnConnect: false,
    logger,
  });

  // Guardado en cola: saveCreds escribe el archivo completo, y dos escrituras
  // concurrentes podrian dejar en disco una version vieja (sin registered).
  let colaCreds = Promise.resolve();
  const guardarCreds = () => {
    colaCreds = colaCreds.then(() => saveCreds()).catch((e) => {
      diag({ evento: "error-guardando-creds", error: e && e.message });
    });
    return colaCreds;
  };
  // Para que el final del programa espere a que termine la ultima escritura
  // (process.exit a mitad de saveCreds puede dejar creds.json cortado).
  estado.esperarCreds = () => colaCreds;
  sock.ev.on("creds.update", (update) => {
    // El flujo por QR configura el dispositivo pero no marca registered:true
    // (el flujo por codigo si, en messages-recv.js). Lo marcamos aca para que
    // las proximas corridas lo traten como sesion vinculada.
    if (!state.creds.registered && update && update.account && update.me && update.me.id) {
      state.creds.registered = true;
      diag({ evento: "registered-forzado", me: update.me.id });
    }
    guardarCreds();
  });
  sock.ev.on("creds.update", () => {
    diag({
      evento: "creds",
      registrado: state.creds.registered,
      me: state.creds.me && state.creds.me.id,
      account: Boolean(state.creds.account),
    });
  });
  if (opts.diagnostico) {
    try {
      sock.ws.on("CB:notification", (nodo) => {
        const attrs = (nodo && nodo.attrs) || {};
        diag({
          evento: "notificacion",
          tipo: attrs.type,
          id: attrs.id,
          hijos: ((nodo && nodo.children) || []).map((c) => c && c.tag),
        });
      });
    } catch {
      /* el socket no expone ws: se ignora */
    }
  }

  sock.ev.on("chats.upsert", (chats) => {
    for (const c of chats) guardarChat(db, c, "upsert");
  });

  sock.ev.on("messaging-history.set", (payload) => {
    const chats = payload.chats || [];
    db.exec("BEGIN");
    try {
      for (const c of chats) guardarChat(db, c, "sync");
      db.exec("COMMIT");
    } catch {
      db.exec("ROLLBACK");
    }
    const mensajes = payload.messages || [];
    if (mensajes.length) {
      const ultimo = new Map();
      for (const m of mensajes) {
        const jid = m.key && m.key.remoteJid;
        if (!jid || !jid.endsWith("@s.whatsapp.net")) continue;
        const ts = tsNum(m.messageTimestamp) || 0;
        const prev = ultimo.get(jid);
        if (!prev || ts >= prev.ts) {
          const body =
            (m.message && m.message.conversation) ||
            (m.message && m.message.extendedTextMessage && m.message.extendedTextMessage.text) ||
            (m.message && m.message.imageMessage && (m.message.imageMessage.caption || "[foto]")) ||
            null;
          ultimo.set(jid, { ts, texto: body ? String(body).replace(/\s+/g, " ").slice(0, 90) : null });
        }
      }
      for (const [jid, v] of ultimo) {
        db.prepare(
          "UPDATE wa_chats SET ultimo_ts=COALESCE(?,ultimo_ts), ultimo_texto=COALESCE(?,ultimo_texto) WHERE jid=?"
        ).run(v.ts, v.texto, jid);
      }
    }
    estado.historialLlego = true;
  });

  // Cualquier mensaje que pase mientras corre el script (uno que llega, o uno
  // que mandaste desde el telefono) cuenta como conversacion previa.
  sock.ev.on("messages.upsert", ({ messages }) => {
    for (const m of messages) {
      const jid = m.key && m.key.remoteJid;
      if (!jid || !jid.endsWith("@s.whatsapp.net")) continue;
      const previo = db.prepare("SELECT jid FROM wa_chats WHERE jid=?").get(jid);
      if (!previo) {
        db.prepare("INSERT INTO wa_chats(jid, nombre, ultimo_ts, origen) VALUES(?,?,?,?)").run(
          jid,
          m.key.fromMe ? null : m.pushName || null,
          tsNum(m.messageTimestamp),
          m.key.fromMe ? "saliente" : "entrante"
        );
      }
    }
  });

  // Se resuelve con el socket cuando la conexion queda abierta. Ante un corte
  // se llama a conectar() de nuevo (socket nuevo) y su resultado se encadena aca.
  const abierto = new Promise((resolve, reject) => {
    const arranque = Date.now();
    const reloj = setTimeout(() => {
      reject(new Error("No se completo la vinculacion en 5 minutos. Volve a intentar."));
    }, 300000);
    const listo = (s) => {
      clearTimeout(reloj);
      resolve(s);
    };
    const fallo = (e) => {
      clearTimeout(reloj);
      cerrarQr();
      reject(e);
    };
    sock.ev.on("connection.update", async (update) => {
      const { connection, lastDisconnect, qr } = update;
      diag({ evento: "update", connection: connection || null, qr: Boolean(qr) });
      if (qr && !state.creds.registered && !opts.numero && !opts.codigo) {
        diag({ evento: "qr-nuevo", hash: crypto.createHash("sha256").update(qr).digest("hex").slice(0, 10) });
        guardarQr(qr);
      }
      if (connection === "open") {
        estado.abierto = true;
        // La corrida usa siempre estado.sock: si la conexion se cae a mitad
        // de camino y se reconecta, sigue con el socket nuevo.
        estado.sock = sock;
        estado.conectado = true;
        estado.reintentos = 0;
        marcarVinculado();
        listo(sock);
        return;
      }
      if (connection === "close") {
        if (estado.sock === sock) estado.conectado = false;
        const codigo = lastDisconnect && lastDisconnect.error && lastDisconnect.error.output
          ? lastDisconnect.error.output.statusCode
          : null;
        const motivo = (lastDisconnect && lastDisconnect.error && lastDisconnect.error.message) || "";
        const segundos = Math.round((Date.now() - arranque) / 1000);
        diag({
          evento: "close",
          codigo,
          motivo,
          segundos,
          registrado: state.creds.registered,
          me: state.creds.me && state.creds.me.id,
        });
        if (codigo === DisconnectReason.loggedOut) {
          // 401 antes de estar vinculado: la sesion en disco no le sirve a
          // WhatsApp (dispositivo eliminado o vinculacion a medias). Se
          // descarta una vez y se vuelve a empezar con QR limpio.
          if (!state.creds.registered && !estado.reintentoLimpio) {
            estado.reintentoLimpio = true;
            fs.rmSync(AUTH, { recursive: true, force: true });
            console.log("\nWhatsApp no reconoce esta sesion (401). La borro y arranco de cero con un QR nuevo.");
            setTimeout(() => conectar(opts, db, estado).then(listo, fallo), 1000);
            return;
          }
          fallo(
            new Error(
              state.creds.registered
                ? "WhatsApp cerro la sesion de este dispositivo. Volve a vincular."
                : `WhatsApp rechazo la vinculacion (401 ${motivo}) a los ${segundos} s. ` +
                  "Revisa que el numero tenga WhatsApp y que no haya 4 dispositivos ya vinculados."
            )
          );
          return;
        }
        if (codigo === DisconnectReason.restartRequired) {
          // 515 = reiniciar la conexion. Es lo normal justo despues de
          // vincular: los creds ya quedaron configurados y hay que volver a
          // abrir el socket. No es un error y no se descarta la sesion.
          estado.reinicios515 = (estado.reinicios515 || 0) + 1;
          if (estado.reinicios515 > 5) {
            fallo(new Error("WhatsApp pide reiniciar la conexion en bucle (515). Proba de nuevo en unos minutos."));
            return;
          }
          await guardarCreds();
          console.log("\nWhatsApp pidio reiniciar la conexion (515): es lo normal al terminar de vincular.");
          console.log(`  sesion: ${state.creds.me ? state.creds.me.id : "sin identidad"}`);
          setTimeout(() => conectar(opts, db, estado).then(listo, fallo), 500);
          return;
        }
        if (!state.creds.registered && codigo === DisconnectReason.timedOut) {
          estado.reintentos408 = (estado.reintentos408 || 0) + 1;
          if (estado.reintentos408 <= 3) {
            console.log("\nEl QR vencio sin completar la vinculacion. Genero uno nuevo...");
            setTimeout(() => conectar(opts, db, estado).then(listo, fallo), 1000);
            return;
          }
        }
        if (!state.creds.registered) {
          fallo(new Error(`WhatsApp cerro la conexion durante la vinculacion (codigo ${codigo}).`));
          return;
        }
        if (codigo === DisconnectReason.connectionClosed && pedirHistorialCompleto && !estado.abierto) {
          estado.sinHistorialCompleto = true;
          console.log("\nWhatsApp corto la conexion al pedir el historial completo. Reintento sin eso.");
        }
        // El contador vive en `estado` porque cada reintento es una llamada
        // nueva a conectar(). Como variable local se reiniciaba en cada
        // llamada y el limite de 3 nunca se alcanzaba (reconexion infinita).
        estado.reintentos = (estado.reintentos || 0) + 1;
        if (estado.reintentos > 3) {
          fallo(new Error("No se pudo reconectar con WhatsApp despues de 3 intentos."));
          return;
        }
        console.log(`\nConexion caida (${codigo}). Reintentando ${estado.reintentos}/3...`);
        setTimeout(() => conectar(opts, db, estado).then(listo, fallo), 2000 * estado.reintentos);
      }
    });
  });

  // Evita unhandledRejection si el socket falla mientras se pide el codigo.
  abierto.catch(() => {});

  if (!state.creds.registered && (opts.numero || opts.codigo)) {
    const numero = (opts.numero || (await pedirNumero())).replace(/\D/g, "");
    if (numero.length < 10) throw new Error("Numero invalido para vincular. Formato: 54911XXXXXXXX");
    const codigo = await pedirCodigo(sock, numero);
    console.log("\nVinculando WhatsApp");
    linea();
    console.log(`En el telefono de +${numero}:`);
    console.log(`  Dispositivos vinculados > Vincular dispositivo > "Vincular con numero"`);
    console.log(`  y escribi:  ${codigo}`);
    linea();
    console.log("El codigo vence en menos de un minuto. Si falla, usa el QR (sin --numero).");
  } else if (!state.creds.registered) {
    console.log("\nVinculando con QR: se abre el navegador con la imagen.");
    console.log("  WhatsApp > Dispositivos vinculados > Vincular dispositivo");
    console.log("  La pagina se refresca sola; escanea el QR que veas en pantalla.\n");
  }

  const sockListo = await abierto;
  if (!opts.forzar && !estado.historialLlego) {
    console.log(`\nEsperando historial de chats (hasta ${ESPERA_HISTORIAL_MS / 1000} s)...`);
    await esperarHistorial(estado);
  }
  return sockListo;
}

/**
 * El socket vivo. Si la conexion se cayo y se esta reconectando, espera hasta
 * `ms` a que vuelva; si no vuelve, corta con un error claro en vez de seguir
 * mandando por un socket muerto.
 */
async function socketActivo(estado, ms = 60000) {
  const limite = Date.now() + ms;
  while (!estado.conectado) {
    if (Date.now() > limite) throw new Error("Se perdio la conexion con WhatsApp y no volvio.");
    await new Promise((r) => setTimeout(r, 500));
  }
  return estado.sock;
}

function esperarHistorial(estado) {
  return new Promise((resolve) => {
    const limite = Date.now() + ESPERA_HISTORIAL_MS;
    const tic = () => {
      if (estado.historialLlego || Date.now() >= limite) return resolve();
      setTimeout(tic, 500);
    };
    tic();
  });
}

function pedirNumero() {
  if (!process.stdin.isTTY) {
    throw new Error("Falta --numero 54911XXXXXXXX para vincular el dispositivo.");
  }
  return new Promise((resolve) => {
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
    rl.question("Numero de WhatsApp a vincular (54911XXXXXXXX): ", (resp) => {
      rl.close();
      resolve(resp.trim());
    });
  });
}

/**
 * requestPairingCode necesita el websocket abierto; si todavia esta
 * conectando, Baileys tira "Connection Closed". Se reintenta unos segundos.
 */
async function pedirCodigo(sock, numero) {
  const limite = Date.now() + 30000;
  for (;;) {
    try {
      return await sock.requestPairingCode(numero);
    } catch (e) {
      const cerrado = /connection closed/i.test(String(e.message || ""));
      if (!cerrado || Date.now() > limite) throw e;
      console.log("La conexion todavia esta abriendo, reintentando el codigo...");
      await new Promise((r) => setTimeout(r, 800));
    }
  }
}

// --------------------------------------------------------------------------- #
// Cupo y rampa
// --------------------------------------------------------------------------- #

/**
 * Cuantos mensajes quedan hoy. La rampa depende de cuantos dias ANTERIORES
 * hubo envios: 0 dias -> 5, 1 dia -> 10, 2 o mas -> TOPE_DIA.
 *
 * Antes se contaba tambien el dia de hoy, asi que despues del primer envio
 * del primer dia el techo pasaba de 5 a 10 y una segunda corrida ese mismo
 * dia mandaba de mas (paso el 2026-11-16: 6 envios con techo de 5).
 */
function cupo(db, tope) {
  const hoy = hoyIso().slice(0, 10);
  const dias = db
    .prepare(
      "SELECT COUNT(DISTINCT substr(fecha,1,10)) AS n FROM envios WHERE resultado='enviado' AND substr(fecha,1,10) < ?"
    )
    .get(hoy).n;
  const techoRampa = dias === 0 ? 5 : dias === 1 ? 10 : TOPE_DIA;
  const techo = Math.min(tope, techoRampa);
  const enviadosHoy = db
    .prepare("SELECT COUNT(*) AS n FROM envios WHERE resultado='enviado' AND substr(fecha,1,10)=?")
    .get(hoy).n;
  return { dias, techo, enviadosHoy, quedan: Math.max(0, techo - enviadosHoy) };
}

/**
 * numero -> resultado mas importante que tuvo en `envios`.
 *
 * Un numero puede tener varias filas (simulado, error, enviado...). Antes se
 * tomaba la primera, asi que un "simulado" o un "error" viejo tapaba un
 * "enviado" posterior y el numero podia volver a recibir el mensaje.
 * Ahora "enviado" siempre gana, despues "no_existe".
 */
function numerosVistos(db, reintentar) {
  const prioridad = { enviado: 2, no_existe: 1 };
  const filas = db.prepare("SELECT numero, resultado FROM envios WHERE numero IS NOT NULL").all();
  const mapa = new Map();
  for (const f of filas) {
    const previo = mapa.get(f.numero);
    if (previo === undefined || (prioridad[f.resultado] || 0) > (prioridad[previo] || 0)) {
      mapa.set(f.numero, f.resultado);
    }
  }
  // --reintentar: los "no_existe" se vuelven a consultar contra WhatsApp.
  if (reintentar) {
    for (const [num, res] of mapa) if (res === "no_existe") mapa.delete(num);
  }
  return mapa;
}

// --------------------------------------------------------------------------- #
// Contactos
// --------------------------------------------------------------------------- #

// no_llame se carga a mano: vacio, '0', 'no' o 'false' quieren decir que se puede escribir.
// procesar.py (_marcado) usa el mismo criterio.
const SE_PUEDE_LLAMAR = "COALESCE(TRIM(no_llame),'') IN ('','0','no','false')";

function contarSinNumero(db) {
  return db
    .prepare(
      `SELECT COUNT(*) AS n FROM negocios
       WHERE COALESCE(TRIM(celular),'')='' AND COALESCE(TRIM(telefono_fijo),'')=''
         AND COALESCE(TRIM(telefono_alt),'')=''`
    )
    .get().n;
}

function contarNoLlamar(db) {
  return db
    .prepare(
      `SELECT COUNT(*) AS n FROM negocios
       WHERE NOT ${SE_PUEDE_LLAMAR}`
    )
    .get().n;
}

function pendientes(db) {
  return db
    .prepare(
      `SELECT COUNT(*) AS n FROM negocios
       WHERE contactado=0
         AND ${SE_PUEDE_LLAMAR}
         AND (COALESCE(TRIM(celular),'')<>'' OR COALESCE(TRIM(telefono_fijo),'')<>''
              OR COALESCE(TRIM(telefono_alt),'')<>'')`
    )
    .get().n;
}

function listarContactos(db, pasada) {
  const base = `
    SELECT * FROM negocios
    WHERE contactado=0
      AND ${SE_PUEDE_LLAMAR}`;
  const filtro =
    pasada === 1
      ? " AND COALESCE(TRIM(celular),'')<>''"
      : pasada === 2
        ? " AND COALESCE(TRIM(celular),'')='' AND COALESCE(TRIM(telefono_fijo),'')<>''"
        : "";
  const orden = " ORDER BY CASE WHEN COALESCE(TRIM(celular),'')<>'' THEN 0 ELSE 1 END, id";
  return db.prepare(base + filtro + orden).all();
}

// --------------------------------------------------------------------------- #
// Estado
// --------------------------------------------------------------------------- #

function imprimirEstado(db, opts) {
  const total = db.prepare("SELECT COUNT(*) AS n FROM negocios").get().n;
  const c = cupo(db, opts.tope);
  const filas = db.prepare("SELECT resultado, COUNT(*) AS n FROM envios GROUP BY resultado").all();
  const porResultado = Object.fromEntries(filas.map((f) => [f.resultado, f.n]));
  const contactados = db.prepare("SELECT COUNT(*) AS n FROM negocios WHERE contactado=1").get().n;
  const conCel = db.prepare("SELECT COUNT(*) AS n FROM negocios WHERE COALESCE(TRIM(celular),'')<>''").get().n;
  const soloFijo = db
    .prepare(
      "SELECT COUNT(*) AS n FROM negocios WHERE COALESCE(TRIM(celular),'')='' AND COALESCE(TRIM(telefono_fijo),'')<>''"
    )
    .get().n;
  const chatsConocidos = db.prepare("SELECT COUNT(*) AS n FROM wa_chats").get().n;

  linea();
  console.log(`Base        ${path.relative(RAIZ, DB)}`);
  console.log(`Talleres    ${total} · contactados ${contactados} · pendientes ${pendientes(db)}`);
  console.log(`Cupo hoy    ${c.enviadosHoy}/${c.techo}  (rampa: dia ${c.dias + 1})`);
  linea();
  console.log(`Pasada 1    ${conCel} con celular`);
  console.log(`Pasada 2    ${soloFijo} solo con fijo`);
  console.log(`Sin numero  ${contarSinNumero(db)}`);
  console.log(`No llamar   ${contarNoLlamar(db)}`);
  console.log(`Chats sync  ${chatsConocidos} en wa_chats`);
  linea();
  const orden = ["enviado", "no_existe", "conversacion_previa", "duplicado", "salteado", "error", "simulado"];
  for (const k of orden) {
    if (porResultado[k]) console.log(`${k.padEnd(20)} ${porResultado[k]}`);
  }
  const previos = db
    .prepare(
      `SELECT jid, nombre, ultimo_texto, ultimo_ts FROM wa_chats
       WHERE jid LIKE '%@s.whatsapp.net'
       ORDER BY COALESCE(ultimo_ts,0) DESC LIMIT 8`
    )
    .all();
  if (previos.length) {
    console.log("");
    console.log("Ultimos chats conocidos:");
    for (const p of previos) {
      const quien = p.nombre || "+" + p.jid.split("@")[0];
      console.log(`  ${quien}${p.ultimo_texto ? "  · " + p.ultimo_texto : ""}`);
    }
  }
  linea();
}

// --------------------------------------------------------------------------- #
// Corrida
// --------------------------------------------------------------------------- #

function imprimirPreview(estado, negocio, candidato, mensaje, nombre) {
  linea();
  console.log(`[${estado.hechos + 1}/${estado.objetivo}]  id ${negocio.id} · ${nombre || "(sin nombre)"}`);
  const calle = calleDe(negocio.direccion);
  if (calle) console.log(calle.slice(0, 60));
  console.log(`+${candidato.num}   ${candidato.origen} · existe en WhatsApp · sin chat previo`);
  linea();
  console.log(mensaje);
  linea();
  console.log("Enter enviar   ·   e nombre   ·   a mensaje   ·   s saltear   ·   q salir");
}

function calleDe(direccion) {
  const bruto = String(direccion || "").trim();
  if (!bruto) return "";
  if (bruto.startsWith("{")) {
    try {
      const d = JSON.parse(bruto);
      return [d.street, d.borough].filter(Boolean).join(" · ");
    } catch {
      return "";
    }
  }
  return bruto;
}

// Aviso sonoro cuando hay un mensaje listo para revisar. En Windows el BEL
// de la terminal suele estar apagado, asi que se usa el beep del sistema.
function avisarListo() {
  if (process.platform === "win32") {
    spawn("powershell", ["-NoProfile", "-Command", "[console]::beep(880,200)"], {
      stdio: "ignore",
      windowsHide: true,
      detached: true,
    })
      .on("error", () => process.stdout.write("\x07")) // sin PowerShell: queda el BEL
      .unref();
    return;
  }
  process.stdout.write("\x07");
}

async function revisar(estado, negocio, candidato, plantilla, nombreInicial) {
  let nombre = nombreInicial;
  let texto = renderMensaje(plantilla, nombre);
  avisarListo();
  for (;;) {
    imprimirPreview(estado, negocio, candidato, texto, nombre);
    const tecla = await esperarTecla(["enter", "e", "a", "s", "q"]);
    if (tecla === "enter") return { accion: "enviar", texto, nombre };
    if (tecla === "e") {
      nombre = await editarNombre(nombre);
      texto = renderMensaje(plantilla, nombre);
      continue;
    }
    if (tecla === "a") {
      texto = await editar(texto);
      continue;
    }
    if (tecla === "s") return { accion: "saltear" };
    return { accion: "salir" };
  }
}

async function correr(opts) {
  prepararConsola();
  const db = abrirDb();
  if (opts.estado) {
    imprimirEstado(db, opts);
    return;
  }

  const faltaFoto = !fs.existsSync(opts.foto);
  if (faltaFoto && !opts.simular) {
    throw new Error(`No existe la foto ${opts.foto}. Pone tu imagen ahi o pasa --foto ARCHIVO.`);
  }
  const plantilla = leerPlantilla(opts.plantilla);
  if (!plantilla.includes("{nombre}")) {
    console.log("Aviso: la plantilla no tiene {nombre}; el saludo va a salir sin nombre.");
  }
  const foto = faltaFoto ? null : fs.readFileSync(opts.foto);

  // Estado compartido de la corrida. Lo leen y escriben correr(), conectar()
  // (y sus reconexiones) y los handlers de eventos de Baileys.
  const estado = {
    // contadores para el resumen
    enviados: 0,
    hechos: 0, // enviados + simulados: lo que cuenta contra `objetivo`
    salteados: 0,
    previos: 0,
    inexistentes: 0,
    duplicados: 0,
    erroresSeguidos: 0, // dos seguidos cortan la corrida
    objetivo: 0, // tope de esta corrida (cupo del dia o --limite)
    // conexion
    sock: null, // socket vivo; cambia si hay reconexion
    conectado: false,
    abierto: false, // se llego a abrir alguna vez
    reintentos: 0, // cortes seguidos (se reinicia al abrir)
    historialLlego: false,
    sinHistorialCompleto: false,
    esperarCreds: null, // promesa de la ultima escritura de creds
  };

  if (opts.probar) {
    const sock = await conectar(opts, db, estado);
    try {
      await probar(sock, db, opts, plantilla, foto, opts.probar);
    } finally {
      await cerrar(estado.sock || sock, estado);
    }
    return;
  }

  const c = cupo(db, opts.tope);
  if (c.quedan <= 0) {
    console.log(`\nCupo de hoy agotado (${c.enviadosHoy}/${c.techo}). Volve manana.`);
    return;
  }
  const contactos = listarContactos(db, opts.pasada);
  if (!contactos.length) {
    console.log("\nNo hay contactos pendientes. Corre python buscar.py y python procesar.py.");
    return;
  }
  estado.objetivo = opts.limite ? Math.min(opts.limite, c.quedan) : c.quedan;
  console.log(`Cupo de hoy: ${c.enviadosHoy}/${c.techo} (rampa dia ${c.dias + 1}).`);

  const sock = await conectar(opts, db, estado);
  try {
    await recorrerContactos(db, opts, estado, contactos, plantilla, foto);
  } finally {
    // Tambien ante un error: cierra prolijo y deja las credenciales escritas.
    await cerrar(estado.sock || sock, estado);
  }
  imprimirResumen(db, estado, opts);
}

/** Recorre la lista de contactos: verifica, muestra, envia y registra. */
async function recorrerContactos(db, opts, estado, contactos, plantilla, foto) {
  if (!estado.historialLlego) {
    console.log("\nAviso: no llego el historial de chats. No puedo saber con quien ya hablaste.");
    console.log("       Si ya usabas este WhatsApp, revisa a mano antes de seguir.\n");
  } else {
    console.log(`Historial: ${db.prepare("SELECT COUNT(*) AS n FROM wa_chats").get().n} chats conocidos.`);
  }

  const vistos = numerosVistos(db, opts.reintentar);
  const intentados = new Set();

  console.log(`Contactos a revisar: ${contactos.length}. Tope de esta corrida: ${estado.objetivo}.\n`);

  for (const negocio of contactos) {
    if (estado.hechos >= estado.objetivo) break;
    const opciones = candidatos(negocio);
    if (!opciones.length) {
      estado.salteados += 1;
      continue;
    }

    // Se prueban los numeros del taller en orden (celular, fijo, alternativos)
    // hasta que uno "resuelve" la ficha: enviado, salteado o con chat previo.
    // Los que no tienen WhatsApp o ya se usaron pasan al siguiente.
    let resuelto = false;
    for (const cand of opciones) {
      if (resuelto) break;
      if (intentados.has(cand.num)) {
        estado.duplicados += 1;
        continue;
      }
      intentados.add(cand.num);

      const yaVisto = vistos.get(cand.num);
      if (yaVisto === "enviado") {
        estado.duplicados += 1;
        registrar(db, negocio, cand.num, null, "duplicado", "ya enviado antes");
        continue;
      }
      if (yaVisto === "no_existe") {
        estado.inexistentes += 1;
        continue;
      }

      const jid = cand.num + "@s.whatsapp.net";
      let existe = false;
      try {
        const [info] = (await (await socketActivo(estado)).onWhatsApp(jid)) || [];
        existe = Boolean(info && info.exists);
      } catch (e) {
        estado.erroresSeguidos += 1;
        registrar(db, negocio, cand.num, jid, "error", String(e.message || e).slice(0, 200));
        if (estado.erroresSeguidos >= 2) {
          throw new Error(`Dos errores tecnicos seguidos. Ultimo: ${e.message || e}`);
        }
        continue;
      }

      if (!existe) {
        estado.inexistentes += 1;
        registrar(db, negocio, cand.num, jid, "no_existe", null);
        console.log(`  +${cand.num} (${cand.origen}) no tiene WhatsApp.`);
        await dormir(0.4 + Math.random() * 0.6, "siguiente");
        continue;
      }

      const chatPrevio = db.prepare("SELECT jid FROM wa_chats WHERE jid=?").get(jid);
      if (chatPrevio) {
        estado.previos += 1;
        registrar(db, negocio, cand.num, jid, "conversacion_previa", "ya habia chat");
        marcarContactado(db, negocio);
        console.log(`  ${limpiarNombre(negocio.nombre)}: ya habia conversacion con +${cand.num}. Salteado.`);
        resuelto = true;
        break;
      }

      const decision = await revisar(
        estado,
        negocio,
        cand,
        plantilla,
        limpiarNombre(negocio.nombre)
      );
      if (decision.accion === "salir") {
        console.log("\nCorrida interrumpida.");
        return; // correr() cierra el socket e imprime el resumen
      }
      if (decision.accion === "saltear") {
        estado.salteados += 1;
        registrar(db, negocio, cand.num, jid, "salteado", "manual");
        resuelto = true;
        break;
      }

      if (opts.simular) {
        console.log("  [simulado] no se envio nada.");
        registrar(db, negocio, cand.num, jid, "simulado", null);
        estado.hechos += 1;
      } else {
        try {
          const activo = await socketActivo(estado);
          const enviado = await activo.sendMessage(jid, { image: foto, caption: decision.texto });
          const id = enviado && enviado.key ? enviado.key.id : null;
          // Se registra YA, antes de esperar el acuse: si cortas con Ctrl+C
          // durante la espera, el envio igual queda anotado y la proxima
          // corrida no le vuelve a escribir.
          const envioId = registrar(db, negocio, cand.num, jid, "enviado", "esperando acuse", id);
          marcarContactado(db, negocio);
          db.prepare(
            "INSERT INTO wa_chats(jid, nombre, ultimo_ts, origen) VALUES(?,?,?, 'envio') ON CONFLICT(jid) DO UPDATE SET ultimo_ts=excluded.ultimo_ts"
          ).run(jid, decision.nombre || null, Math.floor(Date.now() / 1000));
          vistos.set(cand.num, "enviado");
          estado.enviados += 1;
          estado.hechos += 1;
          estado.erroresSeguidos = 0;
          // Cerrar el socket apenas se manda puede cortar el reintento que pide
          // el telefono si no pudo descifrar. Esperamos el acuse de entrega.
          const acuse = id ? await esperarAcuse(activo, id) : "sin acuse";
          actualizarMotivo(db, envioId, acuse);
          console.log(`  enviado a +${cand.num} · ${decision.nombre} (${acuse})`);
        } catch (e) {
          estado.erroresSeguidos += 1;
          registrar(db, negocio, cand.num, jid, "error", String(e.message || e).slice(0, 200));
          console.log(`  error enviando a +${cand.num}: ${e.message || e}`);
          if (estado.erroresSeguidos >= 2) {
            throw new Error(`Dos errores tecnicos seguidos. Ultimo: ${e.message || e}`);
          }
        }
      }
      resuelto = true;

      if (!opts.simular && estado.hechos < estado.objetivo) {
        await dormir(PAUSA_SEG + Math.random() * JITTER_SEG, "Proximo envio");
      }
    }
  }
}

async function probar(sock, db, opts, plantilla, foto, numero) {
  const n = opts.sinValidar
    ? { num: soloDigitos(numero) }
    : normalizar(numero);
  if (!n || !n.num || n.num.length < 8) {
    throw new Error(
      "--probar necesita un numero valido. Si no es de CABA, sumá --sin-validar."
    );
  }
  const jid = n.num + "@s.whatsapp.net";
  const [info] = (await sock.onWhatsApp(jid)) || [];
  if (!info || !info.exists) console.log(`Aviso: ${n.num} no figura en WhatsApp.`);
  const decision = await revisar(
    { hechos: 0, objetivo: 1 },
    { id: 0, nombre: "prueba", direccion: "" },
    { num: n.num, origen: "probando" },
    plantilla,
    "David (prueba)"
  );
  if (decision.accion !== "enviar") return;
  if (opts.simular) {
    console.log("  [simulado] no se envio nada.");
    return;
  }
  const enviado = await sock.sendMessage(jid, { image: foto, caption: decision.texto });
  const id = enviado && enviado.key ? enviado.key.id : null;
  const acuse = id ? await esperarAcuse(sock, id) : "sin acuse";
  console.log(`  enviado. id ${id || "?"} (${acuse})`);
}

// Espera el acuse de entrega o de lectura. Si el destinatario esta
// desconectado no hay acuse: se sigue igual cuando vence el plazo.
function esperarAcuse(sock, id, ms = 15000) {
  return new Promise((resolve) => {
    let listo = false;
    const terminar = (valor) => {
      if (listo) return;
      listo = true;
      clearTimeout(reloj);
      sock.ev.off("messages.update", mirarEstado);
      sock.ev.off("message-receipt.update", mirarRecibo);
      resolve(valor);
    };
    const mirarEstado = (lista) => {
      for (const u of lista) {
        if (!u.key || u.key.id !== id) continue;
        const estado = u.update && u.update.status;
        if (estado === 0) return terminar("error del servidor");
        if (estado >= 4) return terminar("leido");
        if (estado === 3) return terminar("entregado");
      }
    };
    const mirarRecibo = (lista) => {
      for (const r of lista) {
        if (!r.key || r.key.id !== id) continue;
        const recibo = r.receipt || {};
        if (recibo.readTimestamp || recibo.playedTimestamp) return terminar("leido");
        if (recibo.receiptTimestamp) return terminar("entregado");
      }
    };
    const reloj = setTimeout(() => terminar("sin acuse"), ms);
    sock.ev.on("messages.update", mirarEstado);
    sock.ev.on("message-receipt.update", mirarRecibo);
  });
}

async function cerrar(sock, estado) {
  try {
    // Un respiro antes de cortar: da tiempo a que salga el ultimo reintento.
    await new Promise((r) => setTimeout(r, 2000));
    sock.end(undefined);
  } catch {
    /* ya estaba cerrado */
  }
  // Despues viene process.exit: hay que dejar terminar la escritura de creds.
  if (estado && estado.esperarCreds) await estado.esperarCreds();
}

function imprimirResumen(db, estado, opts) {
  linea();
  console.log(opts.simular ? "Simulacion terminada" : "Corrida terminada");
  console.log(`  enviados            ${estado.enviados}`);
  console.log(`  no tiene WhatsApp   ${estado.inexistentes}`);
  console.log(`  chat previo         ${estado.previos}`);
  console.log(`  duplicados          ${estado.duplicados}`);
  console.log(`  salteados           ${estado.salteados}`);
  const c = cupo(db, opts.tope);
  console.log(`  cupo                ${c.enviadosHoy}/${c.techo} (rampa dia ${c.dias + 1})`);
  linea();
}

// --------------------------------------------------------------------------- #

if (require.main === module) {
  (async () => {
    let opts;
    try {
      opts = leerArgs(process.argv.slice(2));
      await correr(opts);
      process.exit(0);
    } catch (e) {
      console.error(`\n${e.message || e}`);
      process.exit(1);
    }
  })();
}

module.exports = {
  normalizar,
  candidatos,
  limpiarNombre,
  renderMensaje,
  leerArgs,
  limpiarSesionIncompleta,
  cupo,
  numerosVistos,
};
