import logging
import asyncio
import threading
import dbus
import dbus.exceptions
import dbus.mainloop.glib
import dbus.service
import subprocess
import time
import socket
import requests
from gi.repository import GLib
from bluebird.commissioner import (
    CommissioningService,
    NetworkScanningService
)
from bluebird.ble import (
    BaseApplication,
    BaseAdvertisement
)

# --- D-Bus Constants ---
BLUEZ_SERVICE = "org.bluez"
DBUS_OM_IFACE = "org.freedesktop.DBus.ObjectManager"
DBUS_PROP_IFACE = "org.freedesktop.DBus.Properties"
ADAPTER_IFACE = f"{BLUEZ_SERVICE}.Adapter1"
GATT_MANAGER_IFACE = f"{BLUEZ_SERVICE}.GattManager1"
LE_ADVERTISING_MANAGER_IFACE = f"{BLUEZ_SERVICE}.LEAdvertisingManager1"
AGENT_MANAGER_IFACE = f"{BLUEZ_SERVICE}.AgentManager1"
AGENT_IFACE = f"{BLUEZ_SERVICE}.Agent1"
LE_ADVERTISEMENT_IFACE = f"{BLUEZ_SERVICE}.LEAdvertisement1"

# --- Default Paths and Config ---
APP_PATH_BASE = "/com/example/bluebird/commissioning"
APP_AGENT_PATH = f"{APP_PATH_BASE}/agent"
DEFAULT_AD_PATH = f"{APP_PATH_BASE}/advertisement0"
DEFAULT_LOCAL_NAME = "Bluebird-Setup"
DEFAULT_NETWORK_CHECK_INTERVAL = 15

log = logging.getLogger(__name__)

# ==== Overarching Commissioning Application ==== #
class BluebirdCommissioner():
    """
    Manages the BLE commissioning services lifecycle asynchronously.
    Includes network scanning and credential handling.
    """
    def __init__(self,
                 local_name=DEFAULT_LOCAL_NAME,
                 app_path_base=APP_PATH_BASE,
                 network_check_interval=DEFAULT_NETWORK_CHECK_INTERVAL,
                 on_credentials_received=None, # func(ssid, psk)
                 on_status_update=None,        # func(status_str)
                 wifi_connect_func=None        # Optional: func(ssid, psk) -> bool
                ):
        self._local_name = local_name
        self._app_path_base = app_path_base
        self._agent_path = f"{app_path_base}/agent"
        self._ad_path = f"{app_path_base}/advertisement0"
        self._network_check_interval = network_check_interval

        # Callbacks
        self._on_credentials_received_user = on_credentials_received
        self._on_status_update_user = on_status_update
        self._wifi_connect_func = wifi_connect_func or self._default_wifi_connect

        # State (add service references)
        self._bus = None
        self._adapter_path = None
        self._adapter_props = None
        self._agent_manager = None
        self._gatt_manager = None
        self._ad_manager = None
        self._glib_loop = None
        self._glib_thread = None
        self._monitor_task = None
        self._app = None
        self._commissioning_service: CommissioningService = None # Type hint
        self._network_scan_service: NetworkScanningService = None # Type hint
        self._agent = None
        self._advertisement = None
        self._is_commissioning_active = False
        self._is_advertising = False
        self._is_gatt_registered = False
        self._is_agent_registered = False
        self._lock = asyncio.Lock()

    async def _init_dbus(self):
        """Initializes D-Bus connection and main loop thread."""
        if self._bus:
            return True # Already initialized

        try:
            # Ensure GLib main loop integration with dbus-python
            dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
            self._bus = dbus.SystemBus()

            # Start GLib main loop in a separate thread
            self._glib_loop = GLib.MainLoop()
            self._glib_thread = threading.Thread(target=self._glib_loop.run, daemon=True)
            self._glib_thread.start()
            log.info("D-Bus connection and GLib main loop thread started.")
            return True
        except Exception as e:
            log.exception(f"Failed to initialize D-Bus: {e}")
            self._bus = None
            self._glib_loop = None
            self._glib_thread = None
            return False

    async def _find_adapter(self):
        """Finds the first BLE adapter."""
        if not self._bus: return None
        try:
            om = dbus.Interface(self._bus.get_object(BLUEZ_SERVICE, '/'), DBUS_OM_IFACE)
            objects = om.GetManagedObjects()
            for path, ifaces in objects.items():
                if ADAPTER_IFACE in ifaces:
                    log.info(f"Found adapter: {path}")
                    self._adapter_path = path
                    # Get proxies for later use
                    self._adapter_props = dbus.Interface(self._bus.get_object(BLUEZ_SERVICE, path), DBUS_PROP_IFACE)
                    self._agent_manager = dbus.Interface(self._bus.get_object(BLUEZ_SERVICE, "/org/bluez"), AGENT_MANAGER_IFACE)
                    self._gatt_manager = dbus.Interface(self._bus.get_object(BLUEZ_SERVICE, path), GATT_MANAGER_IFACE)
                    self._ad_manager = dbus.Interface(self._bus.get_object(BLUEZ_SERVICE, path), LE_ADVERTISING_MANAGER_IFACE)
                    return path
            log.error("No Bluetooth adapter found.")
            return None
        except dbus.exceptions.DBusException as e:
            log.error(f"D-Bus error finding adapter: {e}")
            return None
    
    async def _setup_agent(self):
        """Sets up and registers the pairing agent."""
        if not self._bus or not self._agent_manager or not self._adapter_props: return False
        if self._is_agent_registered: return True

        try:
            self._agent = Agent(self._bus, self._agent_path) # Agent object is created on the bus
            capability = "NoInputNoOutput"
            log.info(f"Registering agent with capability: {capability}")
            # Run registration in GLib thread
            await asyncio.get_event_loop().run_in_executor(None,
                lambda: self._agent_manager.RegisterAgent(self._agent_path, capability)
            )
            await asyncio.get_event_loop().run_in_executor(None,
                lambda: self._agent_manager.RequestDefaultAgent(self._agent_path)
            )
            log.info("Agent registered and set as default.")

            log.info("Setting adapter pairable...")
            await asyncio.get_event_loop().run_in_executor(None,
                lambda: self._adapter_props.Set(ADAPTER_IFACE, "Pairable", dbus.Boolean(True))
            )
            await asyncio.get_event_loop().run_in_executor(None,
                lambda: self._adapter_props.Set(ADAPTER_IFACE, "PairableTimeout", dbus.UInt32(0))
            )
            log.info("Adapter set to pairable.")
            self._is_agent_registered = True
            return True
        except dbus.exceptions.DBusException as e:
            log.error(f"Failed agent setup: {e}")
            self._is_agent_registered = False
            return False

    async def _teardown_agent(self):
        """Unregisters the pairing agent."""
        if not self._is_agent_registered or not self._agent_manager: return
        try:
            log.info("Unregistering agent...")
            await asyncio.get_event_loop().run_in_executor(None,
                lambda: self._agent_manager.UnregisterAgent(self._agent_path)
            )
            self._is_agent_registered = False
            log.info("Agent unregistered.")
        except dbus.exceptions.DBusException as e:
            log.warning(f"Failed to unregister agent: {e}")
        finally:
            # Agent object might remove itself from bus on unregister, or needs manual cleanup
            self._agent = None # Clear reference
    
    async def _register_gatt_app(self):
        """Creates and registers the GATT application with BOTH services."""
        if not self._bus or not self._gatt_manager: return False
        if self._is_gatt_registered: return True

        try:
            log.info("Creating GATT application object with services...")
            self._app = BaseApplication(self._bus, self._app_path_base)

            # Instantiate BOTH services
            self._network_scan_service = NetworkScanningService(self._bus, 0)
            self._commissioning_service = CommissioningService(self._bus, 1)

            # Set callbacks ONLY on the commissioning service
            self._commissioning_service.set_callbacks(
                cred_received_cb=self._handle_credentials_received_internal, # Internal handler
                trigger_cb=self._handle_connection_trigger_internal, # Internal handler
                status_update_cb=self._handle_status_update_internal # Internal handler
            )

            # Add services to the application manager
            self._app.add_service(self._network_scan_service)
            self._app.add_service(self._commissioning_service)

            log.info("Registering GATT application with BlueZ...")
            await asyncio.get_event_loop().run_in_executor(None,
                lambda: self._gatt_manager.RegisterApplication(self._app.get_path(), {})
            )
            self._is_gatt_registered = True
            log.info("GATT application registered successfully.")
            # Start network scanning AFTER registration
            GLib.idle_add(self._network_scan_service.start_scanning)
            return True
        except dbus.exceptions.DBusException as e:
            log.error(f"Failed to register GATT application: {e}")
            self._is_gatt_registered = False
            self._app = None
            self._commissioning_service = None
            self._network_scan_service = None
            return False

    async def _unregister_gatt_app(self):
        """Unregisters the GATT application and stops scanning."""
        # Stop scanning first
        if self._network_scan_service:
            GLib.idle_add(self._network_scan_service.stop_scanning)

        if not self._is_gatt_registered or not self._gatt_manager or not self._app: return
        try:
            log.info("Unregistering GATT application...")
            await asyncio.get_event_loop().run_in_executor(None,
                 lambda: self._gatt_manager.UnregisterApplication(self._app.get_path())
            )
            self._is_gatt_registered = False
            log.info("GATT application unregistered.")
        except dbus.exceptions.DBusException as e:
            log.warning(f"Failed to unregister GATT application: {e}")
        finally:
            self._app = None
            self._commissioning_service = None
            self._network_scan_service = None

    async def _register_advertisement(self):
        CommissioningServiceUUID = '0000180d-0000-1000-8000-0123411134fb'
        if not self._bus or not self._ad_manager: return False
        if self._is_advertising: return True

        try:
            log.info("Creating advertisement object...")
            # Advertise the COMMISSIONING service UUID
            self._advertisement = BaseAdvertisement(
                self._bus,
                self._ad_path,
                'peripheral',
                CommissioningServiceUUID, # Advertise primary service
                self._local_name
            )

            log.info("Registering LE advertisement...")
            await asyncio.get_event_loop().run_in_executor(None,
                lambda: self._ad_manager.RegisterAdvertisement(self._advertisement.get_path(), {})
            )
            self._is_advertising = True
            log.info("Advertisement registered successfully.")
            return True
        except dbus.exceptions.DBusException as e:
             if "Already Exists" in str(e):
                 log.warning(f"Advertisement registration failed (Already Exists): {e}")
                 self._is_advertising = True
                 return True
             else:
                log.error(f"Failed to register advertisement: {e}")
                self._is_advertising = False
                self._advertisement = None
                return False

    async def _unregister_advertisement(self):
        """Unregisters the LE advertisement."""
        if not self._is_advertising or not self._ad_manager or not self._advertisement: return
        try:
            log.info("Unregistering advertisement...")
            await asyncio.get_event_loop().run_in_executor(None,
                 lambda: self._ad_manager.UnregisterAdvertisement(self._advertisement.get_path())
            )
            self._is_advertising = False
            log.info("Advertisement unregistered.")
        except dbus.exceptions.DBusException as e:
            log.warning(f"Failed to unregister advertisement: {e}")
            self._is_advertising = False # Ensure state is correct
        finally:
            self._advertisement = None # Clear reference

    def _check_network_connectivity(self, host="8.8.8.8", port=53, timeout=3, check_http="http://google.com"):
        """Checks basic DNS and optional HTTP connectivity."""
        try:
            socket.setdefaulttimeout(timeout)
            socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
            log.debug("Network Check: DNS/Socket check passed.")
            if check_http:
                try:
                    response = requests.get(check_http, timeout=5)
                    response.raise_for_status()
                    log.debug(f"Network Check: HTTP check to {check_http} passed.")
                    return True
                except requests.exceptions.RequestException:
                    log.warning(f"Network Check: HTTP check failed.")
                    return False # Consider HTTP failure as network down
            return True
        except Exception as e:
            log.warning(f"Network Check: Failed: {e}")
            return False
        
    async def _network_monitor(self):
        """Periodically checks network and toggles commissioning state."""
        log.info("Network monitor started.")
        await asyncio.sleep(2) # Initial delay
        while True:
            try:
                is_connected = self._check_network_connectivity()
                log.debug(f"Network status: {'Connected' if is_connected else 'Disconnected'}")

                async with self._lock:
                    should_be_active = not is_connected
                    if should_be_active and not self._is_commissioning_active:
                        log.info("Network down. Triggering start_commissioning.")
                        await self.start_commissioning()
                    elif not should_be_active and self._is_commissioning_active:
                        log.info("Network up. Triggering stop_commissioning.")
                        await self.stop_commissioning()

            except asyncio.CancelledError:
                log.info("Network monitor task cancelled.")
                break
            except Exception as e:
                log.exception("Error in network monitor loop.")
                # Avoid rapid looping on error
                await asyncio.sleep(self._network_check_interval)
            else:
                 await asyncio.sleep(self._network_check_interval)

    # --- Internal Callback Handlers ---
    def _handle_credentials_received_internal(self, ssid, psk):
        """Internal handler, calls user callback."""
        log.debug(f"Internal: Credentials received: SSID={ssid}, PSK len={len(psk)}")
        if self._on_credentials_received_user:
            try:
                # Run user callback in the asyncio event loop
                asyncio.get_event_loop().call_soon(
                    self._on_credentials_received_user, ssid, psk
                )
            except Exception as e:
                log.error(f"Error executing user on_credentials_received callback: {e}")

    def _handle_status_update_internal(self, status):
        """Internal handler, calls user callback."""
        log.debug(f"Internal: Status update: {status}")
        if self._on_status_update_user:
             try:
                # Run user callback in the asyncio event loop
                asyncio.get_event_loop().call_soon(
                    self._on_status_update_user, status
                )
             except Exception as e:
                log.error(f"Error executing user on_status_update callback: {e}")

    async def _handle_connection_trigger_internal(self):
        """Internal handler for the trigger, calls wifi connect func."""
        log.info("Handling connection trigger internally...")
        self.update_status("Connecting") # Update status via internal method

        # Get current credentials from the service instance
        ssid = self._commissioning_service.target_ssid
        psk = self._commissioning_service.target_psk

        if not ssid:
            log.error("Trigger received but target SSID is not set.")
            self.update_status("Failed: SSID not set")
            return False

        # Run the potentially blocking connection logic in executor
        success = await asyncio.get_event_loop().run_in_executor(
            None, # Use default executor
            self._wifi_connect_func, # Call the assigned connect function
            ssid, # Pass credentials to it
            psk
        )
        # The connect func is responsible for the final status update
        log.info(f"Connection attempt result: {success}")
        return success

    def _default_wifi_connect(self, ssid, psk) -> bool:
        """Default Wi-Fi connection logic using nmcli."""
        # This runs in an executor thread
        if not ssid:
            log.warning("Default Wi-Fi Connect: SSID missing.")
            self.update_status("Failed: SSID not set") # Update status via manager method
            return False

        log.info(f"Default Wi-Fi Connect: Attempting nmcli connect to SSID: '{ssid}'")
        try:
            subprocess.run(["nmcli", "connection", "down", ssid], check=False, capture_output=True, text=True, timeout=10)
            subprocess.run(["nmcli", "connection", "delete", ssid], check=False, capture_output=True, text=True, timeout=10)
            time.sleep(1)
            command = ["nmcli", "device", "wifi", "connect", ssid]
            if psk: command.extend(["password", psk])
            log.info(f"Running command: {' '.join(command)}")
            result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=60)
            log.info(f"nmcli connection successful: {result.stdout}")
            self.update_status("Success")
            return True
        except subprocess.CalledProcessError as e:
            error_msg = (e.stderr or e.stdout or "Unknown nmcli error").strip()
            log.error(f"nmcli connection failed: {error_msg}")
            self.update_status(f"Failed: {error_msg}")
            return False
        except subprocess.TimeoutExpired:
            log.error("nmcli connection command timed out.")
            self.update_status("Failed: Timeout")
            return False
        except Exception as e:
            log.error(f"An unexpected error during Wi-Fi connection: {e}")
            self.update_status(f"Failed: Unexpected error")
            return False

    def update_status(self, status_str: str):
        """Thread-safe way to update status from connection logic via the service."""
        if self._glib_loop and self._commissioning_service:
             # Schedule the service's update method on the GLib thread
             GLib.idle_add(self._commissioning_service.update_status, status_str)
        else:
            log.warning("Cannot update status: GLib loop or service not available.")


    # --- Public API ---
    async def start(self):
        """Initialize D-Bus and start the network monitor."""
        async with self._lock:
            if self._monitor_task:
                log.warning("Manager already started.")
                return

            log.info("Starting BleCommissioningManager...")
            if not await self._init_dbus():
                log.error("Failed to initialize D-Bus. Cannot start manager.")
                return
            if not await self._find_adapter():
                 log.error("Failed to find Bluetooth adapter. Cannot start manager.")
                 await self.stop() # Cleanup D-Bus if started
                 return

            # Start network monitor which will trigger commissioning if needed
            self._monitor_task = asyncio.create_task(self._network_monitor())
            log.info("BleCommissioningManager started.")

    async def stop(self):
        """Stop the manager, network monitor, and cleanup BLE resources."""
        async with self._lock:
            if not self._monitor_task and not self._glib_loop:
                log.warning("Manager already stopped.")
                return

            log.info("Stopping BleCommissioningManager...")
            # Cancel monitor task
            if self._monitor_task:
                self._monitor_task.cancel()
                try:
                    await self._monitor_task
                except asyncio.CancelledError:
                    pass # Expected
                self._monitor_task = None
                log.info("Network monitor stopped.")

            # Stop commissioning if active
            if self._is_commissioning_active:
                await self.stop_commissioning() # Ensure BLE resources are released

            # Stop GLib loop and thread
            if self._glib_loop:
                try:
                    # Schedule quit from the asyncio loop using run_in_executor
                    # or directly if thread safety allows (GLib.MainLoop.quit is thread-safe)
                    GLib.idle_add(self._glib_loop.quit)
                    # Wait for thread to finish
                    # Note: Joining threads from async code needs care, executor is better
                    # For simplicity, we might just signal quit and not explicitly join here
                    log.info("Requested GLib main loop quit.")
                except Exception as e:
                    log.warning(f"Error quitting GLib loop: {e}")
                finally:
                    self._glib_loop = None
                    self._glib_thread = None # Clear thread reference

            # Disconnect D-Bus
            if self._bus:
                try:
                    self._bus.close() # Close connection
                except Exception as e:
                     log.warning(f"Error closing D-Bus connection: {e}")
                finally:
                    self._bus = None

            log.info("BleCommissioningManager stopped.")

    async def start_commissioning(self):
        """Manually start the BLE commissioning service and advertising."""
        async with self._lock:
            if self._is_commissioning_active:
                log.warning("Commissioning is already active.")
                return True
            if not self._bus or not self._adapter_path:
                log.error("Cannot start commissioning: D-Bus not initialized or adapter not found.")
                return False

            log.info("Starting BLE commissioning...")
            success = await self._setup_agent()
            if success: success = await self._register_gatt_app()
            if success: success = await self._register_advertisement()

            if success:
                self._is_commissioning_active = True
                self.update_status("Idle") # Set initial status
                log.info("BLE commissioning started successfully.")
            else:
                log.error("Failed to start BLE commissioning. Cleaning up partial setup...")
                # Attempt cleanup in reverse order
                await self._unregister_advertisement()
                await self._unregister_gatt_app()
                await self._teardown_agent()
                self._is_commissioning_active = False
                self.update_status("Failed: BLE Setup Error")

            return success

    async def stop_commissioning(self):
        """Manually stop the BLE commissioning service and advertising."""
        async with self._lock:
            if not self._is_commissioning_active:
                log.warning("Commissioning is already stopped.")
                return

            log.info("Stopping BLE commissioning...")
            # Stop in reverse order of start
            await self._unregister_advertisement()
            await self._unregister_gatt_app()
            await self._teardown_agent()
            self._is_commissioning_active = False
            self.update_status("Stopped")
            log.info("BLE commissioning stopped.")