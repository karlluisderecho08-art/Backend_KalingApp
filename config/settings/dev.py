from .base import *  # noqa: F401,F403

DEBUG = True

# 10.0.2.2 is the Android emulator's fixed alias for "the host machine" --
# a physical phone on the same WiFi needs the machine's real LAN IP
# instead, which changes if your router reassigns it later. Update it
# here if a device can't connect and this IP no longer matches
# (check with: Get-NetIPAddress, or ipconfig, look for the Wi-Fi adapter).
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "10.0.2.2", "192.168.100.94"]
