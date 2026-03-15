/**
 * Robotpos AI Call Center - SIP Call Handler
 *
 * Uses drachtio + freeswitch to receive SIP calls and bridge
 * audio to the Python AI Bridge server via WebSocket.
 */

require("dotenv").config();
const Srf = require("drachtio-srf");
const Mrf = require("drachtio-fsmrf");
const WebSocket = require("ws");
const pino = require("pino");

const logger = pino({ level: "info" });

const DRACHTIO_HOST = process.env.DRACHTIO_HOST || "127.0.0.1";
const DRACHTIO_PORT = parseInt(process.env.DRACHTIO_PORT || "9022", 10);
const DRACHTIO_SECRET = process.env.DRACHTIO_SECRET || "cymru";
const FREESWITCH_HOST = process.env.FREESWITCH_HOST || "127.0.0.1";
const FREESWITCH_PORT = parseInt(process.env.FREESWITCH_PORT || "8021", 10);
const FREESWITCH_SECRET = process.env.FREESWITCH_SECRET || "JambonzR0ck$";
const BRIDGE_WS_URL = process.env.BRIDGE_WS_URL || "ws://127.0.0.1:8081/jambonz-ws";

// Prevent crash on uncaught errors
process.on("uncaughtException", (err) => {
  logger.error({ err: err.message, stack: err.stack }, "Uncaught exception (non-fatal)");
});
process.on("unhandledRejection", (err) => {
  logger.error({ err: err?.message || err }, "Unhandled rejection (non-fatal)");
});

const srf = new Srf();
let mrf;
let mediaServer;

// Connect to drachtio
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

// Handle incoming INVITE
srf.invite(async (req, res) => {
  const callId = req.get("Call-ID");
  const fromHeader = req.getParsedHeader("From");
  const callerNumber = fromHeader.uri || "";
  const toHeader = req.getParsedHeader("To");
  const calledNumber = toHeader.uri || "";

  logger.info({ callId, from: callerNumber, to: calledNumber }, "Incoming SIP INVITE");

  if (!mediaServer) {
    logger.error("No media server available, rejecting call");
    res.send(503);
    return;
  }

  let ep, dialog, bridgeWs;

  try {
    // Create endpoint on freeswitch
    ep = await mediaServer.createEndpoint();
    logger.info({ callId }, "Freeswitch endpoint created");

    // Answer the call
    dialog = await srf.createUAS(req, res, {
      localSdp: ep.local.sdp,
    });
    logger.info({ callId }, "Call answered");

    // Connect to AI bridge via WebSocket
    bridgeWs = new WebSocket(BRIDGE_WS_URL, "audio.jambonz.org");

    await new Promise((resolve, reject) => {
      bridgeWs.on("open", resolve);
      bridgeWs.on("error", reject);
      setTimeout(() => reject(new Error("Bridge WS timeout")), 5000);
    });

    logger.info({ callId }, "Connected to AI bridge");

    // Send session:new message
    bridgeWs.send(
      JSON.stringify({
        type: "session:new",
        callSid: callId,
        from: callerNumber,
        to: calledNumber,
      })
    );

    // Fork audio from freeswitch to bridge WebSocket
    try {
      await ep.forkAudioStart({
        wsUrl: BRIDGE_WS_URL,
        sampling: "8k",
        mix: "mono",
      });
      logger.info({ callId }, "Audio fork started");
    } catch (forkErr) {
      logger.warn({ callId, err: forkErr.message }, "forkAudioStart failed, using fallback");
    }

    // Receive audio from bridge and play to caller via freeswitch
    bridgeWs.on("message", (data) => {
      if (Buffer.isBuffer(data) && ep) {
        // Raw PCM audio from bridge
        try {
          ep.conn.execute("playback", `say:${data.toString("base64")}`);
        } catch (e) {}
      }
    });

    bridgeWs.on("close", () => {
      logger.info({ callId }, "Bridge WebSocket closed, ending call");
      if (dialog) dialog.destroy().catch(() => {});
    });

    bridgeWs.on("error", (err) => {
      logger.error({ callId, err: err.message }, "Bridge WebSocket error");
    });

    // Handle call hangup
    dialog.on("destroy", () => {
      logger.info({ callId }, "Call ended by caller");
      cleanup();
    });

  } catch (err) {
    logger.error({ callId, err: err.message }, "Error handling call");
    cleanup();
    try { res.send(500); } catch (e) {}
  }

  function cleanup() {
    if (ep) {
      try { ep.forkAudioStop(); } catch (e) {}
      try { ep.destroy(); } catch (e) {}
      ep = null;
    }
    if (bridgeWs && bridgeWs.readyState === WebSocket.OPEN) {
      bridgeWs.close();
    }
    bridgeWs = null;
    dialog = null;
  }
});

logger.info(
  { drachtio: `${DRACHTIO_HOST}:${DRACHTIO_PORT}`, bridge: BRIDGE_WS_URL },
  "SIP call handler starting..."
);
