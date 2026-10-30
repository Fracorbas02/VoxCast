#!/usr/bin/env python3
"""
Serveur MCP (Model Context Protocol) de VoxCast — transport stdio.

Protocole : JSON-RPC 2.0, un objet JSON par ligne sur stdin/stdout
(transport stdio de la spec MCP). Aucune dépendance externe : on s'appuie
sur video_to_text.py pour tout le pipeline.

Outils exposés :
  - list_transcripts              : liste les transcriptions .md disponibles
  - get_transcript(name)          : renvoie le contenu Markdown d'une transcription
  - search_transcripts(query)     : recherche plein texte dans les transcriptions
  - summarize(name)               : génère (ou recharge) le résumé IA d'une transcription
  - transcribe(source, ...)       : lance le pipeline complet (URL YouTube ou fichier local)

Configuration :
  - clé API    : champ "api_key" de settings.json, ou variable MISTRAL_API_KEY
  - dossier    : champ "transcripts_dir" de settings.json, ou ./transcripts

Lancement (ex. depuis un client MCP) :
  { "mcpServers": { "voxcast": { "command": "python3",
      "args": ["/chemin/vers/mcp_server.py"] } } }
"""

import json
import os
import re
import sys
import threading
from pathlib import Path
from typing import Optional

APP_DIR = Path(__file__).parent
sys.path.insert(0, str(APP_DIR))

import video_to_text as vtt  # noqa: E402

SERVER_NAME = "voxcast"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2025-06-18"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def load_config() -> dict:
    cfg = {
        "api_key": os.environ.get("MISTRAL_API_KEY", ""),
        "transcripts_dir": vtt.OUTPUT_DIR,
    }
    try:
        settings = json.loads((APP_DIR / "settings.json").read_text(encoding="utf-8"))
        cfg["api_key"] = cfg["api_key"] or settings.get("api_key", "")
        tdir = settings.get("transcripts_dir")
        if tdir:
            p = Path(tdir)
            if p.parent.exists():
                cfg["transcripts_dir"] = p
    except Exception:
        pass
    return cfg


CONFIG = load_config()


def log(msg: str):
    """Log sur stderr (stdout est réservé au protocole MCP)."""
    print(f"[voxcast-mcp] {msg}", file=sys.stderr, flush=True)


def send(obj: dict):
    """Écrit une réponse JSON-RPC sur stdout (NDJSON), sans jamais planter
    si le client a fermé le flux."""
    try:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except (BrokenPipeError, ValueError, OSError):
        pass


# ---------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------


def _transcripts() -> list[dict]:
    tdir = CONFIG["transcripts_dir"]
    out = []
    if not tdir.exists():
        return out
    for f in sorted(tdir.glob("*.md"), key=lambda p: p.stat().st_mtime,
                    reverse=True):
        st = f.stat()
        out.append({"name": f.stem, "path": str(f), "size": st.st_size,
                    "modified": st.st_mtime})
    return out


def tool_list_transcripts(_args: dict) -> str:
    items = _transcripts()
    if not items:
        return "Aucune transcription pour le moment."
    lines = [f"{i+1}. {t['name']} ({t['size']} octets)" for i, t in enumerate(items)]
    return "\n".join(lines)


def tool_get_transcript(args: dict) -> str:
    name = str(args.get("name", "")).strip()
    max_chars = int(args.get("max_chars", 20000))
    if not name:
        return "Erreur : paramètre 'name' requis."
    path = CONFIG["transcripts_dir"] / f"{name}.md"
    if not path.exists():
        # tolère un nom avec ou sans extension
        matches = [t for t in _transcripts() if name in t["name"]]
        if not matches:
            return f"Transcription introuvable : {name}"
        path = Path(matches[0]["path"])
    text = path.read_text(encoding="utf-8")
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n... [tronqué, {len(text)} caractères au total]"
    return text


def tool_search_transcripts(args: dict) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        return "Erreur : paramètre 'query' requis."
    q = query.lower()
    results = []
    for t in _transcripts():
        try:
            text = Path(t["path"]).read_text(encoding="utf-8")
        except Exception:
            continue
        for m in re.finditer(re.escape(q), text.lower()):
            start = max(0, m.start() - 120)
            end = min(len(text), m.end() + 120)
            snippet = text[start:end].replace("\n", " ")
            results.append(f"**{t['name']}** : …{snippet}…")
            if len(results) >= 10:
                break
        if len(results) >= 10:
            break
    if not results:
        return f"Aucun résultat pour « {query} »."
    return "\n".join(results)


def tool_summarize(args: dict) -> str:
    name = str(args.get("name", "")).strip()
    if not name:
        return "Erreur : paramètre 'name' requis."
    if not CONFIG["api_key"]:
        return "Erreur : aucune clé API Mistral configurée."
    path = CONFIG["transcripts_dir"] / f"{name}.md"
    if not path.exists():
        return f"Transcription introuvable : {name}"
    text = path.read_text(encoding="utf-8")
    sdir = CONFIG["transcripts_dir"] / ".summaries"
    sdir.mkdir(parents=True, exist_ok=True)
    spath = sdir / f"{name}.md"
    # réutilise le résumé persisté s'il est à jour
    try:
        st = path.stat()
        first = spath.read_text(encoding="utf-8").split("\n", 1)[0]
        m = re.match(r"<!--\s*voxcast:summary\s+mtime=([\d.]+)\s+size=(\d+)\s*-->", first)
        if m and (float(m.group(1)), int(m.group(2))) == (st.st_mtime, st.st_size):
            return spath.read_text(encoding="utf-8").split("\n", 1)[-1]
    except Exception:
        pass
    md = vtt.summarize_text(CONFIG["api_key"], text)
    header = f"<!-- voxcast:summary mtime={st.st_mtime} size={st.st_size} -->\n"
    spath.write_text(header + md, encoding="utf-8")
    return md


def tool_transcribe(args: dict) -> str:
    source = str(args.get("source", "")).strip()
    if not source:
        return "Erreur : paramètre 'source' requis (URL YouTube ou chemin local)."
    if not CONFIG["api_key"]:
        return "Erreur : aucune clé API Mistral configurée."
    is_local = not re.match(r"^https?://", source)
    if is_local and not Path(source).exists():
        return f"Erreur : fichier introuvable : {source}"
    try:
        out = vtt.run_pipeline(
            source=source,
            api_key=CONFIG["api_key"],
            language=args.get("language") or None,
            diarize=bool(args.get("diarize", False)),
            output_name=args.get("output_name") or None,
            is_local_file=is_local,
            transcripts_dir=CONFIG["transcripts_dir"],
            model=args.get("model") or None,
            base_url=args.get("base_url") or None,
            model_api_key=args.get("model_api_key") or None,
            progress_callback=lambda f, msg: log(f"{f:.0%} {msg}"),
        )
        return f"Transcription terminée : {out}"
    except Exception as e:
        return f"Erreur de transcription : {e}"


TOOLS = [
    {
        "name": "list_transcripts",
        "description": "Liste les transcriptions (.md) disponibles dans le dossier de VoxCast.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_transcript",
        "description": "Renvoie le contenu Markdown d'une transcription (paramètre 'name').",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Nom de la transcription (sans .md)"},
                "max_chars": {"type": "integer", "description": "Tronque au-delà de ce nombre de caractères (défaut 20000)"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "search_transcripts",
        "description": "Recherche plein texte dans toutes les transcriptions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Texte recherché"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "summarize",
        "description": "Génère (ou recharge depuis le cache) le résumé IA Mistral d'une transcription existante.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Nom de la transcription (sans .md)"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "transcribe",
        "description": "Transcrit une URL YouTube (ou vidéo/playlist) ou un fichier audio local via l'API Voxtral, et sauvegarde le .md.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "URL (YouTube, vidéo ou playlist) ou chemin de fichier local"},
                "language": {"type": "string", "description": "Langue ISO (ex. 'fr', 'en') ou 'auto'"},
                "output_name": {"type": "string", "description": "Nom de sortie personnalisé (sans extension)"},
                "diarize": {"type": "boolean", "description": "Identification des locuteurs"},
                "model": {"type": "string", "description": "ID de modèle de transcription (défaut : voxtral-small-transcribe-3-0)"},
                "base_url": {"type": "string", "description": "URL d'API personnalisée (endpoint compatible OpenAI)"},
                "model_api_key": {"type": "string", "description": "Clé API pour un endpoint personnalisé"},
            },
            "required": ["source"],
        },
    },
]

TOOL_HANDLERS = {
    "list_transcripts": tool_list_transcripts,
    "get_transcript": tool_get_transcript,
    "search_transcripts": tool_search_transcripts,
    "summarize": tool_summarize,
    "transcribe": tool_transcribe,
}


# ---------------------------------------------------------------------------
# Boucle JSON-RPC (stdio, NDJSON)
# ---------------------------------------------------------------------------

# transcribe peut être long : on exécute les tools/call dans un thread pour
# ne jamais bloquer la lecture de stdin.
_pending: dict[int, threading.Thread] = {}


def handle_request(req: dict) -> Optional[dict]:
    """Traite une requête JSON-RPC. Retourne la réponse, ou None pour les
    notifications (pas de réponse)."""
    method = req.get("method", "")
    rid = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        client_ver = params.get("protocolVersion", PROTOCOL_VERSION)
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "protocolVersion": client_ver if client_ver else PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None

    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            return {
                "jsonrpc": "2.0", "id": rid,
                "result": {"content": [{"type": "text",
                                        "text": f"Outil inconnu : {name}"}],
                           "isError": True},
            }

        def run():
            try:
                text = handler(args)
                result = {"content": [{"type": "text", "text": text}]}
            except Exception as e:
                log(f"erreur outil {name}: {e}")
                result = {"content": [{"type": "text",
                                       "text": f"Erreur : {e}"}],
                          "isError": True}
            resp = {"jsonrpc": "2.0", "id": rid, "result": result}
            send(resp)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        if rid is not None:
            _pending[rid] = t
        return None  # réponse envoyée de manière asynchrone

    if rid is not None:
        return {
            "jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"Méthode inconnue : {method}"},
        }
    return None


def main():
    log(f"démarrage — transcripts_dir={CONFIG['transcripts_dir']} "
        f"api_key={'ok' if CONFIG['api_key'] else 'MANQUANTE'}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(req, list):  # batch JSON-RPC
            for r in req:
                resp = handle_request(r)
                if resp is not None:
                    send(resp)
            continue
        resp = handle_request(req)
        if resp is not None:
            send(resp)
    # attendre la fin des appels d'outils en cours
    for t in list(_pending.values()):
        t.join(timeout=5)
    log("arrêt")


if __name__ == "__main__":
    main()
