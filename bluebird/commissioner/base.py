from gi.repository import GLib
import logging
import dbus

from bluebird.ble import (
    BaseApplication, 
    BaseService, 
    BaseCharacteristic, 
    BaseDescriptor, 
    find_adapter
)
from bluebird.commissioner import scan_wifi_ssids

log = logging.getLogger(__name__)

# ---- Network Scanning Service ---- #
class NetworkScanningService(BaseService):
    NetworkScanningServiceUUID = '0000180d-0000-1000-8000-0080511134fb'

    def __init__(self, bus, index):
        super().__init__(bus, index, self.NetworkScanningServiceUUID, False)
        self.ssid_list_char = SsidListCharacteristic(bus, 0, self)
        self.add_characteristic(self.ssid_list_char)
        self._scan_timer_glib_id = None
        self.scanning_interval = 15 # seconds

    def start_scanning(self):
        if self._scan_timer_glib_id:
            log.warning("Scanning already started.")
            return
        log.info(f"Starting periodic Wi-Fi scan (interval: {self.scanning_interval}s)")
        # Run first scan immediately then schedule periodic scans
        self._perform_scan()
        self._scan_timer_glib_id = GLib.timeout_add_seconds(self.scanning_interval, self._perform_scan)

    def stop_scanning(self):
        if self._scan_timer_glib_id:
            log.info("Stopping periodic Wi-Fi scan.")
            GLib.source_remove(self._scan_timer_glib_id)
            self._scan_timer_glib_id = None

    def _perform_scan(self):
        log.debug("Performing scheduled Wi-Fi scan...")
        ssids = scan_wifi_ssids()
        self.ssid_list_char.update_value(ssids if ssids is not None else [])
        return True # Keep the timer running
    
class SsidListCharacteristic(BaseCharacteristic):
    CommissioningServiceUUID = '0000180d-0000-1240-8000-0121411134fb'

    def __init__(self, bus, index, service):
        super().__init__(
            bus, 
            index, 
            self.CommissioningServiceUUID, 
            ['read', 'notify', 'encrypt-read', 'encrypt-notify'], 
            service
        )
        self._ssid_list_bytes = b"" # Store formatted list as bytes
    
    def update_value(self, ssid_list):
        """Formats the list, updates internal value, and notifies."""
        if ssid_list is None:
            ssid_list = []

        new_value_bytes = "\n".join(ssid_list).encode('utf-8')

        if new_value_bytes != self._ssid_list_bytes:
            log.info(f"Updating SSID list characteristic ({len(ssid_list)} networks)")
            self._ssid_list_bytes = new_value_bytes
            # Update internal dbus value
            self._value = dbus.Array(self._ssid_list_bytes, signature='y')
            # Emit signal if notifying
            self._emit_properties_changed({'Value': self._value})
        else:
            log.debug("SSID list unchanged, not emitting signal.")

    def ReadValue(self, options):
        """Returns the current list of SSIDs."""
        log.info(f"Read request for SSID List ({self.uuid})")
        return self._value
    
# ---- Commissioning Service (Encrypted) ---- #
class CommissioningService(BaseService):
    CommissioningServiceUUID = '0000180d-0000-1000-8000-0123411134fb'
    def __init__(self, bus, index):
        super().__init__(bus,index, self.NetworkScanningServiceUUID, True)
        self.ssid_char = CommSsidCharacteristic(bus, 0, self)
        self.psk_char = CommPskCharacteristic(bus, 1, self)
        self.trigger_char = CommTriggerCharacteristic(bus, 2, self)
        self.status_char = CommStatusCharacteristic(bus, 3, self)

        self.add_characteristic(self.ssid_char)
        self.add_characteristic(self.psk_char)
        self.add_characteristic(self.trigger_char)
        self.add_characteristic(self.status_char)

        # Internal state
        self._target_ssid = ""
        self._target_psk = ""

        # Callbacks (to be set by manager)
        self.on_credentials_received = None # func(ssid, psk)
        self.on_connection_trigger = None   # func() -> bool
        self.on_status_update = None        # func(status_str)

    def set_callbacks(self, cred_received_cb=None, trigger_cb=None, status_update_cb=None):
        self.on_credentials_received = cred_received_cb
        self.on_connection_trigger = trigger_cb
        self.on_status_update = status_update_cb

    def _handle_ssid_write(self, value_bytes):
        try:
            self._target_ssid = value_bytes.decode('utf-8')
            log.info(f"Received target commissioning SSID: '{self._target_ssid}'")
            if self.on_credentials_received:
                self.on_credentials_received(self._target_ssid, self._target_psk)
        except UnicodeDecodeError:
            log.error("Failed to decode target SSID")
            self._target_ssid = ""

    def _handle_psk_write(self, value_bytes):
        try:
            self._target_psk = value_bytes.decode('utf-8')
            log.info(f"Received target commissioning PSK: [length={len(self._target_psk)}]")
            if self.on_credentials_received:
                self.on_credentials_received(self._target_ssid, self._target_psk)
        except UnicodeDecodeError:
            log.error("Failed to decode target PSK")
            self._target_psk = ""

    def _handle_trigger_write(self, value_bytes):
        log.info("Commissioning trigger received.")
        if self.on_connection_trigger:
            GLib.idle_add(self.on_connection_trigger)
        else:
            log.warning("on_connection_trigger callback not set!")
            self.update_status("Failed: Trigger callback missing")

    def update_status(self, status_str):
        log.info(f"Updating commissioning status: {status_str}")
        if self.status_char:
            self.status_char.update_value(status_str)
        if self.on_status_update:
             # Schedule callback in main loop to ensure thread safety if called from elsewhere
            GLib.idle_add(self.on_status_update, status_str)

    @property
    def target_ssid(self):
        return self._target_ssid

    @property
    def target_psk(self):
        return self._target_psk

class CommissioningCharacteristicBase(BaseCharacteristic):
    def __init__(self, bus, index, uuid, flags, service):
        # Ensure service is a CommissioningService instance for type hinting/safety
        if not isinstance(service, CommissioningService):
            raise TypeError("Service must be an instance of CommissioningService")
        super().__init__(bus, index, uuid, flags, service)

class CommSsidCharacteristic(CommissioningCharacteristicBase):
    SsidCharacteristicUUID = '12345678-1234-5678-1234-56789aacdef3'

    def __init__(self, bus, index, service):
        super().__init__(
                bus, 
                index,
                self.SsidCharacteristicUUID,
                ['write', 'encrypt-write'],
                service)

    def _handle_write(self, value_bytes: bytes):
        self.service._handle_ssid_write(value_bytes)

class CommPskCharacteristic(CommissioningCharacteristicBase):
    PasswordCharacteristicUUID = '13335788-1234-5678-1234-56b8babcdef3'
    def __init__(self, bus, index, service):
        super().__init__(
                bus, 
                index,
                self.PasswordCharacteristicUUID,
                ['write', 'encrypt-write'],
                service
        )
        self.value = []

    def _handle_write(self, value_bytes: bytes):
        self.service._handle_psk_write(value_bytes)

class CommTriggerCharacteristic(CommissioningCharacteristicBase):
    CommTriggerCharacteristicUUID = '13339988-1234-5678-1234-56b8babcdef3'
    def __init__(self, bus, index, service):
        super().__init__(
                bus, 
                index, 
                self.CommTriggerCharacteristicUUID, 
                ['write', 'encrypt-write'], 
                service
        )

    def _handle_write(self, value_bytes: bytes):
        self.service._handle_trigger_write(value_bytes) # Delegate to service

class CommStatusCharacteristic(CommissioningCharacteristicBase):
    CommStatusCharacteristicUUID = '13935718-1234-5678-1234-56b8babcdef3'
    def __init__(self, bus, index, service):
        super().__init__(
                bus, 
                index, 
                self.CommStatusCharacteristicUUID,
                ['read', 'notify', 'encrypt-read', 'encrypt-notify'], 
                service
        )
        self._value = dbus.Array(b"Idle", signature='y') # Initial value

    def update_value(self, status_str: str):
        log.debug(f"Updating status characteristic value to: {status_str}")
        new_value = dbus.Array(status_str.encode('utf-8'), signature='y')
        if new_value != self._value:
            self._value = new_value
            self._emit_properties_changed({'Value': self._value})

    def ReadValue(self, options):
        log.info(f"Read request for Commissioning Status ({self.uuid})")
        return self._value