import argparse
import json
import sqlite3
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager, suppress
from functools import partial
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from os import fsencode, getenv
from pathlib import Path
from threading import Condition, Thread
from time import monotonic, sleep, strftime

import cv2
import gi
import numpy as np
from insightface.app import FaceAnalysis
from insightface.utils.storage import ensure_available
from onnxruntime import SessionOptions, get_available_providers

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import Gst, GstApp


WIDTH = 960
HEIGHT = 540
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MODEL_DETECTORS = {"buffalo_l": "det_10g.onnx", "buffalo_m": "det_2.5g.onnx"}
RECOGNITION_MODEL = "w600k_r50.onnx"
PREVIEW_PAGE = b"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sentinal Eye</title>
<style>
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #141414; color: white; }
  img { display: block; width: 100%; max-height: 100vh; object-fit: contain; }
</style>
<img src="/stream.mjpg" alt="Live camera preview">
</html>
"""


def camera_branch(sensor_id, white_balance):
    return (
        f"nvarguscamerasrc sensor-id={sensor_id} wbmode={white_balance} "
        f"! video/x-raw(memory:NVMM),width={WIDTH},height={HEIGHT},framerate=30/1 "
        "! queue ! nvvidconv ! video/x-raw,format=I420 "
        "! videoconvert ! video/x-raw,format=BGR "
        f"! appsink name=camera{sensor_id} max-buffers=1 drop=true "
        "sync=false async=false wait-on-eos=false enable-last-sample=false"
    )


def preview_branch(width):
    return (
        "appsrc name=preview is-live=true format=time do-timestamp=true "
        "max-buffers=1 max-bytes=0 leaky-type=downstream "
        f'caps="video/x-raw,format=BGR,width={width},height={HEIGHT},framerate=30/1" '
        "! videoconvert ! video/x-raw,format=I420 ! nvvidconv "
        "! video/x-raw(memory:NVMM),format=NV12 "
        "! nv3dsink window-x=0 window-y=0 sync=false async=false"
    )


def check_pipeline_error(bus):
    message = bus.pop_filtered(Gst.MessageType.ERROR)
    if message is not None:
        error, debug = message.parse_error()
        raise RuntimeError(f"{message.src.get_name()}: {error.message}\n{debug or ''}")


def read_frame(camera: GstApp.AppSink, sensor_id, bus):
    sample = camera.try_pull_sample(5 * Gst.SECOND)
    if sample is None:
        check_pipeline_error(bus)
        raise RuntimeError(f"No video frames received from sensor {sensor_id}")

    # At 960 pixels, BGR rows need no padding to a four-byte boundary.
    buffer = sample.get_buffer()
    size = WIDTH * HEIGHT * 3
    if buffer.get_size() != size:
        raise RuntimeError(f"Unexpected frame layout from sensor {sensor_id}")
    pixels = np.frombuffer(buffer.extract_dup(0, size), dtype=np.uint8)
    return pixels.reshape(HEIGHT, WIDTH, 3)


def reference_photos(directory):
    return [path for path in sorted(directory.iterdir())
            if path.is_file() and path.suffix.lower() in PHOTO_EXTENSIONS]


def read_reference_embedding(analysis, path):
    image = cv2.imread(str(path))
    if image is None:
        print(f"Skipping {path}: could not read the image", flush=True)
        return None
    faces = analysis.get(image)
    if len(faces) != 1:
        print(f"Skipping {path}: expected one face, found {len(faces)}", flush=True)
        return None
    embedding = faces[0].normed_embedding
    if embedding is None or not np.isfinite(embedding).all():
        print(f"Skipping {path}: no usable face embedding", flush=True)
        return None
    return np.asarray(embedding, dtype=np.float32)


def load_reference_gallery(analysis, directory, name):
    embeddings = []
    for path in reference_photos(directory):
        embedding = read_reference_embedding(analysis, path)
        if embedding is not None:
            embeddings.append(embedding)

    if not embeddings:
        raise RuntimeError(f"No usable reference photos in {directory}. Use photos with one clear face each.")
    print(f"Loaded reference photos: {len(embeddings)}", flush=True)
    return np.stack(embeddings), (name,) * len(embeddings)


def valid_display_name(name):
    return bool(name.strip()) and name.isascii() and name.isprintable()


def file_version(path):
    info = path.stat()
    return f"{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}"


def load_people_gallery(analysis, directory, model_name, det_size):
    photos = {}
    for person in sorted(directory.iterdir()):
        if not person.is_dir() or person.name.startswith("."):
            continue
        if not valid_display_name(person.name):
            print(f"Skipping folder {person.name!r}: use Latin letters for the video label", flush=True)
            continue
        for photo in reference_photos(person):
            photos[str(photo.relative_to(directory))] = photo

    cache_dir = Path.home() / ".cache" / "sentinal-eye"
    cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    gallery_key = sha256(fsencode(directory.resolve())).hexdigest()
    cache_path = cache_dir / f"{gallery_key}.sqlite3"
    model_key = f"{model_name}:{det_size}"
    model_dir = Path(analysis.model_dir)
    detector_version = file_version(model_dir / MODEL_DETECTORS[model_name])
    recognition_version = file_version(model_dir / RECOGNITION_MODEL)
    model_version = f"v1:{detector_version}:{recognition_version}"

    embeddings = []
    names = []
    updates = []
    reused = 0
    print(f"Loading face gallery: {len(photos)} photos in {directory}", flush=True)
    with closing(sqlite3.connect(cache_path)) as cache:
        cache.execute("""
            CREATE TABLE IF NOT EXISTS face_embeddings (
                model TEXT NOT NULL, photo TEXT NOT NULL, version TEXT NOT NULL, embedding BLOB,
                PRIMARY KEY (model, photo)
            )
        """)
        cached = {
            photo: (version, embedding) for photo, version, embedding in cache.execute(
                "SELECT photo, version, embedding FROM face_embeddings WHERE model = ?", (model_key,)
            )
        }
        obsolete = {photo for (photo,) in cache.execute("SELECT DISTINCT photo FROM face_embeddings")} - photos.keys()
        for relative_path, photo in photos.items():
            try:
                version = f"{model_version}:{file_version(photo)}"
            except FileNotFoundError:
                continue
            cached_version, cached_embedding = cached.get(relative_path, (None, None))
            if cached_version == version:
                reused += 1
                embedding = None if cached_embedding is None else np.frombuffer(cached_embedding, dtype=np.float32)
            else:
                embedding = read_reference_embedding(analysis, photo)
                updates.append((model_key, relative_path, version, None if embedding is None else embedding.tobytes()))
            if embedding is not None:
                embeddings.append(embedding)
                names.append(photo.parent.name)

        # Face analysis happens outside the write transaction so it never holds the database lock.
        with cache:
            cache.executemany("""
                INSERT INTO face_embeddings (model, photo, version, embedding) VALUES (?, ?, ?, ?)
                ON CONFLICT (model, photo) DO UPDATE
                SET version = excluded.version, embedding = excluded.embedding
            """, updates)
            cache.executemany("DELETE FROM face_embeddings WHERE photo = ?", [(photo,) for photo in obsolete])

    if not embeddings:
        print(f"Face gallery: no usable reference photos in {directory}. Running face detection without identity matches.",
              flush=True)
        return None
    print(f"Face gallery: {len(set(names))} people, {len(embeddings)} usable photos, "
          f"{reused} cached, {len(updates)} processed", flush=True)
    return np.stack(embeddings), tuple(names)


def detect_faces(analysis, frame, sensor_id, gallery, threshold, expressions):
    faces = analysis.get(frame)
    identities = []
    match_percentages = []
    for face in faces:
        label = ""
        match = None
        if gallery is not None:
            label = "Unknown"
            embedding = face.normed_embedding
            if embedding is not None:
                references, names = gallery
                scores = references @ embedding
                best = int(scores.argmax())
                score = float(scores[best])
                if np.isfinite(score):
                    match = round(min(1.0, max(0.0, score)) * 100)
                    if score >= threshold:
                        label = names[best]
        identities.append(label)
        match_percentages.append(match)
    emotions = [None] * len(faces) if expressions is None else expressions.classify(frame, faces, identities, sensor_id)
    return [(tuple(map(int, face.bbox)), name, emotion, match)
            for face, name, emotion, match in zip(faces, identities, emotions, match_percentages, strict=True)]


def annotation_label(name, emotion, match=None):
    parts = [part for part in (name, emotion) if part]
    if match is not None:
        parts.append(f"best {match}%" if name == "Unknown" else f"{match}% match")
    return " | ".join(parts)


def person_reporter(directory, labels):
    profiles = {}
    last_report = {}
    for label in set(labels) - {"Unknown"}:
        path = directory / label / "person.json"
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        full_name, notes = metadata.get("fullName"), metadata.get("notes")
        profiles[label] = {"label": label,
                           "fullName": full_name.strip()[:100] if isinstance(full_name, str) else "",
                           "notes": notes.strip()[:1000] if isinstance(notes, str) else ""}

    print("Sentinal person details v1", flush=True)

    def report(annotations):
        now = monotonic()
        matches = {}
        for _, label, _, match in annotations:
            if label in profiles and match is not None:
                matches[label] = max(match, matches.get(label, 0))
        for label, match in matches.items():
            if label not in last_report or now - last_report[label] >= 1:
                print("Sentinal person: " + json.dumps({**profiles[label], "matchPercent": match}), flush=True)
                last_report[label] = now

    return report


def resolve_model_dir(model_name, recognize):
    model_dir = Path(ensure_available("models", model_name))
    expected_files = [MODEL_DETECTORS[model_name]]
    if recognize:
        expected_files.append(RECOGNITION_MODEL)

    # The buffalo_m archive wraps its ONNX files in a second buffalo_m directory.
    for directory in (model_dir, model_dir / model_name):
        if all((directory / filename).is_file() for filename in expected_files):
            return directory.resolve()

    raise RuntimeError(f"Expected {', '.join(expected_files)} in {model_dir} or {model_dir / model_name}")


def load_face_detector(model_name, device, det_size, recognize):
    use_cuda = device == "cuda"
    providers = ["CPUExecutionProvider"]
    if use_cuda:
        if "CUDAExecutionProvider" not in get_available_providers():
            raise RuntimeError("CUDA provider is unavailable. Install a Jetson-compatible onnxruntime-gpu wheel.")
        providers.insert(0, "CUDAExecutionProvider")

    session_options = SessionOptions()
    session_options.intra_op_num_threads = 2
    session_options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    modules = ["detection", "recognition"] if recognize else ["detection"]
    analysis = FaceAnalysis(
        name=str(resolve_model_dir(model_name, recognize)),
        allowed_modules=modules,
        providers=providers,
        sess_options=session_options,
        static_shape_sessions=not use_cuda,
    )
    analysis.prepare(ctx_id=0 if use_cuda else -1, det_size=(det_size, det_size))

    if use_cuda:
        for task in modules:
            session = analysis.models[task].session
            active_providers = session.get_providers()
            if "CUDAExecutionProvider" not in active_providers:
                raise RuntimeError(f"CUDA could not initialize {task}. Active providers: {active_providers}")
            session.disable_fallback()
        # An installed CUDA provider can still lack executable kernels for this GPU.
        analysis.get(np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8))
    active_providers = analysis.det_model.session.get_providers()
    print(f"Face model: {model_name}", flush=True)
    print(f"Face detector providers: {', '.join(active_providers)}", flush=True)
    return analysis


def detection_rates(sensor_ids, counts, elapsed):
    return ", ".join(f"sensor {sensor}: {count / elapsed:.1f} FPS"
                     for sensor, count in zip(sensor_ids, counts))


def run_headless_faces(cameras, sensor_ids, detector, detect_fps, bus, report_people=None):
    interval = 1 / (detect_fps * len(cameras))
    last_seen = [None] * len(cameras)
    counts = [0] * len(cameras)
    sample_start = monotonic()

    while True:
        for index, (camera, sensor_id) in enumerate(zip(cameras, sensor_ids)):
            started = monotonic()
            check_pipeline_error(bus)
            frame = read_frame(camera, sensor_id, bus)
            annotations = detector(frame, sensor_id)
            if report_people is not None:
                report_people(annotations)
            labels = Counter(annotation_label(name, emotion) or "Face" for _, name, emotion, _ in annotations)
            if labels != last_seen[index]:
                summary = ", ".join(f"{name} x{count}" if count > 1 else name
                                    for name, count in sorted(labels.items()))
                print(f"{strftime('%H:%M:%S')} | sensor {sensor_id}: {summary or 'No faces'}", flush=True)
                last_seen[index] = labels
            counts[index] += 1

            now = monotonic()
            elapsed = now - sample_start
            if elapsed >= 5:
                print(f"Detection: {detection_rates(sensor_ids, counts, elapsed)}", flush=True)
                sample_start = now
                counts = [0] * len(cameras)

            remaining = interval - (now - started)
            if remaining > 0:
                sleep(remaining)


@contextmanager
def web_preview(port):
    updated = Condition()
    frame_number = 0
    jpeg = b""
    stopped = False
    next_frame = 0

    def publish(frame):
        nonlocal frame_number, jpeg, next_frame
        now = monotonic()
        if now < next_frame:
            return False
        next_frame = now + 0.1
        success, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
        if not success:
            raise RuntimeError("Could not encode the camera preview as JPEG")
        with updated:
            jpeg = encoded.tobytes()
            frame_number += 1
            updated.notify_all()
        return True

    class PreviewHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(PREVIEW_PAGE)))
                self.end_headers()
                self.wfile.write(PREVIEW_PAGE)
            elif self.path == "/stream.mjpg":
                self.stream_frames()
            elif self.path == "/frame.jpg":
                with updated:
                    frame = jpeg
                self.send_response(200 if frame else 503)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(frame)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with suppress(OSError):
                    self.wfile.write(frame)
            else:
                self.send_error(404)

        def log_request(self, code="-", size="-"):
            if self.path != "/frame.jpg":
                super().log_request(code, size)

        def stream_frames(self):
            self.connection.settimeout(5)
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            seen = 0
            try:
                while True:
                    with updated:
                        updated.wait_for(lambda: stopped or frame_number != seen)
                        if stopped:
                            return
                        seen, data = frame_number, jpeg
                    header = f"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(data)}\r\n\r\n"
                    self.wfile.write(header.encode("ascii"))
                    self.wfile.write(data)
                    self.wfile.write(b"\r\n")
            except OSError:
                # A viewer closed the tab or stopped reading the stream.
                return

    with ThreadingHTTPServer(("127.0.0.1", port), PreviewHandler) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield server.server_port, publish
        finally:
            with updated:
                stopped = True
                updated.notify_all()
            server.shutdown()
            worker.join()


def push_preview_frame(preview: GstApp.AppSrc, bus, frame):
    buffer = Gst.Buffer.new_wrapped(frame.tobytes())
    if preview.push_buffer(buffer) != Gst.FlowReturn.OK:
        check_pipeline_error(bus)
        raise RuntimeError("Could not display the annotated frame")
    return True


def show_live_faces(cameras, sensor_ids, detector, detect_fps, render_frame, bus, model_label, report_people=None):
    camera_count = len(cameras)
    detection_interval = 1 / (detect_fps * camera_count)
    label_font = cv2.FONT_HERSHEY_SIMPLEX
    label_scale = 0.6
    label_origin = (14, HEIGHT - 14)
    (label_width, label_height), label_baseline = cv2.getTextSize(model_label, label_font, label_scale, 1)
    label_top_left = (label_origin[0] - 6, label_origin[1] - label_height - 5)
    label_bottom_right = (label_origin[0] + label_width + 6, label_origin[1] + label_baseline + 4)
    annotations = [[] for _ in cameras]
    completed_detections = [0] * camera_count
    display_frames = 0
    sample_start = monotonic()
    next_detection = sample_start
    detection_camera = 0
    pending = None

    with ThreadPoolExecutor(max_workers=1) as worker:
        while True:
            check_pipeline_error(bus)
            frames = [read_frame(camera, sensor, bus) for camera, sensor in zip(cameras, sensor_ids)]
            now = monotonic()

            if pending is not None and pending.done():
                annotations[detection_camera] = pending.result()
                if report_people is not None:
                    report_people(annotations[detection_camera])
                completed_detections[detection_camera] += 1
                detection_camera = (detection_camera + 1) % camera_count
                pending = None

            if pending is None and now >= next_detection:
                pending = worker.submit(detector, frames[detection_camera], sensor_ids[detection_camera])
                next_detection = now + detection_interval

            # Render from the newest camera frames while inference runs on its own snapshot.
            frame = np.concatenate(frames, axis=1)
            for camera_index, camera_annotations in enumerate(annotations):
                offset = camera_index * WIDTH
                panel = frame[:, offset : offset + WIDTH]
                for (left, top, right, bottom), name, emotion, match in camera_annotations:
                    color = (0, 200, 255) if name == "Unknown" else (0, 255, 0)
                    cv2.rectangle(panel, (left, top), (right, bottom), color, 2)
                    label = annotation_label(name, emotion, match)
                    if label:
                        text_width = cv2.getTextSize(label, label_font, 0.55, 2)[0][0]
                        text_left = max(0, min(left, WIDTH - text_width - 2))
                        cv2.putText(panel, label, (text_left, max(18, top - 6)),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2, cv2.LINE_AA)

            cv2.rectangle(frame, label_top_left, label_bottom_right, (20, 20, 20), -1)
            cv2.putText(frame, model_label, label_origin, label_font, label_scale, (255, 255, 255), 1, cv2.LINE_AA)

            display_frames += int(render_frame(frame))

            elapsed = now - sample_start
            if elapsed >= 5:
                rates = detection_rates(sensor_ids, completed_detections, elapsed)
                print(f"Preview: {display_frames / elapsed:.1f} FPS | Detection: {rates}", flush=True)
                sample_start = now
                display_frames = 0
                completed_detections = [0] * camera_count


def main():
    parser = argparse.ArgumentParser(description="Detect faces in live Jetson CSI video")
    parser.add_argument("--sensor-id", type=int, choices=(0, 1), help="Preview only this sensor")
    parser.add_argument("--wbmode", type=int, choices=range(9), default=1)
    parser.add_argument("--model", choices=MODEL_DETECTORS, default="buffalo_l")
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--emotion-backend", choices=("hsemotion", "dan"), help="Enable an expression model")
    parser.add_argument("--emotion-model", type=Path, help="Override the expression ONNX file (defaults to DAN when used alone)")
    presentation = parser.add_mutually_exclusive_group()
    presentation.add_argument("--headless", action="store_true", help="Print face detections without opening a video window")
    presentation.add_argument("--web-port", type=int, help="Serve camera preview on the local interface at this port")
    parser.add_argument("--detect-fps", type=float, default=5, help="Detection FPS limit per camera")
    parser.add_argument("--det-size", type=int, choices=(320, 480, 640), default=320)
    references = parser.add_mutually_exclusive_group()
    references.add_argument("--reference-dir", type=Path, help="Folder with reference photos of one person")
    references.add_argument("--people-dir", type=Path, help="Gallery with one subfolder of photos per person")
    parser.add_argument("--name", default="Me", help="Name to show for a match")
    parser.add_argument("--match-threshold", type=float, default=0.50, help="Minimum cosine similarity")
    args = parser.parse_args()
    if not 0 < args.detect_fps <= 30:
        parser.error("--detect-fps must be greater than 0 and at most 30")
    if not 0 < args.match_threshold <= 1:
        parser.error("--match-threshold must be greater than 0 and at most 1")
    if args.web_port is not None and not 1 <= args.web_port <= 65535:
        parser.error("--web-port must be between 1 and 65535")
    expression_model = None
    if args.emotion_backend is not None or args.emotion_model is not None:
        from dan_emotions import EXPRESSION_MODELS, FaceExpressions

        expression_model = EXPRESSION_MODELS[args.emotion_backend or "dan"]
        if args.emotion_model is None:
            expression_path = Path(__file__).resolve().with_name(expression_model.filename)
        else:
            expression_path = args.emotion_model.expanduser()
        if not expression_path.is_file():
            parser.error(f"{expression_model.name} model file not found: {expression_path}")
    gallery_dir = args.people_dir or args.reference_dir
    if gallery_dir is not None:
        gallery_dir = gallery_dir.expanduser()
        if not gallery_dir.is_dir():
            parser.error(f"Reference directory not found: {gallery_dir}")
    if args.reference_dir is not None and not valid_display_name(args.name):
        parser.error("--name must contain printable ASCII text for the video overlay")
    sensor_ids = (0, 1) if args.sensor_id is None else (args.sensor_id,)

    analysis = load_face_detector(args.model, args.device, args.det_size, recognize=gallery_dir is not None)
    gallery = None
    report_people = None
    if args.people_dir is not None:
        gallery = load_people_gallery(analysis, gallery_dir, args.model, args.det_size)
        if getenv("SENTINAL_PERSON_EVENTS") == "1":
            report_people = person_reporter(gallery_dir, gallery[1] if gallery is not None else ())
    elif args.reference_dir is not None:
        gallery = load_reference_gallery(analysis, gallery_dir, args.name)
    expressions = None
    expression_name = "Off"
    if expression_model is not None:
        expressions = FaceExpressions(expression_model, expression_path, args.device)
        expression_name = expression_model.name
    model_label = f"Face: {args.model} | Expression: {expression_name}"
    print(model_label, flush=True)
    detector = partial(detect_faces, analysis, gallery=gallery, threshold=args.match_threshold, expressions=expressions)

    Gst.init(None)
    description = [camera_branch(sensor_id, args.wbmode) for sensor_id in sensor_ids]
    if not args.headless and args.web_port is None:
        description.append(preview_branch(WIDTH * len(sensor_ids)))
    pipeline = Gst.parse_launch(" ".join(description))
    bus = pipeline.get_bus()
    cameras = [pipeline.get_by_name(f"camera{sensor_id}") for sensor_id in sensor_ids]

    try:
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            check_pipeline_error(bus)
            raise RuntimeError("Could not start the camera pipeline")
        if args.headless:
            print("Headless face detection started. Press Ctrl+C to stop.", flush=True)
            run_headless_faces(cameras, sensor_ids, detector, args.detect_fps, bus, report_people)
        elif args.web_port is not None:
            with web_preview(args.web_port) as (port, render_frame):
                print(f"Web preview listening on http://127.0.0.1:{port}/. Press Ctrl+C to stop.", flush=True)
                show_live_faces(cameras, sensor_ids, detector, args.detect_fps, render_frame, bus, model_label, report_people)
        else:
            print("Live face detection started. Press Ctrl+C to stop.", flush=True)
            preview: GstApp.AppSrc = pipeline.get_by_name("preview")
            render_frame = partial(push_preview_frame, preview, bus)
            show_live_faces(cameras, sensor_ids, detector, args.detect_fps, render_frame, bus, model_label)
    except KeyboardInterrupt:
        pass
    finally:
        pipeline.set_state(Gst.State.NULL)


if __name__ == "__main__":
    main()
