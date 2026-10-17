"""Port and protocol filters, shared by the live and history pages."""

PROTOCOLS = {1: "icmp", 6: "tcp", 17: "udp", 58: "icmpv6", 132: "sctp"}
PROTOCOL_NUMBERS = {name: number for number, name in PROTOCOLS.items()}


class FilterError(ValueError):
    pass


def proto_name(number):
    return PROTOCOLS.get(number, str(number))


def parse_proto(value):
    """A protocol name or number -> its number; empty -> None."""
    value = (value or "").strip().lower()
    if not value:
        return None
    if value.isdigit() and 0 <= int(value) <= 255:
        return int(value)
    if value in PROTOCOL_NUMBERS:
        return PROTOCOL_NUMBERS[value]
    raise FilterError(f"unknown protocol: {value}")


def parse_port(value):
    """A port number -> int, a service name -> lower-case str; empty -> None."""
    value = (value or "").strip().lower()
    if not value:
        return None
    if value.isdigit():
        port = int(value)
        if not 1 <= port <= 65535:
            raise FilterError(f"invalid port: {value}")
        return port
    if len(value) > 64 or not all(ch.isalnum() or ch in "-_." for ch in value):
        raise FilterError(f"invalid service name: {value}")
    return value


def endpoint_has_port(end, port):
    """Whether an iftop endpoint has the port (int) or service name (str)."""
    if isinstance(port, int):
        return end.get("port") == port
    return (end.get("service") or "").lower() == port
