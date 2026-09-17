#!/usr/bin/env python3
"""
VoxCast - Télécharge l'audio d'une vidéo YouTube et le transcrit en texte
via l'API Mistral Voxtral (voxtral-mini-latest / Voxtral Mini Transcribe 2).

Pipeline:
    URL YouTube → yt-dlp (audio MP3) → Mistral Voxtral API → fichier texte

Usage:
    python video_to_text.py <URL> [options]
    python video_to_text.py "https://youtube.com/watch?v=..." --language fr --diarize
    python video_to_text.py "https://youtube.com/watch?v=..." --timestamps segment
"""

import argparse
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Optional

import yt_dlp


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MISTRAL_MODEL = "voxtral-mini-latest"
MAX_AUDIO_HOURS = 3
MAX_FILE_SIZE_MB = 500
# Bitrate MP3 pour la parole : 128kbps suffit largement pour l'ASR
# et garde les fichiers petits (~55 MB pour 1h).
AUDIO_BITRATE = "128K"

OUTPUT_DIR = Path(__file__).parent / "transcripts"
TEMP_DIR = Path(__file__).parent / "downloads"


# ---------------------------------------------------------------------------
# Téléchargement audio
# ---------------------------------------------------------------------------

def check_ffmpeg() -> bool:
    try:
        subprocess.run(
            ["ffmpeg", "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return True
    except FileNotFoundError:
        return False


def get_video_info(url: str) -> dict:
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
        return ydl.extract_info(url, download=False)


def sanitize_filename(name: str) -> str:
    return "".join(
        c for c in name if c.isalnum() or c in (" ", "_", "-", ".")
    ).rstrip().replace(" ", "_")


def download_audio(url: str, output_dir: Path) -> tuple[Path, dict]:
    """
    Télécharge l'audio d'une vidéo YouTube et le convertit en MP3 128kbps
    (suffisant pour la transcription vocale, taille réduite).

    Returns:
        (chemin_mp3, info_video)
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    info = get_video_info(url)
    title = info.get("title", "video")
    duration = info.get("duration", 0)

    print(f"Titre    : {title}")
    print(f"Auteur   : {info.get('uploader', 'Inconnu')}")
    print(f"Durée    : {timedelta(seconds=duration)}")

    if duration > MAX_AUDIO_HOURS * 3600:
        print(
            f"Attention: la vidéo dure {duration // 3600}h, "
            f"la limite Mistral est de {MAX_AUDIO_HOURS}h par requête."
        )

    filename = sanitize_filename(title)
    ydl_opts = {
        "format": "bestaudio/best",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": AUDIO_BITRATE,
            }
        ],
        "outtmpl": str(output_dir / f"{filename}.%(ext)s"),
        "quiet": False,
        "no_warnings": True,
    }

    print("Téléchargement et conversion MP3...")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    mp3_files = list(output_dir.glob(f"{filename}.mp3"))
    if not mp3_files:
        candidates = list(output_dir.glob(f"{filename}.*"))
        if candidates:
            mp3_files = [candidates[0]]
        else:
            sys.exit("Erreur: fichier audio introuvable après téléchargement.")

    mp3_path = mp3_files[0]
    file_size_mb = mp3_path.stat().st_size / (1024 * 1024)
    print(f"Audio    : {mp3_path.name} ({file_size_mb:.1f} MB)")

    if file_size_mb > MAX_FILE_SIZE_MB:
        sys.exit(
            f"Erreur: fichier de {file_size_mb:.0f} MB, "
            f"la limite Mistral est de {MAX_FILE_SIZE_MB} MB."
        )

    return mp3_path, info


# ---------------------------------------------------------------------------
# Transcription Mistral Voxtral
# ---------------------------------------------------------------------------

def transcribe_audio(
    mp3_path: Path,
    api_key: str,
    language: Optional[str] = None,
    diarize: bool = False,
    timestamp_granularities: Optional[list[str]] = None,
    context_bias: Optional[list[str]] = None,
) -> dict:
    """
    Envoie le fichier MP3 à l'API Mistral Voxtral pour transcription.

    Returns:
        Dictionnaire avec: text, language, segments, usage, model
    """
    from mistralai.client import Mistral

    client = Mistral(api_key=api_key)

    print("Transcription en cours via Mistral Voxtral...")

    kwargs = {
        "model": MISTRAL_MODEL,
        "file": {
            "content": open(mp3_path, "rb"),
            "file_name": mp3_path.name,
        },
    }

    if language:
        kwargs["language"] = language
    if diarize:
        kwargs["diarize"] = True
    if timestamp_granularities:
        kwargs["timestamp_granularities"] = timestamp_granularities
    if context_bias:
        kwargs["context_bias"] = context_bias

    response = client.audio.transcriptions.complete(**kwargs)

    return response.model_dump() if hasattr(response, "model_dump") else dict(response)


# ---------------------------------------------------------------------------
# Sauvegarde
# ---------------------------------------------------------------------------

def format_timestamp(seconds: float) -> str:
    td = timedelta(seconds=seconds)
    hours, remainder = divmod(td.total_seconds(), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{int(secs):02d}"


def save_transcription(
    result: dict,
    video_info: dict,
    output_path: Path,
    mp3_path: Path,
    diarize: bool,
    timestamp_granularities: Optional[list[str]],
) -> Path:
    """Sauvegarde la transcription en format Markdown structuré."""
    lines = [
        f"# Transcription: {video_info.get('title', 'Vidéo')}",
        "",
        f"**Source**     : {video_info.get('webpage_url', 'N/A')}",
        f"**Auteur**     : {video_info.get('uploader', 'N/A')}",
        f"**Durée**      : {timedelta(seconds=video_info.get('duration', 0))}",
        f"**Audio**       : {mp3_path.name}",
        f"**Modèle**      : {result.get('model', MISTRAL_MODEL)}",
        f"**Langue**      : {result.get('language', 'auto-détectée')}",
    ]

    if diarize:
        lines.append(f"**Diarisation** : oui")
    if timestamp_granularities:
        lines.append(f"**Timestamps** : {', '.join(timestamp_granularities)}")

    lines.extend([
        "",
        "---",
        "",
    ])

    segments = result.get("segments", [])

    if segments:
        for seg in segments:
            speaker = seg.get("speaker", "")
            start = seg.get("start", 0.0)
            end = seg.get("end", 0.0)
            text = seg.get("text", "").strip()

            if diarize and speaker:
                prefix = f"[{format_timestamp(start)}] **{speaker}**:"
            else:
                prefix = f"[{format_timestamp(start)} - {format_timestamp(end)}]"

            lines.append(f"{prefix}")
            lines.append(f"{text}")
            lines.append("")
    else:
        lines.append(result.get("text", ""))
        lines.append("")

    usage = result.get("usage", {})
    if usage:
        audio_seconds = usage.get("prompt_audio_seconds", 0)
        lines.extend([
            "---",
            "",
            "## Métriques",
            "",
            f"- Audio traité : {timedelta(seconds=audio_seconds)}",
            f"- Tokens total : {usage.get('total_tokens', 'N/A')}",
        ])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Transcription sauvegardée : {output_path}")
    return output_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="VoxCast - YouTube → Audio → Transcription Mistral Voxtral",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exemples:\n"
            '  python video_to_text.py "https://youtube.com/watch?v=..."\n'
            '  python video_to_text.py "https://youtube.com/watch?v=..." --language fr\n'
            '  python video_to_text.py "https://youtube.com/watch?v=..." --diarize --timestamps segment\n'
        ),
    )
    parser.add_argument("url", help="URL de la vidéo YouTube")
    parser.add_argument(
        "-l", "--language",
        help="Code langue (ex: fr, en, es). Accélère et améliore la précision.",
    )
    parser.add_argument(
        "-d", "--diarize",
        action="store_true",
        help="Active la diarisation (identification des speakers).",
    )
    parser.add_argument(
        "-t", "--timestamps",
        choices=["segment", "word"],
        help="Granularité des timestamps (segment ou word).",
    )
    parser.add_argument(
        "-b", "--context-bias",
        nargs="+",
        help="Termes/noms propres pour guider la transcription (max 100).",
    )
    parser.add_argument(
        "-o", "--output",
        help="Nom du fichier de sortie (sans extension).",
    )
    parser.add_argument(
        "--keep-audio",
        action="store_true",
        help="Conserve le fichier MP3 après transcription.",
    )
    parser.add_argument(
        "--api-key",
        help="Clé API Mistral (sinon lit MISTRAL_API_KEY).",
    )

    args = parser.parse_args()

    # Clé API
    api_key = args.api_key or os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        sys.exit(
            "Erreur: aucune clé API Mistral trouvée.\n"
            "Définissez MISTRAL_API_KEY ou utilisez --api-key."
        )

    # ffmpeg
    if not check_ffmpeg():
        sys.exit("Erreur: ffmpeg requis. Installez-le (apt install ffmpeg).")

    # Téléchargement
    print("=" * 60)
    print("ÉTAPE 1/3 — Téléchargement audio")
    print("=" * 60)
    mp3_path, video_info = download_audio(args.url, TEMP_DIR)

    # Transcription
    print("\n" + "=" * 60)
    print("ÉTAPE 2/3 — Transcription Mistral Voxtral")
    print("=" * 60)

    timestamp_granularities = [args.timestamps] if args.timestamps else None

    try:
        result = transcribe_audio(
            mp3_path=mp3_path,
            api_key=api_key,
            language=args.language,
            diarize=args.diarize,
            timestamp_granularities=timestamp_granularities,
            context_bias=args.context_bias,
        )
    except Exception as e:
        sys.exit(f"Erreur de transcription: {e}")
    finally:
        if not args.keep_audio:
            mp3_path.unlink(missing_ok=True)
            # Nettoyer le dossier downloads s'il est vide
            try:
                TEMP_DIR.rmdir()
            except OSError:
                pass

    # Sauvegarde
    print("\n" + "=" * 60)
    print("ÉTAPE 3/3 — Sauvegarde")
    print("=" * 60)

    output_name = args.output or sanitize_filename(video_info.get("title", "transcription"))
    output_path = OUTPUT_DIR / f"{output_name}.md"

    save_transcription(
        result=result,
        video_info=video_info,
        output_path=output_path,
        mp3_path=mp3_path if args.keep_audio else mp3_path,
        diarize=args.diarize,
        timestamp_granularities=timestamp_granularities,
    )

    # Aperçu
    print("\n" + "-" * 60)
    print("Aperçu (300 premiers caractères):")
    print("-" * 60)
    text = result.get("text", "")
    print(text[:300] + ("..." if len(text) > 300 else ""))
    print(f"\nFichier complet: {output_path}")


if __name__ == "__main__":
    main()
