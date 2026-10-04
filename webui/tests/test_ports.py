import pytest

from common.ports import DST, SRC, client_side, server_side


@pytest.mark.parametrize("src,dst,server_ports,expected", [
    # 1. configured server port, even on a high port and against a known service
    ((51820, "51820"), (40000, "40000"), {51820}, SRC),
    ((50000, "50000"), (51820, "51820"), {51820}, DST),
    ((51820, "51820"), (443, "https"), {51820}, SRC),
    # 2. exactly one known service, even above the threshold
    ((40000, "40000"), (33434, "traceroute"), (), DST),
    ((5432, "postgresql"), (41000, "41000"), (), SRC),
    # 3. low port against an unknown port below the threshold
    ((900, "900"), (2000, "2000"), (), SRC),
    # 4. threshold: both unknown, both >= 1024
    ((2000, "2000"), (45000, "45000"), (), SRC),
    ((60000, "60000"), (8081, "8081"), (), DST),
    ((40000, "40000"), (50000, "50000"), (), SRC),
    # nothing folded
    ((2000, "2000"), (3000, "3000"), (), None),   # both below the threshold
    ((123, "ntp"), (123, "ntp"), (), None),       # equal ports
    ((53, "domain"), (80, "http"), (), None),      # both known and low
    ((None, None), (443, "https"), (), None),      # no ports
])
def test_server_side(src, dst, server_ports, expected):
    assert server_side(src[0], src[1], dst[0], dst[1], server_ports) == expected


def test_both_known_falls_through_to_low_port():
    # 3306 (mysql) and 53 (domain) are both known; 53 is the low one.
    assert server_side(3306, "mysql", 53, "domain") == DST


def test_threshold_is_configurable():
    assert server_side(2000, "2000", 12000, "12000", ephemeral_port_min=10000) == SRC
    assert server_side(2000, "2000", 12000, "12000") is None


def test_client_side():
    src = {"addr": "10.0.0.1", "port": 443, "service": "https"}
    dst = {"addr": "10.0.0.2", "port": 51234, "service": "51234"}
    assert client_side(src, dst) == DST
    assert client_side(dst, src) == SRC
    assert client_side({"addr": "a", "port": None}, {"addr": "b", "port": None}) is None
