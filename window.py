#!/usr/bin/env python3
"""
VoxCast - Fenêtre principale GTK4
Interface moderne avec GTK4 + Libadwaita pour une intégration native sous GNOME.
"""

import html
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")

from gi.repository import Gtk, Gdk, Gio, GLib, Adw, Pango, Gst

import video_to_text as vtt

_GST_INIT = [False]


class GstPlayer:
    """Lecteur audio via playbin (GStreamer legacy).

    Gtk.MediaFile passait par decodebin3, qui plante sur certaines
    configurations (assertion mq_slot_handle_stream_start) ; playbin
    évite ce chemin. L'interface imite Gtk.MediaFile (timestamps en µs).
    """

    def __init__(self, path: Path):
        if not _GST_INIT[0]:
            Gst.init(None)
            _GST_INIT[0] = True
        self._pipe = Gst.ElementFactory.make("playbin", None)
        if self._pipe is None:
            raise RuntimeError("playbin indisponible (GStreamer)")
        self._pipe.set_property("uri", path.absolute().as_uri())

    def play(self):
        self._pipe.set_state(Gst.State.PLAYING)

    def set_paused(self, paused: bool):
        self._pipe.set_state(Gst.State.PAUSED if paused else Gst.State.PLAYING)

    def get_paused(self) -> bool:
        return self._pipe.get_state(0)[1] == Gst.State.PAUSED

    def get_timestamp(self) -> int:  # µs
        ok, pos = self._pipe.query_position(Gst.Format.TIME)
        return pos // 1000 if ok and pos >= 0 else 0

    def get_duration(self) -> int:  # µs
        ok, dur = self._pipe.query_duration(Gst.Format.TIME)
        return dur // 1000 if ok and dur >= 0 else 0

    def stop(self):
        self._pipe.set_state(Gst.State.NULL)


# ============================================================================
# CONFIGURATION
# ============================================================================
APP_ID = "com.voxcast.app"
APP_DIR = Path(__file__).parent
DEFAULT_TRANSCRIPTS_DIR = APP_DIR / "transcripts"
QUEUE_FILE = APP_DIR / "queue.json"  # file d'attente persistée (reprise après crash)
LOG_FILE = APP_DIR / "voxccast.log"
VERSION = "1.1.0"

# Les logs vont dans le shell d'où l'app est lancée + dans voxcast.log.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)

LANGUAGES = [
    ("auto", "Auto-détection"),
    ("fr", "Français"),
    ("en", "Anglais"),
    ("es", "Espagnol"),
    ("de", "Allemand"),
    ("it", "Italien"),
    ("pt", "Portugais"),
    ("nl", "Néerlandais"),
    ("ru", "Russe"),
    ("ja", "Japonais"),
    ("zh", "Chinois"),
    ("ar", "Arabe"),
    ("hi", "Hindi"),
]

TIMESTAMP_OPTIONS = [
    ("none", "Aucun"),
    ("segment", "Par segment"),
    ("word", "Par mot"),
]

# Statuts de la file d'attente
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"

STATUS_KEYS = [STATUS_PENDING, STATUS_RUNNING, STATUS_DONE, STATUS_ERROR,
               STATUS_CANCELLED]


def status_label(status: str) -> str:
    """Libellé traduit d'un statut (évalué à la volée selon la langue)."""
    return {
        STATUS_PENDING: _("En attente"),
        STATUS_RUNNING: _("En cours"),
        STATUS_DONE: _("Terminé"),
        STATUS_ERROR: _("Erreur"),
        STATUS_CANCELLED: _("Annulé"),
    }.get(status, status)


PILL_CLASSES = [f"pill-{s}" for s in STATUS_KEYS]

# ============================================================================
# INTERNATIONALISATION (FR / EN)
# ============================================================================
LANG = "fr"


def _(s: str) -> str:
    """Traduit une chaîne selon la langue courante (FR par défaut).
    Utilise globals() car TRANSLATIONS est remplie après cette définition."""
    return globals().get("TRANSLATIONS", {}).get(LANG, {}).get(s, s)


TRANSLATIONS = {
    "en": {
        _("Transcrire"): "Transcribe",
        _("Bibliothèque"): "Library",
        "Stats": "Stats",
        "VoxCast": "VoxCast",
        _("Transcrivez vos vidéos et fichiers audio en Markdown — propulsé par Voxtral."):
            "Transcribe your videos and audio files to Markdown — powered by Voxtral.",
        _("Nouvelle transcription"): "New transcription",
        _("URL YouTube ou glissez un fichier audio ici"):
            "YouTube URL or drop an audio file here",
        _("Parcourir un fichier audio"): "Browse for an audio file",
        _("Ajouter"): "Add",
        _("Options avancées"): "Advanced options",
        _("Nom de sortie (auto si vide)"): "Output name (auto if empty)",
        _("Modèle de transcription"): "Transcription model",
        _("Preset Mistral ou API perso (compatible /v1/audio/transcriptions)"):
            "Mistral preset or custom API (compatible /v1/audio/transcriptions)",
        _("Gérer les modèles personnalisés"): "Manage custom models",
        _("Langue"): "Language",
        _("Forcer la langue améliore la précision"): "Forcing the language improves accuracy",
        _("Timestamps"): _("Timestamps"),
        _("Inclure les horaires dans la sortie"): "Include timestamps in the output",
        _("Diarisation"): "Speaker diarization",
        _("Identifier les speakers"): "Identify speakers",
        _("Garder l'audio"): "Keep audio",
        _("Conserver le MP3 téléchargé"): "Keep the downloaded MP3",
        _("Segment"): _("Segment"),
        _("Transcrire seulement une partie (HH:MM:SS)"): "Transcribe only a part (HH:MM:SS)",
        _("Context bias (termes, espaces, max 100)"): "Context bias (terms, spaces, max 100)",
        _("Exporter en SRT"): "Export as SRT",
        _("Sous-titres SubRip"): "SubRip subtitles",
        _("Exporter en VTT"): "Export as VTT",
        _("Sous-titres WebVTT"): "WebVTT subtitles",
        "Exporter en HTML": "Export as HTML",
        "Page web autonome": "Self-contained web page",
        "Exporter en PDF": "Export as PDF",
        "Nécessite pandoc": "Requires pandoc",
        _("File d'attente"): "Queue",
        _("Démarrer"): "Start",
        _("Annuler le traitement en cours"): "Cancel current processing",
        _("Actions sur la file"): "Queue actions",
        _("Réessayer les échecs"): "Retry failures",
        _("Nettoyer les terminés"): "Clear finished",
        _("File d'attente vide"): "Empty queue",
        _("Ajoutez une URL YouTube ou un fichier audio ci-dessus pour commencer."):
            "Add a YouTube URL or an audio file above to get started.",
        _("Transcriptions"): "Transcripts",
        _("Rechercher (nom ou contenu)..."): "Search (name or content)...",
        _("Ouvrir le dossier des transcriptions"): "Open transcripts folder",
        _("Aucune transcription"): "No transcripts yet",
        _("Dossier introuvable"): "Folder not found",
        _("Le dossier des transcriptions configuré n'existe pas :\n{path}\nVérifiez-le dans les Paramètres (⚙)."):
            "The configured transcripts folder does not exist:\n{path}\nCheck it in Settings (⚙).",
        _("Les fichiers Markdown apparaîtront ici après le traitement de la file."):
            "Markdown files will appear here once the queue is processed.",
        _("Ouvrir le dossier"): "Open folder",
        _("Aucun résultat"): "No results",
        _("Aucune transcription ne correspond à cette recherche."):
            "No transcript matches this search.",
        _("Prêt"): "Ready",
        "En attente": "Pending",
        "En cours": "Running",
        "Terminé": "Done",
        "Erreur": "Error",
        "Annulé": "Cancelled",
        _("Résumé IA"): "AI summary",
        _("Aperçu dans l'app"): "Preview in app",
        "Discuter avec l'IA": "Chat with AI",
        _("Ouvrir (externe)"): "Open (external)",
        _("Supprimer"): "Delete",
        _("Réessayer"): "Retry",
        _("Retirer de la file"): "Remove from queue",
        _("Actions"): _("Actions"),
        _("Chapitres"): "Chapters",
        _("Paramètres"): "Settings",
        _("Clé API Mistral requise"): "Mistral API key required",
        _("Aucune clé API Mistral configurée — les transcriptions seront impossibles"):
            "No Mistral API key configured — transcription will be impossible",
        _("Configurer"): "Configure",
        _("API Mistral"): "Mistral API",
        _("Clé API"): "API key",
        _("Dossier des transcriptions"): "Transcripts folder",
        _("Dossier"): "Folder",
        _("Parcourir..."): "Browse...",
        _("Apparence"): "Appearance",
        _("Thème"): "Theme",
        _("Clair, sombre ou celui du système"): "Light, dark or system",
        _("Système"): "System",
        _("Clair"): "Light",
        _("Sombre"): "Dark",
        _("Traitement"): "Processing",
        _("Transcriptions en parallèle"): "Parallel transcriptions",
        _("Nombre d'éléments traités simultanément"): "Number of items processed simultaneously",
        "Voix TTS": "TTS voice",
        "TTS": "TTS",
        _("Playlist détectée — cliquez sur « Ajouter » pour choisir les vidéos."):
            "Playlist detected — click \"Add\" to pick videos.",
        _("Récupération des informations..."): "Fetching info...",
        _("Annulé (délai dépassé)"): "Cancelled (timeout)",
        _("Délai dépassé ({s}s) — le backend ne répond plus, discussion annulée"):
            "Timeout ({s}s) — the backend is not responding, chat cancelled",
        _("réponse lente..."): "slow response...",
        _("Erreur"): "Error",
        "Aucune": "None",
        "Vue d'ensemble": "Overview",
        "Par mois": "Per month",
        "Par modèle": "Per model",
        "Fichiers": "Files",
        "Durée totale": "Total duration",
        _("Coût estimé"): "Estimated cost",
        "Tokens": "Tokens",
        "Vitesse apprise": "Learned speed",
        "Mois": "Month",
        "Modèle": "Model",
        "Aucun": "None",
        "transcriptions": "transcripts",
        "min": "min",
        _("File terminée"): "Queue finished",
        "Transcription terminée : ": "Transcription finished: ",
        "Résumé IA disponible": "AI summary available",
        _("Général"): "General",
        _("Modèles de transcription"): "Transcription models",
        _("Mistral (API officielle)"): "Mistral (official API)",
        _("Mes modèles perso"): "My custom models",
        _("Ajouter un modèle perso"): "Add a custom model",
        _("Ajouter le modèle"): "Add model",
        _("Playlist YouTube"): "YouTube playlist",
        _("Chargement de la playlist..."): "Loading playlist...",
        _("Tout sélectionner"): "Select all",
        _("Tout désélectionner"): "Deselect all",
        _("Ajouter une playlist"): "Add a playlist",
        _("Copier le résumé (Markdown)"): "Copy summary (Markdown)",
        _("Copier le Markdown brut"): "Copy raw Markdown",
        "Écouter": "Listen",
        "Pause": "Pause",
        "Lecture": "Playback",
        "Envoyer": "Send",
        "Posez une question sur cette transcription...":
            "Ask a question about this transcript...",
        "Recherche web : ": "Web search: ",
        "Réflexion...": "Thinking...",
        "Génération audio...": "Generating audio...",
        _("Supprimer la transcription"): "Delete transcript",
        "Supprimer : ": "Delete: ",
        " ?": "?",
        _("Annuler"): "Cancel",
        _("Enregistrer"): "Save",
        _("Ouvrir"): "Open",
        _("Choisir un fichier audio"): "Choose an audio file",
        _("Dossier des transcriptions"): "Transcripts folder",
        _("Valider"): "OK",
    },
}

# ============================================================================
# FEUILLE DE STYLE CUSTOM (accent orange VoxCast / Mistral)
# ============================================================================
CUSTOM_CSS = """
@define-color vox_accent #ff7a18;
@define-color vox_accent_soft alpha(#ff7a18, 0.15);

.hero-badge {
    background-image: linear-gradient(135deg, #ffa94d 0%, #ff7a18 45%, #f03e3e 140%);
    border-radius: 22px;
    padding: 16px;
    color: #ffffff;
}

.hero-title {
    font-size: 30px;
    font-weight: 800;
}

list.voxcast-list {
    background: transparent;
}

list.voxcast-list row {
    background-color: @card_bg_color;
    border: 1px solid @borders;
    border-radius: 14px;
    margin: 4px 8px;
    padding: 4px;
}

list.voxcast-list row:hover {
    background-color: alpha(@vox_accent, 0.07);
}

list.voxcast-list row:selected {
    background-color: alpha(@vox_accent, 0.12);
    border-color: @vox_accent;
}

.icon-badge {
    border-radius: 12px;
    padding: 9px;
}

.badge-accent {
    background-color: @vox_accent_soft;
    color: @vox_accent;
}

.row-title {
    font-weight: 700;
}

.status-pill {
    border-radius: 999px;
    padding: 3px 10px;
    font-weight: 700;
    font-size: 11px;
}

.pill-pending {
    background-color: alpha(@theme_fg_color, 0.10);
    color: alpha(@theme_fg_color, 0.75);
}

.pill-running {
    background-color: @vox_accent;
    color: #ffffff;
}

.pill-done {
    background-color: @success_bg_color;
    color: @success_fg_color;
}

.pill-error {
    background-color: @error_bg_color;
    color: @error_fg_color;
}

.pill-cancelled {
    background-color: alpha(@theme_fg_color, 0.10);
    color: alpha(@theme_fg_color, 0.55);
}

.pill-summary {
    background-color: @vox_accent_soft;
    color: @vox_accent;
}

.pill-tts {
    background-color: alpha(@accent_bg_color, 0.18);
    color: @accent_color;
}

progressbar.voxcast-progress trough {
    background-color: alpha(@theme_fg_color, 0.12);
    border-radius: 999px;
    min-height: 8px;
}

progressbar.voxcast-progress trough progress {
    background-image: linear-gradient(90deg, #ffa94d, #ff7a18 55%, #f03e3e);
    border-radius: 999px;
    min-height: 8px;
}

.status-bar {
    background-color: @card_bg_color;
    border: 1px solid @borders;
    border-radius: 16px;
    padding: 10px 16px;
    margin: 6px 14px 14px;
}

window.drag-hover {
    box-shadow: inset 0 0 0 3px @vox_accent;
}

button.suggested-action {
    font-weight: 700;
}

.md-code {
    font-family: monospace;
    background-color: alpha(@theme_fg_color, 0.08);
    border-radius: 10px;
    padding: 10px;
}

.md-heading {
    margin-top: 14px;
    font-weight: 700;
}

.chat-bubble-user {
    background-color: @vox_accent_soft;
    border-radius: 14px;
    padding: 10px 14px;
}

.chat-bubble-assistant {
    background-color: alpha(@theme_fg_color, 0.07);
    border-radius: 14px;
    padding: 10px 14px;
}

.tts-current {
    background-color: @vox_accent_soft;
    border-radius: 8px;
}

.stat-card {
    background-color: @card_bg_color;
    border: 1px solid @borders;
    border-radius: 16px;
    padding: 14px 18px;
}

.stat-value {
    font-size: 24px;
    font-weight: 800;
}

.bar-track {
    background-color: alpha(@theme_fg_color, 0.10);
    border-radius: 999px;
    min-height: 10px;
}

.bar-fill {
    background-image: linear-gradient(90deg, #ffa94d, #ff7a18 55%, #f03e3e);
    border-radius: 999px;
    min-height: 10px;
}
"""


# ============================================================================
# ITEM DE LA FILE D'ATTENTE
# ============================================================================

class QueueItem:
    """Représente un élément de la file d'attente de transcription."""
    def __init__(self, source: str, is_local: bool, options: dict):
        self.source = source
        self.is_local = is_local
        self.options = options  # language, diarize, timestamps, etc.
        self.status = STATUS_PENDING
        self.error = None
        self.progress = 0.0
        self.duration: Optional[float] = None  # durée audio en secondes (pour l'ETA)
        self.output_path: Optional[Path] = None
        self.row_widget: Optional[Gtk.Widget] = None
        self.status_label: Optional[Gtk.Label] = None
        self.progress_bar: Optional[Gtk.ProgressBar] = None


# ============================================================================
# CLASSE PRINCIPALE DE L'INTERFACE
# ============================================================================

class VoxCastWindow:
    """Fenêtre principale de l'application VoxCast."""

    def __init__(self):
        self.app = Adw.Application(application_id=APP_ID)
        self.app.connect("activate", self.on_activate)
        self.app.connect("shutdown", self.on_shutdown)

        # État
        self.api_key = ""
        self.transcripts_dir = DEFAULT_TRANSCRIPTS_DIR
        self.running = False
        self.cancelled = False
        self.queue: list[QueueItem] = []
        self.queue_thread: Optional[threading.Thread] = None
        self.all_transcripts: list[dict] = []
        self.max_workers = 2
        self.color_scheme = "default"  # default | light | dark
        self.custom_models: list[dict] = []  # modèles perso {name, model, base_url, api_key}
        self.model_profile: Optional[dict] = None  # profil sélectionné
        self.tts_voice: str = ""  # slug de la voix TTS (vide = voix par défaut)
        self._notif_counter = 0
        self._queue_total = 0
        self._last_summary = ""
        self._summaries_in_flight: set[str] = set()

        self.load_settings()
        self.window = None

    def on_activate(self, app):
        self._load_css()
        self._apply_color_scheme()

        self.window = Gtk.ApplicationWindow(
            application=app,
            title=f"VoxCast v{VERSION}",
            default_width=1040,
            default_height=800,
        )

        # Support drag & drop de fichiers
        dnd = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        dnd.connect("drop", self.on_drop_file)
        dnd.connect("enter", self._on_drag_enter)
        dnd.connect("leave", self._on_drag_leave)
        self.window.add_controller(dnd)

        # ===== BARRE D'EN-TÊTE + SÉLECTEUR DE PAGE =====
        header = Adw.HeaderBar()

        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher()
        switcher.set_stack(self.stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)

        settings_btn = Gtk.Button(icon_name="emblem-system-symbolic")
        settings_btn.set_tooltip_text(_(_("Paramètres")))
        settings_btn.connect("clicked", self.on_settings_clicked)
        header.pack_end(settings_btn)

        # Sélecteur de langue FR/EN
        lang_btn = Gtk.MenuButton(icon_name="preferences-desktop-locale-symbolic",
                                  tooltip_text="Language / Langue")
        lang_menu = Gio.Menu()
        lang_menu.append("Français", "lang.fr")
        lang_menu.append("English", "lang.en")
        lang_btn.set_menu_model(lang_menu)
        lang_actions = Gio.SimpleActionGroup()
        for code in ("fr", "en"):
            act = Gio.SimpleAction(name=code)
            act.connect("activate", lambda *_a, c=code: self.on_language_changed(c))
            lang_actions.add_action(act)
        lang_btn.insert_action_group("lang", lang_actions)
        header.pack_end(lang_btn)

        # Bannière quand la clé API manque
        self.api_banner = Adw.Banner(
            title=_("Aucune clé API Mistral configurée — les transcriptions seront impossibles"),
            button_label=_("Configurer"),
        )
        self.api_banner.connect("button-clicked",
                                lambda *_a: self.on_settings_clicked(None))
        self.api_banner.set_revealed(not self.api_key)

        # ===== CONTENU : deux pages =====
        self.stack.add_titled_with_icon(
            self._build_transcribe_page(), "transcribe", _("Transcrire"),
            "audio-input-microphone-symbolic")
        self.stack.add_titled_with_icon(
            self._build_library_page(), "library", _("Bibliothèque"),
            "folder-documents-symbolic")
        self.stack.add_titled_with_icon(
            self._build_stats_page(), "stats", _("Stats"),
            "applications-system-symbolic")

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(self.api_banner)
        toolbar_view.add_top_bar(header)
        toolbar_view.set_content(self.stack)
        toolbar_view.add_bottom_bar(self._build_footer_bar())

        # Toasts pour les retours utilisateur (succès, erreurs, annulations)
        self.toast_overlay = Adw.ToastOverlay(child=toolbar_view)
        self.window.set_child(self.toast_overlay)

        self.setup_accelerators()
        self.refresh_transcripts_list()
        self._load_queue()  # reprise de la file persistée (crash / redémarrage)

        self.window.present()

    def on_language_changed(self, code: str):
        """Change la langue de l'UI et reconstruit la fenêtre."""
        global LANG
        if code == LANG:
            return
        LANG = code
        self.save_settings()
        if self.window is not None:
            win = self.window
            self.window = None
            win.destroy()
            self.on_activate(self.app)

    def _load_css(self):
        """Charge la feuille de style custom de VoxCast."""
        provider = Gtk.CssProvider()
        provider.load_from_data(CUSTOM_CSS.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    # ========================================================================
    # DRAG & DROP
    # ========================================================================

    def on_drop_file(self, target, value, x, y):
        """Gère le drop d'un fichier audio ou d'une URL."""
        self.window.remove_css_class("drag-hover")
        # Ramener l'utilisateur sur la page de transcription
        if hasattr(self, "stack"):
            self.stack.set_visible_child_name("transcribe")
        if hasattr(value, "get_files"):
            # Gdk.FileList
            for file_info in value.get_files():
                path = file_info.get_path()
                if path:
                    self.url_entry.set_text(path)
                    self._check_source_type()
        elif isinstance(value, str):
            self.url_entry.set_text(value)
            self._check_source_type()

    def _on_drag_enter(self, target, x, y):
        self.window.add_css_class("drag-hover")
        return Gdk.DragAction.COPY

    def _on_drag_leave(self, target):
        self.window.remove_css_class("drag-hover")

    def _check_source_type(self):
        """Vérifie si la source est une URL ou un fichier local."""
        source = self.url_entry.get_text().strip()
        is_local = not source.startswith("http")
        self._show_cost_estimate(source, is_local)

    # ========================================================================
    # CONSTRUCTION DES SECTIONS
    # ========================================================================

    def _build_hero(self) -> Gtk.Widget:
        """Bandeau d'accueil en haut de la page Transcrire."""
        hero = Gtk.Box(spacing=16, halign=Gtk.Align.START, margin_bottom=6)

        badge = Gtk.Box(css_classes=["hero-badge"])
        badge.append(Gtk.Image(icon_name="audio-x-generic-symbolic", pixel_size=34))

        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                         valign=Gtk.Align.CENTER)
        title = Gtk.Label(
            label="VoxCast",
            halign=Gtk.Align.START,
            css_classes=["hero-title"],
        )
        subtitle = Gtk.Label(
            label=_("Transcrivez vos vidéos et fichiers audio en Markdown — propulsé par Voxtral."),
            halign=Gtk.Align.START,
            css_classes=["dim-label"],
        )
        titles.append(title)
        titles.append(subtitle)

        hero.append(badge)
        hero.append(titles)
        return hero

    def _build_transcribe_page(self) -> Gtk.Widget:
        """Page 'Transcrire' : hero + source + file d'attente."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        box.set_margin_start(20)
        box.set_margin_end(20)

        box.append(self._build_hero())
        box.append(self._build_source_section())
        box.append(self._build_queue_section())

        clamp = Adw.Clamp(maximum_size=860, child=box)
        scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            child=clamp,
        )
        scrolled.set_vexpand(True)
        return scrolled

    def _build_library_page(self) -> Gtk.Widget:
        """Page 'Bibliothèque' : liste des transcriptions + recherche."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                       vexpand=True)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        box.set_margin_start(20)
        box.set_margin_end(20)

        box.append(self._build_transcripts_section())

        clamp = Adw.Clamp(maximum_size=860, child=box)
        clamp.set_vexpand(True)
        scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            child=clamp,
        )
        scrolled.set_vexpand(True)
        return scrolled

    def _build_source_section(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=_("Nouvelle transcription"))

        # URL ou fichier
        source_row = Gtk.Box(spacing=10)
        self.url_entry = Gtk.Entry(
            placeholder_text=_("URL YouTube ou glissez un fichier audio ici"),
            hexpand=True,
        )
        self.url_entry.connect("activate", lambda *_a: self.on_add_to_queue())
        self.url_entry.connect("changed", lambda *_a: self._on_source_changed())

        browse_btn = Gtk.Button(
            icon_name="document-open-symbolic",
            tooltip_text=_("Parcourir un fichier audio"),
        )
        browse_btn.connect("clicked", self.on_browse_audio)

        add_btn = Gtk.Button(
            label=_("Ajouter"),
            icon_name="list-add-symbolic",
        )
        add_btn.add_css_class("suggested-action")
        add_btn.connect("clicked", self.on_add_to_queue)

        source_row.append(self.url_entry)
        source_row.append(browse_btn)
        source_row.append(add_btn)
        group.add(source_row)

        # Estimation durée / coût (remplie automatiquement)
        self.cost_label = Gtk.Label(
            label="",
            halign=Gtk.Align.START,
            margin_top=5,
        )
        self.cost_label.add_css_class("dim-label")
        group.add(self.cost_label)

        # ===== Options avancées (repliées par défaut) =====
        options = Adw.ExpanderRow(title=_("Options avancées"))

        # Modèle de transcription (presets Mistral + modèles perso)
        model_row = Adw.ActionRow(
            title=_("Modèle de transcription"),
            subtitle=_("Preset Mistral ou API perso (compatible /v1/audio/transcriptions)"),
        )
        self.model_dropdown = Gtk.DropDown(valign=Gtk.Align.CENTER)
        self.model_dropdown.connect("notify::selected", self.on_model_changed)
        self._rebuild_model_dropdown()
        manage_btn = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER,
                                tooltip_text=_("Gérer les modèles personnalisés"))
        manage_btn.connect("clicked", self.on_manage_models)
        model_suffix = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        model_suffix.append(self.model_dropdown)
        model_suffix.append(manage_btn)
        model_row.add_suffix(model_suffix)
        model_row.set_activatable_widget(self.model_dropdown)
        options.add_row(model_row)

        # Nom de sortie
        name_row = Adw.EntryRow(title=_("Nom de sortie (auto si vide)"))
        self.name_entry = name_row
        options.add_row(name_row)

        # Langue + Timestamps
        lang_row = Adw.ActionRow(title=_("Langue"), subtitle=_("Forcer la langue améliore la précision"))
        lang_model = Gtk.StringList()
        for _code, name in LANGUAGES:
            lang_model.append(name)
        self.lang_dropdown = Gtk.DropDown(model=lang_model, valign=Gtk.Align.CENTER)
        lang_row.add_suffix(self.lang_dropdown)
        lang_row.set_activatable_widget(self.lang_dropdown)
        options.add_row(lang_row)

        ts_row = Adw.ActionRow(title=_("Timestamps"), subtitle=_("Inclure les horaires dans la sortie"))
        ts_model = Gtk.StringList()
        for _code2, name in TIMESTAMP_OPTIONS:
            ts_model.append(name)
        self.ts_dropdown = Gtk.DropDown(model=ts_model, valign=Gtk.Align.CENTER)
        ts_row.add_suffix(self.ts_dropdown)
        ts_row.set_activatable_widget(self.ts_dropdown)
        options.add_row(ts_row)

        # Diarisation
        diarize_row = Adw.ActionRow(title=_("Diarisation"), subtitle=_("Identifier les speakers"))
        self.diarize_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        diarize_row.add_suffix(self.diarize_switch)
        diarize_row.set_activatable_widget(self.diarize_switch)
        options.add_row(diarize_row)

        # Garder l'audio
        keep_row = Adw.ActionRow(title=_("Garder l'audio"), subtitle=_("Conserver le MP3 téléchargé"))
        self.keep_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        keep_row.add_suffix(self.keep_switch)
        keep_row.set_activatable_widget(self.keep_switch)
        options.add_row(keep_row)

        # Segment (transcription partielle)
        seg_row = Adw.ActionRow(title=_("Segment"), subtitle=_("Transcrire seulement une partie (HH:MM:SS)"))
        seg_box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.start_entry = Gtk.Entry(placeholder_text="Début", width_chars=7)
        self.end_entry = Gtk.Entry(placeholder_text="Fin", width_chars=7)
        seg_box.append(self.start_entry)
        seg_box.append(Gtk.Label(label="→"))
        seg_box.append(self.end_entry)
        seg_row.add_suffix(seg_box)
        options.add_row(seg_row)

        # Context bias
        bias_row = Adw.EntryRow(title=_("Context bias (termes, espaces, max 100)"))
        self.bias_entry = bias_row
        options.add_row(bias_row)

        # Exports sous-titres
        srt_row = Adw.ActionRow(title=_("Exporter en SRT"), subtitle=_("Sous-titres SubRip"))
        self.srt_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        srt_row.add_suffix(self.srt_switch)
        srt_row.set_activatable_widget(self.srt_switch)
        options.add_row(srt_row)

        vtt_row = Adw.ActionRow(title=_("Exporter en VTT"), subtitle=_("Sous-titres WebVTT"))
        self.vtt_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        vtt_row.add_suffix(self.vtt_switch)
        vtt_row.set_activatable_widget(self.vtt_switch)
        options.add_row(vtt_row)

        group.add(options)
        return group

    def _build_queue_section(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=_("File d'attente"))

        # Actions de la file dans le header : Démarrer / Annuler / menu ⋮
        header_box = Gtk.Box(spacing=6)

        self.start_btn = Gtk.Button(
            label=_("Démarrer"),
            icon_name="media-playback-start-symbolic",
        )
        self.start_btn.add_css_class("suggested-action")
        self.start_btn.set_sensitive(False)
        self.start_btn.connect("clicked", self.on_start_queue)

        self.cancel_btn = Gtk.Button(
            icon_name="process-stop-symbolic",
            tooltip_text=_("Annuler le traitement en cours"),
            sensitive=False,
        )
        self.cancel_btn.connect("clicked", self.on_cancel_clicked)

        queue_menu_btn = Gtk.MenuButton(
            icon_name="view-more-symbolic",
            tooltip_text=_("Actions sur la file"),
        )
        queue_menu = Gio.Menu()
        queue_menu.append(_("Réessayer les échecs"), "queue.retry")
        queue_menu.append(_("Nettoyer les terminés"), "queue.clear")
        queue_menu_btn.set_menu_model(queue_menu)

        queue_actions = Gio.SimpleActionGroup()
        act_retry = Gio.SimpleAction(name="retry")
        act_retry.connect("activate", lambda *_a: self.on_retry_failed())
        act_clear = Gio.SimpleAction(name="clear")
        act_clear.connect("activate", lambda *_a: self.on_clear_done())
        queue_actions.add_action(act_retry)
        queue_actions.add_action(act_clear)
        queue_menu_btn.insert_action_group("queue", queue_actions)

        header_box.append(self.start_btn)
        header_box.append(self.cancel_btn)
        header_box.append(queue_menu_btn)
        group.set_header_suffix(header_box)

        self.queue_list = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.SINGLE,
            css_classes=["voxcast-list"],
        )

        list_scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            child=self.queue_list,
        )
        list_scrolled.set_size_request(-1, 200)
        list_scrolled.set_vexpand(True)

        # État vide avec call-to-action
        empty_page = Adw.StatusPage(
            icon_name="list-add-symbolic",
            title=_("File d'attente vide"),
            description=_("Ajoutez une URL YouTube ou un fichier audio ci-dessus pour commencer."),
        )
        browse_empty_btn = Gtk.Button(label=_("Parcourir un fichier audio"))
        browse_empty_btn.connect("clicked", self.on_browse_audio)
        empty_page.set_child(browse_empty_btn)

        self.queue_stack = Gtk.Stack()
        self.queue_stack.add_named(empty_page, "empty")
        self.queue_stack.add_named(list_scrolled, "list")
        self.queue_stack.set_size_request(-1, 240)
        self.queue_stack.set_vexpand(True)
        self.queue_stack.set_visible_child_name("empty")

        group.add(self.queue_stack)
        return group

    def _build_transcripts_section(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=_("Transcriptions"))

        # Recherche + bouton dossier dans le header
        self.search_entry = Gtk.SearchEntry(
            placeholder_text=_("Rechercher (nom ou contenu)..."),
            hexpand=True,
        )
        self.search_entry.connect("search-changed", self.on_search_changed)

        folder_btn = Gtk.Button(
            icon_name="folder-open-symbolic",
            tooltip_text=_("Ouvrir le dossier des transcriptions"),
        )
        folder_btn.connect("clicked", self.on_open_folder_clicked)

        header_box = Gtk.Box(spacing=6)
        header_box.append(self.search_entry)
        header_box.append(folder_btn)
        group.set_header_suffix(header_box)

        self.transcripts_list = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.SINGLE,
            css_classes=["voxcast-list"],
        )
        # Double-clic sur une ligne -> ouvrir la transcription
        self.transcripts_list.connect("row-activated", self._on_transcript_activated)

        list_scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            child=self.transcripts_list,
        )
        list_scrolled.set_vexpand(True)

        # États vides
        empty_page = Adw.StatusPage(
            icon_name="folder-documents-symbolic",
            title=_("Aucune transcription"),
            description=_("Les fichiers Markdown apparaîtront ici après le traitement de la file."),
        )
        self.empty_page = empty_page
        open_folder_btn = Gtk.Button(label=_("Ouvrir le dossier"))
        open_folder_btn.connect("clicked", self.on_open_folder_clicked)
        empty_page.set_child(open_folder_btn)

        no_results_page = Adw.StatusPage(
            icon_name="edit-find-symbolic",
            title=_("Aucun résultat"),
            description=_("Aucune transcription ne correspond à cette recherche."),
        )

        self.transcripts_stack = Gtk.Stack()
        self.transcripts_stack.add_named(empty_page, "empty")
        self.transcripts_stack.add_named(no_results_page, "no-results")
        self.transcripts_stack.add_named(list_scrolled, "list")
        self.transcripts_stack.set_vexpand(True)
        self.transcripts_stack.set_visible_child_name("empty")

        group.add(self.transcripts_stack)
        return group

    def _build_footer_bar(self) -> Gtk.Widget:
        """Barre de statut fixe en pied de fenêtre (progression globale)."""
        bar = Gtk.Box(spacing=14, halign=Gtk.Align.FILL, css_classes=["status-bar"])

        self.status_label = Gtk.Label(
            label=_("Prêt"),
            halign=Gtk.Align.START,
            hexpand=True,
            ellipsize=Pango.EllipsizeMode.END,
            css_classes=["heading"],
        )
        self.progress_bar = Gtk.ProgressBar(
            css_classes=["voxcast-progress"],
            width_request=220,
            valign=Gtk.Align.CENTER,
            visible=False,
        )
        self.eta_label = Gtk.Label(
            label="",
            halign=Gtk.Align.END,
            width_request=120,
            css_classes=["caption", "dim-label"],
        )

        bar.append(self.status_label)
        bar.append(self.progress_bar)
        bar.append(self.eta_label)
        return bar

    # ========================================================================
    # RACCOURCIS CLAVIER
    # ========================================================================

    def setup_accelerators(self):
        accel_group = Gio.SimpleActionGroup()
        self.window.insert_action_group("win", accel_group)

        shortcuts = [
            ("add_queue", ["<Control>Return"], self.on_add_to_queue),
            ("start", ["<Control>space"], self.on_start_queue),
            ("cancel", ["<Control>q"], self.on_cancel_clicked),
            ("refresh", ["<Control>r"], self.refresh_transcripts_list),
            ("open", ["<Control>o"],
             lambda: self.on_preview_transcript(self._selected_transcript())),
            ("delete", ["<Control>d"],
             lambda: self.on_delete_transcript_clicked(self._selected_transcript())),
        ]

        for name, accels, callback in shortcuts:
            action = Gio.SimpleAction(name=name)
            action.connect("activate", lambda _a, _p, cb=callback: cb())
            accel_group.add_action(action)
            self.app.set_accels_for_action(f"win.{name}", accels)

    # ========================================================================
    # ESTIMATION DU COÛT
    # ========================================================================

    def _show_cost_estimate(self, source: str, is_local: bool):
        """Estimation durée/coût + auto-remplissage du nom.

        Le fetch réseau (yt-dlp pour les URLs, ffprobe pour les fichiers
        locaux) est exécuté dans un thread : le thread GTK n'est jamais
        bloqué. Le résultat n'est appliqué que si la source n'a pas changé
        entre-temps."""
        if not source:
            self.cost_label.set_label("")
            return
        # cas purement local (pas de réseau) : réponse immédiate
        if source.startswith("http") and vtt.is_playlist_url(source):
            self.cost_label.set_label(
                _("Playlist détectée — cliquez sur « Ajouter » pour choisir les vidéos."))
            return
        self._source_req = getattr(self, "_source_req", 0) + 1
        req = self._source_req
        self.cost_label.set_label(_("Récupération des informations..."))

        def work():
            label = ""
            auto_name = ""
            try:
                if is_local and Path(source).exists():
                    duration = vtt.get_audio_duration_seconds(Path(source))
                    cost = vtt.estimate_cost(duration)
                    label = (f"Durée: {vtt.format_timestamp(duration)} | "
                             f"Coût estimé: ${cost:.2f}")
                    if not self.name_entry.get_text().strip():
                        auto_name = vtt.sanitize_filename(Path(source).stem)
                elif source.startswith("http"):
                    # Pour les URLs, on fetch les infos (avec cache)
                    info = vtt.get_video_info_cached(source)
                    duration = info.get("duration", 0)
                    cost = vtt.estimate_cost(duration)
                    label = (f"Durée: {vtt.format_timestamp(duration)} | "
                             f"Coût estimé: ${cost:.2f}")
                    if not self.name_entry.get_text().strip():
                        title = info.get("title", "")
                        if title:
                            auto_name = vtt.sanitize_filename(title)
            except Exception:
                label = ""
            GLib.idle_add(self._apply_cost_estimate, req, source, label, auto_name)

        threading.Thread(target=work, daemon=True).start()

    def _apply_cost_estimate(self, req: int, source: str, label: str,
                             auto_name: str):
        """Applique le résultat de l'estimation si la source est toujours d'actualité."""
        if req != getattr(self, "_source_req", 0):
            return False
        if self.url_entry.get_text().strip() != source:
            return False
        self.cost_label.set_label(label)
        if auto_name and not self.name_entry.get_text().strip():
            self.name_entry.set_text(auto_name)
        return False

    # ========================================================================
    # FILE D'ATTENTE
    # ========================================================================

    def on_browse_audio(self, button):
        """Ouvre un dialogue de sélection de fichier audio."""
        dialog = Gtk.FileChooserDialog(
            title=_("Choisir un fichier audio"),
            transient_for=self.window,
            action=Gtk.FileChooserAction.OPEN,
            modal=True,
        )
        # Filtres audio
        filt = Gtk.FileFilter(name="Fichiers audio")
        for ext in ["mp3", "wav", "m4a", "webm", "ogg", "flac", "aac", "wma"]:
            filt.add_pattern(f"*.{ext}")
        dialog.add_filter(filt)
        dialog.add_buttons(_("Annuler"), Gtk.ResponseType.CANCEL, _("Ouvrir"), Gtk.ResponseType.ACCEPT)
        dialog.connect("response", self._on_browse_audio_response)
        dialog.present()

    def _on_browse_audio_response(self, dialog, response_id):
        if response_id == Gtk.ResponseType.ACCEPT:
            selected = dialog.get_file()
            if selected:
                self.url_entry.set_text(selected.get_path())
                self._check_source_type()
        dialog.destroy()

    # ========================================================================
    # MODÈLES DE TRANSCRIPTION (presets Mistral + perso)
    # ========================================================================

    def _model_profiles(self) -> list[dict]:
        """Liste des profils de modèle : presets Mistral puis modèles perso."""
        profiles = [
            {"label": lbl, "model": mid, "base_url": None, "api_key": None}
            for mid, lbl in vtt.MISTRAL_TRANSCRIBE_MODELS
        ]
        for cm in self.custom_models:
            profiles.append({
                "label": f"{cm.get('name') or cm['model']} · {cm.get('base_url') or 'custom'}",
                "model": cm["model"],
                "base_url": cm.get("base_url") or None,
                "api_key": cm.get("api_key") or None,
            })
        return profiles

    def _rebuild_model_dropdown(self):
        profiles = self._model_profiles()
        model = Gtk.StringList()
        for p in profiles:
            model.append(p["label"])
        self.model_dropdown.set_model(model)
        idx = 0
        if self.model_profile:
            for i, p in enumerate(profiles):
                if (p["model"] == self.model_profile.get("model")
                        and (p["base_url"] or None) == (self.model_profile.get("base_url") or None)):
                    idx = i
                    break
        self.model_dropdown.set_selected(idx)

    def _selected_profile(self) -> dict:
        profiles = self._model_profiles()
        idx = self.model_dropdown.get_selected()
        if 0 <= idx < len(profiles):
            return profiles[idx]
        return profiles[0]

    def on_model_changed(self, dropdown, _param):
        profile = self._selected_profile()
        if profile:
            self.model_profile = {
                "label": profile["label"],
                "model": profile["model"],
                "base_url": profile["base_url"],
                "api_key": profile["api_key"],
            }
            self.save_settings()

    def on_manage_models(self, button=None):
        """Dialogue de gestion des modèles perso (ajout / suppression)."""
        dialog = Adw.Window(
            transient_for=self.window,
            modal=True,
            title=_("Modèles de transcription"),
            default_width=580,
            default_height=620,
        )
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=_("Modèles de transcription"),
                                           css_classes=["title-4"]))
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()

        preset_group = Adw.PreferencesGroup(
            title=_("Mistral (API officielle)"),
            description="Sélectionnables dans la liste déroulante « Modèle de transcription »")
        for mid, lbl in vtt.MISTRAL_TRANSCRIBE_MODELS:
            preset_group.add(Adw.ActionRow(title=lbl, subtitle=mid))
        page.add(preset_group)

        self._custom_group = Adw.PreferencesGroup(
            title=_("Mes modèles perso"),
            description="Endpoints compatibles /v1/audio/transcriptions (Groq, OpenAI, etc.)")
        page.add(self._custom_group)
        self._custom_rows = []
        self._fill_custom_models_group()

        add_group = Adw.PreferencesGroup(title=_("Ajouter un modèle perso"))
        self._cm_name = Adw.EntryRow(title="Nom (ex: Groq Whisper)")
        self._cm_model = Adw.EntryRow(title="Model ID (ex: whisper-large-v3)")
        self._cm_url = Adw.EntryRow(title="URL de base de l'API (ex: https://api.groq.com)")
        key_row = Adw.ActionRow(title="Clé API du fournisseur")
        self._cm_key = Gtk.PasswordEntry(valign=Gtk.Align.CENTER, width_chars=24)
        key_row.add_suffix(self._cm_key)
        key_row.set_activatable_widget(self._cm_key)
        for row in (self._cm_name, self._cm_model, self._cm_url, key_row):
            add_group.add(row)
        add_btn = Gtk.Button(label=_("Ajouter le modèle"),
                             css_classes=["suggested-action"],
                             halign=Gtk.Align.END, margin_top=8, margin_bottom=8,
                             margin_end=12)
        add_btn.connect("clicked", self._on_add_custom_model)
        add_group.add(add_btn)
        page.add(add_group)

        scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            child=page,
        )
        scrolled.set_vexpand(True)
        toolbar.set_content(scrolled)
        dialog.set_content(toolbar)
        dialog.present()

    def _fill_custom_models_group(self):
        for row in getattr(self, "_custom_rows", []):
            self._custom_group.remove(row)
        self._custom_rows = []
        for i, cm in enumerate(self.custom_models):
            row = Adw.ActionRow(
                title=cm.get("name") or cm["model"],
                subtitle=f"{cm['model']} @ {cm.get('base_url') or '-'}")
            del_btn = Gtk.Button(icon_name="user-trash-symbolic",
                                 valign=Gtk.Align.CENTER,
                                 css_classes=["destructive-action"],
                                 tooltip_text="Supprimer ce modèle")
            del_btn.connect("clicked", lambda *_b, idx=i: self._on_delete_custom_model(idx))
            row.add_suffix(del_btn)
            self._custom_group.add(row)
            self._custom_rows.append(row)

    def _on_add_custom_model(self, button=None):
        name = self._cm_name.get_text().strip()
        model = self._cm_model.get_text().strip()
        base_url = self._cm_url.get_text().strip()
        api_key = self._cm_key.get_text().strip()
        if not model or not base_url:
            self.toast("Model ID et URL de base requis.", "error")
            return
        self.custom_models.append({
            "name": name or model,
            "model": model,
            "base_url": base_url,
            "api_key": api_key,
        })
        self.save_settings()
        self._rebuild_model_dropdown()
        self._fill_custom_models_group()
        for r in (self._cm_name, self._cm_model, self._cm_url, self._cm_key):
            r.set_text("")
        self.toast(f"Modèle ajouté : {name or model}", "success")

    def _on_delete_custom_model(self, index: int):
        if 0 <= index < len(self.custom_models):
            removed = self.custom_models.pop(index)
            # Si le profil supprimé était sélectionné, retomber sur le preset par défaut
            if (self.model_profile
                    and self.model_profile.get("model") == removed["model"]
                    and (self.model_profile.get("base_url") or None) == (removed.get("base_url") or None)):
                self.model_profile = None
            self.save_settings()
            self._rebuild_model_dropdown()
            self._fill_custom_models_group()
            self.toast(f"Modèle supprimé : {removed.get('name') or removed['model']}")

    def _collect_options(self, output_name: Optional[str] = None) -> dict:
        """Récupère les options courantes du formulaire."""
        profile = self._selected_profile()
        lang_idx = self.lang_dropdown.get_selected()
        language = LANGUAGES[lang_idx][0]
        if language == "auto":
            language = None

        ts_idx = self.ts_dropdown.get_selected()
        ts_value = TIMESTAMP_OPTIONS[ts_idx][0]
        timestamp_granularities = [ts_value] if ts_value != "none" else None

        # Parse segment
        start = vtt.parse_timestamp_arg(self.start_entry.get_text().strip()) if self.start_entry.get_text().strip() else None
        end = vtt.parse_timestamp_arg(self.end_entry.get_text().strip()) if self.end_entry.get_text().strip() else None

        bias_text = self.bias_entry.get_text().strip()
        context_bias = bias_text.split() if bias_text else None

        return {
            "language": language,
            "diarize": self.diarize_switch.get_active(),
            "timestamp_granularities": timestamp_granularities,
            "context_bias": context_bias,
            "output_name": output_name or self.name_entry.get_text().strip() or None,
            "keep_audio": self.keep_switch.get_active(),
            "start": start,
            "end": end,
            "export_srt": self.srt_switch.get_active(),
            "export_vtt": self.vtt_switch.get_active(),
            "model": profile["model"],
            "base_url": profile["base_url"],
            "model_api_key": profile["api_key"],
        }

    def _probe_duration(self, source: str, is_local: bool) -> Optional[float]:
        """Durée audio en secondes (sonde ffprobe ou métadonnées YouTube cachées)."""
        try:
            if is_local:
                return vtt.get_audio_duration_seconds(Path(source))
            info = vtt.get_video_info_cached(source)
            return info.get("duration") or None
        except Exception:
            return None

    def on_add_to_queue(self, button=None):
        """Ajoute l'élément courant à la file d'attente."""
        source = self.url_entry.get_text().strip()
        if not source:
            self.toast("Veuillez entrer une URL ou un fichier.")
            return

        is_local = not source.startswith("http")

        # Vérifier que le fichier existe si local
        if is_local and not Path(source).exists():
            self.toast(f"Fichier introuvable : {source}", "error")
            return

        # Playlist YouTube -> dialogue de sélection des vidéos
        if not is_local and vtt.is_playlist_url(source):
            self._open_playlist_dialog(source)
            return

        options = self._collect_options()

        item = QueueItem(source=source, is_local=is_local, options=options)
        item.duration = self._probe_duration(source, is_local)
        self.queue.append(item)
        self._refresh_queue_list()

        # Réinitialiser les champs
        self.url_entry.set_text("")
        self.name_entry.set_text("")
        self.start_entry.set_text("")
        self.end_entry.set_text("")
        self.cost_label.set_label("")

        self.toast(f"Ajouté à la file : {Path(source).name}")
        self.start_btn.set_sensitive(len(self.queue) > 0)

    # ========================================================================
    # PLAYLIST YOUTUBE
    # ========================================================================

    def _open_playlist_dialog(self, url: str):
        """Ouvre un dialogue listant les vidéos d'une playlist YouTube."""
        dialog = Adw.Window(
            transient_for=self.window,
            modal=True,
            title=_("Ajouter une playlist"),
            default_width=620,
            default_height=560,
        )

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=_("Playlist YouTube"), css_classes=["title-4"]))
        toolbar.add_top_bar(header)

        # État de chargement
        loading_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                              halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                              vexpand=True)
        spinner = Adw.Spinner()
        spinner.set_size_request(48, 48)
        loading_box.append(spinner)
        loading_box.append(Gtk.Label(label=_("Chargement de la playlist..."),
                                      css_classes=["dim-label"]))
        toolbar.set_content(loading_box)

        dialog.set_content(toolbar)
        dialog.present()

        def work():
            try:
                playlist = vtt.get_playlist_entries(url)
                GLib.idle_add(self._fill_playlist_dialog, dialog, toolbar, playlist)
            except Exception as e:
                GLib.idle_add(self.toast, f"Playlist illisible : {e}", "error", 6)
                GLib.idle_add(dialog.close)

        threading.Thread(target=work, daemon=True).start()

    def _fill_playlist_dialog(self, dialog, toolbar, playlist: dict):
        entries = playlist.get("entries", [])
        title = playlist.get("title", "Playlist")

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        total_dur = sum(e.get("duration", 0) for e in entries)
        subtitle = Gtk.Label(
            label=f"{title} — {len(entries)} vidéo(s), {vtt.format_timestamp(total_dur)} au total",
            halign=Gtk.Align.START,
            ellipsize=Pango.EllipsizeMode.END,
            css_classes=["heading"],
        )
        content.append(subtitle)

        list_box = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.MULTIPLE,
            css_classes=["voxcast-list"],
        )
        for e in entries:
            row_box = Gtk.Box(spacing=10, margin_top=6, margin_bottom=6,
                              margin_start=10, margin_end=10)
            dur = e.get("duration", 0)
            dur_str = vtt.format_timestamp(dur) if dur else "?"
            info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
            info.append(Gtk.Label(label=e["title"], halign=Gtk.Align.START,
                                  ellipsize=Pango.EllipsizeMode.END, max_width_chars=60,
                                  css_classes=["row-title"]))
            info.append(Gtk.Label(label=dur_str, halign=Gtk.Align.START,
                                  css_classes=["caption", "dim-label"]))
            row_box.append(info)
            row = Gtk.ListBoxRow(child=row_box, activatable=True)
            row.entry = e
            list_box.append(row)
            list_box.select_row(row)  # tout pré-sélectionner

        scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            child=list_box,
        )
        scrolled.set_vexpand(True)
        content.append(scrolled)

        # Barre d'actions
        actions = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        select_all = Gtk.Button(label=_("Tout sélectionner"))
        select_all.connect("clicked", lambda *_a: list_box.select_all())
        select_none = Gtk.Button(label=_("Tout désélectionner"))
        select_none.connect("clicked", lambda *_a: list_box.unselect_all())
        add_btn = Gtk.Button(label=_("Ajouter"), css_classes=["suggested-action"])
        add_btn.connect("clicked", lambda *_a: self._add_playlist_selection(
            dialog, list_box, entries))
        actions.append(select_all)
        actions.append(select_none)
        actions.append(add_btn)
        content.append(actions)

        toolbar.set_content(content)
        return False

    def _add_playlist_selection(self, dialog, list_box, entries: list[dict]):
        selected_rows = list_box.get_selected_rows()
        selected_entries = [getattr(r, "entry") for r in selected_rows]
        if not selected_entries:
            self.toast("Sélectionnez au moins une vidéo.")
            return

        options = self._collect_options()
        added = 0
        for e in selected_entries:
            opts = dict(options)
            opts["output_name"] = vtt.sanitize_filename(e["title"])
            item = QueueItem(source=e["url"], is_local=False, options=opts)
            item.duration = e.get("duration") or None
            self.queue.append(item)
            added += 1

        self._refresh_queue_list()
        self.start_btn.set_sensitive(len(self.queue) > 0)
        self.toast(f"{added} vidéo(s) ajoutée(s) à la file", "success")
        self.url_entry.set_text("")
        self.name_entry.set_text("")
        self.cost_label.set_label("")
        dialog.close()

    def _refresh_queue_list(self):
        """Met à jour visuellement la file d'attente."""
        for row in list(self.queue_list):
            self.queue_list.remove(row)

        if not self.queue:
            self.queue_stack.set_visible_child_name("empty")
            return
        self.queue_stack.set_visible_child_name("list")

        for i, item in enumerate(self.queue):
            row = self._create_queue_row(item, i)
            self.queue_list.append(row)

        # Mettre à jour l'état du bouton start
        pending = [q for q in self.queue if q.status == STATUS_PENDING]
        self.start_btn.set_sensitive(len(pending) > 0 and not self.running)
        self._save_queue()

    # ------------------------------------------------------------------
    # Persistance de la file d'attente (reprise après crash / redémarrage)
    # ------------------------------------------------------------------

    def _save_queue(self):
        """Écrit les items non terminés dans queue.json (à la racine du projet)."""
        try:
            data = [
                {
                    "source": q.source,
                    "is_local": q.is_local,
                    "options": q.options,
                    "duration": q.duration,
                }
                for q in self.queue
                if q.status in (STATUS_PENDING, STATUS_RUNNING, STATUS_ERROR)
            ]
            QUEUE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        except Exception as e:
            self.log_message(f"queue save: {e}")

    def _load_queue(self):
        """Recharge la file persistée ; les items repartent en « pending »."""
        try:
            data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, list):
            return
        for entry in data:
            try:
                item = QueueItem(
                    entry.get("source", ""),
                    bool(entry.get("is_local", False)),
                    entry.get("options") or {},
                )
                item.duration = entry.get("duration")
                if item.source:
                    self.queue.append(item)
            except Exception as e:
                self.log_message(f"queue load: {e}")
        if self.queue:
            self.log_message(f"File d'attente restaurée : {len(self.queue)} élément(s)")
        self._refresh_queue_list()

    def _create_queue_row(self, item: QueueItem, index: int) -> Gtk.Widget:
        box = Gtk.Box(spacing=12, halign=Gtk.Align.FILL,
                      margin_top=8, margin_bottom=8, margin_start=10, margin_end=10)

        # Badge d'icône selon le type de source
        icon_name = "audio-x-generic-symbolic" if item.is_local else "video-display-symbolic"
        badge = Gtk.Box(css_classes=["icon-badge", "badge-accent"],
                        valign=Gtk.Align.CENTER)
        badge.append(Gtk.Image(icon_name=icon_name, pixel_size=20))
        box.append(badge)

        # Info + pastille de statut + barre de progression de l'item
        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                           hexpand=True, valign=Gtk.Align.CENTER)
        name_label = Gtk.Label(
            label=item.options.get("output_name") or Path(item.source).name or item.source,
            halign=Gtk.Align.START,
            ellipsize=Pango.EllipsizeMode.END,
            max_width_chars=50,
            css_classes=["row-title"],
        )

        status_row = Gtk.Box(spacing=8, halign=Gtk.Align.START)
        status_lbl = Gtk.Label(
            label=status_label(item.status),
            halign=Gtk.Align.START,
            css_classes=["status-pill", f"pill-{item.status}"],
        )
        status_row.append(status_lbl)
        if item.status == STATUS_ERROR and item.error:
            error_label = Gtk.Label(
                label=item.error,
                halign=Gtk.Align.START,
                ellipsize=Pango.EllipsizeMode.END,
                max_width_chars=40,
                css_classes=["caption", "dim-label"],
            )
            status_row.append(error_label)

        progress_bar = Gtk.ProgressBar(
            css_classes=["voxcast-progress"],
            halign=Gtk.Align.FILL,
            show_text=False,
            visible=(item.status == STATUS_RUNNING),
            fraction=item.progress or 0.0,
        )

        info_box.append(name_label)
        info_box.append(status_row)
        info_box.append(progress_bar)
        item.status_label = status_lbl
        item.progress_bar = progress_bar

        # Menu par item (⋮) : réessayer / retirer
        menu_btn = Gtk.MenuButton(
            icon_name="view-more-symbolic",
            valign=Gtk.Align.CENTER,
            tooltip_text=_("Actions"),
        )
        menu = Gio.Menu()
        actions = Gio.SimpleActionGroup()
        if item.status in (STATUS_ERROR, STATUS_CANCELLED):
            menu.append(_("Réessayer"), "item.retry")
            act = Gio.SimpleAction(name="retry")
            act.connect("activate", lambda *_a, it=item: self._retry_queue_item(it))
            actions.add_action(act)
        if item.status != STATUS_RUNNING:
            menu.append(_("Retirer de la file"), "item.remove")
            act = Gio.SimpleAction(name="remove")
            act.connect("activate", lambda *_a, it=item: self._remove_queue_item(it))
            actions.add_action(act)
        menu_btn.set_menu_model(menu)
        menu_btn.insert_action_group("item", actions)
        menu_btn.set_sensitive(menu.get_n_items() > 0)

        box.append(info_box)
        box.append(menu_btn)

        row = Gtk.ListBoxRow(child=box, activatable=False)
        item.row_widget = row
        return row

    def _retry_queue_item(self, item: QueueItem):
        """Remet un élément en erreur/annulé en attente."""
        if item.status in (STATUS_ERROR, STATUS_CANCELLED):
            item.status = STATUS_PENDING
            item.error = None
            self._refresh_queue_list()

    def _remove_queue_item(self, item: QueueItem):
        """Retire un élément de la file."""
        if item.status == STATUS_RUNNING:
            self.toast("Impossible de retirer un élément en cours.", "error")
            return
        if item in self.queue:
            self.queue.remove(item)
            self._refresh_queue_list()

    def on_remove_queue_item(self, button=None):
        """Retire l'élément sélectionné de la file (raccourci clavier)."""
        selected = self.queue_list.get_selected_row()
        if selected is None:
            return
        idx = selected.get_index()
        # La première ligne peut être le placeholder "File vide"
        if 0 <= idx < len(self.queue):
            self._remove_queue_item(self.queue[idx])

    def on_clear_done(self, button=None):
        """Retire les éléments terminés, en erreur ou annulés."""
        self.queue = [q for q in self.queue if q.status in (STATUS_PENDING, STATUS_RUNNING)]
        self._refresh_queue_list()
        self.start_btn.set_sensitive(len(self.queue) > 0)

    def on_retry_failed(self, button=None):
        """Remet les éléments en erreur ou annulés en attente."""
        for item in self.queue:
            if item.status in (STATUS_ERROR, STATUS_CANCELLED):
                item.status = STATUS_PENDING
                item.error = None
        self._refresh_queue_list()
        self.toast("Éléments en erreur remis en attente.")

    def on_start_queue(self, button=None):
        """Démarre le traitement de la file d'attente."""
        if self.running:
            return
        if not vtt.check_ffmpeg():
            self.toast("ffmpeg requis. Installez-le (apt install ffmpeg).", "error")
            return

        pending = [q for q in self.queue if q.status == STATUS_PENDING]
        if not pending:
            self.toast("Aucun élément en attente.")
            return

        self._ensure_api_key(self._start_queue_actual)

    def _start_queue_actual(self):
        """Démarre effectivement le traitement de la file."""
        pending = [q for q in self.queue if q.status == STATUS_PENDING]
        if not pending:
            return

        self.running = True
        self.cancelled = False
        self.start_btn.set_sensitive(False)
        self.cancel_btn.set_sensitive(True)
        self.progress_bar.set_visible(True)
        self.progress_bar.set_fraction(0.0)

        self.queue_thread = threading.Thread(
            target=self._process_queue,
            daemon=True,
        )
        self.queue_thread.start()

    def _process_queue(self):
        """Traite la file d'attente en parallèle (max_workers threads).
        Les logs du pipeline vont dans le shell + voxcast.log via logging."""

        def process_one(item: QueueItem):
            if self.cancelled:
                item.status = STATUS_CANCELLED
                GLib.idle_add(self._update_queue_item_status, item)
                GLib.idle_add(self._update_global_progress)
                return

            item.status = STATUS_RUNNING
            item.progress = 0.0
            GLib.idle_add(self._update_queue_item_status, item)
            GLib.idle_add(self._update_global_progress)
            GLib.idle_add(self.update_status,
                f"Traitement : {Path(item.source).name[:40]}", "yellow")
            self.log_message(f"--- {item.source} ---")

            opts = item.options
            try:
                output_path = vtt.run_pipeline(
                    source=item.source,
                    api_key=self.api_key,
                    language=opts.get("language"),
                    diarize=opts.get("diarize", False),
                    timestamp_granularities=opts.get("timestamp_granularities"),
                    context_bias=opts.get("context_bias"),
                    output_name=opts.get("output_name"),
                    keep_audio=opts.get("keep_audio", False),
                    start=opts.get("start"),
                    end=opts.get("end"),
                    is_local_file=item.is_local,
                    transcripts_dir=self.transcripts_dir,
                    export_srt_file=opts.get("export_srt", False),
                    export_vtt_file=opts.get("export_vtt", False),
                    progress_callback=lambda f, msg, it=item: GLib.idle_add(self._on_item_progress, it, f, msg),
                    cancel_check=lambda: self.cancelled,
                    model=opts.get("model"),
                    base_url=opts.get("base_url"),
                    model_api_key=opts.get("model_api_key"),
                )
                item.status = STATUS_DONE
                item.output_path = output_path
                GLib.idle_add(self.toast, f"Transcription terminée : {output_path.name}",
                    "success")
                GLib.idle_add(self._send_notification,
                    "Transcription terminée", output_path.name)
                self.log_message(f"Terminé: {output_path.name}")

            except InterruptedError:
                item.status = STATUS_CANCELLED
                GLib.idle_add(self.toast, "Transcription annulée", "info")
                self.log_message("Annulé")
            except Exception as e:
                item.status = STATUS_ERROR
                item.error = str(e)
                GLib.idle_add(self.toast, f"Erreur : {e}", "error", 6)
                GLib.idle_add(self._send_notification,
                    "Échec de la transcription", f"{Path(item.source).name} : {e}")
                self.log_message(f"Erreur: {e}")
            finally:
                GLib.idle_add(self._update_queue_item_status, item)
                GLib.idle_add(self._update_global_progress)

        try:
            pending = [q for q in self.queue if q.status == STATUS_PENDING]
            total = len(pending)
            self._queue_total = total

            workers = max(1, min(self.max_workers, total))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(process_one, pending))

            if self.cancelled:
                GLib.idle_add(self.update_status, "File annulée", "yellow")
            else:
                GLib.idle_add(self.update_status, _("File terminée"), "green")
                GLib.idle_add(self._send_notification,
                    "File d'attente terminée", f"{total} élément(s) traité(s)")

        except Exception as e:
            self.log_message(f"Erreur fatale: {e}")
            GLib.idle_add(self.update_status, "Erreur", "red")
        finally:
            GLib.idle_add(self._on_queue_done)

    def _update_global_progress(self):
        """Progression globale = (terminés + somme des progressions en cours) / total."""
        if not self._queue_total:
            return
        done = sum(1 for q in self.queue if q.status == STATUS_DONE)
        running = sum(q.progress for q in self.queue if q.status == STATUS_RUNNING)
        cancelled = sum(1 for q in self.queue if q.status == STATUS_CANCELLED)
        frac = (done + cancelled + running) / self._queue_total
        self.progress_bar.set_fraction(min(frac, 1.0))
        self._update_eta()

    def _update_eta(self):
        """
        ETA global : (audio restant de toute la file) × vitesse apprise / workers.
        La vitesse apprise vient des transcriptions déjà terminées (voir
        video_to_text.get_learned_speed) — bien plus fidèle qu'une constante.
        """
        if not self.running or not self._queue_total:
            self.eta_label.set_label("")
            return
        workers = max(1, min(self.max_workers, self._queue_total))
        remaining_seconds = 0.0
        known = False
        for q in self.queue:
            if q.duration is None:
                continue
            known = True
            # vitesse apprise propre au modèle de chaque item
            key = (f"{q.options.get('base_url') or 'mistral'}::"
                   f"{q.options.get('model') or vtt.MISTRAL_MODEL}")
            try:
                speed = vtt.get_learned_speed(key)
            except Exception:
                speed = vtt.TRANSCRIBE_SPEED_DEFAULT
            if q.status == STATUS_PENDING:
                remaining_seconds += q.duration * speed
            elif q.status == STATUS_RUNNING:
                remaining_seconds += q.duration * max(0.0, 1.0 - q.progress) * speed
        if not known:
            self.eta_label.set_label("")
            return
        eta = remaining_seconds / workers
        self.eta_label.set_label(f"~{vtt.fmt_duration(eta)} restantes")

    def _update_queue_item_status(self, item: QueueItem):
        """Met à jour la pastille de statut d'un item de la file."""
        if item.status_label:
            item.status_label.set_label(status_label(item.status))
            for cls in PILL_CLASSES:
                item.status_label.remove_css_class(cls)
            item.status_label.add_css_class(f"pill-{item.status}")
        if item.progress_bar:
            item.progress_bar.set_visible(item.status == STATUS_RUNNING)
        self._save_queue()

    def _on_item_progress(self, item: QueueItem, frac: float, message: str):
        """Colle la barre de l'item sur l'activité réelle du pipeline,
        et la barre globale sur l'agrégat de toute la file."""
        if not self.running:
            return
        item.progress = min(frac, 1.0)
        if item.progress_bar:
            item.progress_bar.set_visible(True)
            item.progress_bar.set_fraction(item.progress)
        self._update_global_progress()
        if message:
            self.status_label.set_label(message)

    def _on_queue_done(self):
        self.running = False
        self.cancelled = False
        self._queue_total = 0
        self.cancel_btn.set_sensitive(False)
        self.progress_bar.set_visible(False)
        self.eta_label.set_label("")
        self._refresh_queue_list()
        self.refresh_transcripts_list()
        self._save_queue()  # file terminée -> queue.json vidée

    def _send_notification(self, title: str, body: str):
        """Notification système (Gio.Notification) — appelé via GLib.idle_add."""
        try:
            notif = Gio.Notification.new(title)
            notif.set_body(body)
            self._notif_counter += 1
            self.app.send_notification(f"voxcast-{self._notif_counter}", notif)
        except Exception as e:
            self.log_message(f"Notification: {e}")

    def on_cancel_clicked(self, button=None):
        if self.running:
            self.cancelled = True
            self.log_message("Annulation demandée...", "yellow")
            self.update_status("Annulation en cours...", "yellow")

    # ========================================================================
    # LISTE DES TRANSCRIPTIONS + RECHERCHE
    # ========================================================================

    def refresh_transcripts_list(self, button=None):
        self.all_transcripts = self._get_transcripts()
        self._filter_transcripts(self.search_entry.get_text())
        self._refresh_stats()

    # ------------------------------------------------------------------
    # Page « Stats » (tableau de bord)
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_md_meta(path: Path) -> dict:
        """Extrait durée / modèle / tokens / coût depuis l'en-tête du .md."""
        meta = {"duration_s": None, "model": "", "tokens": None, "cost": None,
                "lang": ""}
        try:
            head = []
            with path.open(encoding="utf-8") as f:
                for i, line in enumerate(f):
                    if i >= 80:  # l'en-tête + la section Métriques
                        break
                    head.append(line)
        except Exception:
            return meta
        text = "\n".join(head)
        m = re.search(r"\*\*Dur\u00e9e?\*\*\s*:\s*(\d+):(\d{2}):(\d{2})", text)
        if m:
            meta["duration_s"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        m = re.search(r"\*\*Mod\u00e8le\*\*\s*:\s*`?([^`\n]+)`?", text)
        if m:
            meta["model"] = m.group(1).strip()
        m = re.search(r"Tokens?\s*(?:total)?\s*:\s*([\d\s.,]+)", text, re.IGNORECASE)
        if m:
            try:
                meta["tokens"] = int(m.group(1).replace(" ", "").replace(".", "").replace(",", ""))
            except ValueError:
                pass
        m = re.search(r"Co\u00fbt\s*(?:estim\u00e9)?\s*:\s*\$?([\d.,]+)", text, re.IGNORECASE)
        if m:
            try:
                meta["cost"] = float(m.group(1).replace(",", "."))
            except ValueError:
                pass
        m = re.search(r"\*\*Langue\*\*\s*:\s*([^\n]+)", text)
        if m:
            meta["lang"] = m.group(1).strip()
        # coût : lu dans le .md s'il y est, sinon estimé depuis la durée
        if meta["cost"] is None and meta["duration_s"]:
            try:
                meta["cost"] = meta["duration_s"] / 60 * vtt.COST_PER_MIN
            except Exception:
                pass
        return meta

    def _build_stats_page(self) -> Gtk.Widget:
        self.stats_container = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=18,
            margin_top=24, margin_bottom=32,
            margin_start=24, margin_end=24)
        scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            vexpand=True,
            child=Adw.Clamp(maximum_size=860, child=self.stats_container))
        self._refresh_stats()
        return scrolled

    def _refresh_stats(self):
        """Reconstruit le tableau de bord depuis les métadonnées des .md."""
        if not hasattr(self, "stats_container"):
            return
        container = self.stats_container
        child = container.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            container.remove(child)
            child = nxt

        items = []
        total_dur = 0
        total_cost = 0.0
        total_tokens = 0
        per_model: dict[str, dict] = {}
        per_month: dict[str, dict] = {}
        for t in getattr(self, "all_transcripts", []):
            meta = self._parse_md_meta(t["path"])
            items.append((t, meta))
            if meta["duration_s"]:
                total_dur += meta["duration_s"]
            if meta["cost"] is not None:
                total_cost += meta["cost"]
            if meta["tokens"]:
                total_tokens += meta["tokens"]
            model = meta["model"] or _("Inconnu")
            pm = per_model.setdefault(model, {"count": 0, "duration_s": 0})
            pm["count"] += 1
            pm["duration_s"] += meta["duration_s"] or 0
            try:
                month = datetime.fromtimestamp(t["path"].stat().st_mtime).strftime("%Y-%m")
            except Exception:
                month = "?"
            pe = per_month.setdefault(month, {"count": 0, "duration_s": 0})
            pe["count"] += 1
            pe["duration_s"] += meta["duration_s"] or 0

        def hms(s: float) -> str:
            s = int(s)
            return f"{s // 3600}h{(s % 3600) // 60:02d}m{s % 60:02d}s"

        # --- cartes de chiffres clés ---
        cards = Gtk.Box(spacing=12, halign=Gtk.Align.FILL)
        learned = vtt.get_learned_speed() if hasattr(vtt, "get_learned_speed") else None

        def card(value: str, label: str) -> Gtk.Widget:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                          css_classes=["stat-card"], halign=Gtk.Align.FILL,
                          hexpand=True)
            v = Gtk.Label(label=value, halign=Gtk.Align.START,
                          css_classes=["stat-value"])
            l = Gtk.Label(label=label, halign=Gtk.Align.START,
                          css_classes=["caption", "dim-label"])
            box.append(v)
            box.append(l)
            return box

        cards.append(card(str(len(items)), _("Transcriptions")))
        cards.append(card(hms(total_dur), _("Durée totale")))
        cards.append(card(f"${total_cost:.4f}", _("Coût API estimé")))
        cards.append(card(f"{total_tokens:,}".replace(",", " "), _("Tokens (résumés)")))
        if learned:
            cards.append(card(f"{learned:.1f} min/min",
                              _("Vitesse apprise (transcription)")))
        container.append(cards)

        # --- activité par mois (barres) ---
        if per_month:
            months = sorted(per_month)
            max_dur = max((per_month[m]["duration_s"] for m in months), default=0) or 1
            group = Adw.PreferencesGroup(
                title=_("Activité par mois"),
                description=_("Durée totale de média transcrit chaque mois"))
            for m in months:
                e = per_month[m]
                row = Adw.ActionRow(
                    title=datetime.strptime(m, "%Y-%m").strftime("%B %Y")
                    if m != "?" else m,
                    subtitle=f"{e['count']} fichier(s) — {hms(e['duration_s'])}")
                bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                              css_classes=["bar-track"], hexpand=True,
                              valign=Gtk.Align.CENTER, height_request=10)
                fill = Gtk.Box(css_classes=["bar-fill"], hexpand=False)
                fill.set_size_request(int(160 * e["duration_s"] / max_dur), 10)
                bar.append(fill)
                row.add_suffix(bar)
                group.add(row)
            container.append(group)

        # --- par modèle ---
        if per_model:
            group = Adw.PreferencesGroup(title=_("Par modèle de transcription"))
            for model, e in sorted(per_model.items(),
                                   key=lambda kv: -kv[1]["duration_s"]):
                row = Adw.ActionRow(
                    title=model,
                    subtitle=f"{e['count']} fichier(s) — {hms(e['duration_s'])}")
                group.add(row)
            container.append(group)

        if not items:
            container.append(Adw.StatusPage(
                icon_name="applications-system-symbolic",
                title=_("Aucune statistique pour le moment"),
                description=_("Transcrivez des médias pour voir apparaître "
                              "durées, coûts et tokens ici.")))

    def on_search_changed(self, entry):
        self._filter_transcripts(entry.get_text())

    def _filter_transcripts(self, query: str):
        """Filtre la liste par nom ou par contenu."""
        for row in list(self.transcripts_list):
            self.transcripts_list.remove(row)

        summary_index = self._summaries_index()
        tts_index = self._tts_index()
        if not query:
            transcripts = [dict(t, has_summary=t["name"] in summary_index,
                                has_tts=t["name"] in tts_index)
                           for t in self.all_transcripts]
        else:
            query_lower = query.lower()
            transcripts = []
            for t in self.all_transcripts:
                # Recherche par nom
                if query_lower in t["name"].lower():
                    match = dict(t)
                    match["snippet"] = None
                    match["has_summary"] = t["name"] in summary_index
                    match["has_tts"] = t["name"] in tts_index
                    transcripts.append(match)
                    continue
                # Recherche par contenu (avec extrait autour du match)
                try:
                    raw = t["path"].read_text(encoding="utf-8")
                    content = raw.lower()
                    idx = content.find(query_lower)
                    if idx >= 0:
                        match = dict(t)
                        start = max(0, idx - 50)
                        end = min(len(raw), idx + len(query) + 70)
                        snippet = raw[start:end].replace("\n", " ").strip()
                        if start > 0:
                            snippet = "…" + snippet
                        if end < len(raw):
                            snippet = snippet + "…"
                        match["snippet"] = snippet
                        match["has_summary"] = t["name"] in summary_index
                        match["has_tts"] = t["name"] in tts_index
                        transcripts.append(match)
                except Exception:
                    continue

        if not transcripts:
            if not self.all_transcripts:
                # dossier configuré introuvable -> message explicite avec le chemin
                if not self.transcripts_dir.exists():
                    self.empty_page.set_icon_name("dialog-warning-symbolic")
                    self.empty_page.set_title(_("Dossier introuvable"))
                    self.empty_page.set_description(
                        _("Le dossier des transcriptions configuré n'existe pas :\n{path}\n"
                          "Vérifiez-le dans les Paramètres (⚙).").format(
                              path=self.transcripts_dir))
                else:
                    self.empty_page.set_icon_name("folder-documents-symbolic")
                    self.empty_page.set_title(_("Aucune transcription"))
                    self.empty_page.set_description(
                        _("Les fichiers Markdown apparaîtront ici après le traitement de la file."))
                self.transcripts_stack.set_visible_child_name("empty")
            else:
                self.transcripts_stack.set_visible_child_name("no-results")
            return
        self.transcripts_stack.set_visible_child_name("list")

        for t in transcripts:
            row = self._create_transcript_row(t)
            self.transcripts_list.append(row)

    def _get_transcripts(self) -> list[dict]:
        transcripts = []
        if not self.transcripts_dir.exists():
            return transcripts
        for f in sorted(self.transcripts_dir.glob("*.md"),
                        key=lambda p: p.stat().st_mtime, reverse=True):
            stat = f.stat()
            transcripts.append({
                "name": f.stem,
                "path": f,
                "size": stat.st_size,
                "modified": stat.st_mtime,
            })
        return transcripts

    def _create_transcript_row(self, t: dict) -> Gtk.Widget:
        box = Gtk.Box(spacing=12, halign=Gtk.Align.FILL,
                      margin_top=8, margin_bottom=8, margin_start=10, margin_end=10)

        badge = Gtk.Box(css_classes=["icon-badge", "badge-accent"],
                        valign=Gtk.Align.CENTER)
        badge.append(Gtk.Image(icon_name="text-x-generic-symbolic", pixel_size=20))
        box.append(badge)

        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                           hexpand=True, valign=Gtk.Align.CENTER)
        name_label = Gtk.Label(
            label=t["name"],
            halign=Gtk.Align.START,
            ellipsize=Pango.EllipsizeMode.END,
            max_width_chars=50,
            css_classes=["row-title"],
        )

        # Pastilles « Résumé IA » / « TTS » quand les fichiers persistés existent
        title_row = Gtk.Box(spacing=8, halign=Gtk.Align.START)
        title_row.append(name_label)
        if t.get("has_summary"):
            title_row.append(Gtk.Label(
                label=_("Résumé IA"),
                halign=Gtk.Align.START,
                css_classes=["status-pill", "pill-summary"],
            ))
        if t.get("has_tts"):
            title_row.append(Gtk.Label(
                label=_("TTS"),
                halign=Gtk.Align.START,
                css_classes=["status-pill", "pill-tts"],
            ))

        date = datetime.fromtimestamp(t["modified"]).strftime("%Y-%m-%d %H:%M")
        size = self._format_size(t["size"])
        details = Gtk.Label(
            label=f"{size} • {date}",
            halign=Gtk.Align.START,
            ellipsize=Pango.EllipsizeMode.END,
            css_classes=["caption", "dim-label"],
        )

        info_box.append(title_row)
        info_box.append(details)

        # Extrait du match quand on fait une recherche plein texte
        snippet = t.get("snippet")
        if snippet:
            query = self.search_entry.get_text()
            snippet_label = Gtk.Label(
                halign=Gtk.Align.START,
                ellipsize=Pango.EllipsizeMode.END,
                max_width_chars=80,
                use_markup=True,
                label=self._highlight_snippet(snippet, query),
                css_classes=["caption", "dim-label"],
            )
            info_box.append(snippet_label)

        # Menu par ligne (⋮) : aperçu, résumé, ouvrir, exporter, supprimer
        menu_btn = Gtk.MenuButton(
            icon_name="view-more-symbolic",
            valign=Gtk.Align.CENTER,
            tooltip_text=_("Actions"),
        )
        menu = Gio.Menu()
        menu.append(_("Aperçu dans l'app"), "tr.preview")
        menu.append(_("Résumé IA"), "tr.summary")
        menu.append(_("Discuter avec l'IA"), "tr.chat")
        menu.append(_("Ouvrir (externe)"), "tr.open")
        menu.append(_("Exporter en SRT"), "tr.srt")
        menu.append(_("Exporter en VTT"), "tr.vtt")
        menu.append(_("Exporter en HTML"), "tr.html")
        menu.append(_("Exporter en PDF"), "tr.pdf")
        menu.append(_("Supprimer"), "tr.delete")
        menu_btn.set_menu_model(menu)

        actions = Gio.SimpleActionGroup()
        act = Gio.SimpleAction(name="preview")
        act.connect("activate", lambda *_a, tr=t: self.on_preview_transcript(tr))
        actions.add_action(act)
        act = Gio.SimpleAction(name="summary")
        act.connect("activate", lambda *_a, tr=t: self.on_summarize_clicked(tr))
        actions.add_action(act)
        act = Gio.SimpleAction(name="chat")
        act.connect("activate", lambda *_a, tr=t: self.on_chat_clicked(tr))
        actions.add_action(act)
        act = Gio.SimpleAction(name="html")
        act.connect("activate", lambda *_a, tr=t: self.on_export_html(tr))
        actions.add_action(act)
        act = Gio.SimpleAction(name="pdf")
        act.connect("activate", lambda *_a, tr=t: self.on_export_pdf(tr))
        actions.add_action(act)
        act = Gio.SimpleAction(name="open")
        act.connect("activate", lambda *_a, tr=t: self.on_open_transcript_clicked(tr))
        actions.add_action(act)
        act = Gio.SimpleAction(name="srt")
        act.connect("activate", lambda *_a, tr=t: self.on_export_srt(tr))
        actions.add_action(act)
        act = Gio.SimpleAction(name="vtt")
        act.connect("activate", lambda *_a, tr=t: self.on_export_vtt(tr))
        actions.add_action(act)
        act = Gio.SimpleAction(name="delete")
        act.connect("activate", lambda *_a, tr=t: self.on_delete_transcript_clicked(tr))
        actions.add_action(act)
        menu_btn.insert_action_group("tr", actions)

        box.append(info_box)
        box.append(menu_btn)

        row = Gtk.ListBoxRow(child=box, activatable=True)
        row.transcript = t
        return row

    def _on_transcript_activated(self, list_box, row):
        """Double-clic sur une transcription : aperçu dans l'app."""
        transcript = getattr(row, "transcript", None)
        if transcript:
            self.on_preview_transcript(transcript)

    def _highlight_snippet(self, snippet: str, query: str) -> str:
        """Échappe le snippet et met en gras la requête (insensible à la casse)."""
        escaped = html.escape(snippet)
        if not query:
            return escaped
        pattern = re.compile(re.escape(html.escape(query)), re.IGNORECASE)
        return pattern.sub(lambda m: f"<b>{m.group(0)}</b>", escaped)

    def _selected_transcript(self) -> Optional[dict]:
        """Transcription de la ligne sélectionnée (pour les raccourcis clavier)."""
        row = self.transcripts_list.get_selected_row()
        if row is None:
            self.toast("Sélectionnez d'abord une transcription.")
            return None
        return getattr(row, "transcript", None)

    # ========================================================================
    # APERÇU IN-APP (Markdown rendu) + RÉSUMÉ IA
    # ========================================================================

    def on_preview_transcript(self, transcript: Optional[dict] = None):
        """Ouvre l'aperçu d'une transcription dans une fenêtre in-app."""
        transcript = transcript or self._selected_transcript()
        if not transcript:
            return
        try:
            md_text = transcript["path"].read_text(encoding="utf-8")
        except Exception as e:
            self.toast(f"Impossible de lire : {e}", "error")
            return

        dialog = Adw.Window(
            transient_for=self.window,
            modal=False,
            title=transcript["name"],
            default_width=780,
            default_height=680,
        )
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(
            label=transcript["name"], ellipsize=Pango.EllipsizeMode.END,
            max_width_chars=40, css_classes=["title-4"]))
        copy_btn = Gtk.Button(icon_name="edit-copy-symbolic",
                              tooltip_text=_("Copier le Markdown brut"))
        copy_btn.connect("clicked", lambda *_a: self._copy_to_clipboard(md_text))
        header.pack_end(copy_btn)
        summary_btn = Gtk.Button(label=_("Résumé IA"), css_classes=["suggested-action"])
        summary_btn.connect("clicked",
                            lambda *_a: self.on_summarize_clicked(transcript))
        header.pack_end(summary_btn)
        chat_btn = Gtk.Button(icon_name="user-available-symbolic",
                              tooltip_text=_("Discuter avec l'IA"))
        chat_btn.connect("clicked", lambda *_a: self.on_chat_clicked(transcript))
        header.pack_end(chat_btn)
        toolbar.add_top_bar(header)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        inner.set_margin_top(18)
        inner.set_margin_bottom(24)
        inner.set_margin_start(24)
        inner.set_margin_end(24)

        # Table des matières des chapitres (si la vidéo en définit)
        chapters = self._extract_chapters(md_text)
        if chapters:
            source_url = self._extract_source_url(md_text)
            toc = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                          css_classes=["status-bar"])
            toc.append(Gtk.Label(label=_("Chapitres"), halign=Gtk.Align.START,
                                 margin_bottom=4, css_classes=["heading"]))
            for secs, chap_title in chapters:
                btn = Gtk.Button(halign=Gtk.Align.START, css_classes=["flat"])
                btn.set_label(f"{vtt.format_timestamp(secs)} — {chap_title}")
                btn.connect("clicked", lambda *_b, s=secs:
                            self._open_source_at(source_url, s))
                toc.append(btn)
            inner.append(toc)

        inner.append(self._build_md_view(md_text))

        scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            child=Adw.Clamp(maximum_size=720, child=inner),
        )
        scrolled.set_vexpand(True)
        toolbar.set_content(scrolled)
        dialog.set_content(toolbar)
        dialog.present()

    def _extract_chapters(self, md_text: str) -> list[tuple[float, str]]:
        """Extrait la section '## Chapitres' : [(secondes, titre), ...]."""
        chapters = []
        in_section = False
        for line in md_text.split("\n"):
            if line.startswith("## "):
                in_section = line.strip() == "## Chapitres"
                continue
            if in_section:
                m = re.match(r"-\s*\[(\d{2}:\d{2}:\d{2})\]\s*(.*)", line.strip())
                if m:
                    chapters.append((vtt.parse_timestamp_arg(m.group(1)),
                                     m.group(2).strip()))
        return chapters

    def _extract_source_url(self, md_text: str) -> Optional[str]:
        """Récupère l'URL source écrite dans les métadonnées du .md."""
        m = re.search(r"\*\*Source\*\*\s*:\s*(\S+)", md_text)
        return m.group(1) if m else None

    def _open_source_at(self, source_url: Optional[str], seconds: float):
        """Ouvre la vidéo source au timestamp donné (si c'est une URL)."""
        if not source_url or not source_url.startswith("http"):
            self.toast("Pas de source vidéo associée à cette transcription.")
            return
        sep = "&" if "?" in source_url else "?"
        url = f"{source_url}{sep}t={int(seconds)}"
        threading.Thread(
            target=lambda: subprocess.run(["xdg-open", url]),
            daemon=True,
        ).start()

    def _md_inline(self, text: str) -> str:
        """Convertit le Markdown inline en markup Pango (échappé)."""
        s = html.escape(text)
        s = re.sub(r"`([^`]+)`", r"<tt>\1</tt>", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", s)
        s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)  # images ignorées
        s = re.sub(r"\[([^\]]+)\]\([^)]*\)",
                   r'<span underline="single" foreground="#ff7a18">\1</span>', s)
        return s

    def _build_md_view(self, md_text: str, collect: Optional[list] = None) -> Gtk.Widget:
        """Rendu simple du Markdown en widgets GTK (titres, listes, code...).

        Si ``collect`` est fourni (liste), chaque bloc de texte y est ajouté
        sous la forme ``(label, texte_brut)`` — utilisé pour la lecture
        synchronisée TTS (surlignage du paragraphe en cours).
        """
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      halign=Gtk.Align.FILL)

        def add_markup(markup, css_classes=None, raw=None):
            label = Gtk.Label(halign=Gtk.Align.START, wrap=True, xalign=0,
                              selectable=True, use_markup=True)
            label.set_markup(markup)
            if css_classes:
                label.set_css_classes(css_classes)
            box.append(label)
            if collect is not None:
                collect.append((label, raw if raw is not None else markup))

        def add_text(text, css_classes=None):
            label = Gtk.Label(halign=Gtk.Align.START, wrap=True, xalign=0,
                              selectable=True)
            label.set_label(text)
            if css_classes:
                label.set_css_classes(css_classes)
            box.append(label)
            if collect is not None:
                collect.append((label, text))

        in_code = False
        code_lines: list[str] = []
        for line in md_text.split("\n"):
            if line.strip().startswith("```"):
                if in_code:
                    add_text("\n".join(code_lines), css_classes=["md-code"])
                    code_lines = []
                    in_code = False
                else:
                    in_code = True
                continue
            if in_code:
                code_lines.append(line)
                continue

            stripped = line.strip()
            if not stripped:
                continue
            if set(stripped) == {"-"} and len(stripped) >= 3:
                sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
                sep.set_margin_top(6)
                sep.set_margin_bottom(6)
                box.append(sep)
            elif stripped.startswith("### "):
                add_markup(self._md_inline(stripped[4:]),
                           ["md-heading", "title-4"], raw=stripped[4:])
            elif stripped.startswith("## "):
                add_markup(self._md_inline(stripped[3:]),
                           ["md-heading", "title-3"], raw=stripped[3:])
            elif stripped.startswith("# "):
                add_markup(self._md_inline(stripped[2:]),
                           ["md-heading", "title-2"], raw=stripped[2:])
            elif stripped.startswith("> "):
                add_markup(f"<i>{self._md_inline(stripped[2:])}</i>",
                           ["dim-label"], raw=stripped[2:])
            elif re.match(r"^[-*+]\s+", stripped):
                item = re.sub(r"^[-*+]\s+", "", stripped)
                add_markup("•  " + self._md_inline(item), raw=item)
            else:
                add_markup(self._md_inline(stripped), raw=stripped)

        if in_code and code_lines:
            add_text("\n".join(code_lines), css_classes=["md-code"])
        return box

    # ------------------------------------------------------------------
    # Résumé IA (Mistral Large, rendu in-app)
    # ------------------------------------------------------------------

    def on_summarize_clicked(self, transcript: Optional[dict] = None):
        """
        Ouvre le résumé IA s'il est déjà persisté (et à jour), sinon le génère
        une seule fois puis l'ouvre. Un résumé en cours bloque la régénération.
        """
        transcript = transcript or self._selected_transcript()
        if not transcript:
            return
        if transcript["name"] in self._summaries_index():
            md = self._load_summary(transcript)
            if md is not None:
                self._show_summary_window(transcript, md)
                return
        if transcript["name"] in self._summaries_in_flight:
            self.toast("Résumé IA déjà en cours de génération…")
            return
        self._ensure_api_key(lambda: self._start_summary(transcript))

    # ------------------------------------------------------------------
    # Persistance des résumés IA (transcripts/.summaries/<nom>.md)
    # ------------------------------------------------------------------

    def _summary_path(self, transcript: dict) -> Path:
        return self.transcripts_dir / ".summaries" / f"{transcript['name']}.md"

    def _summaries_index(self) -> set[str]:
        """Noms des transcriptions dont le résumé IA persisté est à jour
        (marqueur mtime/taille comparé au fichier source)."""
        index: set[str] = set()
        sdir = self.transcripts_dir / ".summaries"
        if not sdir.exists():
            return index
        current = {}
        for t in self.all_transcripts:
            try:
                st = t["path"].stat()
                current[t["name"]] = (st.st_mtime, st.st_size)
            except Exception:
                pass
        for f in sdir.glob("*.md"):
            name = f.stem
            if name not in current:
                continue
            try:
                with f.open(encoding="utf-8") as fh:
                    first = fh.readline()
            except Exception:
                continue
            m = re.match(
                r"<!--\s*voxcast:summary\s+mtime=([\d.]+)\s+size=(\d+)\s*-->", first)
            if m and (float(m.group(1)), int(m.group(2))) == current[name]:
                index.add(name)
        return index

    def _tts_index(self) -> set[str]:
        """Noms des transcriptions dont le MP3 TTS existe (.summaries/<nom>.mp3)."""
        index: set[str] = set()
        sdir = self.transcripts_dir / ".summaries"
        if not sdir.exists():
            return index
        for f in sdir.glob("*.mp3"):
            if f.stem in {t["name"] for t in self.all_transcripts}:
                index.add(f.stem)
        return index

    def _load_summary(self, transcript: dict) -> Optional[str]:
        """Charge le résumé persisté (sans la ligne de métadonnées)."""
        try:
            text = self._summary_path(transcript).read_text(encoding="utf-8")
        except Exception:
            return None
        lines = text.split("\n")
        if lines and lines[0].startswith("<!-- voxcast:summary"):
            lines = lines[1:]
        return "\n".join(lines).strip()

    def _save_summary(self, transcript: dict, md: str):
        """Persiste le résumé avec un marqueur de fraîcheur (mtime/taille source)."""
        try:
            st = transcript["path"].stat()
            marker = (f"<!-- voxcast:summary mtime={st.st_mtime} "
                      f"size={st.st_size} -->\n\n")
            sdir = self.transcripts_dir / ".summaries"
            sdir.mkdir(parents=True, exist_ok=True)
            self._summary_path(transcript).write_text(marker + md, encoding="utf-8")
        except Exception as e:
            self.log_message(f"Sauvegarde résumé: {e}")

    # ------------------------------------------------------------------
    # Génération du résumé IA
    # ------------------------------------------------------------------

    def _start_summary(self, transcript: dict):
        try:
            text = transcript["path"].read_text(encoding="utf-8")
        except Exception as e:
            self.toast(f"Impossible de lire : {e}", "error")
            return

        name = transcript["name"]
        self._summaries_in_flight.add(name)

        dialog = Adw.Window(
            transient_for=self.window,
            modal=False,
            title=f"Résumé IA — {name}",
            default_width=780,
            default_height=680,
        )
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=_("Résumé IA"), css_classes=["title-4"]))
        toolbar.add_top_bar(header)

        loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                          halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                          vexpand=True)
        spinner = Adw.Spinner()
        spinner.set_size_request(48, 48)
        bar = Gtk.ProgressBar(css_classes=["voxcast-progress"],
                              width_request=300, show_text=False)
        status = Gtk.Label(
            label="Analyse de la transcription (Mistral Large)...",
            css_classes=["dim-label"])
        loading.append(spinner)
        loading.append(bar)
        loading.append(status)
        toolbar.set_content(loading)
        dialog.set_content(toolbar)
        dialog._summary_bar = bar
        dialog._summary_status = status
        dialog.present()

        def work():
            try:
                md = vtt.summarize_text(
                    self.api_key, text,
                    progress_callback=lambda f, msg: GLib.idle_add(
                        self._on_summary_progress, dialog, f, msg),
                )
                self._save_summary(transcript, md)
                GLib.idle_add(self._on_summary_done, dialog, transcript, md)
            except Exception as e:
                GLib.idle_add(self.toast, f"Résumé impossible : {e}", "error", 6)
                GLib.idle_add(self._summaries_in_flight.discard, name)
                GLib.idle_add(dialog.close)

        threading.Thread(target=work, daemon=True).start()

    def _on_summary_progress(self, dialog, frac: float, msg: str):
        try:
            dialog._summary_bar.set_fraction(frac)
            dialog._summary_status.set_label(msg)
        except Exception:
            pass
        return False

    def _on_summary_done(self, dialog, transcript: dict, md: str):
        """Résumé généré + persisté : notif, pastille, fenêtre de lecture."""
        self._summaries_in_flight.discard(transcript["name"])
        dialog.close()
        self.toast("Résumé IA disponible", "success")
        self._send_notification("Résumé IA disponible", transcript["name"])
        self.refresh_transcripts_list()  # fait apparaître la pastille « Résumé IA »
        self._show_summary_window(transcript, md)
        return False

    def _show_summary_window(self, transcript: dict, md: str):
        """Fenêtre affichant un résumé IA (généré ou chargé depuis le cache),
        avec lecture audio TTS synchronisée (surlignage des paragraphes)."""
        self._last_summary = md

        dialog = Adw.Window(
            transient_for=self.window,
            modal=False,
            title=f"Résumé IA — {transcript['name']}",
            default_width=780,
            default_height=680,
        )
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=_("Résumé IA"), css_classes=["title-4"]))
        tts_btn = Gtk.Button(
            icon_name="media-playback-start-symbolic",
            tooltip_text=_("Lire le résumé à voix haute (TTS)"))
        copy_btn = Gtk.Button(icon_name="edit-copy-symbolic",
                              tooltip_text=_("Copier le résumé (Markdown)"))
        copy_btn.connect("clicked", lambda *_a: self._copy_to_clipboard(md))
        header.pack_end(copy_btn)
        header.pack_end(tts_btn)
        toolbar.add_top_bar(header)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        inner.set_margin_top(18)
        inner.set_margin_bottom(24)
        inner.set_margin_start(24)
        inner.set_margin_end(24)
        blocks: list = []  # [(label, texte_brut)] pour la synchro TTS
        inner.append(self._build_md_view(md, collect=blocks))

        scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            child=Adw.Clamp(maximum_size=720, child=inner),
        )
        scrolled.set_vexpand(True)
        toolbar.set_content(scrolled)
        dialog.set_content(toolbar)

        dialog._tts = {
            "transcript": transcript,
            "btn": tts_btn,
            "scrolled": scrolled,
            "blocks": blocks,
            "player": None,
            "poll_id": None,
            "current": -1,
            "generating": False,
        }
        tts_btn.connect("clicked", lambda *_a: self._on_tts_toggle(dialog))
        dialog.connect("close-request", self._on_summary_close)
        dialog.present()

    # ------------------------------------------------------------------
    # Lecture TTS du résumé + surlignage synchronisé
    # ------------------------------------------------------------------

    def _tts_path(self, transcript: dict) -> Path:
        return self.transcripts_dir / ".summaries" / f"{transcript['name']}.mp3"

    def _on_summary_close(self, dialog):
        self._tts_stop(dialog)
        return False  # laisse la fenêtre se fermer

    def _tts_stop(self, dialog):
        t = getattr(dialog, "_tts", None)
        if not t:
            return
        if t["poll_id"] is not None:
            GLib.source_remove(t["poll_id"])
            t["poll_id"] = None
        if t["player"] is not None:
            try:
                t["player"].set_paused(True)
            except Exception:
                pass
            try:
                t["player"].stop()  # libère le pipeline GStreamer
            except Exception:
                pass
            t["player"] = None
        if t["current"] >= 0 and t["current"] < len(t["blocks"]):
            try:
                t["blocks"][t["current"]][0].remove_css_class("tts-current")
            except Exception:
                pass
        t["current"] = -1
        try:
            t["btn"].set_icon_name("media-playback-start-symbolic")
        except Exception:
            pass

    def _on_tts_toggle(self, dialog):
        t = dialog._tts
        if t["player"] is not None:
            # pause / reprise
            try:
                paused = t["player"].get_paused()
                t["player"].set_paused(not paused)
                t["btn"].set_icon_name(
                    "media-playback-pause-symbolic" if paused
                    else "media-playback-start-symbolic")
            except Exception:
                self._tts_stop(dialog)
            return
        mp3 = self._tts_path(t["transcript"])
        if not mp3.exists():
            if t["generating"]:
                return
            t["generating"] = True
            t["btn"].set_icon_name("content-loading-symbolic")
            self.toast(_("Génération de la voix TTS..."), "info", 6)
            md = self._load_summary(t["transcript"]) or ""

            def work():
                try:
                    vtt.tts_generate(self.api_key, md, mp3,
                                     voice_id=self.tts_voice or None)
                    GLib.idle_add(self._tts_after_generate, dialog, True, "")
                except Exception as e:
                    GLib.idle_add(self._tts_after_generate, dialog, False, str(e))

            threading.Thread(target=work, daemon=True).start()
            return
        self._tts_play(dialog)

    def _tts_after_generate(self, dialog, ok: bool, err: str):
        t = dialog._tts
        t["generating"] = False
        if not ok:
            t["btn"].set_icon_name("media-playback-start-symbolic")
            self.toast(f"TTS impossible : {err}", "error", 6)
            return False
        self.refresh_transcripts_list()  # fait apparaître la pastille « TTS »
        self._tts_play(dialog)
        return False

    def _tts_play(self, dialog):
        t = dialog._tts
        mp3 = self._tts_path(t["transcript"])
        player = GstPlayer(mp3)
        player.play()
        t["player"] = player
        t["btn"].set_icon_name("media-playback-pause-symbolic")
        t["poll_id"] = GLib.timeout_add(200, self._tts_poll, dialog)
        # arrêt automatique en fin de lecture (si la durée est connue)
        dur = player.get_duration()  # µs
        if dur > 0:
            GLib.timeout_add(int(dur / 1000) + 800,
                             lambda: (self._tts_stop(dialog), False)[1])

    def _tts_poll(self, dialog):
        """À chaque tick : position -> paragraphe courant -> surlignage + scroll."""
        t = getattr(dialog, "_tts", None)
        if t is None or t["player"] is None:
            return False
        try:
            player = t["player"]
            dur = player.get_duration()
            ts = player.get_timestamp()
            if dur <= 0:
                return True
            frac = max(0.0, min(1.0, ts / dur))
            # part de caractères cumulée -> index de bloc
            total = sum(max(1, len(raw)) for _b, raw in t["blocks"]) or 1
            target = frac * total
            acc = 0
            idx = len(t["blocks"]) - 1
            for i, (_b2, raw) in enumerate(t["blocks"]):
                acc += max(1, len(raw))
                if acc >= target:
                    idx = i
                    break
            if idx != t["current"]:
                if t["current"] >= 0:
                    t["blocks"][t["current"]][0].remove_css_class("tts-current")
                t["blocks"][idx][0].add_css_class("tts-current")
                t["current"] = idx
                adj = t["scrolled"].get_vadjustment()
                n = len(t["blocks"])
                adj.set_value((idx + 0.5) / n * (adj.get_upper() - adj.get_page_size()))
            if ts >= dur:
                self._tts_stop(dialog)
                return False
        except Exception:
            return False
        return True

    def _copy_to_clipboard(self, text: str):
        try:
            Gdk.Display.get_default().get_clipboard().set(text)
            self.toast("Copié dans le presse-papiers", "success")
        except Exception as e:
            self.toast(f"Copie impossible : {e}", "error")

    # ========================================================================
    # CHAT AVEC LA TRANSCRIPTION (Mistral Large + outil web_search)
    # ========================================================================

    def _chat_path(self, transcript: dict) -> Path:
        return self.transcripts_dir / ".summaries" / f"{transcript['name']}.chat.json"

    def on_chat_clicked(self, transcript: Optional[dict] = None):
        """Ouvre la fenêtre de chat sur une transcription (clé API requise)."""
        transcript = transcript or self._selected_transcript()
        if not transcript:
            return
        self._ensure_api_key(lambda: self._show_chat_window(transcript))

    def _load_chat_history(self, transcript: dict) -> list[dict]:
        try:
            data = json.loads(self._chat_path(transcript).read_text(encoding="utf-8"))
        except Exception:
            return []
        if not isinstance(data, list):
            return []
        # ne garder que les tours user/assistant affichables (exclut system/tool)
        return [m for m in data
                if isinstance(m, dict)
                and m.get("role") in ("user", "assistant")
                and "tool_calls" not in m and m.get("content")]

    def _save_chat_history(self, transcript: dict, history: list[dict]):
        try:
            p = self._chat_path(transcript)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(history, ensure_ascii=False, indent=1),
                         encoding="utf-8")
        except Exception as e:
            self.log_message(f"chat history: {e}")

    def _add_chat_bubble(self, container: Gtk.Box, role: str, text: str):
        outer = Gtk.Box(
            halign=Gtk.Align.END if role == "user" else Gtk.Align.START,
            margin_top=4, margin_bottom=4,
            margin_start=60 if role == "user" else 0,
            margin_end=0 if role == "user" else 60,
        )
        bubble = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=4,
            css_classes=["chat-bubble-user" if role == "user"
                         else "chat-bubble-assistant"],
        )
        bubble.append(self._build_md_view(text))
        outer.append(bubble)
        container.append(outer)

    def _show_chat_window(self, transcript: dict):
        dialog = Adw.Window(
            transient_for=self.window,
            modal=False,
            title=f"{_('Discuter avec l\'IA')} — {transcript['name']}",
            default_width=760,
            default_height=640,
        )
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=_("Discuter avec l'IA"),
                                          css_classes=["title-4"]))
        toolbar.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self._chat_scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            vexpand=True,
        )
        self._chat_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._chat_box.set_margin_top(16)
        self._chat_box.set_margin_bottom(16)
        self._chat_box.set_margin_start(16)
        self._chat_box.set_margin_end(16)
        self._chat_scroll.set_child(Adw.Clamp(maximum_size=700,
                                              child=self._chat_box))
        content.append(self._chat_scroll)

        status = Gtk.Label(label="", halign=Gtk.Align.START,
                           margin_start=14, css_classes=["caption", "dim-label"])
        bottom = Gtk.Box(spacing=8)
        bottom.set_margin_top(8)
        bottom.set_margin_bottom(10)
        bottom.set_margin_start(12)
        bottom.set_margin_end(12)
        entry = Gtk.Entry(
            placeholder_text=_("Posez une question sur cette transcription..."),
            hexpand=True,
        )
        send_btn = Gtk.Button(label=_("Envoyer"),
                              css_classes=["suggested-action"])
        bottom.append(entry)
        bottom.append(send_btn)
        content.append(status)
        content.append(bottom)

        toolbar.set_content(content)
        dialog.set_content(toolbar)

        state = {"history": self._load_chat_history(transcript), "busy": False,
                 "cancel": False, "started": 0.0, "last_msg": "",
                 "watch_id": None}
        dialog._chat_state = state
        dialog._chat_transcript = transcript
        dialog._chat_status = status
        dialog._chat_entry = entry

        for m in state["history"]:
            self._add_chat_bubble(self._chat_box, m.get("role", "assistant"),
                                  m.get("content", ""))

        def scroll_bottom():
            adj = self._chat_scroll.get_vadjustment()
            adj.set_value(adj.get_upper() - adj.get_page_size())

        def send(*_a):
            q = entry.get_text().strip()
            if not q or state["busy"]:
                return
            entry.set_text("")
            state["busy"] = True
            state["cancel"] = False
            state["started"] = time.monotonic()
            state["last_msg"] = _("Réflexion...")
            self._add_chat_bubble(self._chat_box, "user", q)
            state["history"].append({"role": "user", "content": q})
            status.set_label(state["last_msg"])
            GLib.idle_add(scroll_bottom)
            # watchdog : affiche le temps écoulé et annule après 10 min
            if state["watch_id"] is not None:
                GLib.source_remove(state["watch_id"])
            state["watch_id"] = GLib.timeout_add(
                5000, self._chat_watchdog, dialog, 600)

            def work():
                try:
                    text = transcript["path"].read_text(encoding="utf-8")
                    answer, new_hist = vtt.chat_with_tools(
                        self.api_key, text, state["history"][:-1], q,
                        progress_callback=lambda msg: GLib.idle_add(
                            self._on_chat_progress, dialog, msg),
                        cancel_check=lambda: state["cancel"],
                    )
                    GLib.idle_add(self._on_chat_answer, dialog, answer,
                                  new_hist)
                except vtt.ChatCancelled:
                    GLib.idle_add(self._on_chat_error, dialog,
                                  _("Annulé (délai dépassé)"))
                except Exception as e:
                    GLib.idle_add(self._on_chat_error, dialog, str(e))

            threading.Thread(target=work, daemon=True).start()

        send_btn.connect("clicked", send)
        entry.connect("activate", send)
        dialog.present()
        GLib.idle_add(scroll_bottom)

    def _on_chat_progress(self, dialog, msg: str):
        """Mémorise le dernier message de progression (le watchdog y ajoute
        le temps écoulé)."""
        state = dialog._chat_state
        state["last_msg"] = msg
        if state["busy"] and state["started"]:
            elapsed = time.monotonic() - state["started"]
            dialog._chat_status.set_label(f"{msg} — {int(elapsed)}s")
        else:
            dialog._chat_status.set_label(msg)
        return False

    def _chat_watchdog(self, dialog, max_seconds: int):
        """Toutes les 5s pendant une réponse : affiche le temps écoulé et
        annule (avec alerte) si le backend ne répond plus depuis trop longtemps."""
        state = dialog._chat_state
        if not state["busy"]:
            state["watch_id"] = None
            return False
        elapsed = time.monotonic() - state["started"]
        if elapsed > max_seconds:
            state["cancel"] = True
            state["watch_id"] = None
            self._on_chat_error(
                dialog, _("Délai dépassé ({s}s) — le backend ne répond plus, "
                          "discussion annulée").format(s=max_seconds))
            return False
        if elapsed > 60:
            # alerte visuelle douce dès 1 min sans réponse
            dialog._chat_status.set_label(
                f"{state['last_msg']} — {int(elapsed)}s "
                f"({_('réponse lente...')})")
        return True

    def _on_chat_answer(self, dialog, answer: str, history: list[dict]):
        state = dialog._chat_state
        if state.get("watch_id") is not None:
            GLib.source_remove(state["watch_id"])
            state["watch_id"] = None
        state["busy"] = False
        state["history"] = history
        self._add_chat_bubble(self._chat_box, "assistant", answer)
        dialog._chat_status.set_label("")
        self._save_chat_history(dialog._chat_transcript, history)
        adj = self._chat_scroll.get_vadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size())
        return False

    def _on_chat_error(self, dialog, message: str):
        state = dialog._chat_state
        if state.get("watch_id") is not None:
            GLib.source_remove(state["watch_id"])
            state["watch_id"] = None
        was_busy = state["busy"]
        state["busy"] = False
        dialog._chat_status.set_label(f"{_('Erreur')} : {message}")
        if was_busy:
            self.toast(f"Chat impossible : {message}", "error", 8)
        return False

    # ========================================================================
    # EXPORT SRT/VTT/HTML/PDF DEPUIS UNE TRANSCRIPTION EXISTANTE
    # ========================================================================

    def on_export_srt(self, transcript: dict):
        if not transcript:
            return
        self._export_subtitle(transcript, "srt")

    def on_export_vtt(self, transcript: dict):
        if not transcript:
            return
        self._export_subtitle(transcript, "vtt")

    # ------------------------------------------------------------------
    # Export enrichi HTML / PDF
    # ------------------------------------------------------------------

    def _md_to_html(self, title: str, md_text: str) -> str:
        """Convertit le Markdown de transcription en HTML autonome (CSS inclus).

        - titres avec ancres (#chapitre-N) + sommaire des chapitres
        - timestamps [HH:MM:SS] cliquables en <span class="ts">
        """
        def esc(s: str) -> str:
            return html.escape(s, quote=False)

        def inline(s: str) -> str:
            s = esc(s)
            s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
            s = re.sub(r"\*(.+?)\*", r"<em>\1</em>", s)
            s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
            s = re.sub(r"\[(\d{2}:\d{2}:\d{2})(?:\s*-\s*(\d{2}:\d{2}:\d{2}))?\]",
                       lambda m: f'<span class="ts">[{m.group(1)}'
                                 + (f' - {m.group(2)}' if m.group(2) else '')
                                 + "]</span>", s)
            return s

        toc: list[str] = []
        body: list[str] = []
        in_code = False
        code_lines: list[str] = []
        in_list = False

        def close_list():
            nonlocal in_list
            if in_list:
                body.append("</ul>")
                in_list = False

        for line in md_text.split("\n"):
            if line.strip().startswith("```"):
                if in_code:
                    body.append("<pre><code>" + esc("\n".join(code_lines))
                                + "</code></pre>")
                    code_lines = []
                    in_code = False
                else:
                    close_list()
                    in_code = True
                continue
            if in_code:
                code_lines.append(line)
                continue
            stripped = line.strip()
            if not stripped:
                close_list()
                continue
            m = re.match(r"^(#{1,3})\s+(.*)$", stripped)
            if m:
                close_list()
                level = len(m.group(1))
                text = m.group(2).strip()
                anchor = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
                if level <= 2 and anchor:
                    toc.append((anchor, text, level))
                body.append(
                    f'<h{level} id="{anchor}">{inline(text)}</h{level}>')
                continue
            if set(stripped) == {"-"} and len(stripped) >= 3:
                close_list()
                body.append("<hr>")
                continue
            m = re.match(r"^[-*+]\s+(.*)$", stripped)
            if m:
                if not in_list:
                    body.append("<ul>")
                    in_list = True
                body.append(f"<li>{inline(m.group(1))}</li>")
                continue
            close_list()
            if stripped.startswith("> "):
                body.append(f"<blockquote>{inline(stripped[2:])}</blockquote>")
            else:
                body.append(f"<p>{inline(stripped)}</p>")
        close_list()
        if in_code and code_lines:
            body.append("<pre><code>" + esc("\n".join(code_lines))
                        + "</code></pre>")

        toc_html = ""
        if toc:
            items = "".join(
                f'<li class="lvl{lvl}"><a href="#{a}">{esc(t)}</a></li>'
                for a, t, lvl in toc)
            toc_html = f"<nav class=\"toc\"><h2>Sommaire</h2><ul>{items}</ul></nav>"

        return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font-family: system-ui, sans-serif; max-width: 780px; margin: 2rem auto;
       padding: 0 1.2rem; line-height: 1.6; }}
h1,h2,h3 {{ line-height: 1.25; }}
.toc {{ background: rgba(127,127,127,.12); border-radius: 10px;
        padding: .8rem 1.2rem; margin-bottom: 1.6rem; }}
.toc ul {{ margin: .3rem 0; padding-left: 1.2rem; }}
.toc .lvl2 {{ margin-left: 1rem; }}
.ts {{ color: #3584e4; font-variant-numeric: tabular-nums; cursor: default;
       white-space: nowrap; }}
code {{ background: rgba(127,127,127,.18); border-radius: 4px; padding: 0 .25em; }}
pre {{ background: rgba(127,127,127,.15); padding: .8rem; border-radius: 8px;
        overflow-x: auto; }}
blockquote {{ border-left: 3px solid #3584e4; margin-left: 0;
               padding-left: 1rem; opacity: .85; }}
hr {{ border: none; border-top: 1px solid rgba(127,127,127,.35); }}
a {{ color: #3584e4; }}
</style>
</head>
<body>
<h1>{esc(title)}</h1>
{toc_html}
{chr(10).join(body)}
</body>
</html>
"""

    def on_export_html(self, transcript: dict):
        if not transcript:
            return
        try:
            md_text = transcript["path"].read_text(encoding="utf-8")
            out = transcript["path"].with_suffix(".html")
            out.write_text(self._md_to_html(transcript["name"], md_text),
                           encoding="utf-8")
            self.toast(f"HTML exporté : {out.name}", "success")
        except Exception as e:
            self.toast(f"Export HTML impossible : {e}", "error")

    def on_export_pdf(self, transcript: dict):
        if not transcript:
            return
        if not shutil.which("pandoc"):
            self.toast(
                "pandoc n'est pas installé — export PDF indisponible "
                "(export HTML possible)", "error", 6)
            return
        try:
            md_text = transcript["path"].read_text(encoding="utf-8")
            tmp_md = transcript["path"].with_suffix(".pdfsrc.md")
            tmp_md.write_text(md_text, encoding="utf-8")
            out = transcript["path"].with_suffix(".pdf")
            r = subprocess.run(
                ["pandoc", str(tmp_md), "-o", str(out),
                 "--metadata", f"title={transcript['name']}",
                 "--pdf-engine=xelatex"],
                capture_output=True, text=True, timeout=120)
            tmp_md.unlink(missing_ok=True)
            if r.returncode == 0 and out.exists():
                self.toast(f"PDF exporté : {out.name}", "success")
            else:
                self.toast(f"pandoc a échoué : {r.stderr[:200]}", "error", 6)
        except Exception as e:
            self.toast(f"Export PDF impossible : {e}", "error")

    def _export_subtitle(self, transcript: dict, fmt: str):
        """Parse un .md de transcription et exporte en SRT ou VTT."""
        path = transcript["path"]
        content = path.read_text(encoding="utf-8")

        # Parser les segments [HH:MM:SS - HH:MM:SS] ou [HH:MM:SS] **Speaker**:
        import re
        entries = []
        idx = 1

        # Pattern: [timestamp] ou [start - end]
        seg_pattern = re.compile(
            r'\[(\d{2}:\d{2}:\d{2})(?:\s*-\s*(\d{2}:\d{2}:\d{2}))?\]'
        )
        speaker_pattern = re.compile(r'\*\*(.+?)\*\*:?')

        lines = content.split("\n")
        current_ts = None
        current_end = None
        current_speaker = None
        current_text = []

        for line in lines:
            ts_match = seg_pattern.match(line)
            if ts_match:
                # Sauvegarder le segment précédent
                if current_ts is not None and current_text:
                    text = " ".join(current_text).strip()
                    if text:
                        entries.append((idx, current_ts, current_end or current_ts,
                                        f"{current_speaker + ': ' if current_speaker else ''}{text}"))
                        idx += 1
                current_ts = ts_match.group(1)
                current_end = ts_match.group(2)
                spk_match = speaker_pattern.search(line)
                current_speaker = spk_match.group(1) if spk_match else None
                current_text = []
            elif current_ts is not None and line.strip() and not line.startswith("---") and not line.startswith("##"):
                current_text.append(line.strip())
            elif current_ts is not None and not line.strip() and current_text:
                text = " ".join(current_text).strip()
                if text:
                    entries.append((idx, current_ts, current_end or current_ts,
                                    f"{current_speaker + ': ' if current_speaker else ''}{text}"))
                    idx += 1
                current_text = []
                current_ts = None
                current_end = None
                current_speaker = None

        # Dernier segment
        if current_ts is not None and current_text:
            text = " ".join(current_text).strip()
            if text:
                entries.append((idx, current_ts, current_end or current_ts,
                                f"{current_speaker + ': ' if current_speaker else ''}{text}"))

        if not entries:
            self.toast("Aucun segment avec timestamps trouvé dans la transcription.", "error", 6)
            return

        # Convertir timestamps string -> secondes
        def ts_to_secs(ts: str) -> float:
            parts = ts.split(":")
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])

        # Écrire le fichier
        out_path = path.with_suffix(f".{fmt}")
        out_lines = []

        if fmt == "vtt":
            out_lines.append("WEBVTT")
            out_lines.append("")

        for idx, start_ts, end_ts, text in entries:
            start_s = ts_to_secs(start_ts)
            end_s = ts_to_secs(end_ts)

            if fmt == "srt":
                out_lines.append(str(idx))
                out_lines.append(f"{vtt._format_srt_timestamp(start_s)} --> {vtt._format_srt_timestamp(end_s)}")
            else:
                out_lines.append(f"{vtt._format_vtt_timestamp(start_s)} --> {vtt._format_vtt_timestamp(end_s)}")

            out_lines.append(text)
            out_lines.append("")

        out_path.write_text("\n".join(out_lines), encoding="utf-8")
        self.toast(f"Exporté : {out_path.name} ({len(entries)} segments)", "success")

    # ========================================================================
    # ACTIONS TRANSCRIPTIONS
    # ========================================================================

    def on_open_transcript_clicked(self, transcript: Optional[dict] = None):
        transcript = transcript or self._selected_transcript()
        if not transcript:
            return
        try:
            subprocess.run(["xdg-open", str(transcript["path"])], check=True)
        except Exception as e:
            self.toast(f"Impossible d'ouvrir : {e}", "error")

    def on_open_folder_clicked(self, button=None):
        try:
            subprocess.run(["xdg-open", str(self.transcripts_dir)], check=True)
        except Exception as e:
            self.toast(f"Impossible d'ouvrir : {e}", "error")

    def on_delete_transcript_clicked(self, transcript: Optional[dict] = None):
        transcript = transcript or self._selected_transcript()
        if not transcript:
            return
        name = transcript["name"]
        path = transcript["path"]

        dialog = Adw.MessageDialog(
            transient_for=self.window,
            title=_("Supprimer la transcription"),
            body=f"Supprimer : {name} ?",
            modal=True,
        )
        dialog.add_response("cancel", _("Annuler"))
        dialog.add_response("delete", _("Supprimer"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._on_delete_response, path)
        dialog.present()

    def _on_delete_response(self, dialog, response, path):
        if response == "delete":
            try:
                path.unlink()
                # Supprime aussi le résumé IA associé s'il existe
                self._summary_path({"name": path.stem}).unlink(missing_ok=True)
                self.toast(f"Supprimé : {path.stem}")
                self.refresh_transcripts_list()
            except Exception as e:
                self.toast(f"Erreur : {e}", "error")
        dialog.destroy()

    # ========================================================================
    # PARAMÈTRES
    # ========================================================================

    def on_settings_clicked(self, button):
        dialog = Adw.PreferencesDialog()
        dialog.set_title(_("Paramètres"))

        page = Adw.PreferencesPage()
        page.set_title(_("Général"))

        # Clé API
        api_group = Adw.PreferencesGroup(title=_("API Mistral"))
        api_row = Adw.EntryRow(title=_("Clé API"))
        api_row.set_text(self.api_key)
        api_row.connect("changed", self.on_api_key_changed)
        api_group.add(api_row)
        page.add(api_group)

        # Dossier de sortie
        output_group = Adw.PreferencesGroup(title=_("Dossier des transcriptions"))
        output_row = Adw.ActionRow(title=_("Dossier"), subtitle=str(self.transcripts_dir))
        browse_btn = Gtk.Button(label=_("Parcourir..."), halign=Gtk.Align.END)
        browse_btn.connect("clicked", self.on_browse_output, output_row)
        output_row.add_suffix(browse_btn)
        output_group.add(output_row)
        page.add(output_group)

        # Apparence : thème clair / sombre
        theme_group = Adw.PreferencesGroup(title=_("Apparence"))
        theme_row = Adw.ActionRow(
            title=_("Thème"),
            subtitle=_("Clair, sombre ou celui du système"),
        )
        theme_model = Gtk.StringList()
        for name in [_("Système"), _("Clair"), _("Sombre")]:
            theme_model.append(name)
        theme_idx = {"default": 0, "light": 1, "dark": 2}[self.color_scheme]
        self.theme_dropdown = Gtk.DropDown(
            model=theme_model, selected=theme_idx, valign=Gtk.Align.CENTER)
        self.theme_dropdown.connect("notify::selected", self.on_theme_changed)
        theme_row.add_suffix(self.theme_dropdown)
        theme_row.set_activatable_widget(self.theme_dropdown)
        theme_group.add(theme_row)
        page.add(theme_group)

        # Traitement parallèle
        proc_group = Adw.PreferencesGroup(title=_("Traitement"))
        workers_row = Adw.ActionRow(
            title=_("Transcriptions en parallèle"),
            subtitle=_("Nombre d'éléments traités simultanément"),
        )
        adjustment = Gtk.Adjustment(
            value=self.max_workers, lower=1, upper=8, step_increment=1)
        self.workers_spin = Gtk.SpinButton(
            adjustment=adjustment, valign=Gtk.Align.CENTER)
        self.workers_spin.connect("value-changed", self.on_workers_changed)
        workers_row.add_suffix(self.workers_spin)
        workers_row.set_activatable_widget(self.workers_spin)
        proc_group.add(workers_row)
        page.add(proc_group)

        # Voix TTS pour la lecture des résumés
        tts_group = Adw.PreferencesGroup(
            title=_("Lecture vocale (TTS)"),
            description=_("Voix utilisée pour lire les résumés IA à voix haute"),
        )
        tts_row = Adw.ActionRow(title=_("Voix TTS"))
        tts_model = Gtk.StringList()
        tts_model.append(_("Voix par défaut du modèle"))
        tts_row._voice_slugs = [""]
        selected = 0
        if self.tts_voice:
            tts_model.append(self.tts_voice)
            tts_row._voice_slugs.append(self.tts_voice)
            selected = 1
        self.tts_dropdown = Gtk.DropDown(
            model=tts_model, selected=selected, valign=Gtk.Align.CENTER)
        self.tts_dropdown.connect("notify::selected", self.on_tts_voice_changed)
        tts_row.add_suffix(self.tts_dropdown)
        tts_row.set_activatable_widget(self.tts_dropdown)
        tts_group.add(tts_row)
        page.add(tts_group)

        def fill_voices():
            if not self.api_key:
                return
            try:
                voices = vtt.list_voices(self.api_key)
            except Exception:
                return
            def apply():
                current = self.tts_dropdown.get_selected()
                cur_slug = (tts_row._voice_slugs[current]
                            if current < len(tts_row._voice_slugs) else "")
                while tts_model.get_n_items() > 1:
                    tts_model.remove(1)
                tts_row._voice_slugs = [""]
                sel = 0
                for i, v in enumerate(voices):
                    slug = v.get("slug") or v.get("id") or ""
                    name = v.get("name") or slug
                    tts_model.append(f"{name} ({slug})")
                    tts_row._voice_slugs.append(slug)
                    if slug and slug == cur_slug:
                        sel = i + 1
                self.tts_dropdown.set_selected(sel)
                return False
            GLib.idle_add(apply)

        threading.Thread(target=fill_voices, daemon=True).start()

        dialog.add(page)
        dialog.present()

    def on_tts_voice_changed(self, dropdown, _param):
        idx = dropdown.get_selected()
        row = dropdown.get_ancestor(Adw.ActionRow)
        slugs = getattr(row, "_voice_slugs", [""]) if row else [""]
        self.tts_voice = slugs[idx] if idx < len(slugs) else ""
        self.save_settings()

    def on_theme_changed(self, dropdown, _param):
        idx = dropdown.get_selected()
        self.color_scheme = {0: "default", 1: "light", 2: "dark"}[idx]
        self.save_settings()
        self._apply_color_scheme()

    def on_workers_changed(self, spin):
        self.max_workers = max(1, int(spin.get_value()))
        self.save_settings()

    def on_api_key_changed(self, row):
        self.api_key = row.get_text().strip()
        self.save_settings()
        if hasattr(self, "api_banner"):
            self.api_banner.set_revealed(not self.api_key)

    def on_browse_output(self, button, output_row):
        dialog = Gtk.FileChooserDialog(
            title=_("Dossier des transcriptions"),
            transient_for=self.window,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            modal=True,
        )
        dialog.add_buttons(_("Annuler"), Gtk.ResponseType.CANCEL, _("Valider"), Gtk.ResponseType.ACCEPT)
        dialog.connect("response", self._on_browse_response, output_row)
        dialog.present()

    def _on_browse_response(self, dialog, response_id, output_row):
        if response_id == Gtk.ResponseType.ACCEPT:
            selected = dialog.get_file()
            if selected:
                new_dir = Path(selected.get_path())
                self.transcripts_dir = new_dir
                output_row.set_subtitle(str(new_dir))
                self.save_settings()
                self.refresh_transcripts_list()
                self.log_message(f"Dossier: {new_dir}")
        dialog.destroy()

    # ========================================================================
    # URL CHANGED
    # ========================================================================

    def _on_source_changed(self):
        """Réagit à chaque changement de la source sans bloquer l'UI :
        - réponse visuelle immédiate (playlist détectée / « récupération... »)
        - debounce de 600 ms avant l'estimation durée/coût, qui elle fait un
          appel réseau yt-dlp exécuté dans un thread (voir _show_cost_estimate)."""
        source = self.url_entry.get_text().strip()
        if getattr(self, "_source_debounce_id", None) is not None:
            GLib.source_remove(self._source_debounce_id)
            self._source_debounce_id = None
        if not source:
            self.cost_label.set_label("")
            return
        if source.startswith("http") and vtt.is_playlist_url(source):
            self.cost_label.set_label(
                _("Playlist détectée — cliquez sur « Ajouter » pour choisir les vidéos."))
        else:
            self.cost_label.set_label(_("Récupération des informations..."))
        self._source_debounce_id = GLib.timeout_add(600, self._launch_source_check)

    def _launch_source_check(self):
        self._source_debounce_id = None
        self._check_source_type()
        return False

    def _ensure_api_key(self, on_success) -> bool:
        """
        Vérifie que la clé API est présente. Si absente, ouvre un dialogue
        pour la saisir. on_success est appelé si la clé est disponible.
        Retourne True si la clé était déjà présente (pas de dialogue affiché).
        """
        if self.api_key:
            on_success()
            return True

        dialog = Adw.MessageDialog(
            transient_for=self.window,
            title=_("Clé API Mistral requise"),
            body="Aucune clé API Mistral n'est configurée.\n"
                 "Saisissez votre clé pour continuer (elle sera sauvegardée).",
            modal=True,
        )

        entry = Gtk.Entry(
            placeholder_text="votre-cle-api...",
            hexpand=True,
            margin_top=10,
            margin_bottom=10,
        )
        entry.set_visibility(False)  # Masquer la clé
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        content_box.append(entry)
        dialog.set_extra_child(content_box)

        dialog.add_response("cancel", _("Annuler"))
        dialog.add_response("save", _("Enregistrer"))
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)

        def on_response(d, response):
            if response == "save":
                key = entry.get_text().strip()
                if key:
                    self.api_key = key
                    self.save_settings()
                    if hasattr(self, "api_banner"):
                        self.api_banner.set_revealed(False)
                    self.toast("Clé API enregistrée.", "success")
                    on_success()
            d.destroy()

        dialog.connect("response", on_response)
        dialog.present()
        return False

    # ========================================================================
    # UTILITAIRES
    # ========================================================================

    def log_message(self, message: str, color: str = None):
        """Écrit un message dans le shell + voxcast.log (plus de console dans l'UI)."""
        level_map = {"red": logging.ERROR, "yellow": logging.WARNING}
        logging.log(level_map.get(color, logging.INFO), message)

    def toast(self, message: str, kind: str = "info", timeout: int = 4):
        """Affiche un retour utilisateur non bloquant sous forme de toast."""
        toast = Adw.Toast(title=message, timeout=timeout)
        self.toast_overlay.add_toast(toast)

    def update_status(self, status: str, color: str = None):
        self.status_label.set_label(status)
        self.status_label.remove_css_class("error")
        self.status_label.remove_css_class("success")
        if color == "red":
            self.status_label.add_css_class("error")
        elif color == "green":
            self.status_label.add_css_class("success")

    def _format_size(self, size_bytes: int) -> str:
        for unit in ["o", "Ko", "Mo", "Go"]:
            if size_bytes < 1024:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024
        return f"{size_bytes:.1f} To"

    def save_settings(self):
        settings = {
            "api_key": self.api_key,
            "transcripts_dir": str(self.transcripts_dir),
            "max_workers": self.max_workers,
            "color_scheme": self.color_scheme,
            "custom_models": self.custom_models,
            "model_profile": self.model_profile,
            "tts_voice": self.tts_voice,
            "lang": LANG,
        }
        settings_path = APP_DIR / "settings.json"
        with open(settings_path, "w") as f:
            json.dump(settings, f, indent=2)

    def load_settings(self):
        settings_path = APP_DIR / "settings.json"
        try:
            with open(settings_path) as f:
                settings = json.load(f)
                self.api_key = settings.get("api_key", "")
                tdir = Path(settings.get("transcripts_dir", str(DEFAULT_TRANSCRIPTS_DIR)))
                if tdir.parent.exists():
                    self.transcripts_dir = tdir
                self.max_workers = int(settings.get("max_workers", 2))
                self.color_scheme = settings.get("color_scheme", "default")
                self.custom_models = settings.get("custom_models", []) or []
                self.model_profile = settings.get("model_profile")
                self.tts_voice = settings.get("tts_voice", "") or ""
                global_lang = settings.get("lang", "fr")
                if global_lang in ("fr", "en"):
                    globals()["LANG"] = global_lang
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _apply_color_scheme(self):
        """Applique le thème clair/sombre choisi (Adw.StyleManager)."""
        try:
            manager = Adw.StyleManager.get_default()
            if self.color_scheme == "light":
                manager.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
            elif self.color_scheme == "dark":
                manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
            else:
                manager.set_color_scheme(Adw.ColorScheme.DEFAULT)
        except Exception as e:
            self.log_message(f"Thème: {e}")

    def on_shutdown(self, app):
        self.cancelled = True
        self._save_queue()  # conserve les éléments en attente
        if self.queue_thread:
            self.queue_thread.join(timeout=3)

    def run(self):
        return self.app.run()


# ============================================================================
# POINT D'ENTRÉE
# ============================================================================

if __name__ == "__main__":
    app = VoxCastWindow()
    sys.exit(app.run())
