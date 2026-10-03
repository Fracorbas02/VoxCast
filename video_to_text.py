#!/usr/bin/env python3
"""
VoxCast - Télécharge l'audio d'une vidéo YouTube et le transcrit en texte
via l'API Mistral Voxtral (voxtral-mini-latest / Voxtral Mini Transcribe 2).

Pipeline:
    URL YouTube → yt-dlp (audio MP3) → Mistral Voxtral API → fichier texte
    Fichier local → ffmpeg (audio MP3) → Mistral Voxtral API → fichier texte

Usage:
    python video_to_text.py <URL> [options]
    python video_to_text.py "https://youtube.com/watch?v=..." --language fr --diarize
    python video_to_text.py "https://youtube.com/watch?v=..." --timestamps segment
    python video_to_text.py /path/to/audio.mp3 --language fr
"""

from concurrent.futures import ProcessPoolExecutor
import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
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
CHUNK_HOURS = 2.75
CHUNK_SECONDS = int(CHUNK_HOURS * 3600)
AUDIO_BITRATE = "128K"
COST_PER_MIN = 0.003
MAX_RETRIES = 3
RETRY_DELAY = 5  # secondes

# Facteur de vitesse initial : secondes de transcription par seconde d'affilée
# d'audio. Ajusté dynamiquement à chaque chunk transcrit.
TRANSCRIBE_SPEED_DEFAULT = 0.15

OUTPUT_DIR = Path(__file__).parent / "transcripts"
TEMP_DIR = Path(__file__).parent / "downloads"
CACHE_FILE = Path(__file__).parent / ".cache.json"


# ---------------------------------------------------------------------------
# Progression
# ---------------------------------------------------------------------------

class ProgressReporter:
    """
    Anime la progression d'un pipeline entre les jalons réels.

    Les jalons réels (octets téléchargés, ffmpeg, chunks transcrits) fixent
    un plancher. Entre deux jalons (ex. pendant une requête API), un ticker
    fait avancer la fraction lentement vers un plafond laissé volontairement
    sous la cible (marge) : la barre bouge pour montrer l'activité mais
    n'atteint jamais 100% avant la fin réelle.
    """

    def __init__(self, callback=None):
        # callback(fraction: float, message: str)
        self._callback = callback
        self._frac = 0.0
        self._ceiling = 1.0
        self._rate = 0.0
        self._message = ""
        self._stop = threading.Event()
        self._thread = None

    def set(self, frac: Optional[float] = None, ceiling: Optional[float] = None,
            rate: Optional[float] = None, message: Optional[str] = None):
        """Met à jour jalons/plafond/message et émet immédiatement."""
        if frac is not None:
            self._frac = max(self._frac, frac)
        if ceiling is not None:
            # Affectation directe : chaque phase définit son propre plafond.
            self._ceiling = max(ceiling, self._frac)
        if rate is not None:
            self._rate = rate
        if message is not None:
            self._message = message
        self._emit()

    def start_ticker(self):
        """Démarre l'avancement progressif (1 tick/seconde)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def _tick():
            while not self._stop.wait(1.0):
                if self._rate > 0 and self._frac < self._ceiling:
                    self._frac = min(self._frac + self._rate, self._ceiling)
                    self._emit()

        self._thread = threading.Thread(target=_tick, daemon=True)
        self._thread.start()

    def stop_ticker(self):
        self._stop.set()
        if self._thread:
            self._thread.join()
            self._thread = None

    def _emit(self):
        if self._callback:
            try:
                self._callback(self._frac, self._message)
            except Exception:
                pass


def fmt_duration(seconds: float) -> str:
    """Formate une durée pour un humain : '1 min 20 s', '2 h 05 min'..."""
    seconds = max(int(seconds), 0)
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min {seconds % 60:02d} s".replace(" 00 s", "")
    return f"{seconds // 3600} h {(seconds % 3600) // 60:02d} min"


def fmt_size(num_bytes: float) -> str:
    for unit in ["o", "Ko", "Mo", "Go"]:
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} To"


def run_ffmpeg_with_progress(cmd: list, total_duration: float, on_progress=None):
    """
    Exécute ffmpeg en capturant sa progression réelle via -progress pipe:1.
    on_progress(fraction) est appelé avec la fraction du fichier traité.
    """
    proc = subprocess.Popen(
        cmd + ["-nostats", "-progress", "pipe:1", "-v", "quiet"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    for line in proc.stdout:
        if line.startswith("out_time_ms=") and on_progress and total_duration > 0:
            try:
                out_seconds = float(line.strip().split("=")[1]) / 1_000_000
            except ValueError:
                continue
            if out_seconds > 0:
                on_progress(min(out_seconds / total_duration, 1.0))
    proc.wait()
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd)


# ---------------------------------------------------------------------------
# Cache des métadonnées
# ---------------------------------------------------------------------------

def load_cache() -> dict:
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cache(cache: dict):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


def cache_key(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()


def get_video_info_cached(url: str, use_cache: bool = True) -> dict:
    cache = load_cache()
    key = cache_key(url)
    if use_cache and key in cache:
        print("(infos depuis le cache)")
        return cache[key]
    print("(récupération des infos vidéo...)")
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    cache[key] = info
    save_cache(cache)
    return info


def estimate_cost(duration_seconds: float) -> float:
    return (duration_seconds / 60) * COST_PER_MIN


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


def sanitize_filename(name: str) -> str:
    return "".join(
        c for c in name if c.isalnum() or c in (" ", "_", "-", ".")
    ).rstrip().replace(" ", "_")


def download_audio(url: str, output_dir: Path, use_cache: bool = True,
                   phase_progress=None) -> tuple[Path, dict]:
    """
    Télécharge l'audio d'une vidéo YouTube et le convertit en MP3 128kbps.
    phase_progress(frac_0_1, message) est appelé pendant le téléchargement.
    Returns: (chemin_mp3, info_video)
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    info = get_video_info_cached(url, use_cache)
    title = info.get("title", "video")
    duration = info.get("duration", 0)
    print(f"Titre    : {title}")
    print(f"Auteur   : {info.get('uploader', 'Inconnu')}")
    print(f"Durée    : {timedelta(seconds=duration)}")
    print(f"Coût est. : ${estimate_cost(duration):.2f}")

    if duration > MAX_AUDIO_HOURS * 3600:
        print(f"Attention: la vidéo dure {duration // 3600}h, "
              f"la limite Mistral est de {MAX_AUDIO_HOURS}h par requête.")

    filename = sanitize_filename(title)

    def _hook(d):
        if not phase_progress:
            return
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes", 0)
            if total > 0:
                frac = min(done / total, 1.0)
                phase_progress(
                    frac,
                    f"Téléchargement {frac * 100:.0f}% "
                    f"({fmt_size(done)} / {fmt_size(total)})",
                )
        elif status == "finished":
            phase_progress(0.97, "Conversion MP3...")

    ydl_opts = {
        "format": "bestaudio/best",
        "postprocessor_args": ["-threads", "0"],
        "postprocessors": [{"key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": AUDIO_BITRATE}],
        "outtmpl": str(output_dir / f"{filename}.%(ext)s"),
        "quiet": False, "no_warnings": True,
        "progress_hooks": [_hook],
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
    return mp3_path, info


def prepare_local_audio(audio_path: Path, output_dir: Path,
                        start: Optional[float] = None,
                        end: Optional[float] = None,
                        phase_progress=None) -> tuple[Path, dict]:
    """
    Prépare un fichier audio local : conversion en MP3 128kbps si nécessaire,
    et extraction d'un segment si start/end sont spécifiés.
    phase_progress(frac_0_1, message) est appelé pendant ffmpeg.
    Returns: (chemin_mp3, info_dict)
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    audio_path = Path(audio_path)

    if not audio_path.exists():
        sys.exit(f"Erreur: fichier introuvable: {audio_path}")

    duration = get_audio_duration_seconds(audio_path)
    title = audio_path.stem

    info = {
        "title": title,
        "uploader": "Fichier local",
        "duration": int(duration),
        "webpage_url": str(audio_path),
    }

    def _ffmpeg_progress(label: str):
        def _on_frac(frac: float):
            phase_progress(frac, f"{label} {frac * 100:.0f}%")
        return _on_frac

    if start is not None or end is not None:
        seg_start = start or 0
        seg_end = end or duration
        seg_duration = seg_end - seg_start
        print(f"Extraction du segment {format_timestamp(seg_start)} - {format_timestamp(seg_end)} "
              f"({timedelta(seconds=int(seg_duration))})")
        print(f"Coût est. : ${estimate_cost(seg_duration):.2f}")
        info["duration"] = int(seg_duration)
        info["title"] = f"{title}_{format_timestamp(seg_start)}-{format_timestamp(seg_end)}"

        mp3_path = output_dir / f"{sanitize_filename(info['title'])}.mp3"
        cmd = [
            "ffmpeg", "-y",
            "-i", str(audio_path),
            "-ss", str(seg_start),
            "-to", str(seg_end),
            "-c:a", "libmp3lame", "-b:a", AUDIO_BITRATE,
            "-threads", "0",
            str(mp3_path),
        ]
        run_ffmpeg_with_progress(cmd, seg_duration, _ffmpeg_progress("Extraction du segment"))
    elif audio_path.suffix.lower() == ".mp3":
        mp3_path = audio_path
        print(f"Coût est. : ${estimate_cost(duration):.2f}")
    else:
        mp3_path = output_dir / f"{sanitize_filename(title)}.mp3"
        print(f"Conversion en MP3 {AUDIO_BITRATE}...")
        print(f"Coût est. : ${estimate_cost(duration):.2f}")
        cmd = [
            "ffmpeg", "-y",
            "-i", str(audio_path),
            "-c:a", "libmp3lame", "-b:a", AUDIO_BITRATE,
            "-threads", "0",
            str(mp3_path),
        ]
        run_ffmpeg_with_progress(cmd, duration, _ffmpeg_progress("Conversion"))

    file_size_mb = mp3_path.stat().st_size / (1024 * 1024)
    print(f"Audio    : {mp3_path.name} ({file_size_mb:.1f} MB)")
    return mp3_path, info


def get_audio_duration_seconds(audio_path: Path) -> float:
    """Récupère la durée d'un fichier audio en secondes via ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "quiet",
         "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1",
         str(audio_path)],
        capture_output=True, text=True,
    )
    return float(result.stdout.strip())


# ---------------------------------------------------------------------------
# Découpage
# ---------------------------------------------------------------------------

def _split_one_chunk(args: tuple[str, str, str, int, float]) -> tuple[str, float]:
    mp3_path, chunk_path, bitrate, _, start = args
    cmd = [
        "ffmpeg", "-y", "-v", "quiet",
        "-i", mp3_path,
        "-ss", str(start),
        "-t", str(CHUNK_SECONDS),
        "-c:a", "libmp3lame", "-b:a", bitrate,
        "-threads", "0",
        chunk_path,
    ]
    subprocess.run(cmd, check=True)
    return (chunk_path, start)


def split_audio(mp3_path: Path, output_dir: Path,
                start: Optional[float] = None,
                end: Optional[float] = None,
                phase_progress=None) -> list[tuple[Path, float]]:
    """
    Découpe un fichier audio en chunks de CHUNK_SECONDS secondes.
    Si start/end sont donnés, ne découpe que ce segment.
    phase_progress(frac_0_1, message) est appelé à chaque chunk produit.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    total_duration = get_audio_duration_seconds(mp3_path)

    if start is not None:
        actual_start = start
    else:
        actual_start = 0
    if end is not None:
        actual_end = min(end, total_duration)
    else:
        actual_end = total_duration

    segment_duration = actual_end - actual_start
    if segment_duration <= MAX_AUDIO_SECONDS:
        # Pas besoin de découper, un seul chunk
        return [(mp3_path, actual_start)]

    num_chunks = int(segment_duration // CHUNK_SECONDS) + 1
    num_workers = min(num_chunks, os.cpu_count() or 4)
    print(f"Découpage en {num_chunks} chunks de ~{CHUNK_HOURS}h "
          f"({num_workers} workers en parallèle)...")

    stem = mp3_path.stem
    tasks = []
    for i in range(num_chunks):
        chunk_start = actual_start + i * CHUNK_SECONDS
        if chunk_start >= actual_end:
            break
        chunk_path = output_dir / f"{stem}_part{i+1}.mp3"
        tasks.append((str(mp3_path), str(chunk_path), AUDIO_BITRATE, i, float(chunk_start)))

    chunks = []
    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        results = executor.map(_split_one_chunk, tasks)
        for i, (chunk_path_str, chunk_start) in enumerate(results, 1):
            chunk_path = Path(chunk_path_str)
            chunk_dur = min(CHUNK_SECONDS, actual_end - chunk_start)
            chunks.append((chunk_path, chunk_start))
            chunk_mb = chunk_path.stat().st_size / (1024 * 1024)
            print(f"  Chunk {i}/{len(tasks)}: {chunk_path.name} "
                  f"({chunk_mb:.1f} MB, {timedelta(seconds=int(chunk_dur))})")
            if phase_progress:
                phase_progress(i / len(tasks), f"Découpage {i}/{len(tasks)}")
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
    from mistralai.client import Mistral
    client = Mistral(api_key=api_key)
    print(f"Transcription en cours via Mistral Voxtral...")
    kwargs = {
        "model": MISTRAL_MODEL,
        "file": {"content": open(mp3_path, "rb"), "file_name": mp3_path.name},
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


def transcribe_with_retry(
    mp3_path: Path,
    api_key: str,
    language: Optional[str] = None,
    diarize: bool = False,
    timestamp_granularities: Optional[list[str]] = None,
    context_bias: Optional[list[str]] = None,
    max_retries: int = MAX_RETRIES,
) -> dict:
    """Transcription avec retry automatique en cas d'échec."""
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            return transcribe_audio(
                mp3_path=mp3_path,
                api_key=api_key,
                language=language,
                diarize=diarize,
                timestamp_granularities=timestamp_granularities,
                context_bias=context_bias,
            )
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                print(f"  Erreur (tentative {attempt}/{max_retries}): {e}")
                print(f"  Nouvel essai dans {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)
            else:
                raise


# ---------------------------------------------------------------------------
# Sauvegarde
# ---------------------------------------------------------------------------

def format_timestamp(seconds: float) -> str:
    td = timedelta(seconds=seconds)
    hours, remainder = divmod(td.total_seconds(), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{int(secs):02d}"


def _format_srt_timestamp(seconds: float) -> str:
    """Format SRT: HH:MM:SS,mmm"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _format_vtt_timestamp(seconds: float) -> str:
    """Format VTT: HH:MM:SS.mmm"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def save_transcription(
    results: list[tuple[dict, float]],
    video_info: dict,
    output_path: Path,
    audio_name: str,
    diarize: bool,
    timestamp_granularities: Optional[list[str]],
) -> Path:
    """Sauvegarde la transcription en Markdown structuré."""
    first_result = results[0][0] if results else {}
    total_audio_seconds = sum(
        r.get("usage", {}).get("prompt_audio_seconds", 0) for r, _ in results)
    total_tokens = sum(
        r.get("usage", {}).get("total_tokens", 0) for r, _ in results)

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
    lines.extend(["", "---", ""])

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
        "---", "",
        "## Métriques", "",
        f"- Audio traité : {timedelta(seconds=int(total_audio_seconds))}",
        f"- Tokens total : {total_tokens or 'N/A'}",
    ])
    if len(results) > 1:
        lines.append(f"- Chunks transcrits : {len(results)}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Transcription sauvegardée : {output_path}")
    return output_path


def export_srt(results: list[tuple[dict, float]], output_path: Path) -> Path:
    """Exporte la transcription en format SRT (sous-titres)."""
    entries = []
    idx = 1
    for result, offset in results:
        segments = result.get("segments", [])
        if segments:
            for seg in segments:
                start = seg.get("start", 0.0) + offset
                end = seg.get("end", 0.0) + offset
                text = seg.get("text", "").strip()
                if text:
                    entries.append((idx, start, end, text))
                    idx += 1
        else:
            # Sans segments, on ne peut pas faire de SRT précis
            text = result.get("text", "").strip()
            if text:
                # Estimer des segments grossiers (non idéal)
                duration = result.get("usage", {}).get("prompt_audio_seconds", 0)
                entries.append((idx, offset, offset + duration, text))
                idx += 1

    lines = []
    for idx, start, end, text in entries:
        lines.append(str(idx))
        lines.append(f"{_format_srt_timestamp(start)} --> {_format_srt_timestamp(end)}")
        lines.append(text)
        lines.append("")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"SRT exporté : {output_path}")
    return output_path


def export_vtt(results: list[tuple[dict, float]], output_path: Path) -> Path:
    """Exporte la transcription en format VTT (WebVTT)."""
    entries = []
    idx = 1
    for result, offset in results:
        segments = result.get("segments", [])
        if segments:
            for seg in segments:
                start = seg.get("start", 0.0) + offset
                end = seg.get("end", 0.0) + offset
                text = seg.get("text", "").strip()
                if text:
                    entries.append((idx, start, end, text))
                    idx += 1
        else:
            text = result.get("text", "").strip()
            if text:
                duration = result.get("usage", {}).get("prompt_audio_seconds", 0)
                entries.append((idx, offset, offset + duration, text))
                idx += 1

    lines = ["WEBVTT", ""]
    for idx, start, end, text in entries:
        lines.append(f"{_format_vtt_timestamp(start)} --> {_format_vtt_timestamp(end)}")
        lines.append(text)
        lines.append("")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"VTT exporté : {output_path}")
    return output_path


# ---------------------------------------------------------------------------
# Pipeline complet (réutilisable par CLI et GUI)
# ---------------------------------------------------------------------------

def run_pipeline(
    source: str,
    api_key: str,
    language: Optional[str] = None,
    diarize: bool = False,
    timestamp_granularities: Optional[list[str]] = None,
    context_bias: Optional[list[str]] = None,
    output_name: Optional[str] = None,
    keep_audio: bool = False,
    start: Optional[float] = None,
    end: Optional[float] = None,
    use_cache: bool = True,
    is_local_file: bool = False,
    transcripts_dir: Optional[Path] = None,
    export_srt_file: bool = False,
    export_vtt_file: bool = False,
    progress_callback=None,
    log_callback=None,
    cancel_check=None,
) -> Path:
    """
    Pipeline complet : download/local → split → transcribe → save.

    progress_callback(fraction: float, message: str) reçoit la progression
    globale [0..1] calée sur ce qui se passe réellement (octets téléchargés,
    avancement ffmpeg, chunks transcrits) avec un message explicite.
    Entre deux jalons fiables (ex. requête API), la barre continue d'avancer
    lentement vers un plafond avec marge pour montrer l'activité.

    Returns: chemin du fichier de transcription.
    """
    def _log(msg):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    rep = ProgressReporter(progress_callback)
    out_dir = transcripts_dir or OUTPUT_DIR

    try:
        # --- Étape 1 : Audio (URL: 0-35%, local: 0-15%) ---
        if cancel_check and cancel_check():
            raise InterruptedError("Annulé par l'utilisateur")

        if is_local_file:
            audio_start, audio_end = 0.0, 0.15
            _log("Préparation du fichier audio local...")
            rep.set(0.0, ceiling=audio_end, rate=0.005,
                    message="Préparation du fichier audio...")
            rep.start_ticker()
            mp3_path, video_info = prepare_local_audio(
                Path(source), TEMP_DIR, start, end,
                phase_progress=lambda f, msg: rep.set(
                    audio_start + (audio_end - audio_start) * min(f, 1.0), message=msg),
            )
        else:
            audio_start, audio_end = 0.0, 0.35
            _log("Téléchargement audio...")
            rep.set(0.0, ceiling=audio_end, rate=0.002,
                    message="Récupération des métadonnées...")
            rep.start_ticker()
            mp3_path, video_info = download_audio(
                source, TEMP_DIR, use_cache,
                phase_progress=lambda f, msg: rep.set(
                    audio_start + (audio_end - audio_start) * min(f, 1.0), message=msg),
            )
            # Si segment demandé sur vidéo YouTube, on découpe après le download
            if start is not None or end is not None:
                total_dur = get_audio_duration_seconds(mp3_path)
                seg_start = start or 0
                seg_end = end or total_dur
                seg_path = TEMP_DIR / f"{mp3_path.stem}_segment.mp3"
                _log(f"Extraction segment {format_timestamp(seg_start)} - {format_timestamp(seg_end)}")
                cmd = ["ffmpeg", "-y",
                       "-i", str(mp3_path),
                       "-ss", str(seg_start),
                       "-to", str(seg_end),
                       "-c:a", "libmp3lame", "-b:a", AUDIO_BITRATE,
                       "-threads", "0", str(seg_path)]
                run_ffmpeg_with_progress(
                    cmd, seg_end - seg_start,
                    lambda f: rep.set(audio_end - 0.02 + 0.02 * f,
                                      message=f"Extraction du segment {format_timestamp(seg_start)} - "
                                              f"{format_timestamp(seg_end)}"))
                mp3_path.unlink(missing_ok=True)
                mp3_path = seg_path
                video_info["duration"] = int(seg_end - seg_start)

        rep.set(audio_end)

        # --- Étape 2a : Découpage si nécessaire (+5%) ---
        if cancel_check and cancel_check():
            raise InterruptedError("Annulé par l'utilisateur")

        audio_duration = get_audio_duration_seconds(mp3_path)
        needs_split = audio_duration > MAX_AUDIO_SECONDS
        split_start, split_end = audio_end, audio_end + 0.05

        if needs_split:
            _log("Découpage audio...")
            rep.set(split_start, message="Découpage audio...")
            chunks = split_audio(
                mp3_path, TEMP_DIR / "chunks",
                phase_progress=lambda f, msg: rep.set(
                    split_start + (split_end - split_start) * f, message=msg),
            )
            if cancel_check and cancel_check():
                raise InterruptedError("Annulé par l'utilisateur")
            audio_files = chunks
        else:
            audio_files = [(mp3_path, 0.0)]

        rep.set(split_end)

        # --- Étape 2b : Transcription (→ 95%), avec estimation du temps restant ---
        transcribe_start = split_end
        transcribe_end = 0.95
        transcribe_span = transcribe_end - transcribe_start

        chunk_durations = []
        for c, _ in audio_files:
            try:
                chunk_durations.append(get_audio_duration_seconds(c))
            except Exception:
                chunk_durations.append(0.0)
        total_audio = sum(chunk_durations) or 1.0

        # Vitesse apprise : secondes de transcription par seconde d'audio,
        # ajustée à chaque chunk terminé (lissage 50/50).
        speed = TRANSCRIBE_SPEED_DEFAULT
        results = []
        total = len(audio_files)
        acc = 0.0
        for i, (chunk_path, offset) in enumerate(audio_files, 1):
            if cancel_check and cancel_check():
                raise InterruptedError("Annulé par l'utilisateur")

            chunk_dur = chunk_durations[i - 1]
            chunk_span = transcribe_span * (chunk_dur / total_audio)
            chunk_base = transcribe_start + transcribe_span * (acc / total_audio)
            est = max(chunk_dur * speed, 1.0)
            remaining = est + (total_audio - acc - chunk_dur) * speed
            label = "Transcription" if total == 1 else f"Transcription chunk {i}/{total}"
            rep.set(chunk_base,
                    ceiling=chunk_base + chunk_span * 0.9,  # marge sous la cible
                    rate=(chunk_span * 0.9) / est,          # arriver "à l'heure"
                    message=f"{label} — ~{fmt_duration(remaining)} restantes")
            rep.start_ticker()

            if total > 1:
                _log(f"Transcription chunk {i}/{total}...")
            t0 = time.monotonic()
            result = transcribe_with_retry(
                mp3_path=chunk_path,
                api_key=api_key,
                language=language,
                diarize=diarize,
                timestamp_granularities=timestamp_granularities,
                context_bias=context_bias,
            )
            elapsed = time.monotonic() - t0
            if chunk_dur > 0:
                speed = 0.5 * speed + 0.5 * (elapsed / chunk_dur)

            rep.stop_ticker()
            acc += chunk_dur
            rep.set(chunk_base + chunk_span)

            results.append((result, offset))
            text = result.get("text", "")
            _log(f"Chunk {i}/{total} transcrit: {len(text)} caractères")

        # --- Étape 3 : Sauvegarde (95% → 100%) ---
        _log("Sauvegarde...")
        rep.set(transcribe_end, message="Sauvegarde...")

        name = output_name or sanitize_filename(video_info.get("title", "transcription"))
        output_path = out_dir / f"{name}.md"

        save_transcription(
            results=results,
            video_info=video_info,
            output_path=output_path,
            audio_name=mp3_path.name,
            diarize=diarize,
            timestamp_granularities=timestamp_granularities,
        )

        # Export SRT/VTT
        if export_srt_file:
            srt_path = out_dir / f"{name}.srt"
            export_srt(results, srt_path)
        if export_vtt_file:
            vtt_path = out_dir / f"{name}.vtt"
            export_vtt(results, vtt_path)

        rep.set(1.0, message="Terminé")

    finally:
        rep.stop_ticker()

    # Nettoyage : uniquement les fichiers temporaires, jamais la source locale
    if not keep_audio:
        temp_dir = TEMP_DIR.resolve()
        if mp3_path.resolve().is_relative_to(temp_dir):
            mp3_path.unlink(missing_ok=True)
        if needs_split:
            for c, _ in audio_files:
                if c.resolve().is_relative_to(temp_dir):
                    c.unlink(missing_ok=True)
            try:
                (TEMP_DIR / "chunks").rmdir()
            except OSError:
                pass
            try:
                TEMP_DIR.rmdir()
            except OSError:
                pass

    full_text = " ".join(r.get("text", "") for r, _ in results)
    preview = full_text[:200] + ("..." if len(full_text) > 200 else "")
    _log(f"Aperçu: {preview}")
    _log(f"Fichier: {output_path}")

    return output_path


def parse_timestamp_arg(value: str) -> float:
    """Parse un timestamp 'HH:MM:SS', 'MM:SS' ou 'SS' en secondes."""
    if not value:
        return None
    parts = value.split(":")
    if len(parts) == 1:
        return float(parts[0])
    elif len(parts) == 2:
        return float(parts[0]) * 60 + float(parts[1])
    elif len(parts) == 3:
        return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    raise ValueError(f"Format de timestamp invalide: {value}")


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
            '  python video_to_text.py /path/to/audio.mp3 --language fr\n'
            '  python video_to_text.py "https://youtube.com/watch?v=..." --start 10:00 --end 30:00\n'
            '  python video_to_text.py "https://youtube.com/watch?v=..." --srt\n'
        ),
    )
    parser.add_argument("source", help="URL YouTube ou chemin vers un fichier audio local")
    parser.add_argument("-l", "--language", help="Code langue (fr, en, es...)")
    parser.add_argument("-d", "--diarize", action="store_true", help="Diarisation")
    parser.add_argument("-t", "--timestamps", choices=["segment", "word"], help="Granularité timestamps")
    parser.add_argument("-b", "--context-bias", nargs="+", help="Termes (max 100)")
    parser.add_argument("-o", "--output", help="Nom du fichier de sortie (sans extension)")
    parser.add_argument("--keep-audio", action="store_true", help="Conserve le MP3")
    parser.add_argument("--api-key", help="Clé API Mistral")
    parser.add_argument("--start", help="Début du segment (HH:MM:SS, MM:SS ou secondes)")
    parser.add_argument("--end", help="Fin du segment (HH:MM:SS, MM:SS ou secondes)")
    parser.add_argument("--no-cache", action="store_true", help="Désactive le cache des métadonnées")
    parser.add_argument("--srt", action="store_true", help="Exporte aussi en SRT")
    parser.add_argument("--vtt", action="store_true", help="Exporte aussi en VTT")
    parser.add_argument("--file", action="store_true", help="Source = fichier audio local (auto-détecté sinon)")

    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        sys.exit("Erreur: aucune clé API Mistral trouvée.\n"
                 "Définissez MISTRAL_API_KEY ou utilisez --api-key.")
    if not check_ffmpeg():
        sys.exit("Erreur: ffmpeg requis. Installez-le (apt install ffmpeg).")

    is_local = args.file or not args.source.startswith("http")

    start = parse_timestamp_arg(args.start) if args.start else None
    end = parse_timestamp_arg(args.end) if args.end else None

    timestamp_granularities = [args.timestamps] if args.timestamps else None

    run_pipeline(
        source=args.source,
        api_key=api_key,
        language=args.language,
        diarize=args.diarize,
        timestamp_granularities=timestamp_granularities,
        context_bias=args.context_bias,
        output_name=args.output,
        keep_audio=args.keep_audio,
        start=start,
        end=end,
        use_cache=not args.no_cache,
        is_local_file=is_local,
        export_srt_file=args.srt,
        export_vtt_file=args.vtt,
    )


if __name__ == "__main__":
    main()
