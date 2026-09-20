"""Entry point para el build web (pygbag).

El juego completo vive en tetris.py; este archivo solo lanza su
corutina principal. El loop de tetris.main() hace
`await asyncio.sleep(0)` por frame, lo que en el navegador cede al
browser (eventos, render, WebSocket) y en escritorio es un yield
inmediato.

Escritorio:  python tetris.py          (no usa este archivo)
Web:         python -m pygbag --build .   (usa este main.py)
"""
import asyncio

import tetris


async def main():
    await tetris.main()


# IMPORTANTE: este guard no es opcional. Sin el, si este modulo llega a
# ser re-importado (por ejemplo por multiprocessing con metodo "spawn"
# en Windows, que reimporta __main__ en cada proceso hijo), el juego
# entero se relanza en cada import en vez de una sola vez al ejecutarlo
# como script. Es la misma familia de bug que el auto-lanzado de
# server.py en tepy.py: un proceso que se relanza a si mismo sin freno.
if __name__ == "__main__":
    asyncio.run(main())
