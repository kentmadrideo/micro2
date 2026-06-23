#!/usr/bin/python3
import json
import io
import logging
import socketserver
import time
import os
from pathlib import Path
import mimetypes
from http import server
from threading import Condition, Lock, Thread

try:
    from picamera2 import Picamera2
    from picamera2.encoders import MJPEGEncoder
    from picamera2.outputs import FileOutput
    try:
        from libcamera import Transform, controls
    except ImportError:
        try:
            from picamera2 import Transform, controls
        except ImportError:
            class Transform:
                def __init__(self, **kwargs): pass
            controls = None
    PICAMERA2_AVAILABLE = True
except ImportError:
    PICAMERA2_AVAILABLE = False

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False

import threading

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False

# ==========================================
# CONFIGURATION
# ==========================================
PORT = 8000
CAMERA_INDEX = 0      # Used only if falling back to OpenCV/USB Webcam
ROTATION = 0          # Set to 180 if camera is mounted upside down
MOTION_CHECK_INTERVAL = 0.5
MOTION_MIN_AREA = 1200
MOTION_STILL_SECONDS = 20
SNAPSHOT_INTERVAL = 10
GALLERY_DIR = os.path.join(os.path.dirname(__file__), 'public', 'gallery')
MAX_SNAPSHOTS = 20
INDEX_FILENAME = 'index.json'
MOTION_BOX_PADDING = 12
MOTION_MAX_BOXES = 5
# Improved sensitivity settings
BACKGROUND_ALPHA = 0.02   # running average update speed (lower = slower adapt)
MOTION_THRESHOLD = 15     # pixel diff threshold (lower = more sensitive)
MIN_CONTOUR_AREA = 400    # smaller area to catch slow/smaller motion
# ==========================================

PAGE = """\
<html>
<head>
<title>Tank Monitor Camera Stream</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
body {
        margin: 0;
        font-family: Arial, sans-serif;
        background: #05070d;
        color: #e5e7eb;
}
.wrap {
        max-width: 980px;
        margin: 0 auto;
        padding: 16px;
}
.header {
        display: flex;
        justify-content: space-between;
        gap: 16px;
        align-items: center;
        flex-wrap: wrap;
        margin-bottom: 14px;
}
.title {
        margin: 0;
        font-size: 1.3rem;
}
.subtitle {
        margin: 4px 0 0;
        color: #9ca3af;
        font-size: 0.92rem;
}
.status-row {
        display: flex;
        align-items: center;
        gap: 10px;
        flex-wrap: wrap;
        margin-bottom: 12px;
}
.pill {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        border-radius: 999px;
        padding: 8px 12px;
        font-size: 0.84rem;
        border: 1px solid rgba(255,255,255,0.12);
        background: rgba(255,255,255,0.04);
}
.dot {
        width: 10px;
        height: 10px;
        border-radius: 50%;
        background: #6b7280;
}
.dot.active { background: #22c55e; }
.dot.quiet { background: #f59e0b; }
.dot.inactive { background: #ef4444; }
.dot.unavailable { background: #9ca3af; }
.note {
        color: #9ca3af;
        font-size: 0.82rem;
}
.viewer {
        position: relative;
        border-radius: 14px;
        overflow: hidden;
        border: 1px solid rgba(255,255,255,0.12);
        background: #000;
}
.viewer img {
        display: block;
        width: 100%;
        height: auto;
}
.overlay {
        position: absolute;
        left: 12px;
        right: 12px;
        bottom: 12px;
        display: flex;
        justify-content: space-between;
        gap: 12px;
        align-items: flex-end;
        flex-wrap: wrap;
}
.overlay-card {
        backdrop-filter: blur(12px);
        background: rgba(5, 7, 13, 0.7);
        border: 1px solid rgba(255,255,255,0.12);
        border-radius: 12px;
        padding: 10px 12px;
        max-width: 60%;
}
.overlay-status {
        font-weight: 700;
        margin-bottom: 4px;
}
.overlay-small {
        color: #cbd5e1;
        font-size: 0.84rem;
        line-height: 1.35;
}
.error-box {
        display: none;
        position: absolute;
        inset: 0;
        align-items: center;
        justify-content: center;
        flex-direction: column;
        text-align: center;
        gap: 10px;
        background: rgba(5, 7, 13, 0.9);
}
.button {
        border: 0;
        border-radius: 10px;
        padding: 10px 14px;
        background: #e5e7eb;
        color: #0f172a;
        font-weight: 700;
        cursor: pointer;
}
</style>
</head>
<body>
<div class="wrap">
<div class="header">
    <div>
        <h1 class="title">Tank Monitor Camera Stream</h1>
        <p class="subtitle">Live video with motion-aware status. Yellow boxes mark moving regions. This reports activity, not a guaranteed fish-identification result.</p>
    </div>
    <div class="pill" id="connectionPill"><span class="dot unavailable" id="statusDot"></span><span id="statusPillText">Checking view...</span></div>
</div>
<div class="status-row">
    <div class="note" id="statusLine">Waiting for the first frame.</div>
</div>
<div class="viewer">
    <img id="cameraFrame" src="stream" alt="Live aquarium feed" />
    <div class="overlay">
        <div class="overlay-card">
            <div class="overlay-status" id="overlayStatus">Warming up</div>
            <div class="overlay-small" id="overlayDetail">No frame analyzed yet.</div>
        </div>
        <div class="pill" id="overlayBadge"><span class="dot unavailable" id="overlayDot"></span><span id="overlayBadgeText">UNKNOWN</span></div>
    </div>
    <div class="error-box" id="cameraOffline">
        <div style="font-size: 2rem;">📹</div>
        <div style="font-weight: 700;">Camera stream unavailable</div>
        <div class="note">Check the camera service and try reconnecting.</div>
        <button class="button" onclick="retryStream()">Retry</button>
    </div>
</div>
</div>
<script>
const STREAM_URL = 'stream';
const STATUS_URL = 'status';
let pollTimer = null;

function setIndicator(state, title, detail) {
    const dot = document.getElementById('statusDot');
    const pillText = document.getElementById('statusPillText');
    const overlayDot = document.getElementById('overlayDot');
    const overlayBadgeText = document.getElementById('overlayBadgeText');
    const overlayStatus = document.getElementById('overlayStatus');
    const overlayDetail = document.getElementById('overlayDetail');
    const statusLine = document.getElementById('statusLine');

    const normalized = state || 'unavailable';
    dot.className = 'dot ' + normalized;
    overlayDot.className = 'dot ' + normalized;
    pillText.textContent = title;
    overlayBadgeText.textContent = normalized.toUpperCase();
    overlayStatus.textContent = title;
    overlayDetail.textContent = detail;
    statusLine.textContent = detail;
}

function renderStatus(payload) {
    const state = payload && payload.state ? payload.state : 'unknown';
    const label = payload && payload.activityLabel ? payload.activityLabel : 'Warming up';
    const seconds = payload && typeof payload.secondsSinceMotion === 'number' ? payload.secondsSinceMotion : null;
    const motionScore = payload && typeof payload.motionScore === 'number' ? payload.motionScore : null;

    let headline = label;
    let detail = 'No frame analyzed yet.';

    if (state === 'active') {
        headline = 'Movement detected in view';
        detail = 'Something in the tank is moving. Yellow boxes will mark the moving regions. This does not confirm a fish, but it shows visible activity.';
    } else if (state === 'quiet') {
        headline = 'Low movement in view';
        detail = 'The view is mostly still right now. Yellow boxes may appear around small motion regions. A fish may still be present, but it is not moving much.';
    } else if (state === 'inactive') {
        headline = 'No movement detected';
        detail = 'Nothing has moved for a while. That can mean no fish is visible, a hidden fish, or a motionless fish. No motion boxes are being drawn right now.';
    } else if (state === 'unavailable') {
        headline = 'Motion analysis unavailable';
        detail = 'The stream is live, but the analyzer could not read frames. Check camera dependencies on the host.';
    } else {
        headline = 'Warming up';
        detail = 'Waiting for enough frames to judge activity.';
    }

    if (seconds !== null && state !== 'unavailable') {
        detail += ' Last visible motion was ' + seconds.toFixed(1) + 's ago.';
    }
    if (motionScore !== null && state !== 'unavailable') {
        detail += ' Motion score: ' + motionScore.toFixed(0) + '.';
    }

    setIndicator(state, headline, detail);
}

async function pollStatus() {
    try {
        const response = await fetch(STATUS_URL + '?t=' + Date.now(), { cache: 'no-store' });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        const payload = await response.json();
        renderStatus(payload);
    } catch (error) {
        setIndicator('unavailable', 'Motion analysis unavailable', 'The status endpoint is not responding.');
        console.warn('[camera] status error:', error);
    }
}

function retryStream() {
    const frame = document.getElementById('cameraFrame');
    const offline = document.getElementById('cameraOffline');
    frame.src = STREAM_URL + '?t=' + Date.now();
    offline.style.display = 'none';
    pollStatus();
}

function onFrameError() {
    document.getElementById('cameraOffline').style.display = 'flex';
    setIndicator('unavailable', 'Camera stream offline', 'The camera image could not be loaded.');
}

document.getElementById('cameraFrame').addEventListener('error', onFrameError);
pollStatus();
pollTimer = setInterval(pollStatus, 4000);
</script>
</body>
</html>
"""


class MotionMonitor:
    def __init__(self):
        self.lock = Lock()
        self.prev_gray = None
        self.bg = None  # background model (float)
        self.last_motion_time = time.time()
        self.motion_score = 0.0
        self.motion_boxes = []
        self.state = 'unknown'
        self.last_update = time.time()

    def update(self, frame_bytes):
        if not CV2_AVAILABLE or not NUMPY_AVAILABLE:
            with self.lock:
                self.state = 'unavailable'
                self.motion_boxes = []
            return

        frame = cv2.imdecode(np.frombuffer(frame_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            return
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (21, 21), 0)

        with self.lock:
            # initialize background model if needed
            if self.bg is None:
                self.bg = gray.astype('float32')
                self.last_update = time.time()
                self.state = 'unknown'
                self.motion_boxes = []
                self.prev_gray = gray
                return

            # compute difference to background model (captures slow movement)
            bg_frame = cv2.convertScaleAbs(self.bg)
            delta = cv2.absdiff(bg_frame, gray)
            # also compute short-term diff (prev frame) to catch quick motion
            short_delta = cv2.absdiff(self.prev_gray, gray)
            # combine deltas to be sensitive to both slow and quick motion
            combined = cv2.max(delta, short_delta)
            # threshold and morph
            thresh = cv2.threshold(combined, MOTION_THRESHOLD, 255, cv2.THRESH_BINARY)[1]
            thresh = cv2.dilate(thresh, None, iterations=2)

            contours = cv2.findContours(thresh.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            contours = contours[0] if len(contours) == 2 else contours[1]

            boxes = []
            for contour in contours:
                if cv2.contourArea(contour) < MIN_CONTOUR_AREA:
                    continue
                x, y, w, h = cv2.boundingRect(contour)
                x = max(0, x - MOTION_BOX_PADDING)
                y = max(0, y - MOTION_BOX_PADDING)
                w = min(frame.shape[1] - x, w + (MOTION_BOX_PADDING * 2))
                h = min(frame.shape[0] - y, h + (MOTION_BOX_PADDING * 2))
                boxes.append((x, y, w, h))

            boxes.sort(key=lambda item: item[2] * item[3], reverse=True)
            boxes = boxes[:MOTION_MAX_BOXES]

            motion_score = float(cv2.countNonZero(thresh))
            self.motion_score = motion_score
            self.motion_boxes = boxes
            self.prev_gray = gray
            self.last_update = time.time()

            # update background slowly so slow motion isn't absorbed too fast
            cv2.accumulateWeighted(gray, self.bg, BACKGROUND_ALPHA)

            if motion_score >= MIN_CONTOUR_AREA:
                self.last_motion_time = time.time()
                self.state = 'active'
            elif (time.time() - self.last_motion_time) >= MOTION_STILL_SECONDS:
                self.state = 'inactive'
            else:
                self.state = 'quiet'

    def snapshot(self):
        with self.lock:
            now = time.time()
            return {
                'state': self.state,
                'motionScore': round(self.motion_score, 2),
                'motionBoxCount': len(self.motion_boxes),
                'secondsSinceMotion': round(now - self.last_motion_time, 1),
                'lastUpdateSecondsAgo': round(now - self.last_update, 1),
            }

    def get_boxes(self):
        with self.lock:
            return list(self.motion_boxes)

    def draw_overlay(self, frame):
        if not CV2_AVAILABLE:
            return frame

        boxes = self.get_boxes()
        if not boxes:
            return frame

        annotated = frame.copy()
        for x, y, w, h in boxes:
            cv2.rectangle(annotated, (x, y), (x + w, y + h), (0, 255, 255), 2)
            cv2.putText(annotated, 'movement', (x, max(18, y - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 2)
        cv2.putText(annotated, 'motion boxes shown', (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
        return annotated


class SnapshotSaver:
    def __init__(self, gallery_dir=GALLERY_DIR, interval=SNAPSHOT_INTERVAL, max_items=MAX_SNAPSHOTS):
        self.gallery_dir = Path(gallery_dir)
        self.interval = interval
        self.max_items = max_items
        self.running = False
        self.thread = None
        self.last_saved = None

        # ensure gallery dir exists
        try:
            self.gallery_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    def _list_files(self):
        items = sorted([p for p in self.gallery_dir.iterdir() if p.is_file() and p.suffix.lower() in ('.jpg', '.jpeg')], key=lambda p: p.stat().st_mtime)
        return items

    def _write_index(self, files):
        index = [f.name for f in files]
        try:
            with (self.gallery_dir / INDEX_FILENAME).open('w', encoding='utf-8') as fh:
                json.dump(index, fh)
        except Exception as e:
            logging.warning('Failed to write gallery index: %s', e)

    def _save_frame(self, frame_bytes):
        if not CV2_AVAILABLE or not NUMPY_AVAILABLE:
            return None
        try:
            frame = cv2.imdecode(np.frombuffer(frame_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                return None
            # annotate overlay boxes if any
            frame = MOTION_MONITOR.draw_overlay(frame)
            ok, enc = cv2.imencode('.jpg', frame)
            if not ok:
                return None
            data = enc.tobytes()
        except Exception:
            return None

        fname = f"snap_{int(time.time())}.jpg"
        path = self.gallery_dir / fname
        try:
            with path.open('wb') as fh:
                fh.write(data)
            return path
        except Exception as e:
            logging.warning('Failed to save snapshot: %s', e)
            return None

    def _prune(self):
        files = self._list_files()
        if len(files) <= self.max_items:
            return files
        # remove oldest until at max_items
        to_remove = files[:len(files) - self.max_items]
        for p in to_remove:
            try:
                p.unlink()
            except Exception:
                pass
        return self._list_files()

    def _get_latest_frame_bytes(self):
        # Try Picamera output first
        if 'output' in globals() and hasattr(output, 'frame') and output.frame is not None:
            return output.frame
        # Fallback to camera_buffer if present
        if 'camera_buffer' in globals() and camera_buffer is not None:
            return camera_buffer.get_frame()
        return None

    def _run(self):
        while self.running:
            try:
                frame = self._get_latest_frame_bytes()
                if frame:
                    saved = self._save_frame(frame)
                    if saved:
                        files = self._prune()
                        self._write_index(files)
                time.sleep(self.interval)
            except Exception as e:
                logging.warning('Snapshot saver error: %s', e)
                time.sleep(self.interval)

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=1)


SNAPSHOT_SAVER = SnapshotSaver()


MOTION_MONITOR = MotionMonitor()


def add_cors_headers(handler):
    handler.send_header('Access-Control-Allow-Origin', '*')
    handler.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
    handler.send_header('Access-Control-Allow-Headers', 'Content-Type')


def build_status_payload():
    payload = MOTION_MONITOR.snapshot()
    payload['activityLabel'] = {
        'active': 'Movement detected',
        'quiet': 'Low movement',
        'inactive': 'No movement for a while',
        'unknown': 'Warming up',
        'unavailable': 'Motion analysis unavailable',
    }.get(payload['state'], 'Unknown')
    payload['possibleAlert'] = payload['state'] == 'inactive'
    return payload

if PICAMERA2_AVAILABLE:
    class StreamingOutput(io.BufferedIOBase):
        def __init__(self):
            self.frame = None
            self.condition = Condition()

        def write(self, buf):
            MOTION_MONITOR.update(buf)
            with self.condition:
                if CV2_AVAILABLE and NUMPY_AVAILABLE:
                    frame = cv2.imdecode(np.frombuffer(buf, dtype=np.uint8), cv2.IMREAD_COLOR)
                    if frame is not None:
                        frame = MOTION_MONITOR.draw_overlay(frame)
                        ok, encoded = cv2.imencode('.jpg', frame)
                        if ok:
                            buf = encoded.tobytes()
                self.frame = buf
                self.condition.notify_all()

    output = StreamingOutput()

    class StreamingHandler(server.BaseHTTPRequestHandler):
        def do_OPTIONS(self):
            self.send_response(204)
            add_cors_headers(self)
            self.end_headers()

        def do_GET(self):
            path = self.path.split('?')[0]
            # Serve gallery from camera server as well
            if path.startswith('/gallery'):
                rel = path[len('/gallery'):]
                if rel == '' or rel == '/':
                    rel = '/index.json'
                gallery_root = Path(GALLERY_DIR).resolve()
                target = (gallery_root / rel.lstrip('/')).resolve()
                if not str(target).startswith(str(gallery_root)):
                    self.send_response(403)
                    self.end_headers()
                    return
                if not target.exists() or not target.is_file():
                    self.send_response(404)
                    self.end_headers()
                    return
                ctype, _ = mimetypes.guess_type(str(target))
                if not ctype:
                    ctype = 'application/octet-stream'
                try:
                    with target.open('rb') as fh:
                        data = fh.read()
                    self.send_response(200)
                    add_cors_headers(self)
                    self.send_header('Content-Type', ctype)
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except Exception as e:
                    logging.warning('Gallery serve error: %s', e)
                    self.send_response(500)
                    self.end_headers()
                return
                # Serve gallery files from camera server: /gallery/index.json and images
                if path.startswith('/gallery'):
                    rel = path[len('/gallery'):]
                    if rel == '' or rel == '/':
                        rel = '/index.json'
                    # Resolve and secure path
                    gallery_root = Path(GALLERY_DIR).resolve()
                    target = (gallery_root / rel.lstrip('/')).resolve()
                    if not str(target).startswith(str(gallery_root)):
                        self.send_response(403)
                        self.end_headers()
                        return
                    if not target.exists() or not target.is_file():
                        self.send_response(404)
                        self.end_headers()
                        return
                    ctype, _ = mimetypes.guess_type(str(target))
                    if not ctype:
                        ctype = 'application/octet-stream'
                    try:
                        with target.open('rb') as fh:
                            data = fh.read()
                        self.send_response(200)
                        add_cors_headers(self)
                        self.send_header('Content-Type', ctype)
                        self.send_header('Content-Length', str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                    except Exception as e:
                        logging.warning('Gallery serve error: %s', e)
                        self.send_response(500)
                        self.end_headers()
                    return
            if path == '/' or path == '/index.html':
                content = PAGE.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', len(content))
                self.end_headers()
                self.wfile.write(content)
            elif path == '/status':
                content = json.dumps(build_status_payload()).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                add_cors_headers(self)
                self.send_header('Cache-Control', 'no-cache, private')
                self.send_header('Content-Length', len(content))
                self.end_headers()
                self.wfile.write(content)
            elif path == '/stream' or path == '/stream.mjpg':
                self.send_response(200)
                self.send_header('Age', '0')
                self.send_header('Cache-Control', 'no-cache, private')
                self.send_header('Pragma', 'no-cache')
                self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=FRAME')
                self.end_headers()
                try:
                    while True:
                        with output.condition:
                            output.condition.wait()
                            frame = output.frame
                        self.wfile.write(b'--FRAME\r\n')
                        self.send_header('Content-Type', 'image/jpeg')
                        self.send_header('Content-Length', len(frame))
                        self.end_headers()
                        self.wfile.write(frame)
                        self.wfile.write(b'\r\n')
                except Exception as e:
                    logging.warning(
                        'Removed streaming client %s: %s',
                        self.client_address, str(e))
            else:
                self.send_response(404)
                self.end_headers()

    class ThreadedHTTPServer(socketserver.ThreadingMixIn, server.HTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    def main():
        print("Initializing native Picamera2 stream...")
        picam2 = Picamera2()
        
        # Setup rotation transform
        transform = Transform()
        if ROTATION == 180:
            transform = Transform(hflip=True, vflip=True)
        elif ROTATION != 0:
            logging.warning('Rotation %s is not supported directly by libcamera/picamera2. Only 180 degrees is supported via transform.', ROTATION)
            
        config = picam2.create_video_configuration(main={"size": (640, 480)}, transform=transform)
        picam2.configure(config)
        
        # Start the camera so properties are loaded and controls can be set
        picam2.start()

        # Keep the crop close to the full sensor area so the view is slightly wider.
        # This uses the full sensor area, which is the widest software view available.
        # Calculate aspect-ratio-matched ScalerCrop to avoid distortion and invalid ranges on Camera Module v3 (16:9 sensor)
        try:
            sensor_w, sensor_h = picam2.camera_properties["PixelArraySize"]
            stream_w, stream_h = 640, 480
            sensor_aspect = sensor_w / sensor_h
            stream_aspect = stream_w / stream_h
            
            if sensor_aspect > stream_aspect:
                crop_h = sensor_h
                crop_w = int(sensor_h * stream_aspect)
                crop_x = (sensor_w - crop_w) // 2
                crop_y = 0
            else:
                crop_w = sensor_w
                crop_h = int(sensor_w / stream_aspect)
                crop_x = 0
                crop_y = (sensor_h - crop_h) // 2
                
            picam2.set_controls({"ScalerCrop": (crop_x, crop_y, crop_w, crop_h)})
            print(f"Applied ScalerCrop: {crop_x, crop_y, crop_w, crop_h} matching {stream_w}x{stream_h} aspect ratio.")
        except Exception as e:
            logging.warning('Failed to apply zoom-out crop: %s', e)
        
        # Enable Continuous Autofocus if supported (Camera Module v3)
        try:
            if controls is not None and "AfMode" in picam2.camera_controls:
                picam2.set_controls({"AfMode": controls.AfModeEnum.Continuous})
                print("Continuous autofocus enabled.")
        except Exception as e:
            logging.warning('Failed to enable continuous autofocus: %s', e)
            
        picam2.start_recording(MJPEGEncoder(), FileOutput(output))
        # Start periodic snapshot saver
        try:
            SNAPSHOT_SAVER.start()
        except Exception:
            logging.warning('Failed to start snapshot saver')
        
        try:
            address = ('0.0.0.0', PORT)
            server_inst = ThreadedHTTPServer(address, StreamingHandler)
            print("==========================================")
            print("  Pi Camera Native Picamera2 Streamer")
            print("==========================================")
            print(f"  Status:       RUNNING")
            print(f"  Port:         {PORT}")
            print(f"  Stream URL:   http://<Pi-IP>:{PORT}/stream")
            print("==========================================")
            print("Press Ctrl+C to stop the server")
            server_inst.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down server...")
        finally:
            picam2.stop_recording()
            picam2.stop()
            picam2.close()
            try:
                SNAPSHOT_SAVER.stop()
            except Exception:
                pass

else:
    # Fallback to OpenCV (useful if testing on PC or running a USB webcam on the Pi)
    class CameraBuffer:
        def __init__(self):
            self.camera = None
            self.latest_frame = None
            self.lock = threading.Lock()
            self.running = False
            self.thread = None
            
        def start(self):
            if not CV2_AVAILABLE:
                print("OpenCV (cv2) is not available. Camera fallback cannot capture frames.")
                return
            self.running = True
            # For fallback, try native CAP_V4L2
            self.camera = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_V4L2)
            self.camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            self.camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            self.thread = threading.Thread(target=self._capture_loop, daemon=True)
            self.thread.start()
            
        def _capture_loop(self):
            while self.running:
                success, frame = self.camera.read()
                if success:
                    frame = MOTION_MONITOR.draw_overlay(frame)
                    ret, jpeg = cv2.imencode('.jpg', frame)
                    if ret:
                        MOTION_MONITOR.update(jpeg.tobytes())
                        with self.lock:
                            self.latest_frame = jpeg.tobytes()
                else:
                    print("Warning: Failed to capture frame from camera. Reconnecting in 2s...")
                    time.sleep(2)
                    with self.lock:
                        if self.camera:
                            self.camera.release()
                        self.camera = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_V4L2)
                        self.camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                        self.camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                time.sleep(0.04)
                
        def get_frame(self):
            with self.lock:
                return self.latest_frame
                
        def stop(self):
            self.running = False
            if CV2_AVAILABLE and self.camera:
                self.camera.release()

    camera_buffer = CameraBuffer()

    class StreamingHandler(server.BaseHTTPRequestHandler):
        def do_OPTIONS(self):
            self.send_response(204)
            add_cors_headers(self)
            self.end_headers()

        def do_GET(self):
            path = self.path.split('?')[0]
            if path == '/' or path == '/index.html':
                content = PAGE.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', len(content))
                self.end_headers()
                self.wfile.write(content)
            elif path == '/status':
                content = json.dumps(build_status_payload()).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                add_cors_headers(self)
                self.send_header('Cache-Control', 'no-cache, private')
                self.send_header('Content-Length', len(content))
                self.end_headers()
                self.wfile.write(content)
            elif path == '/stream' or path == '/stream.mjpg':
                self.send_response(200)
                self.send_header('Age', '0')
                self.send_header('Cache-Control', 'no-cache, private')
                self.send_header('Pragma', 'no-cache')
                self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=FRAME')
                self.end_headers()
                try:
                    last_frame = None
                    while True:
                        frame = camera_buffer.get_frame()
                        if frame is None:
                            time.sleep(0.1)
                            continue
                        if frame != last_frame:
                            self.wfile.write(b'--FRAME\r\n')
                            self.send_header('Content-Type', 'image/jpeg')
                            self.send_header('Content-Length', str(len(frame)))
                            self.end_headers()
                            self.wfile.write(frame)
                            self.wfile.write(b'\r\n')
                            last_frame = frame
                        time.sleep(0.04)
                except Exception as e:
                    pass
            else:
                self.send_response(404)
                self.end_headers()

    class ThreadedHTTPServer(socketserver.ThreadingMixIn, server.HTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    def main():
        camera_buffer.start()
        try:
            SNAPSHOT_SAVER.start()
        except Exception:
            logging.warning('Failed to start snapshot saver')
        try:
            server_inst = ThreadedHTTPServer(('0.0.0.0', PORT), StreamingHandler)
            print("==========================================")
            print("  Camera HTTP MJPEG Streaming Server (OpenCV Fallback)")
            print("==========================================")
            print(f"  Status:       RUNNING")
            print(f"  Port:         {PORT}")
            print(f"  Stream URL:   http://<Pi-IP>:{PORT}/stream")
            print("==========================================")
            print("Press Ctrl+C to stop the server")
            server_inst.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down server...")
            camera_buffer.stop()
            try:
                SNAPSHOT_SAVER.stop()
            except Exception:
                pass

if __name__ == '__main__':
    main()
