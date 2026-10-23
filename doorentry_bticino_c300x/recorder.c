/**
 * recorder.c  Modulo baresip per registrare la chiamata su file (audio WAV + video Y4M).
 *
 * Si aggancia alla pipeline di DECODIFICA (filtro audio + filtro video) e, mentre la
 * registrazione è attiva, scrive i campioni PCM ricevuti su un file .wav e i fotogrammi YUV su un
 * file .y4m. doorbell.py poi assembla i due in MP4 con ffmpeg (già presente nel container).
 *
 * Comandi baresip:
 *   recorder_start <basepath>   apre <basepath>.wav e <basepath>.y4m, attiva la registrazione
 *   recorder_stop               chiude i file
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <pthread.h>
#include <re.h>
#include <rem.h>
#include <baresip.h>


static struct {
	pthread_mutex_t lock;
	int active;
	FILE *wav;
	FILE *y4m;
	uint32_t wav_data_bytes;   /* per aggiornare l'header alla chiusura           */
	uint32_t srate;            /* sample rate preso dal primo frame audio         */
	uint8_t  ch;               /* canali                                          */
	int      y4m_header_done;  /* header Y4M scritto al primo frame video         */
	uint32_t vframes;          /* numero di frame video scritti (diagnostica)     */
	uint32_t aframes;          /* numero di callback audio (diagnostica)          */
	char basepath[512];
} R = { .lock = PTHREAD_MUTEX_INITIALIZER };


/* === WAV ================================================================= */

static void wav_write_u32(FILE *f, uint32_t v)
{
	uint8_t b[4] = { v & 0xff, (v >> 8) & 0xff, (v >> 16) & 0xff, (v >> 24) & 0xff };
	fwrite(b, 1, 4, f);
}

static void wav_write_u16(FILE *f, uint16_t v)
{
	uint8_t b[2] = { v & 0xff, (v >> 8) & 0xff };
	fwrite(b, 1, 2, f);
}

static void wav_write_header(FILE *f, uint32_t srate, uint8_t ch, uint32_t data_bytes)
{
	uint32_t byte_rate = srate * ch * 2;
	fseek(f, 0, SEEK_SET);
	fwrite("RIFF", 1, 4, f);
	wav_write_u32(f, 36 + data_bytes);
	fwrite("WAVEfmt ", 1, 8, f);
	wav_write_u32(f, 16);           /* fmt chunk size            */
	wav_write_u16(f, 1);            /* PCM                       */
	wav_write_u16(f, ch);
	wav_write_u32(f, srate);
	wav_write_u32(f, byte_rate);
	wav_write_u16(f, ch * 2);       /* block align                */
	wav_write_u16(f, 16);           /* bits per sample            */
	fwrite("data", 1, 4, f);
	wav_write_u32(f, data_bytes);
}


/* === filtri baresip ====================================================== */

struct rec_enc { struct aufilt_enc_st af; };
struct rec_dec { struct aufilt_dec_st af; };
struct rec_vdec { struct vidfilt_dec_st vf; };

static int au_decupd(struct aufilt_dec_st **stp, void **ctx, const struct aufilt *af,
                     struct aufilt_prm *prm, const struct audio *au)
{
	(void)ctx; (void)au;
	if (*stp) return 0;
	struct rec_dec *st = mem_zalloc(sizeof(*st), NULL);
	if (!st) return ENOMEM;
	st->af.af = af;
	pthread_mutex_lock(&R.lock);
	if (!R.srate) { R.srate = prm->srate; R.ch = prm->ch; }
	pthread_mutex_unlock(&R.lock);
	*stp = &st->af;
	return 0;
}

static int au_dec(struct aufilt_dec_st *st, struct auframe *af)
{
	(void)st;
	if (!af || !af->sampv || !af->sampc) return 0;
	pthread_mutex_lock(&R.lock);
	if (R.active && R.wav) {
		R.aframes++;
		if (R.aframes <= 3)
			info("recorder: audio frame #%u fmt=%d sampc=%zu\n",
			     R.aframes, af->fmt, af->sampc);
		/* G.729 decoder emette S16LE a 8kHz, ma non diamo per scontato nulla:
		   accettiamo S16LE (ottimale) e RAW (lo trattiamo come S16LE perché il flusso
		   intermedio in baresip è PCM interleaved 16-bit) */
		if (af->fmt == AUFMT_S16LE || af->fmt == AUFMT_RAW) {
			size_t bytes = af->sampc * 2;
			if (fwrite(af->sampv, 1, bytes, R.wav) == bytes)
				R.wav_data_bytes += (uint32_t)bytes;
		}
		else if (R.aframes == 1) {
			warning("recorder: formato audio inatteso (%d), audio non registrato\n",
			        af->fmt);
		}
	}
	pthread_mutex_unlock(&R.lock);
	return 0;
}

static int vid_decupd(struct vidfilt_dec_st **stp, void **ctx, const struct vidfilt *vf,
                      struct vidfilt_prm *prm, const struct video *vid)
{
	(void)ctx; (void)prm; (void)vid;
	if (*stp) return 0;
	struct rec_vdec *st = mem_zalloc(sizeof(*st), NULL);
	if (!st) return ENOMEM;
	st->vf.vf = vf;
	*stp = &st->vf;
	return 0;
}

static int vid_dec(struct vidfilt_dec_st *st, struct vidframe *frame, uint64_t *timestamp)
{
	(void)st; (void)timestamp;
	if (!frame || frame->fmt != VID_FMT_YUV420P) return 0;
	pthread_mutex_lock(&R.lock);
	if (R.active && R.y4m) {
		int w = frame->size.w, h = frame->size.h;
		if (!R.y4m_header_done) {
			/* Y4M header: dichiariamo 10 fps nominali (il C300X in autoaccensione invia
			   ~9-10 fps). doorbell.py comunque calcola l'fps reale dai frame scritti e
			   la passa a ffmpeg con -r, per audio e video sincronizzati. */
			fprintf(R.y4m, "YUV4MPEG2 W%d H%d F10:1 Ip A1:1\n", w, h);
			R.y4m_header_done = 1;
			info("recorder: primo frame video %dx%d, header Y4M scritto\n", w, h);
		}
		R.vframes++;
		if (R.vframes <= 5 || R.vframes % 25 == 0)
			info("recorder: frame video #%u (%dx%d)\n", R.vframes, w, h);
		fputs("FRAME\n", R.y4m);
		int uv_w = w / 2, uv_h = h / 2;
		for (int y = 0; y < h; y++)
			fwrite(frame->data[0] + y * frame->linesize[0], 1, w, R.y4m);
		for (int y = 0; y < uv_h; y++)
			fwrite(frame->data[1] + y * frame->linesize[1], 1, uv_w, R.y4m);
		for (int y = 0; y < uv_h; y++)
			fwrite(frame->data[2] + y * frame->linesize[2], 1, uv_w, R.y4m);
	}
	pthread_mutex_unlock(&R.lock);
	return 0;
}


/* === comandi ============================================================= */

static int cmd_start(struct re_printf *pf, void *arg)
{
	const struct cmd_arg *carg = arg;
	if (!carg || !carg->prm) return re_hprintf(pf, "recorder_start: uso <basepath>\n");

	pthread_mutex_lock(&R.lock);
	if (R.active) { pthread_mutex_unlock(&R.lock); return re_hprintf(pf, "gia' attivo\n"); }
	strncpy(R.basepath, carg->prm, sizeof(R.basepath) - 1);
	R.basepath[sizeof(R.basepath) - 1] = 0;

	char path[600];
	snprintf(path, sizeof(path), "%s.wav", R.basepath);
	R.wav = fopen(path, "wb");
	snprintf(path, sizeof(path), "%s.y4m", R.basepath);
	R.y4m = fopen(path, "wb");
	if (!R.wav || !R.y4m) {
		if (R.wav) { fclose(R.wav); R.wav = NULL; }
		if (R.y4m) { fclose(R.y4m); R.y4m = NULL; }
		pthread_mutex_unlock(&R.lock);
		return re_hprintf(pf, "recorder_start: impossibile aprire i file\n");
	}
	/* header WAV placeholder (riscritto alla chiusura con le dimensioni reali) */
	wav_write_header(R.wav, R.srate ? R.srate : 8000, R.ch ? R.ch : 1, 0);
	R.wav_data_bytes = 0;
	R.y4m_header_done = 0;
	R.vframes = 0;
	R.aframes = 0;
	R.active = 1;
	pthread_mutex_unlock(&R.lock);
	return re_hprintf(pf, "recorder_start: ok %s\n", R.basepath);
}

static int cmd_stop(struct re_printf *pf, void *arg)
{
	(void)arg;
	pthread_mutex_lock(&R.lock);
	if (!R.active) { pthread_mutex_unlock(&R.lock); return re_hprintf(pf, "non attivo\n"); }
	R.active = 0;
	if (R.wav) {
		wav_write_header(R.wav, R.srate ? R.srate : 8000, R.ch ? R.ch : 1, R.wav_data_bytes);
		fclose(R.wav); R.wav = NULL;
	}
	if (R.y4m) { fclose(R.y4m); R.y4m = NULL; }
	uint32_t saved_bytes = R.wav_data_bytes;
	uint32_t saved_vframes = R.vframes;
	/* reset per la prossima registrazione (altrimenti il secondo giro usa srate/header vecchi) */
	R.wav_data_bytes = 0;
	R.y4m_header_done = 0;
	R.vframes = 0;
	R.aframes = 0;
	pthread_mutex_unlock(&R.lock);
	return re_hprintf(pf, "recorder_stop: ok (%u byte audio, %u frame video)\n",
	                  saved_bytes, saved_vframes);
}

static const struct cmd cmdv[] = {
	{"recorder_start", 0, CMD_PRM, "Avvia registrazione (basepath)", cmd_start},
	{"recorder_stop",  0, 0,       "Chiude i file di registrazione", cmd_stop},
};

/* Per alcune versioni di baresip, un aufilt senza handler di encode non viene instanziato
   affatto per lo stream: dichiariamo encupdh/ench no-op (ritornano 0, non alterano l'audio TX).
   Questo garantisce che decupdh/dech vengano effettivamente chiamati per l'audio in ingresso. */
static int au_encupd(struct aufilt_enc_st **stp, void **ctx, const struct aufilt *af,
                     struct aufilt_prm *prm, const struct audio *au)
{
	(void)ctx; (void)prm; (void)au;
	if (*stp) return 0;
	struct rec_enc *st = mem_zalloc(sizeof(*st), NULL);
	if (!st) return ENOMEM;
	st->af.af = af;
	*stp = &st->af;
	return 0;
}
static int au_enc(struct aufilt_enc_st *st, struct auframe *af)
{
	(void)st; (void)af;
	return 0;   /* nessuna modifica all'audio trasmesso */
}

static struct aufilt af_rec = {
	.name = "recorder",
	.encupdh = au_encupd, .ench = au_enc,
	.decupdh = au_decupd, .dech = au_dec,
};
static int vid_encupd(struct vidfilt_enc_st **stp, void **ctx, const struct vidfilt *vf,
                      struct vidfilt_prm *prm, const struct video *vid)
{
	(void)ctx; (void)prm; (void)vid;
	if (*stp) return 0;
	struct vidfilt_enc_st *st = mem_zalloc(sizeof(*st), NULL);
	if (!st) return ENOMEM;
	st->vf = vf;
	*stp = st;
	return 0;
}
static int vid_enc(struct vidfilt_enc_st *st, struct vidframe *frame, uint64_t *timestamp)
{
	(void)st; (void)frame; (void)timestamp;
	return 0;
}

static struct vidfilt vf_rec = {
	.name = "recorder",
	.encupdh = vid_encupd, .ench = vid_enc,
	.decupdh = vid_decupd, .dech = vid_dec,
};

static int module_init(void)
{
	aufilt_register(baresip_aufiltl(), &af_rec);
	vidfilt_register(baresip_vidfiltl(), &vf_rec);
	return cmd_register(baresip_commands(), cmdv, RE_ARRAY_SIZE(cmdv));
}

static int module_close(void)
{
	cmd_unregister(baresip_commands(), cmdv);
	aufilt_unregister(&af_rec);
	vidfilt_unregister(&vf_rec);
	return 0;
}

const struct mod_export DECL_EXPORTS(recorder) = {
	"recorder", "application", module_init, module_close
};
