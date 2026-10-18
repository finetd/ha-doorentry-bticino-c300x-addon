#!/usr/bin/env python3
"""Micro-patch a baresip (src/call.c): aggiunge l'attributo SDP di sessione 'DEVADDR'
(letto dall'ambiente) a ogni chiamata. Serve per le chiamate uscenti verso il posto esterno
del Classe 300X, che altrimenti rispondono 488 Not Acceptable Here.
Idempotente: se già applicata, non fa nulla."""
import sys

PATH = "/src/baresip/src/call.c"
ANCHOR = "\terr = sdp_session_alloc(&call->sdp, &prm->laddr);\n\tif (err)\n\t\tgoto out;\n"
INJECT = ANCHOR + (
    "\n\t{\n"
    "\t\tconst char *devaddr = getenv(\"DEVADDR\");\n"
    "\t\tif (devaddr && str_isset(devaddr))\n"
    "\t\t\t(void)sdp_session_set_lattr(call->sdp, true,\n"
    "\t\t\t\t\"DEVADDR\", \"%s\", devaddr);\n"
    "\t}\n"
)

s = open(PATH).read()
if "getenv(\"DEVADDR\")" in s:
    print("patch DEVADDR già presente, salto")
    sys.exit(0)
if ANCHOR not in s:
    print("ERRORE: anchor per la patch DEVADDR non trovato in call.c", file=sys.stderr)
    sys.exit(1)
open(PATH, "w").write(s.replace(ANCHOR, INJECT, 1))
print("patch DEVADDR applicata a call.c")
