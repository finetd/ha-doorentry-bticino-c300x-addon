#!/usr/bin/env python3
"""SPERIMENTALE — patch a libre (src/sa/sa.c): quando sa_set_port riceve un indirizzo con
address family = 0 (AF_UNSPEC), forza AF_INET invece di limitarsi a stampare un warning.

Scopo: verificare se l'avviso "sa_set_port: no af 0 (port 443)" è concatenato alla caduta della
chiamata on-demand verso il cloud BTicino, o se è solo rumore cosmetico.

Robusto: individua la funzione con una regex (tollerante a spaziatura/indentazione) e inserisce il
guard subito dopo l'apertura. Idempotente. Se non trova la funzione NON blocca il build: stampa un
avviso ed esce con successo (così un cambio futuro in re non fa fallire tutto)."""
import re
import sys

PATH = "/src/re/src/sa/sa.c"
MARK = "PATCH SPERIMENTALE BTicino"

GUARD = (
    "\n\t/* {mark}/Flexisip: se la famiglia e' 0 (hostname non risolto nel\n"
    "\t   Record-Route), forza AF_INET invece di scartare l'operazione. */\n"
    "\tif (sa && sa->u.sa.sa_family == 0)\n"
    "\t\tsa->u.sa.sa_family = AF_INET;\n"
).format(mark=MARK)


def main():
    try:
        s = open(PATH).read()
    except FileNotFoundError:
        print(f"AVVISO: {PATH} non trovato, salto la patch (non bloccante)")
        return
    if MARK in s:
        print("patch sa_af gia' presente, salto")
        return

    # trova l'inizio del corpo di sa_set_port: "void sa_set_port(...)" seguito da "{"
    m = re.search(r"void\s+sa_set_port\s*\([^)]*\)\s*\{", s)
    if not m:
        print("AVVISO: funzione sa_set_port non trovata, salto la patch (non bloccante)")
        return

    insert_at = m.end()  # subito dopo la parentesi graffa di apertura
    patched = s[:insert_at] + GUARD + s[insert_at:]
    open(PATH, "w").write(patched)
    print("patch sa_af applicata a sa.c")


if __name__ == "__main__":
    main()
