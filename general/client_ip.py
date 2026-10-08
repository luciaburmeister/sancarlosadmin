"""The real caller address behind our proxies, for throttling by IP."""

import ipaddress


def client_ip(request):
    """
    The caller's address behind the EB load balancer and nginx.

    The rightmost public address in X-Forwarded-For is the one our own
    proxies appended; anything to its left was supplied by the client and
    can be forged. The proxies themselves sit on private VPC addresses.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    for candidate in reversed([part.strip() for part in forwarded.split(",")]):
        try:
            if ipaddress.ip_address(candidate).is_global:
                return candidate
        except ValueError:
            continue
    return request.META.get("REMOTE_ADDR", "")
