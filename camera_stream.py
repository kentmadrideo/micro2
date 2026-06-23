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

# Suppress libcamera warning logs (e.g., "PDAF data in unsupported format" warnings)
os.environ["LIBCAMERA_LOG_LEVELS"] = "ERROR"

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
    print("WARNING: OpenCV (cv2) is not installed! Motion detection and overlay drawing will be disabled.")

import threading

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False
    print("WARNING: NumPy is not installed! Motion detection will be disabled.")

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
# Motion detection tuning
MOG2_HISTORY = 300        # frames of history for MOG2 background model
MOG2_VAR_THRESHOLD = 40   # variance threshold for foreground classification
MOG2_LEARNING_RATE = 0.002  # very slow learning so moving fish aren't absorbed
MIN_CONTOUR_AREA = 800    # minimum contour area to count as motion (in analysis resolution)
WARMUP_FRAMES = 40        # frames to let background model stabilize before detecting
MAX_CONTOUR_ASPECT = 5.0  # reject contours with aspect ratio above this (edge artifacts)
CONSECUTIVE_FRAMES_REQUIRED = 2  # require motion in N consecutive frames before showing boxes
ANALYSIS_RESOLUTION = (320, 240)  # lower resolution for faster, less noisy analysis
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
    """Motion detector using OpenCV MOG2 background subtractor on raw frames.

    Key design decisions:
    - Uses MOG2 (Mixture of Gaussians) which statistically models each pixel
      as a mixture of distributions, handling sensor noise and water shimmer.
    - Analyzes RAW numpy frames (not JPEG-decoded) to avoid compression artifact
      noise that caused false detections on static areas.
    - Requires motion in CONSECUTIVE_FRAMES_REQUIRED consecutive analysis cycles
      before showing boxes (temporal consistency), eliminating transient false positives.
    - Scales box coordinates from analysis resolution to main stream resolution
      so overlays align correctly on the full-size video.
    - Background model only learns when no motion is detected, preventing moving
      objects from being absorbed (which caused ghost boxes at old positions).
    """

    def __init__(self):
        self.lock = Lock()
        self.mog2 = None          # MOG2 background subtractor
        self.warmup_count = 0     # frames processed during warmup
        self.last_motion_time = time.time()
        self.motion_score = 0.0
        self.motion_boxes = []    # boxes in MAIN stream coordinates
        self.state = 'unknown'
        self.last_update = time.time()
        # Temporal consistency: require consecutive detections
        self.consecutive_detections = 0
        self.pending_boxes = []   # candidate boxes awaiting confirmation
        # Coordinate scaling (set when analysis resolution differs from stream)
        self.scale_x = 1.0
        self.scale_y = 1.0

    def _init_mog2(self):
        """Initialize the MOG2 background subtractor."""
        if CV2_AVAILABLE:
            self.mog2 = cv2.createBackgroundSubtractorMOG2(
                history=MOG2_HISTORY,
                varThreshold=MOG2_VAR_THRESHOLD,
                detectShadows=False  # no shadow detection needed for aquarium
            )
            # Reduce the number of Gaussian components to be more selective
            self.mog2.setNMixtures(3)

    def update_raw(self, frame, main_size=None):
        """Process a raw numpy frame (BGR or grayscale, NOT JPEG compressed).

        Args:
            frame: numpy array from camera (raw, no JPEG encoding).
            main_size: (width, height) of the main stream for coordinate scaling.
                       If None, no scaling is applied.
        """
        if not CV2_AVAILABLE or not NUMPY_AVAILABLE:
            with self.lock:
                self.state = 'unavailable'
                self.motion_boxes = []
            return

        now = time.time()
        with self.lock:
            if now - self.last_update < MOTION_CHECK_INTERVAL and self.mog2 is not None:
                return

        if frame is None:
            return

        # Convert to grayscale depending on input format
        if len(frame.shape) == 3:
            if frame.shape[2] == 4:  # XBGR8888 / BGRA
                gray = cv2.cvtColor(frame, cv2.COLOR_BGRA2GRAY)
            else:  # BGR / RGB (3-channel)
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        elif len(frame.shape) == 2:
            # Already grayscale or Y-channel from YUV
            gray = frame
        else:
            return

        gray = cv2.GaussianBlur(gray, (21, 21), 0)

        with self.lock:
            # Compute coordinate scaling from analysis resolution to main stream
            if main_size is not None:
                self.scale_x = main_size[0] / gray.shape[1]
                self.scale_y = main_size[1] / gray.shape[0]

            # Initialize MOG2 on first frame
            if self.mog2 is None:
                self._init_mog2()

            # --- Warmup phase: train the background model without detecting ---
            if self.warmup_count < WARMUP_FRAMES:
                # Fast learning rate during warmup to converge quickly
                self.mog2.apply(gray, learningRate=0.15)
                self.warmup_count += 1
                self.last_update = now
                self.state = 'unknown'
                self.motion_boxes = []
                return

            # --- Detection phase: MOG2 background subtraction ---
            # Use a very slow learning rate so moving fish are NOT absorbed
            # into the background model. The model only adapts to gradual
            # lighting changes over many seconds.
            fg_mask = self.mog2.apply(gray, learningRate=0.0)  # no learning during detection

            # Clean up the foreground mask
            # Elliptical kernel handles natural shapes better than rectangular
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
            fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_OPEN, kernel)
            fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_CLOSE, kernel)
            fg_mask = cv2.dilate(fg_mask, None, iterations=2)

            contours = cv2.findContours(fg_mask.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            contours = contours[0] if len(contours) == 2 else contours[1]

            # Filter contours by area and aspect ratio
            analysis_area = gray.shape[0] * gray.shape[1]
            max_contour_area = analysis_area * 0.40

            raw_boxes = []
            for contour in contours:
                area = cv2.contourArea(contour)
                if area < MIN_CONTOUR_AREA or area > max_contour_area:
                    continue
                x, y, w, h = cv2.boundingRect(contour)
                # Reject very elongated shapes (edge/line artifacts)
                aspect = max(w, h) / max(min(w, h), 1)
                if aspect > MAX_CONTOUR_ASPECT:
                    continue
                # Scale coordinates to main stream resolution and add padding
                sx = int(x * self.scale_x)
                sy = int(y * self.scale_y)
                sw = int(w * self.scale_x)
                sh = int(h * self.scale_y)
                sx = max(0, sx - MOTION_BOX_PADDING)
                sy = max(0, sy - MOTION_BOX_PADDING)
                sw = sw + (MOTION_BOX_PADDING * 2)
                sh = sh + (MOTION_BOX_PADDING * 2)
                raw_boxes.append((sx, sy, sw, sh))

            raw_boxes.sort(key=lambda b: b[2] * b[3], reverse=True)
            raw_boxes = raw_boxes[:MOTION_MAX_BOXES]

            motion_score = float(cv2.countNonZero(fg_mask))
            has_candidates = len(raw_boxes) > 0

            # --- Temporal consistency: require consecutive detections ---
            # This eliminates transient false positives from noise spikes,
            # autofocus adjustments, or momentary reflections.
            if has_candidates:
                self.consecutive_detections += 1
                self.pending_boxes = raw_boxes
            else:
                self.consecutive_detections = 0
                self.pending_boxes = []

            if self.consecutive_detections >= CONSECUTIVE_FRAMES_REQUIRED:
                self.motion_boxes = self.pending_boxes
                has_motion = True
            else:
                self.motion_boxes = []
                has_motion = False

            self.motion_score = motion_score
            self.last_update = time.time()

            # --- Background update: ONLY when no motion is detected ---
            # When the scene is still, let MOG2 slowly adapt to lighting changes.
            # During motion, freeze the background so the moving object isn't absorbed.
            if not has_motion:
                self.mog2.apply(gray, learningRate=MOG2_LEARNING_RATE)

            if has_motion:
                self.last_motion_time = time.time()
                self.state = 'active'
            elif (time.time() - self.last_motion_time) >= MOTION_STILL_SECONDS:
                self.state = 'inactive'
            else:
                self.state = 'quiet'

    def update(self, frame_bytes):
        """Fallback: process a JPEG-compressed frame (less accurate than update_raw)."""
        if not CV2_AVAILABLE or not NUMPY_AVAILABLE:
            with self.lock:
                self.state = 'unavailable'
                self.motion_boxes = []
            return
        try:
            frame = cv2.imdecode(np.frombuffer(frame_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
            if frame is not None:
                self.update_raw(frame)
        except Exception:
            pass

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
            # Do NOT run motion analysis on JPEG bytes here.
            # Motion analysis is done on raw lores frames in a separate thread.
            with self.condition:
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
                            raw_frame = output.frame
                        frame = raw_frame
                        if CV2_AVAILABLE and NUMPY_AVAILABLE:
                            try:
                                img = cv2.imdecode(np.frombuffer(raw_frame, dtype=np.uint8), cv2.IMREAD_COLOR)
                                if img is not None:
                                    img = MOTION_MONITOR.draw_overlay(img)
                                    ok, encoded = cv2.imencode('.jpg', img)
                                    if ok:
                                        frame = encoded.tobytes()
                            except Exception:
                                pass
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
            
        # Configure DUAL streams:
        #   main  = 640x480 for MJPEG encoding (sent to viewers)
        #   lores = 320x240 for motion analysis (raw numpy, no JPEG compression)
        # This eliminates JPEG artifact noise from motion detection entirely.
        main_size = (640, 480)
        lores_size = ANALYSIS_RESOLUTION
        config = picam2.create_video_configuration(
            main={"size": main_size},
            lores={"size": lores_size, "format": "YUV420"},
            transform=transform,
            buffer_count=4
        )
        picam2.configure(config)
        
        # Start the camera so properties are loaded and controls can be set
        picam2.start()

        # Set the framerate to 30 FPS for smoother video
        try:
            picam2.set_controls({"FrameRate": 30})
            print("Camera framerate set to 30 FPS.")
        except Exception as e:
            logging.warning('Failed to set FrameRate control: %s', e)

        # Keep the crop close to the full sensor area so the view is slightly wider.
        # Calculate aspect-ratio-matched ScalerCrop to avoid distortion and invalid ranges on Camera Module v3 (16:9 sensor)
        try:
            sensor_w, sensor_h = picam2.camera_properties["PixelArraySize"]
            stream_w, stream_h = main_size
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

        # --- Start raw-frame motion analysis thread ---
        # This thread grabs uncompressed frames from the lores stream for
        # motion detection, completely bypassing JPEG compression artifacts.
        analysis_running = True
        def _motion_analysis_loop():
            lores_h, lores_w = lores_size[1], lores_size[0]
            while analysis_running:
                try:
                    # capture_array("lores") returns raw YUV420 numpy array.
                    # Shape is (height * 1.5, width) — the Y channel (top rows) is grayscale.
                    yuv = picam2.capture_array("lores")
                    if yuv is not None:
                        # Extract Y channel (grayscale) from YUV420
                        gray = yuv[:lores_h, :lores_w]
                        MOTION_MONITOR.update_raw(gray, main_size=main_size)
                except Exception as e:
                    logging.warning('Motion analysis error: %s', e)
                time.sleep(MOTION_CHECK_INTERVAL)

        analysis_thread = Thread(target=_motion_analysis_loop, daemon=True)
        analysis_thread.start()
        print(f"Motion analysis thread started (raw {lores_size[0]}x{lores_size[1]} frames, no JPEG).")

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
            analysis_running = False
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
            self.latest_annotated_frame = None
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
                    # Pass RAW BGR frame directly to motion monitor (no JPEG encoding)
                    MOTION_MONITOR.update_raw(frame)
                    
                    # Encode clean frame for storage
                    ret, jpeg = cv2.imencode('.jpg', frame)
                    if ret:
                        jpeg_bytes = jpeg.tobytes()
                        
                        # Draw overlay on a copy of the frame for the stream
                        annotated_frame = MOTION_MONITOR.draw_overlay(frame)
                        ret_ann, jpeg_ann = cv2.imencode('.jpg', annotated_frame)
                        
                        with self.lock:
                            self.latest_frame = jpeg_bytes
                            if ret_ann:
                                self.latest_annotated_frame = jpeg_ann.tobytes()
                            else:
                                self.latest_annotated_frame = jpeg_bytes
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

        def get_annotated_frame(self):
            with self.lock:
                return self.latest_annotated_frame
                
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
                        frame = camera_buffer.get_annotated_frame()
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
