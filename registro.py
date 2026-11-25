"""Registro compartido de preguntas (lo usan bot.py y app.py; lo lee panel.py)."""
import csv
import os
import threading
from datetime import datetime

LOG_FILE = os.getenv("LOG_FILE", "preguntas.csv")
CAMPOS = ["fecha", "usuario", "pregunta", "respuesta", "respondida"]
_lock = threading.Lock()


def registrar_pregunta(usuario: str, pregunta: str, respuesta: str, respondida: bool) -> None:
    with _lock:
        nuevo = not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE) == 0
        with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if nuevo:
                w.writerow(CAMPOS)
            w.writerow([datetime.now().isoformat(timespec="seconds"), usuario,
                        pregunta, respuesta, int(respondida)])