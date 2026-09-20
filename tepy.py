import pygame
import math
import random
import sys
import os
import io
import base64
import json
import hashlib
import time as _time
import threading
try:
    import subprocess
    HAS_SUBPROCESS = True
except ImportError:
    HAS_SUBPROCESS = False
import re
import tempfile
import shutil
import queue
import asyncio

# True cuando corre dentro de un navegador (build pygbag / Pyodide).
# En ese entorno no existen subprocess, tkinter, yt_dlp, cv2 ni sockets
# TCP, y no hay hilos reales: todo lo que depende de eso se desactiva
# o se sustituye por su equivalente web.
IS_WEB = sys.platform == "emscripten"
IS_FROZEN = getattr(sys, 'frozen', False)

# --- Guard anti "server que se relanza a si mismo" ------------------
# BUG HISTORICO: el juego intentaba levantar server.py haciendo
# subprocess.Popen([sys.executable, server_path]). Eso asume que
# sys.executable es un interprete de Python que va a *ejecutar*
# server_path como argumento. Pero si el juego esta compilado a .exe
# (PyInstaller/Nuitka/etc.) y por lo que sea IS_FROZEN no lo detecta
# bien, sys.executable apunta al propio .exe del juego: entonces esa
# linea NO lanza server.py, lanza OTRA COPIA COMPLETA DEL JUEGO (que
# ignora el argumento). Esa copia llega al menu y vuelve a hacer lo
# mismo -> explosion exponencial de procesos hasta tumbar la PC.
#
# _server_launch_allowed() es la unica funcion autorizada a decidir si
# corresponde lanzar server.py, y nunca confia solo en IS_FROZEN.
_SERVER_LAUNCH_ATTEMPTED = False


def _server_launch_allowed(server_path):
    """True solo si es seguro y tiene sentido lanzar server.py como
    proceso hijo. Se llama una unica vez por ejecucion (idempotente)."""
    global _SERVER_LAUNCH_ATTEMPTED
    if _SERVER_LAUNCH_ATTEMPTED:
        return False  # ya se intento en este proceso, no reintentar
    _SERVER_LAUNCH_ATTEMPTED = True

    if IS_WEB or IS_FROZEN:
        return False
    # server_path tiene que ser un .py real que exista: si no existe,
    # sys.executable (aunque sea python.exe) no tiene nada que correr,
    # y si estamos frozen-pero-no-detectado esto tambien lo frena
    # porque junto al .exe normalmente no viaja server.py con ese nombre.
    if not server_path.lower().endswith(".py") or not os.path.isfile(server_path):
        return False
    # Nunca lanzar algo que resuelva al mismo ejecutable que nos corre
    # a nosotros mismos (cinturon extra adicional al chequeo de arriba).
    try:
        if os.path.abspath(sys.executable) == os.path.abspath(server_path):
            return False
    except Exception:
        return False
    return True


def _detect_low_end():
    """Detect hardware too weak for full effects: Atom CPUs, <2 cores,
    32-bit OS, or very low clock speeds.  Returns True when the game
    should strip particles, gradients, video, and extra drawing."""
    if IS_WEB:
        return False
    try:
        import platform
        machine = platform.machine().lower()
        processor = platform.processor().lower()
        bits = struct.calcsize("P") * 8  # 32 or 64
        cpu_count = os.cpu_count() or 1
        # Atom / Celeron / old low-power chips
        low_cpu = any(k in processor for k in ("atom", "celeron", "pentium", "athlon"))
        # 32-bit OS on any hardware is a signal
        is_32bit = bits == 32
        # Very few cores
        few_cores = cpu_count <= 2
        # struct not imported yet; use platform directly
    except Exception:
        return False
    return low_cpu or is_32bit or few_cores


try:
    import struct
except ImportError:
    pass

LOW_END = _detect_low_end()

# Mutable flag: can be toggled at runtime from the options menu.
low_end = LOW_END


def _pip_install(pip_name):
    """Install a package with pip, working around PEP 668
    'externally managed environment' restrictions when needed."""
    # CRITICO: nunca intentar esto en un build compilado (PyInstaller,
    # Nuitka, etc.) ni en el navegador. En un .exe frozen, sys.executable
    # apunta al PROPIO .exe del juego (no hay un Python con pip dentro).
    # Correr [sys.executable, "-m", "pip", "install", ...] en ese caso
    # no instala nada: relanza el juego entero desde cero, que al
    # arrancar vuelve a intentar el mismo import fallido y vuelve a
    # llamar a _pip_install -> explosion recursiva de procesos que
    # puede tumbar la maquina. Un build frozen tiene que traer sus
    # dependencias ya incluidas o directamente prescindir de ellas.
    if IS_FROZEN or IS_WEB or not HAS_SUBPROCESS:
        return False
    # Segundo cinturon: nunca lanzar pip apuntando a nuestro propio
    # ejecutable actual, pase lo que pase con la deteccion de IS_FROZEN.
    try:
        if os.path.abspath(sys.executable) == os.path.abspath(sys.argv[0]):
            # Estamos corriendo como un binario propio (no "python algo.py"
            # sino "./nuestro_programa"): no hay garantia de que
            # sys.executable tenga pip. Mejor no arriesgar.
            base_name = os.path.basename(sys.executable).lower()
            if "python" not in base_name:
                return False
    except Exception:
        return False
    base_cmd = [sys.executable, "-m", "pip", "install", "--quiet", pip_name]
    try:
        r = subprocess.run(base_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=240)
        if r.returncode == 0:
            return True
    except Exception:
        pass
    try:
        r = subprocess.run(base_cmd + ["--break-system-packages"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=240)
        return r.returncode == 0
    except Exception:
        return False


def _ensure_import(import_name, pip_name=None):
    """Import a module, auto-installing it via pip on first use if missing.
    Lets optional features (video playback, audio extraction, YouTube
    download) work without the user manually installing anything."""
    pip_name = pip_name or import_name
    try:
        return __import__(import_name)
    except ImportError:
        if _pip_install(pip_name):
            try:
                return __import__(import_name)
            except ImportError:
                return None
        return None


if IS_WEB:
    # En el navegador no se puede instalar nada ni ejecutar binarios:
    # video y descarga de YouTube quedan desactivados.
    yt_dlp = None
    HAS_YTDL = False
    cv2 = None
    HAS_CV2 = False
else:
    try:
        import yt_dlp
        HAS_YTDL = True
    except ImportError:
        yt_dlp = _ensure_import("yt_dlp")
        HAS_YTDL = yt_dlp is not None

    try:
        import cv2
        HAS_CV2 = True
    except ImportError:
        cv2 = _ensure_import("cv2", "opencv-python-headless")
        HAS_CV2 = cv2 is not None

_ffmpeg_exe_cache = None
def get_ffmpeg_exe():
    """Path to a ready-to-use ffmpeg binary, installed automatically via the
    imageio-ffmpeg pip package (bundles a static binary, no system-level
    ffmpeg install required)."""
    global _ffmpeg_exe_cache
    if _ffmpeg_exe_cache:
        return _ffmpeg_exe_cache
    if shutil.which("ffmpeg"):
        _ffmpeg_exe_cache = "ffmpeg"
        return _ffmpeg_exe_cache
    iio = _ensure_import("imageio_ffmpeg", "imageio-ffmpeg")
    if iio is None:
        return None
    try:
        _ffmpeg_exe_cache = iio.get_ffmpeg_exe()
        return _ffmpeg_exe_cache
    except Exception:
        return None

try:
    import tkinter as tk
    from tkinter import filedialog
    HAS_TKINTER = True
except ImportError:
    HAS_TKINTER = False

try:
    from network import NetworkClient
    HAS_NETWORK = True
except ImportError:
    HAS_NETWORK = False

try:
    from pygame._sdl2 import controller as _sdl2_controller
    HAS_SDL2_CONTROLLER = True
except Exception:
    HAS_SDL2_CONTROLLER = False

pygame.init()
HAS_AUDIO = True
if IS_WEB:
    # En el navegador el audio arranca tras la primera interaccion del
    # usuario (politica de autoplay); si falla, el juego sigue sin audio.
    try:
        pygame.mixer.init(44100, -16, 2, 2048)
    except Exception:
        HAS_AUDIO = False
else:
    pygame.mixer.init(44100, -16, 2, 2048)

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


class SoundManager:
    def __init__(self):
        self.sounds = {}
        self.enabled = True
        self.volume = 0.5
        if HAS_NUMPY and HAS_AUDIO:
            try:
                self._generate_all()
            except Exception:
                # Si la sintesis de audio falla (ej. en el navegador),
                # el juego corre sin SFX en vez de crashear el arranque.
                self.sounds = {}
        # Sounds are played from a dedicated worker thread instead of the
        # main thread. On some systems the audio driver makes
        # pygame.mixer.Sound.play() block for a noticeable amount of time;
        # doing that on the main thread stalls rendering and causes constant
        # lag/low fps. Queuing it onto a background thread keeps the game
        # loop responsive even if audio playback itself is slow.
        # En el navegador no hay hilos reales: se reproduce directamente.
        self._sound_queue = queue.Queue()
        self._sound_thread = None
        if not IS_WEB:
            self._sound_thread = threading.Thread(target=self._sound_worker, daemon=True)
            self._sound_thread.start()

    def _sound_worker(self):
        while True:
            name = self._sound_queue.get()
            try:
                s = self.sounds.get(name)
                if s:
                    s.set_volume(self.volume)
                    s.play()
            except Exception:
                pass

    def _envelope(self, n, attack_ms, hold_ms, decay_ms, sample_rate=44100):
        a = int(attack_ms * sample_rate / 1000)
        h = int(hold_ms * sample_rate / 1000)
        d = int(decay_ms * sample_rate / 1000)
        total = a + h + d
        if total < n:
            env = np.concatenate([
                np.linspace(0, 1, max(a, 1)),
                np.ones(h),
                # Decaimiento exponencial: suena mucho mas natural y menos
                # "metalico" que un corte lineal brusco.
                np.exp(-4.5 * np.linspace(0, 1, max(d, 1))),
                np.zeros(n - total)
            ])[:n]
        else:
            env = np.linspace(0, 1, n)
        return env

    def _tone(self, freq, dur, vol=0.28, attack=8, hold=15, decay=90):
        sr = 44100
        n = int(sr * dur)
        t = np.linspace(0, dur, n, False)
        wave = np.sin(2 * np.pi * freq * t)
        # Sub-armonico: da cuerpo y quita el caracter "beep" del seno puro.
        wave += 0.25 * np.sin(2 * np.pi * freq * 0.5 * t)
        wave *= self._envelope(n, attack, hold, decay, sr) * vol
        return (np.clip(wave, -1, 1) * 32767).astype(np.int16)

    def _sweep(self, f1, f2, dur, vol=0.3, attack=8, hold=12, decay=110):
        sr = 44100
        n = int(sr * dur)
        t = np.linspace(0, dur, n, False)
        freqs = np.linspace(f1, f2, n)
        wave = np.sin(2 * np.pi * freqs * t)
        wave += 0.2 * np.sin(2 * np.pi * freqs * 0.5 * t)
        wave *= self._envelope(n, attack, hold, decay, sr) * vol
        return (np.clip(wave, -1, 1) * 32767).astype(np.int16)

    def _noise(self, dur, vol=0.2, attack=6, hold=8, decay=100):
        sr = 44100
        n = int(sr * dur)
        wave = np.random.uniform(-1, 1, n)
        wave *= self._envelope(n, attack, hold, decay, sr) * vol
        return (np.clip(wave, -1, 1) * 32767).astype(np.int16)

    def _chord(self, freqs, dur, vol=0.3, attack=8, hold=30, decay=170):
        sr = 44100
        n = int(sr * dur)
        t = np.linspace(0, dur, n, False)
        wave = sum(np.sin(2 * np.pi * f * t) for f in freqs) / len(freqs)
        wave *= self._envelope(n, attack, hold, decay, sr) * vol
        return (np.clip(wave, -1, 1) * 32767).astype(np.int16)

    def _arpeggio(self, freqs, dur, vol=0.25, note_dur=0.06):
        sr = 44100
        notes = []
        for f in freqs:
            n = int(sr * note_dur)
            t = np.linspace(0, note_dur, n, False)
            wave = np.sin(2 * np.pi * f * t)
            wave += 0.2 * np.sin(2 * np.pi * f * 0.5 * t)
            wave *= self._envelope(n, 4, 10, 50, sr) * vol
            notes.append((np.clip(wave, -1, 1) * 32767).astype(np.int16))
        combined = np.concatenate(notes)
        return combined

    def _soften(self, data, cutoff=2200.0, sr=44100):
        # Filtro paso-bajo FFT: redondea los bordes duros de los tonos
        # agudos para que no suenen estridentes.
        spectrum = np.fft.rfft(data.astype(np.float64))
        freqs = np.fft.rfftfreq(len(data), 1.0 / sr)
        spectrum *= 1.0 / (1.0 + (freqs / cutoff) ** 4)
        out = np.fft.irfft(spectrum, len(data))
        return np.clip(out, -1.0, 1.0).astype(np.int16)

    def _make(self, data):
        data = self._soften(data)
        stereo = np.column_stack((data, data))
        return pygame.sndarray.make_sound(stereo)

    def _generate_all(self):
        self.sounds["move"] = self._make(self._tone(280, 0.04, 0.15, 4, 8, 30))
        self.sounds["rotate"] = self._make(self._tone(520, 0.05, 0.18, 5, 10, 40))
        self.sounds["drop"] = self._make(self._sweep(180, 90, 0.1, 0.26, 5, 15, 80))
        self.sounds["hard_drop"] = self._make(
            np.concatenate([
                self._noise(0.08, 0.32, 4, 5, 40),
                self._tone(120, 0.12, 0.28, 8, 20, 90)
            ])
        )
        self.sounds["line_clear"] = self._make(
            np.concatenate([
                self._tone(600, 0.06, 0.2, 5, 10, 25),
                self._tone(800, 0.06, 0.2, 5, 10, 25),
                self._tone(1000, 0.15, 0.24, 5, 20, 110),
            ])
        )
        self.sounds["tetris"] = self._make(
            np.concatenate([
                self._arpeggio([523, 659, 784, 1047], 0.35, 0.28, 0.05),
                self._chord([523, 659, 784, 1047], 0.25, 0.25, 8, 30, 180),
            ])
        )
        self.sounds["hold"] = self._make(self._sweep(600, 400, 0.06, 0.16, 5, 10, 45))
        self.sounds["menu_click"] = self._make(self._tone(700, 0.03, 0.1, 4, 5, 22))
        self.sounds["menu_select"] = self._make(
            np.concatenate([self._tone(600, 0.03, 0.14, 4, 5, 18), self._tone(900, 0.04, 0.17, 4, 8, 28)])
        )
        self.sounds["rank_up"] = self._make(self._arpeggio([523, 659, 784, 1047, 1319], 0.3, 0.28, 0.04))
        self.sounds["game_over"] = self._make(
            np.concatenate([
                self._sweep(400, 100, 0.6, 0.28, 25, 50, 420),
                self._noise(0.3, 0.1, 100, 50, 220),
            ])
        )
        self.sounds["level_up"] = self._make(self._arpeggio([440, 554, 659, 880], 0.3, 0.25, 0.04))
        for i in range(1, 11):
            freq = 800 + i * 80
            self.sounds[f"combo_{i}"] = self._make(self._tone(freq, 0.1, 0.2, 5, 12, 80))
        self.sounds["combo"] = self.sounds["combo_1"]

    def play(self, name):
        if not self.enabled or not self.sounds.get(name):
            return
        try:
            if IS_WEB or self._sound_thread is None:
                s = self.sounds.get(name)
                if s:
                    s.set_volume(self.volume)
                    s.play()
            else:
                self._sound_queue.put_nowait(name)
        except Exception:
            pass

    def play_combo(self, combo_count):
        idx = max(1, min(combo_count, 10))
        self.play(f"combo_{idx}")


sfx = SoundManager()

_font_cache = {}
def get_font(size, bold=False):
    key = (size, bold)
    if key not in _font_cache:
        _font_cache[key] = pygame.font.SysFont("Arial", size, bold=bold)
    return _font_cache[key]

_text_cache = {}
def get_text(text, color, font):
    """Superficie de texto cacheada por (texto, color, fuente). Evita
    re-renderizar con font.render cada frame los textos del HUD que
    casi nunca cambian (etiquetas, breadcrumbs, ticks, game over...).
    Las fuentes vienen de get_font (cacheadas permanentemente), asi
    que id(font) es estable durante toda la sesion."""
    key = (text, color, id(font))
    surf = _text_cache.get(key)
    if surf is None:
        surf = font.render(text, True, color)
        if len(_text_cache) >= 500:
            _text_cache.clear()
        _text_cache[key] = surf
    return surf

_gradient_cache = {}
def get_gradient(w, h, top_rgb, bot_rgb, alpha=220):
    key = (w, h, top_rgb, bot_rgb, alpha)
    if key not in _gradient_cache or _gradient_cache[key].get_size() != (w, h):
        surf = pygame.Surface((w, h), pygame.SRCALPHA)
        for row in range(h):
            t = row / max(h - 1, 1)
            r = int(top_rgb[0] + (bot_rgb[0] - top_rgb[0]) * t)
            g = int(top_rgb[1] + (bot_rgb[1] - top_rgb[1]) * t)
            b = int(top_rgb[2] + (bot_rgb[2] - top_rgb[2]) * t)
            pygame.draw.line(surf, (r, g, b, alpha), (0, row), (w, row))
        _gradient_cache[key] = surf
    return _gradient_cache[key]

_overlay_cache = {}
_top_bar_cache = {}
def get_overlay(w, h, color, alpha):
    key = (w, h, color)
    if key not in _overlay_cache or _overlay_cache[key][1] != alpha:
        surf = pygame.Surface((w, h), pygame.SRCALPHA)
        surf.fill((*color, alpha))
        _overlay_cache[key] = (surf, alpha)
    return _overlay_cache[key][0]

ACCOUNTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts.json")
SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session.json")

# Usuaria playtester exclusiva: cuenta especial con su propia insignia y un
# mensajito al iniciar sesion. Comparacion sin importar mayusculas/minusculas
# para que "snowf20", "SnowF20", etc. cuenten igual.
SPECIAL_USER = "SnowF20"


def is_special_user(username):
    return bool(username) and username.strip().lower() == SPECIAL_USER.lower()


NEWS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "news.json")
STATS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mp_stats.json")


def load_session():
    """Usuario con sesion recordada de un arranque anterior, para no tener
    que iniciar sesion cada vez que se abre el juego. Mismo patron que
    accounts/mp_stats: localStorage en web, json en disco en escritorio.
    No guarda la contrasena, solo el nombre de usuario ya autenticado."""
    if IS_WEB:
        try:
            import js
            raw = js.localStorage.getItem("tepy_session")
            return str(raw) if raw else ""
        except Exception:
            return ""
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, "r") as f:
                data = json.load(f)
            return data.get("username", "") or ""
        except Exception:
            return ""
    return ""


def save_session(username):
    """Recuerda la sesion activa (o la borra si username es "")."""
    if IS_WEB:
        try:
            import js
            if username:
                js.localStorage.setItem("tepy_session", username)
            else:
                js.localStorage.removeItem("tepy_session")
        except Exception:
            pass
        return
    try:
        if username:
            with open(SESSION_FILE, "w") as f:
                json.dump({"username": username}, f)
        elif os.path.exists(SESSION_FILE):
            os.remove(SESSION_FILE)
    except Exception:
        pass


def load_mp_stats():
    """Ranking de multijugador (victorias/derrotas/rating) por nombre de
    jugador. Mismo patron que accounts: localStorage en web, json en disco
    en escritorio. Es un rating simple estilo Elo casero, no un ranking
    online real contra un servidor de rankings."""
    stats = {}
    if IS_WEB:
        try:
            import js
            raw = js.localStorage.getItem("tepy_mp_stats")
            if raw:
                stats = json.loads(str(raw))
                if not isinstance(stats, dict):
                    stats = {}
        except Exception:
            pass
    elif os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r") as f:
                stats = json.load(f)
        except Exception:
            pass
    return stats


def save_mp_stats(stats):
    if IS_WEB:
        try:
            import js
            js.localStorage.setItem("tepy_mp_stats", json.dumps(stats))
        except Exception:
            pass
        return
    try:
        with open(STATS_FILE, "w") as f:
            json.dump(stats, f, indent=2)
    except Exception:
        pass


def get_player_rank(stats, name):
    entry = stats.get(name)
    if entry is None:
        entry = {"wins": 0, "losses": 0, "rating": 1000}
        stats[name] = entry
    return entry


def apply_match_result(stats, name, won):
    entry = get_player_rank(stats, name)
    if won:
        entry["wins"] = entry.get("wins", 0) + 1
        entry["rating"] = entry.get("rating", 1000) + 25
    else:
        entry["losses"] = entry.get("losses", 0) + 1
        entry["rating"] = max(0, entry.get("rating", 1000) - 20)
    save_mp_stats(stats)
    return entry


def load_local_news():
    """Lee news.json directamente (fallback sin conexion, util en la
    maquina donde corre el server)."""
    try:
        with open(NEWS_FILE, "r", encoding="utf-8") as f:
            news = json.load(f)
            if isinstance(news, list):
                return news
    except Exception:
        pass
    return []


# Tope de tamano (en caracteres base64) para la imagen de una noticia.
# El server tambien rechaza mensajes de mas de 2MB (ver recv_msg en
# server.py), asi que nos quedamos comodos por debajo de eso.
NEWS_IMAGE_MAX_B64 = 900_000
NEWS_IMAGE_MAX_W = 480

# La foto de perfil se guarda embebida (base64) dentro de accounts.json en
# vez de solo guardar la ruta del archivo: asi no se pierde si el archivo
# original se mueve, se borra, o el archivo de cuentas se copia a otra
# maquina. Se guarda chica porque solo se muestra en circulos pequenos.
AVATAR_IMAGE_MAX_B64 = 300_000
AVATAR_IMAGE_MAX_W = 256


def encode_news_image(path):
    """Carga una imagen del disco, la achica si hace falta y la devuelve
    codificada en base64 (PNG) lista para mandar en una noticia.
    Devuelve (base64_str, None) si salio bien, o (None, error_msg)."""
    try:
        img = pygame.image.load(path)
    except Exception:
        return None, "No se pudo abrir esa imagen"
    try:
        img = img.convert_alpha()
    except Exception:
        pass
    w, h = img.get_width(), img.get_height()
    if w > NEWS_IMAGE_MAX_W:
        scale = NEWS_IMAGE_MAX_W / w
        img = pygame.transform.smoothscale(img, (NEWS_IMAGE_MAX_W, max(1, int(h * scale))))
    buf = io.BytesIO()
    try:
        pygame.image.save(img, buf, "news_image.png")
    except Exception:
        try:
            buf = io.BytesIO()
            pygame.image.save(img, buf)
        except Exception:
            return None, "No se pudo codificar la imagen"
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    if len(b64) > NEWS_IMAGE_MAX_B64:
        return None, "Imagen muy pesada, proba con una mas chica"
    return b64, None


_news_img_cache = {}


def get_news_image_surface(item, max_w):
    """Decodifica (y cachea) la imagen en base64 de una noticia, escalada
    a `max_w` de ancho. Devuelve None si la noticia no tiene imagen o no
    se pudo decodificar."""
    b64 = item.get("image") if isinstance(item, dict) else None
    if not b64:
        return None
    key = (item.get("id"), max_w)
    if key in _news_img_cache:
        return _news_img_cache[key]
    surf = None
    try:
        raw = base64.b64decode(b64)
        img = pygame.image.load(io.BytesIO(raw), "news_image.png").convert_alpha()
        if img.get_width() > max_w:
            scale = max_w / img.get_width()
            img = pygame.transform.smoothscale(img, (max_w, max(1, int(img.get_height() * scale))))
        surf = img
    except Exception:
        surf = None
    _news_img_cache[key] = surf
    return surf


def wrap_text(text, font, max_w, max_lines=3):
    """Ajuste de texto por palabra medido con font.size()."""
    lines = []
    cur = ""
    for word in str(text).split():
        test = (cur + " " + word).strip()
        if font.size(test)[0] <= max_w or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = word
        if len(lines) >= max_lines:
            return lines
    if cur:
        lines.append(cur)
    return lines

def _hash_pw(pw):
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()

ADMIN_USER = "Nattyelxdyt"
ADMIN_HASH = "b0e679ef76c9c1f603bfdf3940e9535f18aceb273c74ef957f56209459c2db01"

def load_accounts():
    accounts = {}
    if IS_WEB:
        # En el navegador el sistema de archivos no persiste: las cuentas
        # se guardan en localStorage del navegador.
        try:
            import js
            raw = js.localStorage.getItem("tepy_accounts")
            if raw:
                accounts = json.loads(str(raw))
                if not isinstance(accounts, dict):
                    accounts = {}
        except Exception:
            pass
    elif os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, "r") as f:
                accounts = json.load(f)
        except Exception:
            pass
    accounts[ADMIN_USER] = {"password": ADMIN_HASH, "admin": True, "created": 0}
    return accounts

def save_accounts(accounts):
    if IS_WEB:
        try:
            import js
            js.localStorage.setItem("tepy_accounts", json.dumps(accounts))
        except Exception:
            pass
        return
    try:
        with open(ACCOUNTS_FILE, "w") as f:
            json.dump(accounts, f, indent=2)
    except Exception:
        pass

def save_user_settings(username, settings):
    accounts = load_accounts()
    if username not in accounts:
        return
    accounts[username]["settings"] = {
        "das": settings.get("das", 167),
        "arr": settings.get("arr", 50),
        "lock": settings.get("lock", 500),
        "ghost": settings.get("ghost", True),
        "start_level": settings.get("start_level", 1),
        "next_count": settings.get("next_count", 5),
        "bg_style": settings.get("bg_style", 0),
        "custom_bg_path": settings.get("custom_bg_path", ""),
        "volume": settings.get("volume", 60),
        "music": settings.get("music", True),
        "sfx": settings.get("sfx", True),
        "piece_colors": settings.get("piece_colors", list(COLORS)),
        "menu_falling_blocks": settings.get("menu_falling_blocks", True),
        "avatar_color_idx": settings.get("avatar_color_idx", 0),
        "avatar_path": settings.get("avatar_path", ""),
        "avatar_data": settings.get("avatar_data", ""),
        "banner_color_idx": settings.get("banner_color_idx", 0),
        "profile_title_idx": settings.get("profile_title_idx", 0),
        "keys": settings.get("keys", {}),
        "best_score": settings.get("best_score", 0),
        "bio": settings.get("bio", ""),
    }
    save_accounts(accounts)

def load_user_settings(username):
    accounts = load_accounts()
    acc = accounts.get(username, {})
    return acc.get("settings", None)

def register_account(username, password):
    accounts = load_accounts()
    if username == ADMIN_USER:
        return False, "Nombre no disponible"
    if username in accounts:
        return False, "Usuario ya existe"
    if len(username) < 2 or len(username) > 16:
        return False, "Nombre: 2-16 caracteres"
    if len(password) < 4:
        return False, "Contrasena: minimo 4 caracteres"
    accounts[username] = {"password": _hash_pw(password), "created": _time.time()}
    save_accounts(accounts)
    return True, "Cuenta creada"

def login_account(username, password):
    accounts = load_accounts()
    if username not in accounts:
        return False, "Usuario no encontrado"
    if accounts[username]["password"] != _hash_pw(password):
        return False, "Contrasena incorrecta"
    return True, "Bienvenido"

COLS = 10
ROWS = 20
# Tope de jugadores por sala online (debe coincidir con MAX_ROOM_PLAYERS
# en server.py).
MAX_ROOM_PLAYERS = 8
FPS = 60

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)

# Rect (x, y, w, h) de la insignia de perfil dibujada en la barra superior,
# o None si no se dibujo este frame. Se actualiza en draw_top_bar() y se
# lee en el bucle de eventos para detectar el click, sin tener que pasar
# el rect de vuelta a traves de draw_submenu/draw_menu_with_chat.
_profile_badge_rect = None
GRAY = (40, 40, 40)
DARK_GRAY = (80, 80, 80)

DAS_VALUES = [100, 167, 250]
ARR_VALUES = [33, 50, 80]
LOCK_VALUES = [300, 500, 800]
LEVEL_VALUES = list(range(1, 16))
NEXT_VALUES = [1, 3, 5]
BG_NAMES = ["Gradiente", "Oscuro", "Oceano", "Atardecer", "Synthwave", "Bosque", "Sangre", "Mono"]
CTRL_ACTIONS = ["left", "right", "soft_drop", "hard_drop", "rotate", "rotate_ccw", "hold", "restart"]
CTRL_LABELS = ["Mover izquierda", "Mover derecha", "Soft drop", "Hard drop", "Rotar", "Rotar izquierda", "Hold", "Reiniciar"]

COLORS = [
    (100, 210, 210), (80, 80, 200), (210, 155, 72),
    (210, 210, 80), (80, 190, 80), (150, 80, 200), (200, 80, 80),
]

COLOR_PALETTE = [
    (100, 210, 210), (80, 80, 200), (210, 155, 72),
    (210, 210, 80), (80, 190, 80), (150, 80, 200), (200, 80, 80),
    (255, 160, 180), (180, 255, 180), (180, 180, 255),
    (255, 255, 180), (255, 180, 255), (180, 255, 255), (255, 200, 140),
    (200, 200, 200), (255, 120, 120), (120, 255, 120),
    (120, 120, 255), (255, 255, 255), (60, 60, 60),
]

# Paleta de fondos ("banner") para la tarjeta de perfil.
BANNER_COLORS = [
    (35, 40, 60), (60, 30, 55), (25, 55, 60), (55, 45, 20),
    (25, 45, 30), (50, 25, 25), (30, 30, 55), (45, 45, 45),
]

# Titulos que se pueden mostrar junto al nombre en el perfil. Los que
# tienen requisito se desbloquean solos al cumplirlo (no hace falta
# guardar nada aparte: se recalcula a partir de mp_stats cada vez).
# (nombre, victorias minimas requeridas)
PROFILE_TITLES = [
    ("Sin titulo", 0),
    ("Novato", 0),
    ("Aprendiz", 3),
    ("Competidor", 10),
    ("Veterano", 25),
    ("Estratega", 50),
    ("Leyenda", 100),
]

# Titulos exclusivos que no se desbloquean jugando: solo los tiene la
# gente que trabajo directamente en el proyecto. La clave va en minuscula.
EXCLUSIVE_TITLES = {
    "snowf20": "PlayTester",
}


def get_unlocked_titles(username, p_wins):
    """Titulos desbloqueados por victorias + el titulo exclusivo del
    usuario, si tiene uno asignado (no se puede ganar jugando)."""
    unlocked = [t for t, req in PROFILE_TITLES if p_wins >= req]
    exclusive = EXCLUSIVE_TITLES.get((username or "").strip().lower())
    if exclusive and exclusive not in unlocked:
        unlocked.append(exclusive)
    return unlocked

PIECE_NAMES = ["I", "O", "T", "S", "Z", "L", "J"]

SHAPES = [
    [[1, 1, 1, 1]], [[1, 0, 0], [1, 1, 1]], [[0, 0, 1], [1, 1, 1]],
    [[1, 1], [1, 1]], [[0, 1, 1], [1, 1, 0]], [[0, 1, 0], [1, 1, 1]], [[1, 1, 0], [0, 1, 1]],
]

SPRITE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sprites")

COMBO_SPRITE_MAP = {
    "bubble": "spr_tv_combobubble",
    "fill": "spr_tv_combobubblefill",
    "rank_hud": "spr_ranks_hud",
    "rank_hud_fill": "spr_ranks_hudfill",
    "combo_end": "spr_comboend",
    "combo_very": "spr_combovery",
}


class Sprites:
    _cache = {}

    @classmethod
    def load(cls, name):
        if name in cls._cache:
            return cls._cache[name]
        folder = COMBO_SPRITE_MAP.get(name, name)
        folder_path = os.path.join(SPRITE_DIR, folder)
        if not os.path.isdir(folder_path):
            cls._cache[name] = None
            return None
        pngs = sorted([f for f in os.listdir(folder_path) if f.endswith(".png") and os.path.isfile(os.path.join(folder_path, f))])
        if not pngs:
            layers_path = os.path.join(folder_path, "layers")
            if os.path.isdir(layers_path):
                pngs = sorted([f for f in os.listdir(layers_path) if f.endswith(".png") and os.path.isfile(os.path.join(layers_path, f))])
                folder_path = layers_path
        if not pngs:
            cls._cache[name] = None
            return None
        surfaces = []
        for png in pngs:
            img = pygame.image.load(os.path.join(folder_path, png)).convert_alpha()
            surfaces.append(img)
        result = surfaces if len(surfaces) > 1 else surfaces[0]
        cls._cache[name] = result
        return result

    @classmethod
    def get(cls, name, frame=0):
        data = cls.load(name)
        if data is None:
            return None
        if isinstance(data, list):
            return data[frame % len(data)]
        return data

    @classmethod
    def get_scaled(cls, name, width, height, frame=0):
        surf = cls.get(name, frame)
        if surf is None:
            return None
        return pygame.transform.smoothscale(surf, (width, height))


class Layout:
    def __init__(self, win_w, win_h):
        self.win_w = win_w
        self.win_h = win_h
        self.cell = min(win_w * 0.3 / COLS, win_h * 0.7 / ROWS)
        self.cell = max(int(self.cell), 10)
        self.grid_w = COLS * self.cell
        self.grid_h = ROWS * self.cell
        self.grid_x = (win_w - self.grid_w) // 2
        self.grid_y = int(win_h * 0.08)
        self.hold_x = self.grid_x - self.cell * 5 - self.cell
        self.hold_y = self.grid_y
        self.hold_w = self.cell * 5
        self.hold_h = self.cell * 4 + self.cell
        self.next_x = self.grid_x + self.grid_w + self.cell
        self.next_y = self.grid_y
        self.next_w = self.cell * 5
        self.next_h = self.grid_h
        self.font_size = max(int(self.cell * 0.7), 12)
        self.small_font_size = max(int(self.cell * 0.5), 10)


class Piece:
    def __init__(self, x, y, shape_idx, color=None):
        self.x = x
        self.y = y
        self.shape_idx = shape_idx
        self.shape = [row[:] for row in SHAPES[shape_idx]]
        self.color = color if color is not None else COLORS[shape_idx]

    def rotate(self):
        self.shape = [list(row) for row in zip(*self.shape[::-1])]

    def rotate_ccw(self):
        self.shape = [list(row) for row in zip(*self.shape)][::-1]

    def unrotate(self):
        self.shape = [list(row) for row in zip(*self.shape)][::-1]


def parse_lrc(filepath):
    lyrics = []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                m = re.findall(r"\[(\d+):(\d+)\.(\d+)\]", line)
                if m:
                    text = re.sub(r"\[\d+:\d+\.\d+\]", "", line).strip()
                    for min_s, sec_s, cs_s in m:
                        t = int(min_s) * 60 + int(sec_s) + int(cs_s) / 100
                        lyrics.append((t, text))
    except Exception:
        pass
    lyrics.sort(key=lambda x: x[0])
    return lyrics


class MusicPlayer:
    def __init__(self):
        self.audio_path = None
        self.video_path = None
        self.lrc_path = None
        self.lyrics = []
        self.video_cap = None
        self.video_fps = 30
        self.video_duration = 0.0
        self.video_src_w = 0
        self.video_src_h = 0
        self._ffmpeg_proc = None
        self.playing = False
        self.start_time = 0.0
        self.current_lyric_idx = -1
        self.current_frame = None
        self._frame_id = 0
        self._bg_size = (640, 360)
        self.video_thread = None
        self._stop_video = threading.Event()
        self._volume = 80
        self._cached_duration = 0.0
        self.load_error = None
        self.video_load_error = None

    def load_audio(self, path):
        self.audio_path = path
        self.load_error = None
        self._cached_duration = 0.0
        try:
            pygame.mixer.music.load(path)
        except Exception:
            try:
                pygame.mixer.music.load(path, namehint="music.mp3")
            except Exception as e:
                self.load_error = str(e)
                return
        # Compute duration ONCE here (instead of every frame) to avoid
        # re-decoding the whole file repeatedly, which caused the freeze.
        try:
            info = pygame.mixer.Sound(path)
            self._cached_duration = info.get_length()
        except Exception:
            self._cached_duration = 0.0

    def load_video(self, path):
        self.video_path = path
        self.video_load_error = None
        if low_end:
            self.video_load_error = "Modo de bajo rendimiento: video desactivado"
            return
        if not HAS_CV2:
            self.video_load_error = "No se pudo preparar el modulo de video (revisa tu conexion a internet)"
            return
        if not path:
            return
        try:
            self.video_cap = cv2.VideoCapture(path)
            if not self.video_cap.isOpened():
                self.video_load_error = "No se pudo abrir el video (formato no soportado o archivo invalido)"
                self.video_cap = None
                return
            self.video_fps = self.video_cap.get(cv2.CAP_PROP_FPS) or 30
            frame_count = self.video_cap.get(cv2.CAP_PROP_FRAME_COUNT)
            self.video_duration = frame_count / self.video_fps if self.video_fps > 0 else 0
            self.video_src_w = int(self.video_cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 0
            self.video_src_h = int(self.video_cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 0
        except Exception as e:
            self.video_cap = None
            self.video_load_error = str(e)

    def load_lrc(self, path):
        self.lrc_path = path
        self.lyrics = parse_lrc(path) if path else []

    def get_duration(self):
        if self.video_duration > 0:
            return self.video_duration
        return self._cached_duration

    def play(self):
        self.playing = True
        self.start_time = _time.time()
        self.current_lyric_idx = -1
        # Evento nuevo por reproduccion: el hilo de video anterior conserva
        # el suyo ya marcado y termina solo. Antes se reutilizaba un
        # booleano y al reiniciar (stop + play) el hilo viejo podia seguir
        # vivo junto al nuevo, duplicando la decodificacion y la CPU.
        self._stop_video = threading.Event()

        def _start_playback():
            try:
                vol = self._volume / 100.0
                pygame.mixer.music.set_volume(vol)
                pygame.mixer.music.play()
                # El reloj del video arranca cuando el audio EMPIEZA de
                # verdad: antes se fijaba antes de spawnear el hilo, lo
                # que adelantaba el video respecto al audio.
                self.start_time = _time.time()
            except Exception:
                self.start_time = _time.time()
        # Starting playback can block briefly on some audio drivers; do it
        # off the main thread so it never causes a visible hitch.
        threading.Thread(target=_start_playback, daemon=True).start()

        if self.video_cap:
            # Capturar el tamano actual de la ventana para escalar el video
            # desde el arranque (antes el primer play usaba el valor por
            # defecto hasta que se dibujara el primer frame).
            surf = pygame.display.get_surface()
            if surf is not None:
                self._bg_size = surf.get_size()
            self._ffmpeg_failed = False
            self._fallback_started = False
            self._spawn_video_reader(self._stop_video)

    def stop(self):
        self.playing = False
        self._stop_video.set()
        proc = self._ffmpeg_proc
        if proc:
            self._ffmpeg_proc = None
            try:
                proc.kill()
            except Exception:
                pass
        try:
            pygame.mixer.music.stop()
        except Exception:
            pass
        self.video_thread = None
        self.current_frame = None

    def pause(self):
        try:
            pygame.mixer.music.pause()
        except Exception:
            pass

    def unpause(self):
        try:
            pygame.mixer.music.unpause()
        except Exception:
            pass

    def _audio_elapsed(self):
        """Tiempo REAL de reproduccion segun el mixer (la fuente de verdad
        para sincronizar el video y las letras); cae al reloj de pared si
        el mixer no reporta posicion."""
        try:
            pos = pygame.mixer.music.get_pos()
            if pos >= 0:
                return pos / 1000.0
        except Exception:
            pass
        return max(_time.time() - self.start_time, 0.0)

    def get_elapsed(self):
        if self.playing:
            return self._audio_elapsed()
        return 0.0

    def get_progress(self):
        dur = self.get_duration()
        if dur <= 0:
            return 0.0
        return min(self.get_elapsed() / dur, 1.0)

    def is_finished(self):
        """True cuando la cancion termino de sonar sola (no por un stop()
        manual del usuario). Se usa para detectar la victoria en el modo
        musical: sobrevivir hasta que termine el tema."""
        if not self.playing:
            return False
        elapsed = self._audio_elapsed()
        dur = self.get_duration()
        # Fallback por tiempo: si conocemos la duracion y ya la excedimos,
        # la cancion definitivamente termino aunque get_busy() no lo reporte.
        if dur > 0 and elapsed >= dur + 1.0:
            return True
        try:
            busy = pygame.mixer.music.get_busy()
        except Exception:
            busy = True
        if busy:
            return False
        # Umbral para no confundir el "aun no arranco" (el hilo que llama a
        # play() tarda un instante) con un final real de la cancion.
        return elapsed > 2.0 or (_time.time() - self.start_time) > 2.0

    def get_current_lyric(self):
        elapsed = self.get_elapsed()
        idx = -1
        for i, (t, _) in enumerate(self.lyrics):
            if elapsed >= t:
                idx = i
            else:
                break
        self.current_lyric_idx = idx
        if idx >= 0:
            return self.lyrics[idx][1]
        return ""

    def _spawn_ffmpeg_reader(self, out_w, out_h):
        """Lector de video via ffmpeg con aceleracion por GPU:
        -hwaccel auto usa la GPU dedicada o la integrada para decodificar
        (d3d11va/dxva2/qsv/cuda segun lo disponible; cae a software si no
        hay GPU). ffmpeg tambien hace el escalado y entrega RGB directo,
        asi que este hilo apenas gasta CPU (frombuffer es sin copia)."""
        if not HAS_NUMPY:
            return None
        ffmpeg = get_ffmpeg_exe()
        if not ffmpeg:
            return None
        fps = self.video_fps if self.video_fps and self.video_fps > 0 else 30
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-hwaccel", "auto", "-stream_loop", "-1",
            "-i", self.video_path,
            "-an", "-sn", "-dn",
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            # Escala + pre-oscurecer aqui: el frame llega listo para un
            # blit opaco, sin mezclas alfa en el hilo de render.
            "-vf", "scale=%d:%d,lutrgb=r=val*0.16:g=val*0.16:b=val*0.16" % (out_w, out_h),
            "-",
        ]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                bufsize=1024 * 1024
            )
        except Exception:
            return None
        return proc, fps

    def _ffmpeg_video_loop(self, stop_event, proc, fw, fh, fps):
        interval = 1.0 / max(fps, 1)
        frame_bytes = fw * fh * 3
        stream = proc.stdout
        next_t = _time.time()
        published = 0
        while not stop_event.is_set():
            try:
                # Sincronia con el audio: el frame objetivo sale del tiempo
                # REAL de reproduccion del mixer. Si una lectura lenta o un
                # microcorte nos deja atras, se descartan frames rapidamente
                # hasta alcanzar el audio (antes el ritmo se reiniciaba y el
                # retraso se acumulaba para siempre).
                target = int(self._audio_elapsed() * fps)
                behind = target - published
                if behind > 2:
                    for _ in range(min(behind, 240)):
                        d = stream.read(frame_bytes)
                        if not d or len(d) < frame_bytes:
                            break
                        published += 1
                    continue
                data = stream.read(frame_bytes)
            except Exception:
                break
            if not data or len(data) < frame_bytes:
                break
            try:
                frame = np.frombuffer(data, dtype=np.uint8).reshape((fh, fw, 3))
                self.current_frame = (frame, fw, fh)
                self._frame_id += 1
                published += 1
                # Ritmo de consumo al fps del video: ffmpeg decodifica tan
                # rapido como puede y queda bloqueado en el pipe hasta que
                # se consume el frame, sin gastar CPU de mas.
                next_t += interval
                delay = next_t - _time.time()
                if delay > 0:
                    _time.sleep(delay)
                else:
                    next_t = _time.time()
            except Exception:
                break
        try:
            proc.kill()
            proc.wait(timeout=1)
        except Exception:
            pass
        if published < 5 and not stop_event.is_set():
            self._ffmpeg_failed = True

    def _spawn_video_reader(self, stop_event):
        """Prefiere ffmpeg (decode por GPU); si no esta disponible, cae al
        lector de cv2 (decode por CPU)."""
        bg_w, bg_h = self._bg_size
        sw = self.video_src_w or bg_w
        sh = self.video_src_h or bg_h
        # Tope de resolucion: el video nunca se escala por encima de 480p
        # de alto (aunque la ventana sea grande): menos pixeles por el
        # pipe y menos trabajo de escala, sin diferencia visible en un
        # fondo al 16% de brillo.
        # Cover-fit: escala para CUBRIR toda la ventana (antes era
        # "contain", que dejaba franjas y hacia ver el video chico/con
        # bandas). No se puede topar la resolucion como antes (eso
        # dejaria huecos sin cubrir), pero al recortar el sobrante en el
        # blit el resultado sigue siendo del tamano de la ventana.
        scale = max(bg_w / sw, bg_h / sh)
        out_w = max(int(sw * scale), 2)
        out_h = max(int(sh * scale), 2)
        spawned = self._spawn_ffmpeg_reader(out_w, out_h)
        if spawned:
            proc, fps = spawned
            self._ffmpeg_proc = proc
            self.video_thread = threading.Thread(
                target=self._ffmpeg_video_loop,
                args=(stop_event, proc, out_w, out_h, fps),
                daemon=True,
            )
            self.video_thread.start()
            return
        self.video_thread = threading.Thread(target=self._video_loop, args=(stop_event,), daemon=True)
        self.video_thread.start()

    def _video_loop(self, stop_event):
        fps = self.video_fps if self.video_fps and self.video_fps > 0 else 30
        frame_interval = 1.0 / fps
        total = int(self.video_duration * fps) if self.video_duration > 0 else 0
        pos = 0
        lock = threading.Lock()
        while not stop_event.is_set() and self.video_cap:
            loop_start = _time.time()
            try:
                # Sincronizar con el tiempo real de reproduccion: el frame
                # objetivo sale del tiempo transcurrido desde play(), no de
                # "un frame por tick". Antes se leia a 15fps fijos, asi que
                # un video de 30fps se veia a mitad de velocidad (lento).
                elapsed = self._audio_elapsed()
                target = int(elapsed * fps)
                if total > 0:
                    target %= total
                if target != pos:
                    gap = target - pos
                    if gap < 0 or gap > 60:
                        self.video_cap.set(cv2.CAP_PROP_POS_FRAMES, target)
                    elif gap > 1:
                        for _ in range(gap - 1):
                            self.video_cap.read()
                    pos = target
                ret, frame = self.video_cap.read()
                if not ret:
                    if pos > 10 and total == 0:
                        total = pos
                    pos = 0
                    self.video_cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                pos += 1
                # Escalar directamente al tamano de la ventana AQUI, en el
                # hilo de fondo: antes el hilo de render hacia
                # transform.scale por cada frame nuevo, causando tirones.
                bg_w, bg_h = self._bg_size
                h, w = frame.shape[:2]
                # Cover-fit igual que el lector de ffmpeg: cubrir toda la
                # ventana en vez de caber adentro con bandas.
                scale = max(bg_w / w, bg_h / h)
                new_w = max(int(w * scale), 1)
                new_h = max(int(h * scale), 1)
                if (new_w, new_h) != (w, h):
                    frame = cv2.resize(frame, (new_w, new_h))
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                # Pre-oscurecer igual que ffmpeg (lutrgb val*0.16) para que
                # el blit opaco de draw_video_bg se vea identico en ambos
                # lectores.
                frame = cv2.convertScaleAbs(frame, alpha=0.16, beta=0)
                with lock:
                    self.current_frame = (frame, new_w, new_h)
                    self._frame_id += 1
            except Exception:
                break
            spent = _time.time() - loop_start
            _time.sleep(max(0.004, frame_interval - spent))

    def draw_video_bg(self, screen, win_w, win_h):
        self._bg_size = (win_w, win_h)
        if low_end:
            return
        # Si el lector de ffmpeg murio sin producir casi nada (ffmpeg sin
        # soporte del codec, archivo corrupto...), arranca el fallback de
        # cv2 una sola vez.
        if (getattr(self, '_ffmpeg_failed', False) and self.video_cap
                and not getattr(self, '_fallback_started', False)):
            self._fallback_started = True
            self.video_thread = threading.Thread(target=self._video_loop, args=(self._stop_video,), daemon=True)
            self.video_thread.start()
        if not self.current_frame:
            return
        frame, fw, fh = self.current_frame
        # La superficie ya llega escalada al tamano de la ventana desde el
        # hilo de video; aqui solo se convierte a Surface y se blitea.
        # El tope de reconstruccion (~30fps) acota la carga si el video
        # fuente va a mas fps; el frame mostrado siempre es el actual,
        # asi que la velocidad de reproduccion no cambia.
        now = _time.perf_counter()
        key = (self._frame_id, fw, fh, win_w, win_h)
        if (getattr(self, '_cached_video_key', None) != key
                and now - getattr(self, '_last_video_build', 0.0) >= 0.033):
            # El frame llega pre-oscurecido (ffmpeg hace lutrgb val*0.16,
            # o el lector de cv2 lo atenua), asi que el blit es opaco:
            # una copia directa en vez de una mezcla alfa por pixel.
            surf = pygame.image.frombuffer(frame.tobytes(), (fw, fh), "RGB")
            self._cached_video_surf = surf
            self._cached_video_key = key
            self._last_video_build = now
        surf = getattr(self, '_cached_video_surf', None)
        if surf is None:
            return
        # El frame ahora viene escalado en modo "cover" (siempre >= al
        # tamano de ventana en ambos ejes), asi que centrar y dejar que
        # pygame recorte lo que sobra fuera de pantalla llena la ventana
        # por completo, sin bandas ni franjas.
        x = (win_w - surf.get_width()) // 2
        y = (win_h - surf.get_height()) // 2
        screen.blit(surf, (x, y))

    def draw_lyrics(self, screen, win_w, win_h, font):
        text = self.get_current_lyric()
        if not text:
            return
        surf = get_text(text, (255, 255, 255), font)
        shadow = get_text(text, (0, 0, 0), font)
        x = win_w // 2 - surf.get_width() // 2
        y = win_h - int(win_h * 0.12)
        screen.blit(shadow, (x + 2, y + 2))
        screen.blit(surf, (x, y))

    def draw_progress_bar(self, screen, win_w, win_h):
        bar_w = int(win_w * 0.3)
        bar_h = 4
        bar_x = win_w // 2 - bar_w // 2
        bar_y = win_h - int(win_h * 0.06)
        pygame.draw.rect(screen, (40, 40, 60), (bar_x, bar_y, bar_w, bar_h), border_radius=2)
        prog = self.get_progress()
        pygame.draw.rect(screen, (100, 200, 255), (bar_x, bar_y, int(bar_w * prog), bar_h), border_radius=2)
        elapsed = self.get_elapsed()
        dur = self.get_duration()
        e_m, e_s = divmod(int(elapsed), 60)
        d_m, d_s = divmod(int(dur), 60)
        time_str = f"{e_m}:{e_s:02d}/{d_m}:{d_s:02d}"
        time_surf = get_font(14).render(time_str, True, (150, 160, 180))
        screen.blit(time_surf, (bar_x + bar_w + 8, bar_y - 3))


def extract_audio_from_video(path):
    """Extract the audio track from a video file into a temp mp3 using ffmpeg.
    pygame.mixer.music cannot decode audio directly out of a video container
    (mp4/mkv/etc.), so this is required for the video files loaded in music mode.
    Returns the temp file path on success, or None if ffmpeg couldn't be
    obtained (e.g. no internet on first run) or extraction failed.
    """
    ffmpeg_exe = get_ffmpeg_exe()
    if not ffmpeg_exe:
        return None
    try:
        out_path = os.path.join(tempfile.gettempdir(), "tepy_music_audio.mp3")
        cmd = [
            ffmpeg_exe, "-y", "-i", path,
            "-vn", "-acodec", "libmp3lame", "-q:a", "2",
            out_path,
        ]
        result = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180
        )
        if result.returncode == 0 and os.path.exists(out_path):
            return out_path
    except Exception:
        pass
    return None


def open_file_dialog(title, filetypes):
    if not HAS_TKINTER:
        return None
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(title=title, filetypes=filetypes)
        root.destroy()
        return path if path else None
    except Exception:
        return None


class Chat:
    """Chat de la sala multijugador. Vive una sola instancia por sesion
    (no por partida), asi el historial sobrevive a reinicios de ronda y
    tambien funciona en la sala de espera antes de que arranque el juego."""

    def __init__(self, max_lines=8, max_chars=100):
        self.messages = []  # (nombre, mensaje, tiempo, es_mio)
        self.input = ""
        self.max_lines = max_lines
        self.max_chars = max_chars
        self.input_active = False
        self.cursor_timer = 0.0
        self.cursor_visible = True
        self.unread = False

    def add_message(self, name, message, mine=False):
        if not message:
            return
        self.messages.append((name, message, _time.time(), mine))
        if len(self.messages) > self.max_lines:
            self.messages = self.messages[-self.max_lines:]
        if not mine and not self.input_active:
            self.unread = True

    def clear(self):
        self.messages = []
        self.input = ""
        self.input_active = False
        self.unread = False

    def start_typing(self):
        self.input_active = True
        self.input = ""
        self.unread = False

    def stop_typing(self):
        self.input_active = False
        self.input = ""

    def add_char(self, ch):
        if ch and ch.isprintable() and len(self.input) < self.max_chars:
            self.input += ch

    def backspace(self):
        self.input = self.input[:-1]

    def send(self, network, global_chat_mode=False):
        text = self.input.strip()
        if text and network is not None:
            if global_chat_mode:
                network.send_global_chat(text[:self.max_chars])
            else:
                network.send_chat(text[:self.max_chars])
            self.add_message("Tu", text[:self.max_chars], mine=True)
        self.input = ""
        self.input_active = False

    def update(self, dt):
        self.cursor_timer += dt
        if self.cursor_timer > 0.5:
            self.cursor_visible = not self.cursor_visible
            self.cursor_timer = 0.0

    def draw(self, screen, layout, font, time_=0.0):
        # Cuando no hay nada nuevo que mostrar y nadie esta escribiendo,
        # se reduce a una pildora chica en la esquina en vez de tapar el
        # tablero con un panel grande todo el tiempo.
        has_recent = bool(self.messages) and (_time.time() - self.messages[-1][2]) < 6.0
        pad = 8

        if not self.input_active and not has_recent:
            hint_font = get_font(max(int(layout.cell * 0.32), 11))
            if self.unread:
                hint_text, hint_color = "Mensaje nuevo - T", (255, 210, 90)
            else:
                hint_text, hint_color = "T para chatear", (150, 160, 180)
            hint = hint_font.render(hint_text, True, hint_color)
            box_w, box_h = hint.get_width() + pad * 2 + (10 if self.unread else 0), hint.get_height() + pad
            box_x = int(layout.cell)
            box_y = layout.win_h - box_h - int(layout.cell * 0.5)
            box = get_overlay(box_w, box_h, (10, 12, 20), 160)
            screen.blit(box, (box_x, box_y))
            screen.blit(hint, (box_x + pad, box_y + box_h // 2 - hint.get_height() // 2))
            if self.unread:
                pulse = (math.sin(time_ * 6) + 1) / 2
                pygame.draw.circle(screen, (int(230 + pulse * 20), 90, 90), (box_x + box_w - 8, box_y + box_h // 2), 4)
            return

        panel_w = min(int(layout.win_w * 0.34), 420)
        panel_h = int(layout.cell * 5.0)
        panel_x = int(layout.cell)
        panel_y = layout.win_h - panel_h - int(layout.cell * 0.5)

        bg_surf = get_gradient(panel_w, panel_h, (16, 18, 28), (24, 27, 40), 215)
        screen.blit(bg_surf, (panel_x, panel_y))
        border_col = (90, 160, 220) if self.input_active else (55, 60, 78)
        pygame.draw.rect(screen, border_col, (panel_x, panel_y, panel_w, panel_h), 1, border_radius=8)

        msg_font = get_font(max(int(layout.cell * 0.3), 11))
        line_h = msg_font.get_height() + 2
        input_h = msg_font.get_height() + 12
        msg_area_bottom = panel_y + panel_h - input_h - 6
        cy = msg_area_bottom - line_h
        now = _time.time()
        for name, msg, t, mine in reversed(self.messages):
            if cy < panel_y + 4:
                break
            color = (130, 200, 255) if mine else (215, 220, 230)
            label = "Tu" if mine else name
            line = f"{label}: {msg}"
            if len(line) > 60:
                line = line[:57] + "..."
            surf = msg_font.render(line, True, color)
            age = now - t
            alpha = 255 if age < 4 else max(80, int(255 - (age - 4) * 60))
            surf.set_alpha(alpha)
            screen.blit(surf, (panel_x + pad, cy))
            cy -= line_h

        in_y = panel_y + panel_h - input_h - 4
        in_bg = (22, 25, 36) if self.input_active else (16, 18, 26)
        pygame.draw.rect(screen, in_bg, (panel_x + pad, in_y, panel_w - pad * 2, input_h), border_radius=5)
        if self.input_active:
            pygame.draw.rect(screen, (90, 160, 220), (panel_x + pad, in_y, panel_w - pad * 2, input_h), 1, border_radius=5)
            txt = self.input + ("|" if self.cursor_visible else "")
            in_surf = msg_font.render(txt if txt else " ", True, WHITE)
        else:
            in_surf = msg_font.render("Pulsa T para escribir", True, (110, 120, 140))
        screen.blit(in_surf, (panel_x + pad + 6, in_y + input_h // 2 - in_surf.get_height() // 2))


class Tetris:
    def __init__(self, settings=None):
        if settings is None:
            settings = {}
        self.settings = settings
        self.grid = [[None for _ in range(COLS)] for _ in range(ROWS)]
        # Bolsa de piezas (estilo "random bag" de los Tetris modernos):
        # se baraja un set con una de cada pieza y se van repartiendo en
        # ese orden; al vaciarse se vuelve a barajar. Con random puro
        # (random.randint en cada pieza) podian salir rachas largas de la
        # misma pieza o sequias larguisimas de otra, que es lo que se
        # siente como "previsible"/injusto. Ademas evitamos que el ultimo
        # de una bolsa y el primero de la siguiente sean la misma pieza,
        # que el 7-bag clasico si permite.
        self._piece_bag = []
        self._last_piece_idx = None
        next_count = settings.get("next_count", 5)
        self.next_queue = [self.new_piece() for _ in range(next_count)]
        self.current = self.next_queue.pop(0)
        self.current.x = COLS // 2 - len(self.current.shape[0]) // 2
        self.hold_piece = None
        self.can_hold = True
        self.game_over = False
        self.score = 0
        self.lines = 0
        self.level = settings.get("start_level", 1)
        self.fall_time = 0
        self.fall_speed = max(50, 500 - (self.level - 1) * 40)
        self.lock_time = 0
        self.lock_delay = 500
        self.locking = False
        self.lock_moves = 0
        self.max_lock_moves = 15
        self.floating_text = FloatingText()
        self.line_clear_anim = LineClearAnim()
        self.lock_squash = []
        self.hard_drop_trail = []

    def _refill_piece_bag(self):
        bag = list(range(len(SHAPES)))
        random.shuffle(bag)
        if self._last_piece_idx is not None and bag[0] == self._last_piece_idx and len(bag) > 1:
            # Rompe la racha "ultima del bag anterior = primera del nuevo"
            # sin sesgar el resto: la cambiamos de lugar con otra al azar.
            swap_with = random.randint(1, len(bag) - 1)
            bag[0], bag[swap_with] = bag[swap_with], bag[0]
        self._piece_bag = bag

    def new_piece(self):
        if not self._piece_bag:
            self._refill_piece_bag()
        idx = self._piece_bag.pop(0)
        self._last_piece_idx = idx
        color = self.settings.get("piece_colors", COLORS)[idx] if self.settings.get("piece_colors") else COLORS[idx]
        return Piece(COLS // 2 - 1, 0, idx, color)

    def valid(self, piece, adj_x=0, adj_y=0):
        for y, row in enumerate(piece.shape):
            for x, cell in enumerate(row):
                if cell:
                    nx = piece.x + x + adj_x
                    ny = piece.y + y + adj_y
                    if nx < 0 or nx >= COLS or ny >= ROWS:
                        return False
                    if ny >= 0 and self.grid[ny][nx] is not None:
                        return False
        return True

    def lock(self):
        landed_cells = []
        for y, row in enumerate(self.current.shape):
            for x, cell in enumerate(row):
                if cell:
                    ny = self.current.y + y
                    nx = self.current.x + x
                    if 0 <= ny < ROWS and 0 <= nx < COLS:
                        self.grid[ny][nx] = self.current.color
                        landed_cells.append((nx, ny))
        # Animacion de "asentado": cada bloque que acaba de caer rebota
        # brevemente (ver draw_grid). t va de 1.0 a 0.0 en update().
        self.lock_squash.extend({"x": x, "y": y, "t": 1.0} for x, y in landed_cells)

        cleared_rows = []
        for y in range(ROWS):
            if all(self.grid[y]):
                cleared_rows.append(y)

        cleared = self.clear_lines()
        self.lines += cleared
        base_points = [0, 100, 300, 500, 800][cleared] * self.level
        self.score += base_points

        if cleared_rows and hasattr(self, 'effects') and hasattr(self, '_grid_cx'):
            self.line_clear_anim.trigger(cleared_rows, self._grid_x, self._grid_w, self._grid_cell)
            if cleared >= 4:
                sfx.play("tetris")
            else:
                sfx.play("line_clear")
            self.effects.shake(2 + cleared * 2)
            colors = [(100, 200, 255), (255, 200, 100), (200, 100, 255), (100, 255, 150)]
            self.effects.flash(colors[cleared % len(colors)], 30 + cleared * 15)
            for row in cleared_rows:
                for cx in range(COLS):
                    color = self.grid[row][cx] if row < ROWS else (200, 200, 200)
                    if color is None:
                        color = (150, 150, 150)
                    self.effects.add_particles(
                        self._grid_x + cx * self._grid_cell + self._grid_cell // 2,
                        self._grid_y + row * self._grid_cell + self._grid_cell // 2,
                        color, 2
                    )

        combo_pts = 0
        if hasattr(self, 'combo') and cleared > 0:
            combo_pts = self.combo.add_line_clear(cleared, self.level)
            self.score += combo_pts
            if self.combo.rank_scale > 2.0:
                sfx.play("rank_up")
            elif self.combo.count > 0:
                sfx.play_combo(self.combo.count)
            if hasattr(self, 'effects'):
                self.effects.shake(2 + cleared * 2 + self.combo.count * 0.3)
                cx = self.settings.get("_grid_cx", 400)
                cy = self.settings.get("_grid_cy", 300)
                self.effects.add_particles(cx, cy, self.combo.rank_color, 3 + cleared * 2)

        # El texto flotante muestra EXACTAMENTE lo que se sumo al score
        # (base + combo), calculado despues de aplicar el combo, no antes.
        if cleared_rows and hasattr(self, 'effects') and hasattr(self, '_grid_cx'):
            total_pts = base_points + combo_pts
            self.floating_text.add(
                self._grid_cx, self._grid_y + cleared_rows[0] * self._grid_cell,
                f"+{total_pts:,}", (255, 255, 100), size=18 + cleared * 4
            )

        new_level = self.lines // 10 + 1
        if new_level > self.level:
            sfx.play("level_up")
        self.level = new_level
        self.fall_speed = max(50, 500 - (self.level - 1) * 40)
        self.current = self.next_queue.pop(0)
        self.current.x = COLS // 2 - len(self.current.shape[0]) // 2
        self.current.y = 0
        while len(self.next_queue) < self.settings.get("next_count", 5):
            self.next_queue.append(self.new_piece())
        self.locking = False
        self.lock_time = 0
        self.lock_moves = 0
        self.can_hold = True
        if not self.valid(self.current):
            self.game_over = True
            sfx.play("game_over")

    def clear_lines(self):
        cleared = 0
        y = ROWS - 1
        while y >= 0:
            if all(self.grid[y]):
                del self.grid[y]
                self.grid.insert(0, [None for _ in range(COLS)])
                cleared += 1
            else:
                y -= 1
        return cleared

    def move(self, dx):
        if self.valid(self.current, adj_x=dx):
            self.current.x += dx
            self.reset_lock()
            sfx.play("move")
            return True
        return False

    def rotate_piece(self):
        self.current.rotate()
        if not self.valid(self.current):
            for off in [0, -1, 1, -2, 2]:
                if self.valid(self.current, adj_x=off):
                    self.current.x += off
                    self.reset_lock()
                    sfx.play("rotate")
                    return
            self.current.unrotate()
        else:
            self.reset_lock()
            sfx.play("rotate")

    def rotate_ccw_piece(self):
        self.current.rotate_ccw()
        if not self.valid(self.current):
            for off in [0, 1, -1, 2, -2]:
                if self.valid(self.current, adj_x=off):
                    self.current.x += off
                    self.reset_lock()
                    sfx.play("rotate")
                    return
            self.current.rotate()
        else:
            self.reset_lock()
            sfx.play("rotate")

    def reset_lock(self):
        if self.locking and self.lock_moves < self.max_lock_moves:
            self.lock_time = 0
            self.lock_moves += 1

    def hard_drop(self):
        drop_dist = 0
        while self.valid(self.current, adj_y=1):
            self.current.y += 1
            self.score += 2
            drop_dist += 1
        if drop_dist > 0:
            sfx.play("hard_drop")
        else:
            sfx.play("drop")
        if hasattr(self, 'effects') and drop_dist > 0:
            self.effects.shake(min(3 + drop_dist * 0.5, 12))
            if hasattr(self, '_grid_cx'):
                for y, row in enumerate(self.current.shape):
                    for x, cell in enumerate(row):
                        if cell:
                            px = self._grid_x + (self.current.x + x) * self._grid_cell + self._grid_cell // 2
                            py = self._grid_y + (self.current.y + y) * self._grid_cell + self._grid_cell
                            self.effects.add_particles(px, py, self.current.color, 3)
        self.lock()

    def soft_drop(self):
        if self.valid(self.current, adj_y=1):
            self.current.y += 1
            self.score += 1
            sfx.play("soft_drop")
            return True
        return False

    def do_hold(self):
        if not self.can_hold:
            return
        self.can_hold = False
        sfx.play("hold")
        pc = self.settings.get("piece_colors", COLORS)
        if self.hold_piece is None:
            self.hold_piece = Piece(0, 0, self.current.shape_idx, pc[self.current.shape_idx])
            self.current = self.next_queue.pop(0)
            self.current.x = COLS // 2 - len(self.current.shape[0]) // 2
            self.current.y = 0
            while len(self.next_queue) < self.settings.get("next_count", 5):
                self.next_queue.append(self.new_piece())
        else:
            old_idx = self.hold_piece.shape_idx
            self.hold_piece = Piece(0, 0, self.current.shape_idx, pc[self.current.shape_idx])
            self.current = Piece(COLS // 2 - 1, 0, old_idx, pc[old_idx])
        self.locking = False
        self.lock_time = 0
        self.lock_moves = 0

    def ghost_y(self):
        gy = self.current.y
        while self.valid(self.current, adj_y=gy - self.current.y + 1):
            gy += 1
        return gy

    def update(self, dt):
        if self.lock_squash:
            decay = dt / 180.0
            for s in self.lock_squash:
                s["t"] -= decay
            self.lock_squash = [s for s in self.lock_squash if s["t"] > 0]
        if self.game_over:
            return
        self.fall_time += dt
        if self.locking:
            self.lock_time += dt
            if self.lock_time >= self.lock_delay:
                self.lock()
                return
        if self.fall_time >= self.fall_speed:
            self.fall_time = 0
            if not self.valid(self.current, adj_y=1):
                self.locking = True
            else:
                self.current.y += 1
                self.locking = False
                self.lock_time = 0


class BotAI:
    """IA para el modo VS: evalua todas las rotaciones y columnas con
    una heuristica clasica (lineas, altura, huecos, bumpiness) y coloca
    la mejor pieza directamente. El ritmo lo marca think_delay (ms)
    para que no juegue instantaneo."""

    W_LINES = 0.76
    W_HEIGHT = -0.51
    W_HOLES = -0.36
    W_BUMP = -0.18

    def __init__(self, think_delay=600):
        self.think_delay = think_delay
        self.timer = 0.0

    def _valid(self, grid, piece, adj_y=0):
        for y, row in enumerate(piece.shape):
            for x, cell in enumerate(row):
                if cell:
                    nx = piece.x + x
                    ny = piece.y + y + adj_y
                    if nx < 0 or nx >= COLS or ny >= ROWS:
                        return False
                    if ny >= 0 and grid[ny][nx] is not None:
                        return False
        return True

    def _evaluate(self, sim):
        heights = []
        holes = 0
        for x in range(COLS):
            col_top = None
            for y in range(ROWS):
                if sim[y][x] is not None:
                    col_top = y
                    break
            if col_top is None:
                heights.append(0)
            else:
                heights.append(ROWS - col_top)
                holes += sum(1 for y in range(col_top + 1, ROWS) if sim[y][x] is None)
        bump = sum(abs(heights[i] - heights[i + 1]) for i in range(COLS - 1))
        return heights, holes, bump

    def _simulate(self, grid, shape_idx, color, rotations, target_x):
        piece = Piece(target_x, 0, shape_idx, color)
        for _ in range(rotations):
            piece.rotate()
        if not self._valid(grid, piece):
            return None
        while self._valid(grid, piece, adj_y=1):
            piece.y += 1
        sim = [row[:] for row in grid]
        for y, row in enumerate(piece.shape):
            for x, cell in enumerate(row):
                if cell and 0 <= piece.y + y < ROWS and 0 <= piece.x + x < COLS:
                    sim[piece.y + y][piece.x + x] = color
        cleared = 0
        y = ROWS - 1
        while y >= 0:
            if all(sim[y]):
                del sim[y]
                sim.insert(0, [None] * COLS)
                cleared += 1
            else:
                y -= 1
        heights, holes, bump = self._evaluate(sim)
        return (self.W_LINES * cleared + self.W_HEIGHT * sum(heights)
                + self.W_HOLES * holes + self.W_BUMP * bump)

    def play_piece(self, game):
        cur = game.current
        best = None
        best_score = None
        for rotations in range(4):
            w = len(cur.shape[0]) if rotations % 2 == 0 else len(cur.shape)
            for tx in range(0, COLS - w + 1):
                score = self._simulate(game.grid, cur.shape_idx, cur.color, rotations, tx)
                if score is not None and (best_score is None or score > best_score):
                    best_score = score
                    best = (rotations, tx)
        if best:
            for _ in range(best[0]):
                cur.rotate()
            cur.x = best[1]
        # Subir la pieza hasta una posicion valida (la simulacion cae
        # desde arriba; la pieza real puede estar a media caida) y soltar.
        while not game.valid(cur):
            cur.y -= 1
        while game.valid(cur, adj_y=1):
            cur.y += 1
        game.lock()

    def update(self, dt, game):
        if game.game_over:
            return
        self.timer += dt
        if self.timer < self.think_delay:
            return
        self.timer = 0.0
        self.play_piece(game)


class InputHandler:
    def __init__(self, keys=None):
        self.keys_held = {}
        self.das_timers = {}
        self.arr_timers = {}
        self.keys = keys or {}

    def update(self, dt, game, das_delay, arr_delay):
        left_key = self.keys.get("left", pygame.K_LEFT)
        right_key = self.keys.get("right", pygame.K_RIGHT)
        for key in [left_key, right_key]:
            if self.keys_held.get(key, False):
                self.das_timers[key] = self.das_timers.get(key, 0) + dt
                if self.das_timers[key] >= das_delay:
                    self.arr_timers[key] = self.arr_timers.get(key, 0) + dt
                    while self.arr_timers[key] >= arr_delay:
                        self.arr_timers[key] -= arr_delay
                        dx = -1 if key == left_key else 1
                        if not game.move(dx):
                            break
        soft_key = self.keys.get("soft_drop", pygame.K_DOWN)
        if self.keys_held.get(soft_key, False):
            game.soft_drop()

    def on_key_down(self, key, game):
        self.keys_held[key] = True
        left_key = self.keys.get("left", pygame.K_LEFT)
        right_key = self.keys.get("right", pygame.K_RIGHT)
        soft_key = self.keys.get("soft_drop", pygame.K_DOWN)
        if key in [left_key, right_key]:
            self.das_timers[key] = 0
            self.arr_timers[key] = 0
            dx = -1 if key == left_key else 1
            game.move(dx)

    def on_key_up(self, key):
        self.keys_held[key] = False
        self.das_timers.pop(key, None)
        self.arr_timers.pop(key, None)


class GamepadHandler:
    BUTTON_MAP = {
        0: "a",
        1: "b",
        2: "x",
        3: "y",
        4: "back",
        5: "guide",
        6: "start",
        7: "left_stick",
        8: "right_stick",
        9: "left_bumper",
        10: "right_bumper",
    }
    REVERSE_MAP = {v: k for k, v in BUTTON_MAP.items()}
    # Nombres estandar de la API de controles de SDL2, por id crudo
    SDL2_BTN_NAMES = {
        "a": "a", "b": "b", "x": "x", "y": "y",
        "back": "back", "guide": "guide", "start": "start",
        "left_stick": "leftstick", "right_stick": "rightstick",
        "left_bumper": "leftshoulder", "right_bumper": "rightshoulder",
    }

    GAMEPLAY_MAP = {
        "a": "rotate",
        "b": "rotate_ccw",
        "x": "hard_drop",
        "y": "hold",
        "left_bumper": "rotate_ccw",
        "right_bumper": "rotate",
        "start": "restart",
    }

    MENU_MAP = {
        "a": "confirm",
        "b": "back",
        "start": "confirm",
    }

    def __init__(self):
        self.joy = None
        self.ctrl = None
        self.using_sdl2 = False
        self.joy_name = ""
        self.connected = False
        self.stick_deadzone = 0.3
        self.stick_release = 0.15
        self._stick_active_x = False
        self._stick_active_y = False
        self.dpad_state = (0, 0)
        self._prev_dpad = (0, 0)
        self._prev_buttons = {}
        self._menu_repeat_timer = 0.0
        # Antes 180ms de espera inicial y 60ms entre repeticiones (~16
        # items/seg); se sentia lento/pesado para recorrer listas largas.
        self._menu_repeat_delay = 110.0
        self._menu_repeat_rate = 35.0
        self._menu_held_dir = None
        self._menu_held_time = 0.0

    # Etiquetas y colores de los botones de accion segun la marca del
    # control, para que el HUD en pantalla muestre el simbolo correcto
    # (A/B/X/Y en Xbox, la cruz/circulo/cuadrado/triangulo en PlayStation)
    # en vez de siempre "A/B/X/Y" aunque el jugador tenga un DualSense.
    BUTTON_GLYPHS = {
        "xbox": {
            "a": ("A", (110, 200, 110)), "b": ("B", (220, 90, 90)),
            "x": ("X", (90, 140, 220)), "y": ("Y", (220, 190, 80)),
            "left_bumper": ("LB", (200, 200, 210)), "right_bumper": ("RB", (200, 200, 210)),
            "start": ("MENU", (200, 200, 210)),
        },
        "playstation": {
            "a": ("\u2715", (110, 150, 230)),   # cruz (X)
            "b": ("\u25cb", (230, 110, 130)),   # circulo
            "x": ("\u25a1", (230, 150, 220)),   # cuadrado
            "y": ("\u25b3", (110, 210, 160)),   # triangulo
            "left_bumper": ("L1", (200, 200, 210)), "right_bumper": ("R1", (200, 200, 210)),
            "start": ("OPTIONS", (200, 200, 210)),
        },
        "generic": {
            "a": ("1", (110, 200, 110)), "b": ("2", (220, 90, 90)),
            "x": ("3", (90, 140, 220)), "y": ("4", (220, 190, 80)),
            "left_bumper": ("L1", (200, 200, 210)), "right_bumper": ("R1", (200, 200, 210)),
            "start": ("START", (200, 200, 210)),
        },
    }

    def brand(self):
        """Detecta la marca del control por su nombre (el que reporta el
        driver via SDL2) para saber que set de simbolos mostrar."""
        name = (self.joy_name or "").lower()
        if any(k in name for k in ("playstation", "dualshock", "dualsense", "sony", "ps3", "ps4", "ps5", "wireless controller")):
            # "Wireless Controller" a secas (sin marca) es como Linux/SDL
            # suele reportar los DualShock 4 por bluetooth.
            return "playstation"
        if "xbox" in name or "xinput" in name:
            return "xbox"
        return "generic"

    def glyph(self, action):
        """(texto, color) del boton fisico que dispara `action`
        ("a","b","x","y","left_bumper","right_bumper","start"), segun la
        marca detectada del control conectado."""
        table = self.BUTTON_GLYPHS.get(self.brand(), self.BUTTON_GLYPHS["generic"])
        return table.get(action, (action.upper()[:2], (200, 200, 210)))

    def connect(self):
        if self.ctrl:
            try:
                self.ctrl.quit()
            except Exception:
                pass
            self.ctrl = None
        if self.joy:
            try:
                self.joy.quit()
            except Exception:
                pass
            self.joy = None
        try:
            pygame.joystick.quit()
            pygame.joystick.init()
        except Exception:
            pass
        if HAS_SDL2_CONTROLLER:
            try:
                _sdl2_controller.init()
            except Exception:
                pass
        count = pygame.joystick.get_count()
        if count > 0:
            # Preferir la API estandar de controles de SDL2: mapea igual a
            # Xbox, PlayStation y genericos DirectInput (el joystick crudo
            # asume un orden de botones fijo que no coincide en muchos
            # controles). Si el control no esta en la base de SDL2, cae al
            # joystick crudo.
            if HAS_SDL2_CONTROLLER:
                try:
                    if _sdl2_controller.is_controller(0):
                        self.ctrl = _sdl2_controller.Controller(0)
                except Exception:
                    self.ctrl = None
            try:
                self.joy = pygame.joystick.Joystick(0)
                if not self.joy.get_init():
                    self.joy.init()
                self.joy_name = self.joy.get_name()
                self.connected = True
                self.using_sdl2 = self.ctrl is not None
                return True
            except Exception:
                self.joy = None
                self.ctrl = None
                self.connected = False
                self.using_sdl2 = False
                self.joy_name = ""
                return False
        self.joy = None
        self.ctrl = None
        self.connected = False
        self.using_sdl2 = False
        self.joy_name = ""
        return False

    def disconnect(self):
        if self.ctrl:
            try:
                self.ctrl.quit()
            except Exception:
                pass
        if self.joy:
            try:
                self.joy.quit()
            except Exception:
                pass
        self.joy = None
        self.ctrl = None
        self.connected = False
        self.using_sdl2 = False
        self.joy_name = ""

    def get_dpad(self):
        if not self.connected:
            return (0, 0)
        # API estandar SDL2: el D-pad llega como botones dpup/dpdown/
        # dpleft/dpright (muchos controles no lo reportan como hat).
        if self.ctrl is not None:
            try:
                dx = (1 if self.ctrl.get_button("dpright") else 0) - (1 if self.ctrl.get_button("dpleft") else 0)
                dy = (1 if self.ctrl.get_button("dpdown") else 0) - (1 if self.ctrl.get_button("dpup") else 0)
                if dx or dy:
                    return (dx, dy)
                # Antes, si el D-pad estaba centrado, se caia directo al
                # bloque de mas abajo que lee self.joy.get_axis(0/1) "en
                # crudo": en un control mapeado por SDL2 (Xbox/PS/etc) esos
                # indices de eje NO tienen por que ser el stick izquierdo
                # (puede ser el gatillo, el stick derecho, o venir invertido
                # segun el driver), lo que se sentia como "el stick anda
                # raro". Usando los ejes YA mapeados por el Controller
                # (leftx/lefty) el resultado es consistente sin importar
                # la marca o el orden de ejes que reporte el hardware.
                lx = self.ctrl.get_axis(0) / 32768.0   # CONTROLLER_AXIS_LEFTX
                ly = self.ctrl.get_axis(1) / 32768.0   # CONTROLLER_AXIS_LEFTY
                if self._stick_active_x:
                    self._stick_active_x = abs(lx) > self.stick_release
                else:
                    self._stick_active_x = abs(lx) > self.stick_deadzone
                if self._stick_active_y:
                    self._stick_active_y = abs(ly) > self.stick_release
                else:
                    self._stick_active_y = abs(ly) > self.stick_deadzone
                sdx = (1 if lx > 0 else -1) if self._stick_active_x else 0
                sdy = (1 if ly > 0 else -1) if self._stick_active_y else 0
                if sdx or sdy:
                    return (sdx, sdy)
                return (0, 0)
            except Exception:
                pass
        if self.joy:
            # Hat primero; si esta centrado, usar el stick izquierdo como
            # fallback. Antes el stick se ignoraba por completo cuando el
            # control tenia hat, asi que el eje Y del stick nunca hacia
            # nada y solo se podia mover a los lados con el stick.
            if self.joy.get_numhats() > 0:
                hat = self.joy.get_hat(0)
                if hat != (0, 0):
                    return hat
            lx = 0
            ly = 0
            try:
                lx = self.joy.get_axis(0)
                ly = self.joy.get_axis(1)
            except Exception:
                pass
            if self._stick_active_x:
                self._stick_active_x = abs(lx) > self.stick_release
            else:
                self._stick_active_x = abs(lx) > self.stick_deadzone
            if self._stick_active_y:
                self._stick_active_y = abs(ly) > self.stick_release
            else:
                self._stick_active_y = abs(ly) > self.stick_deadzone
            dx = (1 if lx > 0 else -1) if self._stick_active_x else 0
            dy = (1 if ly > 0 else -1) if self._stick_active_y else 0
            return (dx, dy)
        return (0, 0)

    def is_button_pressed(self, btn_id):
        if not self.connected:
            return False
        if self.ctrl is not None:
            # Traducir el id crudo a nombre estandar de SDL2; si falla,
            # caer al boton crudo del joystick (el layout estandar de
            # Xbox/PS suele coincidir con el orden crudo).
            name = self.BUTTON_MAP.get(btn_id)
            if name:
                try:
                    return bool(self.ctrl.get_button(self.SDL2_BTN_NAMES[name]))
                except Exception:
                    pass
        if self.joy:
            try:
                return self.joy.get_button(btn_id)
            except Exception:
                return False
        return False

    def get_menu_action(self):
        actions = []
        dpad = self.get_dpad()
        prev_dpad = self._prev_dpad

        if dpad[1] == -1 and prev_dpad[1] != -1:
            actions.append("up")
        elif dpad[1] == 1 and prev_dpad[1] != 1:
            actions.append("down")
        if dpad[0] == -1 and prev_dpad[0] != -1:
            actions.append("left")
        elif dpad[0] == 1 and prev_dpad[0] != 1:
            actions.append("right")

        self._prev_dpad = dpad
        return actions

    def get_menu_held(self, dt):
        dpad = self.get_dpad()
        if dpad[1] == 0 and dpad[0] == 0:
            self._menu_held_dir = None
            self._menu_held_time = 0.0
            return []
        d = None
        if dpad[1] == -1:
            d = "up"
        elif dpad[1] == 1:
            d = "down"
        elif dpad[0] == -1:
            d = "left"
        elif dpad[0] == 1:
            d = "right"

        if d != self._menu_held_dir:
            self._menu_held_dir = d
            self._menu_held_time = 0.0
            return []

        self._menu_held_time += dt
        if self._menu_held_time > self._menu_repeat_delay:
            self._menu_held_time -= self._menu_repeat_rate
            return [d]
        return []

    def get_button_down(self, btn_id):
        if not self.joy or not self.connected:
            return False
        pressed = self.is_button_pressed(btn_id)
        was = self._prev_buttons.get(btn_id, False)
        self._prev_buttons[btn_id] = pressed
        return pressed and not was

    def get_button_up(self, btn_id):
        if not self.joy or not self.connected:
            return False
        pressed = self.is_button_pressed(btn_id)
        was = self._prev_buttons.get(btn_id, False)
        self._prev_buttons[btn_id] = pressed
        return not pressed and was



class ComboSystem:
    RANKS = [
        (50, "SSS", (255, 80, 80)),
        (40, "SS", (255, 160, 60)),
        (30, "S+", (220, 100, 220)),
        (20, "S", (200, 80, 80)),
        (15, "A", (220, 180, 60)),
        (10, "B", (80, 160, 220)),
        (5, "C", (100, 180, 100)),
    ]

    def __init__(self):
        self.count = 0
        self.timer = 0.0
        self.timer_max = 6.0
        self.score = 0
        self.total_score = 0
        self.rank = None
        self.rank_color = (150, 150, 150)
        self.rank_scale = 1.0
        self.prev_count = 0
        self.title_text = ""
        self.title_timer = 0.0
        self.title_alpha = 255
        self.title_y = 0
        self.bubble_shake = 0.0
        self.menacing = False
        self.very = False
        self.max_count = 0

    def add_line_clear(self, lines_cleared, level):
        """Calcula el bonus de combo UNA sola vez y lo devuelve; ese mismo
        numero es el que se suma al score real y el que se muestra en el
        texto flotante, para que nunca queden desincronizados.
        Formula simple y predecible: 50 puntos por combo acumulado, por
        nivel. Nada de multiplicar tambien por lineas limpiadas (eso ya
        lo paga el puntaje base de la tabla de lineas) ni redondeos raros
        que antes hacian crecer el bonus de forma desproporcionada."""
        if lines_cleared == 0:
            return 0
        self.count += 1
        self.timer = self.timer_max
        points = 50 * self.count * level
        self.score += points
        self.total_score += points
        self.max_count = max(self.max_count, self.count)
        self._update_rank()
        if self.count != self.prev_count:
            self.prev_count = self.count
            if self.count % 5 == 0:
                self._show_title()
        self.menacing = self.count >= 3
        return points

    def _update_rank(self):
        for threshold, name, color in self.RANKS:
            if self.count >= threshold:
                if self.rank != name:
                    self.rank = name
                    self.rank_color = color
                    self.rank_scale = 2.5
                return
        if self.rank is not None:
            self.rank = None
            self.rank_color = (150, 150, 150)

    def _show_title(self):
        self.very = self.count >= 15
        rank_str = self.rank if self.rank else ""
        if rank_str:
            self.title_text = f"{self.count} - {rank_str}!"
        else:
            self.title_text = f"{self.count} COMBO!"
        self.title_timer = 2.0
        self.title_alpha = 255
        self.title_y = 0

    def reset(self):
        self.count = 0
        self.timer = 0.0
        self.score = 0
        self.rank = None
        self.rank_color = (150, 150, 150)
        self.rank_scale = 1.0
        self.prev_count = 0
        self.title_text = ""
        self.title_timer = 0.0
        self.title_alpha = 255
        self.title_y = 0
        self.bubble_shake = 0.0
        self.menacing = False
        self.very = False

    def update(self, dt):
        dt_sec = dt / 1000.0
        if self.timer > 0:
            self.timer -= dt_sec * 1.2
            if self.timer <= 0:
                self.timer = 0
                self.reset()
        if self.rank_scale > 1.0:
            self.rank_scale = max(1.0, self.rank_scale - dt_sec * 4)
        if self.title_timer > 0:
            self.title_timer -= dt_sec
            self.title_y -= dt_sec * 40
            if self.title_timer < 0.5:
                self.title_alpha = int(255 * (self.title_timer / 0.5))
        if 0 < self.timer < 2.0:
            self.bubble_shake = (1.0 - self.timer / 2.0) * 4
        else:
            self.bubble_shake *= 0.9

    @property
    def active(self):
        return self.count > 0

    @property
    def timer_ratio(self):
        return self.timer / self.timer_max if self.timer_max > 0 else 0


class ScreenEffects:
    _circle_cache = {}

    def __init__(self):
        self.shake_x = 0
        self.shake_y = 0
        self.shake_mag = 0
        self.shake_decay = 0.9
        self.flash_alpha = 0
        self.flash_color = (255, 255, 255)
        self.particles = []

    def shake(self, magnitude):
        self.shake_mag = max(self.shake_mag, magnitude)

    def flash(self, color=(255, 255, 255), alpha=80):
        self.flash_color = color
        self.flash_alpha = alpha

    def add_particles(self, x, y, color, count=5):
        if low_end:
            return
        for _ in range(count):
            self.particles.append({
                "x": x, "y": y,
                "vx": random.uniform(-3, 3),
                "vy": random.uniform(-5, -1),
                "life": random.uniform(0.3, 0.8),
                "max_life": 0.8,
                "color": color,
                "size": random.uniform(2, 5),
            })

    def update(self, dt):
        dt_sec = dt / 1000.0
        if self.shake_mag > 0.1:
            self.shake_x = random.randint(int(-self.shake_mag), int(self.shake_mag))
            self.shake_y = random.randint(int(-self.shake_mag), int(self.shake_mag))
            self.shake_mag *= self.shake_decay
        else:
            self.shake_x = 0
            self.shake_y = 0
            self.shake_mag = 0
        if self.flash_alpha > 0:
            self.flash_alpha = max(0, self.flash_alpha - dt_sec * 300)
        for p in self.particles:
            p["x"] += p["vx"] * dt_sec * 60
            p["y"] += p["vy"] * dt_sec * 60
            p["vy"] += 8 * dt_sec
            p["life"] -= dt_sec
        self.particles = [p for p in self.particles if p["life"] > 0]

    @classmethod
    def _get_circle(cls, radius, color, alpha):
        key = (radius, color, alpha)
        if key not in cls._circle_cache:
            size = radius * 2
            surf = pygame.Surface((size, size), pygame.SRCALPHA)
            pygame.draw.circle(surf, (*color, alpha), (radius, radius), radius)
            cls._circle_cache[key] = surf
        return cls._circle_cache[key]

    def draw(self, surface, win_w, win_h):
        if self.flash_alpha > 0:
            flash_surf = get_overlay(win_w, win_h, self.flash_color, int(self.flash_alpha))
            surface.blit(flash_surf, (0, 0))
        for p in self.particles:
            alpha = int(255 * (p["life"] / p["max_life"]))
            ps = max(1, int(p["size"] * (p["life"] / p["max_life"])))
            psurf = self._get_circle(ps, p["color"], alpha)
            surface.blit(psurf, (int(p["x"]) - ps, int(p["y"]) - ps))


class FloatingText:
    def __init__(self):
        self.texts = []

    def add(self, x, y, text, color=(255, 255, 255), size=20):
        if low_end:
            return
        self.texts.append({
            "x": x, "y": y, "text": text, "color": color,
            "life": 1.0, "max_life": 1.0, "size": size,
            "vy": -2.0,
        })

    def update(self, dt):
        dt_sec = dt / 1000.0
        for t in self.texts:
            t["y"] += t["vy"] * dt_sec * 60
            t["vy"] *= 0.97
            t["life"] -= dt_sec * 0.8
        self.texts = [t for t in self.texts if t["life"] > 0]

    _float_text_cache = {}

    def draw(self, surface):
        for t in self.texts:
            alpha = int(255 * min(1, t["life"] / 0.3))
            font_size = max(int(t["size"] * (0.8 + 0.2 * (t["life"] / t["max_life"]))), 10)
            f = get_font(font_size, True)
            cache_key = (t["text"], t["color"], font_size, id(f))
            surf = self._float_text_cache.get(cache_key)
            if surf is None:
                surf = f.render(t["text"], True, t["color"])
                if len(self._float_text_cache) < 100:
                    self._float_text_cache[cache_key] = surf
            surf.set_alpha(alpha)
            x = int(t["x"] - surf.get_width() // 2)
            y = int(t["y"])
            surface.blit(surf, (x, y))


class LineClearAnim:
    def __init__(self):
        self.active = False
        self.rows = []
        self.timer = 0.0
        self.duration = 0.4
        self.sweep_x = 0
        self.flash_rows = []

    def trigger(self, rows, grid_x, grid_w, cell):
        self.active = True
        self.rows = rows
        self.timer = 0.0
        self.flash_rows = list(rows)
        self.sweep_x = grid_x

    def update(self, dt):
        if not self.active:
            return
        dt_sec = dt / 1000.0
        self.timer += dt_sec
        self.sweep_x += dt_sec * 800
        if self.timer >= self.duration:
            self.active = False
            self.rows = []
            self.flash_rows = []

    def draw(self, surface, grid_x, grid_y, grid_w, cell):
        if not self.active:
            return
        progress = min(self.timer / self.duration, 1.0)

        fade = 1.0 - progress
        flash_surf = get_overlay(grid_w, cell, (255, 255, 255), int(180 * fade))
        for row in self.flash_rows:
            y = grid_y + row * cell
            surface.blit(flash_surf, (grid_x, y))

        if progress < 0.6:
            sweep_w = int(grid_w * 0.15)
            sweep_alpha = int(255 * (1.0 - progress / 0.6))
            sweep_surf = get_gradient(sweep_w, cell, (255, 255, 255), (255, 255, 255), sweep_alpha)
            for row in self.rows:
                y = grid_y + row * cell
                sx = min(int(self.sweep_x), grid_x + grid_w)
                surface.blit(sweep_surf, (sx, y))


_block_glow_cache = {}

def draw_block(surface, x, y, c, cell):
    rect = pygame.Rect(x, y, cell, cell)
    pygame.draw.rect(surface, c, rect)
    lighter = tuple(min(v + 40, 255) for v in c)
    darker = tuple(max(v - 40, 0) for v in c)
    pygame.draw.line(surface, lighter, rect.topleft, (rect.right - 1, rect.top), 2)
    pygame.draw.line(surface, lighter, rect.topleft, (rect.left, rect.bottom - 1), 2)
    pygame.draw.line(surface, darker, (rect.right - 1, rect.top), rect.bottomright, 2)
    pygame.draw.line(surface, darker, (rect.left, rect.bottom - 1), (rect.right - 1, rect.bottom - 1), 2)
    # Highlight diagonal sutil en la esquina superior-izquierda para dar
    # sensacion de "gloss" en vez de bloque plano. Cacheado por tamano de
    # celda ya que la forma del triangulo es siempre la misma.
    if cell not in _block_glow_cache:
        gs = pygame.Surface((cell, cell), pygame.SRCALPHA)
        pygame.draw.polygon(
            gs, (255, 255, 255, 55),
            [(2, 2), (cell * 0.55, 2), (2, cell * 0.55)]
        )
        _block_glow_cache[cell] = gs
    surface.blit(_block_glow_cache[cell], rect.topleft)


_block_surface_cache = {}
def get_block_surface(c, cell):
    """Bloque completo pre-renderizado por (color, tamano). Antes cada
    bloque dibujado en pantalla costaba 1 rect + 4 line + 1 blit; con
    esto todo el tablero se dibuja con 1 blit por bloque."""
    key = (c, cell)
    surf = _block_surface_cache.get(key)
    if surf is None:
        surf = pygame.Surface((cell, cell), pygame.SRCALPHA)
        draw_block(surf, 0, 0, c, cell)
        if len(_block_surface_cache) >= 256:
            _block_surface_cache.clear()
        _block_surface_cache[key] = surf
    return surf


def draw_grid(surface, grid, layout, squash=None):
    cell = layout.cell
    squash_map = {}
    if squash:
        for s in squash:
            squash_map[(s["x"], s["y"])] = s["t"]
    for y in range(ROWS):
        for x in range(COLS):
            if grid[y][x]:
                if (x, y) in squash_map:
                    # Bloque recien asentado: rebote rapido de escala
                    # (1.25 -> 1.0) para dar sensacion de "peso" al caer,
                    # sin generar ninguna superficie nueva (solo un rect
                    # mas grande centrado en la celda).
                    t = squash_map[(x, y)]
                    scale = 1.0 + 0.25 * t
                    sz = int(cell * scale)
                    off = (sz - cell) // 2
                    bx = layout.grid_x + x * cell - off
                    by = layout.grid_y + y * cell - off
                    surface.blit(get_block_surface(grid[y][x], sz), (bx, by))
                else:
                    surface.blit(get_block_surface(grid[y][x], cell), (layout.grid_x + x * cell, layout.grid_y + y * cell))


def draw_piece(surface, piece, layout):
    cell = layout.cell
    for y, row in enumerate(piece.shape):
        for x, c in enumerate(row):
            if c:
                surface.blit(get_block_surface(piece.color, cell), (layout.grid_x + (piece.x + x) * cell, layout.grid_y + (piece.y + y) * cell))


def draw_ghost(surface, game, layout):
    cell = layout.cell
    gy = game.ghost_y()
    for y, row in enumerate(game.current.shape):
        for x, c in enumerate(row):
            if c:
                rect = pygame.Rect(layout.grid_x + (game.current.x + x) * cell, layout.grid_y + (gy + y) * cell, cell, cell)
                pygame.draw.rect(surface, tuple(v // 3 for v in game.current.color), rect)
                pygame.draw.rect(surface, WHITE, rect, 1)


def draw_mini_piece(surface, piece, cx, cy, cell):
    scale = cell * 0.8
    for y, row in enumerate(piece.shape):
        for x, c in enumerate(row):
            if c:
                r = pygame.Rect(cx + x * scale, cy + y * scale, scale, scale)
                pygame.draw.rect(surface, piece.color, r)
                lighter = tuple(min(v + 40, 255) for v in piece.color)
                darker = tuple(max(v - 40, 0) for v in piece.color)
                pygame.draw.line(surface, lighter, r.topleft, (r.right - 1, r.top), 2)
                pygame.draw.line(surface, lighter, r.topleft, (r.left, r.bottom - 1), 2)
                pygame.draw.line(surface, darker, (r.right - 1, r.top), r.bottomright, 2)
                pygame.draw.line(surface, darker, (r.left, r.bottom - 1), (r.right - 1, r.bottom - 1), 2)


_hud_label_cache = {}
def draw_hud_panel(surface, x, y, w, h, label, font, accent_color=(70, 130, 180), glow=False, time=0):
    bg_surf = get_gradient(w, h, (18, 20, 30), (26, 30, 42), 220)
    surface.blit(bg_surf, (x, y))

    border_color = accent_color if glow else (50, 55, 70)
    pygame.draw.rect(surface, border_color, (x, y, w, h), 2, border_radius=6)

    if glow:
        pulse = (math.sin(time * 3) + 1) / 2
        gc = tuple(min(int(c * (0.7 + pulse * 0.3)), 255) for c in accent_color)
        glow_key = (w, h, gc, "glow")
        if not hasattr(draw_hud_panel, '_glow_cache') or draw_hud_panel._glow_key != glow_key:
            gs = pygame.Surface((w, h), pygame.SRCALPHA)
            pygame.draw.rect(gs, (*gc, 25), (0, 0, w, h), border_radius=6)
            draw_hud_panel._glow_cache = gs
            draw_hud_panel._glow_key = glow_key
        surface.blit(draw_hud_panel._glow_cache, (x, y - 2))

    lbl_key = (label, accent_color, font.get_height())
    cached = _hud_label_cache.get(lbl_key)
    if cached is None:
        lbl_surf = font.render(label, True, accent_color)
        lb = pygame.Surface((lbl_surf.get_width() + 10, lbl_surf.get_height() + 2), pygame.SRCALPHA)
        lb.fill((10, 12, 18, 200))
        pygame.draw.rect(lb, accent_color, (0, 0, lb.get_width(), lb.get_height()), 1, border_radius=3)
        cached = (lb, lbl_surf)
        _hud_label_cache[lbl_key] = cached
    cached_bg, cached_lbl = cached
    surface.blit(cached_bg, (x + w // 2 - cached_bg.get_width() // 2, y - cached_lbl.get_height() // 2 - 1))
    surface.blit(cached_lbl, (x + w // 2 - cached_lbl.get_width() // 2, y - cached_lbl.get_height() // 2))


def draw_hold(surface, game, layout, font, time):
    bx, by, bw, bh = layout.hold_x, layout.hold_y, layout.hold_w, layout.hold_h
    accent = (120, 160, 200) if game.can_hold else (80, 80, 100)
    draw_hud_panel(surface, bx, by, bw, bh, "HOLD", font, accent, game.can_hold, time)

    if game.hold_piece:
        pc_alpha = 255 if game.can_hold else 100
        piece_c = game.hold_piece.color
        if not game.can_hold:
            piece_c = tuple(max(int(c * 0.5), 0) for c in piece_c)
        draw_mini_piece(surface, game.hold_piece, bx + bw // 2 - layout.cell, by + int(bh * 0.3), layout.cell)

    if not game.can_hold:
        lock_surf = get_overlay(bw, bh, (0, 0, 0), 60)
        surface.blit(lock_surf, (bx, by))


_next_row_cache = {}
def draw_next_queue(surface, game, layout, font, time):
    bx, by, bw, bh = layout.next_x, layout.next_y, layout.next_w, layout.next_h
    draw_hud_panel(surface, bx, by, bw, bh, "NEXT", font, (100, 180, 130), True, time)

    piece_area_h = (bh - layout.cell) // 5
    cell = layout.cell

    for i, piece in enumerate(game.next_queue):
        py = by + layout.cell + i * piece_area_h
        fade = 1.0 - (i * 0.15)
        fade = max(fade, 0.3)

        row_color = tuple(int(c * fade) for c in (30, 35, 45))
        row_key = (bw - 4, piece_area_h - 2, i, len(game.next_queue))
        row_surf = _next_row_cache.get(row_key)
        if row_surf is None:
            row_surf = pygame.Surface((bw - 4, piece_area_h - 2), pygame.SRCALPHA)
            row_surf.fill((*row_color, int(180 * fade)))
            if i < len(game.next_queue) - 1:
                pygame.draw.line(row_surf, (40, 45, 55, int(100 * fade)), (4, piece_area_h - 3), (bw - 8, piece_area_h - 3))
            _next_row_cache[row_key] = row_surf
        surface.blit(row_surf, (bx + 2, py))

        draw_mini_piece(surface, piece, bx + bw // 2 - cell, py + (piece_area_h - cell) // 2, cell)


def draw_score_panel(surface, game, layout, font, small_font, time):
    panel_w = layout.grid_w
    panel_h = int(layout.cell * 2.2)
    panel_x = layout.grid_x
    panel_y = layout.grid_y + layout.grid_h + int(layout.cell * 0.25)

    panel_surf = get_gradient(panel_w, panel_h, (14, 16, 24), (20, 24, 36), 210)
    surface.blit(panel_surf, (panel_x, panel_y))
    pygame.draw.rect(surface, (50, 55, 70), (panel_x, panel_y, panel_w, panel_h), 1, border_radius=6)

    col_w = panel_w // 4

    score_label = get_text("SCORE", (100, 110, 130), small_font)
    score_x = panel_x + col_w // 2
    surface.blit(score_label, (score_x - score_label.get_width() // 2, panel_y + 4))
    score_val = get_text(f"{game.score:,}", WHITE, font)
    surface.blit(score_val, (score_x - score_val.get_width() // 2, panel_y + 4 + score_label.get_height()))

    pygame.draw.line(surface, (40, 45, 55), (panel_x + col_w, panel_y + 4), (panel_x + col_w, panel_y + panel_h - 4))

    lines_label = get_text("LINES", (100, 110, 130), small_font)
    lines_x = panel_x + col_w + col_w // 2
    surface.blit(lines_label, (lines_x - lines_label.get_width() // 2, panel_y + 4))
    lines_val = get_text(str(game.lines), (160, 200, 180), font)
    surface.blit(lines_val, (lines_x - lines_val.get_width() // 2, panel_y + 4 + lines_label.get_height()))

    pygame.draw.line(surface, (40, 45, 55), (panel_x + col_w * 2, panel_y + 4), (panel_x + col_w * 2, panel_y + panel_h - 4))

    lvl_label = get_text("LVL", (100, 110, 130), small_font)
    lvl_x = panel_x + col_w * 2 + col_w // 2
    surface.blit(lvl_label, (lvl_x - lvl_label.get_width() // 2, panel_y + 4))
    lvl_val = get_text(str(game.level), (220, 180, 80), font)
    surface.blit(lvl_val, (lvl_x - lvl_val.get_width() // 2, panel_y + 4 + lvl_label.get_height()))
    # Progreso hacia el siguiente nivel (cuantas lineas faltan), para que
    # subir de nivel deje de sentirse impredecible.
    lines_into_level = game.lines % 10
    lvl_progress = get_text(f"{lines_into_level}/10", (150, 140, 100), small_font)
    surface.blit(lvl_progress, (lvl_x - lvl_progress.get_width() // 2, panel_y + panel_h - lvl_progress.get_height() - 4))

    pygame.draw.line(surface, (40, 45, 55), (panel_x + col_w * 3, panel_y + 4), (panel_x + col_w * 3, panel_y + panel_h - 4))

    combo_x = panel_x + col_w * 3 + col_w // 2
    combo_obj = getattr(game, "combo", None)
    combo_active = combo_obj is not None and combo_obj.count > 0
    combo_label_color = combo_obj.rank_color if combo_active else (100, 110, 130)
    combo_label = get_text("COMBO", combo_label_color, small_font)
    surface.blit(combo_label, (combo_x - combo_label.get_width() // 2, panel_y + 4))
    if combo_active:
        combo_val = get_text(f"x{combo_obj.count}", combo_obj.rank_color, font)
        surface.blit(combo_val, (combo_x - combo_val.get_width() // 2, panel_y + 4 + combo_label.get_height()))
        # Bono real que suma el PROXIMO clear con este combo, calculado
        # con la misma formula que ComboSystem.add_line_clear, para que
        # el jugador sepa exactamente cuanto vale mantenerlo vivo.
        next_bonus = 50 * (combo_obj.count + 1) * game.level
        bonus_txt = get_text(f"+{next_bonus:,}", (150, 140, 100), small_font)
        surface.blit(bonus_txt, (combo_x - bonus_txt.get_width() // 2, panel_y + panel_h - bonus_txt.get_height() - 4))
    else:
        combo_val = get_text("-", (90, 95, 110), font)
        surface.blit(combo_val, (combo_x - combo_val.get_width() // 2, panel_y + 4 + combo_label.get_height()))


def draw_speed_bar(surface, game, layout, time):
    bar_x = layout.grid_x + layout.grid_w + 4
    bar_w = int(layout.cell * 0.4)
    bar_y = layout.grid_y
    bar_h = layout.grid_h

    bg_surf = get_gradient(bar_w, bar_h, (20, 22, 28), (25, 27, 33), 180)
    surface.blit(bg_surf, (bar_x, bar_y))
    pygame.draw.rect(surface, (50, 55, 65), (bar_x, bar_y, bar_w, bar_h), 1, border_radius=3)

    fill_ratio = min((game.level - 1) / 19, 1.0)
    fill_h = int(bar_h * fill_ratio)
    if fill_h > 0:
        fill_key = (bar_w - 2, fill_h)
        if not hasattr(draw_speed_bar, '_fill_cache') or draw_speed_bar._fill_key != fill_key:
            draw_speed_bar._fill_key = fill_key
            draw_speed_bar._fill_cache = get_gradient(bar_w - 2, fill_h, (60, 130, 220), (220, 170, 70), 230)
        surface.blit(draw_speed_bar._fill_cache, (bar_x + 1, bar_y + bar_h - fill_h))

    tick_font = get_font(max(int(layout.cell * 0.3), 8))
    for lvl in [1, 5, 10, 15, 20]:
        tick_y = bar_y + bar_h - int(bar_h * ((lvl - 1) / 19))
        if bar_y <= tick_y <= bar_y + bar_h:
            pygame.draw.line(surface, (50, 55, 65), (bar_x, tick_y), (bar_x + bar_w, tick_y))
            tick_text = get_text(str(lvl), (80, 85, 95), tick_font)
            surface.blit(tick_text, (bar_x + bar_w + 3, tick_y - tick_text.get_height() // 2))

    speed_label = get_text("SPD", (100, 105, 115), tick_font)
    surface.blit(speed_label, (bar_x + bar_w // 2 - speed_label.get_width() // 2, bar_y - speed_label.get_height() - 3))


_scaled_sprite_cache = {}
def get_scaled_sprite(name, w, h, frame=0):
    """Sprite escalado cacheado por (nombre, w, h). Antes draw_combo_hud
    hacia smoothscale de hasta 8 sprites en CADA frame; smoothscale es
    una de las operaciones mas caras de pygame, esto lo elimina."""
    if w <= 0 or h <= 0:
        return None
    key = (name, w, h, frame)
    surf = _scaled_sprite_cache.get(key)
    if surf is None:
        base = Sprites.get(name, frame)
        if base is None:
            return None
        surf = pygame.transform.smoothscale(base, (w, h))
        if len(_scaled_sprite_cache) >= 600:
            _scaled_sprite_cache.clear()
        _scaled_sprite_cache[key] = surf
    return surf


def draw_gamepad_hud(surface, gamepad, cx, y, font):
    """Fila de controles fisicos con el simbolo correcto segun la marca
    del control conectado (Xbox: A/B/X/Y - PlayStation: cruz/circulo/
    cuadrado/triangulo), para que el jugador sepa que boton apretar sin
    tener que adivinar con el teclado como referencia."""
    if not gamepad or not gamepad.connected:
        return
    actions = [
        ("a", "Rotar"), ("b", "Rotar CCW"),
        ("x", "Caida"), ("y", "Hold"),
    ]
    pad_x, pad_y, gap = 10, 4, 10
    parts = []
    total_w = 0
    for btn_key, label in actions:
        glyph_txt, glyph_col = gamepad.glyph(btn_key)
        gsurf = font.render(glyph_txt, True, glyph_col)
        lsurf = font.render(label, True, (200, 205, 220))
        chip_w = pad_x * 3 + gsurf.get_width() + lsurf.get_width()
        parts.append((gsurf, lsurf, chip_w))
        total_w += chip_w + gap
    total_w -= gap
    chip_h = font.get_height() + pad_y * 2
    x = cx - total_w // 2
    for gsurf, lsurf, chip_w in parts:
        chip = get_overlay(chip_w, chip_h, (15, 17, 26), 170)
        surface.blit(chip, (x, y))
        pygame.draw.rect(surface, (60, 66, 88), (x, y, chip_w, chip_h), 1, border_radius=chip_h // 2)
        gx = x + pad_x
        surface.blit(gsurf, (gx, y + chip_h // 2 - gsurf.get_height() // 2))
        surface.blit(lsurf, (gx + gsurf.get_width() + pad_x, y + chip_h // 2 - lsurf.get_height() // 2))
        x += chip_w + gap


_combo_bar_cache = {}

def draw_combo_hud(surface, combo, layout, font, small_font, time):
    _draw_combo_hud_fallback(surface, combo, layout, font, small_font, time)


def _draw_combo_hud_fallback(surface, combo, layout, font, small_font, time):
    if combo.count == 0 and combo.title_timer <= 0:
        return
    bubble_w = int(layout.cell * 6)
    bubble_h = int(layout.cell * 4.5)
    bubble_x = layout.grid_x + layout.grid_w // 2 - bubble_w // 2
    bubble_y = layout.grid_y - bubble_h - int(layout.cell * 0.5)
    shake_offset = 0
    if not low_end and combo.bubble_shake > 0.5:
        shake_offset = int(math.sin(time * 30) * combo.bubble_shake)
    bubble_y += shake_offset
    bubble_surf = get_gradient(bubble_w, bubble_h, (20, 22, 35), (35, 32, 55), 210)
    surface.blit(bubble_surf, (bubble_x, bubble_y))
    pygame.draw.rect(surface, combo.rank_color, (bubble_x, bubble_y, bubble_w, bubble_h), 2, border_radius=8)

    if combo.count > 0:
        timer_ratio = max(0, combo.timer / combo.timer_max)

        bar_w = int(bubble_w * 0.72)
        bar_h = max(int(bubble_h * 0.09), 5)
        bar_x = bubble_x + bubble_w // 2 - bar_w // 2
        bar_y = bubble_y + int(bubble_h * 0.86)

        bar_key = ("combo_bar", bar_w, bar_h)
        bar_bg = _combo_bar_cache.get(bar_key)
        if bar_bg is None:
            bar_bg = pygame.Surface((bar_w, bar_h), pygame.SRCALPHA)
            pygame.draw.rect(bar_bg, (40, 20, 60, 180), (0, 0, bar_w, bar_h), border_radius=bar_h // 2)
            if len(_combo_bar_cache) < 20:
                _combo_bar_cache[bar_key] = bar_bg
        surface.blit(bar_bg, (bar_x, bar_y))

        fill_w = int(bar_w * timer_ratio)
        if fill_w > 0:
            bar_color = (200, 120, 255) if timer_ratio >= 0.25 else (255, 90, 90)
            if timer_ratio < 0.25 and not low_end:
                blink = (math.sin(time * 12) + 1) / 2
                bar_color = tuple(min(255, int(c + blink * 40)) for c in bar_color)
            fill_surf = pygame.Surface((max(fill_w, bar_h), bar_h), pygame.SRCALPHA)
            pygame.draw.rect(fill_surf, (*bar_color, 235), (0, 0, max(fill_w, bar_h), bar_h), border_radius=bar_h // 2)
            surface.blit(fill_surf, (bar_x, bar_y), (0, 0, fill_w, bar_h))
        pygame.draw.rect(surface, (255, 255, 255, 90), (bar_x, bar_y, bar_w, bar_h), 1, border_radius=bar_h // 2)

        num_size = max(int(bubble_h * 0.3), 16)
        num_font = get_font(num_size, True)
        num_key = ("combo_num", str(combo.count), combo.rank_color, num_size)
        num_text = _combo_bar_cache.get(num_key)
        if num_text is None:
            num_text = num_font.render(str(combo.count), True, combo.rank_color)
            if len(_combo_bar_cache) < 50:
                _combo_bar_cache[num_key] = num_text
        scale_num = combo.rank_scale if not low_end else 1.0
        if scale_num > 1.0:
            num_text = pygame.transform.smoothscale(num_text, (int(num_text.get_width() * scale_num), int(num_text.get_height() * scale_num)))
        num_x = bubble_x + bubble_w // 2 - num_text.get_width() // 2
        num_y = bubble_y + int(bubble_h * 0.35)
        glow_key = ("combo_glow", str(combo.count), num_size)
        glow = _combo_bar_cache.get(glow_key)
        if glow is None:
            glow = num_font.render(str(combo.count), True, (255, 255, 255))
            glow.set_alpha(60)
            if len(_combo_bar_cache) < 50:
                _combo_bar_cache[glow_key] = glow
        if not low_end:
            surface.blit(glow, (num_x + 2, num_y + 2))
        surface.blit(num_text, (num_x, num_y))

    if combo.title_timer > 0:
        title_alpha = max(0, min(255, combo.title_alpha))
        title_y = bubble_y - 30 + int(combo.title_y)
        title_font_size = max(int(layout.cell * 0.9), 20)
        title_surf = get_text(combo.title_text, combo.rank_color, get_font(title_font_size, True))
        title_surf.set_alpha(title_alpha)
        title_x = layout.grid_x + layout.grid_w // 2 - title_surf.get_width() // 2
        surface.blit(title_surf, (title_x, title_y))


def draw_multi_warning(surface, win_w, win_h, font, font_sub, time_=0.0, shown_time=0.0):
    """Aviso que se muestra al entrar al modo multijugador: no hay
    servidores propios, hay que jugar por VPN (Radmin VPN u otra) o en
    red local con amigos. Devuelve el rect del boton "Entendido"."""
    overlay = get_overlay(win_w, win_h, (0, 0, 0), 190)
    surface.blit(overlay, (0, 0))
    cx, cy = win_w // 2, win_h // 2
    box_w = int(win_w * 0.52)
    box_h = int(win_h * 0.5)

    # Animacion de entrada: rebote suave + deslizamiento, en vez de
    # aparecer de golpe (mismo estilo que el resto de los popups).
    t_since = max(0.0, time_ - shown_time)
    t_in = min(t_since * 2.4, 1.0)
    ease = ease_out_back(t_in)
    scale = 0.75 + 0.25 * ease
    cur_w, cur_h = int(box_w * scale), int(box_h * scale)
    box_x = cx - cur_w // 2
    box_y = cy - cur_h // 2 + int((1 - ease) * 35)
    fade_alpha = int(min(t_since * 5.0, 1.0) * 255)

    # Pequeno "shake" de atencion justo al aparecer, para que se note que
    # es algo que hay que leer y no otro cartel mas.
    shake_t = max(0.0, 1.0 - t_since / 0.35)
    shake_x = int(math.sin(t_since * 55) * shake_t * 6)

    pulse = (math.sin(time_ * 2.6) + 1) / 2
    border_col = (int(220 + pulse * 20), int(160 + pulse * 40), int(60 + pulse * 20))

    card = pygame.Surface((cur_w, cur_h), pygame.SRCALPHA)
    pygame.draw.rect(card, (32, 26, 20, 250), (0, 0, cur_w, cur_h), border_radius=16)
    pygame.draw.rect(card, (*border_col, 255), (0, 0, cur_w, cur_h), 3, border_radius=16)

    glow = pygame.Surface((cur_w, int(cur_h * 0.32)), pygame.SRCALPHA)
    pygame.draw.ellipse(glow, (255, 190, 70, 40), (0, 0, cur_w, int(cur_h * 0.32)))
    card.blit(glow, (0, 0))

    # Franja superior de color solida, para separar visualmente el titulo
    # del cuerpo del mensaje (como una barra de "atencion").
    top_bar_h = int(cur_h * 0.05)
    pygame.draw.rect(card, (255, 190, 70, 220), (0, 0, cur_w, top_bar_h), border_top_left_radius=16, border_top_right_radius=16)

    ccx = cur_w // 2

    # Icono de advertencia: triangulo con sombra + signo de exclamacion,
    # con un ligero "respiro" (escala) constante para llamar la atencion.
    tri_pulse = 1.0 + math.sin(time_ * 3.4) * 0.05
    tri_size = int(cur_h * 0.10 * tri_pulse)
    title_text = get_text("¡ADVERTENCIA!", (255, 205, 100), font)
    title_y = top_bar_h + int(cur_h * 0.07)
    total_w = tri_size * 2 + 16 + title_text.get_width()
    tri_cx = ccx - total_w // 2 + tri_size
    tri_cy = title_y + title_text.get_height() // 2

    shadow_pts = [(tri_cx + 2, tri_cy - tri_size + 3), (tri_cx - tri_size + 2, tri_cy + tri_size * 0.85 + 3), (tri_cx + tri_size + 2, tri_cy + tri_size * 0.85 + 3)]
    pygame.draw.polygon(card, (0, 0, 0, 90), shadow_pts)
    p1 = (tri_cx, tri_cy - tri_size)
    p2 = (tri_cx - tri_size, tri_cy + tri_size * 0.85)
    p3 = (tri_cx + tri_size, tri_cy + tri_size * 0.85)
    pygame.draw.polygon(card, (255, 205, 90, 255), [p1, p2, p3])
    pygame.draw.polygon(card, (40, 30, 15, 255), [p1, p2, p3], 3)
    excl = get_text("!", (40, 30, 15), font_sub)
    excl = excl.copy()
    surf_excl = pygame.Surface(excl.get_size(), pygame.SRCALPHA)
    surf_excl.blit(excl, (0, 0))
    card.blit(surf_excl, (tri_cx - excl.get_width() // 2, tri_cy - excl.get_height() // 2 + 2))
    title_surf2 = title_text.copy()
    card.blit(title_surf2, (tri_cx + tri_size + 16, title_y))

    # Divisor sutil debajo del titulo.
    div_y = title_y + title_text.get_height() + int(cur_h * 0.05)
    pygame.draw.line(card, (90, 78, 55, 180), (int(cur_w * 0.1), div_y), (int(cur_w * 0.9), div_y), 1)

    def draw_wifi_icon(target, x, y, size, color):
        """Icono simple de red/wifi: arcos concentricos + punto, para la
        fila que habla de VPN/red local."""
        for i, r in enumerate((size, size * 0.66, size * 0.33)):
            rect = pygame.Rect(0, 0, int(r * 2), int(r * 2))
            rect.center = (x, y + size * 0.3)
            pygame.draw.arc(target, color, rect, math.radians(200), math.radians(340), max(2, int(size * 0.16)))
        pygame.draw.circle(target, color, (x, y + int(size * 0.55)), max(2, int(size * 0.14)))

    def draw_coin_icon(target, x, y, size, color):
        """Icono simple de moneda ($), para la fila que habla del
        presupuesto."""
        pygame.draw.circle(target, color, (x, y), size, max(2, int(size * 0.18)))
        s_surf = get_text("$", color, font_sub)
        target.blit(s_surf, (x - s_surf.get_width() // 2, y - s_surf.get_height() // 2))

    row_icon_x = int(cur_w * 0.11)
    row_text_x = int(cur_w * 0.19)
    row_w = int(cur_w * 0.78)

    row1_y = div_y + int(cur_h * 0.07)
    draw_wifi_icon(card, row_icon_x, row1_y + int(cur_h * 0.04), int(cur_h * 0.045), (140, 210, 255, 255))
    row1_lines = [
        "El multijugador solo se puede jugar usando VPN",
        "o jugando en local con tus amigos.",
        "Puedes crear una red usando Radmin VPN.",
    ]
    ry = row1_y
    for line in row1_lines:
        lsurf = get_text(line, (225, 222, 215), font_sub)
        card.blit(lsurf, (row_text_x, ry))
        ry += lsurf.get_height() + 4

    row2_y = ry + int(cur_h * 0.05)
    draw_coin_icon(card, row_icon_x, row2_y + int(cur_h * 0.035), int(cur_h * 0.04), (255, 200, 120, 255))
    row2_lines = [
        "No es porque no podamos meter servidores,",
        "es solo que no hay presupuesto.",
    ]
    ry2 = row2_y
    for line in row2_lines:
        lsurf = get_text(line, (255, 210, 160), font_sub)
        card.blit(lsurf, (row_text_x, ry2))
        ry2 += lsurf.get_height() + 4

    card.set_alpha(fade_alpha)
    surface.blit(card, (box_x + shake_x, box_y))

    # Boton "Entendido" dibujado directo sobre la superficie final (no
    # sobre la tarjeta) para que su propio fade/hover no dependa del
    # alpha compuesto de la tarjeta.
    mx, my = pygame.mouse.get_pos()
    btn_w = int(cur_w * 0.42)
    btn_h = max(int(cur_h * 0.12), 38)
    btn_x = box_x + cur_w // 2 - btn_w // 2 + shake_x
    btn_y = box_y + cur_h - btn_h - int(cur_h * 0.055)
    hover = btn_x <= mx <= btn_x + btn_w and btn_y <= my <= btn_y + btn_h
    btn_scale = 1.04 if hover else 1.0
    bw2, bh2 = int(btn_w * btn_scale), int(btn_h * btn_scale)
    bx2, by2 = btn_x - (bw2 - btn_w) // 2, btn_y - (bh2 - btn_h) // 2

    btn_surf = pygame.Surface((bw2, bh2), pygame.SRCALPHA)
    pygame.draw.rect(btn_surf, (170, 125, 45, 255) if hover else (120, 88, 32, 255), (0, 0, bw2, bh2), border_radius=10)
    pygame.draw.rect(btn_surf, (255, 215, 140, 255), (0, 0, bw2, bh2), 2, border_radius=10)
    ok_label = get_text("✓ Entendido", (255, 248, 235), font_sub)
    btn_surf.blit(ok_label, (bw2 // 2 - ok_label.get_width() // 2, bh2 // 2 - ok_label.get_height() // 2))
    btn_surf.set_alpha(fade_alpha)
    surface.blit(btn_surf, (bx2, by2))

    return (btn_x, btn_y, btn_w, btn_h)


def draw_love_note(surface, win_w, win_h, font, font_sub, time_=0.0):
    """Cartelito exclusivo para una cuenta muy especial. Se muestra una
    vez al iniciar sesion con esa cuenta. Devuelve el rect del boton
    para cerrarlo."""
    overlay = get_overlay(win_w, win_h, (10, 0, 10), 170)
    surface.blit(overlay, (0, 0))
    cx, cy = win_w // 2, win_h // 2
    box_w = int(win_w * 0.46)
    box_h = int(win_h * 0.4)
    box_x, box_y = cx - box_w // 2, cy - box_h // 2

    pulse = (math.sin(time_ * 3.2) + 1) / 2
    border_col = (int(230 + pulse * 20), int(110 + pulse * 60), int(160 + pulse * 40))
    pygame.draw.rect(surface, (40, 20, 30), (box_x, box_y, box_w, box_h), border_radius=16)
    pygame.draw.rect(surface, border_col, (box_x, box_y, box_w, box_h), 3, border_radius=16)

    glow = pygame.Surface((box_w, int(box_h * 0.35)), pygame.SRCALPHA)
    pygame.draw.ellipse(glow, (255, 140, 190, 35), (0, 0, box_w, int(box_h * 0.35)))
    surface.blit(glow, (box_x, box_y))

    def heart_points(hx, hy, s):
        pts = []
        for i in range(24):
            a = (i / 24) * math.tau
            hx_ = 16 * math.sin(a) ** 3
            hy_ = -(13 * math.cos(a) - 5 * math.cos(2 * a) - 2 * math.cos(3 * a) - math.cos(4 * a))
            pts.append((hx + hx_ * s, hy + hy_ * s))
        return pts

    # Corazones flotando de fondo, suaves y sutiles.
    for i in range(6):
        hseed = i * 1.7
        hs = 0.4 + 0.25 * ((i % 3))
        hx = box_x + box_w * (0.12 + 0.76 * ((i * 0.37) % 1.0))
        hy = box_y + box_h - ((time_ * (14 + i * 4) + hseed * 60) % (box_h + 30))
        halpha = max(0, min(1, (box_y + box_h - hy) / box_h)) * 90
        hsurf = pygame.Surface((60, 60), pygame.SRCALPHA)
        pygame.draw.polygon(hsurf, (255, 120, 170, int(halpha)), heart_points(30, 34, hs))
        surface.blit(hsurf, (hx - 30, hy - 30))

    beat = 1.0 + 0.08 * math.sin(time_ * 4.5)
    big_heart = pygame.Surface((90, 90), pygame.SRCALPHA)
    pygame.draw.polygon(big_heart, (255, 90, 140, 255), heart_points(45, 48, 2.0 * beat))
    surface.blit(big_heart, (cx - 45, box_y + int(box_h * 0.06)))

    title_text = get_text("Para ti ♥", (255, 200, 220), font)
    title_y = box_y + int(box_h * 0.06) + 90 + 6
    surface.blit(title_text, (cx - title_text.get_width() // 2, title_y))

    lines = [
        "te amo mucho mi niña hermosa :3",
        "(por tu niño hermoso :3)",
    ]
    line_y = title_y + title_text.get_height() + 14
    for line in lines:
        lsurf = get_text(line, (255, 225, 235), font_sub)
        surface.blit(lsurf, (cx - lsurf.get_width() // 2, line_y))
        line_y += lsurf.get_height() + 6

    mx, my = pygame.mouse.get_pos()
    btn_w = int(box_w * 0.42)
    btn_h = max(int(box_h * 0.14), 36)
    btn_x = cx - btn_w // 2
    btn_y = box_y + box_h - btn_h - int(box_h * 0.06)
    hover = btn_x <= mx <= btn_x + btn_w and btn_y <= my <= btn_y + btn_h
    pygame.draw.rect(surface, (200, 70, 120) if hover else (150, 55, 95), (btn_x, btn_y, btn_w, btn_h), border_radius=10)
    pygame.draw.rect(surface, (255, 190, 210), (btn_x, btn_y, btn_w, btn_h), 2, border_radius=10)
    ok_label = get_text("♥ Gracias ♥", (255, 240, 245), font_sub)
    surface.blit(ok_label, (cx - ok_label.get_width() // 2, btn_y + btn_h // 2 - ok_label.get_height() // 2))

    return (btn_x, btn_y, btn_w, btn_h)


def draw_pause_screen(surface, font, font_sub, layout, time_=0.0, selected=0):
    """Pantalla de pausa: se muestra al presionar ESC/P durante una partida
    en curso (no en game over). Devuelve los rects de los botones para
    deteccion de clicks: (resume_rect, restart_rect, menu_rect)."""
    overlay = get_overlay(layout.win_w, layout.win_h, (0, 0, 0), 175)
    surface.blit(overlay, (0, 0))
    cx, cy = layout.win_w // 2, layout.win_h // 2
    box_w = int(layout.win_w * 0.42)
    box_h = int(layout.win_h * 0.5)
    box_x, box_y = cx - box_w // 2, cy - box_h // 2

    pulse = (math.sin(time_ * 2.4) + 1) / 2
    border_col = (int(90 + pulse * 40), int(140 + pulse * 40), int(220 + pulse * 25))
    pygame.draw.rect(surface, (22, 24, 34), (box_x, box_y, box_w, box_h), border_radius=12)
    pygame.draw.rect(surface, border_col, (box_x, box_y, box_w, box_h), 3, border_radius=12)

    glow = pygame.Surface((box_w, int(box_h * 0.3)), pygame.SRCALPHA)
    pygame.draw.ellipse(glow, (120, 160, 255, 30), (0, 0, box_w, int(box_h * 0.3)))
    surface.blit(glow, (box_x, box_y))

    # Icono de pausa (dos barras) junto al titulo.
    icon_w, icon_h = int(box_h * 0.07), int(box_h * 0.13)
    icon_gap = int(icon_w * 0.7)
    title_text = get_text("PAUSA", (210, 225, 255), font)
    icon_total_w = icon_w * 2 + icon_gap
    title_y = box_y + int(box_h * 0.10)
    icon_x = cx - (icon_total_w + 14 + title_text.get_width()) // 2
    icon_y = title_y + title_text.get_height() // 2 - icon_h // 2
    pygame.draw.rect(surface, (210, 225, 255), (icon_x, icon_y, icon_w, icon_h), border_radius=2)
    pygame.draw.rect(surface, (210, 225, 255), (icon_x + icon_w + icon_gap, icon_y, icon_w, icon_h), border_radius=2)
    surface.blit(title_text, (icon_x + icon_total_w + 14, title_y))

    sub_text = get_text("El juego esta en pausa", (170, 180, 200), font_sub)
    surface.blit(sub_text, (cx - sub_text.get_width() // 2, title_y + title_text.get_height() + 10))

    mx, my = pygame.mouse.get_pos()

    labels = [("resume", "Continuar", (60, 130, 90), (120, 220, 150)),
              ("restart", "Reiniciar", (60, 90, 130), (120, 170, 220)),
              ("menu", "Salir al menu", (100, 60, 60), (200, 120, 120))]

    btn_w = int(box_w * 0.72)
    btn_h = max(int(box_h * 0.12), 34)
    btn_x = cx - btn_w // 2
    btn_gap = int(box_h * 0.035)
    btn_start_y = box_y + int(box_h * 0.42)

    rects = []
    for i, (key, label, bg_off, bg_on) in enumerate(labels):
        by = btn_start_y + i * (btn_h + btn_gap)
        hover = btn_x <= mx <= btn_x + btn_w and by <= my <= by + btn_h
        is_sel = (i == selected) or hover
        bg = bg_on if is_sel else bg_off
        pygame.draw.rect(surface, bg, (btn_x, by, btn_w, btn_h), border_radius=8)
        border_c = (230, 240, 255) if is_sel else (90, 100, 120)
        pygame.draw.rect(surface, border_c, (btn_x, by, btn_w, btn_h), 2, border_radius=8)
        lbl_surf = get_text(label, (240, 245, 255), font_sub)
        surface.blit(lbl_surf, (cx - lbl_surf.get_width() // 2, by + btn_h // 2 - lbl_surf.get_height() // 2))
        rects.append((btn_x, by, btn_w, btn_h))

    hint = get_text("ESC / P para continuar", (120, 128, 145), font_sub)
    surface.blit(hint, (cx - hint.get_width() // 2, box_y + box_h - hint.get_height() - 14))

    return tuple(rects)


def draw_game_over(surface, font, font_sub, layout, score=0, best_score=0, is_new_record=False, show_best=True):
    overlay = get_overlay(layout.win_w, layout.win_h, (0, 0, 0), 170)
    surface.blit(overlay, (0, 0))
    cx, cy = layout.win_w // 2, layout.win_h // 2
    box_w = int(layout.win_w * 0.45)
    box_h = int(layout.win_h * 0.4)
    box_x, box_y = cx - box_w // 2, cy - box_h // 2
    pygame.draw.rect(surface, (30, 25, 40), (box_x, box_y, box_w, box_h), border_radius=10)
    pygame.draw.rect(surface, (180, 60, 60), (box_x, box_y, box_w, box_h), 3, border_radius=10)
    go_text = get_text("GAME OVER", (255, 80, 80), font)
    surface.blit(go_text, (cx - go_text.get_width() // 2, box_y + int(box_h * 0.14)))

    score_text = get_text(f"Puntaje: {score}", (220, 220, 220), font_sub)
    surface.blit(score_text, (cx - score_text.get_width() // 2, box_y + int(box_h * 0.4)))
    if show_best:
        if is_new_record:
            rec_text = get_text("¡NUEVO RECORD!", (255, 210, 90), font_sub)
            surface.blit(rec_text, (cx - rec_text.get_width() // 2, box_y + int(box_h * 0.4) + score_text.get_height() + 4))
        else:
            best_text = get_text(f"Mejor: {best_score}", (150, 160, 180), font_sub)
            surface.blit(best_text, (cx - best_text.get_width() // 2, box_y + int(box_h * 0.4) + score_text.get_height() + 4))

    mx, my = pygame.mouse.get_pos()

    btn_w = int(box_w * 0.6)
    btn_h = max(int(box_h * 0.12), 32)
    btn_gap = max(int(box_h * 0.04), 6)

    restart_y = box_y + int(box_h * 0.68)
    restart_rect = pygame.Rect(cx - btn_w // 2, restart_y, btn_w, btn_h)
    r_hover = restart_rect.collidepoint(mx, my)
    r_col = (70, 180, 90) if r_hover else (50, 140, 70)
    pygame.draw.rect(surface, r_col, restart_rect, border_radius=6)
    pygame.draw.rect(surface, (100, 220, 120) if r_hover else (70, 170, 90), restart_rect, 2, border_radius=6)
    r_text = get_text("Reiniciar", (255, 255, 255), font_sub)
    surface.blit(r_text, (cx - r_text.get_width() // 2, restart_y + btn_h // 2 - r_text.get_height() // 2))

    menu_y = restart_y + btn_h + btn_gap
    menu_rect = pygame.Rect(cx - btn_w // 2, menu_y, btn_w, btn_h)
    m_hover = menu_rect.collidepoint(mx, my)
    m_col = (100, 80, 130) if m_hover else (70, 55, 100)
    pygame.draw.rect(surface, m_col, menu_rect, border_radius=6)
    pygame.draw.rect(surface, (130, 110, 170) if m_hover else (90, 75, 120), menu_rect, 2, border_radius=6)
    m_text = get_text("Menu", (255, 255, 255), font_sub)
    surface.blit(m_text, (cx - m_text.get_width() // 2, menu_y + btn_h // 2 - m_text.get_height() // 2))

    return restart_rect, menu_rect


_music_win_confetti_cache = {}
def _get_music_win_confetti(box_w, box_h, seed=0):
    """Genera (y cachea) posiciones/colores fijos de confeti para la
    pantalla de victoria; solo la caida (offset vertical) se anima despues,
    asi no hay que recalcular random cada frame."""
    key = (box_w, box_h, seed)
    if key not in _music_win_confetti_cache:
        rnd = random.Random(1234 + seed)
        colors = [(255, 210, 90), (120, 255, 160), (120, 200, 255), (255, 130, 180), (200, 150, 255)]
        pieces = []
        for _ in range(28):
            pieces.append({
                "x": rnd.uniform(0, box_w),
                "y0": rnd.uniform(-box_h, 0),
                "speed": rnd.uniform(18, 46),
                "size": rnd.uniform(3, 6),
                "color": rnd.choice(colors),
                "sway": rnd.uniform(0.5, 2.0),
                "phase": rnd.uniform(0, math.tau),
            })
        _music_win_confetti_cache[key] = pieces
    return _music_win_confetti_cache[key]


def draw_music_win(surface, font, font_sub, layout, score, lines, time_=0.0, won_time=0.0,
                    difficulty=0, best_combo=0, song_name=""):
    """Pantalla de victoria del modo musical: se muestra cuando la cancion
    termina de sonar y el jugador sigue vivo (objetivo: sobrevivir hasta
    el final del tema). Devuelve los rects de los botones para deteccion
    de clicks: (restart_rect, menu_rect)."""
    overlay = get_overlay(layout.win_w, layout.win_h, (0, 0, 0), 180)
    surface.blit(overlay, (0, 0))
    cx, cy = layout.win_w // 2, layout.win_h // 2
    box_w = int(layout.win_w * 0.5)
    box_h = int(layout.win_h * 0.56)

    # Animacion de entrada: la tarjeta aparece con un ligero rebote y
    # deslizamiento hacia arriba en vez de aparecer de golpe.
    t_since = max(0.0, time_ - won_time)
    t_in = min(t_since * 2.2, 1.0)
    ease = ease_out_back(t_in)
    scale = 0.7 + 0.3 * ease
    cur_w, cur_h = int(box_w * scale), int(box_h * scale)
    box_x, box_y = cx - cur_w // 2, cy - cur_h // 2 + int((1 - ease) * 40)
    alpha_in = int(min(t_since * 4.0, 1.0) * 255)

    pulse = (math.sin(time_ * 3) + 1) / 2
    border_col = (int(80 + pulse * 40), int(210 + pulse * 30), int(120 + pulse * 30))

    card = pygame.Surface((cur_w, cur_h), pygame.SRCALPHA)
    pygame.draw.rect(card, (18, 30, 24, 245), (0, 0, cur_w, cur_h), border_radius=14)

    glow = pygame.Surface((cur_w, int(cur_h * 0.38)), pygame.SRCALPHA)
    pygame.draw.ellipse(glow, (100, 255, 150, 35), (0, 0, cur_w, int(cur_h * 0.38)))
    card.blit(glow, (0, 0))

    # Confeti cayendo dentro de la tarjeta, recortado a sus bordes.
    if t_in > 0.4:
        confetti_alpha = int(min((t_in - 0.4) / 0.3, 1.0) * 255)
        for p in _get_music_win_confetti(box_w, box_h):
            fall = (t_since * p["speed"]) % (box_h + 30)
            py = p["y0"] + fall
            if py < -10 or py > cur_h + 10:
                continue
            px = p["x"] * scale + math.sin(time_ * p["sway"] + p["phase"]) * 8
            if not (0 <= px <= cur_w):
                continue
            csize = max(1, int(p["size"] * scale))
            csurf = pygame.Surface((csize * 2, csize * 2), pygame.SRCALPHA)
            pygame.draw.rect(csurf, (*p["color"], confetti_alpha), (0, 0, csize * 2, csize * 2), border_radius=1)
            card.blit(csurf, (px - csize, py - csize))

    pygame.draw.rect(card, (*border_col, 255), (0, 0, cur_w, cur_h), 3, border_radius=14)
    card.set_alpha(alpha_in)
    surface.blit(card, (box_x, box_y))

    # A partir de aca se dibuja directo sobre "surface" (no la tarjeta) con
    # su propia alpha, para que el texto no se vea borroso por el
    # downscale/alpha de la Surface intermedia.
    text_alpha = alpha_in

    title_scale = 1.0 + (1.0 - ease) * 0.6
    win_base = get_text("¡GANASTE!", (120, 255, 160), font)
    if title_scale != 1.0:
        tw, th = win_base.get_size()
        win_text = pygame.transform.smoothscale(win_base, (max(1, int(tw * title_scale)), max(1, int(th * title_scale))))
    else:
        win_text = win_base
    win_text = win_text.copy()
    win_text.set_alpha(text_alpha)
    title_y = box_y + int(cur_h * 0.11)
    surface.blit(win_text, (cx - win_text.get_width() // 2, title_y))

    sub_text = get_text("Sobreviviste hasta el final de la canción", (200, 220, 205), font_sub)
    sub_text = sub_text.copy()
    sub_text.set_alpha(text_alpha)
    sub_y = title_y + win_base.get_height() + 8
    surface.blit(sub_text, (cx - sub_text.get_width() // 2, sub_y))

    if song_name:
        song_surf = get_text(song_name, (140, 190, 220), font_sub)
        song_surf = song_surf.copy()
        song_surf.set_alpha(text_alpha)
        surface.blit(song_surf, (cx - song_surf.get_width() // 2, sub_y + sub_text.get_height() + 4))

    # Tarjetitas de estadisticas (puntaje / lineas / mejor combo / dificultad)
    # en vez de simples lineas de texto sueltas.
    stats = [
        ("PUNTAJE", f"{score}", (230, 230, 230)),
        ("LINEAS", f"{lines}", (230, 230, 230)),
        ("MEJOR COMBO", f"x{best_combo}" if best_combo > 0 else "-", (255, 200, 100)),
        ("DIFICULTAD", "FACIL" if difficulty == 0 else "DIFICIL", (100, 200, 100) if difficulty == 0 else (255, 120, 120)),
    ]
    stats_top = box_y + int(cur_h * 0.42)
    stat_gap = 10
    stat_w = int((cur_w * 0.86 - stat_gap * (len(stats) - 1)) / len(stats))
    stat_h = max(int(cur_h * 0.16), 40)
    stats_left = cx - int(cur_w * 0.86) // 2
    for i, (label, value, color) in enumerate(stats):
        sx = stats_left + i * (stat_w + stat_gap)
        stat_surf = pygame.Surface((stat_w, stat_h), pygame.SRCALPHA)
        pygame.draw.rect(stat_surf, (255, 255, 255, 18), (0, 0, stat_w, stat_h), border_radius=8)
        pygame.draw.rect(stat_surf, (*color, 90), (0, 0, stat_w, stat_h), 1, border_radius=8)
        label_surf = font_sub.render(label, True, (170, 190, 180))
        label_surf = pygame.transform.smoothscale(label_surf, (min(label_surf.get_width(), stat_w - 8), label_surf.get_height())) if label_surf.get_width() > stat_w - 8 else label_surf
        value_surf = font_sub.render(value, True, color)
        stat_surf.blit(label_surf, (stat_w // 2 - label_surf.get_width() // 2, 6))
        stat_surf.blit(value_surf, (stat_w // 2 - value_surf.get_width() // 2, stat_h - value_surf.get_height() - 6))
        stat_surf.set_alpha(text_alpha)
        surface.blit(stat_surf, (sx, stats_top))

    mx, my = pygame.mouse.get_pos()

    btn_w = int(cur_w * 0.65)
    btn_h = max(int(cur_h * 0.115), 32)
    btn_x = cx - btn_w // 2
    btn_restart_y = box_y + int(cur_h * 0.68)
    btn_menu_y = btn_restart_y + btn_h + int(cur_h * 0.035)

    restart_hover = btn_x <= mx <= btn_x + btn_w and btn_restart_y <= my <= btn_restart_y + btn_h
    menu_hover = btn_x <= mx <= btn_x + btn_w and btn_menu_y <= my <= btn_menu_y + btn_h

    restart_bg = (55, 135, 90) if restart_hover else (35, 80, 55)
    menu_bg = (100, 65, 65) if menu_hover else (65, 42, 42)

    btn_layer = pygame.Surface((btn_w, btn_h * 2 + int(cur_h * 0.035)), pygame.SRCALPHA)
    pygame.draw.rect(btn_layer, restart_bg, (0, 0, btn_w, btn_h), border_radius=6)
    pygame.draw.rect(btn_layer, (120, 220, 150), (0, 0, btn_w, btn_h), 2, border_radius=6)
    restart_label = get_text("Reiniciar", (230, 255, 235), font_sub)
    btn_layer.blit(restart_label, (btn_w // 2 - restart_label.get_width() // 2, btn_h // 2 - restart_label.get_height() // 2))

    menu_local_y = btn_h + int(cur_h * 0.035)
    pygame.draw.rect(btn_layer, menu_bg, (0, menu_local_y, btn_w, btn_h), border_radius=6)
    pygame.draw.rect(btn_layer, (200, 120, 120), (0, menu_local_y, btn_w, btn_h), 2, border_radius=6)
    menu_label = get_text("Salir", (255, 220, 220), font_sub)
    btn_layer.blit(menu_label, (btn_w // 2 - menu_label.get_width() // 2, menu_local_y + btn_h // 2 - menu_label.get_height() // 2))

    btn_layer.set_alpha(text_alpha)
    surface.blit(btn_layer, (btn_x, btn_restart_y))

    return (btn_x, btn_restart_y, btn_w, btn_h), (btn_x, btn_menu_y, btn_w, btn_h)


_grid_lines_cache = {}
def get_grid_lines(gx, gy, gw, gh, cell):
    key = (gx, gy, gw, gh, cell)
    if key not in _grid_lines_cache:
        surf = pygame.Surface((gw, gh), pygame.SRCALPHA)
        for i in range(COLS + 1):
            x = i * cell
            pygame.draw.line(surf, (22, 24, 30, 255), (x, 0), (x, gh))
        for i in range(ROWS + 1):
            y = i * cell
            pygame.draw.line(surf, (22, 24, 30, 255), (0, y), (gw, y))
        _grid_lines_cache[key] = surf
    return _grid_lines_cache[key]


_bg_cache = {}
CUSTOM_BG_INDEX = len(BG_NAMES)  # slot extra al final del ciclo de fondos
BG_STYLE_COUNT = len(BG_NAMES) + 1
_custom_bg_img_cache = {}


def get_bg_style_name(bg_style, settings):
    if bg_style < len(BG_NAMES):
        return BG_NAMES[bg_style]
    return "Personalizado" if settings.get("custom_bg_path") else "Personalizado (sin imagen)"


def load_custom_bg_image(path):
    """Carga y cachea la imagen de fondo elegida por el usuario. Se guarda
    cruda (sin escalar) para poder re-escalarla si la ventana cambia de
    tamano."""
    if path in _custom_bg_img_cache:
        return _custom_bg_img_cache[path]
    try:
        img = pygame.image.load(path).convert()
    except Exception:
        img = None
    _custom_bg_img_cache[path] = img
    return img


_avatar_img_cache = {}
_avatar_circle_cache = {}


def load_avatar_image(path):
    """Carga (y cachea) la imagen elegida como foto de perfil, cruda y con
    canal alfa por si el PNG tiene transparencia."""
    if path in _avatar_img_cache:
        return _avatar_img_cache[path]
    img = None
    try:
        img = pygame.image.load(path).convert_alpha()
    except Exception:
        img = None
    _avatar_img_cache[path] = img
    return img


def encode_avatar_image(path):
    """Carga una imagen de disco, la achica y la devuelve codificada en
    base64 (PNG) para guardarla embebida en accounts.json. Devuelve
    (base64_str, None) si salio bien, o (None, error_msg)."""
    try:
        img = pygame.image.load(path)
    except Exception:
        return None, "No se pudo abrir esa imagen"
    try:
        img = img.convert_alpha()
    except Exception:
        pass
    w, h = img.get_width(), img.get_height()
    # Recorte cuadrado centrado antes de achicar, para no deformar la cara
    # de la persona al forzarla despues a un circulo.
    side = min(w, h)
    if side <= 0:
        return None, "Imagen invalida"
    crop_x, crop_y = (w - side) // 2, (h - side) // 2
    square = pygame.Surface((side, side), pygame.SRCALPHA)
    square.blit(img, (0, 0), (crop_x, crop_y, side, side))
    if side > AVATAR_IMAGE_MAX_W:
        square = pygame.transform.smoothscale(square, (AVATAR_IMAGE_MAX_W, AVATAR_IMAGE_MAX_W))
    buf = io.BytesIO()
    try:
        pygame.image.save(square, buf, "avatar.png")
    except Exception:
        try:
            buf = io.BytesIO()
            pygame.image.save(square, buf)
        except Exception:
            return None, "No se pudo codificar la imagen"
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    if len(b64) > AVATAR_IMAGE_MAX_B64:
        return None, "Imagen muy pesada, proba con una mas chica"
    return b64, None


_avatar_data_img_cache = {}
_avatar_data_circle_cache = {}


def load_avatar_image_data(b64_data):
    """Decodifica (y cachea) la foto de perfil embebida en base64."""
    if not b64_data:
        return None
    if b64_data in _avatar_data_img_cache:
        return _avatar_data_img_cache[b64_data]
    img = None
    try:
        raw = base64.b64decode(b64_data)
        img = pygame.image.load(io.BytesIO(raw)).convert_alpha()
    except Exception:
        img = None
    _avatar_data_img_cache[b64_data] = img
    return img


def get_avatar_circle_data(b64_data, diameter):
    """Igual que get_avatar_circle pero a partir de la imagen embebida en
    base64 en vez de una ruta de archivo."""
    if not b64_data:
        return None
    key = (b64_data, diameter)
    if key in _avatar_data_circle_cache:
        return _avatar_data_circle_cache[key]
    src = load_avatar_image_data(b64_data)
    result = None
    if src is not None and src.get_width() > 0 and src.get_height() > 0:
        sw, sh = src.get_width(), src.get_height()
        scale = max(diameter / sw, diameter / sh)
        new_w, new_h = max(1, int(sw * scale)), max(1, int(sh * scale))
        scaled = pygame.transform.smoothscale(src, (new_w, new_h))
        off_x = (new_w - diameter) // 2
        off_y = (new_h - diameter) // 2
        square = pygame.Surface((diameter, diameter), pygame.SRCALPHA)
        square.blit(scaled, (-off_x, -off_y))

        mask = pygame.Surface((diameter, diameter), pygame.SRCALPHA)
        pygame.draw.circle(mask, (255, 255, 255, 255), (diameter // 2, diameter // 2), diameter // 2)
        square.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        result = square
    _avatar_data_circle_cache[key] = result
    return result


def get_avatar_circle(path, diameter):
    """Devuelve la foto de perfil recortada a un circulo de `diameter` px
    (escalado tipo "cover", centrado), cacheada por (path, diameter).
    Devuelve None si no hay imagen o no se pudo cargar, para que quien
    llame use el circulo de color como respaldo."""
    if not path:
        return None
    key = (path, diameter)
    if key in _avatar_circle_cache:
        return _avatar_circle_cache[key]
    src = load_avatar_image(path)
    result = None
    if src is not None and src.get_width() > 0 and src.get_height() > 0:
        sw, sh = src.get_width(), src.get_height()
        scale = max(diameter / sw, diameter / sh)
        new_w, new_h = max(1, int(sw * scale)), max(1, int(sh * scale))
        scaled = pygame.transform.smoothscale(src, (new_w, new_h))
        off_x = (new_w - diameter) // 2
        off_y = (new_h - diameter) // 2
        square = pygame.Surface((diameter, diameter), pygame.SRCALPHA)
        square.blit(scaled, (-off_x, -off_y))

        mask = pygame.Surface((diameter, diameter), pygame.SRCALPHA)
        pygame.draw.circle(mask, (255, 255, 255, 255), (diameter // 2, diameter // 2), diameter // 2)
        square.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        result = square
    _avatar_circle_cache[key] = result
    return result


def draw_gradient_bg(surface, win_w, win_h, style=0, custom_path=None):
    key = (win_w, win_h, style, custom_path if style == CUSTOM_BG_INDEX else None)
    if key not in _bg_cache:
        bg = pygame.Surface((win_w, win_h))
        custom_drawn = False
        if low_end and style != CUSTOM_BG_INDEX:
            bg.fill((15, 14, 22))
            _bg_cache[key] = bg
            surface.blit(bg, (0, 0))
            return
        if style == CUSTOM_BG_INDEX and custom_path:
            src = load_custom_bg_image(custom_path)
            if src is not None:
                # Escala tipo "cover": llena toda la ventana preservando la
                # proporcion, recortando el sobrante centrado.
                src_w, src_h = src.get_width(), src.get_height()
                scale = max(win_w / src_w, win_h / src_h)
                new_w, new_h = max(1, int(src_w * scale)), max(1, int(src_h * scale))
                scaled = pygame.transform.smoothscale(src, (new_w, new_h))
                ox = (new_w - win_w) // 2
                oy = (new_h - win_h) // 2
                bg.blit(scaled, (0, 0), (ox, oy, win_w, win_h))
                # Oscurecido leve parejo para que el HUD siga siendo
                # legible sin importar que tan clara sea la imagen.
                dark = pygame.Surface((win_w, win_h), pygame.SRCALPHA)
                dark.fill((0, 0, 0, 110))
                bg.blit(dark, (0, 0))
                custom_drawn = True
        # Si el estilo pedido es el personalizado pero no hay imagen
        # cargada (o no se pudo abrir), se cae de vuelta al gradiente 0.
        if not custom_drawn:
            if style == 0 or style == CUSTOM_BG_INDEX:
                for y in range(win_h):
                    t = y / win_h
                    pygame.draw.line(bg, (int(12 + t * 45), int(8 + t * 18), int(18 + t * 12)), (0, y), (win_w, y))
            elif style == 1:
                bg.fill((12, 12, 18))
            elif style == 2:
                for y in range(win_h):
                    t = y / win_h
                    pygame.draw.line(bg, (int(8 + t * 15), int(15 + t * 35), int(30 + t * 50)), (0, y), (win_w, y))
            elif style == 3:
                # Atardecer: violeta oscuro arriba -> naranja/rosa abajo.
                for y in range(win_h):
                    t = y / win_h
                    pygame.draw.line(bg, (int(35 + t * 180), int(12 + t * 70), int(55 + t * 40)), (0, y), (win_w, y))
            elif style == 4:
                # Synthwave: magenta/morado profundo -> cian oscuro abajo.
                for y in range(win_h):
                    t = y / win_h
                    pygame.draw.line(bg, (int(30 + t * 40), int(6 + t * 30), int(45 + t * 70)), (0, y), (win_w, y))
            elif style == 5:
                # Bosque: verde muy oscuro con un leve tinte mas claro abajo.
                for y in range(win_h):
                    t = y / win_h
                    pygame.draw.line(bg, (int(8 + t * 10), int(20 + t * 35), int(14 + t * 15)), (0, y), (win_w, y))
            elif style == 6:
                # Sangre: casi negro arriba -> rojo oscuro abajo.
                for y in range(win_h):
                    t = y / win_h
                    pygame.draw.line(bg, (int(10 + t * 70), int(6 + t * 10), int(8 + t * 12)), (0, y), (win_w, y))
            elif style == 7:
                # Mono: gris a gris, sin tinte de color.
                for y in range(win_h):
                    t = y / win_h
                    v = int(14 + t * 30)
                    pygame.draw.line(bg, (v, v, v), (0, y), (win_w, y))
        _bg_cache[key] = bg
    surface.blit(_bg_cache[key], (0, 0))


def draw_block_icon(surface, x, y, size, pattern, color):
    bs = size // 4
    lighter = tuple(min(c + 40, 255) for c in color)
    darker = tuple(max(c - 40, 0) for c in color)
    for by, row in enumerate(pattern):
        for bx, c in enumerate(row):
            if c:
                r = pygame.Rect(x + bx * bs, y + by * bs, bs - 2, bs - 2)
                pygame.draw.rect(surface, color, r, border_radius=2)
                pygame.draw.line(surface, lighter, r.topleft, (r.right - 1, r.top))
                pygame.draw.line(surface, lighter, r.topleft, (r.left, r.bottom - 1))
                pygame.draw.line(surface, darker, (r.right - 1, r.top), r.bottomright)
                pygame.draw.line(surface, darker, (r.left, r.bottom - 1), (r.right - 1, r.bottom - 1))


ICONS = {
    "play": [[1, 1, 1, 0], [0, 0, 1, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
    "options": [[1, 0, 1, 0], [0, 1, 0, 0], [1, 0, 1, 0], [0, 0, 0, 0]],
    "exit": [[1, 1, 1, 1], [1, 0, 0, 0], [1, 0, 0, 0], [1, 1, 1, 1]],
    "ctrl": [[1, 0, 1, 0], [0, 1, 0, 0], [1, 0, 1, 0], [0, 1, 0, 0]],
    "back": [[0, 0, 1, 0], [0, 1, 0, 0], [1, 1, 1, 0], [0, 0, 0, 0]],
    "multi": [[1, 0, 0, 1], [0, 1, 1, 0], [0, 1, 1, 0], [1, 0, 0, 1]],
    "endless": [[1, 1, 1, 1], [1, 0, 0, 0], [1, 0, 0, 0], [1, 1, 1, 1]],
    "vs": [[0, 1, 0, 1], [0, 1, 0, 1], [0, 1, 0, 1], [0, 0, 0, 0]],
    "personal": [[1, 0, 1, 0], [0, 1, 0, 0], [0, 1, 0, 0], [1, 0, 1, 0]],
    "sound": [[0, 1, 0, 0], [1, 0, 1, 0], [1, 0, 1, 0], [0, 1, 0, 0]],
    "game": [[0, 1, 1, 0], [0, 1, 0, 0], [0, 1, 1, 0], [0, 1, 0, 0]],
    "das": [[1, 1, 0, 0], [1, 0, 0, 0], [1, 1, 0, 0], [0, 0, 0, 0]],
    "arr": [[0, 1, 0, 0], [0, 1, 1, 0], [0, 1, 0, 0], [0, 0, 0, 0]],
    "lock": [[0, 1, 1, 0], [1, 0, 0, 1], [1, 0, 0, 1], [0, 1, 1, 0]],
    "ghost": [[1, 0, 1, 0], [0, 1, 0, 0], [1, 0, 1, 0], [0, 0, 0, 0]],
    "level": [[0, 1, 0, 0], [1, 1, 1, 0], [0, 1, 0, 0], [0, 0, 0, 0]],
    "queue": [[1, 1, 0, 0], [0, 1, 1, 0], [0, 0, 1, 1], [0, 0, 0, 0]],
    "bg": [[1, 0, 1, 0], [0, 1, 0, 1], [1, 0, 1, 0], [0, 1, 0, 1]],
    "volume": [[0, 1, 1, 0], [0, 1, 1, 0], [1, 0, 0, 1], [1, 0, 0, 1]],
    "music": [[0, 1, 0, 0], [1, 1, 1, 0], [1, 1, 1, 0], [0, 1, 0, 0]],
    "sfx": [[1, 0, 0, 0], [1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0]],
    "colors": [[1, 0, 0, 1], [0, 1, 1, 0], [0, 1, 1, 0], [1, 0, 0, 1]],
    "chat": [[1, 1, 1, 0], [1, 0, 1, 0], [1, 1, 0, 0], [0, 1, 0, 0]],
}

PALETTE_MENU = [
    ((180, 60, 60), (100, 30, 30)),
    ((80, 50, 140), (45, 25, 80)),
    ((50, 120, 130), (30, 70, 80)),
]
PALETTE_OPTIONS = [
    ((70, 120, 160), (40, 70, 100)),
    ((130, 80, 50), (80, 45, 25)),
    ((100, 50, 120), (60, 30, 75)),
    ((50, 120, 80), (30, 75, 45)),
]
PALETTE_PLAY = [
    ((70, 130, 170), (40, 80, 110)),
    ((160, 100, 50), (100, 60, 30)),
    ((90, 70, 150), (55, 40, 95)),
    ((50, 140, 90), (30, 90, 55)),
]
PALETTE_GAME = [
    ((70, 120, 160), (40, 70, 100)),
    ((160, 100, 50), (100, 60, 30)),
    ((50, 120, 80), (30, 75, 45)),
    ((100, 50, 120), (60, 30, 75)),
    ((140, 100, 50), (90, 65, 30)),
    ((80, 80, 140), (50, 50, 90)),
]
PALETTE_SOUND = [
    ((70, 120, 160), (40, 70, 100)),
    ((130, 80, 50), (80, 45, 25)),
    ((50, 120, 80), (30, 75, 45)),
]


_particle_circle_cache = {}

def _get_particle_circle(size, alpha):
    """Cachea los circulos de particulas por (tamano, alpha redondeado a
    multiplos de 10). Antes se creaba una pygame.Surface nueva por
    particula en CADA frame (25 particulas x 60fps = 1500 Surface/seg);
    ahora se reusan sprites pre-renderizados."""
    alpha_bucket = max(0, min(255, (alpha // 10) * 10))
    key = (size, alpha_bucket)
    if key not in _particle_circle_cache:
        s = max(1, size)
        surf = pygame.Surface((s * 2, s * 2), pygame.SRCALPHA)
        pygame.draw.circle(surf, (150, 160, 200, alpha_bucket), (s, s), s)
        _particle_circle_cache[key] = surf
    return _particle_circle_cache[key]


class Particles:
    def __init__(self):
        self.particles = []
        for _ in range(25):
            self.particles.append({
                "x": random.uniform(0, 1),
                "y": random.uniform(0, 1),
                "speed": random.uniform(0.0001, 0.0004),
                "size": random.uniform(1, 3),
                "alpha": random.randint(20, 70),
                "drift": random.uniform(-0.0002, 0.0002),
            })

    def update(self, dt):
        if low_end:
            return
        for p in self.particles:
            p["y"] -= p["speed"] * dt * 0.06
            p["x"] += p["drift"] * dt * 0.06
            if p["y"] < -0.05:
                p["y"] = 1.05
                p["x"] = random.uniform(0, 1)
            if p["x"] < -0.05 or p["x"] > 1.05:
                p["x"] = random.uniform(0.1, 0.9)

    def draw(self, surface, win_w, win_h, time):
        if low_end:
            return
        for p in self.particles:
            px = int(p["x"] * win_w)
            py = int(p["y"] * win_h)
            flicker = (math.sin(time * 2 + p["x"] * 10) + 1) / 2
            a = int(p["alpha"] * (0.5 + flicker * 0.5))
            s = max(1, int(p["size"]))
            psurf = _get_particle_circle(s, a)
            surface.blit(psurf, (px - s, py - s))


class FadeTransition:
    def __init__(self):
        self.alpha = 0
        self.fading = False
        self.fade_out = False
        self.callback = None
        self.duration = 250

    def start(self, callback):
        self.fading = True
        self.fade_out = True
        self.alpha = 0
        self.callback = callback

    def update(self, dt):
        if not self.fading:
            return
        if self.fade_out:
            self.alpha = min(255, self.alpha + int(255 * dt / self.duration))
            if self.alpha >= 255:
                if self.callback:
                    self.callback()
                self.callback = None
                self.fade_out = False
        else:
            self.alpha = max(0, self.alpha - int(255 * dt / self.duration))
            if self.alpha <= 0:
                self.fading = False

    def draw(self, surface, win_w, win_h):
        if self.alpha > 0:
            overlay = get_overlay(win_w, win_h, (0, 0, 0), self.alpha)
            surface.blit(overlay, (0, 0))


class AnimationState:
    def __init__(self):
        self.time = 0
        self.hover_idx = -1
        self.hover_t = 0

    def reset(self):
        self.time = 0
        self.hover_idx = -1
        self.hover_t = 0

    def update(self, dt):
        self.time += dt / 1000.0
        self.hover_t = min(self.hover_t + dt / 1000.0 * 5, 1.0)


def ease_out_back(t):
    c1 = 1.70158
    c3 = c1 + 1
    return 1 + c3 * pow(t - 1, 3) + c1 * pow(t - 1, 2)


def ease_out_cubic(t):
    return 1 - pow(1 - t, 3)


def ease_out_elastic(t):
    if t == 0 or t == 1:
        return t
    return pow(2, -10 * t) * math.sin((t * 10 - 0.75) * (2 * math.pi / 3)) + 1


_button_glow_cache = {}
_button_bar_cache = {}
_button_text_cache = {}
_action_btn_cache = {}

def draw_button(surface, x, y, w, h, color, dark_color, selected, icon_key, title, subtitle, font_title, font_sub, anim_t=1.0, time=0, hover=False):
    slide = ease_out_back(min(anim_t, 1.0))
    draw_x = x - int((1 - slide) * 400)

    bg = color if selected else dark_color
    draw_h = h
    draw_y = y
    if hover and not selected:
        scale = 1.02 + math.sin(time * 4) * 0.01
        new_h = int(h * scale)
        draw_y = y - (new_h - h) // 2
        draw_h = new_h

    if selected:
        shadow_key = (w, draw_h)
        if shadow_key not in _button_glow_cache.setdefault('_shadow', {}):
            sh = pygame.Surface((w + 16, draw_h + 16), pygame.SRCALPHA)
            pygame.draw.rect(sh, (0, 0, 0, 90), (8, 10, w, draw_h), border_radius=10)
            _button_glow_cache['_shadow'][shadow_key] = sh
        surface.blit(_button_glow_cache['_shadow'][shadow_key], (draw_x - 8, draw_y - 6))

    pygame.draw.rect(surface, bg, (draw_x, draw_y, w, draw_h), border_radius=6)

    if selected:
        glow_size = int(4 + math.sin(time * 3) * 2)
        pygame.draw.rect(surface, (255, 255, 255), (draw_x, draw_y, glow_size, draw_h), border_radius=3)
        glow_color = tuple(min(c + 80, 255) for c in bg)
        # glow_surf y bar_surf dependen solo de (w, draw_h, glow_color) y
        # (draw_h) respectivamente, que cambian muy poco entre frames
        # (el layout es casi siempre el mismo tamano) -> cachear en vez de
        # crear una Surface nueva por boton seleccionado en cada frame.
        glow_key = (w, draw_h, glow_color)
        if glow_key not in _button_glow_cache:
            gs = pygame.Surface((w, draw_h), pygame.SRCALPHA)
            pygame.draw.rect(gs, (*glow_color, 50), (0, 0, w, draw_h), border_radius=6)
            _button_glow_cache[glow_key] = gs
        surface.blit(_button_glow_cache[glow_key], (draw_x, draw_y - 3))

        bar_key = draw_h
        if bar_key not in _button_bar_cache:
            bs = pygame.Surface((6, draw_h - 8), pygame.SRCALPHA)
            pygame.draw.rect(bs, (255, 255, 255, 200), (0, 0, 6, draw_h - 8), border_radius=3)
            _button_bar_cache[bar_key] = bs
        surface.blit(_button_bar_cache[bar_key], (draw_x + 8, draw_y + 4))

    icon_size = int(draw_h * 0.55)
    if selected:
        icon_size = int(icon_size * (1.0 + math.sin(time * 3.5) * 0.05))
    icon_x = draw_x + int(draw_h * 0.3)
    icon_y = draw_y + (draw_h - icon_size) // 2
    ic = (255, 255, 255) if selected else (160, 160, 160)
    if hover and not selected:
        ic = (200, 200, 200)
    draw_block_icon(surface, icon_x, icon_y, icon_size, ICONS.get(icon_key, ICONS["play"]), ic)

    text_x = icon_x + icon_size + int(draw_h * 0.35)
    title_key = (title, id(font_title))
    title_surf = _button_text_cache.get(title_key)
    if title_surf is None:
        title_surf = font_title.render(title, True, WHITE)
        if len(_button_text_cache) < 200:
            _button_text_cache[title_key] = title_surf
    if subtitle:
        sub_color = (200, 200, 200) if selected else (130, 130, 130)
        if hover and not selected:
            sub_color = (170, 170, 170)
        sub_key = (subtitle, sub_color, id(font_sub))
        sub_surf = _button_text_cache.get(sub_key)
        if sub_surf is None:
            sub_surf = font_sub.render(subtitle, True, sub_color)
            if len(_button_text_cache) < 200:
                _button_text_cache[sub_key] = sub_surf
        total_h = title_surf.get_height() + sub_surf.get_height() + 2
        surface.blit(title_surf, (text_x, draw_y + draw_h // 2 - total_h // 2))
        surface.blit(sub_surf, (text_x, draw_y + draw_h // 2 - total_h // 2 + title_surf.get_height() + 2))
    else:
        surface.blit(title_surf, (text_x, draw_y + draw_h // 2 - title_surf.get_height() // 2))


def draw_action_button(screen, x, y, w, h, base_color, hover_color, label, font, hover, pressed, time_=0.0):
    scale = 1.0
    if pressed:
        scale = 0.96
    elif hover:
        scale = 1.03 + math.sin(time_ * 6) * 0.01
    dw, dh = int(w * scale), int(h * scale)
    dx, dy = x - (dw - w) // 2, y - (dh - h) // 2
    color = hover_color if (hover or pressed) else base_color
    if pressed:
        color = tuple(max(0, c - 25) for c in color)
    pygame.draw.rect(screen, color, (dx, dy, dw, dh), border_radius=6)
    if hover and not pressed:
        glow_key = ("ab_glow", dw, dh)
        glow = _action_btn_cache.get(glow_key)
        if glow is None:
            glow = pygame.Surface((dw, dh), pygame.SRCALPHA)
            pygame.draw.rect(glow, (255, 255, 255, 35), (0, 0, dw, dh), border_radius=6)
            if len(_action_btn_cache) < 30:
                _action_btn_cache[glow_key] = glow
        screen.blit(glow, (dx, dy))
    lbl_key = ("ab_lbl", label, id(font))
    lbl = _action_btn_cache.get(lbl_key)
    if lbl is None:
        lbl = font.render(label, True, WHITE)
        if len(_action_btn_cache) < 30:
            _action_btn_cache[lbl_key] = lbl
    screen.blit(lbl, (dx + dw // 2 - lbl.get_width() // 2, dy + dh // 2 - lbl.get_height() // 2))


class FallingBlocks:
    """Fondo decorativo: piezas de tetris cayendo lento y girando en el
    aire, con opacidad baja para no distraer. Se usa en login y en la
    pantalla de carga para reforzar el tema del juego."""
    def __init__(self, count=9):
        self.blocks = []
        for _ in range(count):
            self._spawn(random.uniform(-1.0, 1.0))

    def _spawn(self, y):
        shape_idx = random.randrange(len(SHAPES))
        shape = SHAPES[shape_idx]
        cell = random.uniform(10, 18)
        alpha = random.randint(16, 38)
        color = (*COLORS[shape_idx], alpha)
        rows, cols = len(shape), len(shape[0])
        base = pygame.Surface((int(cols * cell), int(rows * cell)), pygame.SRCALPHA)
        for ry, row in enumerate(shape):
            for cx_, v in enumerate(row):
                if v:
                    pygame.draw.rect(base, color, (cx_ * cell, ry * cell, cell - 1, cell - 1), border_radius=2)
        self.blocks.append({
            "surf": base,
            "x": random.uniform(0.05, 0.95),
            "y": y,
            "speed": random.uniform(0.00006, 0.00014),
            "rot": random.uniform(0, 360),
            "rot_speed": random.uniform(-18, 18),
        })

    def update(self, dt):
        if low_end:
            return
        for b in self.blocks:
            b["y"] += b["speed"] * dt
            b["rot"] = (b["rot"] + b["rot_speed"] * dt / 1000.0) % 360
            if b["y"] > 1.15:
                b["y"] = -0.15
                b["x"] = random.uniform(0.05, 0.95)

    def draw(self, surface, win_w, win_h):
        if low_end:
            return
        for b in self.blocks:
            rotated = pygame.transform.rotate(b["surf"], b["rot"])
            px = int(b["x"] * win_w - rotated.get_width() / 2)
            py = int(b["y"] * win_h - rotated.get_height() / 2)
            surface.blit(rotated, (px, py))


_LOGO_ICON_PATTERN = [
    [0, 1, 0, 0],
    [1, 1, 1, 0],
    [0, 0, 0, 0],
    [0, 0, 0, 0],
]

_LOGO_LETTER_COLORS = [COLORS[2], COLORS[4], COLORS[5], COLORS[0]]


def _rotate_pattern_cw(pattern):
    rows = len(pattern)
    cols = len(pattern[0])
    return [[pattern[rows - 1 - r][c] for r in range(rows)] for c in range(cols)]


def draw_tepy_logo(surface, x, y, font_title, time_, alpha=255, align="left", with_icon=True):
    """Logo TEPY reutilizable: cada letra en un color de pieza distinta,
    resplandor pulsante y un pequeno tetromino que gira al costado.
    Se usa en el login, la pantalla de carga y la cabecera del menu
    principal para que el logo se vea igual (y mejor) en todos lados.
    Devuelve (ancho, alto) del bloque dibujado."""
    letters = "TEPY"
    letter_surfs = [font_title.render(ch, True, WHITE) for ch in letters]
    h = letter_surfs[0].get_height()
    icon_size = int(h * 0.8)
    icon_gap = int(h * 0.22) if with_icon else 0
    text_w = sum(s.get_width() for s in letter_surfs)
    full_w = text_w + (icon_size + icon_gap if with_icon else 0)

    start_x = x - full_w // 2 if align == "center" else x
    pulse = (math.sin(time_ * 2) + 1) / 2
    cur_x = start_x

    if with_icon:
        rot_step = int(time_ * 0.8) % 4
        pattern = _LOGO_ICON_PATTERN
        for _ in range(rot_step):
            pattern = _rotate_pattern_cw(pattern)
        icon_y = y + (h - icon_size) // 2
        icon_col = tuple(min(c + int(pulse * 30), 255) for c in COLORS[2])
        icon_surf = pygame.Surface((icon_size, icon_size), pygame.SRCALPHA)
        draw_block_icon(icon_surf, 0, 0, icon_size, pattern, icon_col)
        if alpha < 255:
            icon_surf.set_alpha(alpha)
        surface.blit(icon_surf, (cur_x, icon_y))
        cur_x += icon_size + icon_gap

    for ch, ch_surf, color in zip(letters, letter_surfs, _LOGO_LETTER_COLORS):
        glow_col = tuple(min(c + int(60 + pulse * 40), 255) for c in color)
        glow_a = int(90 + pulse * 50)
        for offset in [(2, 0), (-2, 0), (0, 2), (0, -2)]:
            gs = font_title.render(ch, True, glow_col)
            gs.set_alpha(int(glow_a * (alpha / 255.0)))
            surface.blit(gs, (cur_x + offset[0], y + offset[1]))
        colored = font_title.render(ch, True, color)
        colored.set_alpha(alpha)
        surface.blit(colored, (cur_x, y))
        cur_x += ch_surf.get_width()

    return full_w, h


def draw_menu_bg(screen, win_w, win_h, anim_state, particles, falling_blocks=None):
    draw_gradient_bg(screen, win_w, win_h, 0)
    if falling_blocks is not None:
        falling_blocks.draw(screen, win_w, win_h)
    particles.draw(screen, win_w, win_h, anim_state.time)


def draw_top_bar(screen, win_w, top_bar_h, font_sub, anim_state, breadcrumb, profile=None):
    bar_alpha = int(ease_out_cubic(min(anim_state.time * 2, 1.0)) * 255)
    bar_surf = get_overlay(win_w, top_bar_h, (15, 15, 25), min(bar_alpha, 230))
    screen.blit(bar_surf, (0, 0))
    pygame.draw.line(screen, (40, 45, 65), (0, top_bar_h), (win_w, top_bar_h))

    bc_text = get_text(breadcrumb, (160, 165, 180), font_sub)
    screen.blit(bc_text, (20, top_bar_h // 2 - bc_text.get_height() // 2))

    sep_x = 20 + bc_text.get_width() + 10
    dots = get_text(">", (80, 85, 100), font_sub)
    screen.blit(dots, (sep_x, top_bar_h // 2 - dots.get_height() // 2))

    global _profile_badge_rect
    _profile_badge_rect = None
    if profile:
        username = profile.get("username") or "Invitado"
        color = profile.get("color", (110, 150, 220))
        is_admin = profile.get("is_admin", False)
        is_playtester = profile.get("is_playtester", False)
        active = profile.get("active", False)

        name_surf = get_text(username, (225, 228, 240), font_sub)
        avatar_d = max(top_bar_h - 12, 18)
        pad_in = 10
        gap = 8
        tag_w = 0
        tag_surf = None
        if is_playtester:
            tag_surf = get_text("PLAYTESTER ♥", (195, 140, 255), font_sub)
            tag_w = tag_surf.get_width() + 10
        badge_w = pad_in + avatar_d + gap + name_surf.get_width() + tag_w + pad_in
        badge_h = min(top_bar_h - 8, avatar_d + 8)
        badge_x = win_w - badge_w - 14
        badge_y = (top_bar_h - badge_h) // 2

        mx, my = pygame.mouse.get_pos()
        hovered = badge_x <= mx <= badge_x + badge_w and badge_y <= my <= badge_y + badge_h

        bg_col = (34, 38, 54) if not (hovered or active) else (46, 52, 74)
        badge_key = ("badge", badge_w, badge_h, bg_col)
        cached_badge = _top_bar_cache.get(badge_key)
        if cached_badge is None:
            cached_badge = pygame.Surface((badge_w, badge_h), pygame.SRCALPHA)
            pygame.draw.rect(cached_badge, (*bg_col, 210), (0, 0, badge_w, badge_h), border_radius=badge_h // 2)
            if len(_top_bar_cache) < 50:
                _top_bar_cache[badge_key] = cached_badge
        screen.blit(cached_badge, (badge_x, badge_y))
        border_col = (170, 100, 255) if is_playtester else (color if (hovered or active) else (60, 66, 88))
        pygame.draw.rect(screen, border_col, (badge_x, badge_y, badge_w, badge_h), 2, border_radius=badge_h // 2)

        av_cx = badge_x + pad_in + avatar_d // 2
        av_cy = badge_y + badge_h // 2
        photo = get_avatar_circle_data(profile.get("avatar_data"), avatar_d) or get_avatar_circle(profile.get("avatar_path"), avatar_d)
        if photo is not None:
            screen.blit(photo, (av_cx - avatar_d // 2, av_cy - avatar_d // 2))
        else:
            pygame.draw.circle(screen, color, (av_cx, av_cy), avatar_d // 2)
            initial = get_text((username[:1] or "?").upper(), (20, 20, 25), font_sub)
            screen.blit(initial, (av_cx - initial.get_width() // 2, av_cy - initial.get_height() // 2))

        screen.blit(name_surf, (badge_x + pad_in + avatar_d + gap, av_cy - name_surf.get_height() // 2))

        if tag_surf is not None:
            tag_x = badge_x + pad_in + avatar_d + gap + name_surf.get_width() + 8
            tag_bg_w = tag_surf.get_width() + 10
            tag_bg_h = tag_surf.get_height() + 4
            tag_key = ("tag", tag_bg_w, tag_bg_h)
            cached_tag = _top_bar_cache.get(tag_key)
            if cached_tag is None:
                cached_tag = pygame.Surface((tag_bg_w, tag_bg_h), pygame.SRCALPHA)
                pygame.draw.rect(cached_tag, (170, 100, 255, 70), (0, 0, tag_bg_w, tag_bg_h), border_radius=tag_bg_h // 2)
                if len(_top_bar_cache) < 50:
                    _top_bar_cache[tag_key] = cached_tag
            screen.blit(cached_tag, (tag_x, av_cy - tag_bg_h // 2))
            screen.blit(tag_surf, (tag_x + 5, av_cy - tag_surf.get_height() // 2))

        if is_admin:
            star = get_text("*", (255, 200, 60), font_sub)
            screen.blit(star, (badge_x + badge_w - pad_in - star.get_width(), av_cy - star.get_height() // 2))

        _profile_badge_rect = pygame.Rect(badge_x, badge_y, badge_w, badge_h)


def draw_title(screen, win_w, anim_state, font_title, title_text, top_bar_h, win_h):
    logo_x = int(win_w * 0.05)
    logo_y = top_bar_h + int(win_h * 0.04)
    title_t = min(anim_state.time * 1.2, 1.0)
    title_y = logo_y + int((1 - ease_out_back(title_t)) * 50)
    title_alpha = int(ease_out_cubic(title_t) * 255)

    if title_text == "TEPY":
        _, logo_h = draw_tepy_logo(screen, logo_x, title_y, font_title, anim_state.time, title_alpha, align="left")
        return logo_x, title_y, logo_h

    logo_surf = font_title.render(title_text, True, WHITE)
    logo_surf.set_alpha(title_alpha)
    screen.blit(logo_surf, (logo_x, title_y))

    return logo_x, title_y, logo_surf.get_height()


def draw_submenu(screen, win_w, win_h, selected, font_title, font_sub, anim_state, particles, breadcrumb, title_text, items, palette=None, falling_blocks=None, profile=None, scroll=0):
    draw_menu_bg(screen, win_w, win_h, anim_state, particles, falling_blocks)
    top_bar_h = max(int(win_h * 0.06), 28)
    draw_top_bar(screen, win_w, top_bar_h, font_sub, anim_state, breadcrumb, profile)

    _, title_y, title_h = draw_title(screen, win_w, anim_state, font_title, title_text, top_bar_h, win_h)

    btn_w = int(win_w * 0.52)
    btn_h = max(int(win_h * 0.1), 44)
    start_x = int(win_w * 0.05)
    start_y = title_y + title_h + int(win_h * 0.04)
    gap = max(int(win_h * 0.015), 6)
    footer_h = max(int(win_h * 0.045), 22)

    if palette is None:
        palette = PALETTE_MENU

    # Area visible de la lista: entre el titulo y el footer. Si el
    # contenido no entra completo, se recorta el dibujo a esta zona y se
    # deja scrollear (rueda del mouse, arrastrar, flechas o barra lateral).
    list_top = start_y
    list_bottom = win_h - footer_h - 6
    content_h = len(items) * (btn_h + gap) - gap if items else 0
    visible_h = max(list_bottom - list_top, 0)
    max_scroll = max(0, content_h - visible_h)
    scroll = max(0, min(scroll, max_scroll))
    needs_scroll = max_scroll > 0

    prev_clip = screen.get_clip()
    if needs_scroll:
        screen.set_clip(pygame.Rect(0, list_top, win_w, visible_h))

    mx, my = pygame.mouse.get_pos()
    hover_idx = -1
    for i in range(len(items)):
        by = start_y + i * (btn_h + gap) - scroll
        if by + btn_h < list_top or by > list_bottom:
            continue
        if start_x <= mx <= start_x + btn_w and by <= my <= by + btn_h:
            hover_idx = i

    for i, (icon_key, title, sub) in enumerate(items):
        by = start_y + i * (btn_h + gap) - scroll
        if by + btn_h < list_top - 4 or by > list_bottom + 4:
            continue
        color, dark = palette[i % len(palette)]
        btn_delay = i * 0.08
        btn_t = max(0, min((anim_state.time - btn_delay) * 3.0, 1.0))
        draw_button(screen, start_x, by, btn_w, btn_h, color, dark, i == selected, icon_key, title, sub, font_title, font_sub, btn_t, anim_state.time, i == hover_idx)

    if needs_scroll:
        screen.set_clip(prev_clip)
        # Barra de scroll simple a la derecha de la lista.
        bar_x = start_x + btn_w + 14
        bar_track_h = visible_h
        pygame.draw.rect(screen, (40, 42, 55), (bar_x, list_top, 6, bar_track_h), border_radius=3)
        thumb_h = max(int(bar_track_h * visible_h / content_h), 24)
        thumb_y = list_top + int((bar_track_h - thumb_h) * (scroll / max_scroll if max_scroll else 0))
        pygame.draw.rect(screen, (120, 140, 180), (bar_x, thumb_y, 6, thumb_h), border_radius=3)

    footer_h = max(int(win_h * 0.045), 22)
    pygame.draw.rect(screen, (12, 12, 18), (0, win_h - footer_h, win_w, footer_h))
    pygame.draw.line(screen, (35, 40, 55), (0, win_h - footer_h), (win_w, win_h - footer_h))
    hint_txt = "↑↓ Seleccionar   ENTER Confirmar   ESC Volver   ←→ Cambiar valor"
    if needs_scroll:
        hint_txt += "   Rueda del mouse: deslizar"
    hint = get_text(hint_txt, (90, 95, 110), font_sub)
    screen.blit(hint, (20, win_h - footer_h // 2 - hint.get_height() // 2))


def draw_menu_with_chat(screen, win_w, win_h, selected, font_title, font_sub, anim_state, particles, items, chat_obj, net_client, falling_blocks=None, palette=None, profile=None):
    """Dibuja el menu principal con el chat global visible a la derecha."""
    draw_menu_bg(screen, win_w, win_h, anim_state, particles, falling_blocks)
    top_bar_h = max(int(win_h * 0.06), 28)
    draw_top_bar(screen, win_w, top_bar_h, font_sub, anim_state, "HOME", profile)

    _, title_y, title_h = draw_title(screen, win_w, anim_state, font_title, "TEPY", top_bar_h, win_h)

    menu_area_w = int(win_w * 0.48)
    chat_area_x = menu_area_w + int(win_w * 0.02)
    chat_area_w = win_w - chat_area_x - int(win_w * 0.02)

    btn_w = int(menu_area_w * 0.88)
    btn_h = max(int(win_h * 0.1), 44)
    start_x = int(win_w * 0.05)
    start_y = title_y + title_h + int(win_h * 0.04)
    gap = max(int(win_h * 0.015), 6)

    if palette is None:
        palette = PALETTE_MENU

    mx, my = pygame.mouse.get_pos()
    hover_idx = -1
    for i in range(len(items)):
        by = start_y + i * (btn_h + gap)
        if start_x <= mx <= start_x + btn_w and by <= my <= by + btn_h:
            hover_idx = i

    for i, (icon_key, title, sub) in enumerate(items):
        by = start_y + i * (btn_h + gap)
        color, dark = palette[i % len(palette)]
        btn_delay = i * 0.08
        btn_t = max(0, min((anim_state.time - btn_delay) * 3.0, 1.0))
        draw_button(screen, start_x, by, btn_w, btn_h, color, dark, i == selected, icon_key, title, sub, font_title, font_sub, btn_t, anim_state.time, i == hover_idx)

    chat_panel_top = title_y + title_h + int(win_h * 0.03)
    chat_footer_h = max(int(win_h * 0.045), 22)
    chat_input_h = font_sub.get_height() + 16
    chat_panel_bottom = win_h - chat_footer_h - chat_input_h - 12
    chat_panel_h = max(chat_panel_bottom - chat_panel_top, 40)

    chat_panel_surf = get_gradient(chat_area_w, chat_panel_h, (16, 18, 28), (22, 25, 36), 215)
    screen.blit(chat_panel_surf, (chat_area_x, chat_panel_top))
    border_col = (90, 160, 220) if chat_obj.input_active else (55, 60, 78)
    pygame.draw.rect(screen, border_col, (chat_area_x, chat_panel_top, chat_area_w, chat_panel_h), 1, border_radius=10)

    chat_label_font = get_font(max(int(win_h * 0.022), 11), True)
    chat_label = chat_label_font.render("CHAT GLOBAL", True, (100, 180, 220))
    screen.blit(chat_label, (chat_area_x + 12, chat_panel_top + 6))

    if not net_client or not net_client.connected:
        gc_offline = font_sub.render("Sin conexion", True, (200, 120, 120))
        screen.blit(gc_offline, (chat_area_x + chat_area_w // 2 - gc_offline.get_width() // 2, chat_panel_top + chat_panel_h // 2))
    elif not chat_obj.messages:
        gc_empty = font_sub.render("Escribe para saludar!", True, (140, 150, 170))
        screen.blit(gc_empty, (chat_area_x + chat_area_w // 2 - gc_empty.get_width() // 2, chat_panel_top + chat_panel_h // 2))
    else:
        gc_pad = 10
        gc_line_h = font_sub.get_height() + 4
        gc_max_lines = max(1, (chat_panel_h - gc_pad * 2 - 20) // gc_line_h)
        gc_shown = chat_obj.messages[-gc_max_lines:]
        gc_cy = chat_panel_top + chat_panel_h - gc_pad - gc_line_h
        for name, message, t, mine in reversed(gc_shown):
            color = (130, 200, 255) if mine else (215, 220, 230)
            label = "Tu" if mine else name
            line = f"{label}: {message}"
            if len(line) > 45:
                line = line[:42] + "..."
            gc_surf = font_sub.render(line, True, color)
            screen.blit(gc_surf, (chat_area_x + gc_pad, gc_cy))
            gc_cy -= gc_line_h

    gc_in_y = chat_panel_bottom + 6
    pygame.draw.rect(screen, (20, 23, 34), (chat_area_x, gc_in_y, chat_area_w, chat_input_h), border_radius=6)
    gc_border = (90, 160, 220) if chat_obj.input_active else (50, 55, 70)
    pygame.draw.rect(screen, gc_border, (chat_area_x, gc_in_y, chat_area_w, chat_input_h), 1, border_radius=6)
    if chat_obj.input_active:
        gc_txt = chat_obj.input + ("|" if chat_obj.cursor_visible else "")
    else:
        gc_txt = "T para chatear"
    gc_color = WHITE if chat_obj.input_active else (110, 120, 140)
    gc_in_surf = font_sub.render(gc_txt if gc_txt else " ", True, gc_color)
    screen.blit(gc_in_surf, (chat_area_x + 10, gc_in_y + chat_input_h // 2 - gc_in_surf.get_height() // 2))


def draw_controls(screen, win_w, win_h, selected, settings, waiting, font_title, font_sub, anim_state, particles, falling_blocks=None):
    draw_menu_bg(screen, win_w, win_h, anim_state, particles, falling_blocks)
    top_bar_h = max(int(win_h * 0.06), 28)
    draw_top_bar(screen, win_w, top_bar_h, font_sub, anim_state, "HOME > OPCIONES > CONTROLES")

    _, title_y, title_h = draw_title(screen, win_w, anim_state, font_title, "CONTROLES", top_bar_h, win_h)

    start_x = int(win_w * 0.05)
    start_y = title_y + title_h + int(win_h * 0.03)
    row_h = max(int(win_h * 0.055), 28)
    gap = max(int(win_h * 0.006), 3)
    row_w = int(win_w * 0.6)

    keys = settings["keys"]
    ctrl_icon_patterns = [
        [[1, 0, 0, 0], [1, 1, 0, 0], [1, 0, 0, 0], [0, 0, 0, 0]],
        [[0, 0, 1, 0], [0, 1, 1, 0], [0, 0, 1, 0], [0, 0, 0, 0]],
        [[0, 0, 0, 0], [0, 1, 1, 0], [0, 1, 1, 0], [0, 0, 0, 0]],
        [[1, 1, 1, 1], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
        [[0, 0, 1, 0], [0, 1, 0, 0], [1, 0, 0, 0], [0, 0, 0, 0]],
        [[0, 1, 0, 0], [1, 0, 0, 0], [0, 0, 1, 0], [0, 0, 0, 0]],
        [[1, 1, 0, 0], [1, 0, 0, 0], [1, 1, 0, 0], [0, 0, 0, 0]],
        [[0, 1, 1, 0], [0, 0, 1, 0], [0, 1, 1, 0], [0, 0, 0, 0]],
    ]

    mx, my = pygame.mouse.get_pos()

    for i, (action, label) in enumerate(zip(CTRL_ACTIONS, CTRL_LABELS)):
        by = start_y + i * (row_h + gap)
        key_val = keys.get(action, pygame.K_UNKNOWN)
        key_name = pygame.key.name(key_val).upper()
        is_sel = i == selected

        if waiting and is_sel:
            pulse = (math.sin(anim_state.time * 8) + 1) / 2
            bg = tuple(int(c * (0.6 + pulse * 0.4)) for c in (180, 140, 40))
            key_name = "..."
        elif is_sel:
            bg = (60, 110, 150)
        else:
            is_hover = start_x <= mx <= start_x + row_w and by <= my <= by + row_h
            bg = (45, 65, 85) if is_hover else (30, 40, 55)

        btn_delay = i * 0.05
        btn_t = max(0, min((anim_state.time - btn_delay) * 3.0, 1.0))
        slide_x = int((1 - ease_out_back(min(btn_t, 1.0))) * 200)
        draw_x = start_x - slide_x

        pygame.draw.rect(screen, bg, (draw_x, by, row_w, row_h), border_radius=4)
        if is_sel:
            pygame.draw.rect(screen, (255, 255, 255), (draw_x, by, 4, row_h), border_radius=2)
            glow_surf = pygame.Surface((row_w, row_h), pygame.SRCALPHA)
            pygame.draw.rect(glow_surf, (60, 140, 200, 30), (0, 0, row_w, row_h), border_radius=4)
            screen.blit(glow_surf, (draw_x, by - 1))

        icon_size = int(row_h * 0.45)
        icon_x = draw_x + int(row_h * 0.25)
        icon_y = by + (row_h - icon_size) // 2
        ic = (255, 255, 255) if is_sel else (130, 140, 160)
        draw_block_icon(screen, icon_x, icon_y, icon_size, ctrl_icon_patterns[i], ic)

        text_x = icon_x + icon_size + int(row_h * 0.25)
        label_surf = font_sub.render(label, True, WHITE if is_sel else (180, 185, 195))
        screen.blit(label_surf, (text_x, by + row_h // 2 - label_surf.get_height() // 2))

        key_box_w = max(int(row_w * 0.2), 70)
        key_box_x = draw_x + row_w - key_box_w - 10
        key_box = pygame.Rect(key_box_x, by + 3, key_box_w, row_h - 6)
        pygame.draw.rect(screen, (15, 18, 25), key_box, border_radius=3)
        border_c = (200, 180, 60) if (waiting and is_sel) else (70, 75, 90)
        pygame.draw.rect(screen, border_c, key_box, 2, border_radius=3)
        key_color = (255, 230, 80) if (waiting and is_sel) else (220, 225, 235)
        key_surf = font_sub.render(key_name, True, key_color)
        screen.blit(key_surf, (key_box_x + key_box_w // 2 - key_surf.get_width() // 2, by + row_h // 2 - key_surf.get_height() // 2))

    back_y = start_y + len(CTRL_ACTIONS) * (row_h + gap)
    back_sel = selected == len(CTRL_ACTIONS)
    back_hover = start_x <= mx <= start_x + row_w and back_y <= my <= back_y + row_h
    back_bg = (90, 90, 110) if back_sel else (50, 50, 65) if back_hover else (35, 35, 48)
    pygame.draw.rect(screen, back_bg, (start_x, back_y, row_w, row_h), border_radius=4)
    if back_sel:
        pygame.draw.rect(screen, (255, 255, 255), (start_x, back_y, 4, row_h), border_radius=2)
    back_text = font_sub.render("VOLVER", True, WHITE)
    screen.blit(back_text, (start_x + 15, back_y + row_h // 2 - back_text.get_height() // 2))

    footer_h = max(int(win_h * 0.045), 22)
    pygame.draw.rect(screen, (12, 12, 18), (0, win_h - footer_h, win_w, footer_h))
    pygame.draw.line(screen, (35, 40, 55), (0, win_h - footer_h), (win_w, win_h - footer_h))
    hint_text = "Presiona ENTER y luego una tecla" if waiting else "↑↓ Navegar   ENTER Reasignar   ESC Volver"
    hint = font_sub.render(hint_text, True, (90, 95, 110))
    screen.blit(hint, (20, win_h - footer_h // 2 - hint.get_height() // 2))


def draw_color_custom(screen, win_w, win_h, selected_piece, piece_colors, font_title, font_sub, anim_state, particles, falling_blocks=None):
    draw_menu_bg(screen, win_w, win_h, anim_state, particles, falling_blocks)
    top_bar_h = max(int(win_h * 0.06), 28)
    draw_top_bar(screen, win_w, top_bar_h, font_sub, anim_state, "HOME > OPCIONES > PERSONALIZACION > COLORES")

    _, title_y, title_h = draw_title(screen, win_w, anim_state, font_title, "COLORES", top_bar_h, win_h)

    start_x = int(win_w * 0.05)
    start_y = title_y + title_h + int(win_h * 0.03)
    row_h = max(int(win_h * 0.065), 32)
    gap = max(int(win_h * 0.007), 3)
    row_w = int(win_w * 0.88)

    shapes_preview = [
        [[1, 1, 1, 1]],
        [[1, 1], [1, 1]],
        [[0, 1, 0], [1, 1, 1]],
        [[0, 1, 1], [1, 1, 0]],
        [[1, 1, 0], [0, 1, 1]],
        [[1, 0, 0], [1, 1, 1]],
        [[0, 0, 1], [1, 1, 1]],
    ]

    mx, my = pygame.mouse.get_pos()

    for i in range(7):
        by = start_y + i * (row_h + gap)
        is_sel = i == selected_piece
        color = piece_colors[i]
        dark_color = tuple(max(c - 50, 0) for c in color)

        is_hover = start_x <= mx <= start_x + row_w and by <= my <= by + row_h
        bg = color if is_sel else dark_color

        btn_delay = i * 0.06
        btn_t = max(0, min((anim_state.time - btn_delay) * 3.0, 1.0))
        slide_x = int((1 - ease_out_back(min(btn_t, 1.0))) * 300)
        draw_x = start_x - slide_x

        pygame.draw.rect(screen, bg, (draw_x, by, row_w, row_h), border_radius=5)
        if is_sel:
            glow_surf = pygame.Surface((row_w, row_h), pygame.SRCALPHA)
            pygame.draw.rect(glow_surf, (255, 255, 255, 40), (0, 0, row_w, row_h), border_radius=5)
            screen.blit(glow_surf, (draw_x, by - 2))
            pygame.draw.rect(screen, (255, 255, 255), (draw_x, by, 5, row_h), border_radius=3)

        preview_size = int(row_h * 0.55)
        px = draw_x + int(row_h * 0.25)
        py = by + (row_h - preview_size) // 2
        cell_s = max(preview_size // 4, 2)
        for sy, srow in enumerate(shapes_preview[i]):
            for sx, sc in enumerate(srow):
                if sc:
                    r = pygame.Rect(px + sx * cell_s, py + sy * cell_s, cell_s - 2, cell_s - 2)
                    lighter = tuple(min(c + 40, 255) for c in color)
                    darker_c = tuple(max(c - 40, 0) for c in color)
                    pygame.draw.rect(screen, color, r, border_radius=2)
                    pygame.draw.line(screen, lighter, r.topleft, (r.right - 1, r.top))
                    pygame.draw.line(screen, lighter, r.topleft, (r.left, r.bottom - 1))
                    pygame.draw.line(screen, darker_c, (r.right - 1, r.top), r.bottomright)
                    pygame.draw.line(screen, darker_c, (r.left, r.bottom - 1), (r.right - 1, r.bottom - 1))

        text_x = px + preview_size + int(row_h * 0.3)
        name_surf = font_title.render(PIECE_NAMES[i], True, WHITE)
        screen.blit(name_surf, (text_x, by + row_h // 2 - name_surf.get_height() // 2))

        palette_x = text_x + name_surf.get_width() + int(row_h * 0.25)
        dot_r = max(int(row_h * 0.04), 2)
        current_palette_idx = -1
        for ci, pc in enumerate(COLOR_PALETTE):
            if pc == piece_colors[i]:
                current_palette_idx = ci
                break
        cols = 10
        for ci, pc in enumerate(COLOR_PALETTE):
            dx = palette_x + (ci % cols) * (dot_r * 2 + 4)
            dy = by + row_h // 2 - (cols // 2) * (dot_r * 2 + 4) // 2 + (ci // cols) * (dot_r * 2 + 4)
            pygame.draw.circle(screen, pc, (dx + dot_r, dy + dot_r), dot_r)
            if ci == current_palette_idx:
                pygame.draw.circle(screen, WHITE, (dx + dot_r, dy + dot_r), dot_r + 3, 2)

        if is_sel:
            arrow_y = by + row_h // 2
            lx = draw_x + row_w - int(row_h * 0.5)
            pygame.draw.polygon(screen, (255, 255, 255), [(lx, arrow_y - 6), (lx + 10, arrow_y), (lx, arrow_y + 6)])
            rx = draw_x + row_w - int(row_h * 0.15)
            pygame.draw.polygon(screen, (255, 255, 255), [(rx, arrow_y - 6), (rx - 10, arrow_y), (rx, arrow_y + 6)])

    back_y = start_y + 7 * (row_h + gap)
    back_sel = selected_piece == 7
    back_hover = start_x <= mx <= start_x + row_w and back_y <= my <= back_y + row_h
    back_bg = (90, 90, 110) if back_sel else (50, 50, 65) if back_hover else (35, 35, 48)
    pygame.draw.rect(screen, back_bg, (start_x, back_y, row_w, row_h), border_radius=5)
    if back_sel:
        pygame.draw.rect(screen, (255, 255, 255), (start_x, back_y, 5, row_h), border_radius=3)
    back_text = font_sub.render("VOLVER", True, WHITE)
    screen.blit(back_text, (start_x + 15, back_y + row_h // 2 - back_text.get_height() // 2))

    footer_h = max(int(win_h * 0.045), 22)
    pygame.draw.rect(screen, (12, 12, 18), (0, win_h - footer_h, win_w, footer_h))
    pygame.draw.line(screen, (35, 40, 55), (0, win_h - footer_h), (win_w, win_h - footer_h))
    hint = font_sub.render("← → Cambiar color   ↑ ↓ Seleccionar pieza   ESC Volver", True, (90, 95, 110))
    screen.blit(hint, (20, win_h - footer_h // 2 - hint.get_height() // 2))


async def main():
    screen = pygame.display.set_mode((800, 600), pygame.RESIZABLE)
    pygame.display.set_caption("Tepy")
    clock = pygame.time.Clock()

    S_MENU = 0
    S_PLAY_MENU = 1
    S_OPTIONS = 2
    S_CONTROLS = 3
    S_GAME = 4
    S_PERSONAL = 5
    S_SOUND = 6
    S_PLAYING = 7
    S_COLOR_CUSTOM = 8
    S_MULTI_MENU = 9
    S_MULTI_HOST = 10
    S_MULTI_JOIN = 11
    S_MULTI_WAIT = 12
    S_MULTI_PLAY = 13
    S_LOGIN = 14
    S_REGISTER = 15
    S_LOADING = 16
    S_MULTI_CONNECTING = 17
    S_MUSIC_MENU = 18
    S_MUSIC_DIFF = 19
    S_MUSIC_PLAYING = 20
    S_MUSIC_YT = 21
    S_NEWS = 22
    S_NEWS_EDIT = 23
    S_VS_PLAY = 24
    S_PROFILE = 25
    S_BIO_EDIT = 26
    state = S_LOGIN

    selected = 0
    waiting_for_key = False
    color_selected_piece = 0
    paused = False
    pause_selected = 0
    pause_btn_rects = None
    show_multi_warning = False
    multi_warning_btn = None
    show_love_note = False
    love_note_btn = None
    bio_edit_text = ""
    multi_warning_time = 0.0

    login_user = ""
    login_pass = ""
    login_field = 0
    login_error = ""
    login_is_register = False

    loading_progress = 0.0
    loading_display = 0.0
    show_fps = False
    fps_smooth = 60.0
    ev_ms = 0.0
    upd_ms = 0.0
    draw_ms = 0.0
    flip_ms = 0.0
    loading_stage = 0
    loading_start_time = 0
    loading_done = False
    loaded_username = ""
    is_admin = False

    # Sesion recordada de un arranque anterior: si hay un usuario guardado
    # y la cuenta todavia existe, se entra directo (mismo camino que un
    # login exitoso) en vez de mostrar la pantalla de inicio de sesion.
    _remembered_user = load_session()
    if _remembered_user and _remembered_user in load_accounts():
        loaded_username = _remembered_user
        is_admin = bool(load_accounts().get(_remembered_user, {}).get("admin"))
        state = S_LOADING
        loading_start_time = _time.time()
    else:
        if _remembered_user:
            save_session("")  # cuenta borrada/invalida: limpiar sesion vieja

    news_list = []
    news_status = ""
    news_selected = 0
    news_scroll = 0
    news_scroll_target = 0
    news_prev_selected = -1
    news_edit_title = ""
    news_edit_body = ""
    news_edit_field = 0
    news_edit_image = ""       # base64 (sin prefijo data:) de la imagen elegida, o ""
    news_edit_image_name = ""  # solo para mostrar el nombre del archivo elegido
    news_delete_pending = {}

    # Scroll generico para los submenus tipo lista (Perfil, Opciones, etc.)
    # cuando hay mas items de los que caben en la pantalla.
    menu_scroll = 0.0
    menu_scroll_target = 0.0
    menu_scroll_state = state

    settings = {
        "das": 167, "arr": 50, "lock": 500, "ghost": True,
        "start_level": 1, "next_count": 5, "bg_style": 0,
        "custom_bg_path": "",
        "volume": 60, "music": True, "sfx": True,
        "player_name": "",
        "avatar_color_idx": 0,
        "avatar_path": "",
        "avatar_data": "",
        "banner_color_idx": 0,
        "profile_title_idx": 0,
        "best_score": 0,
        "bio": "",
        "piece_colors": list(COLORS),
        "menu_falling_blocks": True,
        "low_end": LOW_END,
        "keys": {
            "left": pygame.K_LEFT, "right": pygame.K_RIGHT,
            "soft_drop": pygame.K_DOWN, "hard_drop": pygame.K_SPACE,
            "rotate": pygame.K_UP, "rotate_ccw": pygame.K_z,
            "hold": pygame.K_c, "restart": pygame.K_r,
        }
    }

    game = Tetris(settings)
    combo = ComboSystem()
    effects = ScreenEffects()
    game.combo = combo
    game.effects = effects
    input_handler = InputHandler(settings["keys"])
    gamepad = GamepadHandler()
    gamepad.connect()
    prev_game_dpad_y = 0
    gp_das_dir = 0
    gp_das_timer = 0.0
    gp_arr_timer = 0.0
    anim = AnimationState()
    particles_bg = Particles()
    falling_blocks_bg = FallingBlocks()
    if settings.get("low_end"):
        particles_bg.particles.clear()
        falling_blocks_bg.blocks.clear()
    fade = FadeTransition()

    net_client = None
    if HAS_NETWORK:
        net_client = NetworkClient()

    menu_items = [
        ("play", "JUGAR", "Elige tu modo de juego"),
        ("news", "NOTICIAS", "Enterate de lo nuevo"),
        ("options", "OPCIONES", "Configura el juego"),
        ("exit", "SALIR", "Cierra el juego"),
    ]
    play_items = [
        ("play", "MODO CLASICO", "Juega tetris clasico"),
        ("endless", "MODO SIN FIN", "Sin limite de tiempo"),
        ("music", "MODO MUSICA", "Juega con musica y video"),
        ("multi", "MULTIJUGADOR", "Juega con amigos online"),
        ("vs", "VS", "Compite contra la CPU"),
        ("back", "VOLVER", "Volver al menu principal"),
    ]
    multi_menu_items = [
        ("host", "CREAR SALA", "Crea una sala y espera a un amigo"),
        ("join", "UNIRSE A SALA", "Entra a la sala de un amigo"),
        ("back", "VOLVER", "Volver al menu"),
    ]
    multi_host_items = [
        ("ready", "LISTO", "Espera al rival..."),
        ("leave", "SALIR", "Volver al menu"),
    ]
    multi_join_items = [
        ("join", "CONECTAR", "Unirse a la sala"),
        ("back", "VOLVER", "Volver al menu"),
    ]
    multi_wait_items = [
        ("ready", "LISTO", "Espera al rival..."),
        ("leave", "SALIR", "Volver al menu"),
    ]
    multi_input_text = ""
    multi_input_active = False
    multi_status_msg = ""
    multi_ip = "localhost"
    multi_port = "5555"
    if IS_WEB:
        # En la web el server no se puede spawnear localmente: se conecta
        # por WebSocket al host que sirve la pagina, puerto WS del server.
        try:
            import js
            multi_ip = str(js.location.hostname) or "localhost"
        except Exception:
            pass
        multi_port = "5556"
    multi_input_field = 0
    multi_opponent = {"name": "", "grid": None, "score": 0, "lines": 0, "level": 1, "current_piece": None, "next_queue": [], "combo": 0, "game_over": False}
    # Rivales de una partida online real (hasta 7, salas de hasta 8
    # jugadores), indexados por su player_id de red. multi_opponent (arriba)
    # se sigue usando tal cual para el modo VS contra la CPU (1 solo rival).
    multi_opponents = {}
    room_chat = Chat()
    global_chat = Chat(max_lines=40, max_chars=150)
    bot_game = None
    bot = None
    vs_state = {"player_lines": 0, "bot_lines": 0, "ended": False}
    mp_stats = load_mp_stats()
    # Estado del HUD extendido de multijugador: ping estimado (frescura de
    # la ultima actualizacion del rival), lineas de basura enviadas/
    # recibidas y el popup de "ataque" que aparece un momento tras cada
    # tanda de basura.
    mp_hud = {
        "last_opp_update_t": None,
        "ping_ms": 0,
        "player_lines_sent_ref": 0,
        "sent_total": 0,
        "received_total": 0,
        "popup_text": "",
        "popup_color": (255, 220, 80),
        "popup_timer": 0.0,
    }
    multi_connect_action = ""
    multi_connect_thread = None
    multi_connect_result = None
    server_process = None
    server_started = False
    music_player = MusicPlayer()
    music_file_path = ""
    music_file_name = ""
    music_lrc_path = ""
    music_lrc_name = ""
    music_status_msg = ""
    music_difficulty = 0
    music_won = False
    music_won_time = 0.0
    music_win_btns = None
    music_diff_items = [
        ("easy", "FACIL", "Sobrevive hasta el final de la cancion"),
        ("hard", "DIFICIL", "Haz combos al ritmo de la musica"),
        ("back", "VOLVER", "Volver"),
    ]
    music_yt_url = ""
    music_yt_status = ""
    music_yt_downloading = False
    music_yt_thread = None
    music_yt_progress = 0
    music_menu_items = [
        ("load_music", "CARGAR MUSICA", "Selecciona un archivo MP3 o MP4"),
        ("yt", "DESCARGAR DE YOUTUBE", "Pega un link de YouTube"),
        ("load_lrc", "CARGAR LETRA", "Selecciona un archivo .LRC (opcional)"),
        ("play", "JUGAR", "Inicia el modo musica"),
        ("back", "VOLVER", "Volver al menu"),
    ]
    options_items = [
        ("ctrl", "CONTROLES", "Configura tus teclas"),
        ("game", "JUEGO", "DAS, ARR, Lock, Ghost, Nivel"),
        ("personal", "PERSONALIZACION", "Fondo y colores de piezas"),
        ("sound", "SONIDO", "Volumen y efectos de audio"),
        ("back", "VOLVER", "Volver al menu principal"),
    ]
    game_items = [
        ("das", "DAS", f"{settings['das']}ms - Delay antes de auto-shift"),
        ("arr", "ARR", f"{settings['arr']}ms - Velocidad de auto-shift"),
        ("lock", "LOCK", f"{settings['lock']}ms - Tiempo antes de fijar pieza"),
        ("ghost", "GHOST", f"{'ON' if settings['ghost'] else 'OFF'} - Pieza fantasma"),
        ("queue", "NEXT QUEUE", f"{settings['next_count']} piezas"),
        ("low_end", "MODO LOW-END", f"{'ON' if settings.get('low_end', False) else 'OFF'} - Desactiva video y animaciones"),
        ("back", "VOLVER", "Volver a opciones"),
    ]
    personal_status_msg = ""
    profile_status_msg = ""
    personal_items = [
        ("bg", "FONDO", f"{get_bg_style_name(settings['bg_style'], settings)}"),
        ("custom_bg", "FONDO PROPIO", "Cargar imagen..." if not settings["custom_bg_path"] else os.path.basename(settings["custom_bg_path"])),
        ("menu_blocks", "PIEZAS FLOTANTES", f"{'ON' if settings['menu_falling_blocks'] else 'OFF'} - Piezas cayendo en los menus"),
        ("colors", "COLORES", "Personaliza colores de piezas"),
        ("back", "VOLVER", "Volver a opciones"),
    ]
    sound_items = [
        ("volume", "VOLUMEN", f"{settings['volume']}%"),
        ("music", "MUSICA", f"{'ON' if settings['music'] else 'OFF'}"),
        ("sfx", "EFECTOS", f"{'ON' if settings['sfx'] else 'OFF'}"),
        ("back", "VOLVER", "Volver a opciones"),
    ]
    profile_items = [
        ("avatar_photo", "FOTO DE PERFIL", "Cargar imagen..."),
        ("personal", "PERSONALIZAR", "Fondo y colores de piezas"),
        ("avatar_color", "COLOR DE AVATAR", "Cambia el color de tu insignia"),
        ("banner_color", "COLOR DE FONDO", "Cambia el fondo de tu tarjeta"),
        ("title", "TITULO", "Elige tu titulo"),
        ("bio", "DESCRIPCION", "Sin descripcion..."),
        ("logout", "CERRAR SESION", "Salir y poder iniciar con otra cuenta"),
        ("back", "VOLVER", "Volver al menu principal"),
    ]

    def get_items():
        if state == S_MENU:
            return menu_items
        elif state == S_PLAY_MENU:
            return play_items
        elif state == S_OPTIONS:
            return options_items
        elif state == S_GAME:
            return game_items
        elif state == S_PERSONAL:
            return personal_items
        elif state == S_SOUND:
            return sound_items
        elif state == S_PROFILE:
            return profile_items
        elif state == S_MULTI_MENU:
            return multi_menu_items
        elif state == S_MULTI_JOIN:
            return multi_join_items
        elif state == S_MULTI_WAIT:
            return multi_wait_items
        elif state == S_MUSIC_MENU:
            return music_menu_items
        elif state == S_MUSIC_DIFF:
            return music_diff_items
        elif state == S_MUSIC_YT:
            return [("download", "DESCARGAR", "Descargar audio de YouTube"), ("back", "VOLVER", "Volver")]
        return menu_items

    def update_option_texts():
        game_items[0] = ("das", "DAS", f"{settings['das']}ms - Delay antes de auto-shift")
        game_items[1] = ("arr", "ARR", f"{settings['arr']}ms - Velocidad de auto-shift")
        game_items[2] = ("lock", "LOCK", f"{settings['lock']}ms - Tiempo antes de fijar pieza")
        game_items[3] = ("ghost", "GHOST", f"{'ON' if settings['ghost'] else 'OFF'} - Pieza fantasma")
        game_items[4] = ("queue", "NEXT QUEUE", f"{settings['next_count']} piezas")
        game_items[5] = ("low_end", "MODO LOW-END", f"{'ON' if settings.get('low_end', False) else 'OFF'} - Desactiva video y animaciones")
        personal_items[0] = ("bg", "FONDO", f"{get_bg_style_name(settings['bg_style'], settings)}")
        personal_items[1] = ("custom_bg", "FONDO PROPIO", "Cargar imagen..." if not settings["custom_bg_path"] else os.path.basename(settings["custom_bg_path"]))
        personal_items[2] = ("menu_blocks", "PIEZAS FLOTANTES", f"{'ON' if settings['menu_falling_blocks'] else 'OFF'} - Piezas cayendo en los menus")
        sound_items[0] = ("volume", "VOLUMEN", f"{settings['volume']}%")
        sound_items[1] = ("music", "MUSICA", f"{'ON' if settings['music'] else 'OFF'}")
        sound_items[2] = ("sfx", "EFECTOS", f"{'ON' if settings['sfx'] else 'OFF'}")
        profile_items[0] = ("avatar_photo", "FOTO DE PERFIL", os.path.basename(settings["avatar_path"]) if settings.get("avatar_data") and settings.get("avatar_path") else ("Foto cargada" if settings.get("avatar_data") else "Cargar imagen..."))
        profile_items[2] = ("avatar_color", "COLOR DE AVATAR", f"{settings['avatar_color_idx'] + 1}/{len(settings['piece_colors'])}")
        profile_items[3] = ("banner_color", "COLOR DE FONDO", f"{settings.get('banner_color_idx', 0) + 1}/{len(BANNER_COLORS)}")
        p_wins = get_player_rank(mp_stats, loaded_username).get("wins", 0) if loaded_username else 0
        unlocked_titles = get_unlocked_titles(loaded_username, p_wins)
        if settings.get("profile_title_idx", 0) >= len(unlocked_titles):
            settings["profile_title_idx"] = 0
        title_name = unlocked_titles[settings.get("profile_title_idx", 0)] if unlocked_titles else "Sin titulo"
        total_titles = len(PROFILE_TITLES) + (1 if EXCLUSIVE_TITLES.get((loaded_username or "").strip().lower()) else 0)
        profile_items[4] = ("title", "TITULO", f"{title_name}  ({len(unlocked_titles)}/{total_titles} desbloqueados)")
        profile_items[5] = ("bio", "DESCRIPCION", settings.get("bio", "").strip() or "Sin descripcion...")

    def go_back():
        nonlocal state, selected
        sfx.play("menu_click")
        prev_state = state
        back_map = {
            S_PLAY_MENU: S_MENU,
            S_OPTIONS: S_MENU,
            S_CONTROLS: S_OPTIONS,
            S_GAME: S_OPTIONS,
            S_PERSONAL: S_OPTIONS,
            S_SOUND: S_OPTIONS,
            S_COLOR_CUSTOM: S_PERSONAL,
            S_MULTI_MENU: S_PLAY_MENU,
            S_MULTI_JOIN: S_MULTI_MENU,
            S_MULTI_WAIT: S_MULTI_MENU,
            S_MULTI_PLAY: S_MULTI_MENU,
            S_MULTI_CONNECTING: S_MULTI_MENU,
            S_VS_PLAY: S_PLAY_MENU,
            S_MUSIC_MENU: S_PLAY_MENU,
            S_MUSIC_DIFF: S_MUSIC_MENU,
            S_MUSIC_YT: S_MUSIC_MENU,
            S_NEWS: S_MENU,
            S_NEWS_EDIT: S_NEWS,
            S_PROFILE: S_MENU,
        }
        state = back_map.get(state, S_MENU)
        selected = 0
        anim.reset()
        if prev_state in (S_GAME, S_PERSONAL, S_SOUND, S_COLOR_CUSTOM, S_CONTROLS, S_PROFILE) and loaded_username:
            save_user_settings(loaded_username, settings)

    def select_item(idx):
        global low_end
        nonlocal state, selected, game, input_handler, waiting_for_key, color_selected_piece
        nonlocal bot_game, bot, vs_state, multi_opponent, multi_opponents
        nonlocal server_started, server_process, multi_connect_action
        nonlocal music_file_path, music_file_name, music_lrc_path, music_lrc_name
        nonlocal music_status_msg, music_difficulty, music_won, combo, effects
        nonlocal news_list, news_status, news_selected, news_scroll, news_scroll_target
        nonlocal personal_status_msg
        nonlocal profile_status_msg
        nonlocal loaded_username, is_admin, login_user, login_pass, login_field, login_error, login_is_register
        nonlocal paused, pause_selected
        nonlocal show_multi_warning
        nonlocal bio_edit_text
        nonlocal multi_warning_time
        sfx.play("menu_select")
        items = get_items()
        if idx >= len(items):
            return
        icon_key = items[idx][0]

        if state == S_MENU:
            if icon_key == "play":
                state = S_PLAY_MENU
                selected = 0
                anim.reset()
            elif icon_key == "news":
                if not HAS_NETWORK or net_client is None:
                    multi_status_msg = "Funcion online no disponible"
                    news_list = load_local_news()
                    news_status = "Sin conexion - noticias guardadas"
                    state = S_NEWS
                elif net_client.connected:
                    net_client.get_news()
                    news_status = "Cargando noticias..."
                    state = S_NEWS
                else:
                    if not server_started:
                        server_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")
                        if _server_launch_allowed(server_path):
                            try:
                                server_process = subprocess.Popen(
                                    [sys.executable, server_path],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                                )
                                server_started = True
                                _time.sleep(0.3)
                            except Exception:
                                pass
                    multi_connect_action = "news"
                    state = S_MULTI_CONNECTING
                    if IS_FROZEN and not server_started:
                        multi_status_msg = "Ejecuta server.py por separado antes de jugar online"
                    else:
                        multi_status_msg = "Conectando al servidor..."
                selected = 0
                news_selected = 0
                news_scroll = 0
                news_scroll_target = 0
                anim.reset()
            elif icon_key == "options":
                state = S_OPTIONS
                selected = 0
                anim.reset()
            elif icon_key == "exit":
                if server_process and server_process.poll() is None:
                    try: server_process.terminate()
                    except Exception: pass
                pygame.quit()
                sys.exit()

        elif state == S_PLAY_MENU:
            if icon_key == "back":
                go_back()
            elif icon_key == "multi":
                if not HAS_NETWORK or net_client is None:
                    multi_status_msg = "Modo online no disponible"
                else:
                    if not server_started:
                        server_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")
                        if _server_launch_allowed(server_path):
                            try:
                                server_process = subprocess.Popen(
                                    [sys.executable, server_path],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                                )
                                server_started = True
                                _time.sleep(0.3)
                            except Exception:
                                multi_status_msg = "No se pudo iniciar el servidor"
                    state = S_MULTI_MENU
                    selected = 0
                    multi_status_msg = "" if not IS_FROZEN or server_started else "Ejecuta server.py por separado"
                    show_multi_warning = True
                    anim.reset()
                    multi_warning_time = anim.time
            elif icon_key == "music":
                state = S_MUSIC_MENU
                selected = 0
                music_status_msg = ""
                anim.reset()
            elif icon_key == "vs":
                game = Tetris(settings)
                combo = ComboSystem()
                effects = ScreenEffects()
                game.combo = combo
                game.effects = effects
                input_handler = InputHandler(settings["keys"])
                bot_game = Tetris(settings)
                bot = BotAI(think_delay=600)
                multi_opponent = {
                    "name": "CPU", "grid": bot_game.grid, "score": 0,
                    "lines": 0, "level": 1, "current_piece": None,
                    "next_queue": [], "combo": 0, "game_over": False,
                }
                vs_state = {"player_lines": 0, "bot_lines": 0, "ended": False}
                state = S_VS_PLAY
                selected = 0
                anim.time = 0
            else:
                game = Tetris(settings)
                combo = ComboSystem()
                effects = ScreenEffects()
                game.combo = combo
                game.effects = effects
                input_handler = InputHandler(settings["keys"])
                paused = False
                pause_selected = 0
                state = S_PLAYING

        elif state == S_MUSIC_MENU:
            if icon_key == "back":
                music_player.stop()
                go_back()
            elif icon_key == "load_music":
                if IS_WEB:
                    music_status_msg = "Cargar archivos no disponible en el navegador"
                else:
                    path = open_file_dialog("Selecciona musica", [
                        ("Archivos de audio/video", "*.mp3 *.mp4 *.wav *.ogg *.avi"),
                        ("Todos los archivos", "*.*"),
                    ])
                    if path:
                        music_file_path = path
                        music_file_name = os.path.basename(path)
                        ext = os.path.splitext(path)[1].lower()
                        if ext in (".mp4", ".avi", ".mkv", ".mov"):
                            music_player.load_video(path)
                            # pygame can't play audio straight out of a video
                            # container, so pull the audio track out first.
                            extracted = extract_audio_from_video(path)
                            if extracted:
                                music_player.load_audio(extracted)
                            else:
                                music_player.load_error = (
                                    "No se pudo preparar el audio del video "
                                    "(revisa tu conexion a internet)"
                                )
                        else:
                            music_player.load_audio(path)
                        errors = [e for e in (music_player.load_error, music_player.video_load_error) if e]
                        if errors:
                            music_status_msg = " | ".join(errors)[:120]
                        else:
                            music_status_msg = f"Cargado: {music_file_name}"
                    else:
                        music_status_msg = "No se selecciono archivo"
            elif icon_key == "load_lrc":
                if IS_WEB:
                    music_status_msg = "Cargar archivos no disponible en el navegador"
                else:
                    path = open_file_dialog("Selecciona letra", [
                        ("Archivos LRC", "*.lrc"),
                        ("Todos los archivos", "*.*"),
                    ])
                    if path:
                        music_lrc_path = path
                        music_lrc_name = os.path.basename(path)
                        music_player.load_lrc(path)
                        music_status_msg = f"Letra cargada: {music_lrc_name}"
                    else:
                        music_status_msg = "No se selecciono archivo"
            elif icon_key == "play":
                if not music_file_path:
                    music_status_msg = "Carga un archivo de musica primero"
                else:
                    state = S_MUSIC_DIFF
                    selected = 0
                    anim.reset()
            elif icon_key == "yt":
                music_status_msg = "Descarga de YouTube desactivada temporalmente"

        elif state == S_MUSIC_YT:
            if icon_key == "back":
                go_back()
            elif icon_key == "download":
                if not music_yt_url.strip():
                    music_yt_status = "Escribe un link de YouTube"
                elif music_yt_downloading:
                    music_yt_status = "Ya se esta descargando..."
                else:
                    music_yt_downloading = True
                    music_yt_status = "Descargando..."
                    music_yt_progress = 0

                    dl_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "music_downloads")
                    os.makedirs(dl_dir, exist_ok=True)

                    def _do_download():
                        nonlocal music_yt_downloading, music_yt_status, music_file_path, music_file_name, music_yt_progress
                        try:
                            ffmpeg_exe = get_ffmpeg_exe()
                            ydl_opts = {
                                "format": "bestaudio/best",
                                "outtmpl": os.path.join(dl_dir, "%(title)s.%(ext)s"),
                                "quiet": True,
                                "no_warnings": True,
                                # Convert whatever YouTube gives us (webm/opus, etc.)
                                # into mp3, which pygame.mixer can always play.
                                # Without this, downloads were silent because
                                # pygame can't decode webm/opus.
                                "postprocessors": [{
                                    "key": "FFmpegExtractAudio",
                                    "preferredcodec": "mp3",
                                    "preferredquality": "192",
                                }],
                            }
                            if ffmpeg_exe and ffmpeg_exe != "ffmpeg":
                                ydl_opts["ffmpeg_location"] = ffmpeg_exe
                            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                                info = ydl.extract_info(music_yt_url.strip(), download=True)
                                title = info.get("title", "download")
                                out_path = None
                                # After the mp3 postprocessor runs, the final
                                # file always ends in .mp3.
                                mp3_candidate = os.path.join(dl_dir, title + ".mp3")
                                if os.path.exists(mp3_candidate):
                                    out_path = mp3_candidate
                                else:
                                    for f in os.listdir(dl_dir):
                                        if f.startswith(title):
                                            out_path = os.path.join(dl_dir, f)
                                            break
                                if not out_path:
                                    raise RuntimeError("No se encontro el archivo descargado")
                                music_file_path = out_path
                                music_file_name = os.path.basename(out_path)
                                music_player.load_audio(out_path)
                                if music_player.load_error:
                                    music_yt_status = f"Error al cargar audio: {music_player.load_error[:60]}"
                                else:
                                    music_yt_status = f"Listo: {music_file_name}"
                                music_yt_progress = 100
                        except Exception as e:
                            music_yt_status = f"Error: {str(e)[:80]}"
                        finally:
                            music_yt_downloading = False

                    music_yt_thread = threading.Thread(target=_do_download, daemon=True)
                    music_yt_thread.start()

        elif state == S_MUSIC_DIFF:
            if icon_key == "back":
                go_back()
            elif icon_key == "easy" or icon_key == "hard":
                music_difficulty = 0 if icon_key == "easy" else 1
                music_player.stop()
                game = Tetris(settings)
                combo = ComboSystem()
                effects = ScreenEffects()
                game.combo = combo
                game.effects = effects
                input_handler = InputHandler(settings["keys"])
                music_won = False
                music_win_btns = None
                music_player.play()
                state = S_MUSIC_PLAYING
                anim.reset()

        elif state == S_OPTIONS:
            if icon_key == "back":
                go_back()
            elif icon_key == "ctrl":
                state = S_CONTROLS
                selected = 0
                waiting_for_key = False
                anim.reset()
            elif icon_key == "game":
                state = S_GAME
                selected = 0
                anim.reset()
            elif icon_key == "personal":
                state = S_PERSONAL
                selected = 0
                anim.reset()
            elif icon_key == "sound":
                state = S_SOUND
                selected = 0
                anim.reset()

        elif state == S_GAME:
            if icon_key == "back":
                go_back()
            elif icon_key == "das":
                idx2 = DAS_VALUES.index(settings["das"])
                settings["das"] = DAS_VALUES[(idx2 + 1) % len(DAS_VALUES)]
                update_option_texts()
            elif icon_key == "arr":
                idx2 = ARR_VALUES.index(settings["arr"])
                settings["arr"] = ARR_VALUES[(idx2 + 1) % len(ARR_VALUES)]
                update_option_texts()
            elif icon_key == "lock":
                idx2 = LOCK_VALUES.index(settings["lock"])
                settings["lock"] = LOCK_VALUES[(idx2 + 1) % len(LOCK_VALUES)]
                update_option_texts()
            elif icon_key == "ghost":
                settings["ghost"] = not settings["ghost"]
                update_option_texts()
            elif icon_key == "queue":
                idx2 = NEXT_VALUES.index(settings["next_count"])
                settings["next_count"] = NEXT_VALUES[(idx2 + 1) % len(NEXT_VALUES)]
                update_option_texts()
            elif icon_key == "low_end":
                settings["low_end"] = not settings.get("low_end", False)
                low_end = settings["low_end"]
                update_option_texts()

        elif state == S_PERSONAL:
            if icon_key == "back":
                go_back()
            elif icon_key == "bg":
                settings["bg_style"] = (settings["bg_style"] + 1) % BG_STYLE_COUNT
                update_option_texts()
            elif icon_key == "custom_bg":
                if IS_WEB:
                    personal_status_msg = "Cargar imagenes no disponible en el navegador"
                else:
                    path = open_file_dialog("Selecciona una imagen de fondo", [
                        ("Imagenes", "*.png *.jpg *.jpeg *.bmp *.webp"),
                        ("Todos los archivos", "*.*"),
                    ])
                    if path:
                        img = load_custom_bg_image(path)
                        if img is not None:
                            settings["custom_bg_path"] = path
                            settings["bg_style"] = CUSTOM_BG_INDEX
                            personal_status_msg = f"Fondo cargado: {os.path.basename(path)}"
                        else:
                            personal_status_msg = "No se pudo abrir esa imagen"
                    else:
                        personal_status_msg = "No se selecciono ninguna imagen"
                update_option_texts()
            elif icon_key == "menu_blocks":
                settings["menu_falling_blocks"] = not settings["menu_falling_blocks"]
                update_option_texts()
            elif icon_key == "colors":
                state = S_COLOR_CUSTOM
                color_selected_piece = 0
                anim.reset()

        elif state == S_PROFILE:
            if icon_key == "back":
                go_back()
            elif icon_key == "personal":
                state = S_PERSONAL
                selected = 0
                anim.reset()
            elif icon_key == "avatar_photo":
                if IS_WEB:
                    profile_status_msg = "Cargar imagenes no disponible en el navegador"
                else:
                    path = open_file_dialog("Selecciona tu foto de perfil", [
                        ("Imagenes PNG", "*.png"),
                        ("Imagenes", "*.png *.jpg *.jpeg *.bmp *.webp"),
                        ("Todos los archivos", "*.*"),
                    ])
                    if path:
                        b64, err = encode_avatar_image(path)
                        if b64:
                            # Se guarda embebida (base64) para que no se
                            # pierda si el archivo original se mueve/borra
                            # o si accounts.json se copia a otra maquina.
                            settings["avatar_data"] = b64
                            settings["avatar_path"] = path
                            profile_status_msg = f"Foto cargada: {os.path.basename(path)}"
                        else:
                            profile_status_msg = err or "No se pudo abrir esa imagen"
                    else:
                        profile_status_msg = "No se selecciono ninguna imagen"
                update_option_texts()
                if loaded_username:
                    save_user_settings(loaded_username, settings)
            elif icon_key == "avatar_color":
                settings["avatar_color_idx"] = (settings["avatar_color_idx"] + 1) % len(settings["piece_colors"])
                update_option_texts()
                if loaded_username:
                    save_user_settings(loaded_username, settings)
            elif icon_key == "banner_color":
                settings["banner_color_idx"] = (settings.get("banner_color_idx", 0) + 1) % len(BANNER_COLORS)
                update_option_texts()
                if loaded_username:
                    save_user_settings(loaded_username, settings)
            elif icon_key == "title":
                p_wins = get_player_rank(mp_stats, loaded_username).get("wins", 0) if loaded_username else 0
                unlocked_titles = get_unlocked_titles(loaded_username, p_wins)
                if unlocked_titles:
                    settings["profile_title_idx"] = (settings.get("profile_title_idx", 0) + 1) % len(unlocked_titles)
                update_option_texts()
                if loaded_username:
                    save_user_settings(loaded_username, settings)
            elif icon_key == "bio":
                bio_edit_text = settings.get("bio", "")
                state = S_BIO_EDIT
            elif icon_key == "logout":
                if loaded_username:
                    save_user_settings(loaded_username, settings)
                save_session("")
                loaded_username = ""
                is_admin = False
                login_user = ""
                login_pass = ""
                login_field = 0
                login_error = ""
                login_is_register = False
                state = S_LOGIN
                selected = 0
                anim.reset()

        elif state == S_SOUND:
            if icon_key == "back":
                go_back()
            elif icon_key == "volume":
                settings["volume"] = (settings["volume"] + 10) % 110
                update_option_texts()
                if loaded_username:
                    save_user_settings(loaded_username, settings)
            elif icon_key == "music":
                settings["music"] = not settings["music"]
                update_option_texts()
                if loaded_username:
                    save_user_settings(loaded_username, settings)
            elif icon_key == "sfx":
                settings["sfx"] = not settings["sfx"]
                update_option_texts()
                if loaded_username:
                    save_user_settings(loaded_username, settings)

        elif state == S_MULTI_MENU:
            if icon_key == "back":
                go_back()
            elif icon_key == "host":
                if not HAS_NETWORK or net_client is None:
                    multi_status_msg = "Modo online no disponible"
                else:
                    if net_client.connected:
                        net_client.disconnect()
                    state = S_MULTI_CONNECTING
                    multi_status_msg = "Conectando al servidor..."
                    multi_connect_action = "host"
                    anim.reset()
            elif icon_key == "join":
                state = S_MULTI_JOIN
                multi_input_text = ""
                multi_input_active = True
                multi_input_field = 0
                selected = 0
                multi_status_msg = ""
                anim.reset()

        elif state == S_MULTI_JOIN:
            if icon_key == "back":
                go_back()
            elif icon_key == "join":
                if not multi_input_text.strip():
                    multi_status_msg = "Ingresa un codigo de sala"
                elif net_client:
                    if net_client.connected:
                        net_client.disconnect()
                    # La conexion es asincrona (WebSocket en la web): pasa
                    # por S_MULTI_CONNECTING, que hace set_name + join_room
                    # al confirmarse.
                    multi_connect_action = "join"
                    state = S_MULTI_CONNECTING
                    multi_status_msg = "Conectando al servidor..."
                    anim.reset()

        elif state == S_MULTI_WAIT:
            if icon_key == "ready":
                if net_client:
                    net_client.set_ready(True)
                    multi_status_msg = "Esperando al rival..."
            elif icon_key == "leave":
                if net_client:
                    net_client.leave_room()
                    net_client.disconnect()
                go_back()

    def change_option_left(idx):
        global low_end
        nonlocal profile_status_msg
        items = get_items()
        if idx >= len(items):
            return
        icon_key = items[idx][0]
        if state == S_GAME:
            if icon_key == "das":
                i = DAS_VALUES.index(settings["das"])
                settings["das"] = DAS_VALUES[(i - 1) % len(DAS_VALUES)]
            elif icon_key == "arr":
                i = ARR_VALUES.index(settings["arr"])
                settings["arr"] = ARR_VALUES[(i - 1) % len(ARR_VALUES)]
            elif icon_key == "lock":
                i = LOCK_VALUES.index(settings["lock"])
                settings["lock"] = LOCK_VALUES[(i - 1) % len(LOCK_VALUES)]
            elif icon_key == "ghost":
                settings["ghost"] = not settings["ghost"]
            elif icon_key == "queue":
                i = NEXT_VALUES.index(settings["next_count"])
                settings["next_count"] = NEXT_VALUES[(i - 1) % len(NEXT_VALUES)]
            elif icon_key == "low_end":
                settings["low_end"] = not settings.get("low_end", False)
        elif state == S_PERSONAL:
            if icon_key == "bg":
                settings["bg_style"] = (settings["bg_style"] - 1) % BG_STYLE_COUNT
            elif icon_key == "menu_blocks":
                settings["menu_falling_blocks"] = not settings["menu_falling_blocks"]
        elif state == S_SOUND:
            if icon_key == "volume":
                settings["volume"] = max(0, settings["volume"] - 10)
        elif state == S_PROFILE:
            if icon_key == "avatar_color":
                settings["avatar_color_idx"] = (settings["avatar_color_idx"] - 1) % len(settings["piece_colors"])
            elif icon_key == "banner_color":
                settings["banner_color_idx"] = (settings.get("banner_color_idx", 0) - 1) % len(BANNER_COLORS)
            elif icon_key == "title":
                p_wins = get_player_rank(mp_stats, loaded_username).get("wins", 0) if loaded_username else 0
                unlocked_titles = get_unlocked_titles(loaded_username, p_wins)
                if unlocked_titles:
                    settings["profile_title_idx"] = (settings.get("profile_title_idx", 0) - 1) % len(unlocked_titles)
            elif icon_key == "avatar_photo" and (settings.get("avatar_path") or settings.get("avatar_data")):
                settings["avatar_path"] = ""
                settings["avatar_data"] = ""
                profile_status_msg = "Foto de perfil quitada"
                if loaded_username:
                    save_user_settings(loaded_username, settings)
        update_option_texts()
        if loaded_username:
            save_user_settings(loaded_username, settings)

    def change_option_right(idx):
        select_item(idx)

    def login_submit():
        """Accion del boton ENTRAR/CREAR y de ENTER: registrar o iniciar
        sesion segun el modo actual."""
        nonlocal state, login_is_register, login_error
        nonlocal loading_progress, loading_display, loading_stage, loading_start_time, loading_done, loaded_username, is_admin
        if login_is_register:
            ok, msg = register_account(login_user, login_pass)
            if ok:
                login_is_register = False
                login_error = "Cuenta creada. Ahora inicia sesion."
            else:
                login_error = msg
        else:
            ok, msg = login_account(login_user, login_pass)
            if ok:
                loaded_username = login_user
                is_admin = bool(load_accounts().get(login_user, {}).get("admin"))
                save_session(login_user)
                state = S_LOADING
                loading_progress = 0.0
                loading_display = 0.0
                loading_stage = 0
                loading_start_time = _time.time()
                loading_done = False
            else:
                login_error = msg

    go_btns = None

    while True:
        dt = clock.tick(60)
        win_w, win_h = screen.get_size()

        font_title_size = max(int(min(win_w, win_h) * 0.065), 22)
        font_opt_size = max(int(min(win_w, win_h) * 0.03), 13)
        font_sub_size = max(int(min(win_w, win_h) * 0.02), 10)
        font_game_size = max(int(min(win_w, win_h) * 0.025), 11)

        font_title = get_font(font_title_size, True)
        font_option = get_font(font_opt_size, True)
        font_sub = get_font(font_sub_size)
        font_game = get_font(font_game_size, True)

        t_ev0 = _time.perf_counter()
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                if server_process and server_process.poll() is None:
                    try: server_process.terminate()
                    except Exception: pass
                pygame.quit()
                sys.exit()

            if event.type == pygame.JOYDEVICEADDED:
                if not gamepad.connected:
                    gamepad.connect()
            if event.type == pygame.JOYDEVICEREMOVED:
                gamepad.disconnect()

            if event.type == pygame.KEYDOWN and event.key == pygame.K_F3:
                show_fps = not show_fps

            mx, my = pygame.mouse.get_pos()

            # Click en la insignia de perfil (esquina superior derecha),
            # visible en toda la rama de menus. Se revisa antes que
            # cualquier manejo especifico de estado para que funcione
            # sin importar en que pantalla del menu este el usuario.
            if (loaded_username and event.type == pygame.MOUSEBUTTONDOWN and event.button == 1
                    and _profile_badge_rect and _profile_badge_rect.collidepoint(mx, my)
                    and state in (S_MENU, S_PLAY_MENU, S_OPTIONS, S_GAME, S_PERSONAL, S_SOUND, S_PROFILE)):
                if state != S_PROFILE:
                    sfx.play("menu_click")
                    state = S_PROFILE
                    selected = 0
                    anim.reset()
                continue

            if state == S_LOGIN:
                cx = win_w // 2
                box_w = int(win_w * 0.35)
                box_h = int(win_h * 0.5)
                box_x = cx - box_w // 2
                box_y = int(win_h * 0.28)
                field_w = box_w - 40
                field_x = box_x + 20
                field_h = int(win_h * 0.05)
                field_y = box_y + 60

                if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    for i in range(2):
                        fy = field_y + i * (field_h + 35) + (font_sub.get_height() + 4)
                        if field_x <= mx <= field_x + field_w and fy <= my <= fy + field_h:
                            login_field = i
                    # Boton ENTRAR / CREAR CUENTA
                    login_submit_h = int(win_h * 0.05)
                    login_submit_y = field_y + 2 * (field_h + 35) + 10
                    if field_x <= mx <= field_x + field_w and login_submit_y <= my <= login_submit_y + login_submit_h:
                        login_submit()
                    # Boton alternar modo (antes solo F1)
                    login_tog_h = int(win_h * 0.04)
                    login_tog_y = box_y + box_h - login_tog_h - 18
                    if field_x <= mx <= field_x + field_w and login_tog_y <= my <= login_tog_y + login_tog_h:
                        login_is_register = not login_is_register
                        login_error = ""

                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_TAB:
                        login_field = 1 - login_field
                    elif event.key == pygame.K_RETURN:
                        login_submit()
                    elif event.key == pygame.K_ESCAPE:
                        pygame.quit()
                        sys.exit()
                    elif event.key == pygame.K_F1:
                        login_is_register = not login_is_register
                        login_error = ""
                    elif event.key == pygame.K_BACKSPACE:
                        if login_field == 0:
                            login_user = login_user[:-1]
                        else:
                            login_pass = login_pass[:-1]
                    else:
                        ch = event.unicode
                        if ch and ch.isprintable():
                            if login_field == 0 and len(login_user) < 16:
                                if ch.isalnum() or ch in "_- ":
                                    login_user += ch
                            elif login_field == 1 and len(login_pass) < 32:
                                login_pass += ch

            elif state == S_LOADING:
                if event.type == pygame.KEYDOWN:
                    pass

            elif state == S_PLAYING:
                if paused:
                    if event.type == pygame.KEYDOWN:
                        if event.key in (pygame.K_ESCAPE, pygame.K_p):
                            paused = False
                        elif event.key == pygame.K_UP:
                            pause_selected = (pause_selected - 1) % 3
                        elif event.key == pygame.K_DOWN:
                            pause_selected = (pause_selected + 1) % 3
                        elif event.key == pygame.K_RETURN:
                            if pause_selected == 0:
                                paused = False
                            elif pause_selected == 1:
                                game = Tetris(settings)
                                combo = ComboSystem()
                                effects = ScreenEffects()
                                game.combo = combo
                                game.effects = effects
                                input_handler = InputHandler(settings["keys"])
                                paused = False
                            else:
                                paused = False
                                state = S_MENU
                                selected = 0
                                anim.reset()
                    elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1 and pause_btn_rects:
                        for i, (rx, ry, rw, rh) in enumerate(pause_btn_rects):
                            if rx <= mx <= rx + rw and ry <= my <= ry + rh:
                                pause_selected = i
                                if i == 0:
                                    paused = False
                                elif i == 1:
                                    game = Tetris(settings)
                                    combo = ComboSystem()
                                    effects = ScreenEffects()
                                    game.combo = combo
                                    game.effects = effects
                                    input_handler = InputHandler(settings["keys"])
                                    paused = False
                                else:
                                    paused = False
                                    state = S_MENU
                                    selected = 0
                                    anim.reset()
                                break
                elif event.type == pygame.KEYDOWN:
                    keys = settings["keys"]
                    if event.key == keys.get("restart", pygame.K_r):
                        game = Tetris(settings)
                        combo = ComboSystem()
                        effects = ScreenEffects()
                        game.combo = combo
                        game.effects = effects
                        input_handler = InputHandler(settings["keys"])
                    elif event.key == pygame.K_ESCAPE:
                        if game.game_over:
                            state = S_MENU
                            selected = 0
                            anim.reset()
                        else:
                            paused = True
                            pause_selected = 0
                    elif event.key == pygame.K_p and not game.game_over:
                        paused = True
                        pause_selected = 0
                    elif not game.game_over:
                        if event.key == keys.get("rotate", pygame.K_UP):
                            game.rotate_piece()
                        elif event.key == keys.get("rotate_ccw", pygame.K_z):
                            game.rotate_ccw_piece()
                        elif event.key == keys.get("hard_drop", pygame.K_SPACE):
                            game.hard_drop()
                        elif event.key == keys.get("hold", pygame.K_c):
                            game.do_hold()
                        else:
                            input_handler.on_key_down(event.key, game)
                if event.type == pygame.KEYUP:
                    input_handler.on_key_up(event.key)
                if event.type == pygame.MOUSEBUTTONDOWN and not game.game_over and not paused:
                    if event.button == 3:
                        game.rotate_ccw_piece()
                    elif event.button == 1:
                        game.rotate_piece()
                if event.type == pygame.MOUSEBUTTONDOWN and game.game_over and go_btns:
                    restart_rect, menu_rect = go_btns
                    if restart_rect.collidepoint(event.pos):
                        game = Tetris(settings)
                        combo = ComboSystem()
                        effects = ScreenEffects()
                        game.combo = combo
                        game.effects = effects
                        input_handler = InputHandler(settings["keys"])
                    elif menu_rect.collidepoint(event.pos):
                        state = S_MENU
                        selected = 0
                        anim.reset()

            elif state == S_CONTROLS:
                ctrl_row_w = int(win_w * 0.6)
                ctrl_start_x = int(win_w * 0.05)
                top_bar_h = max(int(win_h * 0.06), 28)
                ctrl_logo_text_h = font_title.get_height()
                ctrl_start_y = top_bar_h + int(win_h * 0.04) + ctrl_logo_text_h + int(win_h * 0.03)
                ctrl_row_h = max(int(win_h * 0.055), 28)
                ctrl_gap = max(int(win_h * 0.006), 3)

                if not waiting_for_key:
                    for i in range(len(CTRL_ACTIONS) + 1):
                        by = ctrl_start_y + i * (ctrl_row_h + ctrl_gap)
                        if ctrl_start_x <= mx <= ctrl_start_x + ctrl_row_w and by <= my <= by + ctrl_row_h:
                            selected = i

                if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    for i in range(len(CTRL_ACTIONS) + 1):
                        by = ctrl_start_y + i * (ctrl_row_h + ctrl_gap)
                        if ctrl_start_x <= mx <= ctrl_start_x + ctrl_row_w and by <= my <= by + ctrl_row_h:
                            if i == len(CTRL_ACTIONS):
                                go_back()
                            else:
                                selected = i
                                waiting_for_key = True

                if event.type == pygame.KEYDOWN:
                    if waiting_for_key:
                        if event.key == pygame.K_ESCAPE:
                            waiting_for_key = False
                        else:
                            settings["keys"][CTRL_ACTIONS[selected]] = event.key
                            waiting_for_key = False
                            if loaded_username:
                                save_user_settings(loaded_username, settings)
                    else:
                        if event.key == pygame.K_UP:
                            selected = (selected - 1) % (len(CTRL_ACTIONS) + 1)
                        elif event.key == pygame.K_DOWN:
                            selected = (selected + 1) % (len(CTRL_ACTIONS) + 1)
                        elif event.key == pygame.K_RETURN:
                            if selected == len(CTRL_ACTIONS):
                                go_back()
                            else:
                                waiting_for_key = True
                        elif event.key == pygame.K_ESCAPE:
                            go_back()

            elif state == S_COLOR_CUSTOM:
                cc_row_w = int(win_w * 0.88)
                cc_start_x = int(win_w * 0.05)
                top_bar_h = max(int(win_h * 0.06), 28)
                cc_logo_text_h = font_title.get_height()
                cc_start_y = top_bar_h + int(win_h * 0.04) + cc_logo_text_h + int(win_h * 0.03)
                cc_row_h = max(int(win_h * 0.065), 32)
                cc_gap = max(int(win_h * 0.007), 3)

                if event.type == pygame.MOUSEMOTION:
                    for i in range(8):
                        by = cc_start_y + i * (cc_row_h + cc_gap)
                        if cc_start_x <= mx <= cc_start_x + cc_row_w and by <= my <= by + cc_row_h:
                            color_selected_piece = i

                if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    for i in range(8):
                        by = cc_start_y + i * (cc_row_h + cc_gap)
                        if cc_start_x <= mx <= cc_start_x + cc_row_w and by <= my <= by + cc_row_h:
                            color_selected_piece = i
                            if i == 7:
                                go_back()
                            else:
                                pal_idx = -1
                                for ci, pc in enumerate(COLOR_PALETTE):
                                    if pc == settings["piece_colors"][i]:
                                        pal_idx = ci
                                        break
                                if pal_idx >= 0:
                                    new_idx = (pal_idx + 1) % len(COLOR_PALETTE)
                                    settings["piece_colors"][i] = COLOR_PALETTE[new_idx]

                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_UP:
                        color_selected_piece = (color_selected_piece - 1) % 8
                    elif event.key == pygame.K_DOWN:
                        color_selected_piece = (color_selected_piece + 1) % 8
                    elif event.key == pygame.K_RETURN:
                        if color_selected_piece == 7:
                            go_back()
                    elif event.key == pygame.K_ESCAPE:
                        go_back()
                    elif event.key in (pygame.K_LEFT, pygame.K_RIGHT):
                        if color_selected_piece < 7:
                            pal_idx = -1
                            for ci, pc in enumerate(COLOR_PALETTE):
                                if pc == settings["piece_colors"][color_selected_piece]:
                                    pal_idx = ci
                                    break
                            if pal_idx >= 0:
                                if event.key == pygame.K_RIGHT:
                                    new_idx = (pal_idx + 1) % len(COLOR_PALETTE)
                                else:
                                    new_idx = (pal_idx - 1) % len(COLOR_PALETTE)
                                settings["piece_colors"][color_selected_piece] = COLOR_PALETTE[new_idx]

            elif state == S_MULTI_JOIN:
                join_cx = win_w // 2
                join_panel_y = int(win_h * 0.18)
                join_panel_h = int(win_h * 0.45)
                join_btn_w = int(win_w * 0.2)
                join_btn_h = max(int(win_h * 0.05), 32)
                join_btn_x = join_cx - join_btn_w // 2
                join_btn_y = join_panel_y + join_panel_h + 15
                join_back_y = join_btn_y + join_btn_h + 10

                if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    if join_btn_x <= mx <= join_btn_x + join_btn_w:
                        if join_btn_y <= my <= join_btn_y + join_btn_h:
                            if multi_input_text.strip() and net_client:
                                state = S_MULTI_CONNECTING
                                multi_status_msg = "Conectando al servidor..."
                                multi_connect_action = "join"
                                anim.reset()
                        elif join_back_y <= my <= join_back_y + join_btn_h:
                            go_back()

                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_BACKSPACE:
                        multi_input_text = multi_input_text[:-1]
                    elif event.key == pygame.K_RETURN:
                        if multi_input_text.strip() and net_client:
                            state = S_MULTI_CONNECTING
                            multi_status_msg = "Conectando al servidor..."
                            multi_connect_action = "join"
                            anim.reset()
                    elif event.key == pygame.K_ESCAPE:
                        go_back()
                    elif event.unicode and len(multi_input_text) < 8:
                        ch = event.unicode.upper()
                        if ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
                            multi_input_text += ch

                if event.type == pygame.MOUSEMOTION:
                    if join_btn_x <= mx <= join_btn_x + join_btn_w:
                        if join_btn_y <= my <= join_btn_y + join_btn_h:
                            selected = 0
                        elif join_back_y <= my <= join_back_y + join_btn_h:
                            selected = 1

            elif state == S_MULTI_WAIT:
                wait_cx = win_w // 2
                wait_panel_y = int(win_h * 0.18)
                wait_panel_h = int(win_h * 0.55)
                wait_btn_w = int(win_w * 0.2)
                wait_btn_h = max(int(win_h * 0.05), 32)
                wait_btn_x = wait_cx - wait_btn_w // 2
                wait_btn_y = wait_panel_y + wait_panel_h + 45
                wait_leave_y = wait_btn_y + wait_btn_h + 10

                if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1 and not room_chat.input_active:
                    if wait_btn_x <= mx <= wait_btn_x + wait_btn_w:
                        if wait_btn_y <= my <= wait_btn_y + wait_btn_h:
                            if net_client:
                                net_client.set_ready(True)
                                multi_status_msg = "Esperando al rival..."
                        elif wait_leave_y <= my <= wait_leave_y + wait_btn_h:
                            if net_client:
                                net_client.leave_room()
                                net_client.disconnect()
                            room_chat.clear()
                            go_back()

                if event.type == pygame.KEYDOWN:
                    if room_chat.input_active:
                        if event.key == pygame.K_RETURN:
                            room_chat.send(net_client)
                        elif event.key == pygame.K_ESCAPE:
                            room_chat.stop_typing()
                        elif event.key == pygame.K_BACKSPACE:
                            room_chat.backspace()
                        else:
                            room_chat.add_char(event.unicode)
                    elif event.key == pygame.K_t:
                        room_chat.start_typing()
                    elif event.key in (pygame.K_RETURN, pygame.K_SPACE):
                        if net_client:
                            net_client.set_ready(True)
                            multi_status_msg = "Esperando al rival..."
                    elif event.key == pygame.K_ESCAPE:
                        if net_client:
                            net_client.leave_room()
                            net_client.disconnect()
                        room_chat.clear()
                        go_back()

                if event.type == pygame.MOUSEMOTION and not room_chat.input_active:
                    if wait_btn_x <= mx <= wait_btn_x + wait_btn_w:
                        if wait_btn_y <= my <= wait_btn_y + wait_btn_h:
                            selected = 0
                        elif wait_leave_y <= my <= wait_leave_y + wait_btn_h:
                            selected = 1

            elif state in (S_MULTI_PLAY, S_MULTI_CONNECTING):
                if event.type == pygame.KEYDOWN:
                    keys = settings["keys"]
                    if event.key == pygame.K_ESCAPE:
                        if state == S_MULTI_PLAY and net_client and game:
                            net_client.send_board_update(
                                game.grid, 0, 0, 1, None, [], 0, True
                            )
                            net_client.leave_room()
                            net_client.disconnect()
                        elif net_client:
                            net_client.disconnect()
                        room_chat.clear()
                        multi_connect_result = None
                        multi_connect_thread = None
                        state = S_MULTI_MENU
                        selected = 0
                        multi_status_msg = ""
                        anim.reset()
                    elif game and not game.game_over:
                        if event.key == keys.get("rotate", pygame.K_UP):
                            game.rotate_piece()
                        elif event.key == keys.get("rotate_ccw", pygame.K_z):
                            game.rotate_ccw_piece()
                        elif event.key == keys.get("hard_drop", pygame.K_SPACE):
                            game.hard_drop()
                        elif event.key == keys.get("hold", pygame.K_c):
                            game.do_hold()
                        else:
                            input_handler.on_key_down(event.key, game)
                if event.type == pygame.KEYUP and game:
                    input_handler.on_key_up(event.key)
                if event.type == pygame.MOUSEBUTTONDOWN and game and not game.game_over:
                    if event.button == 3:
                        game.rotate_ccw_piece()
                    elif event.button == 1:
                        game.rotate_piece()
                if event.type == pygame.MOUSEBUTTONDOWN and game and game.game_over and go_btns:
                    restart_rect, menu_rect = go_btns
                    if restart_rect.collidepoint(event.pos) or menu_rect.collidepoint(event.pos):
                        if state == S_MULTI_PLAY and net_client and game:
                            net_client.send_board_update(
                                game.grid, 0, 0, 1, None, [], 0, True
                            )
                            net_client.leave_room()
                            net_client.disconnect()
                        elif net_client:
                            net_client.disconnect()
                        room_chat.clear()
                        multi_connect_result = None
                        multi_connect_thread = None
                        state = S_MULTI_MENU
                        selected = 0
                        multi_status_msg = ""
                        anim.reset()

            elif state == S_VS_PLAY:
                if event.type == pygame.KEYDOWN:
                    keys = settings["keys"]
                    if event.key == keys.get("restart", pygame.K_r):
                        game = Tetris(settings)
                        combo = ComboSystem()
                        effects = ScreenEffects()
                        game.combo = combo
                        game.effects = effects
                        input_handler = InputHandler(settings["keys"])
                        bot_game = Tetris(settings)
                        bot = BotAI(think_delay=600)
                        multi_opponent = {
                            "name": "CPU", "grid": bot_game.grid, "score": 0,
                            "lines": 0, "level": 1, "current_piece": None,
                            "next_queue": [], "combo": 0, "game_over": False,
                        }
                        vs_state = {"player_lines": 0, "bot_lines": 0, "ended": False}
                    elif event.key == pygame.K_ESCAPE:
                        state = S_PLAY_MENU
                        selected = 0
                        anim.reset()
                    elif not game.game_over:
                        if event.key == keys.get("rotate", pygame.K_UP):
                            game.rotate_piece()
                        elif event.key == keys.get("rotate_ccw", pygame.K_z):
                            game.rotate_ccw_piece()
                        elif event.key == keys.get("hard_drop", pygame.K_SPACE):
                            game.hard_drop()
                        elif event.key == keys.get("hold", pygame.K_c):
                            game.do_hold()
                        else:
                            input_handler.on_key_down(event.key, game)
                if event.type == pygame.KEYUP:
                    input_handler.on_key_up(event.key)
                if event.type == pygame.MOUSEBUTTONDOWN and not game.game_over:
                    if event.button == 3:
                        game.rotate_ccw_piece()
                    elif event.button == 1:
                        game.rotate_piece()
                if event.type == pygame.MOUSEBUTTONDOWN and game.game_over and go_btns:
                    restart_rect, menu_rect = go_btns
                    if restart_rect.collidepoint(event.pos):
                        game = Tetris(settings)
                        combo = ComboSystem()
                        effects = ScreenEffects()
                        game.combo = combo
                        game.effects = effects
                        input_handler = InputHandler(settings["keys"])
                        bot_game = Tetris(settings)
                        bot = BotAI(think_delay=600)
                        multi_opponent = {
                            "name": "CPU", "grid": bot_game.grid, "score": 0,
                            "lines": 0, "level": 1, "current_piece": None,
                            "next_queue": [], "combo": 0, "game_over": False,
                        }
                        vs_state = {"player_lines": 0, "bot_lines": 0, "ended": False}
                    elif menu_rect.collidepoint(event.pos):
                        state = S_PLAY_MENU
                        selected = 0
                        anim.reset()

            elif state == S_MUSIC_YT:
                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        go_back()
                    elif event.key == pygame.K_BACKSPACE:
                        music_yt_url = music_yt_url[:-1]
                    elif event.key == pygame.K_RETURN:
                        if music_yt_url.strip() and not music_yt_downloading:
                            select_item(0)
                    elif event.key == pygame.K_v and (event.mod & (pygame.KMOD_CTRL | pygame.KMOD_GUI)):
                        try:
                            clip = pygame.scrap.get(pygame.SCRAP_TEXT)
                            if clip:
                                text = clip.decode("utf-8", errors="ignore").rstrip("\x00")
                                music_yt_url += text
                        except Exception:
                            pass
                    elif event.unicode and not (event.mod & pygame.KMOD_CTRL):
                        music_yt_url += event.unicode
                if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    cx = win_w // 2
                    panel_w = int(win_w * 0.5)
                    panel_h = int(win_h * 0.45)
                    panel_x = cx - panel_w // 2
                    panel_y = int(win_h * 0.15)
                    input_w = panel_w - 40
                    input_h = 40
                    input_x = panel_x + 20
                    input_y = panel_y + 100
                    dl_btn_w = int(panel_w * 0.5)
                    dl_btn_h = 36
                    dl_btn_x = cx - dl_btn_w // 2
                    dl_btn_y = input_y + input_h + 20
                    back_btn_y = dl_btn_y + dl_btn_h + 10
                    if dl_btn_x <= mx <= dl_btn_x + dl_btn_w and dl_btn_y <= my <= dl_btn_y + dl_btn_h:
                        if music_yt_url.strip() and not music_yt_downloading:
                            select_item(0)
                    if dl_btn_x <= mx <= dl_btn_x + dl_btn_w and back_btn_y <= my <= back_btn_y + dl_btn_h:
                        go_back()
                if event.type == pygame.MOUSEMOTION:
                    cx = win_w // 2
                    panel_w = int(win_w * 0.5)
                    panel_x = cx - panel_w // 2
                    panel_y = int(win_h * 0.15)
                    dl_btn_w = int(panel_w * 0.5)
                    dl_btn_h = 36
                    dl_btn_x = cx - dl_btn_w // 2
                    input_h = 40
                    dl_btn_y = panel_y + 100 + input_h + 20
                    back_btn_y = dl_btn_y + dl_btn_h + 10
                    if dl_btn_x <= mx <= dl_btn_x + dl_btn_w:
                        if dl_btn_y <= my <= dl_btn_y + dl_btn_h:
                            selected = 0
                        elif back_btn_y <= my <= back_btn_y + dl_btn_h:
                            selected = 1

            elif state == S_MUSIC_PLAYING:
                if event.type == pygame.KEYDOWN:
                    keys = settings["keys"]
                    if event.key == pygame.K_ESCAPE:
                        music_player.stop()
                        state = S_MUSIC_MENU
                        selected = 0
                        anim.reset()
                    elif event.key == keys.get("restart", pygame.K_r) and game:
                        game = Tetris(settings)
                        combo = ComboSystem()
                        effects = ScreenEffects()
                        game.combo = combo
                        game.effects = effects
                        input_handler = InputHandler(settings["keys"])
                        music_won = False
                        music_win_btns = None
                        music_player.stop()
                        music_player.play()
                    elif game and not game.game_over and not music_won:
                        if event.key == keys.get("rotate", pygame.K_UP):
                            game.rotate_piece()
                        elif event.key == keys.get("rotate_ccw", pygame.K_z):
                            game.rotate_ccw_piece()
                        elif event.key == keys.get("hard_drop", pygame.K_SPACE):
                            game.hard_drop()
                        elif event.key == keys.get("hold", pygame.K_c):
                            game.do_hold()
                        else:
                            input_handler.on_key_down(event.key, game)
                if event.type == pygame.KEYUP and game:
                    input_handler.on_key_up(event.key)
                if event.type == pygame.MOUSEBUTTONDOWN and game and not game.game_over and not music_won:
                    if event.button == 3:
                        game.rotate_ccw_piece()
                    elif event.button == 1:
                        game.rotate_piece()
                if event.type == pygame.MOUSEBUTTONDOWN and game and game.game_over and not music_won and go_btns:
                    restart_rect, menu_rect = go_btns
                    if restart_rect.collidepoint(event.pos):
                        game = Tetris(settings)
                        combo = ComboSystem()
                        effects = ScreenEffects()
                        game.combo = combo
                        game.effects = effects
                        input_handler = InputHandler(settings["keys"])
                        music_won = False
                        music_win_btns = None
                        music_player.stop()
                        music_player.play()
                    elif menu_rect.collidepoint(event.pos):
                        music_player.stop()
                        state = S_MUSIC_MENU
                        selected = 0
                        anim.reset()
                if event.type == pygame.MOUSEBUTTONDOWN and music_won and music_win_btns:
                    restart_rect, menu_rect = music_win_btns
                    rx, ry, rw, rh = restart_rect
                    mx, my = event.pos
                    if rx <= mx <= rx + rw and ry <= my <= ry + rh:
                        game = Tetris(settings)
                        combo = ComboSystem()
                        effects = ScreenEffects()
                        game.combo = combo
                        game.effects = effects
                        input_handler = InputHandler(settings["keys"])
                        music_won = False
                        music_win_btns = None
                        music_player.stop()
                        music_player.play()
                    mx2, my2 = event.pos
                    menux, menuy, menuw, menuh = menu_rect
                    if menux <= mx2 <= menux + menuw and menuy <= my2 <= menuy + menuh:
                        music_player.stop()
                        state = S_MUSIC_MENU
                        selected = 0
                        anim.reset()

            elif state == S_NEWS:
                news_btn_w = int(win_w * 0.2)
                news_btn_h = max(int(win_h * 0.05), 32)
                news_footer_h = max(int(win_h * 0.045), 22)
                news_btn_y = win_h - news_footer_h - news_btn_h - 20
                news_cx = win_w // 2
                news_pub_x = news_cx - news_btn_w - 10
                news_back_x = news_cx + 10
                card_x = int(win_w * 0.07)
                card_w = int(win_w * 0.86)
                card_h = max(int(win_h * 0.16), 95)
                card_gap = 12
                cards_top = max(int(win_h * 0.06), 28) + int(win_h * 0.04) + font_title.get_height() + int(win_h * 0.03)
                view_h = (news_btn_y if is_admin else win_h - news_footer_h) - 12 - cards_top
                news_max_scroll = max(0, len(news_list) * (card_h + card_gap) - card_gap - view_h)

                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        go_back()
                    elif event.key in (pygame.K_UP, pygame.K_w):
                        news_selected = max(0, news_selected - 1)
                        sfx.play("menu_click")
                        sel_top = cards_top + news_selected * (card_h + card_gap)
                        if sel_top - news_scroll_target < cards_top:
                            news_scroll_target = max(0, sel_top - cards_top)
                    elif event.key in (pygame.K_DOWN, pygame.K_s):
                        news_selected = min(max(len(news_list) - 1, 0), news_selected + 1)
                        sfx.play("menu_click")
                        sel_bot = cards_top + news_selected * (card_h + card_gap) + card_h
                        if sel_bot - news_scroll_target > cards_top + view_h:
                            news_scroll_target = min(news_max_scroll, sel_bot - cards_top - view_h)
                    elif event.key in (pygame.K_RETURN, pygame.K_p) and is_admin:
                        state = S_NEWS_EDIT
                        news_edit_title = ""
                        news_edit_body = ""
                        news_edit_field = 0
                        news_edit_image = ""
                        news_edit_image_name = ""
                        selected = 0
                        anim.reset()
                    elif event.key == pygame.K_DELETE and is_admin and news_list and net_client and net_client.connected:
                        item = news_list[min(news_selected, len(news_list) - 1)]
                        if news_delete_pending.get("id") == item.get("id") and _time.time() - news_delete_pending.get("t", 0) < 5:
                            net_client.delete_news(item.get("id"))
                            news_delete_pending = {}
                            news_status = "Borrando..."
                        else:
                            news_delete_pending = {"id": item.get("id"), "t": _time.time()}
                            news_status = "Pulsa SUPR de nuevo para confirmar"
                elif event.type == pygame.MOUSEWHEEL:
                    news_scroll_target = max(0, min(news_max_scroll, news_scroll_target - event.y * 60))
                elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    for i in range(len(news_list)):
                        cy_i = cards_top + i * (card_h + card_gap) - news_scroll
                        if cy_i + card_h < cards_top or cy_i > news_btn_y:
                            continue
                        if card_x <= mx <= card_x + card_w and cy_i <= my <= cy_i + card_h:
                            if news_selected != i:
                                sfx.play("menu_click")
                            news_selected = i
                            break
                    else:
                        if is_admin and news_pub_x <= mx <= news_pub_x + news_btn_w and news_btn_y <= my <= news_btn_y + news_btn_h:
                            state = S_NEWS_EDIT
                            news_edit_title = ""
                            news_edit_body = ""
                            news_edit_field = 0
                            news_edit_image = ""
                            news_edit_image_name = ""
                            selected = 0
                            anim.reset()
                        elif news_back_x <= mx <= news_back_x + news_btn_w and news_btn_y <= my <= news_btn_y + news_btn_h:
                            go_back()
                        elif not is_admin:
                            nb_x = news_cx - news_btn_w // 2
                            if nb_x <= mx <= nb_x + news_btn_w and news_btn_y <= my <= news_btn_y + news_btn_h:
                                go_back()

            elif state == S_NEWS_EDIT:
                edit_cx = win_w // 2
                edit_panel_w = int(win_w * 0.5)
                edit_panel_h = int(win_h * 0.6)
                edit_panel_x = edit_cx - edit_panel_w // 2
                edit_panel_y = int(win_h * 0.13)
                edit_field_w = edit_panel_w - 60
                edit_field_h = max(int(win_h * 0.06), 36)
                edit_title_y = edit_panel_y + 90
                edit_body_y = edit_title_y + edit_field_h + 70
                img_row_h = max(int(win_h * 0.05), 32)
                edit_img_y = edit_body_y + edit_field_h * 2 + 20
                edit_btn_w = int(edit_panel_w * 0.35)
                edit_btn_h = max(int(win_h * 0.05), 32)
                edit_btn_y = edit_img_y + img_row_h + 26
                edit_pub_x = edit_cx - edit_btn_w - 10
                edit_can_x = edit_cx + 10

                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        state = S_NEWS
                    elif event.key == pygame.K_TAB:
                        news_edit_field = 1 - news_edit_field
                    elif event.key == pygame.K_RETURN:
                        if news_edit_field == 0:
                            if news_edit_title.strip():
                                news_edit_field = 1
                        elif not news_edit_title.strip():
                            news_status = "Escribe un titulo primero"
                        elif not (net_client and net_client.connected):
                            news_status = "Sin conexion - no se puede publicar"
                        else:
                            net_client.add_news(news_edit_title.strip(), news_edit_body.strip(), news_edit_image)
                            news_status = "Publicando..."
                            state = S_NEWS
                    elif event.key == pygame.K_BACKSPACE:
                        if news_edit_field == 0:
                            news_edit_title = news_edit_title[:-1]
                        else:
                            news_edit_body = news_edit_body[:-1]
                    else:
                        ch = event.unicode
                        if ch and ch.isprintable() and not (event.mod & pygame.KMOD_CTRL):
                            if news_edit_field == 0 and len(news_edit_title) < 60:
                                news_edit_title += ch
                            elif news_edit_field == 1 and len(news_edit_body) < 300:
                                news_edit_body += ch
                if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    if edit_panel_x + 30 <= mx <= edit_panel_x + 30 + edit_field_w:
                        if edit_title_y <= my <= edit_title_y + edit_field_h:
                            news_edit_field = 0
                        elif edit_body_y <= my <= edit_body_y + edit_field_h * 2:
                            news_edit_field = 1
                        elif edit_img_y <= my <= edit_img_y + img_row_h:
                            clear_zone_x = edit_panel_x + 30 + edit_field_w - 34
                            if news_edit_image and mx >= clear_zone_x:
                                news_edit_image = ""
                                news_edit_image_name = ""
                                news_status = ""
                            elif IS_WEB:
                                news_status = "Cargar imagenes no disponible en el navegador"
                            else:
                                img_path = open_file_dialog("Selecciona una imagen para la noticia", [
                                    ("Imagenes", "*.png *.jpg *.jpeg *.bmp *.webp"),
                                    ("Todos los archivos", "*.*"),
                                ])
                                if img_path:
                                    b64_img, img_err = encode_news_image(img_path)
                                    if b64_img:
                                        news_edit_image = b64_img
                                        news_edit_image_name = os.path.basename(img_path)
                                        news_status = ""
                                    else:
                                        news_status = img_err or "No se pudo procesar la imagen"
                    if edit_pub_x <= mx <= edit_pub_x + edit_btn_w and edit_btn_y <= my <= edit_btn_y + edit_btn_h:
                        if news_edit_title.strip() and net_client and net_client.connected:
                            net_client.add_news(news_edit_title.strip(), news_edit_body.strip(), news_edit_image)
                            news_status = "Publicando..."
                            state = S_NEWS
                        elif not news_edit_title.strip():
                            news_status = "Escribe un titulo primero"
                        else:
                            news_status = "Sin conexion - no se puede publicar"
                    elif edit_can_x <= mx <= edit_can_x + edit_btn_w and edit_btn_y <= my <= edit_btn_y + edit_btn_h:
                        state = S_NEWS

            elif state == S_BIO_EDIT:
                cx = win_w // 2
                box_w = int(win_w * 0.55)
                field_h = max(int(win_h * 0.16), 70)
                field_y = win_h // 2 - field_h // 2
                btn_w = int(box_w * 0.3)
                btn_h = max(int(win_h * 0.08), 34)
                btn_y = field_y + field_h + int(win_h * 0.03)
                save_x = cx - btn_w - 8
                cancel_x = cx + 8
                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        state = S_PROFILE
                        selected = 0
                    elif event.key == pygame.K_RETURN:
                        settings["bio"] = bio_edit_text.strip()
                        if loaded_username:
                            save_user_settings(loaded_username, settings)
                        update_option_texts()
                        state = S_PROFILE
                        selected = 0
                    elif event.key == pygame.K_BACKSPACE:
                        bio_edit_text = bio_edit_text[:-1]
                    else:
                        ch = event.unicode
                        if ch and ch.isprintable() and not (event.mod & pygame.KMOD_CTRL) and len(bio_edit_text) < 80:
                            bio_edit_text += ch
                elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    if save_x <= mx <= save_x + btn_w and btn_y <= my <= btn_y + btn_h:
                        settings["bio"] = bio_edit_text.strip()
                        if loaded_username:
                            save_user_settings(loaded_username, settings)
                        update_option_texts()
                        state = S_PROFILE
                        selected = 0
                    elif cancel_x <= mx <= cancel_x + btn_w and btn_y <= my <= btn_y + btn_h:
                        state = S_PROFILE
                        selected = 0

            else:
                items = get_items()
                n_items = len(items)
                btn_w = int(win_w * 0.52)
                btn_h = max(int(win_h * 0.1), 44)
                start_x = int(win_w * 0.05)
                top_bar_h = max(int(win_h * 0.06), 28)
                logo_y = top_bar_h + int(win_h * 0.04)
                title_t = min(anim.time * 1.2, 1.0)
                title_y = logo_y + int((1 - ease_out_back(title_t)) * 50)
                title_h = font_title.get_height()
                start_y = title_y + title_h + int(win_h * 0.04)
                gap = max(int(win_h * 0.015), 6)

                if state == S_MULTI_MENU and show_multi_warning:
                    if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1 and multi_warning_btn:
                        wx, wy, ww, wh = multi_warning_btn
                        if wx <= mx <= wx + ww and wy <= my <= wy + wh:
                            show_multi_warning = False
                    elif event.type == pygame.KEYDOWN and event.key in (pygame.K_RETURN, pygame.K_ESCAPE, pygame.K_SPACE):
                        show_multi_warning = False
                elif state == S_MENU and show_love_note:
                    if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1 and love_note_btn:
                        lx, ly, lw, lh = love_note_btn
                        if lx <= mx <= lx + lw and ly <= my <= ly + lh:
                            show_love_note = False
                    elif event.type == pygame.KEYDOWN and event.key in (pygame.K_RETURN, pygame.K_ESCAPE, pygame.K_SPACE):
                        show_love_note = False
                elif state == S_MENU and global_chat.input_active:
                    if event.type == pygame.KEYDOWN:
                        if event.key == pygame.K_ESCAPE:
                            global_chat.stop_typing()
                        elif event.key == pygame.K_RETURN:
                            global_chat.send(net_client, global_chat_mode=True)
                        elif event.key == pygame.K_BACKSPACE:
                            global_chat.backspace()
                        else:
                            global_chat.add_char(event.unicode)
                    elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                        _tbh = max(int(win_h * 0.06), 28)
                        _tt = min(anim.time * 1.2, 1.0)
                        _ly = _tbh + int(win_h * 0.04) + int((1 - ease_out_back(_tt)) * 50)
                        _lh = font_title.get_height()
                        _cax = int(win_w * 0.48) + int(win_w * 0.02)
                        _caw = win_w - _cax - int(win_w * 0.02)
                        _cpt = _ly + _lh + int(win_h * 0.03)
                        _cfh = max(int(win_h * 0.045), 22)
                        _cih = font_sub.get_height() + 16
                        _cpb = win_h - _cfh - _cih - 12
                        _giy = _cpb + 6
                        if not (_cax <= mx <= _cax + _caw and _giy <= my <= _giy + _cih):
                            global_chat.stop_typing()
                else:
                    if not waiting_for_key:
                        for i in range(n_items):
                            by = start_y + i * (btn_h + gap) - menu_scroll
                            if start_x <= mx <= start_x + btn_w and by <= my <= by + btn_h:
                                selected = i

                    if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                        _tbh = max(int(win_h * 0.06), 28)
                        _tt = min(anim.time * 1.2, 1.0)
                        _ly = _tbh + int(win_h * 0.04) + int((1 - ease_out_back(_tt)) * 50)
                        _lh = font_title.get_height()
                        _cax = int(win_w * 0.48) + int(win_w * 0.02)
                        _caw = win_w - _cax - int(win_w * 0.02)
                        _cpt = _ly + _lh + int(win_h * 0.03)
                        _cfh = max(int(win_h * 0.045), 22)
                        _cih = font_sub.get_height() + 16
                        _cpb = win_h - _cfh - _cih - 12
                        _giy = _cpb + 6
                        if state == S_MENU and _cax <= mx <= _cax + _caw and _giy <= my <= _giy + _cih:
                            if net_client and net_client.connected:
                                global_chat.start_typing()
                        else:
                            for i in range(n_items):
                                by = start_y + i * (btn_h + gap) - menu_scroll
                                if start_x <= mx <= start_x + btn_w and by <= my <= by + btn_h:
                                    selected = i
                                    select_item(i)

                    if event.type == pygame.MOUSEWHEEL and state != S_MENU:
                        footer_h = max(int(win_h * 0.045), 22)
                        list_bottom = win_h - footer_h - 6
                        content_h = n_items * (btn_h + gap) - gap if n_items else 0
                        visible_h = max(list_bottom - start_y, 0)
                        max_scroll = max(0, content_h - visible_h)
                        menu_scroll_target = max(0, min(max_scroll, menu_scroll_target - event.y * 60))

                    if event.type == pygame.KEYDOWN:
                        if state == S_MENU and event.key == pygame.K_t:
                            if net_client and net_client.connected:
                                global_chat.start_typing()
                        elif event.key == pygame.K_UP:
                            selected = (selected - 1) % n_items
                        elif event.key == pygame.K_DOWN:
                            selected = (selected + 1) % n_items
                        elif event.key == pygame.K_RETURN:
                            select_item(selected)
                        elif event.key == pygame.K_ESCAPE:
                            go_back()
                        elif event.key == pygame.K_LEFT:
                            change_option_left(selected)
                        elif event.key == pygame.K_RIGHT:
                            change_option_right(selected)

                        if event.key in (pygame.K_UP, pygame.K_DOWN) and state != S_MENU:
                            # Autoscroll: si el item seleccionado queda fuera
                            # del area visible, deslizar la lista para mostrarlo.
                            footer_h = max(int(win_h * 0.045), 22)
                            list_bottom = win_h - footer_h - 6
                            sel_top = start_y + selected * (btn_h + gap)
                            sel_bot = sel_top + btn_h
                            if sel_top - menu_scroll_target < start_y:
                                menu_scroll_target = max(0, sel_top - start_y)
                            elif sel_bot - menu_scroll_target > list_bottom:
                                menu_scroll_target = max(0, sel_bot - list_bottom)

        ev_ms += ((_time.perf_counter() - t_ev0) * 1000 - ev_ms) * 0.05
        t_upd0 = _time.perf_counter()

        anim.update(dt)
        particles_bg.update(dt)
        falling_blocks_bg.update(dt)
        room_chat.update(dt)
        global_chat.update(dt)
        news_scroll += (news_scroll_target - news_scroll) * min(dt / 90.0, 1.0)
        if state != menu_scroll_state:
            menu_scroll_state = state
            menu_scroll = 0.0
            menu_scroll_target = 0.0
        else:
            menu_scroll += (menu_scroll_target - menu_scroll) * min(dt / 90.0, 1.0)
        fade.update(dt)

        if gamepad.connected and state in (S_PLAYING, S_MULTI_PLAY, S_VS_PLAY, S_MUSIC_PLAYING) and not game.game_over and not (state == S_MUSIC_PLAYING and music_won):
            for btn_id, action in GamepadHandler.GAMEPLAY_MAP.items():
                idx = GamepadHandler.REVERSE_MAP.get(btn_id, -1)
                if idx >= 0 and gamepad.get_button_down(idx):
                    if action == "rotate":
                        game.rotate_piece()
                    elif action == "rotate_ccw":
                        game.rotate_ccw_piece()
                    elif action == "hard_drop":
                        game.hard_drop()
                    elif action == "hold":
                        game.do_hold()
                    elif action == "restart":
                        game = Tetris(settings)
                        combo = ComboSystem()
                        effects = ScreenEffects()
                        game.combo = combo
                        game.effects = effects
                        input_handler = InputHandler(settings["keys"])
            dpad = gamepad.get_dpad()
            # DAS/ARR del control con las mismas velocidades que el
            # teclado: antes se movia 1 celda por frame (60 celdas/seg,
            # imposible de controlar).
            gp_dir = 0
            if dpad[0] == -1:
                gp_dir = -1
            elif dpad[0] == 1:
                gp_dir = 1
            if gp_dir != 0 and gp_dir != gp_das_dir:
                game.move(gp_dir)
                gp_das_dir = gp_dir
                gp_das_timer = 0.0
                gp_arr_timer = 0.0
            elif gp_dir != 0:
                gp_das_timer += dt
                if gp_das_timer >= settings["das"]:
                    gp_arr_timer += dt
                    while gp_arr_timer >= settings["arr"]:
                        gp_arr_timer -= settings["arr"]
                        if not game.move(gp_dir):
                            break
            else:
                gp_das_dir = 0
                gp_das_timer = 0.0
                gp_arr_timer = 0.0
            if dpad[1] == 1:
                game.soft_drop()
            elif dpad[1] == -1 and prev_game_dpad_y != -1:
                # D-pad/stick hacia arriba: rotar (como UP en teclado),
                # solo en el flanco de presion para no girar sin parar
                game.rotate_piece()
            prev_game_dpad_y = dpad[1]

        if gamepad.connected and state == S_MUSIC_PLAYING and music_won:
            if gamepad.get_button_down(GamepadHandler.REVERSE_MAP.get("a", -1)):
                game = Tetris(settings)
                combo = ComboSystem()
                effects = ScreenEffects()
                game.combo = combo
                game.effects = effects
                input_handler = InputHandler(settings["keys"])
                music_won = False
                music_win_btns = None
                music_player.stop()
                music_player.play()
            elif gamepad.get_button_down(GamepadHandler.REVERSE_MAP.get("b", -1)):
                music_player.stop()
                state = S_MUSIC_MENU
                selected = 0
                anim.reset()

        # Input de control para menus, por frame: ANTES el polling estaba
        # dentro del handler de KEYDOWN, asi que sin presionar teclas del
        # teclado el control nunca hacia nada en los menus.
        if gamepad.connected and state in (S_MENU, S_PLAY_MENU, S_OPTIONS, S_GAME, S_PERSONAL, S_SOUND, S_PROFILE, S_MULTI_MENU, S_MUSIC_MENU, S_MUSIC_DIFF, S_MUSIC_YT):
            menu_items_list = get_items()
            n_menu = len(menu_items_list)
            for action in gamepad.get_menu_action():
                if action == "up":
                    selected = (selected - 1) % n_menu
                elif action == "down":
                    selected = (selected + 1) % n_menu
                elif action == "left":
                    change_option_left(selected)
                elif action == "right":
                    change_option_right(selected)
            for held in gamepad.get_menu_held(dt):
                if held == "up":
                    selected = (selected - 1) % n_menu
                elif held == "down":
                    selected = (selected + 1) % n_menu
                elif held == "left":
                    change_option_left(selected)
                elif held == "right":
                    change_option_right(selected)
            num_btns = gamepad.joy.get_numbuttons() if gamepad.joy else 0
            for btn_id in range(min(num_btns, 11)):
                if gamepad.get_button_down(btn_id):
                    name = GamepadHandler.BUTTON_MAP.get(btn_id, "")
                    mapped = GamepadHandler.MENU_MAP.get(name, "")
                    if mapped == "confirm":
                        select_item(selected)
                    elif mapped == "back":
                        go_back()

        if state == S_LOADING:
            dt_sec = dt / 1000.0
            elapsed = _time.time() - loading_start_time
            loading_stages = [
                (0.3, "Cargando sprites..."),
                (0.5, "Cargando sonidos..."),
                (0.7, "Cargando fuentes..."),
                (0.9, "Preparando juego..."),
                (1.0, "Listo!"),
            ]
            for i, (threshold, _) in enumerate(loading_stages):
                if elapsed > threshold * 1.5:
                    loading_progress = min(threshold, 1.0)
                    loading_stage = i
            # Progreso mostrado suavizado: la barra llena de forma continua
            # en vez de saltar bruscamente entre etapas.
            loading_display += (loading_progress - loading_display) * min(1.0, dt_sec * 6.0)
            if loading_progress >= 1.0 and loading_display > 0.99:
                loading_display = 1.0
                loading_done = True
            if loading_done:
                settings["player_name"] = loaded_username
                saved = load_user_settings(loaded_username)
                if saved:
                    for k, v in saved.items():
                        if k in settings:
                            settings[k] = v
                    input_handler = InputHandler(settings["keys"])
                state = S_MENU
                selected = 0
                anim.reset()
                if is_special_user(loaded_username):
                    show_love_note = True
                if HAS_NETWORK and net_client and not net_client.connected and not server_started:
                    server_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")
                    if _server_launch_allowed(server_path):
                        try:
                            server_process = subprocess.Popen(
                                [sys.executable, server_path],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                            )
                            server_started = True
                            _time.sleep(0.3)
                        except Exception:
                            pass
                    def _auto_connect():
                        if net_client.connected:
                            return
                        if net_client.connect(multi_ip, int(multi_port)):
                            net_client.set_name(settings.get("player_name", "Jugador"))
                    threading.Thread(target=_auto_connect, daemon=True).start()

        if state == S_MULTI_CONNECTING:
            if IS_WEB:
                # WebSocket: la conexion se inicia una vez y se consulta
                # cada frame (onopen/onerror llegan entre frames).
                if multi_connect_thread is None:
                    if net_client.connected:
                        net_client.disconnect()
                    net_client.connect(multi_ip, int(multi_port))
                    multi_connect_thread = True
                    multi_connect_result = None
                else:
                    if net_client.connected:
                        ok = True
                    elif getattr(net_client, "connect_failed", False):
                        ok = False
                    else:
                        ok = None
                    if ok is not None:
                        multi_connect_thread = None
                        if ok:
                            net_client.set_name(settings.get("player_name", "Jugador"))
                            if multi_connect_action == "host":
                                room_chat.clear()
                                net_client.create_room()
                                state = S_MULTI_WAIT
                                multi_status_msg = "Esperando jugador..."
                            elif multi_connect_action == "join":
                                room_chat.clear()
                                net_client.join_room(multi_input_text.strip())
                                state = S_MULTI_WAIT
                                multi_status_msg = "Esperando confirmacion..."
                            elif multi_connect_action == "news":
                                net_client.get_news()
                                state = S_NEWS
                                news_status = "Cargando noticias..."
                                multi_status_msg = ""
                            anim.reset()
                        else:
                            if multi_connect_action == "news":
                                state = S_MENU
                                multi_status_msg = ""
                                news_list = load_local_news()
                                news_status = "No se pudo conectar al servidor"
                            else:
                                state = S_MULTI_MENU if multi_connect_action == "host" else S_MULTI_JOIN
                                multi_status_msg = f"No se pudo conectar a {multi_ip}:{multi_port}"
                            selected = 0
                            anim.reset()
            else:
                if multi_connect_thread is None or not multi_connect_thread.is_alive():
                    if multi_connect_result is not None:
                        ok = multi_connect_result[0] if isinstance(multi_connect_result, list) else multi_connect_result
                        multi_connect_result = None
                        multi_connect_thread = None
                        if ok:
                            net_client.set_name(settings.get("player_name", "Jugador"))
                            if multi_connect_action == "host":
                                room_chat.clear()
                                net_client.create_room()
                                state = S_MULTI_WAIT
                                multi_status_msg = "Esperando jugador..."
                            elif multi_connect_action == "join":
                                room_chat.clear()
                                net_client.join_room(multi_input_text.strip())
                                state = S_MULTI_WAIT
                                multi_status_msg = "Esperando confirmacion..."
                            elif multi_connect_action == "news":
                                net_client.get_news()
                                state = S_NEWS
                                news_status = "Cargando noticias..."
                                multi_status_msg = ""
                            anim.reset()
                        else:
                            if multi_connect_action == "news":
                                state = S_MENU
                                multi_status_msg = ""
                                news_list = load_local_news()
                                news_status = "No se pudo conectar al servidor"
                            else:
                                state = S_MULTI_MENU if multi_connect_action == "host" else S_MULTI_JOIN
                                multi_status_msg = f"No se pudo conectar a {multi_ip}:{multi_port}"
                            selected = 0
                            anim.reset()
                    else:
                        connect_result = [None]
                        def _do_connect():
                            if net_client.connected:
                                net_client.disconnect()
                            connect_result[0] = net_client.connect(multi_ip, int(multi_port))
                        multi_connect_thread = threading.Thread(target=_do_connect, daemon=True)
                        multi_connect_result = connect_result
                        multi_connect_thread.start()

        if net_client and net_client.connected:
            for msg in net_client.get_messages():
                mtype = msg.get("type")
                if mtype == "game_start":
                    game = Tetris(settings)
                    combo = ComboSystem()
                    effects = ScreenEffects()
                    game.combo = combo
                    game.effects = effects
                    input_handler = InputHandler(settings["keys"])
                    # Un rival por cada otro jugador de la sala (hasta 7 en
                    # una sala llena de 8). multi_opponent (singular) queda
                    # sin usar aca; es solo para el modo VS contra la CPU.
                    multi_opponents = {}
                    for p in net_client.other_players():
                        multi_opponents[p["id"]] = {
                            "name": p.get("name", "Rival"),
                            "grid": None, "score": 0, "lines": 0, "level": 1,
                            "current_piece": None, "next_queue": [], "combo": 0,
                            "game_over": False,
                        }
                    mp_hud = {
                        "last_opp_update_t": None, "ping_ms": 0,
                        "player_lines_sent_ref": 0, "sent_total": 0, "received_total": 0,
                        "popup_text": "", "popup_color": (255, 220, 80), "popup_timer": 0.0,
                    }
                    state = S_MULTI_PLAY
                    selected = 0
                    anim.time = 0
                elif mtype == "opponent_update":
                    pid = msg.get("player_id")
                    opp = multi_opponents.get(pid)
                    if opp is None:
                        # Rival del que aun no teniamos entrada (por ejemplo
                        # si el roster llego incompleto); se crea al vuelo.
                        opp = {
                            "name": msg.get("name", "Rival"),
                            "grid": None, "score": 0, "lines": 0, "level": 1,
                            "current_piece": None, "next_queue": [], "combo": 0,
                            "game_over": False,
                        }
                        multi_opponents[pid] = opp
                    opp["name"] = msg.get("name", opp["name"])
                    opp["grid"] = msg.get("grid")
                    opp["score"] = msg.get("score", 0)
                    opp["lines"] = msg.get("lines", 0)
                    opp["level"] = msg.get("level", 1)
                    opp["current_piece"] = msg.get("current_piece")
                    opp["next_queue"] = msg.get("next_queue", [])
                    opp["combo"] = msg.get("combo", 0)
                    opp["game_over"] = msg.get("game_over", False)
                    # No hay un mensaje de ping/pong en el protocolo, asi que
                    # estimamos la latencia con el intervalo entre updates
                    # de rivales: cuanto mas tarda en llegar el siguiente
                    # paquete, mayor la demora real de red+servidor. Se
                    # suaviza con una media movil para que no salte a los
                    # tirones.
                    now_t = _time.perf_counter()
                    if mp_hud["last_opp_update_t"] is not None:
                        interval_ms = (now_t - mp_hud["last_opp_update_t"]) * 1000.0
                        mp_hud["ping_ms"] += (interval_ms - mp_hud["ping_ms"]) * 0.3
                    mp_hud["last_opp_update_t"] = now_t
                elif mtype == "receive_garbage":
                    if game and not game.game_over:
                        garbage_lines = msg.get("lines", 0)
                        for _ in range(garbage_lines):
                            gap = random.randint(0, COLS - 1)
                            new_row = [(120, 120, 120) if c != gap else None for c in range(COLS)]
                            game.grid.pop(0)
                            game.grid.append(new_row)
                        effects.shake(2 + garbage_lines * 2)
                        effects.flash((200, 80, 80), 30 + garbage_lines * 10)
                        if state == S_MULTI_PLAY and garbage_lines > 0:
                            mp_hud["received_total"] += garbage_lines
                            atk_from = msg.get("from_player", "")
                            mp_hud["popup_text"] = f"ATAQUE DE {atk_from} x{garbage_lines}" if atk_from else f"ATAQUE RECIBIDO x{garbage_lines}"
                            mp_hud["popup_color"] = (255, 90, 90)
                            mp_hud["popup_timer"] = 1.6
                elif mtype == "player_left":
                    left_name = msg.get("name", "Un jugador")
                    multi_status_msg = f"{left_name} se desconecto"
                    for opp in multi_opponents.values():
                        if opp.get("name") == left_name:
                            opp["game_over"] = True
                elif mtype == "game_result":
                    result = msg.get("result", "")
                    if result == "win":
                        game.game_over = True
                        multi_status_msg = "GANASTE LA PARTIDA!" if len(multi_opponents) > 1 else f"GANASTE! Derrotaste a {msg.get('opponent', '')}"
                        apply_match_result(mp_stats, settings.get("player_name", "Jugador"), True)
                    elif result == "lose":
                        game.game_over = True
                        multi_status_msg = "PERDISTE! Fuiste eliminado" if len(multi_opponents) > 1 else f"PERDISTE! {msg.get('opponent', '')} gano"
                        apply_match_result(mp_stats, settings.get("player_name", "Jugador"), False)
                elif mtype == "ready_update":
                    ready_n = sum(1 for p in msg.get("players", []) if p.get("ready"))
                    total_n = len(msg.get("players", [])) or 1
                    multi_status_msg = f"Listos: {ready_n}/{total_n}"
                elif mtype == "error":
                    multi_status_msg = msg.get("message", "Error")
                elif mtype == "chat":
                    sender = msg.get("from", "Rival")
                    if sender != settings.get("player_name", ""):
                        room_chat.add_message(sender, msg.get("message", ""), mine=False)
                        sfx.play("menu_click")
                elif mtype == "global_chat":
                    sender = msg.get("from", "Jugador")
                    mine = sender == settings.get("player_name", "")
                    if not mine:
                        global_chat.add_message(sender, msg.get("message", ""), mine=False)
                        sfx.play("menu_click")
                elif mtype == "global_chat_history":
                    global_chat.messages = []
                    my_name = settings.get("player_name", "")
                    for entry in msg.get("messages", []):
                        sender = entry.get("from", "Jugador")
                        global_chat.messages.append((sender, entry.get("message", ""), _time.time(), sender == my_name))
                    global_chat.messages = global_chat.messages[-global_chat.max_lines:]
                elif mtype == "news_list":
                    news_list = msg.get("news", [])
                    news_status = ""
                    news_selected = min(news_selected, max(len(news_list) - 1, 0))
                elif mtype == "news_error":
                    news_status = msg.get("message", "Error de noticias")

        if state == S_PLAYING:
            game.lock_delay = settings["lock"]
            sfx.volume = settings["volume"] / 100.0
            sfx.enabled = settings.get("sfx", True)
            if not paused:
                if not game.game_over:
                    input_handler.update(dt, game, settings["das"], settings["arr"])
                game.update(dt)
                combo.update(dt)
                effects.update(dt)
                game.floating_text.update(dt)
                game.line_clear_anim.update(dt)
            if game.game_over and not getattr(game, "_score_saved", False):
                # Guardar el mejor puntaje del jugador apenas termina la
                # partida, para que quede aunque cierre el juego enseguida.
                game._score_saved = True
                game._is_new_record = False
                if loaded_username and game.score > settings.get("best_score", 0):
                    settings["best_score"] = game.score
                    game._is_new_record = True
                    save_user_settings(loaded_username, settings)

        if state == S_MULTI_PLAY and net_client:
            game.lock_delay = settings["lock"]
            if not game.game_over:
                input_handler.update(dt, game, settings["das"], settings["arr"])
            game.update(dt)
            combo.update(dt)
            effects.update(dt)
            game.floating_text.update(dt)
            game.line_clear_anim.update(dt)
            # El servidor es quien decide y envia la basura real
            # ("receive_garbage"), pero podemos estimar localmente cuantas
            # lineas le mandamos al rival con la misma regla (n-1 lineas
            # limpiadas de una), solo para mostrarlo en el HUD.
            if game.lines > mp_hud["player_lines_sent_ref"]:
                delta_lines = game.lines - mp_hud["player_lines_sent_ref"]
                mp_hud["player_lines_sent_ref"] = game.lines
                attack_sent = max(0, delta_lines - 1)
                if attack_sent > 0:
                    mp_hud["sent_total"] += attack_sent
                    mp_hud["popup_text"] = f"ATAQUE ENVIADO x{attack_sent}"
                    mp_hud["popup_color"] = (100, 220, 140)
                    mp_hud["popup_timer"] = 1.6
            if mp_hud["popup_timer"] > 0:
                mp_hud["popup_timer"] = max(0.0, mp_hud["popup_timer"] - dt / 1000.0)
            if not game.game_over:
                net_client.send_board_update(
                    game.grid, game.score, game.lines, game.level,
                    game.current, game.next_queue, combo.count, False
                )
            if game.game_over and not hasattr(game, '_sent_game_over'):
                game._sent_game_over = True
                net_client.send_board_update(
                    game.grid, game.score, game.lines, game.level,
                    game.current, game.next_queue, combo.count, True
                )

        if state == S_VS_PLAY:
            game.lock_delay = settings["lock"]
            if not game.game_over:
                input_handler.update(dt, game, settings["das"], settings["arr"])
            game.update(dt)
            combo.update(dt)
            effects.update(dt)
            game.floating_text.update(dt)
            game.line_clear_anim.update(dt)
            if bot and not game.game_over:
                bot.update(dt, bot_game)
            multi_opponent["grid"] = bot_game.grid
            multi_opponent["score"] = bot_game.score
            multi_opponent["lines"] = bot_game.lines
            multi_opponent["level"] = bot_game.level
            multi_opponent["game_over"] = bot_game.game_over
            # Basura: quien limpia lineas manda filas al rival (n-1).
            if not game.game_over and not bot_game.game_over:
                if game.lines > vs_state["player_lines"]:
                    delta = game.lines - vs_state["player_lines"]
                    vs_state["player_lines"] = game.lines
                    garbage = max(0, delta - 1)
                    for _ in range(garbage):
                        gap = random.randint(0, COLS - 1)
                        new_row = [(120, 120, 120) if c != gap else None for c in range(COLS)]
                        bot_game.grid.pop(0)
                        bot_game.grid.append(new_row)
                if bot_game.lines > vs_state["bot_lines"]:
                    delta = bot_game.lines - vs_state["bot_lines"]
                    vs_state["bot_lines"] = bot_game.lines
                    garbage = max(0, delta - 1)
                    if garbage:
                        for _ in range(garbage):
                            gap = random.randint(0, COLS - 1)
                            new_row = [(120, 120, 120) if c != gap else None for c in range(COLS)]
                            game.grid.pop(0)
                            game.grid.append(new_row)
                        effects.shake(2 + garbage * 2)
                        effects.flash((200, 80, 80), 30 + garbage * 10)
            if not vs_state["ended"]:
                if game.game_over and bot_game.game_over:
                    vs_state["ended"] = True
                    multi_status_msg = "EMPATE"
                elif game.game_over:
                    vs_state["ended"] = True
                    multi_status_msg = "PERDISTE! La CPU gano"
                elif bot_game.game_over:
                    vs_state["ended"] = True
                    game.game_over = True
                    multi_status_msg = "GANASTE! Derrotaste a la CPU"

        if state == S_MUSIC_PLAYING:
            game.lock_delay = settings["lock"]
            if not game.game_over and not music_won:
                input_handler.update(dt, game, settings["das"], settings["arr"])
                game.update(dt)
                combo.update(dt)
                effects.update(dt)
                game.floating_text.update(dt)
                game.line_clear_anim.update(dt)
            music_player._volume = settings["volume"]
            # Only touch the mixer when the volume actually changes: calling
            # set_volume() every single frame can block on some audio
            # drivers and was causing constant lag in this mode.
            if getattr(music_player, "_last_set_volume", None) != settings["volume"]:
                music_player._last_set_volume = settings["volume"]
                try:
                    pygame.mixer.music.set_volume(settings["volume"] / 100.0)
                except Exception:
                    pass
            sfx.volume = settings["volume"] / 100.0
            sfx.enabled = settings.get("sfx", True)
            if music_difficulty == 1 and not game.game_over:
                lyric_text = music_player.get_current_lyric()
                if lyric_text and not hasattr(game, '_last_lyric'):
                    game._last_lyric = ""
                if lyric_text != getattr(game, '_last_lyric', ""):
                    game._last_lyric = lyric_text
            if game.game_over:
                music_player.stop()
            elif not music_won and music_player.is_finished():
                # La cancion termino sola y el jugador seguia vivo: victoria.
                music_won = True
                music_won_time = anim.time
                music_player.stop()
                sfx.play("rank_up")

        upd_ms += ((_time.perf_counter() - t_upd0) * 1000 - upd_ms) * 0.05
        t_draw0 = _time.perf_counter()

        # Sin screen.fill: todos los estados dibujan un fondo de ventana
        # completa (gradiente cacheado), asi que el fill era una pasada
        # full-screen extra que en ventanas grandes costaba varios ms.

        # Piezas cayendo de fondo en menus (activable/desactivable en
        # Personalizacion); None cuando esta apagado para no dibujarlas.
        _menu_blocks = falling_blocks_bg if settings.get("menu_falling_blocks", True) else None

        # Info de la insignia de perfil (esquina superior derecha, como en
        # tetr.io). Solo se pasa a las pantallas del arbol de menus; None
        # en el resto para que draw_top_bar no dibuje nada.
        _profile_info = None
        if loaded_username:
            _avatar_colors = settings.get("piece_colors") or COLORS
            _avatar_idx = settings.get("avatar_color_idx", 0) % len(_avatar_colors)
            _profile_info = {
                "username": loaded_username,
                "color": _avatar_colors[_avatar_idx],
                "avatar_path": settings.get("avatar_path", ""),
                "avatar_data": settings.get("avatar_data", ""),
                "is_admin": is_admin,
                "is_playtester": is_special_user(loaded_username),
                "active": state == S_PROFILE,
            }

        if state == S_LOGIN:
            draw_gradient_bg(screen, win_w, win_h, 0)
            falling_blocks_bg.draw(screen, win_w, win_h)
            particles_bg.draw(screen, win_w, win_h, anim.time)
            cx = win_w // 2

            # Entrada escalonada: logo baja con rebote, subtitulo/caja/hints
            # aparecen con fade. Los campos hacen fade en el sitio para no
            # descuadrar los hitboxes del mouse del handler de eventos.
            t_logo = min(anim.time * 1.8, 1.0)
            t_sub = max(0.0, min((anim.time - 0.15) * 2.0, 1.0))
            t_box = max(0.0, min((anim.time - 0.3) * 1.8, 1.0))
            t_hint = max(0.0, min((anim.time - 0.55) * 2.0, 1.0))

            logo_y = int(win_h * 0.08) + int((1 - ease_out_back(t_logo)) * 60)
            fade_logo = ease_out_cubic(t_logo)
            _, logo_h = draw_tepy_logo(screen, cx, logo_y, font_title, anim.time, int(fade_logo * 255), align="center")

            subtitle = font_sub.render("Tetris Online", True, (120, 130, 150))
            subtitle.set_alpha(int(ease_out_cubic(t_sub) * 255))
            screen.blit(subtitle, (cx - subtitle.get_width() // 2, logo_y + logo_h + 5))

            version_text = font_sub.render("VERSION 0.1", True, (80, 85, 100))
            version_text.set_alpha(int(ease_out_cubic(t_sub) * 200))
            screen.blit(version_text, (cx - version_text.get_width() // 2, logo_y + logo_h + 5 + subtitle.get_height() + 2))

            box_w = int(win_w * 0.35)
            box_h = int(win_h * 0.5)
            box_x = cx - box_w // 2
            box_y = int(win_h * 0.28)

            fade_box = ease_out_cubic(t_box)
            box_surf = get_gradient(box_w, box_h, (18, 20, 32), (25, 28, 42), 230)
            box_surf.set_alpha(int(230 * fade_box))
            screen.blit(box_surf, (box_x, box_y))
            # Borde con fade por color (los draw.rect no respetan alpha
            # sobre la ventana): de casi-negro al azul del borde.
            border_col = tuple(int(12 + (c - 12) * fade_box) for c in (60, 70, 100))
            pygame.draw.rect(screen, border_col, (box_x, box_y, box_w, box_h), 1, border_radius=8)

            title_text = "CREAR CUENTA" if login_is_register else "INICIAR SESION"
            title_surf = font_option.render(title_text, True, WHITE)
            title_surf.set_alpha(int(fade_box * 255))
            screen.blit(title_surf, (cx - title_surf.get_width() // 2, box_y + 20))

            field_y = box_y + 60
            field_w = box_w - 40
            field_x = box_x + 20
            field_h = int(win_h * 0.05)

            for i, (label, value) in enumerate([("USUARIO", login_user), ("CONTRASENA", login_pass)]):
                t_field = max(0.0, min((anim.time - 0.4 - i * 0.12) * 2.2, 1.0))
                fade_f = ease_out_cubic(t_field)
                fy = field_y + i * (field_h + 35)
                lbl = font_sub.render(label, True, (140, 150, 170))
                lbl.set_alpha(int(fade_f * 255))
                screen.blit(lbl, (field_x, fy))
                field_bg = tuple(int(c * fade_f) for c in (30, 32, 45))
                pygame.draw.rect(screen, field_bg, (field_x, fy + lbl.get_height() + 4, field_w, field_h), border_radius=4)
                if login_field == i:
                    # Borde del campo enfocado con pulso suave
                    focus_pulse = (math.sin(anim.time * 4) + 1) / 2
                    border_col = tuple(int(lo + (hi - lo) * focus_pulse) for lo, hi in zip((100, 160, 220), (150, 210, 255)))
                else:
                    border_col = (50, 55, 70)
                border_col = tuple(int(c * fade_f) for c in border_col)
                pygame.draw.rect(screen, border_col, (field_x, fy + lbl.get_height() + 4, field_w, field_h), 2, border_radius=4)
                display_val = "*" * len(value) if i == 1 else value
                if login_field == i and anim.time % 1.0 < 0.5:
                    display_val += "_"
                val_surf = font_sub.render(display_val, True, WHITE)
                val_surf.set_alpha(int(fade_f * 255))
                screen.blit(val_surf, (field_x + 8, fy + lbl.get_height() + 4 + field_h // 2 - val_surf.get_height() // 2))

            # Botones: ENTRAR/CREAR (clic o ENTER) y alternar de modo
            # (clic o F1). Misma animacion de entrada que el resto.
            submit_h = int(win_h * 0.05)
            submit_y = field_y + 2 * (field_h + 35) + 10
            t_btn = max(0.0, min((anim.time - 0.55) * 2.0, 1.0))
            fade_btn = ease_out_cubic(t_btn)
            btn_alpha = int(255 * fade_btn)
            if login_is_register:
                sub_label, sub_col, sub_dark = "CREAR CUENTA", (100, 180, 130), (60, 120, 80)
            else:
                sub_label, sub_col, sub_dark = "INICIAR SESION", (100, 160, 220), (60, 100, 150)
            sub_hover = field_x <= mx <= field_x + field_w and submit_y <= my <= submit_y + submit_h
            if sub_hover:
                sub_col = tuple(min(c + 30, 255) for c in sub_col)
            sub_bg = get_gradient(field_w, submit_h, sub_dark, tuple(max(c - 15, 0) for c in sub_dark), 230)
            sub_bg.set_alpha(btn_alpha)
            screen.blit(sub_bg, (field_x, submit_y))
            pygame.draw.rect(screen, tuple(int(c * fade_btn) for c in sub_col), (field_x, submit_y, field_w, submit_h), 2, border_radius=6)
            sub_text = font_sub.render(sub_label, True, WHITE)
            sub_text.set_alpha(btn_alpha)
            screen.blit(sub_text, (field_x + field_w // 2 - sub_text.get_width() // 2, submit_y + submit_h // 2 - sub_text.get_height() // 2))

            # Boton de alternar modo (texto con subrayado al hover)
            toggle_h = int(win_h * 0.04)
            toggle_y = box_y + box_h - toggle_h - 18
            tog_hover = field_x <= mx <= field_x + field_w and toggle_y <= my <= toggle_y + toggle_h
            toggle_text = "No tengo cuenta, crear una" if not login_is_register else "Ya tengo cuenta, iniciar sesion"
            tog_col = (180, 200, 255) if tog_hover else (140, 160, 200)
            tog_surf = font_sub.render(toggle_text, True, tog_col)
            tog_alpha = int(ease_out_cubic(t_hint) * 255)
            tog_surf.set_alpha(tog_alpha)
            tog_x = field_x + field_w // 2 - tog_surf.get_width() // 2
            screen.blit(tog_surf, (tog_x, toggle_y + toggle_h // 2 - tog_surf.get_height() // 2))
            if tog_hover:
                pygame.draw.line(screen, (180, 200, 255), (tog_x, toggle_y + toggle_h - 4), (tog_x + tog_surf.get_width(), toggle_y + toggle_h - 4), 1)

            if login_error:
                # Mensaje de error con pulso rojo suave
                err_pulse = (math.sin(anim.time * 6) + 1) / 2
                err_col = (255, int(100 - err_pulse * 40), int(100 - err_pulse * 40))
                err_surf = font_sub.render(login_error, True, err_col)
                err_surf.set_alpha(int(fade_box * 255))
                screen.blit(err_surf, (cx - err_surf.get_width() // 2, submit_y + submit_h + 6))

            hint = font_sub.render("ENTER: continuar   F1: alternar   ESC: salir", True, (100, 110, 130))
            hint.set_alpha(int(ease_out_cubic(t_hint) * 255))
            screen.blit(hint, (cx - hint.get_width() // 2, box_y + box_h + 15))

        elif state == S_LOADING:
            draw_gradient_bg(screen, win_w, win_h, 0)
            falling_blocks_bg.draw(screen, win_w, win_h)
            particles_bg.draw(screen, win_w, win_h, anim.time)
            cx = win_w // 2
            cy = win_h // 2

            # Entrada basada en el tiempo desde que empezo la carga
            t_load = max(0.0, min((_time.time() - loading_start_time) * 2.0, 1.0))
            fade_load = ease_out_cubic(t_load)
            fade_alpha = int(fade_load * 255)

            # Logo unificado (mismo que login y menu) con glow y entrada con rebote
            logo_y = cy - 150 + int((1 - ease_out_back(t_load)) * 50)
            _, logo_h = draw_tepy_logo(screen, cx, logo_y, font_title, anim.time, fade_alpha, align="center")

            user_label = font_sub.render(f"Jugando como: {loaded_username}", True, (120, 130, 150))
            user_label.set_alpha(fade_alpha)
            user_y = logo_y + logo_h + 10
            screen.blit(user_label, (cx - user_label.get_width() // 2, user_y))
            if loaded_username == ADMIN_USER:
                admin_badge = font_sub.render("ADMIN", True, (255, 200, 60))
                admin_badge.set_alpha(fade_alpha)
                screen.blit(admin_badge, (cx + user_label.get_width() // 2 + 8, user_y))

            loading_stages = [
                (0.3, "Cargando sprites..."),
                (0.5, "Cargando sonidos..."),
                (0.7, "Cargando fuentes..."),
                (0.9, "Preparando juego..."),
                (1.0, "Listo!"),
            ]

            # Checklist vertical: cada etapa ya completada muestra un check
            # verde, la etapa activa un bloque pulsante, y las pendientes
            # quedan atenuadas. Reemplaza el texto suelto de una sola linea
            # por algo mas legible del progreso real de la carga.
            list_w = int(win_w * 0.34)
            list_x = cx - list_w // 2
            row_h = max(int(win_h * 0.032), 20)
            list_y = user_y + user_label.get_height() + 22
            dots = "." * (int(anim.time * 2) % 4)
            for i, (_, label) in enumerate(loading_stages):
                ry = list_y + i * row_h
                done = i < loading_stage
                active = i == loading_stage
                if done:
                    dot_col = (90, 220, 140)
                    text_col = (170, 210, 185)
                elif active:
                    pulse_a = (math.sin(anim.time * 6) + 1) / 2
                    dot_col = (int(120 + pulse_a * 100), int(180 + pulse_a * 60), 255)
                    text_col = (220, 225, 240)
                else:
                    dot_col = (60, 64, 78)
                    text_col = (90, 95, 110)
                dot_col_a = tuple(int(c * fade_load) for c in dot_col)
                text_col_a = tuple(int(c * fade_load) for c in text_col)

                mark_cx = list_x + 7
                mark_cy = ry + row_h // 2
                if done:
                    # Marca de check simple con dos lineas.
                    pygame.draw.circle(screen, dot_col_a, (mark_cx, mark_cy), 7)
                    pygame.draw.line(screen, (15, 25, 20), (mark_cx - 3, mark_cy), (mark_cx - 1, mark_cy + 3), 2)
                    pygame.draw.line(screen, (15, 25, 20), (mark_cx - 1, mark_cy + 3), (mark_cx + 4, mark_cy - 3), 2)
                elif active:
                    r = 6 + int(pulse_a * 2)
                    pygame.draw.circle(screen, dot_col_a, (mark_cx, mark_cy), r)
                else:
                    pygame.draw.circle(screen, dot_col_a, (mark_cx, mark_cy), 4)

                label_text = (label + dots) if active else label
                label_surf = font_sub.render(label_text, True, text_col_a)
                screen.blit(label_surf, (list_x + 22, mark_cy - label_surf.get_height() // 2))

            list_bottom = list_y + len(loading_stages) * row_h

            # Barra con progreso suavizado + brillo deslizante
            bar_w = int(win_w * 0.4)
            bar_h = 12
            bar_x = cx - bar_w // 2
            bar_y = list_bottom + 16
            bg_bar = tuple(int(c * fade_load) for c in (30, 32, 45))
            pygame.draw.rect(screen, bg_bar, (bar_x, bar_y, bar_w, bar_h), border_radius=6)
            fill_w = int(bar_w * min(loading_display, 1.0))
            if fill_w > 0:
                bar_surf = get_gradient(fill_w, bar_h, (60, 140, 220), (100, 200, 140))
                bar_surf.set_alpha(fade_alpha)
                screen.blit(bar_surf, (bar_x, bar_y))
                # Brillo que recorre la parte llena de la barra
                shine_w = 30
                shine_off = int((anim.time * 90) % (fill_w + shine_w)) - shine_w
                shine = get_gradient(shine_w, bar_h, (255, 255, 255), (255, 255, 255), 70)
                old_clip = screen.get_clip()
                screen.set_clip((bar_x, bar_y, fill_w, bar_h))
                screen.blit(shine, (bar_x + shine_off, bar_y))
                screen.set_clip(old_clip)
            pygame.draw.rect(screen, (int(60 * fade_load), int(70 * fade_load), int(100 * fade_load)), (bar_x, bar_y, bar_w, bar_h), 1, border_radius=6)

            # Porcentaje con el progreso suavizado
            pct = int(min(loading_display, 1.0) * 100)
            pct_surf = font_sub.render(f"{pct}%", True, (160, 170, 190))
            pct_surf.set_alpha(fade_alpha)
            screen.blit(pct_surf, (cx - pct_surf.get_width() // 2, bar_y + bar_h + 8))

            # Spinner tematico: un mini-tetromino girando en vez de puntos
            # genericos orbitando.
            spin_size = 22
            spin_step = int(anim.time * 1.6) % 4
            spin_pattern = _LOGO_ICON_PATTERN
            for _ in range(spin_step):
                spin_pattern = _rotate_pattern_cw(spin_pattern)
            spin_surf = pygame.Surface((spin_size, spin_size), pygame.SRCALPHA)
            draw_block_icon(spin_surf, 0, 0, spin_size, spin_pattern, (110, 210, 160))
            spin_surf.set_alpha(fade_alpha)
            screen.blit(spin_surf, (cx - spin_size // 2, bar_y + bar_h + 8 + pct_surf.get_height() + 10))

        elif state == S_MENU:
            draw_menu_with_chat(screen, win_w, win_h, selected, font_title, font_sub, anim, particles_bg, menu_items, global_chat, net_client, _menu_blocks, profile=_profile_info)
            if show_love_note:
                love_note_btn = draw_love_note(screen, win_w, win_h, font_game, font_sub, anim.time)

        elif state == S_PLAY_MENU:
            draw_submenu(screen, win_w, win_h, selected, font_title, font_sub, anim, particles_bg, "HOME > JUGAR", "MODO DE JUEGO", play_items, PALETTE_PLAY, _menu_blocks, profile=_profile_info, scroll=menu_scroll)

        elif state == S_OPTIONS:
            draw_submenu(screen, win_w, win_h, selected, font_title, font_sub, anim, particles_bg, "HOME", "OPCIONES", options_items, PALETTE_OPTIONS, _menu_blocks, profile=_profile_info, scroll=menu_scroll)

        elif state == S_CONTROLS:
            draw_controls(screen, win_w, win_h, selected, settings, waiting_for_key, font_title, font_sub, anim, particles_bg, _menu_blocks)

        elif state == S_GAME:
            draw_submenu(screen, win_w, win_h, selected, font_title, font_sub, anim, particles_bg, "HOME > OPCIONES", "JUEGO", game_items, PALETTE_GAME, _menu_blocks, profile=_profile_info, scroll=menu_scroll)

        elif state == S_PERSONAL:
            draw_submenu(screen, win_w, win_h, selected, font_title, font_sub, anim, particles_bg, "HOME > OPCIONES", "PERSONALIZACION", personal_items, PALETTE_OPTIONS, _menu_blocks, profile=_profile_info, scroll=menu_scroll)
            if personal_status_msg:
                cx = win_w // 2
                status_color = (100, 255, 150) if "cargado" in personal_status_msg.lower() else (255, 150, 100)
                status_surf = font_sub.render(personal_status_msg, True, status_color)
                n_items = len(personal_items)
                btn_h = max(int(win_h * 0.1), 44)
                btn_y_end = int(win_h * 0.28) + n_items * (btn_h + max(int(win_h * 0.015), 6))
                screen.blit(status_surf, (cx - status_surf.get_width() // 2, btn_y_end + 10))

        elif state == S_SOUND:
            draw_submenu(screen, win_w, win_h, selected, font_title, font_sub, anim, particles_bg, "HOME > OPCIONES", "SONIDO", sound_items, PALETTE_SOUND, _menu_blocks, profile=_profile_info, scroll=menu_scroll)

        elif state == S_PROFILE:
            draw_submenu(screen, win_w, win_h, selected, font_title, font_sub, anim, particles_bg, "HOME", "PERFIL", profile_items, PALETTE_OPTIONS, _menu_blocks, profile=_profile_info, scroll=menu_scroll)

            p_stats = get_player_rank(mp_stats, loaded_username)
            wins = p_stats.get("wins", 0)
            losses = p_stats.get("losses", 0)
            rating = p_stats.get("rating", 1000)

            card_w = int(win_w * 0.3)
            card_h = int(win_h * 0.18)
            card_x = win_w - card_w - int(win_w * 0.05)
            card_y = int(win_h * 0.22)
            banner_col = BANNER_COLORS[settings.get("banner_color_idx", 0) % len(BANNER_COLORS)]
            card_surf = pygame.Surface((card_w, card_h), pygame.SRCALPHA)
            pygame.draw.rect(card_surf, (25, 28, 40, 210), (0, 0, card_w, card_h), border_radius=12)
            banner_h = int(card_h * 0.4)
            pygame.draw.rect(card_surf, (*banner_col, 255), (0, 0, card_w, banner_h), border_top_left_radius=12, border_top_right_radius=12)
            screen.blit(card_surf, (card_x, card_y))
            pygame.draw.rect(screen, (60, 66, 90), (card_x, card_y, card_w, card_h), 2, border_radius=12)

            av_r = int(card_h * 0.24)
            av_cx = card_x + av_r + 20
            av_cy = card_y + banner_h
            pygame.draw.circle(screen, (25, 28, 40), (av_cx, av_cy), av_r + 3)
            photo = get_avatar_circle_data(_profile_info.get("avatar_data"), av_r * 2) or get_avatar_circle(_profile_info["avatar_path"], av_r * 2)
            if photo is not None:
                screen.blit(photo, (av_cx - av_r, av_cy - av_r))
            else:
                pygame.draw.circle(screen, _profile_info["color"], (av_cx, av_cy), av_r)
                big_initial = get_font(int(av_r * 1.1), True).render((loaded_username[:1] or "?").upper(), True, (20, 20, 25))
                screen.blit(big_initial, (av_cx - big_initial.get_width() // 2, av_cy - big_initial.get_height() // 2))

            uname_surf = get_font(max(int(win_h * 0.026), 15), True).render(loaded_username, True, WHITE)
            screen.blit(uname_surf, (av_cx + av_r + 16, av_cy - uname_surf.get_height() - 2))

            p_wins_disp = wins
            unlocked_titles_disp = get_unlocked_titles(loaded_username, p_wins_disp)
            cur_title = unlocked_titles_disp[settings.get("profile_title_idx", 0) % len(unlocked_titles_disp)] if unlocked_titles_disp else "Sin titulo"
            title_color = (195, 140, 255) if cur_title == EXCLUSIVE_TITLES.get((loaded_username or "").strip().lower()) else (255, 210, 110)
            title_surf = font_sub.render(cur_title, True, title_color)
            screen.blit(title_surf, (av_cx + av_r + 16, av_cy + 4))
            if is_admin:
                admin_surf = font_sub.render("ADMIN", True, (255, 120, 120))
                screen.blit(admin_surf, (av_cx + av_r + 16 + title_surf.get_width() + 10, av_cy + 4))

            stats_y = card_y + card_h - 26
            stats_txt = font_sub.render(f"Rating: {rating}   |   {wins}V - {losses}D   |   Mejor: {settings.get('best_score', 0)} pts", True, (170, 178, 200))
            screen.blit(stats_txt, (card_x + 20, stats_y))

            bio_txt = settings.get("bio", "").strip()
            if bio_txt:
                bio_surf = font_sub.render(bio_txt if len(bio_txt) <= 70 else bio_txt[:67] + "...", True, (150, 160, 185))
                screen.blit(bio_surf, (av_cx + av_r + 16, av_cy + 4 + title_surf.get_height() + 6))

            if profile_status_msg:
                pcx = win_w // 2
                pstat_color = (255, 150, 100) if ("no " in profile_status_msg.lower() or "sin " in profile_status_msg.lower()) else (100, 255, 150)
                pstat_surf = font_sub.render(profile_status_msg, True, pstat_color)
                n_pitems = len(profile_items)
                pbtn_h = max(int(win_h * 0.1), 44)
                pbtn_y_end = int(win_h * 0.28) + n_pitems * (pbtn_h + max(int(win_h * 0.015), 6))
                screen.blit(pstat_surf, (pcx - pstat_surf.get_width() // 2, pbtn_y_end + 10))

        elif state == S_COLOR_CUSTOM:
            draw_color_custom(screen, win_w, win_h, color_selected_piece, settings["piece_colors"], font_title, font_sub, anim, particles_bg, _menu_blocks)

        elif state == S_MULTI_MENU:
            draw_submenu(screen, win_w, win_h, selected, font_title, font_sub, anim, particles_bg, "HOME > JUGAR", "MULTIJUGADOR", multi_menu_items, PALETTE_PLAY, _menu_blocks, scroll=menu_scroll)
            if multi_status_msg:
                cx = win_w // 2
                err_color = (255, 100, 100) if "no" in multi_status_msg.lower() else (200, 200, 100)
                status_surf = font_sub.render(multi_status_msg, True, err_color)
                n_items = len(multi_menu_items)
                btn_h = max(int(win_h * 0.1), 44)
                btn_y_end = int(win_h * 0.28) + n_items * (btn_h + max(int(win_h * 0.015), 6))
                screen.blit(status_surf, (cx - status_surf.get_width() // 2, btn_y_end + 10))
            if show_multi_warning:
                multi_warning_btn = draw_multi_warning(screen, win_w, win_h, font_game, font_sub, anim.time, multi_warning_time)

        elif state == S_MULTI_CONNECTING:
            draw_gradient_bg(screen, win_w, win_h, settings["bg_style"], settings.get("custom_bg_path"))
            falling_blocks_bg.draw(screen, win_w, win_h)
            cx = win_w // 2
            cy = win_h // 2

            panel_w = int(win_w * 0.4)
            panel_h = int(win_h * 0.3)
            panel_x = cx - panel_w // 2
            panel_y = cy - panel_h // 2
            panel_surf = get_gradient(panel_w, panel_h, (18, 22, 35), (25, 30, 45), 220)
            screen.blit(panel_surf, (panel_x, panel_y))
            pygame.draw.rect(screen, (60, 70, 100), (panel_x, panel_y, panel_w, panel_h), 1, border_radius=10)

            conn_text = font_title.render("CONECTANDO", True, WHITE)
            screen.blit(conn_text, (cx - conn_text.get_width() // 2, panel_y + 25))

            action_text = "Creando sala..." if multi_connect_action == "host" else "Uniendose a sala..."
            act_surf = font_sub.render(action_text, True, (180, 190, 210))
            screen.blit(act_surf, (cx - act_surf.get_width() // 2, panel_y + 25 + conn_text.get_height() + 8))

            # Spinner tematico (mini-tetromino girando) en vez de puntos sueltos.
            spin_size = 24
            spin_step = int(anim.time * 1.6) % 4
            spin_pattern = _LOGO_ICON_PATTERN
            for _ in range(spin_step):
                spin_pattern = _rotate_pattern_cw(spin_pattern)
            spin_surf = pygame.Surface((spin_size, spin_size), pygame.SRCALPHA)
            draw_block_icon(spin_surf, 0, 0, spin_size, spin_pattern, (120, 200, 150))
            screen.blit(spin_surf, (cx - spin_size // 2, cy - spin_size // 2))

            bar_w = int(panel_w * 0.6)
            bar_h = 6
            bar_x = cx - bar_w // 2
            bar_y = cy + 40
            pygame.draw.rect(screen, (30, 32, 45), (bar_x, bar_y, bar_w, bar_h), border_radius=3)
            anim_offset = (anim.time * 200) % bar_w
            glow_w = 40
            glow_surf = get_gradient(glow_w, bar_h, (100, 180, 255), (60, 120, 200), 200)
            screen.blit(glow_surf, (int(bar_x + anim_offset) % bar_w, bar_y))

            hint = font_sub.render("ESC para cancelar", True, (100, 110, 130))
            screen.blit(hint, (cx - hint.get_width() // 2, panel_y + panel_h - 30))

        elif state == S_MULTI_JOIN:
            draw_gradient_bg(screen, win_w, win_h, settings["bg_style"], settings.get("custom_bg_path"))
            cx = win_w // 2
            cy = win_h // 2

            title = font_title.render("UNIRSE A SALA", True, WHITE)
            screen.blit(title, (cx - title.get_width() // 2, int(win_h * 0.1)))

            panel_w = int(win_w * 0.45)
            panel_h = int(win_h * 0.45)
            panel_x = cx - panel_w // 2
            panel_y = int(win_h * 0.18)
            panel_surf = get_gradient(panel_w, panel_h, (18, 22, 35), (25, 30, 45), 220)
            screen.blit(panel_surf, (panel_x, panel_y))
            pygame.draw.rect(screen, (60, 70, 100), (panel_x, panel_y, panel_w, panel_h), 1, border_radius=10)

            inner_cx = panel_x + panel_w // 2

            code_hint = font_sub.render("INGRESA EL CODIGO DE LA SALA", True, (140, 150, 170))
            screen.blit(code_hint, (inner_cx - code_hint.get_width() // 2, panel_y + 25))

            code_bg_w = int(panel_w * 0.65)
            code_bg_h = int(panel_h * 0.2)
            code_bg_x = inner_cx - code_bg_w // 2
            code_bg_y = panel_y + 25 + code_hint.get_height() + 15
            pygame.draw.rect(screen, (12, 15, 25), (code_bg_x, code_bg_y, code_bg_w, code_bg_h), border_radius=8)
            pulse = (math.sin(anim.time * 2) + 1) / 2
            border_col = (int(80 + pulse * 40), int(160 + pulse * 40), int(200 + pulse * 40))
            pygame.draw.rect(screen, border_col, (code_bg_x, code_bg_y, code_bg_w, code_bg_h), 2, border_radius=8)

            cursor = "_" if anim.time % 1.0 < 0.5 else ""
            code_text = font_title.render(multi_input_text + cursor, True, WHITE)
            screen.blit(code_text, (inner_cx - code_text.get_width() // 2, code_bg_y + code_bg_h // 2 - code_text.get_height() // 2))

            hint1 = font_sub.render("Escribe el codigo de 6 caracteres", True, (120, 130, 150))
            screen.blit(hint1, (inner_cx - hint1.get_width() // 2, code_bg_y + code_bg_h + 12))
            hint2 = font_sub.render("y presiona ENTER para unirte", True, (120, 130, 150))
            screen.blit(hint2, (inner_cx - hint2.get_width() // 2, code_bg_y + code_bg_h + 12 + hint1.get_height() + 2))

            pygame.draw.line(screen, (50, 55, 70), (panel_x + 30, panel_y + panel_h - 60), (panel_x + panel_w - 30, panel_y + panel_h - 60))

            if multi_status_msg:
                err_color = (255, 100, 100) if "error" in multi_status_msg.lower() or "no se" in multi_status_msg.lower() or "no encontro" in multi_status_msg.lower() else (200, 200, 100)
                status_surf = font_sub.render(multi_status_msg, True, err_color)
                screen.blit(status_surf, (inner_cx - status_surf.get_width() // 2, panel_y + panel_h - 48))

            btn_w = int(win_w * 0.2)
            btn_h = max(int(win_h * 0.05), 32)
            btn_x = cx - btn_w // 2
            btn_y = panel_y + panel_h + 15

            connect_color = (60, 130, 180) if selected == 0 else (40, 90, 130)
            pygame.draw.rect(screen, connect_color, (btn_x, btn_y, btn_w, btn_h), border_radius=6)
            connect_text = font_sub.render("CONECTAR", True, WHITE)
            screen.blit(connect_text, (cx - connect_text.get_width() // 2, btn_y + btn_h // 2 - connect_text.get_height() // 2))

            back_btn_y = btn_y + btn_h + 10
            back_color = (80, 50, 50) if selected == 1 else (60, 35, 35)
            pygame.draw.rect(screen, back_color, (btn_x, back_btn_y, btn_w, btn_h), border_radius=6)
            back_text = font_sub.render("VOLVER", True, WHITE)
            screen.blit(back_text, (cx - back_text.get_width() // 2, back_btn_y + btn_h // 2 - back_text.get_height() // 2))

            if mx >= btn_x and mx <= btn_x + btn_w:
                if btn_y <= my <= btn_y + btn_h:
                    selected = 0
                elif back_btn_y <= my <= back_btn_y + btn_h:
                    selected = 1

        elif state == S_MULTI_WAIT:
            multi_layout = Layout(win_w, win_h)
            draw_gradient_bg(screen, win_w, win_h, settings["bg_style"], settings.get("custom_bg_path"))
            cx = win_w // 2
            cy = win_h // 2

            n_room_players = len(net_client.players) if net_client and net_client.players else 1
            title = font_title.render(f"SALA DE ESPERA ({n_room_players}/{MAX_ROOM_PLAYERS})", True, WHITE)
            screen.blit(title, (cx - title.get_width() // 2, int(win_h * 0.1)))

            panel_w = int(win_w * 0.5)
            panel_h = int(win_h * 0.55)
            panel_x = cx - panel_w // 2
            panel_y = int(win_h * 0.18)
            panel_surf = get_gradient(panel_w, panel_h, (18, 22, 35), (25, 30, 45), 220)
            screen.blit(panel_surf, (panel_x, panel_y))
            pygame.draw.rect(screen, (60, 70, 100), (panel_x, panel_y, panel_w, panel_h), 1, border_radius=10)

            if net_client and net_client.room_id:
                inner_cx = panel_x + panel_w // 2

                code_hint = font_sub.render("CODIGO DE SALA - compartilo para que se unan hasta 8", True, (140, 150, 170))
                screen.blit(code_hint, (inner_cx - code_hint.get_width() // 2, panel_y + 16))

                code_bg_w = int(panel_w * 0.55)
                code_bg_h = int(panel_h * 0.13)
                code_bg_x = inner_cx - code_bg_w // 2
                code_bg_y = panel_y + 16 + code_hint.get_height() + 8
                pygame.draw.rect(screen, (12, 15, 25), (code_bg_x, code_bg_y, code_bg_w, code_bg_h), border_radius=8)
                pulse = (math.sin(anim.time * 2) + 1) / 2
                border_color = (int(80 + pulse * 40), int(180 + pulse * 40), int(120 + pulse * 40))
                pygame.draw.rect(screen, border_color, (code_bg_x, code_bg_y, code_bg_w, code_bg_h), 2, border_radius=8)

                code_text = font_option.render(net_client.room_id, True, (220, 255, 200))
                screen.blit(code_text, (inner_cx - code_text.get_width() // 2, code_bg_y + code_bg_h // 2 - code_text.get_height() // 2))

                list_top = code_bg_y + code_bg_h + 14
                pygame.draw.line(screen, (50, 55, 70), (panel_x + 30, list_top), (panel_x + panel_w - 30, list_top))

                players_hdr = font_sub.render("JUGADORES", True, (140, 150, 170))
                screen.blit(players_hdr, (panel_x + 30, list_top + 10))

                row_h = max(int(win_h * 0.045), 26)
                rows_top = list_top + 10 + players_hdr.get_height() + 8
                my_name = settings.get("player_name", "")
                roster = net_client.players or [{"id": net_client.player_id, "name": my_name, "ready": False}]
                for i, p in enumerate(roster[:MAX_ROOM_PLAYERS]):
                    ry = rows_top + i * row_h
                    if ry + row_h > panel_y + panel_h - 60:
                        break
                    is_me = p.get("id") == net_client.player_id
                    p_name = p.get("name", "Jugador") + ("  (vos)" if is_me else "")
                    name_col = (140, 210, 255) if is_me else (210, 215, 230)
                    name_surf = font_sub.render(p_name, True, name_col)
                    screen.blit(name_surf, (panel_x + 40, ry))
                    is_ready = p.get("ready", False)
                    tag_txt = "LISTO" if is_ready else "ESPERANDO"
                    tag_col = (100, 230, 150) if is_ready else (200, 170, 90)
                    tag_surf = font_sub.render(tag_txt, True, tag_col)
                    screen.blit(tag_surf, (panel_x + panel_w - 40 - tag_surf.get_width(), ry))

                if n_room_players < 2:
                    dots = "." * (int(anim.time * 2) % 4)
                    hint_txt = f"Esperando a mas jugadores{dots}"
                else:
                    dots = "." * (int(anim.time * 2) % 4)
                    hint_txt = f"Listo cuando todos confirmen{dots}"
                waiting_text = font_sub.render(hint_txt, True, (200, 200, 100))
                screen.blit(waiting_text, (inner_cx - waiting_text.get_width() // 2, panel_y + panel_h - 42))

                # Chat mientras esperamos rivales
                room_chat.draw(screen, multi_layout, font_sub, anim.time)

            if multi_status_msg:
                status_surf = font_sub.render(multi_status_msg, True, (255, 100, 100))
                screen.blit(status_surf, (cx - status_surf.get_width() // 2, panel_y + panel_h + 15))

            btn_w = int(win_w * 0.2)
            btn_h = max(int(win_h * 0.05), 32)
            btn_x = cx - btn_w // 2
            btn_y = panel_y + panel_h + 45

            ready_color = (60, 160, 100) if selected == 0 else (40, 100, 60)
            pygame.draw.rect(screen, ready_color, (btn_x, btn_y, btn_w, btn_h), border_radius=6)
            ready_text = font_sub.render("LISTO", True, WHITE)
            screen.blit(ready_text, (cx - ready_text.get_width() // 2, btn_y + btn_h // 2 - ready_text.get_height() // 2))

            leave_btn_y = btn_y + btn_h + 10
            leave_color = (120, 50, 50) if selected == 1 else (80, 35, 35)
            pygame.draw.rect(screen, leave_color, (btn_x, leave_btn_y, btn_w, btn_h), border_radius=6)
            leave_text = font_sub.render("SALIR", True, WHITE)
            screen.blit(leave_text, (cx - leave_text.get_width() // 2, leave_btn_y + btn_h // 2 - leave_text.get_height() // 2))

            if mx >= btn_x and mx <= btn_x + btn_w:
                if btn_y <= my <= btn_y + btn_h:
                    selected = 0
                elif leave_btn_y <= my <= leave_btn_y + btn_h:
                    selected = 1

        elif state in (S_MULTI_PLAY, S_VS_PLAY):
            multi_layout = Layout(win_w, win_h)
            font_multi = get_font(max(int(min(win_w, win_h) * 0.04), 16), True)
            multi_font = get_font(multi_layout.font_size, True)
            multi_small = get_font(multi_layout.small_font_size)

            game._grid_x = multi_layout.grid_x
            game._grid_y = multi_layout.grid_y
            game._grid_w = multi_layout.grid_w
            game._grid_h = multi_layout.grid_h
            game._grid_cell = multi_layout.cell
            game._grid_cx = multi_layout.grid_x + multi_layout.grid_w // 2
            game._grid_cy = multi_layout.grid_y + multi_layout.grid_h // 2

            draw_gradient_bg(screen, win_w, win_h, settings["bg_style"], settings.get("custom_bg_path"))

            pygame.draw.rect(screen, (12, 14, 18), (multi_layout.grid_x, multi_layout.grid_y, multi_layout.grid_w, multi_layout.grid_h))
            screen.blit(get_grid_lines(multi_layout.grid_x, multi_layout.grid_y, multi_layout.grid_w, multi_layout.grid_h, multi_layout.cell), (multi_layout.grid_x, multi_layout.grid_y))

            gw2, gh2 = multi_layout.grid_w + 6, multi_layout.grid_h + 6
            gg_key2 = (gw2, gh2)
            if not hasattr(draw_gradient_bg, '_grid_glow_cache') or draw_gradient_bg._grid_glow_key != gg_key2:
                gs2 = pygame.Surface((gw2, gh2), pygame.SRCALPHA)
                pygame.draw.rect(gs2, (60, 80, 120, 20), (0, 0, gw2, gh2), border_radius=4)
                draw_gradient_bg._grid_glow_cache = gs2
                draw_gradient_bg._grid_glow_key = gg_key2
            screen.blit(draw_gradient_bg._grid_glow_cache, (multi_layout.grid_x - 3, multi_layout.grid_y - 3))
            pygame.draw.rect(screen, (55, 60, 75), (multi_layout.grid_x, multi_layout.grid_y, multi_layout.grid_w, multi_layout.grid_h), 2, border_radius=2)

            draw_grid(screen, game.grid, multi_layout, game.lock_squash)
            game.line_clear_anim.draw(screen, multi_layout.grid_x, multi_layout.grid_y, multi_layout.grid_w, multi_layout.cell)
            if not game.game_over:
                if settings["ghost"]:
                    draw_ghost(screen, game, multi_layout)
                draw_piece(screen, game.current, multi_layout)

            game.floating_text.draw(screen)

            draw_hold(screen, game, multi_layout, multi_small, anim.time)
            draw_next_queue(screen, game, multi_layout, multi_small, anim.time)
            draw_score_panel(screen, game, multi_layout, multi_font, multi_small, anim.time)
            draw_combo_hud(screen, combo, multi_layout, multi_font, multi_small, anim.time)

            # El area de paneles de rivales tiene que quedar SIEMPRE despues
            # de tu caja de NEXT (nunca encima): el limite izquierdo real es
            # el borde derecho de esa caja, no un offset fijo desde la
            # grilla, que era lo que hacia que se superpusieran.
            opp_min_x = multi_layout.next_x + multi_layout.next_w + multi_layout.cell
            available_w = max(win_w - 10 - opp_min_x, 40)
            available_h = multi_layout.grid_h

            if state == S_VS_PLAY:
                opp_list = [multi_opponent]
            else:
                opp_list = list(multi_opponents.values())
                if not opp_list:
                    opp_list = [{"name": "Esperando datos...", "grid": None, "score": 0, "lines": 0, "game_over": False}]

            n_opp = len(opp_list)
            gap = 8
            if n_opp <= 1:
                p_cols = 1
            elif n_opp <= 6:
                p_cols = 2
            else:
                p_cols = 3
            p_rows = math.ceil(n_opp / p_cols)
            panel_w = max((available_w - (p_cols - 1) * gap) // p_cols, 30)
            panel_h = max((available_h - (p_rows - 1) * gap) // p_rows, 40)
            name_h = multi_small.get_height() + 4
            cell_avail_h = max(panel_h - name_h - 16, 10)
            opp_cell = max(min(panel_w // COLS, cell_avail_h // ROWS), 2)
            board_w = COLS * opp_cell
            board_h = ROWS * opp_cell

            for i, opp in enumerate(opp_list):
                col_i = i % p_cols
                row_i = i // p_cols
                px = opp_min_x + col_i * (panel_w + gap)
                py = multi_layout.grid_y + row_i * (panel_h + gap)
                bx = px + (panel_w - board_w) // 2
                by = py + name_h + 2

                panel_surf = get_gradient(panel_w, panel_h, (14, 16, 24), (20, 24, 36), 200)
                screen.blit(panel_surf, (px, py))
                border_col = (90, 40, 40) if opp.get("game_over") else (50, 55, 70)
                pygame.draw.rect(screen, border_col, (px, py, panel_w, panel_h), 1, border_radius=4)

                opp_name_txt = opp.get("name", "Rival")
                if len(opp_name_txt) > 12:
                    opp_name_txt = opp_name_txt[:11] + "."
                opp_name = multi_small.render(opp_name_txt, True, (180, 190, 210))
                screen.blit(opp_name, (px + panel_w // 2 - opp_name.get_width() // 2, py + 2))

                pygame.draw.rect(screen, (10, 12, 18), (bx, by, board_w, board_h))
                if opp.get("grid"):
                    for ry, row in enumerate(opp["grid"]):
                        for rx, cell in enumerate(row):
                            if cell:
                                cxp = bx + rx * opp_cell
                                cyp = by + ry * opp_cell
                                color = tuple(cell) if isinstance(cell, (list, tuple)) else (150, 150, 150)
                                pygame.draw.rect(screen, color, (cxp, cyp, max(opp_cell - 1, 1), max(opp_cell - 1, 1)))

                if panel_h > name_h + board_h + 14:
                    stat_txt = multi_small.render(f"{opp.get('score', 0):,} | {opp.get('lines', 0)}L", True, (190, 195, 205))
                    screen.blit(stat_txt, (px + 4, by + board_h + 2))

                if opp.get("game_over"):
                    overlay = get_overlay(board_w, board_h, (40, 0, 0), 130)
                    screen.blit(overlay, (bx, by))
                    ko_font = font_multi if panel_w > 90 else multi_small
                    opp_over = ko_font.render("KO", True, (255, 90, 90))
                    screen.blit(opp_over, (bx + board_w // 2 - opp_over.get_width() // 2, by + board_h // 2 - opp_over.get_height() // 2))

            # Variables que el HUD de ping/rango de mas abajo usa como ancla;
            # antes apuntaban al unico panel de rival, ahora al area completa.
            opp_x, opp_y, opp_w = opp_min_x, multi_layout.grid_y, available_w

            if state == S_MULTI_PLAY:
                # --- Ping estimado -------------------------------------
                # No existe un mensaje ping/pong en el protocolo online;
                # se estima con el intervalo entre actualizaciones del
                # rival (mp_hud["ping_ms"], calculado al recibir cada
                # "opponent_update"). Es una aproximacion de la demora de
                # sincronizacion, no un RTT de socket puro.
                ping_val = mp_hud["ping_ms"]
                if ping_val < 120:
                    ping_col = (110, 230, 140)
                elif ping_val < 300:
                    ping_col = (255, 200, 80)
                else:
                    ping_col = (255, 100, 100)
                ping_label = "--" if mp_hud["last_opp_update_t"] is None else f"{int(ping_val)} ms"
                ping_surf = multi_small.render(f"PING {ping_label}", True, ping_col)
                ping_box_w = ping_surf.get_width() + 16
                ping_box = get_overlay(ping_box_w, ping_surf.get_height() + 8, (0, 0, 0), 150)
                screen.blit(ping_box, (opp_x + opp_w - ping_box_w, opp_y - 25 - ping_surf.get_height() - 4))
                screen.blit(ping_surf, (opp_x + opp_w - ping_box_w + 8, opp_y - 25 - ping_surf.get_height()))

                # --- Ranking del jugador --------------------------------
                rk = get_player_rank(mp_stats, settings.get("player_name", "Jugador"))
                rank_txt = f"Rating {rk.get('rating', 1000)}  ({rk.get('wins', 0)}V-{rk.get('losses', 0)}D)"
                rank_surf = multi_small.render(rank_txt, True, (180, 200, 255))
                rank_box = get_overlay(rank_surf.get_width() + 16, rank_surf.get_height() + 8, (0, 0, 0), 150)
                screen.blit(rank_box, (multi_layout.grid_x, multi_layout.grid_y - rank_surf.get_height() - 16))
                screen.blit(rank_surf, (multi_layout.grid_x + 8, multi_layout.grid_y - rank_surf.get_height() - 12))

                # --- Contador de ataques enviados / recibidos -----------
                atk_txt = f"Ataque: {mp_hud['sent_total']} enviado / {mp_hud['received_total']} recibido"
                atk_surf = multi_small.render(atk_txt, True, (210, 210, 220))
                atk_box = get_overlay(atk_surf.get_width() + 16, atk_surf.get_height() + 8, (0, 0, 0), 150)
                atk_bx = multi_layout.grid_x + multi_layout.grid_w // 2 - (atk_surf.get_width() + 16) // 2
                screen.blit(atk_box, (atk_bx, multi_layout.grid_y - atk_surf.get_height() - 16))
                screen.blit(atk_surf, (atk_bx + 8, multi_layout.grid_y - atk_surf.get_height() - 12))

                # --- Popup de ataque (aparece un momento y se desvanece)
                if mp_hud["popup_timer"] > 0:
                    fade_a = min(1.0, mp_hud["popup_timer"] / 0.4) if mp_hud["popup_timer"] < 0.4 else 1.0
                    popup_surf = font_multi.render(mp_hud["popup_text"], True, mp_hud["popup_color"])
                    popup_surf.set_alpha(int(255 * fade_a))
                    pop_y = multi_layout.grid_y + multi_layout.grid_h // 2 - popup_surf.get_height() // 2
                    screen.blit(popup_surf, (multi_layout.grid_x + multi_layout.grid_w // 2 - popup_surf.get_width() // 2, pop_y))

            if gamepad.connected:
                draw_gamepad_hud(screen, gamepad, multi_layout.grid_x + multi_layout.grid_w // 2, multi_layout.grid_y + multi_layout.grid_h + int(multi_layout.cell * 2.45) + 14, multi_small)

            if game.game_over:
                go_btns = draw_game_over(screen, font_game, font_sub, multi_layout, game.score, show_best=False)
                if multi_status_msg:
                    res_surf = font_multi.render(multi_status_msg, True, (255, 220, 80))
                    screen.blit(res_surf, (win_w // 2 - res_surf.get_width() // 2, int(win_h * 0.3)))
            else:
                go_btns = None

            effects.draw(screen, win_w, win_h)

        elif state == S_PLAYING:
            layout = Layout(win_w, win_h)
            font_layout = get_font(layout.font_size, True)
            small_font_layout = get_font(layout.small_font_size)

            game._grid_x = layout.grid_x
            game._grid_y = layout.grid_y
            game._grid_w = layout.grid_w
            game._grid_h = layout.grid_h
            game._grid_cell = layout.cell
            game._grid_cx = layout.grid_x + layout.grid_w // 2
            game._grid_cy = layout.grid_y + layout.grid_h // 2

            draw_gradient_bg(screen, win_w, win_h, settings["bg_style"], settings.get("custom_bg_path"))

            pygame.draw.rect(screen, (12, 14, 18), (layout.grid_x, layout.grid_y, layout.grid_w, layout.grid_h))
            screen.blit(get_grid_lines(layout.grid_x, layout.grid_y, layout.grid_w, layout.grid_h, layout.cell), (layout.grid_x, layout.grid_y))

            gw, gh = layout.grid_w + 6, layout.grid_h + 6
            grid_glow_key = (gw, gh)
            if not hasattr(draw_gradient_bg, '_grid_glow_cache') or draw_gradient_bg._grid_glow_key != grid_glow_key:
                gs = pygame.Surface((gw, gh), pygame.SRCALPHA)
                pygame.draw.rect(gs, (60, 80, 120, 20), (0, 0, gw, gh), border_radius=4)
                draw_gradient_bg._grid_glow_cache = gs
                draw_gradient_bg._grid_glow_key = grid_glow_key
            screen.blit(draw_gradient_bg._grid_glow_cache, (layout.grid_x - 3, layout.grid_y - 3))
            pygame.draw.rect(screen, (55, 60, 75), (layout.grid_x, layout.grid_y, layout.grid_w, layout.grid_h), 2, border_radius=2)

            draw_grid(screen, game.grid, layout, game.lock_squash)
            game.line_clear_anim.draw(screen, layout.grid_x, layout.grid_y, layout.grid_w, layout.cell)
            if not game.game_over:
                if settings["ghost"]:
                    draw_ghost(screen, game, layout)
                draw_piece(screen, game.current, layout)

            game.floating_text.draw(screen)

            draw_hold(screen, game, layout, small_font_layout, anim.time)
            draw_next_queue(screen, game, layout, small_font_layout, anim.time)
            draw_score_panel(screen, game, layout, font_layout, small_font_layout, anim.time)
            draw_speed_bar(screen, game, layout, anim.time)
            draw_combo_hud(screen, combo, layout, font_layout, small_font_layout, anim.time)
            if gamepad.connected:
                draw_gamepad_hud(screen, gamepad, layout.grid_x + layout.grid_w // 2, layout.grid_y + layout.grid_h + int(layout.cell * 2.45) + 14, small_font_layout)

            if game.game_over:
                go_btns = draw_game_over(screen, font_game, font_sub, layout, game.score, settings.get("best_score", 0), getattr(game, "_is_new_record", False))
            else:
                go_btns = None

            effects.draw(screen, win_w, win_h)

            if paused:
                pause_btn_rects = draw_pause_screen(screen, font_game, font_sub, layout, anim.time, pause_selected)

        elif state == S_MUSIC_MENU:
            draw_submenu(screen, win_w, win_h, selected, font_title, font_sub, anim, particles_bg, "HOME > JUGAR", "MODO MUSICA", music_menu_items, PALETTE_PLAY, _menu_blocks, scroll=menu_scroll)
            if music_status_msg:
                cx = win_w // 2
                status_color = (100, 255, 150) if "Cargado" in music_status_msg or "Letra" in music_status_msg else (255, 150, 100)
                status_surf = font_sub.render(music_status_msg, True, status_color)
                n_items = len(music_menu_items)
                btn_h = max(int(win_h * 0.1), 44)
                btn_y_end = int(win_h * 0.28) + n_items * (btn_h + max(int(win_h * 0.015), 6))
                screen.blit(status_surf, (cx - status_surf.get_width() // 2, btn_y_end + 10))
            if music_file_name:
                cx = win_w // 2
                file_info = font_sub.render(f"Archivo: {music_file_name}", True, (140, 180, 220))
                n_items = len(music_menu_items)
                btn_h = max(int(win_h * 0.1), 44)
                btn_y_end = int(win_h * 0.28) + n_items * (btn_h + max(int(win_h * 0.015), 6))
                screen.blit(file_info, (cx - file_info.get_width() // 2, btn_y_end + 35))
            if music_lrc_name:
                cx = win_w // 2
                lrc_info = font_sub.render(f"Letra: {music_lrc_name}", True, (140, 180, 160))
                n_items = len(music_menu_items)
                btn_h = max(int(win_h * 0.1), 44)
                btn_y_end = int(win_h * 0.28) + n_items * (btn_h + max(int(win_h * 0.015), 6))
                screen.blit(lrc_info, (cx - lrc_info.get_width() // 2, btn_y_end + 55))

        elif state == S_MUSIC_DIFF:
            draw_submenu(screen, win_w, win_h, selected, font_title, font_sub, anim, particles_bg, "HOME > JUGAR > MUSICA", "DIFICULTAD", music_diff_items, PALETTE_PLAY, _menu_blocks, scroll=menu_scroll)

        elif state == S_MUSIC_YT:
            draw_gradient_bg(screen, win_w, win_h, settings["bg_style"], settings.get("custom_bg_path"))
            cx = win_w // 2
            panel_w = int(win_w * 0.5)
            panel_h = int(win_h * 0.45)
            panel_x = cx - panel_w // 2
            panel_y = int(win_h * 0.15)
            panel_surf = get_gradient(panel_w, panel_h, (18, 22, 35), (25, 30, 45), 220)
            screen.blit(panel_surf, (panel_x, panel_y))
            pygame.draw.rect(screen, (60, 70, 100), (panel_x, panel_y, panel_w, panel_h), 1, border_radius=10)

            yt_title = font_title.render("DESCARGAR DE YOUTUBE", True, WHITE)
            screen.blit(yt_title, (cx - yt_title.get_width() // 2, panel_y + 20))

            url_label = font_sub.render("URL:", True, (140, 150, 170))
            screen.blit(url_label, (panel_x + 20, panel_y + 70))

            input_w = panel_w - 40
            input_h = 40
            input_x = panel_x + 20
            input_y = panel_y + 100
            pygame.draw.rect(screen, (12, 15, 25), (input_x, input_y, input_w, input_h), border_radius=6)
            border_col = (100, 180, 255) if not music_yt_downloading else (100, 100, 100)
            pygame.draw.rect(screen, border_col, (input_x, input_y, input_w, input_h), 2, border_radius=6)

            display_url = music_yt_url
            url_surf = font_sub.render(display_url, True, (220, 220, 240))
            max_text_w = input_w - 16
            if url_surf.get_width() > max_text_w:
                display_url = display_url[-40:]
                url_surf = font_sub.render("..." + display_url, True, (220, 220, 240))
            screen.blit(url_surf, (input_x + 8, input_y + input_h // 2 - url_surf.get_height() // 2))

            if int(anim.time * 2) % 2 == 0 and not music_yt_downloading:
                cursor_x = input_x + 8 + url_surf.get_width() + 2
                pygame.draw.line(screen, WHITE, (cursor_x, input_y + 8), (cursor_x, input_y + input_h - 8), 2)

            hint = font_sub.render("Ctrl+V para pegar, ENTER para descargar", True, (100, 110, 130))
            screen.blit(hint, (cx - hint.get_width() // 2, input_y + input_h + 8))

            dl_btn_w = int(panel_w * 0.5)
            dl_btn_h = 36
            dl_btn_x = cx - dl_btn_w // 2
            dl_btn_y = input_y + input_h + 35
            dl_color = (80, 160, 220) if not music_yt_downloading else (60, 60, 80)
            pygame.draw.rect(screen, dl_color, (dl_btn_x, dl_btn_y, dl_btn_w, dl_btn_h), border_radius=6)
            dl_text = font_sub.render("DESCARGAR", True, WHITE)
            screen.blit(dl_text, (cx - dl_text.get_width() // 2, dl_btn_y + dl_btn_h // 2 - dl_text.get_height() // 2))

            if music_yt_downloading:
                prog_bar_w = dl_btn_w - 20
                prog_bar_h = 4
                prog_bar_x = dl_btn_x + 10
                prog_bar_y = dl_btn_y + dl_btn_h + 8
                pygame.draw.rect(screen, (30, 32, 45), (prog_bar_x, prog_bar_y, prog_bar_w, prog_bar_h), border_radius=2)
                anim_offset = (anim.time * 150) % prog_bar_w
                glow_w = 30
                pygame.draw.rect(screen, (100, 180, 255), (int(prog_bar_x + anim_offset) % prog_bar_w, prog_bar_y, glow_w, prog_bar_h), border_radius=2)

            if music_yt_status:
                status_color = (100, 255, 150) if "Descargado" in music_yt_status else (255, 180, 100) if "Error" in music_yt_status else (200, 200, 100)
                status_surf = font_sub.render(music_yt_status[:60], True, status_color)
                screen.blit(status_surf, (cx - status_surf.get_width() // 2, dl_btn_y + dl_btn_h + 20))

            back_btn_y = dl_btn_y + dl_btn_h + 45
            back_color = (100, 60, 60) if selected == 1 else (70, 40, 40)
            pygame.draw.rect(screen, back_color, (dl_btn_x, back_btn_y, dl_btn_w, dl_btn_h), border_radius=6)
            back_text = font_sub.render("VOLVER", True, WHITE)
            screen.blit(back_text, (cx - back_text.get_width() // 2, back_btn_y + dl_btn_h // 2 - back_text.get_height() // 2))

        elif state == S_NEWS:
            draw_menu_bg(screen, win_w, win_h, anim, particles_bg, _menu_blocks)
            news_footer_h = max(int(win_h * 0.045), 22)
            news_top_bar_h = max(int(win_h * 0.06), 28)
            draw_top_bar(screen, win_w, news_top_bar_h, font_sub, anim, "HOME")
            _, n_title_y, n_title_h = draw_title(screen, win_w, anim, font_title, "NOTICIAS", news_top_bar_h, win_h)

            news_btn_w = int(win_w * 0.2)
            news_btn_h = max(int(win_h * 0.05), 32)
            news_btn_y = win_h - news_footer_h - news_btn_h - 20
            news_cx = win_w // 2
            card_x = int(win_w * 0.07)
            card_w = int(win_w * 0.86)
            card_h = max(int(win_h * 0.16), 95)
            card_gap = 12
            cards_top = n_title_y + n_title_h + int(win_h * 0.03)
            clip_bottom = (news_btn_y if is_admin else win_h - news_footer_h) - 12
            news_max_scroll = max(0, len(news_list) * (card_h + card_gap) - card_gap - (clip_bottom - cards_top))

            mx, my = pygame.mouse.get_pos()
            screen.set_clip(pygame.Rect(0, cards_top, win_w, clip_bottom - cards_top))
            now_ts = _time.time()
            for i, item in enumerate(news_list):
                cy_base = cards_top + i * (card_h + card_gap) - news_scroll
                if cy_base + card_h < cards_top or cy_base > clip_bottom:
                    continue

                # Entrada escalonada: cada tarjeta aparece con un ligero
                # retraso respecto a la anterior, deslizandose y con fade,
                # igual que los botones del menu principal.
                card_delay = min(i, 8) * 0.05
                card_t = max(0.0, min((anim.time - card_delay) * 3.2, 1.0))
                slide = ease_out_back(card_t)
                cx_off = int((1 - slide) * 120)
                alpha = int(ease_out_cubic(card_t) * 255)
                if alpha <= 0:
                    continue

                sel = (i == news_selected)
                hover = (not sel and card_x <= mx <= card_x + card_w and cy_base <= my <= cy_base + card_h)

                # Elevacion suave al pasar el mouse o al estar seleccionada.
                lift = 0
                if sel:
                    lift = 3
                elif hover:
                    lift = 2
                cy_i = cy_base - lift
                draw_x = card_x + cx_off

                card_surf = pygame.Surface((card_w, card_h), pygame.SRCALPHA)
                if sel:
                    bg = (32, 40, 58)
                elif hover:
                    bg = (26, 32, 47)
                else:
                    bg = (22, 27, 40)
                pygame.draw.rect(card_surf, bg, (0, 0, card_w, card_h), border_radius=8)

                if sel:
                    pulse = (math.sin(anim.time * 3) + 1) / 2
                    border_col = (int(110 + pulse * 40), int(190 + pulse * 30), int(140 + pulse * 30))
                    border_w = 2
                elif hover:
                    border_col = (90, 130, 160)
                    border_w = 1
                else:
                    border_col = (55, 62, 85)
                    border_w = 1
                pygame.draw.rect(card_surf, border_col, (0, 0, card_w, card_h), border_w, border_radius=8)

                if sel:
                    # Barra de acento animada, igual que en los botones del menu.
                    bar_h = card_h - 16
                    bs = pygame.Surface((5, bar_h), pygame.SRCALPHA)
                    pygame.draw.rect(bs, (255, 255, 255, 210), (0, 0, 5, bar_h), border_radius=3)
                    card_surf.blit(bs, (6, 8))

                text_pad = 26 if sel else 16
                thumb = get_news_image_surface(item, min(card_h - 20, 150))
                img_reserve = (thumb.get_width() + 24) if thumb is not None else 0

                ntitle = font_option.render(str(item.get("title", ""))[:60], True, WHITE)
                card_surf.blit(ntitle, (text_pad, 10))

                is_new = (now_ts - float(item.get("date", 0) or 0)) < 86400
                title_end_x = text_pad + ntitle.get_width() + 10
                if is_new:
                    badge_pulse = (math.sin(anim.time * 4 + i) + 1) / 2
                    badge_col = (60, int(170 + badge_pulse * 40), 100)
                    nbadge = font_sub.render("NUEVO", True, WHITE)
                    badge_w = nbadge.get_width() + 14
                    badge_h = nbadge.get_height() + 6
                    pygame.draw.rect(card_surf, badge_col, (title_end_x, 8, badge_w, badge_h), border_radius=badge_h // 2)
                    card_surf.blit(nbadge, (title_end_x + 7, 8 + 3))

                date_str = _time.strftime("%d/%m/%Y %H:%M", _time.localtime(item.get("date", 0)))
                ndate = font_sub.render(f"{date_str}  -  {item.get('author', '')}", True, (120, 130, 150))
                card_surf.blit(ndate, (card_w - img_reserve - ndate.get_width() - 16, 14))
                body_lines = wrap_text(item.get("body", ""), font_sub, card_w - text_pad - 16 - img_reserve, 2)
                for j, line in enumerate(body_lines):
                    nline = font_sub.render(line, True, (200, 205, 220))
                    card_surf.blit(nline, (text_pad, 12 + ntitle.get_height() + 6 + j * (font_sub.get_height() + 2)))

                if thumb is not None:
                    thumb_x = card_w - thumb.get_width() - 12
                    thumb_y = (card_h - thumb.get_height()) // 2
                    pygame.draw.rect(card_surf, (10, 12, 18), (thumb_x - 2, thumb_y - 2, thumb.get_width() + 4, thumb.get_height() + 4), border_radius=4)
                    card_surf.blit(thumb, (thumb_x, thumb_y))
                    pygame.draw.rect(card_surf, (60, 66, 88), (thumb_x - 2, thumb_y - 2, thumb.get_width() + 4, thumb.get_height() + 4), 1, border_radius=4)

                if alpha < 255:
                    card_surf.set_alpha(alpha)
                screen.blit(card_surf, (draw_x, cy_i))
            screen.set_clip(None)

            if news_status:
                st_color = (255, 100, 100) if ("no " in news_status.lower() or "sin " in news_status.lower() or "error" in news_status.lower() or "confirmar" in news_status.lower()) else (200, 200, 100)
                st_surf = font_sub.render(news_status, True, st_color)
                screen.blit(st_surf, (news_cx - st_surf.get_width() // 2, news_btn_y - 30))
            if not news_list and not news_status:
                nempty = font_sub.render("No hay noticias todavia", True, (150, 155, 170))
                screen.blit(nempty, (news_cx - nempty.get_width() // 2, cards_top + 40))
                if is_admin:
                    nhint = font_sub.render("Pulsa P para publicar la primera", True, (110, 115, 130))
                    screen.blit(nhint, (news_cx - nhint.get_width() // 2, cards_top + 40 + nempty.get_height() + 6))

            mouse_down = pygame.mouse.get_pressed()[0]
            if is_admin:
                news_pub_x = news_cx - news_btn_w - 10
                pub_hover = news_pub_x <= mx <= news_pub_x + news_btn_w and news_btn_y <= my <= news_btn_y + news_btn_h
                draw_action_button(screen, news_pub_x, news_btn_y, news_btn_w, news_btn_h,
                                    (50, 130, 85), (70, 180, 110), "PUBLICAR (P)", font_sub,
                                    pub_hover, pub_hover and mouse_down, anim.time)
                news_back_x = news_cx + 10
                back_hover = news_back_x <= mx <= news_back_x + news_btn_w and news_btn_y <= my <= news_btn_y + news_btn_h
                draw_action_button(screen, news_back_x, news_btn_y, news_btn_w, news_btn_h,
                                    (100, 50, 50), (150, 70, 70), "VOLVER", font_sub,
                                    back_hover, back_hover and mouse_down, anim.time)
            else:
                news_back_x = news_cx - news_btn_w // 2
                back_hover = news_back_x <= mx <= news_back_x + news_btn_w and news_btn_y <= my <= news_btn_y + news_btn_h
                draw_action_button(screen, news_back_x, news_btn_y, news_btn_w, news_btn_h,
                                    (100, 50, 50), (150, 70, 70), "VOLVER", font_sub,
                                    back_hover, back_hover and mouse_down, anim.time)

            pygame.draw.rect(screen, (12, 12, 18), (0, win_h - news_footer_h, win_w, news_footer_h))
            pygame.draw.line(screen, (35, 40, 55), (0, win_h - news_footer_h), (win_w, win_h - news_footer_h))
            nfooter = "↑↓ Seleccionar   ENTER/P Publicar   SUPR Borrar (2 veces)   ESC Volver" if is_admin else "↑↓ Seleccionar   ESC Volver"
            nfooter_surf = font_sub.render(nfooter, True, (90, 95, 110))
            screen.blit(nfooter_surf, (20, win_h - news_footer_h // 2 - nfooter_surf.get_height() // 2))

        elif state == S_NEWS_EDIT:
            draw_gradient_bg(screen, win_w, win_h, settings["bg_style"], settings.get("custom_bg_path"))
            edit_cx = win_w // 2
            edit_panel_w = int(win_w * 0.5)
            edit_panel_h = int(win_h * 0.6)
            edit_panel_x = edit_cx - edit_panel_w // 2
            edit_panel_y = int(win_h * 0.13)
            panel_surf = get_gradient(edit_panel_w, edit_panel_h, (18, 22, 35), (25, 30, 45), 220)
            screen.blit(panel_surf, (edit_panel_x, edit_panel_y))
            pygame.draw.rect(screen, (60, 70, 100), (edit_panel_x, edit_panel_y, edit_panel_w, edit_panel_h), 1, border_radius=10)

            etitle = font_title.render("PUBLICAR NOTICIA", True, WHITE)
            screen.blit(etitle, (edit_cx - etitle.get_width() // 2, edit_panel_y + 20))

            edit_field_w = edit_panel_w - 60
            edit_field_h = max(int(win_h * 0.06), 36)
            edit_title_y = edit_panel_y + 90
            elbl1 = font_sub.render("TITULO", True, (140, 150, 170))
            screen.blit(elbl1, (edit_panel_x + 30, edit_title_y - 22))
            ecnt1 = font_sub.render(f"{len(news_edit_title)}/60", True, (100, 110, 130))
            screen.blit(ecnt1, (edit_panel_x + edit_panel_w - 30 - ecnt1.get_width(), edit_title_y - 22))
            pygame.draw.rect(screen, (12, 15, 25), (edit_panel_x + 30, edit_title_y, edit_field_w, edit_field_h), border_radius=6)
            pygame.draw.rect(screen, (100, 180, 255) if news_edit_field == 0 else (60, 70, 100), (edit_panel_x + 30, edit_title_y, edit_field_w, edit_field_h), 2, border_radius=6)
            t_disp = news_edit_title + ("_" if news_edit_field == 0 and anim.time % 1.0 < 0.5 else "")
            etxt = font_sub.render(t_disp, True, (220, 220, 240))
            screen.blit(etxt, (edit_panel_x + 38, edit_title_y + edit_field_h // 2 - etxt.get_height() // 2))

            edit_body_y = edit_title_y + edit_field_h + 70
            body_box_h = edit_field_h * 2
            elbl2 = font_sub.render("CONTENIDO", True, (140, 150, 170))
            screen.blit(elbl2, (edit_panel_x + 30, edit_body_y - 22))
            ecnt2 = font_sub.render(f"{len(news_edit_body)}/300", True, (100, 110, 130))
            screen.blit(ecnt2, (edit_panel_x + edit_panel_w - 30 - ecnt2.get_width(), edit_body_y - 22))
            pygame.draw.rect(screen, (12, 15, 25), (edit_panel_x + 30, edit_body_y, edit_field_w, body_box_h), border_radius=6)
            pygame.draw.rect(screen, (100, 180, 255) if news_edit_field == 1 else (60, 70, 100), (edit_panel_x + 30, edit_body_y, edit_field_w, body_box_h), 2, border_radius=6)
            ebody_lines = wrap_text(news_edit_body, font_sub, edit_field_w - 16, 4)
            if not ebody_lines:
                ebody_lines = ["_" if news_edit_field == 1 and anim.time % 1.0 < 0.5 else ""]
            for j, line in enumerate(ebody_lines):
                eline = font_sub.render(line, True, (220, 220, 240))
                screen.blit(eline, (edit_panel_x + 38, edit_body_y + 10 + j * (font_sub.get_height() + 3)))

            img_row_h = max(int(win_h * 0.05), 32)
            edit_img_y = edit_body_y + body_box_h + 20
            elbl3 = font_sub.render("IMAGEN (opcional)", True, (140, 150, 170))
            screen.blit(elbl3, (edit_panel_x + 30, edit_img_y - 22))
            pygame.draw.rect(screen, (12, 15, 25), (edit_panel_x + 30, edit_img_y, edit_field_w, img_row_h), border_radius=6)
            pygame.draw.rect(screen, (60, 70, 100), (edit_panel_x + 30, edit_img_y, edit_field_w, img_row_h), 2, border_radius=6)
            if news_edit_image:
                img_label = font_sub.render(news_edit_image_name or "Imagen cargada", True, (150, 230, 170))
                screen.blit(img_label, (edit_panel_x + 40, edit_img_y + img_row_h // 2 - img_label.get_height() // 2))
                clear_x = edit_panel_x + 30 + edit_field_w - 30
                clear_surf = font_sub.render("X", True, (255, 130, 130))
                screen.blit(clear_surf, (clear_x, edit_img_y + img_row_h // 2 - clear_surf.get_height() // 2))
            else:
                img_hint = font_sub.render("Click para elegir una imagen (PNG/JPG)...", True, (130, 140, 160))
                screen.blit(img_hint, (edit_panel_x + 40, edit_img_y + img_row_h // 2 - img_hint.get_height() // 2))

            edit_btn_w = int(edit_panel_w * 0.35)
            edit_btn_h = max(int(win_h * 0.05), 32)
            edit_btn_y = edit_img_y + img_row_h + 26
            edit_pub_x = edit_cx - edit_btn_w - 10
            edit_can_x = edit_cx + 10
            pygame.draw.rect(screen, (60, 160, 100), (edit_pub_x, edit_btn_y, edit_btn_w, edit_btn_h), border_radius=6)
            epub = font_sub.render("PUBLICAR", True, WHITE)
            screen.blit(epub, (edit_pub_x + edit_btn_w // 2 - epub.get_width() // 2, edit_btn_y + edit_btn_h // 2 - epub.get_height() // 2))
            pygame.draw.rect(screen, (120, 50, 50), (edit_can_x, edit_btn_y, edit_btn_w, edit_btn_h), border_radius=6)
            ecan = font_sub.render("CANCELAR", True, WHITE)
            screen.blit(ecan, (edit_can_x + edit_btn_w // 2 - ecan.get_width() // 2, edit_btn_y + edit_btn_h // 2 - ecan.get_height() // 2))

            if news_status:
                stc = (255, 100, 100) if ("no " in news_status.lower() or "sin " in news_status.lower()) else (200, 200, 100)
                est = font_sub.render(news_status, True, stc)
                screen.blit(est, (edit_cx - est.get_width() // 2, edit_btn_y + edit_btn_h + 12))

            ehint = font_sub.render("ENTER siguiente/publicar - TAB alterna campo - ESC cancelar", True, (100, 110, 130))
            screen.blit(ehint, (edit_cx - ehint.get_width() // 2, edit_panel_y + edit_panel_h - 30))

        elif state == S_BIO_EDIT:
            draw_gradient_bg(screen, win_w, win_h, settings["bg_style"], settings.get("custom_bg_path"))
            cx = win_w // 2
            box_w = int(win_w * 0.55)
            field_h = max(int(win_h * 0.16), 70)
            field_y = win_h // 2 - field_h // 2
            box_x = cx - box_w // 2

            btitle = font_title.render("DESCRIPCION DE PERFIL", True, WHITE)
            screen.blit(btitle, (cx - btitle.get_width() // 2, field_y - btitle.get_height() - 26))

            cnt = font_sub.render(f"{len(bio_edit_text)}/80", True, (100, 110, 130))
            screen.blit(cnt, (box_x + box_w - cnt.get_width(), field_y - cnt.get_height() - 6))

            pygame.draw.rect(screen, (12, 15, 25), (box_x, field_y, box_w, field_h), border_radius=8)
            pygame.draw.rect(screen, (100, 180, 255), (box_x, field_y, box_w, field_h), 2, border_radius=8)

            # Texto envuelto en varias lineas dentro del cuadro, con cursor
            # parpadeante al final.
            cursor = "_" if anim.time % 1.0 < 0.5 else ""
            words = (bio_edit_text + cursor).split(" ")
            b_lines, cur_line = [], ""
            for w in words:
                test = (cur_line + " " + w).strip()
                if font_sub.size(test)[0] > box_w - 30 and cur_line:
                    b_lines.append(cur_line)
                    cur_line = w
                else:
                    cur_line = test
            if cur_line:
                b_lines.append(cur_line)
            ly = field_y + 14
            for bl in b_lines[:3]:
                lsurf = font_sub.render(bl, True, (225, 228, 240))
                screen.blit(lsurf, (box_x + 15, ly))
                ly += lsurf.get_height() + 4

            btn_w = int(box_w * 0.3)
            btn_h = max(int(win_h * 0.08), 34)
            btn_y = field_y + field_h + int(win_h * 0.03)
            save_x = cx - btn_w - 8
            cancel_x = cx + 8
            mx, my = pygame.mouse.get_pos()
            save_hover = save_x <= mx <= save_x + btn_w and btn_y <= my <= btn_y + btn_h
            cancel_hover = cancel_x <= mx <= cancel_x + btn_w and btn_y <= my <= btn_y + btn_h
            pygame.draw.rect(screen, (60, 130, 90) if save_hover else (40, 90, 65), (save_x, btn_y, btn_w, btn_h), border_radius=8)
            pygame.draw.rect(screen, (120, 220, 150), (save_x, btn_y, btn_w, btn_h), 2, border_radius=8)
            slabel = font_sub.render("Guardar", True, WHITE)
            screen.blit(slabel, (save_x + btn_w // 2 - slabel.get_width() // 2, btn_y + btn_h // 2 - slabel.get_height() // 2))

            pygame.draw.rect(screen, (100, 60, 60) if cancel_hover else (70, 42, 42), (cancel_x, btn_y, btn_w, btn_h), border_radius=8)
            pygame.draw.rect(screen, (200, 120, 120), (cancel_x, btn_y, btn_w, btn_h), 2, border_radius=8)
            clabel = font_sub.render("Cancelar", True, WHITE)
            screen.blit(clabel, (cancel_x + btn_w // 2 - clabel.get_width() // 2, btn_y + btn_h // 2 - clabel.get_height() // 2))

            bhint = font_sub.render("ENTER guarda - ESC cancela", True, (100, 110, 130))
            screen.blit(bhint, (cx - bhint.get_width() // 2, btn_y + btn_h + 14))

        elif state == S_MUSIC_PLAYING:
            music_layout = Layout(win_w, win_h)
            font_music = get_font(music_layout.font_size, True)
            small_font_music = get_font(music_layout.small_font_size)

            game._grid_x = music_layout.grid_x
            game._grid_y = music_layout.grid_y
            game._grid_w = music_layout.grid_w
            game._grid_h = music_layout.grid_h
            game._grid_cell = music_layout.cell
            game._grid_cx = music_layout.grid_x + music_layout.grid_w // 2
            game._grid_cy = music_layout.grid_y + music_layout.grid_h // 2

            # Gradiente SIEMPRE como base: el video opaco pre-oscurecido
            # se blitea encima de su area y las bandas del letterbox
            # muestran el gradiente (antes dependian del screen.fill).
            draw_gradient_bg(screen, win_w, win_h, settings["bg_style"], settings.get("custom_bg_path"))
            music_player.draw_video_bg(screen, win_w, win_h)

            # Con video pre-oscurecido el oscurecimiento ya viene "horneado"
            # en el blit opaco (2 pasadas full-screen -> 0). Sin video,
            # overlay cacheado (antes se creaba un Surface full-screen nuevo
            # en cada frame).
            if not music_player.current_frame:
                screen.blit(get_overlay(win_w, win_h, (0, 0, 0), 80), (0, 0))

            pygame.draw.rect(screen, (12, 14, 18), (music_layout.grid_x, music_layout.grid_y, music_layout.grid_w, music_layout.grid_h))
            screen.blit(get_grid_lines(music_layout.grid_x, music_layout.grid_y, music_layout.grid_w, music_layout.grid_h, music_layout.cell), (music_layout.grid_x, music_layout.grid_y))

            gw, gh = music_layout.grid_w + 6, music_layout.grid_h + 6
            grid_glow_key = (gw, gh)
            if not hasattr(draw_gradient_bg, '_grid_glow_cache') or draw_gradient_bg._grid_glow_key != grid_glow_key:
                gs = pygame.Surface((gw, gh), pygame.SRCALPHA)
                pygame.draw.rect(gs, (60, 80, 120, 20), (0, 0, gw, gh), border_radius=4)
                draw_gradient_bg._grid_glow_cache = gs
                draw_gradient_bg._grid_glow_key = grid_glow_key
            screen.blit(draw_gradient_bg._grid_glow_cache, (music_layout.grid_x - 3, music_layout.grid_y - 3))
            pygame.draw.rect(screen, (55, 60, 75), (music_layout.grid_x, music_layout.grid_y, music_layout.grid_w, music_layout.grid_h), 2, border_radius=2)

            draw_grid(screen, game.grid, music_layout, game.lock_squash)
            game.line_clear_anim.draw(screen, music_layout.grid_x, music_layout.grid_y, music_layout.grid_w, music_layout.cell)
            if not game.game_over:
                if settings["ghost"]:
                    draw_ghost(screen, game, music_layout)
                draw_piece(screen, game.current, music_layout)

            game.floating_text.draw(screen)

            draw_hold(screen, game, music_layout, small_font_music, anim.time)
            draw_next_queue(screen, game, music_layout, small_font_music, anim.time)
            draw_score_panel(screen, game, music_layout, font_music, small_font_music, anim.time)
            draw_speed_bar(screen, game, music_layout, anim.time)
            draw_combo_hud(screen, combo, music_layout, font_music, small_font_music, anim.time)

            if gamepad.connected:
                draw_gamepad_hud(screen, gamepad, music_layout.grid_x + music_layout.grid_w // 2, music_layout.grid_y + music_layout.grid_h + int(music_layout.cell * 2.45) + 14, small_font_music)

            music_player.draw_lyrics(screen, win_w, win_h, get_font(max(int(min(win_w, win_h) * 0.04), 18), True))
            music_player.draw_progress_bar(screen, win_w, win_h)

            diff_label = "FACIL" if music_difficulty == 0 else "DIFICIL"
            diff_color = (100, 200, 100) if music_difficulty == 0 else (255, 100, 100)
            diff_surf = font_sub.render(diff_label, True, diff_color)
            screen.blit(diff_surf, (10, 10))

            if music_player.playing and music_difficulty == 1:
                elapsed = music_player.get_elapsed()
                beat_score = int(combo.count * 10)
                beat_surf = font_sub.render(f"RITMO x{combo.count}", True, (255, 200, 80))
                screen.blit(beat_surf, (10, 35))

            if music_won:
                song_display = os.path.splitext(music_file_name)[0] if music_file_name else ""
                music_win_btns = draw_music_win(screen, font_game, font_sub, music_layout, game.score, game.lines, anim.time,
                                                 won_time=music_won_time, difficulty=music_difficulty,
                                                 best_combo=combo.max_count, song_name=song_display)
            elif game.game_over:
                go_btns = draw_game_over(screen, font_game, font_sub, music_layout, game.score, show_best=False)
            else:
                go_btns = None

            effects.draw(screen, win_w, win_h)

        draw_ms += ((_time.perf_counter() - t_draw0) * 1000 - draw_ms) * 0.05
        t_flip0 = _time.perf_counter()

        # Contador de FPS (F3 para alternar): fps suavizado, ms/frame y
        # reparto por fase (eventos / update / draw / present). El texto
        # cambia cada frame, asi que se renderiza directo y la caja usa
        # anchos redondeados para no contaminar get_text/get_overlay.
        if show_fps:
            inst = 1000.0 / max(dt, 1)
            fps_smooth += (inst - fps_smooth) * 0.05
            if fps_smooth >= 55:
                fps_col = (100, 255, 150)
            elif fps_smooth >= 30:
                fps_col = (255, 200, 80)
            else:
                fps_col = (255, 100, 100)
            fps_text = get_font(14, True).render(
                "%.0f FPS %.1f ms | ev %.1f upd %.1f draw %.1f flip %.1f | %dx%d | %s"
                % (fps_smooth, dt, ev_ms, upd_ms, draw_ms, flip_ms, win_w, win_h,
                   ("pad[%s]: %s" % ("SDL2" if gamepad.using_sdl2 else "RAW", gamepad.joy_name[:16])) if gamepad.connected else "sin control"), True, fps_col)
            pad = 4
            bw = (fps_text.get_width() + 20 + 49) // 50 * 50
            box = get_overlay(bw, fps_text.get_height() + 8, (0, 0, 0), 150)
            px = win_w - bw - 8
            screen.blit(box, (px, pad))
            screen.blit(fps_text, (px + 10, pad + 4))

        fade.draw(screen, win_w, win_h)
        pygame.display.flip()
        flip_ms += ((_time.perf_counter() - t_flip0) * 1000 - flip_ms) * 0.05

        # Yield al event loop: en el navegador esto cede al browser y
        # permite que lleguen eventos (WebSocket, audio, render rAF);
        # en escritorio es un yield inmediato y clock.tick cap el fps.
        await asyncio.sleep(0)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "crash.log"), "w", encoding="utf-8") as f:
                f.write(tb)
        except Exception:
            pass
        try:
            if server_process and server_process.poll() is None:
                server_process.terminate()
                server_process.wait(timeout=2)
        except Exception:
            pass
        pygame.quit()
        print(tb)
