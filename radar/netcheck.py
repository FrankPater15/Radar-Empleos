"""Comprobación de red antes de la corrida desatendida (el Wi-Fi puede tardar al encender el PC)."""
from __future__ import annotations

import socket
import time
from typing import Callable

HOSTS = (("www.getonbrd.com", 443), ("himalayas.app", 443), ("1.1.1.1", 443))


def has_network(hosts=HOSTS, timeout: float = 5) -> bool:
    """True si al menos uno de los hosts acepta una conexión TCP."""
    for host, port in hosts:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def wait_for_network(max_wait_s: int = 180, step_s: int = 15, sleep: Callable[[float], None] = time.sleep,
                     check: Callable[[], bool] = has_network, log=lambda msg: None) -> bool:
    """Reintenta cada step_s segundos hasta max_wait_s. Devuelve False si nunca hubo red."""
    waited = 0
    while True:
        if check():
            return True
        if waited >= max_wait_s:
            return False
        log(f"Sin red; reintento en {step_s} s (esperado {waited}/{max_wait_s} s)")
        sleep(step_s)
        waited += step_s
