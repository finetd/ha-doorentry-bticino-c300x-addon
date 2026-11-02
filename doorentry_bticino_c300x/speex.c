/**
 * speex.c  Codec audio Speex (banda stretta, 8 kHz) per baresip.
 *
 * Il citofono C300X offre l'audio come "96 speex/8000" (preferito) e "18 G729".
 * L'app ufficiale, basata su Linphone, negozia Speex. baresip ha rimosso il proprio
 * modulo Speex anni fa: questa e' una riscrittura minimale per l'API attuale.
 *
 * Dipendenza: libspeex (pacchetto Debian libspeex-dev).
 */
#include <stdlib.h>
#include <string.h>
#include <speex/speex.h>
#include <re.h>
#include <rem.h>
#include <baresip.h>

#define SPEEX_FRAME 160   /* campioni per frame a 8 kHz = 20 ms */

struct auenc_state {
	void *enc;
	SpeexBits bits;
};

struct audec_state {
	void *dec;
	SpeexBits bits;
};


static void enc_destructor(void *arg)
{
	struct auenc_state *st = arg;
	speex_bits_destroy(&st->bits);
	if (st->enc) speex_encoder_destroy(st->enc);
}

static void dec_destructor(void *arg)
{
	struct audec_state *st = arg;
	speex_bits_destroy(&st->bits);
	if (st->dec) speex_decoder_destroy(st->dec);
}


static int encode_update(struct auenc_state **aesp, const struct aucodec *ac,
			 struct auenc_param *prm, const char *fmtp)
{
	(void)ac; (void)prm; (void)fmtp;
	if (!aesp) return EINVAL;
	if (*aesp) return 0;

	struct auenc_state *st = mem_zalloc(sizeof(*st), enc_destructor);
	if (!st) return ENOMEM;

	st->enc = speex_encoder_init(&speex_nb_mode);
	if (!st->enc) { mem_deref(st); return ENOMEM; }
	speex_bits_init(&st->bits);

	int quality = 8;   /* default di Linphone */
	speex_encoder_ctl(st->enc, SPEEX_SET_QUALITY, &quality);

	*aesp = st;
	return 0;
}

static int decode_update(struct audec_state **adsp, const struct aucodec *ac,
			 const char *fmtp)
{
	(void)ac; (void)fmtp;
	if (!adsp) return EINVAL;
	if (*adsp) return 0;

	struct audec_state *st = mem_zalloc(sizeof(*st), dec_destructor);
	if (!st) return ENOMEM;

	st->dec = speex_decoder_init(&speex_nb_mode);
	if (!st->dec) { mem_deref(st); return ENOMEM; }
	speex_bits_init(&st->bits);

	int enh = 1;
	speex_decoder_ctl(st->dec, SPEEX_SET_ENH, &enh);

	*adsp = st;
	return 0;
}


static int encode(struct auenc_state *st, bool *marker, uint8_t *buf,
		  size_t *len, int fmt, const void *sampv, size_t sampc)
{
	(void)marker;
	if (!st || !buf || !len || !sampv) return EINVAL;
	if (fmt != AUFMT_S16LE) return ENOTSUP;

	const int16_t *s = sampv;
	speex_bits_reset(&st->bits);
	for (size_t i = 0; i + SPEEX_FRAME <= sampc; i += SPEEX_FRAME)
		speex_encode_int(st->enc, (spx_int16_t *)(s + i), &st->bits);

	int n = speex_bits_write(&st->bits, (char *)buf, (int)*len);
	if (n <= 0) return ENOMEM;
	*len = (size_t)n;
	return 0;
}

static int decode(struct audec_state *st, int fmt, void *sampv, size_t *sampc,
		  bool marker, const uint8_t *buf, size_t len)
{
	(void)marker;
	if (!st || !sampv || !sampc || !buf) return EINVAL;
	if (fmt != AUFMT_S16LE) return ENOTSUP;

	int16_t *out = sampv;
	size_t cap = *sampc, n = 0;

	speex_bits_read_from(&st->bits, (char *)buf, (int)len);
	/* un pacchetto puo' contenere piu' frame (ptime > 20 ms) */
	while (n + SPEEX_FRAME <= cap) {
		if (speex_bits_remaining(&st->bits) < 5) break;
		int ret = speex_decode_int(st->dec, &st->bits, out + n);
		if (ret != 0) break;    /* -1 fine stream, -2 dati corrotti */
		n += SPEEX_FRAME;
	}
	*sampc = n;
	return 0;
}

static int plc(struct audec_state *st, int fmt, void *sampv, size_t *sampc,
	       const uint8_t *buf, size_t len)
{
	(void)buf; (void)len;
	if (!st || !sampv || !sampc) return EINVAL;
	if (fmt != AUFMT_S16LE || *sampc < SPEEX_FRAME) return ENOTSUP;
	speex_decode_int(st->dec, NULL, sampv);   /* ricostruzione frame perso */
	*sampc = SPEEX_FRAME;
	return 0;
}


static struct aucodec speex_nb = {
	.name    = "speex",
	.srate   = 8000,
	.crate   = 8000,
	.ch      = 1,
	.pch     = 1,
	.encupdh = encode_update,
	.ench    = encode,
	.decupdh = decode_update,
	.dech    = decode,
	.plch    = plc,
};

static int module_init(void)
{
	aucodec_register(baresip_aucodecl(), &speex_nb);
	return 0;
}

static int module_close(void)
{
	aucodec_unregister(&speex_nb);
	return 0;
}

const struct mod_export DECL_EXPORTS(speex) = {
	"speex", "codec", module_init, module_close
};
