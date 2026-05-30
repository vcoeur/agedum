import fs from "node:fs";
import os from "node:os";
import path from "node:path";

/**
 * Claude Code hook (shipped + auto-injected by agedum): emit the session
 * transcript in-band as a neutral OSC escape, so a terminal capturer (condash,
 * `script`, tmux, asciinema) can recover a clean transcript from Claude's
 * alternate-screen TUI instead of scraping the repainted grid.
 *
 * Registered as two hooks in a project `.claude/settings.json` agedum binds in:
 *   - `UserPromptSubmit` (argv "user"): the submitted prompt arrives on stdin as
 *     `.prompt`; framed as a `[user]` message.
 *   - `Stop` (argv "stop"): after each assistant turn, the new tail of the
 *     session JSONL at `.transcript_path` is read (from a per-session byte
 *     checkpoint) and each assistant `text` / `thinking` block is framed.
 *
 * Same neutral protocol as the opencode plugin (assets/opencode/transcript-osc.js):
 *   ESC ] 7373 ; agent-transcript ; <frameId> ; <i> ; <n> ; <base64piece> BEL
 *   { "v":1, "t":"msg", "sid", "role":"user"|"assistant"|"reasoning", "text" }
 * so condash's harness-blind extractor decodes Claude exactly like opencode.
 *
 * Writes go ONLY to /dev/tty (the capturer's pty). Critically, a UserPromptSubmit
 * hook's stdout is injected into Claude's context — so this script never writes to
 * stdout, and always exits 0, so a logging hiccup can never disrupt the session.
 */

const PREFIX = "\x1b]7373;agent-transcript;";
const BEL = "\x07";
const MAX_PIECE = 1024;

let frameCounter = 0;

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

function readStdin() {
  try {
    return fs.readFileSync(0, "utf8");
  } catch {
    return "";
  }
}

/** Per-session byte checkpoint file: how far into the JSONL we've already framed. */
function offsetPath(sid) {
  const dir = path.join(os.tmpdir(), "agedum-claude-transcript");
  fs.mkdirSync(dir, { recursive: true });
  const safe = String(sid || "default").replace(/[^a-zA-Z0-9_-]/g, "_");
  return path.join(dir, `${safe}.offset`);
}

/** Frame the new assistant + thinking content appended since the last checkpoint. */
function emitStop(input) {
  const tp = input.transcript_path;
  if (!tp || !fs.existsSync(tp)) return;
  const sid = input.session_id;
  const op = offsetPath(sid);

  let offset = 0;
  try {
    offset = parseInt(fs.readFileSync(op, "utf8"), 10) || 0;
  } catch {
    offset = 0;
  }

  const size = fs.statSync(tp).size;
  if (offset > size) offset = 0; // file rotated/truncated — re-read from start
  if (size <= offset) return;

  const fd = fs.openSync(tp, "r");
  let consumed = offset;
  try {
    const buf = Buffer.alloc(size - offset);
    fs.readSync(fd, buf, 0, buf.length, offset);
    const chunk = buf.toString("utf8");
    // Only consume up to the last complete line; leave any partial tail for next time.
    const lastNl = chunk.lastIndexOf("\n");
    if (lastNl < 0) return;
    const complete = chunk.slice(0, lastNl);
    consumed = offset + Buffer.byteLength(complete, "utf8") + 1;

    for (const line of complete.split("\n")) {
      if (!line.trim()) continue;
      let obj;
      try {
        obj = JSON.parse(line);
      } catch {
        continue; // skip non-JSON / partial / meta lines
      }
      if (obj.type !== "assistant") continue; // user/tool lines: skip (prompt framed live)
      const content = obj.message?.content ?? obj.content;
      if (!Array.isArray(content)) continue;
      for (const block of content) {
        if (block?.type === "thinking" && block.thinking) {
          emitFrame({ v: 1, t: "msg", sid, role: "reasoning", text: block.thinking });
        } else if (block?.type === "text" && block.text) {
          emitFrame({ v: 1, t: "msg", sid, role: "assistant", text: block.text });
        }
      }
    }
  } finally {
    fs.closeSync(fd);
  }

  try {
    fs.writeFileSync(op, String(consumed));
  } catch {
    /* checkpoint best-effort — a re-read at worst re-frames a turn */
  }
}

function main() {
  const mode = process.argv[2];
  let input = {};
  try {
    input = JSON.parse(readStdin() || "{}");
  } catch {
    input = {};
  }

  if (mode === "user") {
    const text = input.prompt;
    if (typeof text === "string" && text.length > 0) {
      emitFrame({ v: 1, t: "msg", sid: input.session_id, role: "user", text });
    }
  } else if (mode === "stop") {
    emitStop(input);
  }
}

try {
  main();
} catch {
  /* never disrupt the harness over logging */
}
process.exit(0);
