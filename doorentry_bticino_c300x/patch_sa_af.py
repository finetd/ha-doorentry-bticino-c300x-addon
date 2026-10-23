#!/usr/bin/env python3
"""SPERIMENTALE — patch a libre (src/sa/sa.c): quando sa_set_port riceve un indirizzo con
address family = 0 (AF_UNSPEC), forza AF_INET invece di limitarsi a stampare un warning.
 
Contesto: nelle chiamate verso il cloud BTicino, baresip logga "sa_set_port: no af 0 (port 443)"
mentre instrada l'ACK attraverso i Record-Route (host in schema sips:) del proxy Flexisip.
Questo tentativo verifica se l'af=0 è concatenato alla caduta della chiamata (ENOSYS) o se è solo
rumore cosmetico. È scritto sul vero tipo `struct sa` di re (union u.sa / u.in), non su sockaddr.
 
Idempotente. Se l'ancora non viene trovata (per cambi futuri in re) esce con errore chiaro."""
import sys
 
PATH = "/src/re/src/sa/sa.c"
 
ANCHOR = """	if (!sa)
		return;
 
	switch (sa->u.sa.sa_family) {"""
 
REPLACEMENT = """	if (!sa)
		return;
 
	/* PATCH SPERIMENTALE BTicino/Flexisip: se la famiglia è 0 (hostname non risolto nel
	   Record-Route), forza AF_INET invece di scartare l'operazione. */
	if (sa->u.sa.sa_family == 0)
		sa->u.sa.sa_family = AF_INET;
 
	switch (sa->u.sa.sa_family) {"""
 
 
def main():
    try:
        s = open(PATH).read()
    except FileNotFoundError:
        print(f"ERRORE: {PATH} non trovato", file=sys.stderr)
        sys.exit(1)
    if "PATCH SPERIMENTALE BTicino/Flexisip" in s:
        print("patch sa_af già presente, salto")
        return
    if ANCHOR not in s:
        print("ERRORE: ancora per la patch sa_af non trovata in sa.c", file=sys.stderr)
        sys.exit(1)
    open(PATH, "w").write(s.replace(ANCHOR, REPLACEMENT, 1))
    print("patch sa_af applicata a sa.c")
 
 
if __name__ == "__main__":
    main()
