from bluebird.ble import (
    Application, 
    Service, 
    Characteristic, 
    Descriptor, 
    find_adapter
)

# ==== Overarching Commissioning Application ==== #
class BluebirdCommissioner(Application):
    def __init__(self, bus):
        self.add_service(NetworkScanningService(bus, 0))
        self.add_service(CommissioningService(bus, 1))

# ---- Network Scanning Service ---- #
class NetworkScanningService(Service):
    NetworkScanningServiceUUID = '0000180d-0000-1000-8000-0080511134fb'

    def __init__(self, bus, index):
        Service.__init__(self,bus,index, self.NetworkScanningServiceUUID, True)
        # TODO: Add in characteristics for all of the found networks

# ---- Commissioning Service (Encrypted) ---- #
class CommissioningService(Service):
    CommissioningServiceUUID = '0000180d-0000-1000-8000-0123411134fb'
    def __init__(self, bus, index):
        Service.__init__(self,bus,index, self.NetworkScanningServiceUUID, True)
        self.add_characteristic(SsidCharacteristic(bus, 0, self))
        self.add_characteristic(PasswordCharacteristic(bus, 1, self))

class SsidCharacteristic(Characteristic):
    TEST_CHRC_UUID = '12345678-1234-5678-1234-56789aacdef3'

    def __init__(self, bus, index, service):
        Characteristic.__init__(
                self, bus, index,
                self.TEST_CHRC_UUID,
                ['encrypt-read', 'encrypt-write'],
                service)
        self.value = []

    def ReadValue(self, options):
        print('SsidCharacteristic Read: ' + repr(self.value))
        return self.value

    def WriteValue(self, value, options):
        print('SsidCharacteristic Write: ' + repr(value))
        self.value = value

class PasswordCharacteristic(Characteristic):
    TEST_CHRC_UUID = '12345678-1234-5678-1234-56b8babcdef3'

    def __init__(self, bus, index, service):
        Characteristic.__init__(
                self, bus, index,
                self.TEST_CHRC_UUID,
                ['encrypt-read', 'encrypt-write'],
                service)
        self.value = []

    def ReadValue(self, options):
        print('PasswordCharacteristic Read: ' + repr(self.value))
        return self.value

    def WriteValue(self, value, options):
        print('PasswordCharacteristic Write: ' + repr(value))
        self.value = value