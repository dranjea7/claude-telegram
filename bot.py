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
SESSIONS_DIR    = Path("/root/claude-telegram/sessions")  # persistance sur disque

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
    """Envoie un message en le découpant si > MAX_MSG_LEN. Fallback sans Markdown si erreur de parsing."""
    chunks = textwrap.wrap(text, MAX_MSG_LEN, replace_whitespace=False, break_long_words=True)
    for i, chunk in enumerate(chunks or [""]):
        params = dict(chat_id=chat_id, text=chunk, parse_mode="Markdown")
        if reply_to and i == 0:
            params["reply_to_message_id"] = reply_to
        result = tg("sendMessage", **params)
        # Si Telegram rejette le Markdown, on renvoie en texte brut
        if not result.get("ok"):
            params.pop("parse_mode", None)
            tg("sendMessage", **params)

def send_typing(chat_id: int):
    tg("sendChatAction", chat_id=chat_id, action="typing")

# ── Historique de conversation (persistant sur disque) ───────────────────────
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

def session_file(chat_id: int) -> Path:
    return SESSIONS_DIR / f"{chat_id}.json"

def load_history(chat_id: int) -> list:
    f = session_file(chat_id)
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            return []
    return []

def save_history(chat_id: int, history: list):
    try:
        session_file(chat_id).write_text(json.dumps(history, ensure_ascii=False, indent=2))
    except Exception as e:
        log.error("Sauvegarde historique échouée : %s", e)

def build_prompt(history: list, new_message: str) -> str:
    """Construit un prompt avec tout l'historique de la conversation."""
    if not history:
        return new_message
    lines = ["Voici l'intégralité de notre conversation (contexte, ne la résume pas) :\n"]
    for entry in history:
        lines.append(f"Utilisateur : {entry['user']}")
        lines.append(f"Toi : {entry['assistant']}\n")
    lines.append(f"Utilisateur : {new_message}")
    return "\n".join(lines)

def add_to_history(chat_id: int, session: dict, user_msg: str, assistant_msg: str):
    """Ajoute un échange à l'historique et le persiste sur disque."""
    session["history"].append({"user": user_msg, "assistant": assistant_msg})
    save_history(chat_id, session["history"])

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
        sessions[chat_id] = {"workdir": WORKDIR, "history": load_history(chat_id)}
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
`/reset` — effacer la mémoire et réinitialiser
`/memory` — voir les échanges mémorisés
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
        f = session_file(chat_id)
        if f.exists():
            f.unlink()
        send(chat_id, "🔄 Mémoire complète effacée. Nouvelle conversation.", reply_to=msg_id)
        return

    if text == "/memory":
        history = session.get("history", [])
        if not history:
            send(chat_id, "🧠 Aucun historique pour l'instant.", reply_to=msg_id)
        else:
            lines = [f"🧠 Mémoire complète : {len(history)} échange(s) enregistrés\n"]
            for i, entry in enumerate(history, 1):
                u = entry['user']
                a = entry['assistant']
                lines.append(f"{i}. Toi : {u[:80]}{'…' if len(u)>80 else ''}")
                lines.append(f"   Claude : {a[:80]}{'…' if len(a)>80 else ''}\n")
            send(chat_id, "\n".join(lines), reply_to=msg_id)
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

    # ── Requête Claude avec mémoire ────────────────────────────────
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
        prompt = build_prompt(session.get("history", []), text)
        reply = run_claude(prompt, workdir=session["workdir"])
    finally:
        stop_typing.set()

    # Mémoriser l'échange (persisté sur disque)
    add_to_history(chat_id, session, text, reply)

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
