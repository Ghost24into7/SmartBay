"""
Parking Management System - Launcher Script

This script provides a convenient way to start the parking management system.
It launches the Flask web service in the background, waits for it to initialize,
automatically opens the web interface in the default browser, and handles
graceful shutdown when the user presses Ctrl+C.

Features:
- Automatic service startup
- Browser auto-launch
- Graceful shutdown handling
- Cross-platform compatibility (Windows/Linux/macOS)

Usage:
    python run.py

The script will:
1. Start the parking_service.py Flask application
2. Wait 3 seconds for initialization
3. Open http://localhost:5000 in the default browser
4. Keep running until interrupted (Ctrl+C)
5. Gracefully terminate the service on exit
"""

import subprocess
import webbrowser
import time
import signal
import sys
import os

import gevent
from gevent import monkey
monkey.patch_all()


def main():
    """
    Main launcher function.

    Starts the parking service, opens browser, and handles shutdown.
    """
    # Start the parking service
    print("Starting Parking Management System...")
    service_process = subprocess.Popen([sys.executable, 'parking_service.py'])

    # Wait for the service to start
    time.sleep(3)

    # Open the web interface in the default browser
    print("Opening web interface...")
    webbrowser.open('http://localhost:5000')

    def signal_handler(sig, frame):
        """
        Signal handler for graceful shutdown.

        Terminates the service process and exits cleanly.
        """
        print("\nShutting down Parking Management System...")
        service_process.terminate()
        service_process.wait()
        print("System stopped.")
        sys.exit(0)

    # Register signal handler for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        # Keep the script running
        service_process.wait()
    except KeyboardInterrupt:
        signal_handler(signal.SIGINT, None)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"Starting Parking Management System on port {port}")
    socketio.run(app, host='0.0.0.0', port=port)