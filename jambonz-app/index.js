/**
 * Robotpos AI Call Center - jambonz Call Handler
 *
 * Receives incoming SIP calls from jambonz and uses the `listen` verb
 * to stream bidirectional audio to the Python AI Bridge server.
 */

require("dotenv").config();
const http = require("http");
const { createEndpoint } = require("@jambonz/node-client-ws");
const pino = require("pino");

const logger = pino({ level: "info" });
const PORT = parseInt(process.env.JAMBONZ_PORT || "3000", 10);
const BRIDGE_WS_URL = process.env.BRIDGE_WS_URL || "ws://127.0.0.1:8081/jambonz-ws";
const SAMPLE_RATE = parseInt(process.env.SAMPLE_RATE || "16000", 10);

const server = http.createServer((req, res) => {
  res.writeHead(200);
  res.end("jambonz call handler running");
});

const makeService = createEndpoint({ server, port: PORT, logger });
const svc = makeService({ path: "/" });

svc.on("session:new", (session) => {
  const callSid = session.call_sid;
  const from = session.data.from || session.from || "unknown";
  const to = session.data.to || session.to || "unknown";

  logger.info({ callSid, from, to }, "Incoming call");

  session.payload = [
    {
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
        callSid: callSid,
        callerNumber: from,
        calledNumber: to,
      },
    },
  ];
  session.send();

  session.on("verb:hook", (msg) => {
    logger.info({ callSid, hook: msg.hook }, "Verb hook received");
    if (msg.hook === "/call-ended") {
      session.payload = [{ verb: "hangup" }];
      session.reply();
    }
  });

  session.on("close", (code, reason) => {
    logger.info({ callSid, code }, "Session closed");
  });

  session.on("error", (err) => {
    logger.error({ callSid, err: err.message }, "Session error");
  });
});

logger.info({ port: PORT, bridge: BRIDGE_WS_URL }, "jambonz call handler started");
