/**
 * owncmd.c — comando baresip per inviare un frame OpenWebNet come SIP MESSAGE al citofono.
 *
 * Registra il comando ctrl_tcp:  owncmd <frame>
 * Invia il frame (es. "*8*92##") come SIP MESSAGE a OWN_PEER (es. sip:c300x@<gw>.bs.iotleg.com),
 * replicando ciò che fa l'app Door Entry per segreteria, luce scale, apertura porta, ecc.
 * Il peer è letto dalla variabile d'ambiente OWN_PEER.
 */
#include <stdlib.h>
#include <re.h>
#include <baresip.h>

static void resp_handler(int err, const struct sip_msg *msg, void *arg)
{
	(void)arg;
	if (err)
		warning("owncmd: risposta errore (%m)\n", err);
	else if (msg)
		info("owncmd: risposta %u %r\n", msg->scode, &msg->reason);
}

static int cmd_owncmd(struct re_printf *pf, void *arg)
{
	const struct cmd_arg *carg = arg;
	const char *peer = getenv("OWN_PEER");
	const char *frame = carg->prm;
	struct ua *ua;
	int err;

	if (!peer || !str_isset(peer))
		return re_hprintf(pf, "owncmd: OWN_PEER non impostato\n");
	if (!frame || !str_isset(frame))
		return re_hprintf(pf, "owncmd: uso: owncmd <frame>\n");

	ua = uag_find_requri(peer);
	if (!ua)
		return re_hprintf(pf, "owncmd: nessun UA per %s\n", peer);

	err = message_send(ua, peer, frame, resp_handler, NULL);
	if (err)
		return re_hprintf(pf, "owncmd: message_send fallito (%m)\n", err);

	return re_hprintf(pf, "owncmd: inviato '%s' a %s\n", frame, peer);
}

static const struct cmd cmdv[] = {
	{"owncmd", 0, CMD_PRM, "Invia frame OpenWebNet via SIP MESSAGE", cmd_owncmd},
};

static int module_init(void)
{
	return cmd_register(baresip_commands(), cmdv, RE_ARRAY_SIZE(cmdv));
}

static int module_close(void)
{
	cmd_unregister(baresip_commands(), cmdv);
	return 0;
}

EXPORT_SYM const struct mod_export DECL_EXPORTS(owncmd) = {
	"owncmd",
	"application",
	module_init,
	module_close,
};
