#!/usr/bin/env python3
"""
VoxCast - Fenêtre principale GTK4
Interface moderne avec GTK4 + Libadwaita pour une intégration native sous GNOME.
"""

import json
import os
import subprocess
import sys
import threading
import io
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
VERSION = "1.0.0"

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


# ============================================================================
# CAPTURE DE STDOUT POUR LE LOG
# ============================================================================

class GlibLogStream(io.TextIOBase):
    """Redirige les prints vers le log panel de l'UI (thread-safe)."""
    def __init__(self, callback):
        self.callback = callback
        self.buffer = ""

    def write(self, text):
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if line.strip():
                GLib.idle_add(self.callback, line)
        return len(text)

    def flush(self):
        if self.buffer.strip():
            GLib.idle_add(self.callback, self.buffer)
            self.buffer = ""


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
        self.thread: Optional[threading.Thread] = None
        self.selected_transcript: Optional[dict] = None

        self.load_settings()

        # Pour le cancel
        self.cancelled = False

        self.window = None

    def on_activate(self, app):
        self.window = Gtk.ApplicationWindow(
            application=app,
            title=f"VoxCast v{VERSION}",
            default_width=950,
            default_height=750,
        )

        # ===== HEADER BAR =====
        header = Adw.HeaderBar()

        settings_btn = Gtk.Button(icon_name="emblem-system-symbolic")
        settings_btn.set_tooltip_text("Paramètres")
        settings_btn.connect("clicked", self.on_settings_clicked)
        header.pack_end(settings_btn)

        self.window.set_titlebar(header)

        # ===== CONTENU =====
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        main_box.set_margin_top(20)
        main_box.set_margin_bottom(20)
        main_box.set_margin_start(20)
        main_box.set_margin_end(20)

        main_box.append(self._build_transcription_section())
        main_box.append(self._build_transcripts_section())
        main_box.append(self._build_status_section())

        self.window.set_child(main_box)

        self.setup_accelerators()
        self.refresh_transcripts_list()

        self.window.present()

    # ========================================================================
    # CONSTRUCTION DES SECTIONS
    # ========================================================================

    def _build_transcription_section(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="Transcription")

        # URL
        url_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        url_label = Gtk.Label(label="URL YouTube", halign=Gtk.Align.START)
        url_label.add_css_class("caption")
        self.url_entry = Gtk.Entry(
            placeholder_text="https://www.youtube.com/watch?v=...",
            hexpand=True,
        )
        self.url_entry.connect("activate", lambda *_: self.on_transcribe_clicked())
        self.url_entry.connect("changed", self.on_url_changed)
        url_box.append(url_label)
        url_box.append(self.url_entry)
        group.add(url_box)

        # Nom personnalisé
        name_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        name_label = Gtk.Label(label="Nom de sortie (optionnel)", halign=Gtk.Align.START)
        name_label.add_css_class("caption")
        self.name_entry = Gtk.Entry(
            placeholder_text="Mon titre personnalisé",
            hexpand=True,
        )
        name_box.append(name_label)
        name_box.append(self.name_entry)
        group.add(name_box)

        # Ligne : langue + timestamps
        options_row = Gtk.Box(spacing=15)

        # Langue
        lang_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        lang_label = Gtk.Label(label="Langue", halign=Gtk.Align.START)
        lang_label.add_css_class("caption")
        lang_model = Gtk.StringList()
        for code, name in LANGUAGES:
            lang_model.append(name)
        self.lang_dropdown = Gtk.DropDown(model=lang_model)
        self.lang_dropdown.set_tooltip_text("Forcer la langue améliore la précision")
        lang_box.append(lang_label)
        lang_box.append(self.lang_dropdown)
        options_row.append(lang_box)

        # Timestamps
        ts_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        ts_label = Gtk.Label(label="Timestamps", halign=Gtk.Align.START)
        ts_label.add_css_class("caption")
        ts_model = Gtk.StringList()
        for _, name in TIMESTAMP_OPTIONS:
            ts_model.append(name)
        self.ts_dropdown = Gtk.DropDown(model=ts_model)
        ts_box.append(ts_label)
        ts_box.append(self.ts_dropdown)
        options_row.append(ts_box)

        group.add(options_row)

        # Toggles : diarize + keep audio
        toggles_row = Gtk.Box(spacing=15)

        # Diarize
        diarize_row = Adw.ActionRow(title="Diarisation", subtitle="Identifier les speakers")
        self.diarize_switch = Gtk.Switch(halign=Gtk.Align.END, valign=Gtk.Align.CENTER)
        diarize_row.add_suffix(self.diarize_switch)
        diarize_row.set_activatable_widget(self.diarize_switch)
        toggles_row.append(diarize_row)

        # Keep audio
        keep_row = Adw.ActionRow(title="Garder l'audio", subtitle="Conserver le MP3 après transcription")
        self.keep_switch = Gtk.Switch(halign=Gtk.Align.END, valign=Gtk.Align.CENTER)
        keep_row.add_suffix(self.keep_switch)
        keep_row.set_activatable_widget(self.keep_switch)
        toggles_row.append(keep_row)

        group.add(toggles_row)

        # Context bias
        bias_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        bias_label = Gtk.Label(label="Context bias (optionnel)", halign=Gtk.Align.START)
        bias_label.add_css_class("caption")
        self.bias_entry = Gtk.Entry(
            placeholder_text="Termes séparés par espaces (max 100)",
            hexpand=True,
        )
        bias_box.append(bias_label)
        bias_box.append(self.bias_entry)
        group.add(bias_box)

        # Boutons
        buttons_box = Gtk.Box(spacing=10, halign=Gtk.Align.END, margin_top=10)

        self.cancel_btn = Gtk.Button(
            label="Annuler",
            icon_name="process-stop-symbolic",
            sensitive=False,
        )
        self.cancel_btn.connect("clicked", self.on_cancel_clicked)

        self.transcribe_btn = Gtk.Button(
            label="Transcrire",
            icon_name="media-playback-start-symbolic",
            halign=Gtk.Align.END,
        )
        self.transcribe_btn.add_css_class("suggested-action")
        self.transcribe_btn.connect("clicked", self.on_transcribe_clicked)
        self.transcribe_btn.set_sensitive(False)

        buttons_box.append(self.cancel_btn)
        buttons_box.append(self.transcribe_btn)
        group.add(buttons_box)

        return group

    def _build_transcripts_section(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="Transcriptions")

        self.transcripts_list = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.SINGLE,
            show_separators=True,
            css_classes=["navigation-sidebar"],
        )
        self.transcripts_list.connect("row-selected", self.on_transcript_selected)

        scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            child=self.transcripts_list,
        )
        scrolled.set_size_request(-1, 180)

        list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        list_box.append(scrolled)

        # Boutons
        list_buttons = Gtk.Box(spacing=10, halign=Gtk.Align.END, margin_top=10)

        self.open_btn = Gtk.Button(
            label="Ouvrir",
            icon_name="document-open-symbolic",
            sensitive=False,
        )
        self.open_btn.connect("clicked", self.on_open_transcript_clicked)

        self.open_folder_btn = Gtk.Button(
            label="Dossier",
            icon_name="folder-open-symbolic",
        )
        self.open_folder_btn.connect("clicked", self.on_open_folder_clicked)

        self.delete_btn = Gtk.Button(
            label="Supprimer",
            icon_name="user-trash-symbolic",
            sensitive=False,
        )
        self.delete_btn.add_css_class("destructive-action")
        self.delete_btn.connect("clicked", self.on_delete_transcript_clicked)

        list_buttons.append(self.open_btn)
        list_buttons.append(self.open_folder_btn)
        list_buttons.append(self.delete_btn)
        list_box.append(list_buttons)

        group.add(list_box)
        return group

    def _build_status_section(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()

        # Barre de progression
        self.progress_bar = Gtk.ProgressBar(
            halign=Gtk.Align.FILL,
            visible=False,
            margin_bottom=10,
        )
        group.add(self.progress_bar)

        # Label de statut
        self.status_label = Gtk.Label(
            label="Prêt",
            halign=Gtk.Align.START,
            margin_bottom=5,
        )
        self.status_label.add_css_class("title-4")
        group.add(self.status_label)

        # Log
        self.log_buffer = Gtk.TextBuffer()
        log_view = Gtk.TextView(
            buffer=self.log_buffer,
            editable=False,
            cursor_visible=False,
            wrap_mode=Gtk.WrapMode.WORD,
            monospace=True,
            margin_top=10,
            margin_bottom=10,
            margin_start=10,
            margin_end=10,
        )
        log_view.add_css_class("monospace")
        log_view.set_size_request(-1, 120)

        log_scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            child=log_view,
        )
        group.add(log_scrolled)

        return group

    # ========================================================================
    # RACCOURCIS CLAVIER
    # ========================================================================

    def setup_accelerators(self):
        accel_group = Gio.SimpleActionGroup()
        self.window.insert_action_group("win", accel_group)

        shortcuts = [
            ("transcribe", ["<Control>Return"], self.on_transcribe_clicked),
            ("cancel", ["<Control>q"], self.on_cancel_clicked),
            ("refresh", ["<Control>r"], self.refresh_transcripts_list),
            ("open", ["<Control>o"], self.on_open_transcript_clicked),
            ("delete", ["<Control>d"], self.on_delete_transcript_clicked),
        ]

        for name, accels, callback in shortcuts:
            action = Gio.SimpleAction(name=name)
            action.connect("activate", lambda _a, _p, cb=cb: cb())
            accel_group.add_action(action)
            self.app.set_accels_for_action(f"win.{name}", accels)

    # ========================================================================
    # PIPELINE DE TRANSCRIPTION
    # ========================================================================

    def on_transcribe_clicked(self, button=None):
        url = self.url_entry.get_text().strip()
        if not url:
            self.log_message("Veuillez entrer une URL YouTube.")
            return
        if self.running:
            self.log_message("Une transcription est déjà en cours.")
            return
        if not self.api_key:
            self.log_message("Aucune clé API Mistral configurée. Ouvrez les paramètres.", "red")
            return
        if not vtt.check_ffmpeg():
            self.log_message("ffmpeg requis. Installez-le (apt install ffmpeg).", "red")
            return

        self.running = True
        self.cancelled = False
        self.transcribe_btn.set_sensitive(False)
        self.cancel_btn.set_sensitive(True)
        self.progress_bar.set_visible(True)
        self.progress_bar.set_fraction(0.0)
        self.update_status("Démarrage...", "yellow")

        self.thread = threading.Thread(
            target=self._run_pipeline,
            args=(url,),
            daemon=True,
        )
        self.thread.start()

    def _run_pipeline(self, url: str):
        # Capturer stdout vers le log
        old_stdout = sys.stdout
        log_stream = GlibLogStream(self.log_message)
        sys.stdout = log_stream

        try:
            # --- ÉTAPE 1 : Téléchargement ---
            GLib.idle_add(self.update_status, "Téléchargement audio...", "yellow")
            GLib.idle_add(self.progress_bar.set_fraction, 0.05)

            mp3_path, video_info = vtt.download_audio(url, vtt.TEMP_DIR)

            if self.cancelled:
                self._cleanup(mp3_path, [], False)
                return

            GLib.idle_add(self.progress_bar.set_fraction, 0.20)

            # --- ÉTAPE 2a : Découpage si nécessaire ---
            audio_duration = vtt.get_audio_duration_seconds(mp3_path)
            needs_split = audio_duration > vtt.MAX_AUDIO_SECONDS

            if needs_split:
                GLib.idle_add(self.update_status, "Découpage audio...", "yellow")
                chunks = vtt.split_audio(mp3_path, vtt.TEMP_DIR / "chunks")
                if self.cancelled:
                    self._cleanup(mp3_path, [c for c, _ in chunks], needs_split)
                    return
                audio_files = chunks
            else:
                audio_files = [(mp3_path, 0.0)]

            GLib.idle_add(self.progress_bar.set_fraction, 0.30)

            # --- ÉTAPE 2b : Transcription ---
            lang_idx = self.lang_dropdown.get_selected()
            language = LANGUAGES[lang_idx][0]
            if language == "auto":
                language = None

            ts_idx = self.ts_dropdown.get_selected()
            ts_value = TIMESTAMP_OPTIONS[ts_idx][0]
            timestamp_granularities = [ts_value] if ts_value != "none" else None

            diarize = self.diarize_switch.get_active()
            keep_audio = self.keep_switch.get_active()
            bias_text = self.bias_entry.get_text().strip()
            context_bias = bias_text.split() if bias_text else None

            results = []
            total = len(audio_files)

            for i, (chunk_path, offset) in enumerate(audio_files, 1):
                if self.cancelled:
                    self._cleanup(mp3_path, [c for c, _ in audio_files], needs_split)
                    return

                if total > 1:
                    GLib.idle_add(self.update_status, f"Transcription chunk {i}/{total}...", "yellow")

                result = vtt.transcribe_audio(
                    mp3_path=chunk_path,
                    api_key=self.api_key,
                    language=language,
                    diarize=diarize,
                    timestamp_granularities=timestamp_granularities,
                    context_bias=context_bias,
                )
                results.append((result, offset))

                text = result.get("text", "")
                GLib.idle_add(self.log_message, f"Chunk {i}/{total} transcrit: {len(text)} caractères")

                # Progression : 30% -> 90% répartis sur les chunks
                progress = 0.30 + 0.60 * (i / total)
                GLib.idle_add(self.progress_bar.set_fraction, progress)

            # --- ÉTAPE 3 : Sauvegarde ---
            GLib.idle_add(self.update_status, "Sauvegarde...", "yellow")
            GLib.idle_add(self.progress_bar.set_fraction, 0.95)

            custom_name = self.name_entry.get_text().strip()
            output_name = vtt.sanitize_filename(custom_name) if custom_name else vtt.sanitize_filename(video_info.get("title", "transcription"))
            output_path = self.transcripts_dir / f"{output_name}.md"

            vtt.save_transcription(
                results=results,
                video_info=video_info,
                output_path=output_path,
                audio_name=mp3_path.name,
                diarize=diarize,
                timestamp_granularities=timestamp_granularities,
            )

            GLib.idle_add(self.progress_bar.set_fraction, 1.0)
            GLib.idle_add(self.update_status, "Transcription terminée", "green")
            GLib.idle_add(self.log_message, f"Fichier: {output_path}", "green")

            # Aperçu
            full_text = " ".join(r.get("text", "") for r, _ in results)
            preview = full_text[:200] + ("..." if len(full_text) > 200 else "")
            GLib.idle_add(self.log_message, f"Aperçu: {preview}")

            # Nettoyage
            self._cleanup(mp3_path, [c for c, _ in audio_files], needs_split, keep_audio)

        except Exception as e:
            import traceback
            GLib.idle_add(self.log_message, f"Erreur: {e}", "red")
            GLib.idle_add(self.log_message, traceback.format_exc(), "red")
            GLib.idle_add(self.update_status, "Erreur", "red")
        finally:
            sys.stdout = old_stdout
            log_stream.flush()
            GLib.idle_add(self._on_pipeline_done)

    def _cleanup(self, mp3_path, chunk_paths, needs_split, keep_audio=False):
        if not keep_audio:
            mp3_path.unlink(missing_ok=True)
            if needs_split:
                for c in chunk_paths:
                    c.unlink(missing_ok=True)
                try:
                    (vtt.TEMP_DIR / "chunks").rmdir()
                except OSError:
                    pass
                try:
                    vtt.TEMP_DIR.rmdir()
                except OSError:
                    pass

    def _on_pipeline_done(self):
        self.running = False
        self.cancelled = False
        self.transcribe_btn.set_sensitive(True)
        self.cancel_btn.set_sensitive(False)
        self.progress_bar.set_visible(False)
        self.refresh_transcripts_list()

    def on_cancel_clicked(self, button=None):
        if self.running:
            self.cancelled = True
            self.log_message("Annulation demandée...", "yellow")
            self.update_status("Annulation en cours...", "yellow")

    # ========================================================================
    # LISTE DES TRANSCRIPTIONS
    # ========================================================================

    def refresh_transcripts_list(self, button=None):
        transcripts = self._get_transcripts()

        for row in list(self.transcripts_list):
            self.transcripts_list.remove(row)

        if not transcripts:
            empty = Gtk.Label(
                label="Aucune transcription",
                halign=Gtk.Align.CENTER,
                margin_top=15,
                margin_bottom=15,
            )
            empty.add_css_class("dim-label")
            self.transcripts_list.append(empty)
            self.update_status("Prêt", None)
            return

        for t in transcripts:
            row = self._create_transcript_row(t)
            self.transcripts_list.append(row)

        self.update_status(f"{len(transcripts)} transcription(s)", None)

    def _get_transcripts(self) -> list[dict]:
        transcripts = []
        if not self.transcripts_dir.exists():
            return transcripts
        for f in sorted(self.transcripts_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
            stat = f.stat()
            transcripts.append({
                "name": f.stem,
                "path": f,
                "size": stat.st_size,
                "modified": stat.st_mtime,
            })
        return transcripts

    def _create_transcript_row(self, t: dict) -> Gtk.Widget:
        box = Gtk.Box(spacing=10, halign=Gtk.Align.FILL)
        icon = Gtk.Image(icon_name="text-x-generic-symbolic", pixel_size=24)

        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        name_label = Gtk.Label(
            label=t["name"],
            halign=Gtk.Align.START,
            ellipsize=Pango.EllipsizeMode.END,
            max_width_chars=50,
        )
        name_label.add_css_class("title-4")

        date = datetime.fromtimestamp(t["modified"]).strftime("%Y-%m-%d %H:%M")
        size = self._format_size(t["size"])
        details = Gtk.Label(
            label=f"{size} • {date}",
            halign=Gtk.Align.START,
            ellipsize=Pango.EllipsizeMode.END,
        )
        details.add_css_class("dim-label")

        info_box.append(name_label)
        info_box.append(details)
        box.append(icon)
        box.append(info_box)
        box.append(Gtk.Box())

        return Gtk.ListBoxRow(child=box, activatable=False)

    def on_transcript_selected(self, list_box, row):
        if row is None:
            self.open_btn.set_sensitive(False)
            self.delete_btn.set_sensitive(False)
            self.selected_transcript = None
            return
        idx = row.get_index()
        transcripts = self._get_transcripts()
        if 0 <= idx < len(transcripts):
            self.selected_transcript = transcripts[idx]
            self.open_btn.set_sensitive(True)
            self.delete_btn.set_sensitive(True)

    def on_open_transcript_clicked(self, button=None):
        if not self.selected_transcript:
            return
        try:
            subprocess.run(["xdg-open", str(self.selected_transcript["path"])], check=True)
        except Exception as e:
            self.log_message(f"Impossible d'ouvrir: {e}", "red")

    def on_open_folder_clicked(self, button=None):
        try:
            subprocess.run(["xdg-open", str(self.transcripts_dir)], check=True)
        except Exception as e:
            self.log_message(f"Impossible d'ouvrir: {e}", "red")

    def on_delete_transcript_clicked(self, button=None):
        if not self.selected_transcript:
            return

        name = self.selected_transcript["name"]
        path = self.selected_transcript["path"]

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
                self.log_message(f"Supprimé: {path.stem}")
                self.refresh_transcripts_list()
            except Exception as e:
                self.log_message(f"Erreur: {e}", "red")
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

        dialog.add(page)
        dialog.present()

    def on_api_key_changed(self, row):
        self.api_key = row.get_text().strip()
        self.save_settings()

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
    # URL
    # ========================================================================

    def on_url_changed(self, entry):
        url = entry.get_text().strip()
        self.transcribe_btn.set_sensitive(bool(url) and not self.running)

    # ========================================================================
    # UTILITAIRES
    # ========================================================================

    def log_message(self, message: str, color: str = None):
        timestamp = datetime.now().strftime("%H:%M:%S")
        full = f"[{timestamp}] {message}\n"

        end_iter = self.log_buffer.get_end_iter()
        self.log_buffer.insert(end_iter, full)

        if color:
            color_map = {"red": "red", "green": "green", "yellow": "orange"}
            tag = self.log_buffer.create_tag(None, foreground=color_map.get(color, color))
            start = self.log_buffer.get_iter_at_offset(
                self.log_buffer.get_char_count() - len(full)
            )
            self.log_buffer.apply_tag(tag, start, end_iter)

        self.log_buffer.place_cursor(end_iter)

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
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def on_shutdown(self, app):
        self.cancelled = True
        if self.thread:
            self.thread.join(timeout=3)

    def run(self):
        return self.app.run()


# ============================================================================
# POINT D'ENTRÉE
# ============================================================================

if __name__ == "__main__":
    app = VoxCastWindow()
    sys.exit(app.run())
