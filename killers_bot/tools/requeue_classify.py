#!/usr/bin/env python3
"""Lista ou reenfileira mensagens na fila de reclassificacao do observer (#95).

Uso (VPS, no checkout do observer):
    python3 killers_bot/tools/requeue_classify.py --db DB --list
    python3 killers_bot/tools/requeue_classify.py --db DB MSG_ID [MSG_ID ...]

DB e o banco do feed: /home/ubuntu/killers-bot/state.sqlite (killers) ou
/home/ubuntu/insiders-bot/state.sqlite (insiders).

`--list` so le. A coluna de etapa diz onde a mensagem parou: `classify` (o
Claude nao classificou) ou `deliver` (classificada, mas o receiver nunca
respondeu 2xx; o corpo exato do POST esta guardado).

Reenfileirar ESCREVE no banco do observer em producao e, na proxima passada
da fila (<= 15s), a mensagem segue: etapa `deliver` reenvia o MESMO corpo,
etapa `classify` reclassifica. `--reclassify` descarta o corpo guardado e
reclassifica. So o operador decide. O receiver deduplica por msg_id, e um
`open` superado por mensagem posterior do mesmo sinal nao e reaberto.
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from killers_bot import classify_queue  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--list", action="store_true",
                    help="mostra o que nao esta em done (somente leitura)")
    ap.add_argument("--reclassify", action="store_true",
                    help="descarta o corpo guardado e reclassifica")
    ap.add_argument("msg_ids", nargs="*", type=int)
    args = ap.parse_args(argv)
    if not args.list and not args.msg_ids:
        ap.error("informe --list ou ao menos um msg_id")

    if args.list:
        conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        print("msg_id\tstate\tstage\tattempts\tupdated_at\tlast_error")
        for row in conn.execute(
                "SELECT msg_id, state, CASE WHEN payload IS NULL THEN 'classify' "
                "ELSE 'deliver' END, attempts, updated_at, last_error "
                "FROM classify_queue WHERE state != ? ORDER BY msg_id",
                (classify_queue.DONE,)):
            print(*row, sep="\t")
        print(classify_queue.counts(conn), file=sys.stderr)
        return 0

    conn = sqlite3.connect(args.db, timeout=10)
    missing = [m for m in args.msg_ids
               if not classify_queue.requeue(conn, m, reclassify=args.reclassify)]
    for m in missing:
        print(f"msg_id {m}: nao existe em raw_messages — ignorado", file=sys.stderr)
    print(f"reenfileiradas: {len(args.msg_ids) - len(missing)}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
