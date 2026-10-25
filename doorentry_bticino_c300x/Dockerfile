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

# 2) libre (base di baresip), con due patch:
#    - patch_sips_route: accetta le route "sips:" nell'ACK (il cloud BTicino usa Flexisip con
#      Record-Route sips:, che il libre originale rifiuta con ENOSYS -> on-demand bloccato).
#    - patch_sa_af: forza AF_INET quando l'address family è 0 (toglie il warning cosmetico).
RUN git clone --depth 1 https://github.com/baresip/re.git
COPY patch_sips_route.py /tmp/patch_sips_route.py
COPY patch_sa_af.py /tmp/patch_sa_af.py
COPY patch_call_sdp.py /tmp/patch_call_sdp.py
RUN python3 /tmp/patch_sips_route.py \
    && python3 /tmp/patch_sa_af.py \
    && python3 /tmp/patch_call_sdp.py \
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

# 5) moduli custom:
#    - owncmd:      invia i frame OpenWebNet (segreteria, luce, apri-porta) come SIP MESSAGE
#    - recorder:    registra audio+video in file WAV e Y4M (poi ffmpeg mixa in MP4)
#    - devaddr_sdp: aggiunge "DEVADDR" nell'SDP INVITE outgoing (necessario per l'audio)
COPY owncmd.c      /tmp/owncmd.c
COPY recorder.c    /tmp/recorder.c
COPY devaddr_sdp.c /tmp/devaddr_sdp.c
RUN MODDIR=$(dirname "$(find /usr/local/lib -name account.so -path '*baresip*' | head -1)") \
    && INC="-I/usr/local/include -I/usr/local/include/re -I/src/baresip/include" \
    && gcc -fPIC -shared -O2 $INC -o "$MODDIR/owncmd.so"      /tmp/owncmd.c \
    && gcc -fPIC -shared -O2 $INC -o "$MODDIR/recorder.so"    /tmp/recorder.c -lpthread \
    && gcc -fPIC -shared -O2 $INC -o "$MODDIR/devaddr_sdp.so" /tmp/devaddr_sdp.c \
    && ls -l "$MODDIR/owncmd.so" "$MODDIR/recorder.so" "$MODDIR/devaddr_sdp.so"

COPY run.sh /run.sh
COPY provision.py /provision.py
COPY doorbell.py /doorbell.py
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
