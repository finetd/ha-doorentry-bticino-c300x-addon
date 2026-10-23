ARG BUILD_FROM
FROM ${BUILD_FROM}

# Compila baresip DA SORGENTE + modulo G.729 (il citofono parla solo Speex/G.729, assenti nel
# pacchetto Debian). bcg729 = G.729 libero; re = base baresip; g729.c = modulo codec (juha-h).

# dipendenze di build e runtime. NB: avcodec (video H.264) richiede ANCHE avfilter+avdevice.
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake git pkg-config \
      libssl-dev libopus-dev zlib1g-dev libpng-dev \
      libavcodec-dev libavformat-dev libavutil-dev libswscale-dev \
      libavfilter-dev libavdevice-dev \
      ffmpeg jq ca-certificates python3 python3-cryptography python3-pip \
    && pip3 install --break-system-packages --no-cache-dir pyzipper \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src

# 1) bcg729 (libreria G.729)
RUN git clone --depth 1 https://github.com/BelledonneCommunications/bcg729.git \
    && cmake -S bcg729 -B bcg729/build -DENABLE_TESTS=NO -DCMAKE_INSTALL_PREFIX=/usr/local \
    && cmake --build bcg729/build -j"$(nproc)" \
    && cmake --install bcg729/build && ldconfig

# 2) libre (base di baresip), con patch SPERIMENTALE sull'address family 0 nell'ACK routing
RUN git clone --depth 1 https://github.com/baresip/re.git
COPY patch_sa_af.py /tmp/patch_sa_af.py
RUN python3 /tmp/patch_sa_af.py \
    && cmake -S re -B re/build -DCMAKE_INSTALL_PREFIX=/usr/local \
    && cmake --build re/build -j"$(nproc)" \
    && cmake --install re/build && ldconfig

# 3) baresip, con micro-patch che aggiunge l'attributo SDP di sessione "DEVADDR" (letto
#    dall'ambiente) a ogni chiamata: il posto esterno lo richiede per le chiamate uscenti
#    (cattura on-demand), altrimenti risponde 488 Not Acceptable Here.
RUN git clone --depth 1 https://github.com/baresip/baresip.git
COPY patch_devaddr.py /tmp/patch_devaddr.py
RUN python3 /tmp/patch_devaddr.py \
    && cmake -S baresip -B baresip/build -DCMAKE_INSTALL_PREFIX=/usr/local \
    && cmake --build baresip/build -j"$(nproc)" \
    && cmake --install baresip/build && ldconfig

# 4) modulo G.729: lo compilo come libreria condivisa e lo metto tra i moduli baresip.
#    I simboli di baresip/re restano non risolti e vengono soddisfatti al caricamento (dlopen).
RUN git clone --depth 1 https://github.com/juha-h/libbaresip-android.git lba \
    && MODDIR=$(dirname "$(find /usr/local/lib -name account.so -path '*baresip*' | head -1)") \
    && echo "Installo g729.so in $MODDIR" \
    && gcc -fPIC -shared -O2 \
         -I/usr/local/include -I/usr/local/include/re -I/src/baresip/include \
         -o "$MODDIR/g729.so" \
         lba/baresip-app-modules/g729/g729.c \
         -L/usr/local/lib -lbcg729 \
    && ls -l "$MODDIR/g729.so"

# 5) modulo owncmd: invia i frame OpenWebNet (segreteria, luce, apri-porta) come SIP MESSAGE
COPY owncmd.c /tmp/owncmd.c
RUN MODDIR=$(dirname "$(find /usr/local/lib -name account.so -path '*baresip*' | head -1)") \
    && gcc -fPIC -shared -O2 \
         -I/usr/local/include -I/usr/local/include/re -I/src/baresip/include \
         -o "$MODDIR/owncmd.so" /tmp/owncmd.c \
    && ls -l "$MODDIR/owncmd.so"

COPY run.sh /run.sh
COPY provision.py /provision.py
COPY doorbell.py /doorbell.py
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
