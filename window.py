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
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gtk, Gdk, Gio, GLib, Adw, Pango

import video_to_text as vtt


# ============================================================================
# CONFIGURATION
# ============================================================================
APP_ID = "com.voxcast.app"
APP_DIR = Path(__file__).parent
DEFAULT_TRANSCRIPTS_DIR = APP_DIR / "transcripts"
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

STATUS_LABELS = {
    STATUS_PENDING: "En attente",
    STATUS_RUNNING: "En cours",
    STATUS_DONE: "Terminé",
    STATUS_ERROR: "Erreur",
    STATUS_CANCELLED: "Annulé",
}

PILL_CLASSES = [f"pill-{s}" for s in STATUS_LABELS]

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
        settings_btn.set_tooltip_text("Paramètres")
        settings_btn.connect("clicked", self.on_settings_clicked)
        header.pack_end(settings_btn)

        # Bannière quand la clé API manque
        self.api_banner = Adw.Banner(
            title="Aucune clé API Mistral configurée — les transcriptions seront impossibles",
            button_label="Configurer",
        )
        self.api_banner.connect("button-clicked",
                                lambda *_: self.on_settings_clicked(None))
        self.api_banner.set_revealed(not self.api_key)

        # ===== CONTENU : deux pages =====
        self.stack.add_titled_with_icon(
            self._build_transcribe_page(), "transcribe", "Transcrire",
            "audio-input-microphone-symbolic")
        self.stack.add_titled_with_icon(
            self._build_library_page(), "library", "Bibliothèque",
            "folder-documents-symbolic")

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

        self.window.present()

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
            label="Transcrivez vos vidéos et fichiers audio en Markdown — propulsé par Voxtral.",
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
        group = Adw.PreferencesGroup(title="Nouvelle transcription")

        # URL ou fichier
        source_row = Gtk.Box(spacing=10)
        self.url_entry = Gtk.Entry(
            placeholder_text="URL YouTube ou glissez un fichier audio ici",
            hexpand=True,
        )
        self.url_entry.connect("activate", lambda *_: self.on_add_to_queue())
        self.url_entry.connect("changed", lambda *_: self._on_source_changed())

        browse_btn = Gtk.Button(
            icon_name="document-open-symbolic",
            tooltip_text="Parcourir un fichier audio",
        )
        browse_btn.connect("clicked", self.on_browse_audio)

        add_btn = Gtk.Button(
            label="Ajouter",
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
        options = Adw.ExpanderRow(title="Options avancées")

        # Modèle de transcription (presets Mistral + modèles perso)
        model_row = Adw.ActionRow(
            title="Modèle de transcription",
            subtitle="Preset Mistral ou API perso (compatible /v1/audio/transcriptions)",
        )
        self.model_dropdown = Gtk.DropDown(valign=Gtk.Align.CENTER)
        self.model_dropdown.connect("notify::selected", self.on_model_changed)
        self._rebuild_model_dropdown()
        manage_btn = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER,
                                tooltip_text="Gérer les modèles personnalisés")
        manage_btn.connect("clicked", self.on_manage_models)
        model_suffix = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        model_suffix.append(self.model_dropdown)
        model_suffix.append(manage_btn)
        model_row.add_suffix(model_suffix)
        model_row.set_activatable_widget(self.model_dropdown)
        options.add_row(model_row)

        # Nom de sortie
        name_row = Adw.EntryRow(title="Nom de sortie (auto si vide)")
        self.name_entry = name_row
        options.add_row(name_row)

        # Langue + Timestamps
        lang_row = Adw.ActionRow(title="Langue", subtitle="Forcer la langue améliore la précision")
        lang_model = Gtk.StringList()
        for _, name in LANGUAGES:
            lang_model.append(name)
        self.lang_dropdown = Gtk.DropDown(model=lang_model, valign=Gtk.Align.CENTER)
        lang_row.add_suffix(self.lang_dropdown)
        lang_row.set_activatable_widget(self.lang_dropdown)
        options.add_row(lang_row)

        ts_row = Adw.ActionRow(title="Timestamps", subtitle="Inclure les horaires dans la sortie")
        ts_model = Gtk.StringList()
        for _, name in TIMESTAMP_OPTIONS:
            ts_model.append(name)
        self.ts_dropdown = Gtk.DropDown(model=ts_model, valign=Gtk.Align.CENTER)
        ts_row.add_suffix(self.ts_dropdown)
        ts_row.set_activatable_widget(self.ts_dropdown)
        options.add_row(ts_row)

        # Diarisation
        diarize_row = Adw.ActionRow(title="Diarisation", subtitle="Identifier les speakers")
        self.diarize_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        diarize_row.add_suffix(self.diarize_switch)
        diarize_row.set_activatable_widget(self.diarize_switch)
        options.add_row(diarize_row)

        # Garder l'audio
        keep_row = Adw.ActionRow(title="Garder l'audio", subtitle="Conserver le MP3 téléchargé")
        self.keep_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        keep_row.add_suffix(self.keep_switch)
        keep_row.set_activatable_widget(self.keep_switch)
        options.add_row(keep_row)

        # Segment (transcription partielle)
        seg_row = Adw.ActionRow(title="Segment", subtitle="Transcrire seulement une partie (HH:MM:SS)")
        seg_box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.start_entry = Gtk.Entry(placeholder_text="Début", width_chars=7)
        self.end_entry = Gtk.Entry(placeholder_text="Fin", width_chars=7)
        seg_box.append(self.start_entry)
        seg_box.append(Gtk.Label(label="→"))
        seg_box.append(self.end_entry)
        seg_row.add_suffix(seg_box)
        options.add_row(seg_row)

        # Context bias
        bias_row = Adw.EntryRow(title="Context bias (termes, espaces, max 100)")
        self.bias_entry = bias_row
        options.add_row(bias_row)

        # Exports sous-titres
        srt_row = Adw.ActionRow(title="Exporter en SRT", subtitle="Sous-titres SubRip")
        self.srt_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        srt_row.add_suffix(self.srt_switch)
        srt_row.set_activatable_widget(self.srt_switch)
        options.add_row(srt_row)

        vtt_row = Adw.ActionRow(title="Exporter en VTT", subtitle="Sous-titres WebVTT")
        self.vtt_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        vtt_row.add_suffix(self.vtt_switch)
        vtt_row.set_activatable_widget(self.vtt_switch)
        options.add_row(vtt_row)

        group.add(options)
        return group

    def _build_queue_section(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="File d'attente")

        # Actions de la file dans le header : Démarrer / Annuler / menu ⋮
        header_box = Gtk.Box(spacing=6)

        self.start_btn = Gtk.Button(
            label="Démarrer",
            icon_name="media-playback-start-symbolic",
        )
        self.start_btn.add_css_class("suggested-action")
        self.start_btn.set_sensitive(False)
        self.start_btn.connect("clicked", self.on_start_queue)

        self.cancel_btn = Gtk.Button(
            icon_name="process-stop-symbolic",
            tooltip_text="Annuler le traitement en cours",
            sensitive=False,
        )
        self.cancel_btn.connect("clicked", self.on_cancel_clicked)

        queue_menu_btn = Gtk.MenuButton(
            icon_name="view-more-symbolic",
            tooltip_text="Actions sur la file",
        )
        queue_menu = Gio.Menu()
        queue_menu.append("Réessayer les échecs", "queue.retry")
        queue_menu.append("Nettoyer les terminés", "queue.clear")
        queue_menu_btn.set_menu_model(queue_menu)

        queue_actions = Gio.SimpleActionGroup()
        act_retry = Gio.SimpleAction(name="retry")
        act_retry.connect("activate", lambda *_: self.on_retry_failed())
        act_clear = Gio.SimpleAction(name="clear")
        act_clear.connect("activate", lambda *_: self.on_clear_done())
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
            title="File d'attente vide",
            description="Ajoutez une URL YouTube ou un fichier audio ci-dessus pour commencer.",
        )
        browse_empty_btn = Gtk.Button(label="Parcourir un fichier audio")
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
        group = Adw.PreferencesGroup(title="Transcriptions")

        # Recherche + bouton dossier dans le header
        self.search_entry = Gtk.SearchEntry(
            placeholder_text="Rechercher (nom ou contenu)...",
            hexpand=True,
        )
        self.search_entry.connect("search-changed", self.on_search_changed)

        folder_btn = Gtk.Button(
            icon_name="folder-open-symbolic",
            tooltip_text="Ouvrir le dossier des transcriptions",
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
            title="Aucune transcription",
            description="Les fichiers Markdown apparaîtront ici après le traitement de la file.",
        )
        open_folder_btn = Gtk.Button(label="Ouvrir le dossier")
        open_folder_btn.connect("clicked", self.on_open_folder_clicked)
        empty_page.set_child(open_folder_btn)

        no_results_page = Adw.StatusPage(
            icon_name="edit-find-symbolic",
            title="Aucun résultat",
            description="Aucune transcription ne correspond à cette recherche.",
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
            label="Prêt",
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
        """Affiche une estimation du coût et de la durée, et auto-remplit le nom."""
        if not source:
            self.cost_label.set_label("")
            return

        try:
            if is_local and Path(source).exists():
                duration = vtt.get_audio_duration_seconds(Path(source))
                cost = vtt.estimate_cost(duration)
                self.cost_label.set_label(
                    f"Durée: {vtt.format_timestamp(duration)} | "
                    f"Coût estimé: ${cost:.2f}"
                )
                # Auto-remplir avec le nom du fichier
                if not self.name_entry.get_text().strip():
                    auto_name = vtt.sanitize_filename(Path(source).stem)
                    self.name_entry.set_text(auto_name)
            elif source.startswith("http"):
                if vtt.is_playlist_url(source):
                    self.cost_label.set_label(
                        "Playlist détectée — cliquez sur « Ajouter » pour choisir les vidéos.")
                    return
                # Pour les URLs, on fetch les infos (avec cache)
                info = vtt.get_video_info_cached(source)
                duration = info.get("duration", 0)
                cost = vtt.estimate_cost(duration)
                self.cost_label.set_label(
                    f"Durée: {vtt.format_timestamp(duration)} | "
                    f"Coût estimé: ${cost:.2f}"
                )
                # Auto-remplir avec le titre de la vidéo
                if not self.name_entry.get_text().strip():
                    title = info.get("title", "")
                    if title:
                        auto_name = vtt.sanitize_filename(title)
                        self.name_entry.set_text(auto_name)
            else:
                self.cost_label.set_label("")
        except Exception:
            self.cost_label.set_label("")

    # ========================================================================
    # FILE D'ATTENTE
    # ========================================================================

    def on_browse_audio(self, button):
        """Ouvre un dialogue de sélection de fichier audio."""
        dialog = Gtk.FileChooserDialog(
            title="Choisir un fichier audio",
            transient_for=self.window,
            action=Gtk.FileChooserAction.OPEN,
            modal=True,
        )
        # Filtres audio
        filt = Gtk.FileFilter(name="Fichiers audio")
        for ext in ["mp3", "wav", "m4a", "webm", "ogg", "flac", "aac", "wma"]:
            filt.add_pattern(f"*.{ext}")
        dialog.add_filter(filt)
        dialog.add_buttons("Annuler", Gtk.ResponseType.CANCEL, "Ouvrir", Gtk.ResponseType.ACCEPT)
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
            title="Modèles de transcription",
            default_width=580,
            default_height=620,
        )
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label="Modèles de transcription",
                                           css_classes=["title-4"]))
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()

        preset_group = Adw.PreferencesGroup(
            title="Mistral (API officielle)",
            description="Sélectionnables dans la liste déroulante « Modèle de transcription »")
        for mid, lbl in vtt.MISTRAL_TRANSCRIBE_MODELS:
            preset_group.add(Adw.ActionRow(title=lbl, subtitle=mid))
        page.add(preset_group)

        self._custom_group = Adw.PreferencesGroup(
            title="Mes modèles perso",
            description="Endpoints compatibles /v1/audio/transcriptions (Groq, OpenAI, etc.)")
        page.add(self._custom_group)
        self._custom_rows = []
        self._fill_custom_models_group()

        add_group = Adw.PreferencesGroup(title="Ajouter un modèle perso")
        self._cm_name = Adw.EntryRow(title="Nom (ex: Groq Whisper)")
        self._cm_model = Adw.EntryRow(title="Model ID (ex: whisper-large-v3)")
        self._cm_url = Adw.EntryRow(title="URL de base de l'API (ex: https://api.groq.com)")
        key_row = Adw.ActionRow(title="Clé API du fournisseur")
        self._cm_key = Gtk.PasswordEntry(valign=Gtk.Align.CENTER, width_chars=24)
        key_row.add_suffix(self._cm_key)
        key_row.set_activatable_widget(self._cm_key)
        for row in (self._cm_name, self._cm_model, self._cm_url, key_row):
            add_group.add(row)
        add_btn = Gtk.Button(label="Ajouter le modèle",
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
            title="Ajouter une playlist",
            default_width=620,
            default_height=560,
        )

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label="Playlist YouTube", css_classes=["title-4"]))
        toolbar.add_top_bar(header)

        # État de chargement
        loading_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                              halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                              vexpand=True)
        spinner = Adw.Spinner()
        spinner.set_size_request(48, 48)
        loading_box.append(spinner)
        loading_box.append(Gtk.Label(label="Chargement de la playlist...",
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
        select_all = Gtk.Button(label="Tout sélectionner")
        select_all.connect("clicked", lambda *_: list_box.select_all())
        select_none = Gtk.Button(label="Tout désélectionner")
        select_none.connect("clicked", lambda *_: list_box.unselect_all())
        add_btn = Gtk.Button(label="Ajouter", css_classes=["suggested-action"])
        add_btn.connect("clicked", lambda *_: self._add_playlist_selection(
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
        status_label = Gtk.Label(
            label=STATUS_LABELS.get(item.status, item.status),
            halign=Gtk.Align.START,
            css_classes=["status-pill", f"pill-{item.status}"],
        )
        status_row.append(status_label)
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
        item.status_label = status_label
        item.progress_bar = progress_bar

        # Menu par item (⋮) : réessayer / retirer
        menu_btn = Gtk.MenuButton(
            icon_name="view-more-symbolic",
            valign=Gtk.Align.CENTER,
            tooltip_text="Actions",
        )
        menu = Gio.Menu()
        actions = Gio.SimpleActionGroup()
        if item.status in (STATUS_ERROR, STATUS_CANCELLED):
            menu.append("Réessayer", "item.retry")
            act = Gio.SimpleAction(name="retry")
            act.connect("activate", lambda *_a, it=item: self._retry_queue_item(it))
            actions.add_action(act)
        if item.status != STATUS_RUNNING:
            menu.append("Retirer de la file", "item.remove")
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
                GLib.idle_add(self.update_status, "File terminée", "green")
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
            item.status_label.set_label(STATUS_LABELS.get(item.status, item.status))
            for cls in PILL_CLASSES:
                item.status_label.remove_css_class(cls)
            item.status_label.add_css_class(f"pill-{item.status}")
        if item.progress_bar:
            item.progress_bar.set_visible(item.status == STATUS_RUNNING)

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

    def on_search_changed(self, entry):
        self._filter_transcripts(entry.get_text())

    def _filter_transcripts(self, query: str):
        """Filtre la liste par nom ou par contenu."""
        for row in list(self.transcripts_list):
            self.transcripts_list.remove(row)

        summary_index = self._summaries_index()
        if not query:
            transcripts = [dict(t, has_summary=t["name"] in summary_index)
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
                        transcripts.append(match)
                except Exception:
                    continue

        if not transcripts:
            if not self.all_transcripts:
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

        # Pastille « Résumé IA » quand un résumé persisté existe pour ce fichier
        title_row = Gtk.Box(spacing=8, halign=Gtk.Align.START)
        title_row.append(name_label)
        if t.get("has_summary"):
            title_row.append(Gtk.Label(
                label="Résumé IA",
                halign=Gtk.Align.START,
                css_classes=["status-pill", "pill-summary"],
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
            tooltip_text="Actions",
        )
        menu = Gio.Menu()
        menu.append("Aperçu dans l'app", "tr.preview")
        menu.append("Résumé IA", "tr.summary")
        menu.append("Ouvrir (externe)", "tr.open")
        menu.append("Exporter en SRT", "tr.srt")
        menu.append("Exporter en VTT", "tr.vtt")
        menu.append("Supprimer", "tr.delete")
        menu_btn.set_menu_model(menu)

        actions = Gio.SimpleActionGroup()
        act = Gio.SimpleAction(name="preview")
        act.connect("activate", lambda *_a, tr=t: self.on_preview_transcript(tr))
        actions.add_action(act)
        act = Gio.SimpleAction(name="summary")
        act.connect("activate", lambda *_a, tr=t: self.on_summarize_clicked(tr))
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
                              tooltip_text="Copier le Markdown brut")
        copy_btn.connect("clicked", lambda *_: self._copy_to_clipboard(md_text))
        header.pack_end(copy_btn)
        summary_btn = Gtk.Button(label="Résumé IA", css_classes=["suggested-action"])
        summary_btn.connect("clicked",
                            lambda *_: self.on_summarize_clicked(transcript))
        header.pack_end(summary_btn)
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
            toc.append(Gtk.Label(label="Chapitres", halign=Gtk.Align.START,
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

    def _build_md_view(self, md_text: str) -> Gtk.Widget:
        """Rendu simple du Markdown en widgets GTK (titres, listes, code...)."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      halign=Gtk.Align.FILL)

        def add_markup(markup, css_classes=None):
            label = Gtk.Label(halign=Gtk.Align.START, wrap=True, xalign=0,
                              selectable=True, use_markup=True)
            label.set_markup(markup)
            if css_classes:
                label.set_css_classes(css_classes)
            box.append(label)

        def add_text(text, css_classes=None):
            label = Gtk.Label(halign=Gtk.Align.START, wrap=True, xalign=0,
                              selectable=True)
            label.set_label(text)
            if css_classes:
                label.set_css_classes(css_classes)
            box.append(label)

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
                           ["md-heading", "title-4"])
            elif stripped.startswith("## "):
                add_markup(self._md_inline(stripped[3:]),
                           ["md-heading", "title-3"])
            elif stripped.startswith("# "):
                add_markup(self._md_inline(stripped[2:]),
                           ["md-heading", "title-2"])
            elif stripped.startswith("> "):
                add_markup(f"<i>{self._md_inline(stripped[2:])}</i>",
                           ["dim-label"])
            elif re.match(r"^[-*+]\s+", stripped):
                add_markup("•  " + self._md_inline(
                    re.sub(r"^[-*+]\s+", "", stripped)))
            else:
                add_markup(self._md_inline(stripped))

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
        header.set_title_widget(Gtk.Label(label="Résumé IA", css_classes=["title-4"]))
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
        """Fenêtre affichant un résumé IA (généré ou chargé depuis le cache)."""
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
        header.set_title_widget(Gtk.Label(label="Résumé IA", css_classes=["title-4"]))
        copy_btn = Gtk.Button(icon_name="edit-copy-symbolic",
                              tooltip_text="Copier le résumé (Markdown)")
        copy_btn.connect("clicked", lambda *_: self._copy_to_clipboard(md))
        header.pack_end(copy_btn)
        toolbar.add_top_bar(header)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        inner.set_margin_top(18)
        inner.set_margin_bottom(24)
        inner.set_margin_start(24)
        inner.set_margin_end(24)
        inner.append(self._build_md_view(md))

        scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            child=Adw.Clamp(maximum_size=720, child=inner),
        )
        scrolled.set_vexpand(True)
        toolbar.set_content(scrolled)
        dialog.set_content(toolbar)
        dialog.present()

    def _copy_to_clipboard(self, text: str):
        try:
            Gdk.Display.get_default().get_clipboard().set(text)
            self.toast("Copié dans le presse-papiers", "success")
        except Exception as e:
            self.toast(f"Copie impossible : {e}", "error")

    # ========================================================================
    # EXPORT SRT/VTT DEPUIS UNE TRANSCRIPTION EXISTANTE
    # ========================================================================

    def on_export_srt(self, transcript: dict):
        if not transcript:
            return
        self._export_subtitle(transcript, "srt")

    def on_export_vtt(self, transcript: dict):
        if not transcript:
            return
        self._export_subtitle(transcript, "vtt")

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
            title="Supprimer la transcription",
            body=f"Supprimer : {name} ?",
            modal=True,
        )
        dialog.add_response("cancel", "Annuler")
        dialog.add_response("delete", "Supprimer")
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
        dialog.set_title("Paramètres")

        page = Adw.PreferencesPage()
        page.set_title("Général")

        # Clé API
        api_group = Adw.PreferencesGroup(title="API Mistral")
        api_row = Adw.EntryRow(title="Clé API")
        api_row.set_text(self.api_key)
        api_row.connect("changed", self.on_api_key_changed)
        api_group.add(api_row)
        page.add(api_group)

        # Dossier de sortie
        output_group = Adw.PreferencesGroup(title="Dossier des transcriptions")
        output_row = Adw.ActionRow(title="Dossier", subtitle=str(self.transcripts_dir))
        browse_btn = Gtk.Button(label="Parcourir...", halign=Gtk.Align.END)
        browse_btn.connect("clicked", self.on_browse_output, output_row)
        output_row.add_suffix(browse_btn)
        output_group.add(output_row)
        page.add(output_group)

        # Apparence : thème clair / sombre
        theme_group = Adw.PreferencesGroup(title="Apparence")
        theme_row = Adw.ActionRow(
            title="Thème",
            subtitle="Clair, sombre ou celui du système",
        )
        theme_model = Gtk.StringList()
        for name in ["Système", "Clair", "Sombre"]:
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
        proc_group = Adw.PreferencesGroup(title="Traitement")
        workers_row = Adw.ActionRow(
            title="Transcriptions en parallèle",
            subtitle="Nombre d'éléments traités simultanément",
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

        dialog.add(page)
        dialog.present()

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
            title="Dossier des transcriptions",
            transient_for=self.window,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            modal=True,
        )
        dialog.add_buttons("Annuler", Gtk.ResponseType.CANCEL, "Valider", Gtk.ResponseType.ACCEPT)
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
        """Gère le changement de source : lance l'estimation durée/coût."""
        url = self.url_entry.get_text().strip()
        self._check_source_type()

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
            title="Clé API Mistral requise",
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

        dialog.add_response("cancel", "Annuler")
        dialog.add_response("save", "Enregistrer")
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
