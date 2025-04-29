import asyncio
import logging
import signal
import os
from bluebird import BluebirdCommissioner

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)-8s - [%(filename)s:%(lineno)d] - (%(name)s) - %(message)s',
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# --- Callback Functions ---
def handle_credentials(ssid, psk):
    log.info(f"[USER CALLBACK] Credentials Received: SSID='{ssid}', PSK length={len(psk)}")
    # Store credentials securely or process them

def handle_status_update(status):
    log.info(f"[USER CALLBACK] Status Update: {status}")
    # Update UI, log status, etc.

# --- Main Async Function ---
async def main():
    log.info("Starting Bluebird Commissioning Example (Multi-Service)")
    if os.geteuid() != 0: log.warning("Script might require root privileges.")

    manager = BluebirdCommissioner(
        on_credentials_received=handle_credentials,
        on_status_update=handle_status_update
    )

    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()
    def signal_handler(): log.info("Shutdown signal received."); stop_event.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError: log.warning(f"Signal handler for {sig} not supported.")

    try:
        await manager.start()
        log.info("Manager started. Waiting for shutdown signal (Ctrl+C)...")
        await stop_event.wait()
    except asyncio.CancelledError: log.info("Main task cancelled.")
    except Exception: log.exception("Error in main execution.")
    finally:
        log.info("Initiating shutdown...")
        await manager.stop()
        log.info("Shutdown complete.")

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: log.info("KeyboardInterrupt caught.")