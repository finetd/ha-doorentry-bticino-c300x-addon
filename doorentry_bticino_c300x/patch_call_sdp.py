#!/usr/bin/env python3
"""
Aggiunge in baresip un accessor pubblico `call_sdp()` che ritorna il campo privato
`struct sdp_session *sdp` della struct call. Serve al modulo devaddr_sdp per aggiungere
attributi SDP a livello di SESSIONE (come fa l'app ufficiale con linphone), non solo a
livello di media.

Idempotente: se la patch è già applicata, esce senza fare nulla.
"""
import os, sys

ROOT = "/src/baresip"
HDR  = os.path.join(ROOT, "include", "baresip.h")
SRC  = os.path.join(ROOT, "src", "call.c")

HDR_LINE = "struct sdp_session *call_sdp(const struct call *call);\n"
SRC_FUNC = """
/* Added by patch_call_sdp.py: public accessor to the private sdp_session field */
struct sdp_session *call_sdp(const struct call *call)
{
	return call ? call->sdp : NULL;
}

"""

def patch_header():
    with open(HDR) as f:
        c = f.read()
    if "call_sdp(const struct call" in c:
        print("patch_call_sdp: header gia' patchato"); return
    # inseriamo subito dopo call_video declaration
    anchor = "struct video *call_video(const struct call *call);"
    if anchor not in c:
        print("patch_call_sdp: ancora in header non trovata, salto", file=sys.stderr); return
    c = c.replace(anchor, anchor + "\n" + HDR_LINE.rstrip())
    with open(HDR, "w") as f: f.write(c)
    print("patch_call_sdp: header patchato")

def patch_source():
    with open(SRC) as f:
        c = f.read()
    if "call_sdp(const struct call" in c:
        print("patch_call_sdp: sorgente gia' patchato"); return
    # Inseriamo prima della fine del file (una funzione pubblica tipica è alla fine)
    # cerchiamo una funzione pubblica di call come ancora
    anchor = "struct audio *call_audio(const struct call *call)"
    if anchor not in c:
        print("patch_call_sdp: ancora in sorgente non trovata, salto", file=sys.stderr); return
    c = c.replace(anchor, SRC_FUNC + anchor)
    with open(SRC, "w") as f: f.write(c)
    print("patch_call_sdp: sorgente patchato")

if __name__ == "__main__":
    patch_header()
    patch_source()
