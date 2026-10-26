"""A stand-in Bolt server for connection-failure tests, never the real Neo4j.

It accepts TCP, answers the Bolt handshake as protocol 5.4, and answers the
login (HELLO, LOGON) and a pooled connection's liveness check (RESET) with
SUCCESS. Everything else (BEGIN, RUN, PULL) is never answered: a query sent to
it waits forever, the way one does through a forwarder whose upstream vanished.

`freeze()` makes it stop answering anything at all, the handshake of a new
connection included, while still accepting the socket: what a local forwarder
(socat) does once the Neo4j behind it is gone.
"""

from __future__ import annotations

import socket
import struct
import threading

_HELLO, _LOGON, _RESET = 0x01, 0x6A, 0x0F


def _tiny_string(text: str) -> bytes:
    data = text.encode()
    return bytes([0x80 + len(data)]) + data


_SUCCESS = (
    bytes([0xB1, 0x70, 0xA2])
    + _tiny_string("server") + _tiny_string("Neo4j/5.26.0")
    + _tiny_string("connection_id") + _tiny_string("bolt-1")
)


def _chunked(message: bytes) -> bytes:
    return struct.pack(">H", len(message)) + message + b"\0\0"


class FakeBolt:
    def __init__(self) -> None:
        self._frozen = threading.Event()
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(50)
        self._held: list[socket.socket] = []
        self.uri = f"bolt://127.0.0.1:{self._listener.getsockname()[1]}"
        threading.Thread(target=self._accept, daemon=True).start()

    def freeze(self) -> None:
        self._frozen.set()

    def close(self) -> None:
        self._listener.close()
        for conn in self._held:
            conn.close()

    def _accept(self) -> None:
        while True:
            try:
                conn = self._listener.accept()[0]
            except OSError:
                return
            self._held.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            if self._frozen.is_set() or len(conn.recv(20, socket.MSG_WAITALL)) < 20:
                return
            conn.sendall(b"\0\0\4\5")  # Bolt 5.4
            while (message := self._read_message(conn)) is not None:
                if self._frozen.is_set():
                    return
                if len(message) > 1 and message[1] in (_HELLO, _LOGON, _RESET):
                    conn.sendall(_chunked(_SUCCESS))
        except OSError:
            return

    @staticmethod
    def _read_message(conn: socket.socket) -> bytes | None:
        data = b""
        while True:
            header = conn.recv(2, socket.MSG_WAITALL)
            if len(header) < 2:
                return None
            (size,) = struct.unpack(">H", header)
            if size == 0:
                if data:
                    return data
                continue
            data += conn.recv(size, socket.MSG_WAITALL)
