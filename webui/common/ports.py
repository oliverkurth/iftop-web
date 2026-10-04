"""Deciding which side of a connection is the client, so its port can be folded.

There is no single ephemeral port range (IANA 49152-65535, Linux 32768-60999,
FreeBSD 10000-65535, ...) and servers also listen on high ports, so the server
side is found by rules; the first that applies wins (see PLAN.md, stage 2):

1. a port listed in server_ports,
2. a known service (in /etc/services),
3. a port below 1024,
4. otherwise the lower port, if the higher one is >= ephemeral_port_min.
"""

SRC = "src"
DST = "dst"

LOW_PORT_LIMIT = 1024


def is_known_service(port, service):
    # iftop reports the port number itself when /etc/services has no entry.
    return service is not None and service != str(port)


def _one_side(src_true, dst_true):
    if src_true and not dst_true:
        return SRC
    if dst_true and not src_true:
        return DST
    return None


def server_side(src_port, src_service, dst_port, dst_service, server_ports=(), ephemeral_port_min=32768):
    """Returns SRC or DST for the server side, or None to fold nothing."""
    if src_port is None or dst_port is None or src_port == dst_port:
        return None
    side = (_one_side(src_port in server_ports, dst_port in server_ports)
            or _one_side(is_known_service(src_port, src_service), is_known_service(dst_port, dst_service))
            or _one_side(src_port < LOW_PORT_LIMIT, dst_port < LOW_PORT_LIMIT))
    if side:
        return side
    if max(src_port, dst_port) >= ephemeral_port_min:
        return SRC if src_port < dst_port else DST
    return None


def client_side(src, dst, server_ports=(), ephemeral_port_min=32768):
    """For two iftop endpoints, returns the side whose port is folded, or None."""
    side = server_side(src.get("port"), src.get("service"), dst.get("port"), dst.get("service"),
                       server_ports, ephemeral_port_min)
    if side is None:
        return None
    return DST if side == SRC else SRC
