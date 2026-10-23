#!/usr/bin/env python3
"""Patch a libre (src/sip/request.c): accetta le route in schema "sips" (SIP over TLS) oltre a
"sip" quando invia una richiesta in-dialog (es. l'ACK del 200 OK).

PERCHÉ: il proxy Flexisip del cloud BTicino inserisce Record-Route in schema sips:
(<sips:host:443;lr>). Il libre originale, in sip_request_alloc, fa:
        if (pl_strcasecmp(&route->scheme, "sip"))
                return ENOSYS;
cioè accetta SOLO "sip" e rifiuta "sips" con ENOSYS ("Function not implemented"). È questo il
motivo per cui la chiamata on-demand cade subito dopo il 200 OK: baresip non riesce a mandare
l'ACK attraverso la route sips. La patch accetta anche "sips" e, in quel caso, forza il trasporto
TLS.

Robusto (regex, tollerante a spaziatura/versione), idempotente, non bloccante se non trova il punto."""
import re
import sys

PATH = "/src/re/src/sip/request.c"
MARK = "PATCH BTicino sips-route"

# accetta sip OPPURE sips; se sips e nessun trasporto imposto dal chiamante -> TLS
REPLACEMENT = (
    "if (pl_strcasecmp(&route->scheme, \"sip\") &&\n"
    "\t    pl_strcasecmp(&route->scheme, \"sips\"))  /* {mark} */\n"
    "\t\treturn ENOSYS;\n"
    "\n"
    "\tif (!pl_strcasecmp(&route->scheme, \"sips\") && tp == SIP_TRANSP_NONE)\n"
    "\t\ttp = SIP_TRANSP_TLS;"
).format(mark=MARK)

# il blocco originale: if (pl_strcasecmp(&route->scheme, "sip")) return ENOSYS;
PATTERN = re.compile(
    r'if\s*\(\s*pl_strcasecmp\(\s*&route->scheme\s*,\s*"sip"\s*\)\s*\)\s*'
    r'return\s+ENOSYS\s*;'
)


def main():
    try:
        s = open(PATH).read()
    except FileNotFoundError:
        print(f"AVVISO: {PATH} non trovato, salto la patch (non bloccante)")
        return
    if MARK in s:
        print("patch sips-route gia' presente, salto")
        return
    s2, n = PATTERN.subn(REPLACEMENT, s, count=1)
    if n == 0:
        print("AVVISO: punto per la patch sips-route non trovato, salto (non bloccante)")
        return
    open(PATH, "w").write(s2)
    print("patch sips-route applicata a request.c")


if __name__ == "__main__":
    main()
