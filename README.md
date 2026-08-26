## Branches

This fork contains the following branches and their respective changes:

- **`main`** – Main development branch of this fork. It includes the changes from the branches listed below.
- **`rectangular-card-format`** – Adds support for rectangular RFID card formats, including configurable distortion and related card-rendering changes.
- **`pyinstaller`** – Adds and documents PyInstaller-based packaging of Funkuino Studio, including support for bundling a local `ffmpeg` executable.
- **`feat-hide-cards-agent-tabs`** – Adds command-line options to hide the Cards and Agent features/tabs when they are not needed.
- **`feat-show-tracks-in-album_folge`** – Adds the corresponding library display/sorting changes for tracks in album/episode views.
- **`feat-library-sort-mode`** – Adds an alternative alphabetical (German phonebook) library sorting mode.
- **`feat-m3u-webstream-support`** – Recognises `.m3u`/`.m3u8` files as their own card units (a dedicated `Webstreams_Playlisten` folder, plus loose playlists in `Lieder/` and series folders), fixes a Windows path-separator bug affecting nested unit ids, and makes the "now playing" indicator always visible with a combined container/sub-item title.
- **`fix-remote-root-paths`** – Fixes handling of remote paths when a non-default `ESPUINO_REMOTE_ROOT` is configured.
- **`fix-windows-directory-fsync`** – Adds Windows compatibility fixes for directory `fsync` and remote path separators.
- **`UTF-8-Fix`** – Contains UTF-8 related fixes.
- **`upstream/main`** – Reference branch for the original upstream repository.

The `main` branch of this fork intentionally contains changes that are not part of the original upstream repository. This fork is maintained independently and no further pull requests to the upstream repository are planned.

---


# Funkuino

**Manage an [ESPuino](https://espuino.de) kids' audio player from your
computer — library mirroring, printable RFID cards, and a web dashboard with
an embedded Claude agent.**

The [ESPuino](https://forum.espuino.de) is a wonderful DIY, RFID-controlled
audio player for children: put a card on the box, the story plays. What it
doesn't come with is comfortable tooling for the grown-up side of the workflow —
getting audio onto the device, keeping a library organised, and producing the
physical cards. Funkuino is that missing half: a Mac app you can double-click,
and the same tools as small, sharp command-line programs underneath.

> **This fork adds first-class native Windows support** — no WSL required.
> Everything below (Studio, sync, cards, the library conventions) works the
> same way on Windows as on macOS/Linux; see [Windows](#windows) under
> Install for setup, including a packaged, double-click `.exe` for people you
> don't want to hand a terminal to.

**[Download Funkuino for macOS](https://github.com/sadilek/Funkuino/releases/latest)**
— a signed, notarised app for Apple Silicon (macOS 14+). It brings its own
Python and ffmpeg, so there is nothing to install first.

![Funkuino Studio — library view](https://raw.githubusercontent.com/sadilek/Funkuino/main/docs/screenshots/bibliothek.png)

*Screenshots show a fictional demo library.*

## Funkuino Studio

Studio is a dashboard covering the entire pipeline per card — Download ·
Cover · Sync · Print · RFID — where the status boxes are also the actions:
click a unit's sync box to upload it, select covers for printing via a floating
print bar, assign RFID cards straight from the library view.

- **Live RFID assignment:** Studio keeps a passive websocket to the device and
  hears every card placement. Put an unknown card on the box, click the row it
  should belong to — assigned (with sensible play-mode defaults per content
  type).
- **Always-visible now playing:** a header chip shows what's playing
  regardless of search/filter state — container units (an album, a
  multi-story episode folder, a webstream playlist) combine the container's
  own title with the currently playing sub-item (e.g. "Anne Kaffeekanne –
  Trarira"); single-file units just show their own title.
- **Toggleable panes:** `--no-cards` hides the card-printing tab and column,
  `--no-agent` hides the agent tab — for setups that only use sync and the
  library view, both features (and their API endpoints) disappear entirely
  rather than sitting there unused.
- **Integrated card printing** with the same manifest and `--undo` semantics as
  the command line — every cover as a thumbnail (newest first, printed ones
  greyed out), hand-pick covers onto a sheet or one-click the oldest full page:

![Funkuino Studio — card printing](https://raw.githubusercontent.com/sadilek/Funkuino/main/docs/screenshots/karten.png)

- **Embedded Claude agent** (optional, [Claude Agent
  SDK](https://docs.anthropic.com/en/api/agent-sdk/overview)): paste a URL into
  the library tab and an agent session handles the intake — probe, classify
  (song/album/audiobook), download with the right flags, verify naming and
  covers, report back — asking *you* whenever a judgment call is needed
  (episode numbering, attribution, intro wording), via question cards in the
  browser. Permission model: read-only tools and the project's own commands are
  auto-allowed, risky ones always raise an approval card. Needs your own
  [Claude Code](https://claude.com/claude-code) installation and a subscription
  token — the Agent tab walks you through both.

## The tools underneath

Studio is a front end for these; each is also a command of its own, and the
app puts them on your PATH as `funkuino <command>`.

- **`funkuino sync` — rsync-style mirror to the device.** One-way mirror of a local
  `files/` folder to the ESPuino over its HTTP API. Fast recursive listing,
  change detection via a local manifest (the device can't report file sizes
  cheaply), crash-safe incremental state (an interrupted upload is never
  trusted), size-scaled timeouts for big audiobook files, `--delete` with
  guard rails, automatic NFC/NFD Unicode normalisation (macOS vs. device), and
  per-device manifests keyed by MAC address — mirror the same library to two
  ESPuinos without confusion.
- **`funkuino download` — audio intake in the library's conventions.** A yt-dlp
  wrapper that lands audio as your library expects it: songs, albums with
  ordered track numbers, or audiobooks (Hörspiele) — multi-part episodes merged
  into a single MP3 with a spoken title intro (macOS `say`), correct tags, and
  the full-resolution title image saved separately for card printing (covers
  are deliberately *not* embedded in the audio: the device has no display and a
  large leading ID3 tag makes playback start-stutter).
- **`funkuino prepare` / `funkuino covers`** — the audiobook merge as a standalone tool, and
  batch extraction of embedded cover art from an existing library.
- **`funkuino cards` — printable RFID card sheets.** Lays title images out as
  edge-to-edge 6×6 cm squares on A4 PDFs (12 per page) with cut tick marks in
  the margins. Print, stick an RFID tag on each card's back, laminate, cut. A
  print manifest makes a bare `funkuino cards` mean "everything new since last
  time", with stackable `--undo`. `--rectangular-cards` switches to a
  wallet/checkout-card-shaped format instead of the square default, for print
  services whose own stock size doesn't match a plain square — the source
  image is bleed-extended by mirroring/stretching its edges (rather than
  leaving a hard white border) so a slightly-off manual trim never exposes
  blank paper.

## Extensions — your own commands and your own agent

Some sources need handling the shipped `download` does not have, and everyone's
library has conventions of its own. Both are extendable without touching this
repository, because both live in *your* library folder:

- **`extensions/<name>.py` becomes `funkuino <name>`.** A script dropped there
  is run with the project's own modules importable, so it can reuse the
  download pipeline, the naming rules and the device client. The one that grew
  this mechanism reconstructs complete audiobook series from a YouTube Music
  "Topic" channel, where the shipped downloader only sees a jumble of chapter
  tracks. An optional `<name>.permissions.json` (`{"allow": [...], "ask": [...]}`)
  declares how the embedded agent may call it — an extension that declares
  nothing simply needs your approval every time.
- **`CLAUDE.md` in your library folder teaches the agent your conventions.**
  It is loaded into every agent session on top of the project's own
  instructions: how you want a particular series named, that a certain source
  needs cookies, which play mode a kind of content should get. This is how the
  intake flow learns your habits instead of you repeating them per download.

## Install

Download the DMG from
[Releases](https://github.com/sadilek/Funkuino/releases/latest), drag the app
to Applications, start it. On first launch it asks for two things: where your
library should live (a normal folder — put it on an external disk if you like)
and your ESPuino's name or IP. Nothing else to install: the app carries its own
Python runtime and a purpose-built ffmpeg.

![First launch](https://raw.githubusercontent.com/sadilek/Funkuino/main/docs/screenshots/einrichtung.png)

**Two limitations to know before you download:**

- **The interface is German only.** The tools, the code and this documentation
  are English, but every label and message a user sees is German. Translations
  would be welcome.
- **The app is Apple Silicon only** (macOS 14+). The bundled Python is
  single-architecture, so an Intel build has to be built on an Intel Mac —
  possible (`make release` in `mac/`), just not something I can produce here.
  On Linux, install it with pipx instead (below); there the same Studio runs in
  your browser.

### Linux

There is no app bundle outside macOS, but Studio and every command are plain
Python and run anywhere:

```bash
pipx install funkuino
sudo apt install ffmpeg        # or dnf/pacman/zypper — funkuino tells you which

funkuino studio                # dashboard at http://127.0.0.1:8800
funkuino sync --dry-run        # see what would be uploaded
funkuino cards                 # printable PDF of the new covers
```

The library then defaults to `~/Funkuino/` (change it with `funkuino --data-dir`
or a `data_dir` entry in `~/.config/funkuino/config.json`), and configuration
follows the platform: `$XDG_CONFIG_HOME/funkuino` on Linux,
`%APPDATA%\Funkuino` on Windows.

Two things differ from macOS: the spoken intro in front of merged audiobooks
uses macOS' `say` and is skipped elsewhere (the merge itself runs normally), and
a finished print sheet is not opened for you — it is written to
`print-sheets/`.

### Windows

Native Windows works well — no WSL needed. Two ways to run it, pick based on
who's using it:

- **You, or anyone who's fine with a terminal:** the `pipx` route below. One
  command to update (`git pull` if running from a checkout), full access to
  every flag and command.
- **Someone you're handing a finished setup to** (family, a co-parent, a
  friend's kid's ESPuino) **and don't want anywhere near a terminal or a
  Python install:** the packaged `.exe` further down. Self-contained, but a
  manual rebuild is needed whenever the code changes — there's no
  auto-update.

#### Quick start (pipx)

```powershell
# Python 3.10+ from python.org, "Add python.exe to PATH" checked during setup, then:
python -m pip install --user pipx
python -m pipx ensurepath
# close and reopen PowerShell so the updated PATH takes effect, then:
pipx install funkuino
winget install Gyan.FFmpeg     # or download a static build and put ffmpeg.exe on PATH

funkuino studio                # dashboard at http://127.0.0.1:8800
```

Configuration is environment variables, same names as everywhere else
(`ESPUINO_HOST`, `ESPUINO_LOCAL_DIR`, `ESPUINO_REMOTE_ROOT`,
`FUNKUINO_DATA_DIR`) — set with `$env:NAME = "value"` for the current
PowerShell window only, or `setx NAME "value"` to persist across window/reboot
(a new window is needed to see a `setx` change — it does not apply
retroactively to the one you ran it in).

Working from a git checkout instead of `pipx install` (to track a fork, or to
build from source) works the same as on Linux/macOS:

```powershell
git clone https://github.com/<you>/Funkuino.git
cd Funkuino
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python scripts\studio.py
```

`pipx install --editable <path-to-checkout>` also works, and keeps the
`funkuino` command in sync with the checkout without a separate install step
per change.

#### Packaged `.exe`, for people you don't want to hand a terminal to

A `FunkuinoStudio.spec` ([PyInstaller](https://pyinstaller.org/)) bundles
Studio, its Python dependencies and a local `ffmpeg.exe` into one folder — the
person you give it to double-clicks an icon, never sees Python or a terminal
prompt (well — one console window still opens alongside, showing Studio's own
startup line and log output; that's expected, not a bug). This builds from a
git checkout, in its own `.venv` (create one now if you haven't already):

```powershell
git clone https://github.com/<you>/Funkuino.git
cd <repo-root>
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pip install pyinstaller
# Download a static Windows ffmpeg build (e.g. from https://www.gyan.dev/ffmpeg/builds/)
# and place the extracted ffmpeg.exe at:  <repo-root>\ffmpeg\ffmpeg.exe

.venv\Scripts\python.exe -m PyInstaller --clean --onedir --name FunkuinoStudio `
    --collect-all aiohttp --paths scripts `
    --add-data "scripts\studio_web;studio_web" `
    --add-binary "ffmpeg\ffmpeg.exe;ffmpeg" `
    scripts\studio.py
```

That first run writes out `FunkuinoStudio.spec` with all of the above baked
in; every rebuild after that is just `.venv\Scripts\python.exe -m PyInstaller
FunkuinoStudio.spec` (or edit the `.spec` directly for further changes,
rather than retyping the full command).

The output is `dist\FunkuinoStudio\` — a **folder**, not a single portable
`.exe` (PyInstaller's `--onedir` mode: `FunkuinoStudio.exe` plus the DLLs,
`studio_web` assets and `ffmpeg.exe` it depends on all sit alongside it). Zip
the whole folder to hand it over; don't copy just the `.exe` out on its own.

Since the recipient's copy has no way to set environment variables
themselves, bake their configuration into a small launcher instead of asking
them to: a `.bat` file, dropped next to (or inside) the distributed folder,
that sets their values and starts the bundled exe —

```bat
@echo off
set ESPUINO_HOST=192.168.1.29
set ESPUINO_LOCAL_DIR=M:\Kids\audiofiles
set ESPUINO_REMOTE_ROOT=mp3
set FUNKUINO_DATA_DIR=M:\Kids
FunkuinoStudio\FunkuinoStudio.exe --no-cards --no-agent
```

— then turn that `.bat` into a desktop shortcut (right-click → *Create
shortcut*, rename, optionally give it a nicer icon). That shortcut is the only
thing they ever need to click.

### From source

The same tools work without any installation, and that is how they are
developed:

```bash
git clone https://github.com/sadilek/Funkuino.git
cd Funkuino
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

./sync --dry-run        # see what would be uploaded (files/ -> device)
./cards                 # lay out new cover images as a printable PDF
./studio                # open the dashboard at http://127.0.0.1:8800
```

`bin/funkuino <command>` is the same entry point the app uses; the `./sync`,
`./download`, … wrappers in the root are one-liners onto it. A checkout keeps
its library in the checkout, so cloning and running changes nothing outside it.

Requirements for the source route: Python ≥ 3.10 and **ffmpeg** on PATH.

Configuration: CLI flags > `ESPUINO_*` env vars > defaults (`ESPUINO_HOST`,
`ESPUINO_LOCAL_DIR`, `ESPUINO_REMOTE_ROOT`, `ESPUINO_HTTP_USER`/`_PASSWORD`).
`FUNKUINO_DATA_DIR` (or `funkuino --data-dir`) points the whole installation at
a different library folder — that is what the app configures for you.

## Library conventions

One RFID card = one *unit* of content, derived from file layout alone — no
database:

| Unit | Layout |
|------|--------|
| Song | `files/Lieder/<Artist> - <Title>.mp3` |
| Album | `files/<Artist> - <Album>/<NN> <Title>.mp3` |
| Audiobook episode | `files/<Series>/<NNN> - <Title>.mp3` (one merged file) |
| Multi-story episode | `files/<Series>/Folge <N>/<NN> <Story>.mp3` (folder = card, stories navigable with prev/next) |
| Webstream / playlist | `files/Webstreams_Playlisten/<...>/<name>.m3u[8]` (any depth, grouped by immediate parent folder), or a loose `.m3u`/`.m3u8` in `Lieder/` or directly in a series folder |

Cover images mirror the same paths under `card-covers/` (one image per card).

A `.m3u`/`.m3u8` file is played via the ESPuino firmware's own `LOCAL_M3U`
play mode (one line per entry, no folder references) and can mix webstream
URLs with paths to files already on the SD card — mixing has worked since
July 2021, per the feature's own [announcement
thread](https://forum.espuino.de/t/neues-feature-playlist-aus-lokaler-m3u-datei/551).
A local-file line is an *absolute* SD path (`/mp3/Lieder/Sommerlied.mp3`, not a
bare filename or a folder); avoid umlauts in those paths, since the firmware
author's own caveat is that the file's character encoding when uploaded isn't
guaranteed. A single-entry `.m3u` pointing at one webstream URL works exactly
like ESPuino's regular "webradio" entries — Funkuino doesn't need to
distinguish "one station" from "a podcast feed with twenty episodes", both are
just a `.m3u` file of a different length.

A library folder holds `files/`, `card-covers/`, `print-sheets/` and a visible
`status/` with the sync and print manifests — nothing hidden, so moving the
library by hand cannot silently leave its state behind. Your Claude token is
deliberately *not* in there: it lives with the installation, in
`~/Library/Application Support/Funkuino/` (or `~/.config/funkuino/` on Linux).

## Hard-won device knowledge

`CLAUDE.md` documents the ESPuino's HTTP API and a collection of quirks that
took real debugging to learn — mDNS resolution cost, SD-writer wedging, why
FTP is useless for diffing, NFC/NFD filename mismatches, and a full root-cause
analysis of the PN5180 "resting card briefly lost during playback" problem
(fixed upstream in ESPuino PR #444). If you script against an ESPuino, read it
before relearning any of this the hard way.

## A note on content

`./download` is a thin convenience wrapper around
[yt-dlp](https://github.com/yt-dlp/yt-dlp) that organises audio into the
library conventions above. Only download content you are entitled to download.

## Credits & license

Built around the excellent [ESPuino](https://github.com/biologist79/ESPuino) by
biologist79 and contributors. Funkuino itself is [MIT-licensed](https://github.com/sadilek/Funkuino/blob/main/LICENSE).
