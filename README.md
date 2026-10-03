# VoxCast

Télécharge l'audio d'une vidéo YouTube et le transcrit en texte via l'API Mistral Voxtral (Voxtral Mini Transcribe 2).

Le pipeline est simple :

```
URL YouTube → yt-dlp (audio MP3 128kbps) → Mistral Voxtral API → fichier Markdown
Fichier local → ffmpeg (audio MP3) → Mistral Voxtral API → fichier Markdown
```

Le fichier de sortie est structuré pour être directement exploitable par un outil d'analyse comme [Vibe](https://mistral.ai/products/vibe) : recherche, synthèse, résumé, extraction d'informations.

## Fonctionnalités

- Téléchargement audio via `yt-dlp` (MP3 128kbps, optimisé pour la parole)
- **Support des fichiers audio locaux** (MP3, WAV, M4A, OGG, FLAC...) en plus de YouTube
- Transcription via l'API Mistral Voxtral (`voxtral-mini-latest`)
- **Diarisation** optionnelle (identification des speakers)
- **Timestamps** optionnels (par segment ou par mot)
- **Context biasing** (jusqu'à 100 termes pour guider la transcription de noms propres)
- Détection automatique de la langue ou forçage manuel (13 langues)
- **Découpage automatique** des vidéos longues (>3h) en chunks, transcription séparée puis fusion en un seul fichier
- **Découpage parallèle** des chunks via ffmpeg multi-threaded (tous les coeurs CPU)
- **Reprise sur échec** : retry automatique des chunks échoués (3 tentatives)
- **Cache des métadonnées** : évite de re-fetcher les infos YouTube
- **Transcription partielle** : extraire et transcrire un segment précis (ex: 10:00 → 30:00)
- **Export SRT / VTT** : génère des sous-titres depuis la transcription
- **Estimation du coût** : affiche la durée et le coût avant de lancer
- Sortie en Markdown structuré avec métadonnées
- Nettoyage automatique du fichier audio temporaire
- **Interface graphique GTK4 + Libadwaita** (style GNOME natif)
  - **File d'attente** : ajoutez plusieurs vidéos, traitez-les les unes après les autres
  - **Recherche** dans les transcriptions par nom ou par contenu
  - **Glisser-déposer** de fichiers audio directement dans la fenêtre
- **CLI** pour automatisation et scripts

## Prérequis

- Python 3.8+
- `ffmpeg` installé sur le système
- **GTK4 et Libadwaita** (pour l'interface graphique)
- Une clé API Mistral ([console.mistral.ai](https://console.mistral.ai))

## Installation

1. Installer les dépendances système pour GTK4 :

   - **Ubuntu/Debian** :
     ```bash
     sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-libadwaita-1.0 ffmpeg
     ```
   - **Fedora** :
     ```bash
     sudo dnf install python3-gobject gtk4 libadwaita ffmpeg
     ```
   - **Arch Linux** :
     ```bash
     sudo pacman -S python-gobject gtk4 libadwaita ffmpeg
     ```

2. Installer les dépendances Python :

```bash
cd VoxCast
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## Configuration

Définissez votre clé API Mistral (CLI) :

```bash
export MISTRAL_API_KEY="votre-cle"
```

Ou saisissez-la dans l'interface graphique via Paramètres > Clé API.

## Utilisation

### Interface graphique (recommandée)

```bash
./venv/bin/python3 main.py
```

L'interface permet de :
- Saisir une URL YouTube ou un chemin de fichier local (ou glisser-déposer un fichier)
- Voir l'estimation du coût et de la durée en temps réel
- Replier les options avancées (langue, timestamps, diarisation, context bias, segment, export SRT/VTT)
- Ajouter plusieurs sources à la **file d'attente** avec une barre de progression par élément
- Démarrer/annuler le traitement de la file
- Suivre la progression en temps réel (téléchargement, transcription, estimation du temps restant)
- Recevoir les retours (succès, erreurs, annulations) via des toasts GNOME
- **Rechercher** dans les transcriptions par nom ou par contenu
- Agir sur chaque transcription via son menu (⋮) : ouvrir (ou double-clic), exporter en SRT/VTT, supprimer
- Configurer la clé API et le dossier de sortie

Les messages techniques du pipeline (yt-dlp, ffmpeg, API Mistral) ne sont plus affichés dans l'interface : ils vont dans le shell d'où l'application est lancée et dans le fichier `voxccast.log`.

**Raccourcis clavier :**
- `Ctrl+Entrée` : Ajouter à la file
- `Ctrl+Espace` : Démarrer la file
- `Ctrl+Q` : Annuler
- `Ctrl+R` : Rafraîchir la liste
- `Ctrl+O` : Ouvrir la transcription sélectionnée
- `Ctrl+D` : Supprimer la transcription sélectionnée

### En ligne de commande (CLI)

```bash
# Basique
./venv/bin/python3 video_to_text.py "https://youtube.com/watch?v=..."

# Avec langue forcée (recommandé)
./venv/bin/python3 video_to_text.py "https://youtube.com/watch?v=..." --language fr

# Fichier audio local
./venv/bin/python3 video_to_text.py /path/to/audio.mp3 --language fr

# Transcription partielle (segment)
./venv/bin/python3 video_to_text.py "https://youtube.com/watch?v=..." --start 10:00 --end 30:00

# Avec diarisation + timestamps par segment
./venv/bin/python3 video_to_text.py "https://youtube.com/watch?v=..." -d -t segment

# Avec export SRT + VTT
./venv/bin/python3 video_to_text.py "https://youtube.com/watch?v=..." --srt --vtt

# Avec context bias (noms propres, termes techniques)
./venv/bin/python3 video_to_text.py "https://youtube.com/watch?v=..." -b "Mistral" "Voxtral" "ASR"

# Conserver le fichier MP3 après transcription
./venv/bin/python3 video_to_text.py "https://youtube.com/watch?v=..." --keep-audio

# Désactiver le cache des métadonnées
./venv/bin/python3 video_to_text.py "https://youtube.com/watch?v=..." --no-cache
```

Les transcriptions sont sauvegardées dans `transcripts/` au format Markdown.

## Options CLI

| Option | Description |
|---|---|
| `-l, --language` | Code langue (fr, en, es...). Accélère et améliore la précision. |
| `-d, --diarize` | Active la diarisation (identification des speakers). |
| `-t, --timestamps` | Granularité des timestamps : `segment` ou `word`. |
| `-b, --context-bias` | Termes pour guider la transcription (max 100). |
| `-o, --output` | Nom du fichier de sortie (sans extension). |
| `--keep-audio` | Conserve le fichier MP3 après transcription. |
| `--api-key` | Clé API Mistral (sinon lit `MISTRAL_API_KEY`). |
| `--start` | Début du segment (HH:MM:SS, MM:SS ou secondes). |
| `--end` | Fin du segment (HH:MM:SS, MM:SS ou secondes). |
| `--no-cache` | Désactive le cache des métadonnées YouTube. |
| `--srt` | Exporte aussi en SRT (sous-titres). |
| `--vtt` | Exporte aussi en VTT (sous-titres WebVTT). |
| `--file` | Force la source comme fichier local (auto-détecté sinon). |

Note : `--timestamps` et `--language` ne sont pas compatibles simultanément (limitation API Mistral).

## Structure du projet

```
VoxCast/
├── main.py              # Point d'entrée GUI (GTK4)
├── window.py            # Interface graphique (GTK4 + Libadwaita)
├── video_to_text.py     # Logique principale (CLI + pipeline)
├── requirements.txt     # Dépendances (yt-dlp, mistralai, PyGObject)
├── settings.json        # Paramètres utilisateur (généré)
├── .cache.json          # Cache des métadonnées YouTube (généré)
├── voxcast.log          # Journal de l'application (généré)
├── downloads/           # MP3 temporaires (auto-nettoyés)
└── transcripts/         # Transcriptions .md, .srt, .vtt de sortie
```

## Projets liés

VoxCast réutilise la logique de téléchargement audio de [DownloadYTMusic](https://github.com/fracorbas/DownloadYTMusic), un téléchargeur YouTube MP3 avec interface graphique GTK4. VoxCast adapte cette logique pour produire un MP3 128kbps optimisé pour la transcription vocale (au lieu de 320kbps pour la musique), puis enchaîne avec la transcription Mistral Voxtral.

## Avertissement

Respectez les droits d'auteur. Ce projet est fourni à des fins éducatives et personnelles. Assurez-vous d'avoir le droit de télécharger et de transcrire le contenu avant de l'utiliser.
