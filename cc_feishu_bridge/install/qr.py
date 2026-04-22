"""Terminal QR code printing using qrcode built-in ASCII output."""
from __future__ import annotations

import qrcode


def print_qr(url: str) -> None:
    """Print QR code to terminal using qrcode's built-in ASCII renderer.

    Uses Unicode full-block characters (█ and space) for a compact
    rendering that fits well in standard 80-char terminals.
    """
    print()
    qr = qrcode.QRCode(box_size=4, border=2)
    qr.add_data(url)
    qr.make()
    qr.print_ascii()
    print()
