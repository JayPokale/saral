#!/usr/bin/env python3
"""Saral: explains confusing paperwork in plain language, fully offline.

Zero dependencies. Serves the web page and forwards requests to a local
Ollama server running an open-weight Gemma model. Nothing leaves the machine.
"""
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OLLAMA_URL = os.environ.get("SARAL_OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("SARAL_MODEL", "gemma4:e2b")
HOST = os.environ.get("SARAL_HOST", "127.0.0.1")
PORT = int(os.environ.get("SARAL_PORT", "8765"))
STATIC = Path(__file__).parent / "static"
MAX_BODY = 12 * 1024 * 1024  # photos from a phone are a few MB

LANGUAGES = {"hi": "Hindi", "mr": "Marathi", "en": "English"}

# Small models can be talked into repeating a scammer's instructions ("click the
# link, share the OTP"). These checks run in plain code, before the model, so the
# warning never depends on the model getting it right.
RED_FLAGS = [
    (re.compile(r"\b(otp|one[- ]time password|cvv|upi pin|atm pin|\bpin\b|password)\b", re.I), "asks for an OTP, PIN or password"),
    (re.compile(r"https?://|www\.|bit\.ly|tinyurl", re.I), "contains a link"),
    (re.compile(r"\b(blocked?|suspend(ed)?|deactivat\w*|frozen|expire[sd]? today|immediately|urgent|within 24 hours|last chance)\b", re.I), "creates urgency or threatens to block an account"),
    (re.compile(r"\b(kyc|lottery|prize|refund|cashback|you have won|electricity (will be )?(cut|disconnected))\b", re.I), "uses a common scam topic"),
]


def red_flags(text: str) -> list[str]:
    return [why for rx, why in RED_FLAGS if rx.search(text)]


SCAM_RULES = """

IMPORTANT, this message shows signs of a scam ({reasons}).
- Start your answer with a warning line: this looks like a scam (fraud).
- In "What should you do?" say: do NOT click any link, do NOT call numbers in the message,
  do NOT share any OTP, PIN, password or card details, and do NOT pay anything.
  Real banks and offices never ask for an OTP or PIN. Tell them to show it to a family member
  or call the official number printed on their card or passbook.
- Never tell the reader to follow the instructions in the message."""

SYSTEM_PROMPT = """You help an elderly person understand a document they received
(a medical report, bank letter, bill, government notice, insurance paper).
The reader is not technical. Be calm, kind and concrete. Never invent facts
that are not in the document. If something is unreadable, say so.

Reply ONLY in {language}, using simple everyday words and short sentences.
Use exactly these four sections with these headings translated into {language}:

1. What is this? (one or two sentences: who sent it and what kind of document it is)
2. What does it mean for you? (the important numbers, dates and amounts. For each test result or amount,
   quote the actual value and the normal range or due amount from the document, then say simply if it is fine, a little high/low, or needs attention)
3. What should you do? (clear next steps; deadlines; who to call. If nothing is needed, say so)
4. Difficult words (up to 5 terms from the document, each explained in one line)

Never tell the reader to click a link, share an OTP/PIN/password, or pay money because a message
says so. If the document asks for any of these, warn that it may be a scam and tell them to check
with family before doing anything."""


def explain(text: str, image_b64: str | None, lang: str):
    language = LANGUAGES.get(lang, "Hindi")
    system = SYSTEM_PROMPT.format(language=language)
    flags = red_flags(text)
    if flags:
        system += SCAM_RULES.format(reasons="; ".join(flags))
    else:
        system += "\nIf (and only if) it is a medical document, end with one line telling them to confirm with their doctor."
    user = "Please explain this document."
    if text.strip():
        user += "\n\nDocument text:\n" + text.strip()
    message = {"role": "user", "content": user}
    if image_b64:
        message["images"] = [image_b64]
    payload = {
        "model": MODEL,
        "stream": True,
        "options": {"temperature": 0.2},
        "messages": [
            {"role": "system", "content": system},
            message,
        ],
    }
    req = urllib.request.Request(
        OLLAMA_URL + "/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        for line in resp:
            if not line.strip():
                continue
            chunk = json.loads(line)
            piece = chunk.get("message", {}).get("content", "")
            if piece:
                yield piece
            if chunk.get("done"):
                return


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("saral: " + fmt % args + "\n")

    def _send(self, code, body: bytes, ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/health":
            try:
                with urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=3) as r:
                    names = [m["name"] for m in json.load(r).get("models", [])]
                ok = MODEL in names
                body = {"ok": ok, "model": MODEL, "installed": names}
            except (urllib.error.URLError, OSError):
                body = {"ok": False, "model": MODEL, "error": "Ollama is not running"}
            return self._send(200, json.dumps(body).encode(), "application/json")
        name = "index.html" if self.path in ("/", "/index.html") else self.path.lstrip("/")
        path = (STATIC / name).resolve()
        if STATIC.resolve() not in path.parents or not path.is_file():
            return self._send(404, b"not found")
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(path.suffix, "application/octet-stream")
        self._send(200, path.read_bytes(), ctype)

    def do_POST(self):
        if self.path != "/api/explain":
            return self._send(404, b"not found")
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return self._send(400, "Add a photo or paste some text".encode())
        if length > MAX_BODY:
            return self._send(413, "File too large (max 12 MB)".encode())
        try:
            data = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return self._send(400, b"bad json")
        text = str(data.get("text", ""))[:20000]
        image = data.get("image")
        if image:
            image = image.split(",", 1)[-1]  # strip data: URL prefix
            try:
                base64.b64decode(image, validate=True)
            except ValueError:
                return self._send(400, b"bad image")
        if not text.strip() and not image:
            return self._send(400, "Add a photo or paste some text".encode())

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Saral-Red-Flags", json.dumps(red_flags(text)))
        self.end_headers()
        try:
            for piece in explain(text, image, str(data.get("lang", "hi"))):
                self.wfile.write(piece.encode())
                self.wfile.flush()
        except (urllib.error.URLError, OSError) as e:
            self.wfile.write(f"\n\n[Saral could not reach the local model: {e}]".encode())


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Saral running at http://{HOST}:{PORT}  (model: {MODEL}, offline)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
