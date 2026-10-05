import subprocess
import sys

print("Installing requirements...")
subprocess.run([sys.executable, "-m", "pip", "install", "--prefer-binary", "-r", "requirements.txt"], check=True)

print("Installing Playwright browsers...")
subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)

print("\n✅ Setup complete! Run 'python main.py' or start_brahma.bat to start Brahma Evo.")
