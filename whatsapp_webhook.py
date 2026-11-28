"""Webhook de WhatsApp Cloud API (Meta) para el asistente de la maestría.

Variables de entorno necesarias:
  WHATSAPP_TOKEN      token de acceso de Meta
  PHONE_NUMBER_ID     id del número de WhatsApp (Meta -> WhatsApp -> API Setup)
  VERIFY_TOKEN        texto que tú inventas; se repite en Meta al configurar el webhook
  APP_SECRET          (recomendado) "Clave secreta de la app" en Configuración -> Básica
  GEMINI_API_KEY      igual que en tu app de Streamlit
Opcionales: GRAPH_VERSION (por defecto v21.0), SESION_MINUTOS, MAX_PREGUNTAS_SESION, etc.

Arranque local:  uvicorn whatsapp_webhook:app --reload --port 8000
Arranque Render: uvicorn whatsapp_webhook:app --host 0.0.0.0 --port $PORT
"""
import os
import re
import hmac
import time
import hashlib
import logging
import threading

import httpx
from fastapi import FastAPI, Request, Response, BackgroundTasks, HTTPException

import asistente_core as core

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("whatsapp")

WHATSAPP_TOKEN = (os.getenv("WHATSAPP_TOKEN") or "").strip()
PHONE_NUMBER_ID = (os.getenv("PHONE_NUMBER_ID") or "").strip()
VERIFY_TOKEN = (os.getenv("VERIFY_TOKEN") or "").strip()
APP_SECRET = (os.getenv("APP_SECRET") or "").strip()
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v21.0")
SESION_MINUTOS = float(os.getenv("SESION_MINUTOS", "30"))   # en WhatsApp conviene más tiempo que en la web
MAX_INTENTOS = 5
SEGUNDOS_BLOQUEO = 60

URL_ENVIO = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/messages"

app = FastAPI(title="Asistente Maestría - WhatsApp")

# Estado por número de teléfono (en memoria: se pierde si Render reinicia el servicio)
SESIONES = {}
_lock = threading.Lock()
_vistos = {}  # ids de mensajes ya procesados (Meta a veces reenvía)


# ------------------------------ Envío a WhatsApp ---------------------------
def _post(payload):
    try:
        r = httpx.post(
            URL_ENVIO,
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
            json={"messaging_product": "whatsapp", **payload},
            timeout=15,
        )
        if r.status_code >= 400:
            log.error("Error al enviar (%s): %s", r.status_code, r.text[:500])
    except Exception:
        log.exception("Fallo de red al enviar a WhatsApp")


def _a_formato_whatsapp(texto):
    """WhatsApp usa *negritas*, no **negritas**; y no entiende encabezados #."""
    texto = re.sub(r"\*\*(.+?)\*\*", r"*\1*", texto)
    texto = re.sub(r"^#{1,6}\s*", "", texto, flags=re.MULTILINE)
    return texto


def enviar_texto(to, texto):
    texto = _a_formato_whatsapp(texto)
    # Límite de WhatsApp: 4096 caracteres por mensaje
    while texto:
        trozo, texto = texto[:4000], texto[4000:]
        _post({"to": to, "type": "text", "text": {"body": trozo, "preview_url": False}})


def enviar_menu(to, encabezado=None):
    cuerpo = (encabezado + "\n\n" if encabezado else "") + \
        "Asistente Virtual - Maestría 🎓\n¿Cómo quieres ingresar?"
    _post({
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": cuerpo},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": "rol_aspirante", "title": "Aspirante"}},
                {"type": "reply", "reply": {"id": "rol_estudiante", "title": "Estudiante"}},
            ]},
        },
    })


def texto_faq(rol):
    lineas = [f"{i}. {q}" for i, q in enumerate(core.PREGUNTAS_FRECUENTES[rol], 1)]
    return ("Preguntas frecuentes (responde con el número o escribe tu propia duda):\n\n"
            + "\n".join(lineas)
            + "\n\nEscribe *menu* para cambiar de perfil.")


# ------------------------------ Lógica de conversación ---------------------
def _nueva_sesion():
    return {"estado": "menu", "rol": None, "id": None, "messages": [],
            "ultima": time.time(), "intentos": 0, "bloqueado_hasta": 0.0}


def procesar(numero, texto, boton_id):
    with _lock:
        s = SESIONES.setdefault(numero, _nueva_sesion())
        expiro = s["estado"] == "chat" and time.time() - s["ultima"] > SESION_MINUTOS * 60
        if expiro:
            bloqueo = s["bloqueado_hasta"]
            s.clear()
            s.update(_nueva_sesion())
            s["bloqueado_hasta"] = bloqueo
        s["ultima"] = time.time()

    if expiro:
        enviar_menu(numero, f"Tu sesión se cerró por inactividad ({SESION_MINUTOS:g} min).")
        return

    t = (texto or "").strip()
    comando = t.lower()

    # Comandos globales
    if comando in ("menu", "menú", "salir", "inicio", "hola") and s["estado"] != "esperando_id":
        s.update(_nueva_sesion())
        enviar_menu(numero)
        return

    # ---- Menú de perfil ----
    if s["estado"] == "menu":
        if boton_id == "rol_aspirante":
            s.update(estado="chat", rol=core.ASPIRANTE, messages=[])
            enviar_texto(numero, core.BIENVENIDA[core.ASPIRANTE])
            enviar_texto(numero, texto_faq(core.ASPIRANTE))
        elif boton_id == "rol_estudiante":
            restante = int(s["bloqueado_hasta"] - time.time())
            if restante > 0:
                enviar_texto(numero, f"Demasiados intentos fallidos. Espera {restante} segundos.")
                return
            s["estado"] = "esperando_id"
            enviar_texto(numero, "Escribe tu *matrícula* o tu *correo institucional* para verificar que eres estudiante.")
        else:
            enviar_menu(numero)
        return

    # ---- Verificación de estudiante ----
    if s["estado"] == "esperando_id":
        restante = int(s["bloqueado_hasta"] - time.time())
        if restante > 0:
            enviar_texto(numero, f"Demasiados intentos fallidos. Espera {restante} segundos.")
            return
        if comando in ("menu", "menú", "salir", "cancelar"):
            s.update(_nueva_sesion())
            enviar_menu(numero)
            return
        try:
            ok, error, ident = core.validar_acceso_estudiante(t)
        except core.ListaNoDisponible:
            enviar_texto(numero, "El acceso de estudiantes no está disponible en este momento. "
                                 "Inténtalo más tarde o contacta a la coordinación.")
            return
        if ok:
            s.update(estado="chat", rol=core.ESTUDIANTE, id=ident, messages=[], intentos=0)
            enviar_texto(numero, "✅ Verificado.\n" + core.BIENVENIDA[core.ESTUDIANTE])
            enviar_texto(numero, texto_faq(core.ESTUDIANTE))
        else:
            s["intentos"] += 1
            if s["intentos"] >= MAX_INTENTOS:
                s["bloqueado_hasta"] = time.time() + SEGUNDOS_BLOQUEO
                s["intentos"] = 0
            enviar_texto(numero, error)
        return

    # ---- Chat ----
    if not t:
        enviar_texto(numero, "Por ahora solo puedo leer mensajes de texto. Escribe tu duda sobre la maestría.")
        return
    rol = s["rol"]
    # Atajo: "1".."5" = pregunta frecuente
    if t.isdigit() and 1 <= int(t) <= len(core.PREGUNTAS_FRECUENTES[rol]):
        t = core.PREGUNTAS_FRECUENTES[rol][int(t) - 1]
    t = t[:core.MAX_CARACTERES_PREGUNTA]

    preguntas = sum(1 for m in s["messages"] if m["role"] == "user")
    if preguntas >= core.MAX_PREGUNTAS_SESION:
        enviar_texto(numero, "Llegaste al límite de preguntas de esta sesión. Escribe *menu* para iniciar otra.")
        return

    s["messages"].append({"role": "user", "content": t})
    respuesta, ok = core.responder(rol, s["messages"])
    s["messages"].append({"role": "assistant", "content": respuesta, "error": not ok})
    s["ultima"] = time.time()
    enviar_texto(numero, respuesta)


# ------------------------------ Rutas --------------------------------------
@app.get("/")
def raiz():
    return {"status": "ok", "servicio": "asistente-maestria-whatsapp"}


@app.get("/webhook")
async def verificar(request: Request):
    """Meta llama aquí una vez para verificar tu URL."""
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and VERIFY_TOKEN and hmac.compare_digest(
            q.get("hub.verify_token", ""), VERIFY_TOKEN):
        return Response(content=q.get("hub.challenge", ""), media_type="text/plain")
    raise HTTPException(status_code=403, detail="Token de verificación inválido")


def _firma_valida(cuerpo: bytes, cabecera: str) -> bool:
    if not APP_SECRET:
        log.warning("APP_SECRET no configurado: no se está verificando la firma de Meta")
        return True
    esperado = "sha256=" + hmac.new(APP_SECRET.encode(), cuerpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado, cabecera or "")


def _tarea(numero, texto, boton_id):
    try:
        procesar(numero, texto, boton_id)
    except Exception:
        log.exception("Error procesando mensaje")
        enviar_texto(numero, "Ocurrió un error inesperado. Intenta de nuevo en un momento.")


@app.post("/webhook")
async def recibir(request: Request, tareas: BackgroundTasks):
    cuerpo = await request.body()
    if not _firma_valida(cuerpo, request.headers.get("X-Hub-Signature-256", "")):
        raise HTTPException(status_code=403, detail="Firma inválida")

    datos = await request.json()
    ahora = time.time()
    for k in [k for k, v in _vistos.items() if ahora - v > 3600]:
        _vistos.pop(k, None)

    for entrada in datos.get("entry", []):
        for cambio in entrada.get("changes", []):
            for msg in cambio.get("value", {}).get("messages", []):
                if msg.get("id") in _vistos:
                    continue
                _vistos[msg.get("id")] = ahora
                numero = msg.get("from")
                tipo = msg.get("type")
                texto, boton_id = "", None
                if tipo == "text":
                    texto = msg["text"].get("body", "")
                elif tipo == "interactive":
                    inter = msg.get("interactive", {})
                    boton_id = (inter.get("button_reply") or inter.get("list_reply") or {}).get("id")
                # Responder 200 rápido a Meta y procesar en segundo plano (Gemini tarda)
                tareas.add_task(_tarea, numero, texto, boton_id)
    return {"status": "ok"}