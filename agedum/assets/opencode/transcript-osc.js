import fs from "node:fs";

/**
 * opencode plugin (shipped + auto-injected by agedum): emit the session
 * transcript in-band as a neutral OSC escape.
 *
 * opencode runs as a full-screen alternate-screen TUI, so a terminal capturer
 * (condash, `script`, tmux, asciinema) only ever sees the current frame — the
 * conversation that scrolls inside the TUI is repainted, never retained. This
 * plugin streams each finalized message into the terminal stream as an OSC
 * escape the terminal ignores for display, so any capturer can recover a clean
 * transcript without parsing escape-sequence redraws.
 *
 * Neutral by design: the protocol names no specific viewer, so the plugin has
 * no dependency on condash (or any consumer).
 *
 * Protocol:
 *   ESC ] 7373 ; agent-transcript ; <frameId> ; <i> ; <n> ; <base64piece> BEL
 * A frame's base64 payload is split into `n` pieces (kept small so each write
 * stays under a pty's atomic-write size); the receiver reassembles by frameId
 * and base64-decodes a JSON frame:
 *   { "v":1, "t":"msg", "sid", "mid", "role":"user"|"assistant", "text" }
 *   { "v":1, "t":"end", "sid" }
 *
 * Writes go to /dev/tty (the controlling terminal = the capturer's pty),
 * independent of however opencode wires the plugin's own stdout.
 */

const PREFIX = "\x1b]7373;agent-transcript;";
const BEL = "\x07";
const MAX_PIECE = 1024;

let frameCounter = 0;
const roles = new Map();
const emitted = new Set();

function ttyWrite(str) {
  try {
    fs.writeFileSync("/dev/tty", str);
  } catch {
    /* no controlling tty (headless / piped) — nothing to capture; ignore */
  }
}

function emitFrame(frame) {
  const b64 = Buffer.from(JSON.stringify(frame), "utf8").toString("base64");
  const id = (frameCounter++).toString(36);
  const n = Math.ceil(b64.length / MAX_PIECE) || 1;
  let out = "";
  for (let i = 0; i < n; i++) {
    out += `${PREFIX}${id};${i};${n};${b64.slice(i * MAX_PIECE, (i + 1) * MAX_PIECE)}${BEL}`;
  }
  ttyWrite(out);
}

export const TranscriptOscPlugin = async () => {
  return {
    event: async ({ event }) => {
      try {
        if (event.type === "message.updated") {
          const info = event.properties?.info;
          if (info?.id && info?.role) roles.set(info.id, info.role);
          return;
        }
        if (event.type === "message.part.updated") {
          const part = event.properties?.part;
          if (!part || part.type !== "text" || part.synthetic) return;
          const done = part.time && typeof part.time.end === "number";
          if (!done || emitted.has(part.id)) return;
          emitted.add(part.id);
          emitFrame({
            v: 1,
            t: "msg",
            sid: part.sessionID,
            mid: part.messageID,
            role: roles.get(part.messageID) || "assistant",
            text: part.text || "",
          });
          return;
        }
        if (event.type === "session.idle") {
          emitFrame({ v: 1, t: "end", sid: event.properties?.sessionID });
        }
      } catch {
        /* never break the harness over logging */
      }
    },
  };
};

export default TranscriptOscPlugin;
