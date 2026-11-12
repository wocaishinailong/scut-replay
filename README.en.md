# SCUT Replay Subtitle Exporter

[中文 README](README.md)

Export subtitles from South China University of Technology course replays by course name and date. The tool uses platform subtitles when available and can transcribe replay audio locally on Apple Silicon Macs. It produces timestamped TXT, SRT, and JSON files.

## Installation

- **Full workflow:** Apple Silicon Mac (M-series), Google Chrome, Python 3 with pip, and a terminal. The setup script creates an isolated Python 3.11 environment.
- **Platform subtitles only:** On macOS or Linux systems that can run the dependencies and Chrome, try `--site-only`. The full workflow was verified on Apple Silicon macOS. Linux and Windows have not been verified.
- You need access to the school replay site and a Python package index. The initial model download also requires access to Hugging Face. Model weights take about 1.6 GB. Local transcription also needs disk space for the replay and its audio track.

Get the source and run this from the project root:

```sh
./setup.sh
```

The script installs the versions pinned in `uv.lock`. If you already have Python 3.11, you can instead use [requirements.txt](requirements.txt), which is generated from the same lockfile:

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.txt
```

## Quick start

```sh
./scut sync --course '操作系统' --since 2026-09-01 --through 2026-11-12
```

Replace the name with the course title shown in your timetable and choose your dates. On first use, sign in on the school's page in the dedicated Chrome window. If that page offers QR sign-in, scan the code there. The command continues after sign-in and reuses the session while it is valid. Course discovery is scoped to your timetable.

Use `YYYY-MM-DD` dates. Both endpoints are inclusive. Add `--dry-run` to preview the selection. A rerun skips completed lessons that pass the file checks. Press Ctrl+C to stop, then repeat the same command to reuse completed download and transcription chunks.

## CLI reference

| Command | Purpose |
| --- | --- |
| `./scut login` | Open the school sign-in page and save the local session |
| `./scut courses` | List course names and IDs from your timetable |
| `./scut sync --course '操作系统' --on 2026-11-11` | Process one date |
| `./scut sync --course '操作系统' --through 2026-11-12` | Process lessons in the catalogue through the given date |
| `./scut sync --course '操作系统' --through 2026-11-12 --site-only` | Export platform subtitles and leave missing ones pending |
| `./scut status` | Inspect local progress offline |
| `./scut prepare-model` | Download the speech recognition model in advance |
| `./scut sync --help` | Show all options |

If multiple courses share a name, replace `--course` with `--course-id` using the ID from `./scut courses`. With a valid session, place `--headless` before the subcommand to run without showing Chrome. If the timetable request cannot be detected, retry with Chrome visible and check that the replay homepage has loaded your timetable.

## Local speech recognition

The model is downloaded when a replay first needs local transcription. To download it in advance, run:

```sh
./scut prepare-model
```

The tool uses [mlx-community/whisper-large-v3-turbo](https://huggingface.co/mlx-community/whisper-large-v3-turbo/tree/a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb), pinned to revision `a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb` and cached in `.data/models/`. FFmpeg from the Python dependency extracts 16 kHz mono audio. Silero VAD identifies speech, and MLX Whisper transcribes it on the Mac in the detected language. Only model weights are downloaded, and lecture audio stays on your machine. No API key is required. Automatic transcription can make mistakes. The `[待核对]` marker identifies some segments for review.

## Output and privacy

Per-lesson output goes to `.data/courses/<course-id>/<date>_<lesson-id>/`:

- `transcript.txt`: readable, timestamped text.
- `transcript.srt`: standard subtitles for video players.
- `transcript.json`: structured cues, source, and transcription metadata.
- `slides.json`: the platform's screenshot timeline. The indices have not been matched to the original slide deck.

The CLI exports each lesson separately. Lessons without a replay stay pending and can be picked up by rerunning the same date range. The tool handles subtitle collection and speech transcription.

`.data/` contains the local browser session, processing records, models, media, and subtitles. `exports/` may contain assembled subtitles. Both are private local directories covered by Git ignore rules. Keep any custom `--data-dir` outside the repository. Use only replays you have access to and obtain permission before sharing course content.

## Tests and license

```sh
.venv/bin/python -m unittest discover -s tests -v
```

The source code is released under the [MIT License](LICENSE). Models and dependencies retain their own licenses.
