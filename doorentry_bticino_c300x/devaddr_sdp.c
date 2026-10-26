/**
 * devaddr_sdp.c  Modulo baresip che aggiunge l'attributo SDP "DEVADDR" nelle INVITE uscenti.
 *
 * L'app ufficiale BTicino Door Entry aggiunge sempre `a=DEVADDR:<dev><addr>` all'SDP INVITE
 * verso il posto esterno (es: "a=DEVADDR:10" per dev=1 address=0). Senza questo attributo,
 * il citofono C300X risponde ma non invia audio (il video sì, con il trucco sendrecv).
 *
 * Config via env var:
 *   DOORENTRY_SDP_DEVADDR   es: "10", "20"  (default "10")
 */
#include <stdlib.h>
#include <string.h>
#include <re.h>
#include <baresip.h>


static char g_devaddr[32] = "10";


static void inject_devaddr(struct call *call)
{
	if (!call) return;

	/* L'app ufficiale (linphone addCustomSdpAttribute) aggiunge a livello di SESSIONE,
	   non di media. Esiste call_sdp() dopo patch_call_sdp.py; se la patch manca il
	   simbolo e' NULL e usiamo solo il fallback a livello di media.
	   Nota: lo mettiamo in piu' punti perche' non abbiamo garanzia su dove il citofono
	   si aspetta di trovarlo. */
	struct sdp_session *sess = call_sdp(call);
	if (sess) {
		sdp_session_set_lattr(sess, true, "DEVADDR", "%s", g_devaddr);
		info("devaddr_sdp: aggiunto DEVADDR=%s a livello di SESSIONE SDP\n", g_devaddr);
	}

	struct audio *au = call_audio(call);
	if (au) {
		struct sdp_media *m = stream_sdpmedia(audio_strm(au));
		if (m) sdp_media_set_lattr(m, true, "DEVADDR", "%s", g_devaddr);
	}
	struct video *vi = call_video(call);
	if (vi) {
		struct sdp_media *m = stream_sdpmedia(video_strm(vi));
		if (m) sdp_media_set_lattr(m, true, "DEVADDR", "%s", g_devaddr);
	}
}


static void event_handler(enum bevent_ev ev, struct bevent *event, void *arg)
{
	(void)arg;
	if (ev != BEVENT_CALL_LOCAL_SDP) return;
	/* param "offer" = SDP outgoing nostro, "answer" = risposta a un INVITE in arrivo */
	const char *txt = bevent_get_text(event);
	if (!txt || strcmp(txt, "offer") != 0) return;
	inject_devaddr(bevent_get_call(event));
}


static int module_init(void)
{
	const char *v = getenv("DOORENTRY_SDP_DEVADDR");
	if (v && *v) {
		strncpy(g_devaddr, v, sizeof(g_devaddr) - 1);
		g_devaddr[sizeof(g_devaddr) - 1] = 0;
	}
	int err = bevent_register(event_handler, NULL);
	if (err) {
		warning("devaddr_sdp: bevent_register fallito: %d\n", err);
		return err;
	}
	info("devaddr_sdp: modulo caricato (DEVADDR=%s)\n", g_devaddr);
	return 0;
}

static int module_close(void)
{
	bevent_unregister(event_handler);
	return 0;
}


const struct mod_export DECL_EXPORTS(devaddr_sdp) = {
	"devaddr_sdp", "application", module_init, module_close
};
