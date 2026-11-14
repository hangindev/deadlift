"""The firewall for containers: the rules it wants in each chain (ADR 0001)."""

from deadlift.server import firewall


def test_drops_a_containers_new_connections_to_private_ipv4_ranges() -> None:
    rules = firewall.rules("iptables")

    assert rules.forward == (
        "-m conntrack --ctstate RELATED,ESTABLISHED -j RETURN",
        "-o docker0 -j RETURN",
        "-o br-+ -j RETURN",
        "-d 10.0.0.0/8 -j DROP",
        "-d 100.64.0.0/10 -j DROP",
        "-d 169.254.0.0/16 -j DROP",
        "-d 172.16.0.0/12 -j DROP",
        "-d 192.168.0.0/16 -j DROP",
    )
    assert rules.input == ("-m conntrack --ctstate RELATED,ESTABLISHED -j RETURN", "-j DROP")


def test_lets_ipv6_neighbour_discovery_through_to_the_host() -> None:
    rules = firewall.rules("ip6tables")

    assert rules.forward[-2:] == ("-d fc00::/7 -j DROP", "-d fe80::/10 -j DROP")
    assert rules.input == (
        "-m conntrack --ctstate RELATED,ESTABLISHED -j RETURN",
        "-p ipv6-icmp -m icmp6 --icmpv6-type 133 -j RETURN",
        "-p ipv6-icmp -m icmp6 --icmpv6-type 135 -j RETURN",
        "-p ipv6-icmp -m icmp6 --icmpv6-type 136 -j RETURN",
        "-j DROP",
    )


def test_leaves_a_chain_that_holds_exactly_its_rules() -> None:
    listing = "-N DEADLIFT-INPUT\n-A DEADLIFT-INPUT -j DROP\n"

    assert firewall.chain_holds(listing, "DEADLIFT-INPUT", ("-j DROP",))


def test_rewrites_a_chain_that_holds_other_rules() -> None:
    listing = "-N DEADLIFT-INPUT\n-A DEADLIFT-INPUT -j ACCEPT\n-A DEADLIFT-INPUT -j DROP\n"

    assert not firewall.chain_holds(listing, "DEADLIFT-INPUT", ("-j DROP",))
    assert not firewall.chain_holds("-N DEADLIFT-INPUT\n", "DEADLIFT-INPUT", ("-j DROP",))
