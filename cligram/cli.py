import argparse
import fcntl
import hashlib
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Event

from .instagram import InstagramError, LoginRequired, RateLimited, open_instagram
from .mirror import StopRequested, sync_collection


DEFAULT_INTERVAL = 60
MIN_INTERVAL = 30
UNIT = "cligram.service"
LOG = logging.getLogger(__name__)


class CLIError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    account_id: str
    collection_id: str
    collection_name: str
    output: str
    interval: int = DEFAULT_INTERVAL

    @property
    def folder(self):
        return Path(self.output)

    @property
    def database_name(self):
        target = json.dumps([self.account_id, self.collection_id, self.output])
        return f"mirror-{hashlib.sha256(target.encode()).hexdigest()}.sqlite3"


def state_directory():
    base = os.environ.get("XDG_STATE_HOME")
    return (Path(base).expanduser() if base else Path.home() / ".local" / "state") / "cligram"


def load_settings(state, *, required=True):
    try:
        data = json.loads((state / "settings.json").read_text(encoding="utf-8"))
        settings = Settings(**data)
        if (
            not isinstance(settings.account_id, str) or not settings.account_id.isdecimal()
            or not isinstance(settings.collection_id, str) or not settings.collection_id.isdecimal()
            or not isinstance(settings.collection_name, str)
            or not isinstance(settings.output, str) or not settings.folder.is_absolute()
            or type(settings.interval) is not int or settings.interval < MIN_INTERVAL
        ):
            raise ValueError("Invalid settings")
        return settings
    except FileNotFoundError:
        if required:
            raise CLIError("Run `cligram setup` to pick a collection and download folder.") from None
        return None
    except (ValueError, TypeError):
        raise CLIError("Cligram settings are invalid.") from None


def save_settings(state, settings):
    path = state / "settings.json"
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=state, delete=False) as temporary:
        staged = Path(temporary.name)
        try:
            json.dump(asdict(settings), temporary, indent=2, ensure_ascii=False)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            staged.replace(path)
        finally:
            staged.unlink(missing_ok=True)


@contextmanager
def exclusive(state):
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state / "sync.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CLIError("Cligram is busy. Use `cligram stop` to stop the background watcher.") from None
        yield


def interval_value(text):
    try:
        seconds = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("Enter a whole number of seconds.") from None
    if seconds < MIN_INTERVAL:
        raise argparse.ArgumentTypeError(f"Use an interval of at least {MIN_INTERVAL} seconds.")
    return seconds


def choose_collection(collections, previous):
    if not collections:
        raise CLIError("No named saved collections were found in this Instagram account.")
    default = None
    print("\nSaved collections")
    for number, collection in enumerate(collections, 1):
        print(f"  {number}. {collection.name}")
        if previous and collection.id == previous.collection_id:
            default = number

    suffix = f" [{default}]" if default else ""
    while True:
        choice = input(f"Choose a collection by number{suffix} › ").strip()
        if not choice and default:
            return collections[default - 1]
        try:
            number = int(choice)
        except ValueError:
            number = 0
        if 1 <= number <= len(collections):
            return collections[number - 1]
        print(f"Enter a number from 1 to {len(collections)}.")


def setup(state, args):
    with exclusive(state):
        previous = load_settings(state, required=False)
        with open_instagram(state / "browser", headed=True) as instagram:
            account_id = instagram.login()
            collections = instagram.collections()
        selection = choose_collection(collections, previous if previous and previous.account_id == account_id else None)

        output = args.output
        while not output:
            default = f" [{previous.output}]" if previous else ""
            output = input(f"Download folder{default} › ").strip()
            if not output and previous:
                output = previous.output
        folder = Path(output).expanduser().resolve()
        if folder.is_relative_to(state):
            raise CLIError("Choose a download folder outside Cligram's private state directory.")
        folder.mkdir(parents=True, exist_ok=True)
        if not os.access(folder, os.W_OK | os.X_OK):
            raise CLIError(f"Cligram cannot write to {folder}.")
        interval = args.interval or (previous.interval if previous else DEFAULT_INTERVAL)
        settings = Settings(account_id, selection.id, selection.name, str(folder), interval)
        save_settings(state, settings)
    print(f'Configured “{selection.name}” → {folder} (every {interval} seconds).')
    print("Run `cligram start` to begin the initial download and watch for changes.")


def verify_account(instagram, settings):
    if instagram.account_id() != settings.account_id:
        raise LoginRequired("The browser is signed into a different account. Run `cligram setup`.")


def sync_summary(result):
    return (
        f"Downloaded {result.downloaded}, removed {result.deleted}, "
        f"awaiting removal confirmation {result.pending_removal}, skipped {result.skipped}."
    )


def sync_once(state):
    with exclusive(state):
        settings = load_settings(state)
        with open_instagram(state / "browser") as instagram:
            verify_account(instagram, settings)
            result = sync_collection(
                instagram, settings.collection_id, settings.folder, state / settings.database_name, Event()
            )
    print(sync_summary(result))
    return int(not result.complete or bool(result.skipped))


def watch_collection(state):
    stop = Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    with exclusive(state):
        settings = load_settings(state)
        LOG.info('Watching “%s” → %s every %d seconds.', settings.collection_name, settings.folder, settings.interval)
        backoff = 0
        first_poll = True
        while not stop.is_set():
            try:
                with open_instagram(state / "browser") as instagram:
                    while not stop.is_set():
                        verify_account(instagram, settings)
                        result = sync_collection(
                            instagram, settings.collection_id, settings.folder, state / settings.database_name, stop
                        )
                        if first_poll or result.downloaded or result.deleted or result.pending_removal or result.skipped:
                            LOG.info(sync_summary(result))
                        first_poll = False
                        backoff = 0
                        stop.wait(settings.interval)
            except StopRequested:
                break
            except LoginRequired as error:
                if stop.is_set():
                    break
                LOG.error("%s", error)
                return 2
            except (InstagramError, OSError, sqlite3.Error) as error:
                if stop.is_set():
                    break
                minimum = 300 if isinstance(error, RateLimited) else settings.interval
                backoff = max(minimum, min(2 * backoff, 900))
                LOG.warning("%s Retrying in %d seconds.", error, backoff)
                stop.wait(backoff)
    LOG.info("Watcher stopped.")
    return 0


def service_state():
    result = subprocess.run(
        ["systemctl", "--user", "show", "--property=ActiveState", "--value", UNIT],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        message = result.stderr.strip()
        if message:
            raise CLIError(f"Could not reach the user service manager. {message}")
    return result.stdout.strip() or "inactive"


def run_system_command(command):
    result = subprocess.run(command, check=False)
    if result.returncode:
        raise CLIError(f"{command[0]} exited with status {result.returncode}.")


def start(state):
    load_settings(state)
    if service_state() in {"active", "activating", "reloading"}:
        print("Cligram is already running.")
        return
    command = [
        "systemd-run", "--user", "--quiet", "--collect", f"--unit={UNIT}",
        "--property=Description=Cligram saved Reel watcher", "--property=Type=exec",
        "--property=Restart=on-failure", "--property=RestartSec=30s", "--property=RestartPreventExitStatus=2",
        "--property=TimeoutStopSec=30s", "--property=KillMode=mixed", "--property=UMask=0077",
    ]
    if browsers_path := os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        command.append(f"--setenv=PLAYWRIGHT_BROWSERS_PATH={browsers_path}")
    run_system_command([*command, sys.executable, "-m", "cligram", "_worker", str(state)])
    print("Cligram started. Run `cligram logs --follow` to follow the sync.")


def stop_service():
    if service_state() in {"inactive", "failed"}:
        print("Cligram is stopped.")
        return
    run_system_command(["systemctl", "--user", "stop", UNIT])
    print("Cligram stopped.")


def status(state):
    current = service_state()
    label = {"active": "running", "inactive": "stopped", "activating": "starting"}.get(current, current)
    print(f"Cligram is {label}.")
    if settings := load_settings(state, required=False):
        print(f"Collection  {settings.collection_name}")
        print(f"Folder      {settings.folder}")
        print(f"Interval    {settings.interval} seconds")
    else:
        print("Run `cligram setup` to configure the watcher.")


def parser_for_cli():
    parser = argparse.ArgumentParser(prog="cligram", description="Mirror one Instagram saved Reel collection locally.")
    commands = parser.add_subparsers(dest="command", required=True)
    configure = commands.add_parser("setup", help="Sign in and choose a collection and download folder")
    configure.add_argument("--output", metavar="DIR", help="Download folder (prompted for by default)")
    configure.add_argument("--interval", metavar="SECONDS", type=interval_value, help="Polling interval (default 60)")
    commands.add_parser("start", help="Start the background watcher")
    commands.add_parser("stop", help="Stop the background watcher")
    commands.add_parser("status", help="Show watcher state and configured collection")
    commands.add_parser("sync", help="Run one sync while the watcher is stopped")
    logs = commands.add_parser("logs", help="Show watcher logs")
    logs.add_argument("--follow", "-f", action="store_true", help="Stream new log entries")
    return parser


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if len(arguments) == 2 and arguments[0] == "_worker":
            return watch_collection(Path(arguments[1]))
        args = parser_for_cli().parse_args(arguments)
        state = state_directory().resolve()
        if args.command == "setup":
            setup(state, args)
        elif args.command == "start":
            start(state)
        elif args.command == "stop":
            stop_service()
        elif args.command == "status":
            status(state)
        elif args.command == "sync":
            return sync_once(state)
        elif args.command == "logs":
            command = ["journalctl", "--user-unit", UNIT, "--no-pager", "-n", "50"]
            if args.follow:
                command.append("--follow")
            run_system_command(command)
    except (KeyboardInterrupt, EOFError):
        return 130
    except (CLIError, InstagramError, OSError, sqlite3.Error) as error:
        print(f"Cligram: {error}", file=sys.stderr)
        return 1
    return 0
