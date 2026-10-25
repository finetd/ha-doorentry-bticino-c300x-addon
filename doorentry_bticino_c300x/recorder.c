/**
 * recorder.c  Modulo baresip per registrare la chiamata su file (audio WAV + video Y4M).
 *
 * Esporta:
 *   - un AUPLAY ("recplay") che SOSTITUISCE aubridge come audio player: pulla attivamente
 *     i dati decodificati dalla pipeline (G.729 -> aubuf -> wh) e li scrive su un file WAV
 *     quando la registrazione è attiva. Serve perché aubridge/nil non consuma mai la
 *     pipeline, quindi i filtri audio di decodifica non venivano mai invocati.
 *   - un VIDEO FILTER che si aggancia alla pipeline di decodifica video (push-based,
 *     funziona bene) e scrive i fotogrammi YUV su un file Y4M.
 *   - due comandi:
 *       recorder_start <basepath>   apre <basepath>.wav e <basepath>.y4m, attiva la scrittura
 *       recorder_stop               chiude i file, disattiva la scrittura
 *
 * doorbell.py assembla WAV + Y4M in un MP4 con ffmpeg (già nel container).
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <errno.h>
#include <time.h>
#include <pthread.h>
#include <stdbool.h>
#include <re.h>
#include <rem.h>
#include <baresip.h>


/* ======================================================================= */
/*  Stato condiviso                                                        */
/* ======================================================================= */

static struct {
	pthread_mutex_t lock;
	int active;
	FILE *wav;
	FILE *y4m;
	uint32_t wav_data_bytes;
	uint32_t srate;            /* preso dal primo auplay allocato         */
	uint8_t  ch;
	int      y4m_header_done;
	uint32_t vframes;
	uint32_t aframes;
	char basepath[512];
} R = { .lock = PTHREAD_MUTEX_INITIALIZER };


/* ======================================================================= */
/*  WAV header                                                              */
/* ======================================================================= */

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
	wav_write_u32(f, 16);
	wav_write_u16(f, 1);                /* PCM                          */
	wav_write_u16(f, ch);
	wav_write_u32(f, srate);
	wav_write_u32(f, byte_rate);
	wav_write_u16(f, ch * 2);
	wav_write_u16(f, 16);
	fwrite("data", 1, 4, f);
	wav_write_u32(f, data_bytes);
}


/* ======================================================================= */
/*  AUPLAY recplay: thread che pulla dalla pipeline audio e scrive su WAV   */
/* ======================================================================= */

struct auplay_st {
	const struct auplay *ap;
	pthread_t thread;
	volatile bool run;
	uint32_t srate;
	uint8_t  ch;
	uint32_t ptime;             /* ms per iterazione, tipicamente 20     */
	auplay_write_h *wh;         /* callback per pullare dati             */
	void *arg;
};

static void auplay_destructor(void *arg)
{
	struct auplay_st *st = arg;
	if (st->run) {
		st->run = false;
		pthread_join(st->thread, NULL);
	}
	info("recorder: auplay distrutto\n");
}

static void *auplay_thread(void *data)
{
	struct auplay_st *st = data;
	size_t nsamp = (st->srate * st->ptime) / 1000;
	size_t bytes = nsamp * st->ch * 2;
	int16_t *buf = malloc(bytes);
	if (!buf) return NULL;
	struct auframe af;

	info("recorder: auplay thread avviato (srate=%u ch=%u ptime=%u nsamp=%zu)\n",
	     st->srate, st->ch, st->ptime, nsamp);

	while (st->run) {
		memset(buf, 0, bytes);
		auframe_init(&af, AUFMT_S16LE, buf, nsamp * st->ch,
		             st->srate, st->ch);
		st->wh(&af, st->arg);   /* pull: baresip riempie buf             */

		pthread_mutex_lock(&R.lock);
		if (R.active && R.wav) {
			R.aframes++;
			if (R.aframes <= 3 || R.aframes % 100 == 0)
				info("recorder: audio write #%u, %zu byte\n",
				     R.aframes, bytes);
			if (fwrite(buf, 1, bytes, R.wav) == bytes)
				R.wav_data_bytes += (uint32_t)bytes;
		}
		pthread_mutex_unlock(&R.lock);

		struct timespec ts = { 0, (long)st->ptime * 1000000L };
		nanosleep(&ts, NULL);
	}
	free(buf);
	return NULL;
}

static int recplay_alloc(struct auplay_st **stp, const struct auplay *ap,
                        struct auplay_prm *prm, const char *device,
                        auplay_write_h *wh, void *arg)
{
	(void)device;
	if (!stp || !ap || !prm || !wh) return EINVAL;

	struct auplay_st *st = mem_zalloc(sizeof(*st), auplay_destructor);
	if (!st) return ENOMEM;

	st->ap    = ap;
	st->srate = prm->srate;
	st->ch    = prm->ch;
	st->ptime = prm->ptime ? prm->ptime : 20;
	st->wh    = wh;
	st->arg   = arg;
	st->run   = true;

	pthread_mutex_lock(&R.lock);
	if (!R.srate) { R.srate = st->srate; R.ch = st->ch; }
	pthread_mutex_unlock(&R.lock);

	int err = pthread_create(&st->thread, NULL, auplay_thread, st);
	if (err) {
		mem_deref(st);
		return err;
	}

	info("recorder: auplay alloc OK (srate=%u ch=%u ptime=%u)\n",
	     st->srate, st->ch, st->ptime);

	*stp = st;
	return 0;
}


/* ======================================================================= */
/*  VIDEO FILTER: scrive i fotogrammi YUV in un file Y4M                   */
/* ======================================================================= */

struct rec_vdec { struct vidfilt_dec_st vf; };

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

/* no-op encode handlers: alcuni builds di baresip richiedono entrambi
   per attivare il filtro, anche se noi non modifichiamo il video TX */
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
static int vid_enc(struct vidfilt_enc_st *st, struct vidframe *f, uint64_t *t)
{ (void)st; (void)f; (void)t; return 0; }


/* ======================================================================= */
/*  Comandi recorder_start / recorder_stop                                  */
/* ======================================================================= */

static int cmd_start(struct re_printf *pf, void *arg)
{
	const struct cmd_arg *carg = arg;
	if (!carg || !carg->prm) return re_hprintf(pf, "recorder_start: uso <basepath>\n");

	pthread_mutex_lock(&R.lock);
	if (R.active) {
		pthread_mutex_unlock(&R.lock);
		return re_hprintf(pf, "gia' attivo\n");
	}
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
	wav_write_header(R.wav, R.srate ? R.srate : 8000, R.ch ? R.ch : 1, 0);
	R.wav_data_bytes   = 0;
	R.y4m_header_done  = 0;
	R.vframes          = 0;
	R.aframes          = 0;
	R.active           = 1;
	pthread_mutex_unlock(&R.lock);
	info("recorder: avviato, basepath=%s\n", R.basepath);
	return re_hprintf(pf, "recorder_start: ok %s\n", R.basepath);
}

static int cmd_stop(struct re_printf *pf, void *arg)
{
	(void)arg;
	pthread_mutex_lock(&R.lock);
	if (!R.active) {
		pthread_mutex_unlock(&R.lock);
		return re_hprintf(pf, "non attivo\n");
	}
	R.active = 0;
	if (R.wav) {
		wav_write_header(R.wav, R.srate ? R.srate : 8000,
		                 R.ch ? R.ch : 1, R.wav_data_bytes);
		fclose(R.wav); R.wav = NULL;
	}
	if (R.y4m) { fclose(R.y4m); R.y4m = NULL; }
	uint32_t saved_bytes   = R.wav_data_bytes;
	uint32_t saved_vframes = R.vframes;
	uint32_t saved_aframes = R.aframes;
	R.wav_data_bytes   = 0;
	R.y4m_header_done  = 0;
	R.vframes          = 0;
	R.aframes          = 0;
	pthread_mutex_unlock(&R.lock);
	return re_hprintf(pf, "recorder_stop: ok (%u byte audio in %u iter, %u frame video)\n",
	                  saved_bytes, saved_aframes, saved_vframes);
}

static const struct cmd cmdv[] = {
	{"recorder_start", 0, CMD_PRM, "Avvia registrazione (basepath)", cmd_start},
	{"recorder_stop",  0, 0,       "Chiude i file di registrazione", cmd_stop},
};


/* ======================================================================= */
/*  Registrazioni                                                           */
/* ======================================================================= */

static struct auplay  *recplay_ap = NULL;
static struct vidfilt  vf_rec = {
	.name    = "recorder",
	.encupdh = vid_encupd, .ench = vid_enc,
	.decupdh = vid_decupd, .dech = vid_dec,
};

static int module_init(void)
{
	int err = auplay_register(&recplay_ap, baresip_auplayl(),
	                          "recplay", recplay_alloc);
	if (err) {
		warning("recorder: auplay_register fallito: %d\n", err);
		return err;
	}
	vidfilt_register(baresip_vidfiltl(), &vf_rec);
	info("recorder: modulo caricato (auplay=recplay, vidfilt=recorder)\n");
	return cmd_register(baresip_commands(), cmdv, RE_ARRAY_SIZE(cmdv));
}

static int module_close(void)
{
	cmd_unregister(baresip_commands(), cmdv);
	vidfilt_unregister(&vf_rec);
	recplay_ap = mem_deref(recplay_ap);
	return 0;
}

const struct mod_export DECL_EXPORTS(recorder) = {
	"recorder", "application", module_init, module_close
};
