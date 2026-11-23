import asyncio
import ipaddress
import socket


async def resolve_public_address(hostname: str, port: int) -> str:
    """Resolve once and return a numeric address for the caller to connect to."""
    if not hostname or "%" in hostname:
        raise ValueError("Invalid destination hostname.")
    records = await asyncio.wait_for(
        asyncio.get_running_loop().getaddrinfo(hostname, port, type=socket.SOCK_STREAM),
        timeout=5,
    )
    addresses = [ipaddress.ip_address(record[4][0]) for record in records]
    if not addresses or any(not address.is_global or address.is_multicast for address in addresses):
        raise ValueError("Destination must resolve exclusively to public unicast addresses.")
    return str(next((address for address in addresses if address.version == 4), addresses[0]))
