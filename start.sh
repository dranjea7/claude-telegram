#!/bin/bash
# Démarrage rapide (sans systemd)
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

if [ ! -f "$DIR/.env" ]; then
  echo "❌ Fichier .env manquant. Copie .env.example → .env et remplis-le."
  exit 1
fi

export $(grep -v '^#' "$DIR/.env" | xargs)
exec python3 "$DIR/bot.py"
