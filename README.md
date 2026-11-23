# Cligram

**Download an Instagram Saved collection of Reels to your Linux PC.**

Cligram is a command-line tool that saves Reels as MP4s in a folder you choose. Run a one-time download or keep the folder in sync as you save and remove Reels on Instagram.

[Install](#1-install-cligram) · [Connect Instagram](#2-choose-a-collection-and-download-folder) · [Download Reels](#3-download-your-reels) · [Troubleshooting](#if-something-goes-wrong)

## 1. Install Cligram

Use a Linux desktop with Python 3.10 or newer and `uv`. First-time sign-in needs a graphical session. The background watcher also needs a running systemd user session. [Install uv](https://docs.astral.sh/uv/getting-started/installation/)

Clone the repository if you need a copy of the source.

```sh
git clone https://github.com/BL47E/cligram.git
cd cligram
```

Install the CLI from the source directory that contains `pyproject.toml`. The help command confirms that your shell can run it.

```sh
uv tool install --editable .
cligram --help
```

The editable install uses the files in this directory. Keep the source here, or reinstall from the new location if you move it.

If your shell cannot find `cligram` after installation, these commands add uv's executable directory to `PATH` for the current terminal and check it again.

```sh
export PATH="$(uv tool dir --bin):$PATH"
cligram --help
```

For a runner that opens a fresh shell per command, repeat the export in that shell or invoke `"$(uv tool dir --bin)/cligram"` directly.

Cligram can use an installed `chromium` or `google-chrome`. If you need a browser, install Chromium using the same Playwright version Cligram uses. [Playwright browser installation](https://playwright.dev/python/docs/browsers)

```sh
"$(uv tool dir)/cligram/bin/python" -m playwright install chromium
```

## 2. Choose a collection and download folder

In Instagram, open **Saved** and create or choose a named collection, such as **Download**. Add a Reel to that collection to test the download.

```sh
cligram setup --output "$HOME/Videos/Instagram Reels"
```

1. Sign in to Instagram in the Chromium window that opens. Complete any verification in the browser.
2. Once your account is open in the browser, return to the terminal and press Enter.
3. Enter the number beside the collection you want to sync. The terminal prints the chosen collection and folder.

The folder is created if needed. To choose a folder interactively, run `cligram setup` without `--output`. Use a writable local folder on a filesystem that supports hard links, such as ext4 or Btrfs.

## 3. Download your Reels

Use `cligram sync` for a one-time download. The next command lists the downloaded MP4s.

```sh
cligram sync
find "$HOME/Videos/Instagram Reels" -maxdepth 1 -type f -name '*.mp4' -print
```

Use your configured folder in the `find` command if you chose a different one. The sync summary reports how many Reels were downloaded in this pass. Reel filenames use their Instagram shortcodes, for example `Cxyz123.mp4`. Open one in your video player to check playback. If the watcher is already running, use `cligram stop` before `cligram sync`.

Use `cligram start` to watch the collection continuously.

```sh
cligram start
cligram status
cligram logs --follow
```

`start` checks the collection immediately and leaves the watcher running in the background. `status` shows the configured collection, folder, and interval. Look for `Downloaded …` in the logs, or open the folder to verify the MP4s. Press Ctrl+C to exit the log view. Run `cligram stop` to stop the watcher, and `cligram start` to resume it, including after a reboot.

## What sync does

- Cligram checks the whole collection on each pass. It downloads Reels and skips photos, carousels, and regular video posts.
- The watcher waits 60 seconds between checks by default. To change it, stop the watcher and run `cligram setup --interval 90`. The minimum is 30 seconds.
- A Reel removed from the collection is deleted locally after two complete checks confirm its absence. This applies to one-time syncs and background checks. Only complete checks count toward removal. Seeing the Reel again clears its pending removal.
- Cligram only deletes files it downloaded and still recognizes. It skips a download if the destination filename already exists.
- Choosing a different account, collection, or output folder starts a separate mirror. Each mirror tracks its own downloads.

## If something goes wrong

| What you see | What to do |
| --- | --- |
| `No named saved collections were found` | Create a named collection under Saved in the signed-in account and run `cligram setup` again. |
| Setup cannot open Chromium | Run setup from a terminal in your Linux desktop session. Check that `chromium` or `google-chrome` is installed, or use the Playwright Chromium command above. |
| `Could not reach the user service manager` | Run the watcher from your regular Linux user session and check `systemctl --user status`. With setup complete, `cligram sync` can download in one pass without the watcher. |
| The watcher stopped or no files appeared | Run `cligram logs` to see recent watcher activity. For a one-time sync, check its terminal output. If Instagram asks you to sign in again, stop the watcher if it is running and run `cligram setup` again. |
| The logs mention rate limiting | Leave the watcher running. It waits and retries automatically. |
| The log says a filename already exists | Open the file to check it. If you want Cligram to download and manage that Reel, move the existing file elsewhere and sync again. |

Cligram uses Instagram's web interface for Saved, so an Instagram change can interrupt syncing. Browser session data and download tracking live in `~/.local/state/cligram/`, or in `$XDG_STATE_HOME/cligram/` if set. Use the same state location across commands and keep the `browser/` directory private.

## Running this for someone else

An AI or script can install Cligram and run commands after setup. Ask the account owner to run `cligram setup` in an interactive terminal, sign in directly in the browser, and choose the collection and folder. Run later commands as the same Linux user. Use `cligram sync` for a requested one-time download and check its summary and the MP4 files. Use `cligram start` when the owner wants ongoing mirroring.
