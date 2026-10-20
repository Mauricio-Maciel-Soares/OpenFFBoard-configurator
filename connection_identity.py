"""Reconnect only to the selected device, even if its COM name changes."""
def port_identity(port):
    return (port.vendorIdentifier(), port.productIdentifier(),
            port.serialNumber(), port.systemLocation())


def same_device(identity, port):
    candidate = port_identity(port)
    if identity[:2] != candidate[:2]:
        return False
    return identity[2] == candidate[2] if identity[2] else identity[3] == candidate[3]
