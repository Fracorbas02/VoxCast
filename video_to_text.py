#!.venv/bin/python3
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

from concurrent.futures import ProcessPoolExecutor
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
MAX_AUDIO_SECONDS = MAX_AUDIO_HOURS * 3600
MAX_FILE_SIZE_MB = 500
# Bitrate MP3 pour la parole : 128kbps suffit largement pour l'ASR
# et garde les fichiers petits (~55 MB pour 1h).
# On vise 2h45 par chunk pour rester sous la limite de 3h avec une marge.
CHUNK_HOURS = 2.75
CHUNK_SECONDS = int(CHUNK_HOURS * 3600)
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
        "postprocessor_args": ["-threads", "0"],
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

    if duration > MAX_AUDIO_SECONDS:
        print(
            f"Info: vidéo de {duration // 3600}h{duration % 3600 // 60:02d}, "
            f"découpage en chunks de {CHUNK_HOURS}h nécessaire."
        )

    return mp3_path, info


def get_audio_duration_seconds(mp3_path: Path) -> float:
    """Récupère la durée d'un fichier audio en secondes via ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(mp3_path),
        ],
        capture_output=True, text=True,
    )
    return float(result.stdout.strip())


def _split_one_chunk(args: tuple[str, str, str, int, float]) -> tuple[str, float]:
    """Worker exécuté dans un ProcessPool pour découper un chunk."""
    mp3_path, chunk_path, bitrate, _, start = args
    cmd = [
        "ffmpeg", "-y", "-v", "quiet",
        "-i", mp3_path,
        "-ss", str(start),
        "-t", str(CHUNK_SECONDS),
        "-c:a", "libmp3lame",
        "-b:a", bitrate,
        "-threads", "0",
        chunk_path,
    ]
    subprocess.run(cmd, check=True)
    return (chunk_path, start)


def split_audio(mp3_path: Path, output_dir: Path) -> list[tuple[Path, float]]:
    """
    Découpe un fichier audio en chunks de CHUNK_SECONDS secondes.
    Les chunks sont créés en parallèle pour utiliser tous les coeurs CPU.
    Retourne une liste de (chemin_chunk, offset_en_secondes).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    total_duration = get_audio_duration_seconds(mp3_path)

    num_chunks = int(total_duration // CHUNK_SECONDS) + 1
    num_workers = min(num_chunks, os.cpu_count() or 4)
    print(
        f"Découpage en {num_chunks} chunks de ~{CHUNK_HOURS}h "
        f"({num_workers} workers en parallèle)..."
    )

    stem = mp3_path.stem
    tasks = []
    for i in range(num_chunks):
        start = i * CHUNK_SECONDS
        if start >= total_duration:
            break
        chunk_path = output_dir / f"{stem}_part{i+1}.mp3"
        tasks.append((str(mp3_path), str(chunk_path), AUDIO_BITRATE, i, float(start)))

    chunks = []
    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        results = executor.map(_split_one_chunk, tasks)
        for i, (chunk_path_str, start) in enumerate(results, 1):
            chunk_path = Path(chunk_path_str)
            chunk_duration = min(CHUNK_SECONDS, total_duration - start)
            chunks.append((chunk_path, start))
            chunk_size_mb = chunk_path.stat().st_size / (1024 * 1024)
            print(
                f"  Chunk {i}/{len(tasks)}: {chunk_path.name} "
                f"({chunk_size_mb:.1f} MB, {timedelta(seconds=int(chunk_duration))})"
            )

    return chunks


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
    results: list[tuple[dict, float]],
    video_info: dict,
    output_path: Path,
    audio_name: str,
    diarize: bool,
    timestamp_granularities: Optional[list[str]],
) -> Path:
    """
    Sauvegarde une ou plusieurs transcriptions en format Markdown structuré.
    Les timestamps sont ajustés selon l'offset de chaque chunk.

    Args:
        results: liste de (resultat_transcription, offset_seconds)
    """
    first_result = results[0][0] if results else {}
    total_audio_seconds = sum(
        r.get("usage", {}).get("prompt_audio_seconds", 0) for r, _ in results
    )
    total_tokens = sum(
        r.get("usage", {}).get("total_tokens", 0) for r, _ in results
    )

    lines = [
        f"# Transcription: {video_info.get('title', 'Vidéo')}",
        "",
        f"**Source**     : {video_info.get('webpage_url', 'N/A')}",
        f"**Auteur**     : {video_info.get('uploader', 'N/A')}",
        f"**Durée**      : {timedelta(seconds=video_info.get('duration', 0))}",
        f"**Audio**       : {audio_name}",
        f"**Modèle**      : {first_result.get('model', MISTRAL_MODEL)}",
        f"**Langue**      : {first_result.get('language', 'auto-détectée')}",
    ]

    if diarize:
        lines.append(f"**Diarisation** : oui")
    if timestamp_granularities:
        lines.append(f"**Timestamps** : {', '.join(timestamp_granularities)}")
    if len(results) > 1:
        lines.append(f"**Chunks**      : {len(results)}")

    lines.extend([
        "",
        "---",
        "",
    ])

    for result, offset in results:
        segments = result.get("segments", [])

        if segments:
            for seg in segments:
                speaker = seg.get("speaker", "")
                start = seg.get("start", 0.0) + offset
                end = seg.get("end", 0.0) + offset
                text = seg.get("text", "").strip()

                if diarize and speaker:
                    prefix = f"[{format_timestamp(start)}] **{speaker}**:"
                else:
                    prefix = f"[{format_timestamp(start)} - {format_timestamp(end)}]"

                lines.append(f"{prefix}")
                lines.append(f"{text}")
                lines.append("")
        else:
            text = result.get("text", "")
            if offset > 0:
                lines.append(f"_[Suite à partir de {format_timestamp(offset)}]_")
                lines.append("")
            lines.append(text)
            lines.append("")

    lines.extend([
        "---",
        "",
        "## Métriques",
        "",
        f"- Audio traité : {timedelta(seconds=int(total_audio_seconds))}",
        f"- Tokens total : {total_tokens or 'N/A'}",
    ])

    if len(results) > 1:
        lines.append(f"- Chunks transcrits : {len(results)}")

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

    # Découpage si nécessaire
    audio_duration = get_audio_duration_seconds(mp3_path)
    needs_split = audio_duration > MAX_AUDIO_SECONDS

    if needs_split:
        print("\n" + "=" * 60)
        print("ÉTAPE 2a/3 — Découpage audio")
        print("=" * 60)
        chunks = split_audio(mp3_path, TEMP_DIR / "chunks")
        audio_files = chunks  # list of (path, offset_seconds)
    else:
        audio_files = [(mp3_path, 0.0)]

    # Transcription
    print("\n" + "=" * 60)
    print("ÉTAPE 2b/3 — Transcription Mistral Voxtral")
    print("=" * 60)

    timestamp_granularities = [args.timestamps] if args.timestamps else None

    results = []
    try:
        for i, (chunk_path, offset) in enumerate(audio_files, 1):
            if len(audio_files) > 1:
                print(f"\n--- Chunk {i}/{len(audio_files)} ---")

            result = transcribe_audio(
                mp3_path=chunk_path,
                api_key=api_key,
                language=args.language,
                diarize=args.diarize,
                timestamp_granularities=timestamp_granularities,
                context_bias=args.context_bias,
            )
            results.append((result, offset))

            text = result.get("text", "")
            print(f"  Transcrit: {len(text)} caractères")
    except Exception as e:
        sys.exit(f"Erreur de transcription: {e}")
    finally:
        # Nettoyage
        if not args.keep_audio:
            mp3_path.unlink(missing_ok=True)
            if needs_split:
                for chunk_path, _ in audio_files:
                    chunk_path.unlink(missing_ok=True)
                try:
                    (TEMP_DIR / "chunks").rmdir()
                except OSError:
                    pass
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
        results=results,
        video_info=video_info,
        output_path=output_path,
        audio_name=mp3_path.name,
        diarize=args.diarize,
        timestamp_granularities=timestamp_granularities,
    )

    # Aperçu
    print("\n" + "-" * 60)
    print("Aperçu (300 premiers caractères):")
    print("-" * 60)
    full_text = " ".join(r.get("text", "") for r, _ in results)
    print(full_text[:300] + ("..." if len(full_text) > 300 else ""))
    print(f"\nFichier complet: {output_path}")


if __name__ == "__main__":
    main()
