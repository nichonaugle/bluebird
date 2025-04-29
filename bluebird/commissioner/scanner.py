import subprocess
import logging

log = logging.getLogger(__name__)

def scan_wifi_ssids():
    """
    Scans for Wi-Fi SSIDs using nmcli.
    Returns a list of unique SSIDs or None on error.
    """
    try:
        log.debug("Scanning for Wi-Fi networks...")
        cmd = ["nmcli", "--terse", "--fields", "SSID", "--escape", "no", "device", "wifi", "list", "--rescan", "yes"]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=15)

        ssids = set()
        raw_ssids = result.stdout.strip().split('\n')
        for ssid in raw_ssids:
            if ssid and ssid != '--' and len(ssid) > 0 and len(ssid) <= 32:
                ssids.add(ssid)

        log.info(f"Found {len(ssids)} unique SSIDs.")
        return sorted(list(ssids)) # Return sorted list

    except FileNotFoundError:
        log.error("nmcli command not found. Is NetworkManager installed?")
        return None
    except subprocess.CalledProcessError as e:
        log.error(f"nmcli scan failed: {e.stderr or e.stdout}")
        return None
    except subprocess.TimeoutExpired:
        log.error("nmcli scan timed out.")
        return None
    except Exception as e:
        log.exception("Unexpected error during Wi-Fi scan.")
        return None