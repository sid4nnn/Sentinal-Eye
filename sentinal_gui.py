"""Sentinal Eye control panel for a Mac or a standalone Jetson."""

import argparse
from collections import deque
from contextlib import suppress
from hashlib import sha256
from hmac import compare_digest
from html import escape
from http import HTTPStatus
from http.client import HTTPConnection, HTTPException
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address, ip_address
import json
import os
from pathlib import Path
import re
from secrets import token_urlsafe
import shlex
from shutil import copyfileobj
from signal import SIGINT, SIGKILL, SIGTERM
import ssl
import subprocess
import tarfile
import tempfile
from threading import Lock, Thread
from time import monotonic
from urllib.parse import parse_qs
import webbrowser


JETSON_USER = "admin"
DEFAULT_JETSON_IP = "192.168.0.29"
PREVIEW_PORT = 8765
PROJECT_DIR = Path(__file__).resolve().parent
GUI_DIR = PROJECT_DIR / "gui"
PANEL_CONFIG = PROJECT_DIR / ".panel"
PEOPLE_DIR = Path.home() / "Pictures" / "sentinal-people"
SSH = ["ssh", "-x", "-S", "none", "-o", "BatchMode=no", "-o", "NumberOfPasswordPrompts=1",
       "-o", "StrictHostKeyChecking=yes", "-o", "ForwardAgent=no", "-o", "ConnectTimeout=8",
       "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2"]
SSH_ENV = {**os.environ, "SSH_ASKPASS": str(PROJECT_DIR / "ssh_askpass.sh"), "SSH_ASKPASS_REQUIRE": "force"}
FACE_MODELS = {"buffalo_m", "buffalo_l"}
EXPRESSION_MODELS = {"off", "hsemotion", "dan"}
MAX_PHOTOS = 12
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 40 * 1024 * 1024
MAX_HEADER_BYTES = 8192
ALREADY_EXISTS = 73
PERSON_EVENT = "Sentinal person: "
PERSON_VISIBLE_SECONDS = 6
SESSION_COOKIE = "__Host-sentinal_session"
SESSION_SECONDS = 12 * 60 * 60
PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}

REMOTE_UPLOAD = r'''
set -eu
umask 077
cd "$HOME/sentinal-eye"
mkdir -p -- sentinal-people
dest="sentinal-people/$1"
if [ -e "$dest" ] || [ -L "$dest" ]; then exit 73; fi
stage=$(mktemp -d -- sentinal-people/.incoming.XXXXXXXX)
trap 'rm -rf -- "$stage"' EXIT
trap 'exit 1' HUP INT TERM
tar -xf - -C "$stage" --no-same-owner --no-same-permissions
mv -nT -- "$stage" "$dest"
if [ -d "$stage" ]; then exit 73; fi
'''


class RequestError(Exception):
    def __init__(self, message, status=HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


def jetson_ip(value):
    if isinstance(value, str) and "%" not in value:
        try:
            return str(ip_address(value.strip()))
        except ValueError:
            pass
    raise RequestError("Enter a valid Jetson IP address.")


def parse_jetson_stats(line):
    sample = {}
    if ram := re.search(r"\bRAM\s+(\d+)/(\d+)MB\b", line):
        sample["ramUsedMb"], sample["ramTotalMb"] = map(int, ram.groups())
    if cpu := re.search(r"\bCPU\s+\[([^\]]+)\]", line):
        cores = [int(load) for load in re.findall(r"(\d+)%", cpu[1])]
        if cores:
            sample["cpuPercent"] = round(sum(cores) / len(cores))
    if gpu := re.search(r"\bGR3D_FREQ\s+(\d+)%", line):
        sample["gpuPercent"] = int(gpu[1])
    temperatures = [(float(value), name) for name, value in re.findall(r"\b([\w-]+)@(\d+(?:\.\d+)?)C\b", line)]
    if temperatures:
        sample["temperatureC"], sample["temperatureSensor"] = max(temperatures)
    return sample


def recognition_rates(line):
    if not line.startswith(("Preview: ", "Detection: ")):
        return None
    rates = {sensor: float(fps) for sensor, fps in re.findall(r"sensor (\d+): (\d+(?:\.\d+)?) FPS", line)}
    if not rates:
        return None
    preview = re.match(r"Preview: (\d+(?:\.\d+)?) FPS", line)
    return {"detectionFps": rates, "previewFps": float(preview[1]) if preview else None}


class JetsonStats:
    def __init__(self):
        self.process = None
        self.sample = None

    def start(self):
        try:
            self.process = subprocess.Popen(["tegrastats", "--interval", "2000"],
                                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL, text=True, errors="replace")
        except OSError as exc:
            print(f"Jetson metrics unavailable: {exc}", flush=True)
            return
        Thread(target=self._read, args=(self.process,), daemon=True).start()

    def _read(self, process):
        with process.stdout:
            for line in process.stdout:
                if sample := parse_jetson_stats(line):
                    self.sample = (monotonic(), sample)
        process.wait()

    def status(self):
        sample = self.sample
        return sample[1] if sample and monotonic() - sample[0] < 8 else None

    def close(self):
        process = self.process
        if process is not None and process.poll() is None:
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


class CameraRun:
    def __init__(self, hosted=False):
        self.hosted = hosted
        self.lock = Lock()
        self.process = None
        self.lines = deque(maxlen=100)
        self.state = "idle"
        self.preview = False
        self.revision = 0
        self.ip = None
        self.person_events = False
        self.recent_people = {}
        self.rates = None

    def status(self):
        with self.lock:
            now = monotonic()
            self.recent_people = {label: (seen, profile) for label, (seen, profile) in self.recent_people.items()
                                  if now - seen < PERSON_VISIBLE_SECONDS}
            people = [self.recent_people[label][1] for label in sorted(self.recent_people, key=str.casefold)]
            rates = self.rates[1] if self.state == "running" and self.rates and now - self.rates[0] < 15 else None
            return {"state": self.state, "preview": self.preview and self.state == "running",
                    "revision": self.revision, "logs": list(self.lines), "ip": self.ip,
                    "personEvents": self.person_events, "people": people if self.state == "running" else [],
                    "hosted": self.hosted, "recognitionRates": rates}

    def start(self, settings):
        face, expression, mode = (settings.get(key) for key in ("face", "expression", "mode"))
        if not (isinstance(face, str) and face in FACE_MODELS
                and isinstance(expression, str) and expression in EXPRESSION_MODELS
                and mode in ("web", "headless")):
            raise RequestError("Select a valid model and output mode.")

        args = ["--web" if mode == "web" else "--headless", "--model", face]
        if expression != "off":
            args += ["--emotion-backend", expression]
        ip = None
        if self.hosted:
            command = ["flock", "-n", "-F", "-E", "75", str(PROJECT_DIR / ".gui-camera.lock"),
                       "bash", str(PROJECT_DIR / "run.sh"), *args]
            description = "Jetson"
            environment = {**os.environ, "SENTINAL_PERSON_EVENTS": "1"}
            stdin = subprocess.DEVNULL
        else:
            ip = jetson_ip(settings.get("ip", DEFAULT_JETSON_IP))
            target = f"{JETSON_USER}@{ip}"
            command = SSH + ["-tt", "-e", "none"]
            if mode == "web":
                command += ["-o", "ExitOnForwardFailure=yes", "-L",
                            f"127.0.0.1:{PREVIEW_PORT}:127.0.0.1:{PREVIEW_PORT}"]
            remote = ('exec env SENTINAL_PERSON_EVENTS=1 flock -n -F -E 75 "$HOME/sentinal-eye/.gui-camera.lock" '
                      'bash "$HOME/sentinal-eye/run.sh" ' + shlex.join(args))
            command += [target, remote]
            description = f"SSH → {target}"
            environment = SSH_ENV
            stdin = subprocess.PIPE
        with self.lock:
            if self.process is not None:
                raise RequestError("Stop the active session before starting another.", HTTPStatus.CONFLICT)
            try:
                process = subprocess.Popen(command, stdin=stdin,
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           start_new_session=True, env=environment)
            except OSError as exc:
                raise RequestError(f"Could not start recognition: {exc}", HTTPStatus.BAD_GATEWAY) from exc
            self.process = process
            self.lines.clear()
            self.recent_people.clear()
            self.person_events = False
            self.rates = None
            self.lines.append(f"{description} | {face} | {expression} | {mode}")
            self.preview = mode == "web"
            self.state = "starting"
            self.ip = ip
            self.revision += 1
            Thread(target=self._read, args=(process,), daemon=True).start()

    def _read(self, process):
        with process.stdout:
            for data in iter(lambda: process.stdout.readline(16384), b""):
                line = data.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                with self.lock:
                    if line == "Sentinal person details v1":
                        self.person_events = True
                        continue
                    if line.startswith(PERSON_EVENT):
                        try:
                            profile = json.loads(line[len(PERSON_EVENT):])
                        except ValueError:
                            continue
                        if (self.state == "running" and isinstance(profile, dict)
                                and all(isinstance(profile.get(key), str) for key in ("label", "fullName", "notes"))):
                            self.recent_people[profile["label"]] = (monotonic(), profile)
                        continue
                    self.lines.append(line)
                    if rates := recognition_rates(line):
                        self.rates = (monotonic(), rates)
                    if self.state == "starting" and line.startswith(("Web preview listening", "Headless face detection started")):
                        self.state = "running"
        code = process.wait()
        with self.lock:
            if process.stdin is not None:
                process.stdin.close()
            if self.state == "stopping" or code == 0:
                self.state = "idle"
            else:
                self.state = "error"
                if code == 75:
                    self.lines.append("Another control panel session is already running.")
                elif not self.hosted and code == 255 and any("Permission denied (" in line for line in self.lines):
                    self.lines.append("SSH sign-in was denied. Enter the Jetson admin password when macOS prompts you.")
                elif not self.hosted and code == 255:
                    self.lines.append("Check the Jetson address and the SSH error above.")
                self.lines.append(f"Session exited with code {code}.")
            self.process = None
            self.ip = None
            self.recent_people.clear()

    def stop(self):
        with self.lock:
            process = self.process
            if process is None or self.state == "stopping":
                return process
            self.state = "stopping"
            with suppress(OSError):
                if self.hosted:
                    os.killpg(process.pid, SIGINT)
                else:
                    # The remote PTY turns Ctrl+C into SIGINT.
                    process.stdin.write(b"\x03")
                    process.stdin.flush()
            Thread(target=self._wait_for_stop, args=(process,), daemon=True).start()
            return process

    @staticmethod
    def _wait_for_stop(process):
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                # Also close any password dialog started by this SSH process.
                os.killpg(process.pid, SIGTERM)

    def close(self):
        process = self.stop()
        if process is not None:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, SIGKILL)
                process.wait()


def person_fields(data, current_label=None):
    if not isinstance(data, dict):
        raise RequestError("The person's details are invalid.")
    label, name, notes = (data.get(key) for key in ("label", "fullName", "notes"))
    if not isinstance(label, str) or (label != current_label and (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,39}", label)
            or label != label.strip() or label.casefold() == "unknown")):
        raise RequestError("Use up to 40 characters for the label: letters A–Z, numbers, spaces, hyphens, or underscores.")
    if not isinstance(name, str) or not isinstance(notes, str) or len(name) > 100 or len(notes) > 1000:
        raise RequestError("The full name can have up to 100 characters, and notes up to 1,000.")
    return {"label": label, "fullName": name.strip(), "notes": notes.strip()}


def read_photo_upload(source, length):
    raw = source.readline(min(length, MAX_HEADER_BYTES) + 1)
    if len(raw) > MAX_HEADER_BYTES or not raw.endswith(b"\n") or len(raw) > length:
        raise RequestError("The upload header is invalid.")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RequestError("Could not read the upload details.") from exc
    if not isinstance(data, dict):
        raise RequestError("The upload details are invalid.")
    photos = data.get("photos")
    if not isinstance(photos, list) or not 1 <= len(photos) <= MAX_PHOTOS:
        raise RequestError("Choose between 1 and 12 reference photos.")
    total = 0
    for photo in photos:
        if (not isinstance(photo, dict) or type(photo.get("size")) is not int
                or not 1 <= photo["size"] <= MAX_PHOTO_BYTES):
            raise RequestError("Each photo must be between 1 byte and 10 MB.")
        total += photo["size"]
    if total > MAX_IMAGE_BYTES or total != length - len(raw):
        raise RequestError("The total photo size is invalid or exceeds 40 MB.")
    return data, photos


def photo_extension(head):
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    return None


def save_photo(source, directory, index, size):
    head = source.read(min(size, 12))
    extension = photo_extension(head)
    if extension is None:
        raise RequestError(f"Photo {index} is not readable as JPEG, PNG, or WebP. Export it as JPEG and try again.")
    destination = directory / f"photo-{index:02d}.{extension}"
    with destination.open("wb") as file:
        file.write(head)
        remaining = size - len(head)
        while remaining:
            chunk = source.read(min(remaining, 1024 * 1024))
            if not chunk:
                raise RequestError("The photo upload from the browser was interrupted.")
            file.write(chunk)
            remaining -= len(chunk)


def send_person(directory, label, ip):
    with tempfile.TemporaryFile() as archive:
        with tarfile.open(fileobj=archive, mode="w") as bundle:
            for path in directory.iterdir():
                bundle.add(path, arcname=path.name, recursive=False)
        archive.seek(0)
        remote = shlex.join(["bash", "-c", REMOTE_UPLOAD, "sentinal-upload", label])
        try:
            result = subprocess.run(SSH + ["-T", f"{JETSON_USER}@{ip}", remote], stdin=archive,
                                    capture_output=True, text=True, errors="replace", timeout=120, env=SSH_ENV)
        except subprocess.TimeoutExpired as exc:
            raise RequestError("The upload timed out. Check the Jetson connection and try again.",
                               HTTPStatus.GATEWAY_TIMEOUT) from exc
        except OSError as exc:
            raise RequestError(f"Could not launch SSH: {exc}", HTTPStatus.BAD_GATEWAY) from exc
    if result.returncode == ALREADY_EXISTS:
        raise RequestError("A folder with that label already exists on the Jetson.", HTTPStatus.CONFLICT)
    if result.returncode:
        error = result.stderr.strip()[:1500]
        raise RequestError(f"Upload to the Jetson failed. {error}", HTTPStatus.BAD_GATEWAY)


def enroll(source, length, people_dir, lock, hosted=False):
    manifest, photos = read_photo_upload(source, length)
    details = person_fields(manifest)
    ip = None if hosted else jetson_ip(manifest.get("ip", DEFAULT_JETSON_IP))
    label = details["label"]
    destination = people_dir / label
    with lock:
        people_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.path.lexists(destination):
            place = "Jetson" if hosted else "Mac"
            raise RequestError(f"A folder with that label already exists on the {place}.", HTTPStatus.CONFLICT)
        with tempfile.TemporaryDirectory(prefix=".incoming-", dir=people_dir) as temp:
            stage = Path(temp)
            for index, photo in enumerate(photos, start=1):
                save_photo(source, stage, index, photo["size"])
            (stage / "person.json").write_text(json.dumps(details, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if not hosted:
                send_person(stage, label, ip)
            try:
                stage.rename(destination)
            except OSError as exc:
                message = "Could not save the person on the Jetson" if hosted else "Uploaded to the Jetson, but could not save the Mac copy"
                raise RequestError(f"{message}: {exc}",
                                   HTTPStatus.INTERNAL_SERVER_ERROR) from exc
    return {"label": label, "photos": len(photos)}


def person_folder(directory):
    return not directory.name.startswith(".") and not directory.is_symlink() and directory.is_dir()


def gallery_name(name):
    return isinstance(name, str) and bool(name) and not name.startswith(".") and "/" not in name and "\x00" not in name


def reference_photo(path):
    return path.suffix.lower() in PHOTO_SUFFIXES and not path.is_symlink() and path.is_file()


def read_person_record(directory):
    path = directory / "person.json"
    if path.is_symlink() or not path.is_file():
        return {}, ""
    raw = path.read_bytes()
    try:
        record = json.loads(raw)
    except ValueError:
        record = {}
    return record if isinstance(record, dict) else {}, sha256(raw).hexdigest()


def person_profile(directory, record, version):
    photos = sorted((file.name for file in directory.iterdir() if gallery_name(file.name) and reference_photo(file)),
                    key=str.casefold)
    full_name, notes = record.get("fullName"), record.get("notes")
    return {"label": directory.name, "fullName": full_name if isinstance(full_name, str) else "",
            "notes": notes if isinstance(notes, str) else "", "photoCount": len(photos), "photos": photos, "version": version}


def list_people(people_dir, lock):
    with lock:
        if not people_dir.is_dir():
            return []
        return [person_profile(directory, *read_person_record(directory))
                for directory in sorted(people_dir.iterdir(), key=lambda path: path.name.casefold())
                if person_folder(directory)]


def selected_person_folder(people_dir, data):
    label = data.get("currentLabel") if isinstance(data, dict) else None
    if not gallery_name(label):
        raise RequestError("Select a person from the gallery.")
    folder = people_dir / label
    if not person_folder(folder):
        raise RequestError("This person is no longer in the gallery. Reload the list.", HTTPStatus.NOT_FOUND)
    return folder


def add_reference_photos(source, length, people_dir, lock):
    manifest, photos = read_photo_upload(source, length)
    with lock:
        folder = selected_person_folder(people_dir, manifest)
        record, version = read_person_record(folder)
        with tempfile.TemporaryDirectory(prefix=".incoming-", dir=folder) as temp:
            stage = Path(temp)
            for index, photo in enumerate(photos, start=1):
                save_photo(source, stage, index, photo["size"])
            batch_id = token_urlsafe(6)
            files = [(file, folder / f"{file.stem}-{batch_id}{file.suffix}") for file in stage.iterdir()]
            if any(os.path.lexists(destination) for _, destination in files):
                raise RequestError("Could not reserve names for the new photos. Try again.", HTTPStatus.CONFLICT)
            added = []
            try:
                for source_file, destination in files:
                    source_file.rename(destination)
                    added.append((source_file, destination))
            except OSError:
                for source_file, destination in reversed(added):
                    destination.rename(source_file)
                raise
        return person_profile(folder, record, version)


def move_reference_photo(people_dir, lock, data, restore=False):
    name = data.get("name") if isinstance(data, dict) else None
    if not gallery_name(name):
        raise RequestError("Select a reference photo.")
    with lock:
        folder = selected_person_folder(people_dir, data)
        record, version = read_person_record(folder)
        photo = folder / name
        archive = folder / ".removed-photos"
        if archive.is_symlink():
            raise RequestError("The removed photos folder is unavailable.", HTTPStatus.CONFLICT)
        if restore:
            removal_id = data.get("removalId")
            if not isinstance(removal_id, str) or not re.fullmatch(r"removed-[a-zA-Z0-9_-]{1,64}", removal_id):
                raise RequestError("Select a removed photo to restore.")
            removed_folder = archive / removal_id
            removed_photo = removed_folder / name
            if removed_folder.is_symlink() or not reference_photo(removed_photo):
                raise RequestError("The removed photo is no longer available.", HTTPStatus.NOT_FOUND)
            if os.path.lexists(photo):
                raise RequestError("A reference photo already has that filename.", HTTPStatus.CONFLICT)
            removed_photo.rename(photo)
            with suppress(OSError):
                removed_folder.rmdir()
            removed = None
        else:
            if not reference_photo(photo):
                raise RequestError("This reference photo is no longer in the gallery. Reload the list.", HTTPStatus.NOT_FOUND)
            archive.mkdir(mode=0o700, exist_ok=True)
            removed_folder = Path(tempfile.mkdtemp(dir=archive, prefix="removed-"))
            try:
                photo.rename(removed_folder / name)
            except OSError:
                removed_folder.rmdir()
                raise
            removed = {"name": name, "removalId": removed_folder.name}
        return {"person": person_profile(folder, record, version), "removed": removed}


def update_person(people_dir, lock, data):
    current_label = data.get("currentLabel") if isinstance(data, dict) else None
    if not gallery_name(current_label):
        raise RequestError("Select a person from the gallery.")
    fields = person_fields(data, current_label)
    source = people_dir / current_label
    destination = people_dir / fields["label"]
    renamed = source != destination

    with lock:
        if not person_folder(source):
            raise RequestError("This person is no longer in the gallery. Reload the list.", HTTPStatus.NOT_FOUND)
        record, version = read_person_record(source)
        if data.get("version") != version:
            raise RequestError("This record has changed. Reload the list before saving again.", HTTPStatus.CONFLICT)
        if renamed and os.path.lexists(destination):
            raise RequestError("A person with that label already exists.", HTTPStatus.CONFLICT)

        record.update(fields)
        body = (json.dumps(record, indent=2) + "\n").encode("utf-8")
        staged = None
        try:
            with tempfile.NamedTemporaryFile(dir=source, prefix=".person-", delete=False) as file:
                staged = Path(file.name)
                file.write(body)
            if renamed:
                source.rename(destination)
                staged = destination / staged.name
            try:
                staged.replace(destination / "person.json")
            except OSError as exc:
                if renamed:
                    try:
                        destination.rename(source)
                    except OSError:
                        raise RequestError("The label changed, but the profile could not be saved. Reload the list.",
                                           HTTPStatus.INTERNAL_SERVER_ERROR) from exc
                    staged = source / staged.name
                raise
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)
        return person_profile(destination, record, sha256(body).hexdigest())


ASSETS = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8"), "/photos.js": ("photos.js", "text/javascript; charset=utf-8")}
ADMIN_ASSETS = {"/admin": ("admin.html", "text/html; charset=utf-8"),
                "/admin.js": ("admin.js", "text/javascript; charset=utf-8")}


class PanelServer(ThreadingHTTPServer):
    def __init__(self, port, people_dir, host="127.0.0.1", tls=None, password=None):
        if (tls is None) != (password is None) or (host != "127.0.0.1" and tls is None):
            raise ValueError("Network access requires TLS and a panel password.")
        super().__init__((host, port), PanelHandler)
        self.tls = tls
        self.hosted = tls is not None
        self.panel_password = password.encode("ascii") if self.hosted else None
        self.sessions = {}
        self.session_lock = Lock()
        default_port = 443 if self.hosted else 80
        self.authority = host if self.server_port == default_port else f"{host}:{self.server_port}"
        self.origin = f"{'https' if self.hosted else 'http'}://{self.authority}"
        self.camera = CameraRun(hosted=self.hosted)
        self.stats = JetsonStats()
        self.people_dir = people_dir
        self.gallery_lock = Lock()

    def status(self):
        status = self.camera.status()
        status["jetsonStats"] = self.stats.status()
        return status

    def sign_in(self, username, password):
        if username.strip().lower() != "admin" or not compare_digest(password.strip().encode("utf-8"), self.panel_password):
            return None
        token = token_urlsafe(32)
        now = monotonic()
        with self.session_lock:
            self.sessions = {key: expires for key, expires in self.sessions.items() if expires > now}
            self.sessions[token] = now + SESSION_SECONDS
        return token

    def has_session(self, token):
        with self.session_lock:
            if self.sessions.get(token, 0) > monotonic():
                return True
            self.sessions.pop(token, None)
            return False

    def sign_out(self, token):
        with self.session_lock:
            self.sessions.pop(token, None)

    def finish_request(self, request, address):
        request.settimeout(15)
        if self.tls is None:
            super().finish_request(request, address)
            return
        try:
            with self.tls.wrap_socket(request, server_side=True) as connection:
                super().finish_request(connection, address)
        except OSError:
            pass


class PanelHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def response_headers(self, status, length, content_type, headers):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        image_sources = "'self' blob:"
        if not self.server.hosted:
            image_sources += f" http://127.0.0.1:{PREVIEW_PORT}"
        self.send_header("Content-Security-Policy", "default-src 'self'; "
                         f"img-src {image_sources}; "
                         "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        for name, value in headers:
            self.send_header(name, value)
        if self.command == "POST" or self.close_connection:
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()

    def respond(self, status, body, content_type, headers=()):
        self.response_headers(status, len(body), content_type, headers)
        with suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def json_response(self, status, data):
        self.respond(status, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    def check_origin(self, post=False):
        origin = self.headers.get("Origin")
        if post and origin is None and self.headers.get("Referer", "").startswith(self.server.origin + "/"):
            origin = self.server.origin
        if self.headers.get("Host") != self.server.authority or (post and origin != self.server.origin):
            self.json_response(HTTPStatus.FORBIDDEN, {"error": "This request must come from the local control panel."})
            return False
        return True

    def session_cookie(self):
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", "")).get(SESSION_COOKIE)
        except CookieError:
            return None
        return cookie.value if cookie else None

    def redirect(self, location, session=None, max_age=SESSION_SECONDS):
        headers = [("Location", location)]
        if session is not None:
            headers.append(("Set-Cookie", f"{SESSION_COOKIE}={session}; Max-Age={max_age}; Path=/; Secure; HttpOnly; SameSite=Lax"))
        self.respond(HTTPStatus.SEE_OTHER, b"", "text/plain; charset=utf-8", headers)

    def login_page(self, error=None):
        page = (GUI_DIR / "login.html").read_text(encoding="utf-8")
        message = f'<p class="message" data-kind="error" role="alert">{escape(error)}</p>' if error else ""
        body = page.replace("<!-- LOGIN_ERROR -->", message).encode("utf-8")
        self.respond(HTTPStatus.UNAUTHORIZED if error else HTTPStatus.OK, body, "text/html; charset=utf-8")

    def login(self, length):
        if not 1 <= length <= 1024 or self.headers.get_content_type() != "application/x-www-form-urlencoded":
            raise RequestError("Invalid sign-in request.")
        fields = parse_qs(self.rfile.read(length).decode("utf-8"), strict_parsing=True, max_num_fields=2)
        token = self.server.sign_in(fields.get("username", [""])[0], fields.get("password", [""])[0])
        if token is None:
            self.login_page("Sign-in failed. Use admin and the password from panel-password.txt.")
            return
        self.redirect("/", session=token)

    def camera_frame(self):
        run = self.server.camera
        with run.lock:
            preview = run.preview and run.state == "running"
        if preview:
            camera = HTTPConnection("127.0.0.1", PREVIEW_PORT, timeout=2)
            try:
                camera.request("GET", "/frame.jpg")
                response = camera.getresponse()
                if response.status == HTTPStatus.OK and response.headers.get_content_type() == "image/jpeg":
                    frame = response.read(4 * 1024 * 1024 + 1)
                    if 0 < len(frame) <= 4 * 1024 * 1024:
                        self.respond(HTTPStatus.OK, frame, "image/jpeg")
                        return
            except (OSError, HTTPException):
                pass
            finally:
                camera.close()
        self.json_response(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Waiting for a camera frame."})

    def gallery_photo(self, query):
        try:
            fields = parse_qs(query, strict_parsing=True, max_num_fields=2)
            label, name = fields.get("label", [None])[0], fields.get("name", [None])[0]
            if not gallery_name(label) or not gallery_name(name):
                raise ValueError("Invalid gallery path")
            folder = self.server.people_dir / label
            photo = folder / name
            with self.server.gallery_lock:
                if not person_folder(folder) or not reference_photo(photo):
                    raise FileNotFoundError(photo)
                source = photo.open("rb")
        except (ValueError, OSError):
            self.json_response(HTTPStatus.NOT_FOUND, {"error": "Reference photo not found."})
            return

        with source:
            extension = photo_extension(source.read(12))
            if extension is None:
                self.json_response(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "Unsupported reference photo format."})
                return
            source.seek(0)
            length = os.fstat(source.fileno()).st_size
            content_type = "image/jpeg" if extension == "jpg" else f"image/{extension}"
            headers = (("Cross-Origin-Resource-Policy", "same-origin"),)
            with suppress(OSError):
                self.response_headers(HTTPStatus.OK, length, content_type, headers)
                copyfileobj(source, self.wfile, length=64 * 1024)

    def do_GET(self):
        if not self.check_origin():
            return
        if self.server.hosted:
            path, _, query = self.path.partition("?")
            if path == "/admin/photo":
                self.gallery_photo(query)
                return
            if self.path == "/api/people":
                try:
                    people = list_people(self.server.people_dir, self.server.gallery_lock)
                except OSError:
                    self.json_response(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Could not read the Jetson gallery."})
                else:
                    self.json_response(HTTPStatus.OK, {"people": people})
                return
            if self.path in ADMIN_ASSETS:
                filename, content_type = ADMIN_ASSETS[self.path]
                self.respond(HTTPStatus.OK, (GUI_DIR / filename).read_bytes(), content_type)
                return
            if self.path == "/admin/":
                self.redirect("/admin")
                return
        if self.server.hosted and self.path not in {"/style.css", "/photos.js"}:
            signed_in = self.server.has_session(self.session_cookie())
            if self.path == "/login":
                if signed_in:
                    self.redirect("/")
                else:
                    self.login_page()
                return
            if not signed_in:
                if self.path == "/":
                    self.redirect("/login")
                else:
                    self.json_response(HTTPStatus.UNAUTHORIZED, {"error": "Sign in to Sentinal Eye."})
                return
        if self.path == "/api/status":
            self.json_response(HTTPStatus.OK, self.server.status())
        elif self.path == "/frame.jpg" and self.server.hosted:
            self.camera_frame()
        elif self.path in ASSETS:
            filename, content_type = ASSETS[self.path]
            self.respond(HTTPStatus.OK, (GUI_DIR / filename).read_bytes(), content_type)
        else:
            self.json_response(HTTPStatus.NOT_FOUND, {"error": "Page not found."})

    def do_POST(self):
        if not self.check_origin(post=True):
            return
        self.connection.settimeout(30)
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if self.server.hosted:
                if self.path == "/api/people/photos/add":
                    if not 1 <= length <= MAX_IMAGE_BYTES + MAX_HEADER_BYTES:
                        raise RequestError("The upload size is invalid.", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                    profile = add_reference_photos(self.rfile, length, self.server.people_dir, self.server.gallery_lock)
                    self.json_response(HTTPStatus.OK, {"person": profile})
                    return
                if self.path in {"/api/people/photos/remove", "/api/people/photos/restore"}:
                    if not 1 <= length <= MAX_HEADER_BYTES or self.headers.get_content_type() != "application/json":
                        raise RequestError("Invalid reference photo request.")
                    data = json.loads(self.rfile.read(length))
                    result = move_reference_photo(self.server.people_dir, self.server.gallery_lock, data,
                                                  restore=self.path.endswith("/restore"))
                    self.json_response(HTTPStatus.OK, result)
                    return
                if self.path == "/api/people/update":
                    if not 1 <= length <= MAX_HEADER_BYTES or self.headers.get_content_type() != "application/json":
                        raise RequestError("Invalid profile update.")
                    data = json.loads(self.rfile.read(length))
                    profile = update_person(self.server.people_dir, self.server.gallery_lock, data)
                    self.json_response(HTTPStatus.OK, {"person": profile})
                    return
                if self.path == "/login":
                    self.login(length)
                    return
                token = self.session_cookie()
                if self.path == "/logout":
                    self.server.sign_out(token)
                    self.redirect("/login", session="", max_age=0)
                    return
                if not self.server.has_session(token):
                    self.json_response(HTTPStatus.UNAUTHORIZED, {"error": "Sign in to Sentinal Eye."})
                    return
            if self.path == "/api/enroll":
                if not 1 <= length <= MAX_IMAGE_BYTES + MAX_HEADER_BYTES:
                    raise RequestError("The upload size is invalid.", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                result = enroll(self.rfile, length, self.server.people_dir, self.server.gallery_lock, self.server.hosted)
            elif self.path == "/api/start" and 1 <= length <= 1024:
                settings = json.loads(self.rfile.read(length))
                if not isinstance(settings, dict):
                    raise RequestError("The camera settings are invalid.")
                self.server.camera.start(settings)
                result = self.server.status()
            elif self.path == "/api/stop" and length == 0:
                self.server.camera.stop()
                result = self.server.status()
            else:
                raise RequestError("Invalid request.")
            self.json_response(HTTPStatus.OK, result)
        except RequestError as exc:
            self.json_response(exc.status, {"error": str(exc)})
        except (ValueError, UnicodeError):
            self.json_response(HTTPStatus.BAD_REQUEST, {"error": "Invalid request."})
        except OSError as exc:
            self.json_response(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Could not complete the request: {exc}"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8750)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--jetson", metavar="IP", help="Host the panel and run recognition on this Jetson")
    args = parser.parse_args()
    try:
        if args.jetson:
            ip = IPv4Address(args.jetson)
            if ip.is_unspecified or ip.is_multicast or ip.is_loopback or ip.is_link_local:
                raise ValueError("Enter the Jetson's IPv4 LAN address.")
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.minimum_version = ssl.TLSVersion.TLSv1_2
            tls.load_cert_chain(PANEL_CONFIG / "server.crt", PANEL_CONFIG / "server.key")
            password = (PANEL_CONFIG / "password").read_text(encoding="ascii").strip()
            if not re.fullmatch(r"[a-f0-9]{48}", password):
                raise ValueError("Invalid panel password. Run the Jetson installer.")
            server = PanelServer(args.port, PROJECT_DIR / "sentinal-people", host=str(ip), tls=tls, password=password)
        else:
            server = PanelServer(args.port, PEOPLE_DIR)
    except (OSError, OverflowError, ValueError) as exc:
        parser.exit(1, f"Could not open the Sentinal Eye control panel on port {args.port}: {exc}\n")
    with server:
        print(f"Sentinal Eye: {server.origin}/  (Ctrl+C to close)", flush=True)
        if not args.no_browser and not server.hosted:
            Thread(target=webbrowser.open, args=(server.origin + "/",), daemon=True).start()
        if server.hosted:
            server.stats.start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.camera.close()
            server.stats.close()


if __name__ == "__main__":
    main()
