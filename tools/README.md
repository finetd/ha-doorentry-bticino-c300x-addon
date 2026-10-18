# tools/ — script di sviluppo / reverse engineering

Script da riga di comando usati durante lo sviluppo e utili per il debug. **Non servono
all'utente finale**: l'add-on fa tutto in automatico. Richiedono Python 3 sul PC.

- `c300x_client.py` — login al cloud, discover impianto/credenziali SIP, scarica la configurazione
  (`conf`), stampa il DEVADDR; comandi vari.
- `c300x_setup.py` — genera i certificati client (CSR + signcert).
- `c300x_sip_register.py` — test di registrazione SIP in Python puro.
- `c300x_devaddr.py` — scarica la configurazione e ricava il DEVADDR.
- `c300x_doorbell.py` — prototipo iniziale (storico).
