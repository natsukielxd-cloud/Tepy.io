import socket
import threading
import json
import sys
import os
import time
import uuid
import asyncio
import random

try:
    import websockets
    HAS_WS = True
except ImportError:
    HAS_WS = False

HOST = "0.0.0.0"
PORT = 5555
# Puerto del servidor WebSocket para clientes web (build pygbag).
WS_PORT = 5556
# Tope de jugadores por sala (battle royale estilo tetr.io). Antes las
# salas eran estrictamente 1v1 (host + un guest); ahora una sala acepta
# hasta este numero de jugadores conectados simultaneamente.
MAX_ROOM_PLAYERS = 8

NEWS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "news.json")
ACCOUNTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts.json")
MAX_NEWS = 50
# Tope de tamano (en caracteres base64) para la imagen de una noticia.
# Bien por debajo del limite de 2MB por mensaje de recv_msg.
NEWS_IMAGE_MAX_B64 = 900_000

clients = {}
rooms = {}
lock = threading.Lock()
# Historial corto del chat global (no ligado a ninguna sala), para que
# alguien que entra al chat vea algo de contexto en vez de una pantalla
# vacia. Se pierde si se reinicia el server (no se persiste a disco).
global_chat_history = []
MAX_GLOBAL_CHAT = 50


def send_msg(sock, data):
    try:
        msg = json.dumps(data).encode("utf-8")
        length = len(msg).to_bytes(4, "big")
        sock.sendall(length + msg)
    except Exception:
        pass


def recv_msg(sock):
    try:
        raw_len = sock.recv(4)
        if not raw_len or len(raw_len) < 4:
            return None
        msg_len = int.from_bytes(raw_len, "big")
        # Limite de seguridad: un board_update legitimo nunca deberia
        # superar esto. Evita que un cliente malicioso o corrupto pida
        # reservar megas de memoria con un length falso.
        if msg_len > 2_000_000:
            return None
        data = b""
        while len(data) < msg_len:
            chunk = sock.recv(min(msg_len - len(data), 4096))
            if not chunk:
                return None
            data += chunk
        return json.loads(data.decode("utf-8"))
    except (ConnectionResetError, ConnectionAbortedError, OSError):
        return None
    except json.JSONDecodeError:
        return None
    except Exception as e:
        print(f"[!] Error inesperado en recv_msg: {e}")
        return None


def load_news():
    if os.path.exists(NEWS_FILE):
        try:
            with open(NEWS_FILE, "r", encoding="utf-8") as f:
                news = json.load(f)
                if isinstance(news, list):
                    return news
        except Exception:
            pass
    return []


def save_news(news):
    try:
        with open(NEWS_FILE, "w", encoding="utf-8") as f:
            json.dump(news, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[!] Error al guardar noticias: {e}")


def is_admin_player(name):
    # Admin: cualquier cuenta marcada admin:true en accounts.json
    # (el archivo vive junto al server, asi que solo el dueño de la
    # maquina donde corre el server puede publicar).
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            accounts = json.load(f)
    except Exception:
        return False
    acc = accounts.get(name)
    return bool(acc and acc.get("admin"))


def broadcast_all(data):
    with lock:
        targets = list(clients.values())
    for p in targets:
        p.send(data)


class Player:
    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.id = str(uuid.uuid4())[:8]
        self.name = f"Jugador-{self.id[:4]}"
        self.room_id = None
        self.ready = False

    def send(self, data):
        send_msg(self.sock, data)


class WSPlayer:
    """Adaptador de cliente WebSocket con la misma interfaz que Player,
    para que los clientes web compartan salas con los de escritorio.
    El socket WS vive en el event loop del hilo WS, así que send()
    programa el envio en ese loop de forma thread-safe."""

    def __init__(self, ws, loop):
        self.ws = ws
        self.loop = loop
        self.addr = ("ws", 0)
        self.id = str(uuid.uuid4())[:8]
        self.name = f"Jugador-{self.id[:4]}"
        self.room_id = None
        self.ready = False

    def send(self, data):
        try:
            msg = json.dumps(data)
            asyncio.run_coroutine_threadsafe(self.ws.send(msg), self.loop)
        except Exception:
            pass

    def close(self):
        try:
            asyncio.run_coroutine_threadsafe(self.ws.close(), self.loop)
        except Exception:
            pass


class Room:
    """Sala de hasta MAX_ROOM_PLAYERS jugadores (battle royale). `host` es
    solo quien la creo (para mostrar "sala de fulano" en el lobby); el
    juego en si no distingue host/guest, todos son pares una vez arranca.
    """

    def __init__(self, room_id, host):
        self.id = room_id
        self.host = host
        self.players = [host]
        self.ready = {host.id: False}
        self.eliminated = set()  # ids de jugadores que ya perdieron (topped out)
        self.state = "waiting"
        self.last_update = time.time()

    def broadcast(self, data, exclude=None):
        for p in list(self.players):
            if p and p != exclude:
                p.send(data)

    def roster(self):
        return [
            {"id": p.id, "name": p.name, "ready": self.ready.get(p.id, False)}
            for p in self.players
        ]

    def alive_players(self):
        return [p for p in self.players if p.id not in self.eliminated]


def handle_client(player):
    print(f"[+] {player.name} conectado ({player.addr[0]}:{player.addr[1]})")
    try:
        while True:
            data = recv_msg(player.sock)
            if data is None:
                break
            handle_message(player, data)
    except Exception as e:
        print(f"[!] Error con {player.name}: {e}")
    finally:
        disconnect_player(player)


def handle_message(player, data):
    msg_type = data.get("type")

    if msg_type == "set_name":
        player.name = data.get("name", player.name)[:16]
        player.send({"type": "welcome", "id": player.id, "name": player.name})
        # Empujar las noticias justo al conectar: los jugadores las ven
        # sin tener que pedirlas.
        player.send({"type": "news_list", "news": load_news()})

    elif msg_type == "create_room":
        room_id = str(uuid.uuid4())[:6].upper()
        room = Room(room_id, player)
        with lock:
            rooms[room_id] = room
            player.room_id = room_id
        player.send({"type": "room_created", "room_id": room_id, "players": room.roster()})
        print(f"[*] Sala {room_id} creada por {player.name}")

    elif msg_type == "join_room":
        room_id = data.get("room_id", "").upper()
        with lock:
            room = rooms.get(room_id)
            if room is None:
                player.send({"type": "error", "message": "Sala no encontrada"})
            elif room.state != "waiting":
                player.send({"type": "error", "message": "La partida ya empezo"})
            elif len(room.players) >= MAX_ROOM_PLAYERS:
                player.send({"type": "error", "message": "Sala llena"})
            else:
                room.players.append(player)
                room.ready[player.id] = False
                player.room_id = room_id
                player.send({
                    "type": "room_joined",
                    "room_id": room_id,
                    "host_name": room.host.name,
                    "players": room.roster(),
                })
                room.broadcast({
                    "type": "player_joined",
                    "name": player.name,
                    "players": room.roster(),
                }, exclude=player)
                print(f"[*] {player.name} se unio a sala {room_id} ({len(room.players)}/{MAX_ROOM_PLAYERS})")

    elif msg_type == "ready":
        with lock:
            room = rooms.get(player.room_id)
            if room is None:
                return
            room.ready[player.id] = data.get("ready", False)

            if (room.state == "waiting" and len(room.players) >= 2
                    and all(room.ready.get(p.id, False) for p in room.players)):
                room.state = "playing"
                room.eliminated = set()
                room.broadcast({
                    "type": "game_start",
                    "players": room.roster(),
                })
                print(f"[*] Juego iniciado en sala {room.id} ({len(room.players)} jugadores)")
            else:
                room.broadcast({
                    "type": "ready_update",
                    "players": room.roster(),
                })

    elif msg_type == "board_update":
        with lock:
            room = rooms.get(player.room_id)
            if room is None or room.state != "playing":
                return
            room.last_update = time.time()
            board = data.get("grid")
            score = data.get("score", 0)
            lines = data.get("lines", 0)
            level = data.get("level", 1)
            current_piece = data.get("current_piece")
            next_queue = data.get("next_queue", [])
            combo = data.get("combo", 0)
            game_over = data.get("game_over", False)

            room.broadcast({
                "type": "opponent_update",
                "player_id": player.id,
                "name": player.name,
                "grid": board,
                "score": score,
                "lines": lines,
                "level": level,
                "current_piece": current_piece,
                "next_queue": next_queue,
                "combo": combo,
                "game_over": game_over,
            }, exclude=player)

            if game_over and player.id not in room.eliminated:
                room.eliminated.add(player.id)
                player.send({"type": "game_result", "result": "lose", "opponent": ""})
                alive = room.alive_players()
                if room.state == "playing" and len(alive) <= 1:
                    room.state = "finished"
                    if len(alive) == 1:
                        alive[0].send({"type": "game_result", "result": "win", "opponent": ""})
                    print(f"[*] Partida terminada en sala {room.id}")

    elif msg_type == "send_garbage":
        with lock:
            room = rooms.get(player.room_id)
            if room is None or room.state != "playing":
                return
            candidates = [p for p in room.alive_players() if p.id != player.id]
            if not candidates:
                return
            target = random.choice(candidates)
            target.send({
                "type": "receive_garbage",
                "lines": data.get("lines", 0),
                "from_player": player.name,
            })

    elif msg_type == "chat":
        with lock:
            room = rooms.get(player.room_id)
            if room:
                room.broadcast({
                    "type": "chat",
                    "from": player.name,
                    "message": data.get("message", "")[:100],
                }, exclude=None)

    elif msg_type == "global_chat":
        text = data.get("message", "")[:150].strip()
        if text:
            entry = {"type": "global_chat", "from": player.name, "message": text}
            with lock:
                global_chat_history.append(entry)
                if len(global_chat_history) > MAX_GLOBAL_CHAT:
                    del global_chat_history[: len(global_chat_history) - MAX_GLOBAL_CHAT]
            broadcast_all(entry)

    elif msg_type == "get_global_chat":
        with lock:
            history = list(global_chat_history)
        player.send({"type": "global_chat_history", "messages": history})

    elif msg_type == "get_news":
        player.send({"type": "news_list", "news": load_news()})

    elif msg_type == "add_news":
        if not is_admin_player(player.name):
            player.send({"type": "news_error", "message": "Solo el administrador puede publicar"})
            return
        title = str(data.get("title", "")).strip()[:60]
        body = str(data.get("body", "")).strip()[:300]
        image = data.get("image") or ""
        if not isinstance(image, str):
            image = ""
        if len(image) > NEWS_IMAGE_MAX_B64:
            player.send({"type": "news_error", "message": "La imagen es muy pesada"})
            return
        if not title:
            player.send({"type": "news_error", "message": "Falta el titulo"})
            return
        news = load_news()
        news.insert(0, {
            "id": str(uuid.uuid4())[:8],
            "title": title,
            "body": body,
            "image": image,
            "author": player.name,
            "date": time.time(),
        })
        news = news[:MAX_NEWS]
        save_news(news)
        player.send({"type": "news_list", "news": news})
        broadcast_all({"type": "news_list", "news": news})
        print(f"[*] {player.name} publico una noticia: {title}" + (" (con imagen)" if image else ""))

    elif msg_type == "delete_news":
        if not is_admin_player(player.name):
            player.send({"type": "news_error", "message": "Solo el administrador puede borrar"})
            return
        nid = data.get("id")
        news = [n for n in load_news() if n.get("id") != nid]
        save_news(news)
        player.send({"type": "news_list", "news": news})
        broadcast_all({"type": "news_list", "news": news})
        print(f"[*] {player.name} borro una noticia")

    elif msg_type == "leave_room":
        leave_room(player)


def leave_room(player):
    with lock:
        room = rooms.get(player.room_id)
        if room is None:
            return
        was_playing = room.state == "playing"
        if player in room.players:
            room.players.remove(player)
        room.ready.pop(player.id, None)
        room.eliminated.discard(player.id)
        player.room_id = None
        player.ready = False

        if room.host == player and room.players:
            room.host = room.players[0]

        room.broadcast({"type": "player_left", "name": player.name, "players": room.roster()})

        if was_playing:
            alive = room.alive_players()
            if len(alive) <= 1:
                room.state = "finished"
                if len(alive) == 1:
                    alive[0].send({"type": "game_result", "result": "win", "opponent": ""})

        if not room.players:
            del rooms[room.id]
            print(f"[-] Sala {room.id} eliminada (vacia)")
        print(f"[*] {player.name} salio de sala {room.id}")


def disconnect_player(player):
    leave_room(player)
    with lock:
        clients.pop(player.id, None)
    close = getattr(player, "close", None)
    if close is not None:
        try:
            close()
        except Exception:
            pass
    else:
        try:
            player.sock.close()
        except Exception:
            pass
    print(f"[-] {player.name} desconectado")


def cleanup_loop():
    """Elimina salas 'finished' abandonadas (nadie hizo leave_room)
    para que no se acumulen en memoria en partidas largas del server."""
    while True:
        time.sleep(60)
        with lock:
            stale = [
                rid for rid, r in rooms.items()
                if r.state == "finished" and time.time() - r.last_update > 120
            ]
            for rid in stale:
                del rooms[rid]
        if stale:
            print(f"[*] Limpieza: {len(stale)} sala(s) inactiva(s) eliminada(s)")


async def _ws_handler(ws, path=None):
    # Firma compatible con websockets viejos (ws, path) y nuevos (ws).
    player = WSPlayer(ws, asyncio.get_running_loop())
    with lock:
        clients[player.id] = player
    print(f"[+] {player.name} conectado via WebSocket")
    try:
        async for raw in ws:
            try:
                data = json.loads(raw)
            except Exception:
                continue
            handle_message(player, data)
    except Exception:
        pass
    finally:
        disconnect_player(player)


def _ws_thread_main():
    """Hilo del servidor WebSocket: un event loop asyncio propio que
    comparte el mismo estado (clients/rooms) que el server TCP."""
    async def runner():
        async with websockets.serve(_ws_handler, HOST, WS_PORT, max_size=2_000_000):
            await asyncio.Future()
    try:
        asyncio.run(runner())
    except Exception as e:
        print(f"[!] Error en el servidor WebSocket: {e}")


def main():
    threading.Thread(target=cleanup_loop, daemon=True).start()
    if HAS_WS:
        threading.Thread(target=_ws_thread_main, daemon=True).start()
        print(f"[*] Servidor WebSocket para web escuchando en 0.0.0.0:{WS_PORT}")
    else:
        print("[!] 'pip install websockets' para aceptar clientes web (puerto " + str(WS_PORT) + ")")
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind((HOST, PORT))
    except OSError as e:
        print(f"[!] Error al bind: {e}")
        print(f"[!] Puerto {PORT} en uso. Intenta otro puerto o cierra la otra instancia.")
        sys.exit(1)
    server.listen(5)
    print(f"[*] Servidor TEPY escuchando en {HOST}:{PORT}")
    print(f"[*] Conecta con: localhost:{PORT}")
    print(f"[*] Ctrl+C para detener")

    try:
        while True:
            sock, addr = server.accept()
            player = Player(sock, addr)
            with lock:
                clients[player.id] = player
            thread = threading.Thread(target=handle_client, args=(player,), daemon=True)
            thread.start()
    except KeyboardInterrupt:
        print("\n[*] Servidor detenido")
    finally:
        server.close()


if __name__ == "__main__":
    main()
