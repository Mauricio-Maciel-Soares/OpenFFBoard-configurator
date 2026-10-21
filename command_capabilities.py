"""Discover command names, without sending unknown commands to old firmware.

Old firmware silently drops unknown names, whereas `help!` lists every supported
command. A timeout therefore must never be interpreted as 'unsupported'.
"""
import re


def parse_capabilities(reply):
    result = {}
    for line in reply.splitlines():
        match = re.match(r'^([A-Za-z_][\w]*),0x[0-9A-Fa-f]+,(.*),([^,]*)$', line)
        if match:
            name, description, flags = match.groups()
            result[name] = {'description': description.strip(), 'flags': set(flags.split())}
    if not result:
        raise ValueError('Invalid command capability list')
    return result
