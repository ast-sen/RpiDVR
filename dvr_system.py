#!/usr/bin/env python3
"""
Raspberry Pi DVR System with Battery Monitoring and Web Authentication
Requires: Flask, OpenCV, Flask-HTTPAuth, INA219
Install: pip3 install flask opencv-python flask-httpauth pi-ina219
"""

from flask import Flask, render_template_string, Response, jsonify, request
from flask_httpauth import HTTPBasicAuth
from werkzeug.security import generate_password_hash, check_password_hash
import cv2
import threading
import time
from datetime import datetime
import os
import sys

# For INA219 current/voltage sensor
try:
    from ina219 import INA219
    from ina219 import DeviceRangeError
    INA219_AVAILABLE = True
except ImportError:
    print("Warning: INA219 library not found. Install with: pip3 install pi-ina219")
    INA219_AVAILABLE = False

app = Flask(__name__)
auth = HTTPBasicAuth()

# ============================================================================
# CONFIGURATION SECTION - EDIT THESE VALUES
# ============================================================================

# Web Authentication (CHANGE THESE!)
users = {
    "admin": generate_password_hash("raspberry"),  # Change this password!
    "viewer": generate_password_hash("viewer123")   # Additional user (optional)
}

# Storage Configuration
SSD_MOUNT_PATH = "/mnt/ssd/recordings"  # Path to SSD storage
ENABLE_LOCAL_BACKUP = False  # Set True to also save to SD card
LOCAL_BACKUP_PATH = "/home/pi/recordings"

# Camera Configuration
CAMERA_INDEX = 0  # 0 for first USB camera, or use /dev/video0
CAMERA_WIDTH = 640
CAMERA_HEIGHT = 480
CAMERA_FPS = 30

# INA219 Sensor Configuration
SHUNT_OHMS = 0.1
MAX_EXPECTED_AMPS = 3.0

# Recording Configuration
VIDEO_CODEC = 'XVID'  # or 'MJPG', 'mp4v'
RECORD_FPS = 20.0

# Server Configuration
SERVER_HOST = '0.0.0.0'  # Listen on all interfaces
SERVER_PORT = 5000
DEBUG_MODE = False

# ============================================================================
# GLOBAL VARIABLES
# ============================================================================

camera = None
camera_lock = threading.Lock()
recording = False
video_writer = None
recording_filename = None
current_voltage = 0.0
current_current = 0.0
current_power = 0.0
system_start_time = time.time()

# ============================================================================
# AUTHENTICATION
# ============================================================================

@auth.verify_password
def verify_password(username, password):
    """Verify user credentials"""
    if username in users and check_password_hash(users.get(username), password):
        return username
    return None

@auth.error_handler
def auth_error(status):
    """Custom authentication error message"""
    return jsonify({'error': 'Authentication required', 'message': 'Please provide valid credentials'}), 401

# ============================================================================
# STORAGE MANAGEMENT
# ============================================================================

def initialize_storage():
    """Create storage directories if they don't exist"""
    try:
        os.makedirs(SSD_MOUNT_PATH, exist_ok=True)
        print(f"✓ SSD storage initialized: {SSD_MOUNT_PATH}")
        
        if ENABLE_LOCAL_BACKUP:
            os.makedirs(LOCAL_BACKUP_PATH, exist_ok=True)
            print(f"✓ Local backup initialized: {LOCAL_BACKUP_PATH}")
        
        return True
    except Exception as e:
        print(f"✗ Error initializing storage: {e}")
        return False

def get_ssd_info():
    """Get SSD storage information"""
    try:
        stat = os.statvfs(SSD_MOUNT_PATH)
        # Calculate storage in GB
        total = (stat.f_blocks * stat.f_frsize) / (1024**3)
        free = (stat.f_bavail * stat.f_frsize) / (1024**3)
        used = total - free
        percent_used = (used / total) * 100 if total > 0 else 0
        
        return {
            'total': round(total, 2),
            'used': round(used, 2),
            'free': round(free, 2),
            'percent': round(percent_used, 2),
            'mounted': True
        }
    except Exception as e:
        print(f"Error getting SSD info: {e}")
        return {
            'total': 0,
            'used': 0,
            'free': 0,
            'percent': 0,
            'mounted': False
        }

def get_recording_list():
    """Get list of recorded files"""
    try:
        files = []
        for filename in os.listdir(SSD_MOUNT_PATH):
            if filename.endswith('.avi') or filename.endswith('.mp4'):
                filepath = os.path.join(SSD_MOUNT_PATH, filename)
                size = os.path.getsize(filepath) / (1024**2)  # MB
                mtime = os.path.getmtime(filepath)
                files.append({
                    'name': filename,
                    'size': round(size, 2),
                    'date': datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')
                })
        return sorted(files, key=lambda x: x['date'], reverse=True)
    except Exception as e:
        print(f"Error listing recordings: {e}")
        return []

# ============================================================================
# BATTERY MONITORING
# ============================================================================

def read_battery_data():
    """Read voltage and current from INA219 sensor continuously"""
    global current_voltage, current_current, current_power
    
    if not INA219_AVAILABLE:
        print("⚠ INA219 not available - using simulated data")
        # Simulate data if sensor not available
        while True:
            current_voltage = 12.0 + (time.time() % 10) / 10
            current_current = 0.5 + (time.time() % 5) / 10
            current_power = current_voltage * current_current
            time.sleep(1)
        return
    
    try:
        ina = INA219(SHUNT_OHMS, MAX_EXPECTED_AMPS)
        ina.configure(ina.RANGE_16V)
        print("✓ INA219 sensor initialized")
        
        while True:
            try:
                current_voltage = ina.voltage()
                current_current = ina.current() / 1000.0  # Convert mA to A
                current_power = ina.power() / 1000.0  # Convert mW to W
            except DeviceRangeError as e:
                print(f"⚠ Current overflow: {e}")
            except Exception as e:
                print(f"⚠ Sensor read error: {e}")
            
            time.sleep(1)
    except Exception as e:
        print(f"✗ Error initializing INA219: {e}")
        print("⚠ Falling back to simulated data")
        # Fallback to simulated data
        while True:
            current_voltage = 12.0 + (time.time() % 10) / 10
            current_current = 0.5 + (time.time() % 5) / 10
            current_power = current_voltage * current_current
            time.sleep(1)

# ============================================================================
# CAMERA STREAMING
# ============================================================================

class CameraStream:
    def __init__(self, camera_index=0):
        self.camera = cv2.VideoCapture(camera_index)
        if not self.camera.isOpened():
            raise RuntimeError(f"Cannot open camera {camera_index}")
        
        self.camera.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
        self.camera.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
        self.camera.set(cv2.CAP_PROP_FPS, CAMERA_FPS)
        
        # Verify camera settings
        actual_width = self.camera.get(cv2.CAP_PROP_FRAME_WIDTH)
        actual_height = self.camera.get(cv2.CAP_PROP_FRAME_HEIGHT)
        print(f"✓ Camera initialized: {int(actual_width)}x{int(actual_height)}")
        
    def __del__(self):
        if self.camera:
            self.camera.release()
    
    def get_frame(self):
        success, frame = self.camera.read()
        if not success:
            return None
        
        # Add timestamp overlay
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cv2.putText(frame, timestamp, (10, 30), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        
        # Add battery info overlay
        battery_text = f"Battery: {current_voltage:.2f}V | {current_current:.3f}A | {current_power:.2f}W"
        cv2.putText(frame, battery_text, (10, 60), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 2)
        
        # Add recording indicator
        if recording:
            cv2.circle(frame, (CAMERA_WIDTH - 30, 30), 10, (0, 0, 255), -1)
            cv2.putText(frame, "REC", (CAMERA_WIDTH - 70, 35), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)
        
        return frame
    
    def generate_frames(self):
        while True:
            frame = self.get_frame()
            if frame is None:
                break
            
            # Encode frame to JPEG
            ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if not ret:
                continue
                
            frame_bytes = buffer.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')

# ============================================================================
# RECORDING FUNCTIONS
# ============================================================================

def record_video(filepath):
    """Record video to file on SSD"""
    global recording, video_writer
    
    print(f"📹 Starting recording: {filepath}")
    
    fourcc = cv2.VideoWriter_fourcc(*VIDEO_CODEC)
    video_writer = cv2.VideoWriter(filepath, fourcc, RECORD_FPS, (CAMERA_WIDTH, CAMERA_HEIGHT))
    
    temp_camera = cv2.VideoCapture(CAMERA_INDEX)
    temp_camera.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
    temp_camera.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
    
    frame_count = 0
    start_time = time.time()
    
    while recording:
        ret, frame = temp_camera.read()
        if ret:
            # Add timestamp to recorded video
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cv2.putText(frame, timestamp, (10, 30), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
            
            video_writer.write(frame)
            frame_count += 1
    
    # Cleanup
    temp_camera.release()
    video_writer.release()
    
    duration = time.time() - start_time
    print(f"✓ Recording saved: {frame_count} frames, {duration:.1f}s")

# ============================================================================
# WEB INTERFACE HTML
# ============================================================================

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>RPi DVR System</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #1a1a1a 0%, #2d2d2d 100%);
            color: #fff;
            min-height: 100vh;
        }
        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 20px;
        }
        header {
            text-align: center;
            padding: 20px 0;
            border-bottom: 2px solid #4CAF50;
            margin-bottom: 30px;
        }
        h1 {
            color: #4CAF50;
            font-size: 2.5em;
            margin-bottom: 10px;
        }
        .subtitle {
            color: #888;
            font-size: 1.1em;
        }
        .main-grid {
            display: grid;
            grid-template-columns: 2fr 1fr;
            gap: 20px;
            margin-bottom: 20px;
        }
        .video-container {
            background: #000;
            padding: 15px;
            border-radius: 12px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.5);
        }
        .video-container img {
            width: 100%;
            height: auto;
            border: 3px solid #4CAF50;
            border-radius: 8px;
            display: block;
        }
        .side-panel {
            display: flex;
            flex-direction: column;
            gap: 20px;
        }
        .stats {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 15px;
        }
        .stat-card {
            background: linear-gradient(135deg, #2a2a2a 0%, #1f1f1f 100%);
            padding: 20px;
            border-radius: 10px;
            border-left: 4px solid #4CAF50;
            box-shadow: 0 2px 10px rgba(0,0,0,0.3);
            transition: transform 0.2s;
        }
        .stat-card:hover {
            transform: translateY(-3px);
        }
        .stat-label {
            font-size: 12px;
            color: #888;
            margin-bottom: 8px;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        .stat-value {
            font-size: 32px;
            font-weight: bold;
            color: #4CAF50;
        }
        .stat-unit {
            font-size: 16px;
            color: #aaa;
            margin-left: 5px;
        }
        .status-panel {
            background: #2a2a2a;
            padding: 20px;
            border-radius: 10px;
            text-align: center;
        }
        .status-indicator {
            display: inline-block;
            width: 15px;
            height: 15px;
            border-radius: 50%;
            margin-right: 10px;
            animation: pulse 2s infinite;
        }
        .status-idle { background: #4CAF50; }
        .status-recording { background: #f44336; }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        .controls {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 10px;
            margin-top: 20px;
        }
        button {
            background: linear-gradient(135deg, #4CAF50 0%, #45a049 100%);
            color: white;
            border: none;
            padding: 15px 20px;
            font-size: 16px;
            font-weight: bold;
            border-radius: 8px;
            cursor: pointer;
            transition: all 0.3s;
            box-shadow: 0 2px 10px rgba(76, 175, 80, 0.3);
        }
        button:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 15px rgba(76, 175, 80, 0.5);
        }
        button:active {
            transform: translateY(0);
        }
        button.danger {
            background: linear-gradient(135deg, #f44336 0%, #da190b 100%);
        }
        button.secondary {
            background: linear-gradient(135deg, #666 0%, #555 100%);
        }
        button:disabled {
            background: #333;
            cursor: not-allowed;
            opacity: 0.5;
        }
        .recordings-panel {
            background: #2a2a2a;
            padding: 20px;
            border-radius: 10px;
            margin-top: 20px;
        }
        .recordings-panel h3 {
            color: #4CAF50;
            margin-bottom: 15px;
        }
        .recording-item {
            background: #1f1f1f;
            padding: 10px;
            border-radius: 5px;
            margin-bottom: 10px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .alert {
            background: #ff9800;
            color: #000;
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 20px;
            text-align: center;
            font-weight: bold;
        }
        @media (max-width: 1024px) {
            .main-grid {
                grid-template-columns: 1fr;
            }
            .stats {
                grid-template-columns: repeat(3, 1fr);
            }
        }
        @media (max-width: 768px) {
            .stats {
                grid-template-columns: repeat(2, 1fr);
            }
            .controls {
                grid-template-columns: 1fr;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🎥 Raspberry Pi DVR System</h1>
            <div class="subtitle">Real-time Monitoring & Recording</div>
        </header>
        
        <div class="main-grid">
            <div class="video-container">
                <img src="{{ url_for('video_feed') }}" alt="Live Camera Feed" id="videoFeed">
            </div>
            
            <div class="side-panel">
                <div class="status-panel">
                    <h3>System Status</h3>
                    <div style="margin: 20px 0;">
                        <span class="status-indicator status-idle" id="statusDot"></span>
                        <span id="statusText">Idle - Ready to Record</span>
                    </div>
                    <div style="font-size: 14px; color: #888;">
                        <div>Current File: <span id="currentFile">None</span></div>
                    </div>
                </div>
                
                <div class="stats">
                    <div class="stat-card">
                        <div class="stat-label">Voltage</div>
                        <div class="stat-value" id="voltage">--</div>
                        <span class="stat-unit">V</span>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">Current</div>
                        <div class="stat-value" id="current">--</div>
                        <span class="stat-unit">A</span>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">Power</div>
                        <div class="stat-value" id="power">--</div>
                        <span class="stat-unit">W</span>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">Storage Used</div>
                        <div class="stat-value" id="storageUsed">--</div>
                        <span class="stat-unit">GB</span>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">Free Space</div>
                        <div class="stat-value" id="storageFree">--</div>
                        <span class="stat-unit">GB</span>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">Usage</div>
                        <div class="stat-value" id="storagePercent">--</div>
                        <span class="stat-unit">%</span>
                    </div>
                </div>
            </div>
        </div>
        
        <div id="storageAlert" class="alert" style="display: none;">
            ⚠️ Warning: Storage space is running low!
        </div>
        
        <div class="controls">
            <button onclick="startRecording()" id="startBtn">▶️ Start Recording</button>
            <button onclick="stopRecording()" class="danger" id="stopBtn">⏹️ Stop Recording</button>
            <button onclick="refreshFeed()" class="secondary">🔄 Refresh Feed</button>
        </div>
        
        <div class="recordings-panel">
            <h3>📁 Recent Recordings</h3>
            <div id="recordingsList">Loading...</div>
        </div>
    </div>
    
    <script>
        let isRecording = false;
        
        function updateStats() {
            fetch('/api/stats')
                .then(response => response.json())
                .then(data => {
                    // Battery stats
                    document.getElementById('voltage').textContent = data.voltage.toFixed(2);
                    document.getElementById('current').textContent = data.current.toFixed(3);
                    document.getElementById('power').textContent = data.power.toFixed(2);
                    
                    // Storage stats
                    document.getElementById('storageUsed').textContent = data.storage.used.toFixed(1);
                    document.getElementById('storageFree').textContent = data.storage.free.toFixed(1);
                    document.getElementById('storagePercent').textContent = data.storage.percent.toFixed(1);
                    
                    // Storage warning
                    const alertDiv = document.getElementById('storageAlert');
                    if (data.storage.percent > 90) {
                        alertDiv.style.display = 'block';
                        document.getElementById('storagePercent').style.color = '#ff5252';
                    } else if (data.storage.percent > 75) {
                        document.getElementById('storagePercent').style.color = '#ffa726';
                        alertDiv.style.display = 'none';
                    } else {
                        document.getElementById('storagePercent').style.color = '#4CAF50';
                        alertDiv.style.display = 'none';
                    }
                    
                    // Recording status
                    isRecording = data.recording;
                    const statusDot = document.getElementById('statusDot');
                    const statusText = document.getElementById('statusText');
                    const currentFile = document.getElementById('currentFile');
                    
                    if (isRecording) {
                        statusDot.className = 'status-indicator status-recording';
                        statusText.textContent = '🔴 Recording in Progress';
                        currentFile.textContent = data.filename || 'Recording...';
                        document.getElementById('startBtn').disabled = true;
                        document.getElementById('stopBtn').disabled = false;
                    } else {
                        statusDot.className = 'status-indicator status-idle';
                        statusText.textContent = 'Idle - Ready to Record';
                        currentFile.textContent = 'None';
                        document.getElementById('startBtn').disabled = false;
                        document.getElementById('stopBtn').disabled = true;
                    }
                });
        }
        
        function updateRecordings() {
            fetch('/api/recordings')
                .then(response => response.json())
                .then(data => {
                    const list = document.getElementById('recordingsList');
                    if (data.recordings.length === 0) {
                        list.innerHTML = '<div style="color: #888;">No recordings yet</div>';
                    } else {
                        list.innerHTML = data.recordings.slice(0, 5).map(rec => `
                            <div class="recording-item">
                                <div>
                                    <div style="font-weight: bold;">${rec.name}</div>
                                    <div style="font-size: 12px; color: #888;">${rec.date} • ${rec.size} MB</div>
                                </div>
                            </div>
                        `).join('');
                    }
                });
        }
        
        function startRecording() {
            if (confirm('Start recording to SSD?')) {
                fetch('/api/record/start', {method: 'POST'})
                    .then(response => response.json())
                    .then(data => {
                        alert(data.message);
                        updateStats();
                    })
                    .catch(err => alert('Error: ' + err));
            }
        }
        
        function stopRecording() {
            fetch('/api/record/stop', {method: 'POST'})
                .then(response => response.json())
                .then(data => {
                    alert(data.message);
                    updateStats();
                    updateRecordings();
                })
                .catch(err => alert('Error: ' + err));
        }
        
        function refreshFeed() {
            document.getElementById('videoFeed').src = "{{ url_for('video_feed') }}?" + new Date().getTime();
        }
        
        // Update stats every 2 seconds
        setInterval(updateStats, 2000);
        setInterval(updateRecordings, 5000);
        
        // Initial load
        updateStats();
        updateRecordings();
        
        // Disable stop button initially
        document.getElementById('stopBtn').disabled = true;
    </script>
</body>
</html>
"""

# ============================================================================
# FLASK ROUTES
# ============================================================================

@app.route('/')
@auth.login_required
def index():
    """Main web interface"""
    return render_template_string(HTML_TEMPLATE)

@app.route('/video_feed')
@auth.login_required
def video_feed():
    """Video streaming route"""
    global camera
    if camera is None:
        try:
            camera = CameraStream(CAMERA_INDEX)
        except Exception as e:
            print(f"Error initializing camera: {e}")
            return "Camera initialization failed", 500
    
    return Response(camera.generate_frames(),
                   mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/api/stats')
@auth.login_required
def get_stats():
    """Get system statistics"""
    ssd_info = get_ssd_info()
    return jsonify({
        'voltage': round(current_voltage, 2),
        'current': round(current_current, 3),
        'power': round(current_power, 2),
        'recording': recording,
        'filename': recording_filename,
        'storage': ssd_info,
        'uptime': round((time.time() - system_start_time) / 60, 1)
    })

@app.route('/api/recordings')
@auth.login_required
def get_recordings():
    """Get list of recordings"""
    recordings = get_recording_list()
    return jsonify({
        'recordings': recordings,
        'count': len(recordings)
    })

@app.route('/api/record/start', methods=['POST'])
@auth.login_required
def start_recording():
    """Start recording video"""
    global recording, recording_filename
    
    if recording:
        return jsonify({'message': 'Already recording', 'status': 'error'}), 400
    
    recording = True
    recording_filename = datetime.now().strftime("%Y%m%d_%H%M%S") + ".avi"
    filepath = os.path.join(SSD_MOUNT_PATH, recording_filename)
    
    # Start recording in background thread
    threading.Thread(target=record_video, args=(filepath,), daemon=True).start()
    
    return jsonify({
        'message': f'Recording started: {recording_filename}',
        'status': 'success',
        'filename': recording_filename
    })

@app.route('/api/record/stop', methods=['POST'])
@auth.login_required
def stop_recording():
    """Stop recording video"""
    global recording, recording_filename
    
    if not recording:
        return jsonify({'message': 'Not currently recording', 'status': 'error'}), 400
    
    recording = False
    saved_filename = recording_filename
    recording_filename = None
    
    return jsonify({
        'message': f'Recording stopped: {saved_filename}',
        'status': 'success'
    })

@app.route('/api/system/info')
@auth.login_required
def system_info():
    """Get system information"""
    return jsonify({
        'hostname': os.uname().nodename,
        'python_version': sys.version,
        'uptime_minutes': round((time.time() - system_start_time) / 60, 1),
        'camera_available': camera is not None,
        'ina219_available': INA219_AVAILABLE
    })

# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def print_banner():
    """Print startup banner"""
    print("\n" + "="*60)
    print("   🎥 RASPBERRY PI DVR SYSTEM WITH BATTERY MONITORING")
    print("="*60)
    print(f"Storage Path: {SSD_MOUNT_PATH}")
    print(f"Camera: {CAMERA_WIDTH}x{CAMERA_HEIGHT} @ {CAMERA_FPS}fps")
    print(f"Server: http://{SERVER_HOST}:{SERVER_PORT}")
    print("-"*60)
    print("Default Credentials:")
    print("  Username: admin")
    print("  Password: raspberry")
    print("  ⚠️  CHANGE THESE IN PRODUCTION!")
    print("="*60 + "\n")

if __name__ == '__main__':
    print_banner()
    
    # Initialize storage
    if not initialize_storage():
        print("⚠️  Warning: Storage initialization failed. Check SSD mount.")
        response = input("Continue anyway? (y/n): ")
        if response.lower() != 'y':
            sys.exit(1)
    
    # Start battery monitoring thread
    print("Starting battery monitoring thread...")
    battery_thread = threading.Thread(target=read_battery_data, daemon=True)
    battery_thread.start()
    
    # Wait a moment for sensor initialization
    time.sleep(2)
    
    print("\n✓ System ready!")
    print(f"\n📡 Access the web interface at:")
    print(f"   Local:   http://localhost:{SERVER_PORT}")
    print(f"   Network: http://<your-pi-ip>:{SERVER_PORT}")
    print(f"\n🔐 Login required - use credentials above")
    print(f"🛑 Press Ctrl+C to stop the server\n")
    
    try:
        # Start Flask web server
        app.run(
            host=SERVER_HOST,
            port=SERVER_PORT,
            threaded=True,
            debug=DEBUG_MODE
        )
    except KeyboardInterrupt:
        print("\n\n🛑 Shutting down DVR system...")
        recording = False
        if camera:
            del camera
        print("✓ Shutdown complete. Goodbye!\n")
        sys.exit(0)