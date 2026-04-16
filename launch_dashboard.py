import subprocess
import os
import sys
import time

def main():
    print("========================================")
    print(" Ω OMEGA COMMAND CENTER INITIALIZATION")
    print("========================================")
    
    # 1. Start the FastAPI Database backend
    print("[INFO] Booting FastAPI Backend Telemetry (Port 8000)...")
    backend_env = os.environ.copy()
    
    # Determine the correct python executable depending on virtual environment
    python_exe = sys.executable
    if os.path.exists(os.path.join("venv", "Scripts", "python.exe")):
        python_exe = os.path.join("venv", "Scripts", "python.exe")
        
    backend_process = subprocess.Popen(
        [python_exe, "-m", "uvicorn", "dashboard:app", "--host", "127.0.0.1", "--port", "8000"],
        cwd=os.getcwd()
    )
    
    # Allow 1 second for the server to bind before hitting the Vite dev server
    time.sleep(1)

    # 2. Start the Vite React Frontend
    print("[INFO] Booting Vite Frontend Engine...")
    frontend_dir = os.path.join(os.getcwd(), "frontend")
    npm_cmd = "npm.cmd" if os.name == "nt" else "npm"
    
    frontend_process = subprocess.Popen(
        [npm_cmd, "run", "dev"],
        cwd=frontend_dir
    )
    
    print("\n-------------------------------------------")
    print(">>> BOTH ENGINES ARE ONLINE IN ONE TERMINAL")
    print(">>> Press [CTRL+C] once to cleanly shutdown both servers.")
    print("-------------------------------------------\n")

    try:
        # Wait indefinitely 
        backend_process.wait()
        frontend_process.wait()
    except KeyboardInterrupt:
        print("\n[INFO] Interrupt Signal Received. Shutting down servers gracefully...")
        
        # Kill both child processes on termination
        backend_process.terminate()
        frontend_process.terminate()
        
        backend_process.wait()
        frontend_process.wait()
        print("[INFO] Omega Command Center is offline. Goodbye.")

if __name__ == "__main__":
    main()
