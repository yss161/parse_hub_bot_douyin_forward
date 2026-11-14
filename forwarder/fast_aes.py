# -*- coding: utf-8 -*-
"""Telegram MTProto AES-IGE 加速。

Pyrogram/kurigram 在未安装 tgcrypto 时会退回 pyaes（纯 Python AES）。
上传大文件时每个 512KB 分片都要走一次 AES-IGE，49MB 视频可能耗时数十分钟，
并触发 upload.SaveBigFilePart 超时。

tgcrypto 需要编译 C 扩展，新版 Python 常常没有现成 wheel；这里用
cryptography（C 后端）或 pycryptodome 实现同样的 AES-IGE，并在启动时
替换 pyrogram.crypto.aes 的 ige256_encrypt / ige256_decrypt。
只影响加密实现，协议、分片、消息格式完全不变。
"""

from __future__ import annotations

from typing import Any

_BACKEND = ""


def _xor(a: bytes, b: bytes) -> bytes:
    # int 异或在 C 层完成，比逐字节生成器快约 4 倍；AES-IGE 每 16 字节调用一次。
    return (int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).to_bytes(len(a), "big")


def _ige_cryptography(data: bytes, key: bytes, iv: bytes, encrypt: bool) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if len(data) % 16:
        raise ValueError("AES-IGE 数据长度必须是 16 的倍数")
    if len(iv) != 32:
        raise ValueError("AES-IGE IV 必须是 32 字节")

    cipher = Cipher(algorithms.AES(key), modes.ECB())
    ctx = cipher.encryptor() if encrypt else cipher.decryptor()
    iv1, iv2 = iv[:16], iv[16:]
    out = bytearray(len(data))

    for offset in range(0, len(data), 16):
        block = data[offset : offset + 16]
        if encrypt:
            # C_i = E(P_i xor C_{i-1}) xor P_{i-1}
            result = _xor(ctx.update(_xor(block, iv1)), iv2)
            iv1, iv2 = result, block
        else:
            # P_i = D(C_i xor P_{i-1}) xor C_{i-1}
            result = _xor(ctx.update(_xor(block, iv2)), iv1)
            iv1, iv2 = block, result
        out[offset : offset + 16] = result

    return bytes(out)


def _ige_pycryptodome(data: bytes, key: bytes, iv: bytes, encrypt: bool) -> bytes:
    from Crypto.Cipher import AES

    if len(data) % 16:
        raise ValueError("AES-IGE 数据长度必须是 16 的倍数")
    if len(iv) != 32:
        raise ValueError("AES-IGE IV 必须是 32 字节")

    cipher = AES.new(key, AES.MODE_ECB)
    iv1, iv2 = iv[:16], iv[16:]
    out = bytearray(len(data))

    for offset in range(0, len(data), 16):
        block = data[offset : offset + 16]
        if encrypt:
            result = _xor(cipher.encrypt(_xor(block, iv1)), iv2)
            iv1, iv2 = result, block
        else:
            result = _xor(cipher.decrypt(_xor(block, iv2)), iv1)
            iv1, iv2 = block, result
        out[offset : offset + 16] = result

    return bytes(out)


def _install(aes_module: Any, crypt: Any) -> None:
    """把 fast 实现挂到 pyrogram.crypto.aes 模块上（幂等）。"""
    def ige256_encrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
        return crypt(data, key, iv, True)

    def ige256_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
        return crypt(data, key, iv, False)

    def ige(data: bytes, key: bytes, iv: bytes, encrypt: bool) -> bytes:
        return crypt(data, key, iv, encrypt)

    aes_module.ige256_encrypt = ige256_encrypt
    aes_module.ige256_decrypt = ige256_decrypt
    aes_module.ige = ige
    aes_module._forwarder_fast_aes = True


def install_fast_aes() -> str:
    """启用可用的最快 AES-IGE 后端，返回后端名称。

    返回值：
      - tgcrypto      ：已安装 tgcrypto，Pyrogram 本身就会用，无需替换；
      - cryptography  ：已替换为 cryptography 的 C 后端；
      - pycryptodome  ：已替换为 pycryptodome 的 C 后端；
      - pyaes         ：没有可用的 C 后端，仍是纯 Python（大文件会很慢）。
    """
    global _BACKEND
    if _BACKEND:
        return _BACKEND

    try:
        import tgcrypto  # noqa: F401

        _BACKEND = "tgcrypto"
        return _BACKEND
    except Exception:
        pass

    try:
        import pyrogram.crypto.aes as aes_module
    except Exception:
        _BACKEND = "pyaes"
        return _BACKEND

    try:
        _ige_cryptography(b"\x00" * 16, b"\x00" * 32, b"\x00" * 32, True)
        _install(aes_module, _ige_cryptography)
        _BACKEND = "cryptography"
        return _BACKEND
    except Exception:
        pass

    try:
        _ige_pycryptodome(b"\x00" * 16, b"\x00" * 32, b"\x00" * 32, True)
        _install(aes_module, _ige_pycryptodome)
        _BACKEND = "pycryptodome"
        return _BACKEND
    except Exception:
        pass

    _BACKEND = "pyaes"
    return _BACKEND
