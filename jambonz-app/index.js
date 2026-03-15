/**
 * Robotpos AI Call Center - SIP Call Handler
 *
 * Uses drachtio + freeswitch to receive SIP calls.
 * Freeswitch connects to the AI bridge via audio fork (WebSocket).
 * Bridge returns audio which is played back via freeswitch.
 */

require("dotenv").config();
const Srf = require("drachtio-srf");
const Mrf = require("drachtio-fsmrf");
const pino = require("pino");

const logger = pino({ level: "info" });

const DRACHTIO_HOST = process.env.DRACHTIO_HOST || "127.0.0.1";
const DRACHTIO_PORT = parseInt(process.env.DRACHTIO_PORT || "9022", 10);
const DRACHTIO_SECRET = process.env.DRACHTIO_SECRET || "cymru";
const FREESWITCH_HOST = process.env.FREESWITCH_HOST || "127.0.0.1";
const FREESWITCH_PORT = parseInt(process.env.FREESWITCH_PORT || "8021", 10);
const FREESWITCH_SECRET = process.env.FREESWITCH_SECRET || "JambonzR0ck$";
const BRIDGE_WS_URL = process.env.BRIDGE_WS_URL || "ws://127.0.0.1:8081/fs-audio";

process.on("uncaughtException", (err) => {
  logger.error({ err: err.message }, "Uncaught exception");
});
process.on("unhandledRejection", (err) => {
  logger.error({ err: err?.message || err }, "Unhandled rejection");
});

const srf = new Srf();
let mrf;
let mediaServer;

srf.connect({
  host: DRACHTIO_HOST,
  port: DRACHTIO_PORT,
  secret: DRACHTIO_SECRET,
});

srf.on("connect", (err, hp) => {
  if (err) {
    logger.error({ err: err.message }, "Failed to connect to drachtio");
    return;
  }
  logger.info({ hp }, "Connected to drachtio");

  mrf = new Mrf(srf);
  mrf.connect({
    address: FREESWITCH_HOST,
    port: FREESWITCH_PORT,
    secret: FREESWITCH_SECRET,
  })
    .then((ms) => {
      mediaServer = ms;
      logger.info("Connected to freeswitch media server");
    })
    .catch((err) => {
      logger.error({ err: err.message }, "Failed to connect to freeswitch");
    });
});

srf.on("error", (err) => {
  logger.error({ err: err.message }, "drachtio connection error");
});

srf.invite(async (req, res) => {
  const callId = req.get("Call-ID");
  const fromHeader = req.getParsedHeader("From");
  const callerNumber = fromHeader.uri || "";

  logger.info({ callId, from: callerNumber }, "Incoming SIP INVITE");

  if (!mediaServer) {
    logger.error("No media server, rejecting");
    return res.send(503);
  }

  let ep, dialog;

  try {
    ep = await mediaServer.createEndpoint();
    logger.info({ callId }, "Endpoint created");

    dialog = await srf.createUAS(req, res, { localSdp: ep.local.sdp });
    logger.info({ callId }, "Call answered");

    // Fork audio to bridge WebSocket
    // This sends caller's audio as L16 PCM via WebSocket
    await ep.forkAudioStart({
      wsUrl: BRIDGE_WS_URL,
      sampling: "8k",
      mix: "mono",
      metadata: JSON.stringify({
        callSid: callId,
        from: callerNumber,
      }),
    });
    logger.info({ callId }, "Audio fork started to bridge");

    // Handle call hangup
    dialog.on("destroy", () => {
      logger.info({ callId }, "Call ended");
      if (ep) {
        ep.forkAudioStop().catch(() => {});
        ep.destroy().catch(() => {});
        ep = null;
      }
    });

  } catch (err) {
    logger.error({ callId, err: err.message }, "Error handling call");
    if (ep) ep.destroy().catch(() => {});
    if (dialog) dialog.destroy().catch(() => {});
    try { res.send(500); } catch (e) {}
  }
});

logger.info(
  { drachtio: `${DRACHTIO_HOST}:${DRACHTIO_PORT}`, bridge: BRIDGE_WS_URL },
  "SIP call handler starting..."
);
