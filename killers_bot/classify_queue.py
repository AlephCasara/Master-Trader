"""Fila de reclassificacao do observer (#95).

Antes, `persist_raw` gravava a mensagem e o cursor do backfill (`MAX(msg_id)`
em `raw_messages`) passava a trata-la como vista, mesmo quando a classificacao
falhava. Uma falha (timeout, login expirado, resposta sem JSON) virava perda
silenciosa: nenhum alerta, nenhuma nova tentativa, nem depois de reiniciar.

Aqui cada mensagem do canal que entra no pipeline ganha uma linha em
`classify_queue`, gravada na MESMA transacao do `raw_messages`. Ela so conta
como vista ao chegar a `done`:

    in_progress -> done                  classificou e encaminhou
    in_progress -> retry -> in_progress  falhou; nova tentativa com backoff
    retry ... -> exhausted               esgotou as tentativas; alerta e para

`in_progress` que sobrevive a um reinicio (o processo morreu no meio) volta
para `retry` na subida. Uma nova entrega do Telegram (edicao) zera a contagem.

Este modulo so mexe no banco. Orquestracao, alerta e reprocessamento ficam no
observer.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

IN_PROGRESS = "in_progress"
RETRY = "retry"
EXHAUSTED = "exhausted"
DONE = "done"

# Espera antes da tentativa N+1, apos a N-esima falha. A ultima se repete se
# max_attempts passar do tamanho da lista. Com o padrao de 7 tentativas a
# janela total e ~1h52min: cobre uma oscilacao do CLI e um login refeito, sem
# reabrir sinal velho horas depois.
BACKOFF_SEC = (30, 120, 300, 900, 1800, 3600)
DEFAULT_MAX_ATTEMPTS = 7
DEFAULT_ALERT_AFTER = 2


@dataclass(frozen=True)
class Failure:
    msg_id: int
    attempts: int
    state: str                       # retry | exhausted
    next_retry_at: Optional[float]   # epoch s; None quando exhausted
    alerted: bool


def _iso(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).isoformat()


def backoff(attempts: int) -> int:
    """Espera apos a `attempts`-esima falha (1 = primeira)."""
    return BACKOFF_SEC[min(max(attempts, 1), len(BACKOFF_SEC)) - 1]


def begin(conn: sqlite3.Connection, msg_id: int, *, fresh: bool,
          now: Optional[float] = None, commit: bool = True) -> None:
    """Marca a mensagem como em processamento.

    `fresh=True` e uma entrega do Telegram (nova, editada, backfill): versao
    possivelmente nova, entao a contagem de falhas zera. `alerted` fica, para
    que um sucesso posterior ainda gere o aviso de recuperacao.

    `commit=False` deixa a escrita na transacao aberta; o chamador a confirma
    junto com o `raw_messages`, para que nenhuma das duas exista sem a outra.
    """
    now = time.time() if now is None else now
    ts = _iso(now)
    if fresh:
        conn.execute(
            "INSERT INTO classify_queue (msg_id, state, attempts, first_seen_at, "
            "updated_at) VALUES (?, ?, 0, ?, ?) "
            "ON CONFLICT(msg_id) DO UPDATE SET state = excluded.state, "
            "attempts = 0, next_retry_at = NULL, updated_at = excluded.updated_at",
            (msg_id, IN_PROGRESS, ts, ts),
        )
    else:
        conn.execute(
            "UPDATE classify_queue SET state = ?, next_retry_at = NULL, "
            "updated_at = ? WHERE msg_id = ?",
            (IN_PROGRESS, ts, msg_id),
        )
    if commit:
        conn.commit()


def record_failure(conn: sqlite3.Connection, msg_id: int, reason: str, *,
                   now: Optional[float] = None,
                   max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> Failure:
    """Conta mais uma falha e agenda a proxima tentativa (ou desiste)."""
    now = time.time() if now is None else now
    row = conn.execute(
        "SELECT attempts, alerted FROM classify_queue WHERE msg_id = ?", (msg_id,)
    ).fetchone()
    attempts = (row[0] if row else 0) + 1
    alerted = bool(row[1]) if row else False
    if attempts >= max_attempts:
        state, next_at = EXHAUSTED, None
    else:
        state, next_at = RETRY, now + backoff(attempts)
    ts = _iso(now)
    conn.execute(
        "INSERT INTO classify_queue (msg_id, state, attempts, first_seen_at, "
        "updated_at, next_retry_at, last_error) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(msg_id) DO UPDATE SET state = excluded.state, "
        "attempts = excluded.attempts, updated_at = excluded.updated_at, "
        "next_retry_at = excluded.next_retry_at, last_error = excluded.last_error",
        (msg_id, state, attempts, ts, ts, next_at, reason[:500]),
    )
    conn.commit()
    return Failure(msg_id, attempts, state, next_at, alerted)


def mark_done(conn: sqlite3.Connection, msg_id: int, *, note: Optional[str] = None,
              now: Optional[float] = None) -> tuple[int, bool]:
    """Fecha a mensagem. Devolve (falhas ate aqui, se houve alerta de falha)
    e zera o `alerted`: o aviso de recuperacao sai uma vez so."""
    now = time.time() if now is None else now
    row = conn.execute(
        "SELECT attempts, alerted FROM classify_queue WHERE msg_id = ?", (msg_id,)
    ).fetchone()
    ts = _iso(now)
    conn.execute(
        "INSERT INTO classify_queue (msg_id, state, attempts, first_seen_at, "
        "updated_at, last_error) VALUES (?, ?, 0, ?, ?, ?) "
        "ON CONFLICT(msg_id) DO UPDATE SET state = excluded.state, "
        "next_retry_at = NULL, alerted = 0, updated_at = excluded.updated_at, "
        "last_error = COALESCE(excluded.last_error, classify_queue.last_error)",
        (msg_id, DONE, ts, ts, note),
    )
    conn.commit()
    return (row[0], bool(row[1])) if row else (0, False)


def give_up(conn: sqlite3.Connection, msg_id: int, reason: str, *,
            now: Optional[float] = None) -> None:
    """Desiste sem nova tentativa (ex.: a mensagem crua sumiu do banco)."""
    now = time.time() if now is None else now
    conn.execute(
        "UPDATE classify_queue SET state = ?, next_retry_at = NULL, "
        "updated_at = ?, last_error = ? WHERE msg_id = ?",
        (EXHAUSTED, _iso(now), reason[:500], msg_id),
    )
    conn.commit()


def set_alerted(conn: sqlite3.Connection, msg_id: int) -> None:
    conn.execute("UPDATE classify_queue SET alerted = 1 WHERE msg_id = ?", (msg_id,))
    conn.commit()


def is_due(conn: sqlite3.Connection, msg_id: int, now: float) -> bool:
    row = conn.execute(
        "SELECT state, next_retry_at FROM classify_queue WHERE msg_id = ?", (msg_id,)
    ).fetchone()
    return bool(row and row[0] == RETRY and row[1] is not None and row[1] <= now)


def due(conn: sqlite3.Connection, now: float, limit: int = 20) -> list[int]:
    """Mensagens com tentativa vencida, da mais antiga para a mais nova: um
    `open` que falhou volta antes do fechamento que veio depois dele."""
    return [r[0] for r in conn.execute(
        "SELECT msg_id FROM classify_queue WHERE state = ? AND next_retry_at <= ? "
        "ORDER BY msg_id LIMIT ?",
        (RETRY, now, limit),
    ).fetchall()]


def requeue_interrupted(conn: sqlite3.Connection, *,
                        now: Optional[float] = None) -> int:
    """Na subida: o que ficou `in_progress` morreu com o processo anterior.
    Volta para a fila ja vencido. Pode ter chegado ao receiver antes da queda;
    o receiver deduplica por msg_id."""
    now = time.time() if now is None else now
    cur = conn.execute(
        "UPDATE classify_queue SET state = ?, next_retry_at = ?, updated_at = ?, "
        "last_error = 'interrompida: o observer parou no meio do processamento' "
        "WHERE state = ?",
        (RETRY, now, _iso(now), IN_PROGRESS),
    )
    conn.commit()
    return cur.rowcount


def requeue(conn: sqlite3.Connection, msg_id: int, *,
            now: Optional[float] = None) -> bool:
    """Reenfileira por decisao do operador (ex.: depois de `exhausted`, ou uma
    falha anterior a esta fila). False se a mensagem crua nao existe."""
    if conn.execute("SELECT 1 FROM raw_messages WHERE msg_id = ?",
                    (msg_id,)).fetchone() is None:
        return False
    now = time.time() if now is None else now
    ts = _iso(now)
    conn.execute(
        "INSERT INTO classify_queue (msg_id, state, attempts, first_seen_at, "
        "updated_at, next_retry_at, last_error) VALUES (?, ?, 0, ?, ?, ?, ?) "
        "ON CONFLICT(msg_id) DO UPDATE SET state = excluded.state, attempts = 0, "
        "updated_at = excluded.updated_at, next_retry_at = excluded.next_retry_at, "
        "last_error = excluded.last_error",
        (msg_id, RETRY, ts, ts, now, "requeued by operator"),
    )
    conn.commit()
    return True


def counts(conn: sqlite3.Connection) -> dict:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT state, COUNT(*) FROM classify_queue "
        "WHERE state IN (?, ?, ?) GROUP BY state",
        (RETRY, EXHAUSTED, IN_PROGRESS),
    ).fetchall()}
