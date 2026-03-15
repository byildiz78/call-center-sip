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
const FREESWITCH_SECRET = process.env.FREESWITCH_SECRET || "JambonzFSw0rd!";
const BRIDGE_WS_URL = process.env.BRIDGE_WS_URL || "ws://127.0.0.1:8081/jambonz-ws";
const SAMPLE_RATE = parseInt(process.env.SAMPLE_RATE || "8000", 10);

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

  // Connect to freeswitch
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
  const from = req.getParsedHeader("From").uri;
  const to = req.getParsedHeader("To").uri;

  logger.info({ callId, from, to }, "Incoming SIP INVITE");

  if (!mediaServer) {
    logger.error("No media server available, rejecting call");
    res.send(503);
    return;
  }

  try {
    // Create endpoint on freeswitch
    const ep = await mediaServer.createEndpoint();
    logger.info({ callId }, "Freeswitch endpoint created");

    // Answer the call with the endpoint's SDP
    const dialog = await srf.createUAS(req, res, {
      localSdp: ep.local.sdp,
    });
    logger.info({ callId }, "Call answered");

    // Connect to AI bridge via WebSocket
    const bridgeWs = new WebSocket(BRIDGE_WS_URL, "audio.jambonz.org");

    bridgeWs.on("open", () => {
      logger.info({ callId }, "Connected to AI bridge");

      // Send session:new message
      bridgeWs.send(
        JSON.stringify({
          type: "session:new",
          callSid: callId,
          from: from,
          to: to,
        })
      );

      // Forward audio from caller to bridge
      ep.on("dtmf", (evt) => {
        logger.info({ callId, dtmf: evt.dtmf }, "DTMF received");
      });

      // Set up audio forking - send RTP audio to bridge as raw PCM
      ep.set("playback_terminators", "none");

      // Use forkAudioStart to capture audio
      ep.forkAudioStart({
        wsUrl: BRIDGE_WS_URL,
        sampling: SAMPLE_RATE,
        mix: "mono",
      }).catch((err) => {
        logger.error({ err: err.message }, "Failed to start audio fork");
      });
    });

    // Receive audio from bridge and play to caller
    bridgeWs.on("message", (data) => {
      if (Buffer.isBuffer(data)) {
        // Raw PCM audio from bridge - play to caller
        ep.play(data).catch(() => {});
      }
    });

    bridgeWs.on("error", (err) => {
      logger.error({ callId, err: err.message }, "Bridge WebSocket error");
    });

    bridgeWs.on("close", () => {
      logger.info({ callId }, "Bridge WebSocket closed");
      dialog.destroy().catch(() => {});
    });

    // Handle call hangup
    dialog.on("destroy", () => {
      logger.info({ callId }, "Call ended");
      ep.forkAudioStop().catch(() => {});
      ep.destroy().catch(() => {});
      if (bridgeWs.readyState === WebSocket.OPEN) {
        bridgeWs.close();
      }
    });
  } catch (err) {
    logger.error({ callId, err: err.message }, "Error handling call");
    res.send(500);
  }
});

logger.info(
  { drachtio: `${DRACHTIO_HOST}:${DRACHTIO_PORT}`, bridge: BRIDGE_WS_URL },
  "SIP call handler starting..."
);
