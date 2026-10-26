"""A stand-in Bolt server for connection-failure tests, never the real Neo4j.

It accepts TCP, answers the Bolt handshake as protocol 5.4, and answers the
login (HELLO, LOGON) and a pooled connection's liveness check (RESET) with
SUCCESS. A transaction's BEGIN is never answered unless `query` is given: a
query sent to it then waits forever, the way one does through a forwarder
whose upstream vanished.

With `query`, transactions work: BEGIN, COMMIT and ROLLBACK succeed, and each
RUN asks `query(n)` (n counting RUNs from 1) how to answer:
- "rows": one record `{x: 1}`;
- "hint": a FAILURE saying a hinted index is still being built;
- ("slow", seconds): one record, that long later.

`freeze()` makes it stop answering anything at all, the handshake of a new
connection included, while still accepting the socket: what a local forwarder
(socat) does once the Neo4j behind it is gone.

Reads are loops over `recv`, not `MSG_WAITALL`, so it behaves the same on Windows.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from collections.abc import Callable
from typing import Any

_HELLO, _RESET, _RUN, _BEGIN, _COMMIT, _ROLLBACK, _PULL, _LOGON = 0x01, 0x0F, 0x10, 0x11, 0x12, 0x13, 0x3F, 0x6A
_SUCCESS, _RECORD, _IGNORED, _FAILURE = 0x70, 0x71, 0x7E, 0x7F
HINT_FAILURE = "Failed to fulfil the hints of the query. Index is still populating"


def _string(text: str) -> bytes:
    data = text.encode()
    return (bytes([0x80 + len(data)]) if len(data) < 16 else bytes([0xD0, len(data)])) + data


def _map(**fields: str | list[str]) -> bytes:
    out = bytes([0xA0 + len(fields)])
    for key, value in fields.items():
        out += _string(key)
        if isinstance(value, list):
            out += bytes([0x90 + len(value)]) + b"".join(map(_string, value))
        else:
            out += _string(value)
    return out


def _message(signature: int, body: bytes) -> bytes:
    message = bytes([0xB1, signature]) + body
    return struct.pack(">H", len(message)) + message + b"\0\0"


_LOGIN_SUCCESS = _message(_SUCCESS, _map(server="Neo4j/5.26.0", connection_id="bolt-1"))
_EMPTY_SUCCESS = _message(_SUCCESS, _map())
_RUN_SUCCESS = _message(_SUCCESS, _map(fields=["x"]))
_ROW = _message(_RECORD, bytes([0x91, 0x01]))
_IGNORED_MESSAGE = _message(_IGNORED, _map())
_HINT = _message(_FAILURE, _map(code="Neo.ClientError.Statement.ExecutionFailed", message=HINT_FAILURE))


class FakeBolt:
    def __init__(self, query: Callable[[int], Any] | None = None) -> None:
        self._query = query
        self.runs = 0
        self._lock = threading.Lock()
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
            if self._frozen.is_set() or self._recv_exact(conn, 20) is None:
                return
            conn.sendall(b"\0\0\4\5")  # Bolt 5.4
            failed = False
            while (message := self._read_message(conn)) is not None:
                if self._frozen.is_set():
                    return
                signature = message[1] if len(message) > 1 else None
                if signature in (_HELLO, _LOGON):
                    conn.sendall(_LOGIN_SUCCESS)
                elif signature == _RESET:
                    failed = False
                    conn.sendall(_EMPTY_SUCCESS)
                elif self._query is None:
                    continue  # logged in, and then silence
                elif failed:
                    conn.sendall(_IGNORED_MESSAGE)
                elif signature in (_BEGIN, _COMMIT, _ROLLBACK):
                    conn.sendall(_EMPTY_SUCCESS)
                elif signature == _RUN:
                    with self._lock:
                        self.runs += 1
                        answer = self._query(self.runs)
                    if answer == "hint":
                        failed = True
                        conn.sendall(_HINT)
                        continue
                    if isinstance(answer, tuple):
                        time.sleep(answer[1])
                    conn.sendall(_RUN_SUCCESS)
                elif signature == _PULL:
                    conn.sendall(_ROW + _EMPTY_SUCCESS)
        except OSError:
            return

    @staticmethod
    def _recv_exact(conn: socket.socket, size: int) -> bytes | None:
        data = b""
        while len(data) < size:
            chunk = conn.recv(size - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    def _read_message(self, conn: socket.socket) -> bytes | None:
        data = b""
        while True:
            header = self._recv_exact(conn, 2)
            if header is None:
                return None
            (size,) = struct.unpack(">H", header)
            if size == 0:
                if data:
                    return data
                continue
            chunk = self._recv_exact(conn, size)
            if chunk is None:
                return None
            data += chunk
