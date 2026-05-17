"""微信个人（iLink）AES-128-ECB 加解密工具。

用于 CDN 媒体上传的 AES-128-ECB 加密。
参考 Hermes Agent: gateway/platforms/weixin.py
"""
from __future__ import annotations

import base64
import secrets
from typing import Optional

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend

    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False
    default_backend = None  # type: ignore[assignment]
    Cipher = None  # type: ignore[assignment]
    algorithms = None  # type: ignore[assignment]
    modes = None  # type: ignore[assignment]


def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    """PKCS7 padding."""
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)


def _pkcs7_unpad(data: bytes) -> bytes:
    """Remove PKCS7 padding."""
    if not data:
        return data
    pad_len = data[-1]
    if 1 <= pad_len <= 16 and data.endswith(bytes([pad_len]) * pad_len):
        return data[:-pad_len]
    return data


def aes128_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    """AES-128-ECB 加密（PKCS7 padding）。

    Args:
        plaintext: 待加密的原始字节数据
        key: 16字节 AES 密钥

    Returns:
        加密后的密文字节数据
    """
    if not CRYPTO_AVAILABLE:
        raise RuntimeError("cryptography package is required for WeChat AES encryption. Install with: pip install cryptography")

    if len(key) != 16:
        raise ValueError(f"AES key must be 16 bytes, got {len(key)}")

    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    encryptor = cipher.encryptor()
    return encryptor.update(_pkcs7_pad(plaintext)) + encryptor.finalize()


def aes128_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    """AES-128-ECB 解密（PKCS7 unpadding）。

    Args:
        ciphertext: 加密的密文字节数据
        key: 16字节 AES 密钥

    Returns:
        解密后的原始字节数据
    """
    if not CRYPTO_AVAILABLE:
        raise RuntimeError("cryptography package is required for WeChat AES decryption. Install with: pip install cryptography")

    if len(key) != 16:
        raise ValueError(f"AES key must be 16 bytes, got {len(key)}")

    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    return _pkcs7_unpad(padded)


def aes_padded_size(size: int) -> int:
    """返回对齐到 16 字节边界后的文件大小（用于 CDN 上传）。"""
    return ((size + 1 + 15) // 16) * 16


def generate_aes_key() -> bytes:
    """生成随机的 16 字节 AES 密钥。"""
    return secrets.token_bytes(16)


def parse_aes_key(aes_key_b64: str) -> bytes:
    """解析 AES 密钥。

    支持两种格式：
    - 原始 16 字节的 base64 编码
    - 32 字符的十六进制字符串

    Args:
        aes_key_b64: base64 编码的 AES 密钥

    Returns:
        16 字节的密钥

    Raises:
        ValueError: 密钥格式不正确
    """
    decoded = base64.b64decode(aes_key_b64)
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32:
        text = decoded.decode("ascii", errors="ignore")
        if text and all(ch in "0123456789abcdefABCDEF" for ch in text):
            return bytes.fromhex(text)
    raise ValueError(f"unexpected aes_key format ({len(decoded)} decoded bytes)")


def aes_key_to_hex(key: bytes) -> str:
    """将 16 字节 AES 密钥转为十六进制字符串。"""
    return key.hex()


def aes_key_to_b64(key: bytes) -> str:
    """将 16 字节 AES 密钥转为 base64 编码。

    iLink API 的 aeskey 字段使用 hex-encoded 的 base64 形式：
    base64(hex_string)，即 base64(b'token_hex'.decode('ascii'))
    """
    return base64.b64encode(key.hex().encode("ascii")).decode("ascii")


def check_crypto_available() -> bool:
    """检查 cryptography 库是否可用。"""
    return CRYPTO_AVAILABLE