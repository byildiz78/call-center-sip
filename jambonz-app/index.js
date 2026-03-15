/**
 * Robotpos AI Call Center - jambonz Call Handler
 *
 * Receives incoming SIP calls from jambonz and uses the `listen` verb
 * to stream bidirectional audio to the Python AI Bridge server.
 */

require("dotenv").config();
const { WebSocketServer } = require("ws");
const { createEndpoint } = require("@jambonz/node-client-ws");
const pino = require("pino");

const logger = pino({ level: "info" });
const PORT = parseInt(process.env.JAMBONZ_PORT || "3000", 10);
const BRIDGE_WS_URL = process.env.BRIDGE_WS_URL || "ws://127.0.0.1:8081/jambonz-ws";
const SAMPLE_RATE = parseInt(process.env.SAMPLE_RATE || "16000", 10);

const wss = new WebSocketServer({ port: PORT });

createEndpoint({ ws: wss, logger }, (session) => {
  const { call_sid, from, to, direction } = session;

  logger.info({ call_sid, from, to, direction }, "Incoming call");

  session
    .on("session:new", (evt) => {
      logger.info({ call_sid: evt.call_sid }, "Session started");
    })
    .on("/incoming", (req, res) => {
      // Route the call to the AI Bridge via listen verb
      res.send({
        verb: "listen",
        url: BRIDGE_WS_URL,
        bidirectionalAudio: {
          enabled: true,
          streaming: true,
          sampleRate: SAMPLE_RATE,
        },
        sampleRate: SAMPLE_RATE,
        mixType: "mono",
        actionHook: "/call-ended",
        metadata: {
          callSid: req.call_sid || call_sid,
          callerNumber: req.from || from,
          calledNumber: req.to || to,
        },
      });
    })
    .on("/call-ended", (req, res) => {
      logger.info({ call_sid }, "Call ended via actionHook");
      res.send({ verb: "hangup" });
    })
    .on("close", (code, reason) => {
      logger.info({ call_sid, code, reason: reason?.toString() }, "Session closed");
    })
    .on("error", (err) => {
      logger.error({ call_sid, err: err.message }, "Session error");
    });
});

logger.info({ port: PORT, bridge: BRIDGE_WS_URL }, "jambonz call handler started");
