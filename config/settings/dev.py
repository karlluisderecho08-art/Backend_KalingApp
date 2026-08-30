from .base import *  # noqa: F401,F403

DEBUG = True

# 10.0.2.2 is the Android emulator's fixed alias for "the host machine" --
# a physical phone on the same WiFi needs the machine's real LAN IP
# instead, which changes if your router reassigns it later. Update it
# here if a device can't connect and this IP no longer matches
# (check with: Get-NetIPAddress, or ipconfig, look for the Wi-Fi adapter).
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "10.0.2.2", "192.168.100.94"]

# The admin/facility web dashboards run on your machine during dev, on
# whatever port their dev server picks -- these are the common defaults
# for React (3000/5173 for Vite), Vue, and Angular. If yours uses a
# different port, add it here (check the terminal output when you run
# `npm run dev` / `npm start`, it prints the exact address).
CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:5173",
    "http://localhost:8080",
    "http://localhost:4200",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:8080",
    "http://127.0.0.1:4200",
]
