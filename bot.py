#!/usr/bin/env python3
"""
Claude Code ↔ Telegram bridge
Pilote Claude Code CLI depuis Telegram, 24h/24.
"""

import os, sys, json, time, subprocess, threading, logging, textwrap
from pathlib import Path
from datetime import datetime
import urllib.request, urllib.parse, urllib.error

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "")
ALLOWED_IDS     = set(int(x) for x in os.environ.get("ALLOWED_IDS", "").split(",") if x.strip())
CLAUDE_CMD      = os.environ.get("CLAUDE_CMD", "claude")   # chemin vers le binaire claude
WORKDIR         = os.environ.get("WORKDIR", str(Path.home()))
MAX_MSG_LEN     = 4000   # limite Telegram
POLL_TIMEOUT    = 30     # long-polling timeout (sec)
CLAUDE_TIMEOUT  = 300    # timeout max par requête claude (5 min)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("claude-tg")

# ── Telegram API ──────────────────────────────────────────────────────────────
BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

def tg(method: str, **params) -> dict:
    url = f"{BASE}/{method}"
    data = json.dumps(params).encode()
    req  = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=35) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        log.error("TG HTTP %s: %s", e.code, e.read())
        return {}
    except Exception as e:
        log.error("TG error: %s", e)
        return {}

def send(chat_id: int, text: str, reply_to: int = None):
    """Envoie un message en le découpant si > MAX_MSG_LEN."""
    chunks = textwrap.wrap(text, MAX_MSG_LEN, replace_whitespace=False, break_long_words=True)
    for i, chunk in enumerate(chunks or [""]):
        params = dict(chat_id=chat_id, text=chunk, parse_mode="Markdown")
        if reply_to and i == 0:
            params["reply_to_message_id"] = reply_to
        tg("sendMessage", **params)

def send_typing(chat_id: int):
    tg("sendChatAction", chat_id=chat_id, action="typing")

# ── Claude runner ─────────────────────────────────────────────────────────────
def run_claude(prompt: str, workdir: str = WORKDIR) -> str:
    """Exécute `claude --print '<prompt>'` et retourne stdout."""
    cmd = [CLAUDE_CMD, "--print", prompt]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT,
            cwd=workdir,
        )
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        if result.returncode != 0 and not out:
            return f"⚠️ Erreur (code {result.returncode}):\n```\n{err[:1000]}\n```"
        return out or "_(réponse vide)_"
    except subprocess.TimeoutExpired:
        return f"⏱️ Timeout après {CLAUDE_TIMEOUT}s — essaie une requête plus courte."
    except FileNotFoundError:
        return f"❌ Binaire `{CLAUDE_CMD}` introuvable. Vérifie le PATH ou CLAUDE_CMD dans .env"
    except Exception as e:
        return f"❌ Exception: {e}"

def run_bash(command: str, workdir: str = WORKDIR) -> str:
    """Exécute une commande bash brute (commande /run)."""
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=60, cwd=workdir
        )
        out = result.stdout or ""
        err = result.stderr or ""
        combined = (out + ("\n" + err if err else "")).strip()
        return f"```\n{combined[:3000]}\n```" if combined else "_(pas de sortie)_"
    except subprocess.TimeoutExpired:
        return "⏱️ Timeout bash (60s)"
    except Exception as e:
        return f"❌ {e}"

# ── Sessions par chat ─────────────────────────────────────────────────────────
sessions: dict[int, dict] = {}

def get_session(chat_id: int) -> dict:
    if chat_id not in sessions:
        sessions[chat_id] = {"workdir": WORKDIR, "history": []}
    return sessions[chat_id]

# ── Gestionnaire de messages ──────────────────────────────────────────────────
HELP_TEXT = """
*Claude Code — Telegram Remote*

Envoie n'importe quel message pour interroger Claude.

*Commandes spéciales :*
`/help` — cette aide
`/run <cmd>` — exécuter une commande bash
`/cd <path>` — changer le répertoire de travail
`/pwd` — afficher le répertoire actuel
`/reset` — réinitialiser la session
`/status` — infos système
`/id` — ton Telegram ID (pour le whitelist)

*Exemples :*
`Crée un script Python qui lit un CSV`
`/run ls -la`
`/cd /root/mon-projet`
`Explique le fichier main.py`
""".strip()

def handle_message(msg: dict):
    chat_id  = msg["chat"]["id"]
    user     = msg.get("from", {})
    user_id  = user.get("id")
    username = user.get("username", str(user_id))
    text     = msg.get("text", "").strip()
    msg_id   = msg["message_id"]

    if not text:
        return

    # Commande /id accessible à tous (pour setup whitelist)
    if text == "/id":
        send(chat_id, f"Ton Telegram ID : `{user_id}`", reply_to=msg_id)
        return

    # Whitelist check
    if ALLOWED_IDS and user_id not in ALLOWED_IDS:
        send(chat_id, "⛔ Accès refusé. Ajoute ton ID au whitelist.", reply_to=msg_id)
        log.warning("Accès refusé à user_id=%s (@%s)", user_id, username)
        return

    session = get_session(chat_id)
    log.info("Message de @%s: %s", username, text[:80])

    # ── Commandes ──────────────────────────────────────────────────
    if text == "/help" or text == "/start":
        send(chat_id, HELP_TEXT, reply_to=msg_id)
        return

    if text == "/reset":
        sessions.pop(chat_id, None)
        send(chat_id, "🔄 Session réinitialisée.", reply_to=msg_id)
        return

    if text == "/pwd":
        send(chat_id, f"`{session['workdir']}`", reply_to=msg_id)
        return

    if text == "/status":
        info = run_bash("echo \"User: $(whoami)\" && echo \"Host: $(hostname)\" && echo \"Uptime: $(uptime -p)\" && echo \"Python: $(python3 --version)\" && echo \"Claude: $(claude --version 2>/dev/null || echo 'non trouvé')\"")
        send(chat_id, f"*Statut système*\n{info}", reply_to=msg_id)
        return

    if text.startswith("/cd "):
        path = text[4:].strip()
        expanded = os.path.expanduser(path)
        if os.path.isdir(expanded):
            session["workdir"] = expanded
            send(chat_id, f"📁 Répertoire : `{expanded}`", reply_to=msg_id)
        else:
            send(chat_id, f"❌ Dossier introuvable : `{expanded}`", reply_to=msg_id)
        return

    if text.startswith("/run "):
        cmd = text[5:].strip()
        send_typing(chat_id)
        result = run_bash(cmd, workdir=session["workdir"])
        send(chat_id, result, reply_to=msg_id)
        return

    # ── Requête Claude ─────────────────────────────────────────────
    send_typing(chat_id)

    # Thread pour maintenir le "typing..." pendant que Claude réfléchit
    stop_typing = threading.Event()
    def keep_typing():
        while not stop_typing.is_set():
            tg("sendChatAction", chat_id=chat_id, action="typing")
            time.sleep(4)
    t = threading.Thread(target=keep_typing, daemon=True)
    t.start()

    try:
        reply = run_claude(text, workdir=session["workdir"])
    finally:
        stop_typing.set()

    send(chat_id, reply, reply_to=msg_id)

# ── Boucle principale (long-polling) ─────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN:
        log.error("TELEGRAM_TOKEN manquant. Copie .env.example → .env et remplis-le.")
        sys.exit(1)

    # Vérification du bot
    me = tg("getMe")
    if not me.get("ok"):
        log.error("Token Telegram invalide : %s", me)
        sys.exit(1)
    bot_name = me["result"]["username"]
    log.info("Bot démarré : @%s | Whitelist : %s", bot_name, ALLOWED_IDS or "OUVERTE ⚠️")

    offset = 0
    log.info("En écoute… (Ctrl+C pour arrêter)")

    while True:
        try:
            resp = tg("getUpdates", offset=offset, timeout=POLL_TIMEOUT, allowed_updates=["message"])
            if not resp.get("ok"):
                time.sleep(5)
                continue
            for update in resp.get("result", []):
                offset = update["update_id"] + 1
                if "message" in update:
                    threading.Thread(
                        target=handle_message,
                        args=(update["message"],),
                        daemon=True,
                    ).start()
        except KeyboardInterrupt:
            log.info("Arrêt.")
            sys.exit(0)
        except Exception as e:
            log.error("Erreur boucle : %s", e)
            time.sleep(5)

if __name__ == "__main__":
    main()
