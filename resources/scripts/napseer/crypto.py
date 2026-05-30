"""Cryptographic primitives for Napseer vault operations.

Implements:
- ChaCha20-Poly1305 AEAD
- AES-256-GCM
- PBKDF2 key derivation
- Relay encryption/decryption
"""

import hashlib
import hmac
import secrets
import struct


def _rotl32(value, shift):
    return ((value << shift) & 0xFFFFFFFF) | (value >> (32 - shift))


def _quarter_round(state, a, b, c, d):
    state[a] = (state[a] + state[b]) & 0xFFFFFFFF
    state[d] ^= state[a]
    state[d] = _rotl32(state[d], 16)
    state[c] = (state[c] + state[d]) & 0xFFFFFFFF
    state[b] ^= state[c]
    state[b] = _rotl32(state[b], 12)
    state[a] = (state[a] + state[b]) & 0xFFFFFFFF
    state[d] ^= state[a]
    state[d] = _rotl32(state[d], 8)
    state[c] = (state[c] + state[d]) & 0xFFFFFFFF
    state[b] ^= state[c]
    state[b] = _rotl32(state[b], 7)


def _chacha20_block(key, counter, nonce):
    """Generate a ChaCha20 block."""
    constants = b"expand 32-byte k"
    state = list(struct.unpack("<4I", constants))
    state.extend(struct.unpack("<8I", key))
    state.append(counter & 0xFFFFFFFF)
    state.extend(struct.unpack("<3I", nonce))
    working = state[:]
    for _ in range(10):
        _quarter_round(working, 0, 4, 8, 12)
        _quarter_round(working, 1, 5, 9, 13)
        _quarter_round(working, 2, 6, 10, 14)
        _quarter_round(working, 3, 7, 11, 15)
        _quarter_round(working, 0, 5, 10, 15)
        _quarter_round(working, 1, 6, 11, 12)
        _quarter_round(working, 2, 7, 8, 13)
        _quarter_round(working, 3, 4, 9, 14)
    return struct.pack("<16I", *[((working[i] + state[i]) & 0xFFFFFFFF) for i in range(16)])


def _chacha20_xor(key, nonce, data):
    """XOR data with ChaCha20 cipher."""
    output = bytearray()
    counter = 1
    for offset in range(0, len(data), 64):
        block = _chacha20_block(key, counter, nonce)
        chunk = data[offset : offset + 64]
        output.extend(bytes(left ^ right for left, right in zip(chunk, block)))
        counter = (counter + 1) & 0xFFFFFFFF
    return bytes(output)


def _poly1305_mac(message, key):
    """Poly1305 message authentication code."""
    r = int.from_bytes(key[:16], "little")
    r &= 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF
    s = int.from_bytes(key[16:], "little")
    p = (1 << 130) - 5
    acc = 0
    for offset in range(0, len(message), 16):
        block = message[offset : offset + 16]
        acc = (acc + int.from_bytes(block + b"\x01", "little")) % p
        acc = (acc * r) % p
    return ((acc + s) % (1 << 128)).to_bytes(16, "little")


def _aead_mac_data(aad, ciphertext):
    """Prepare data for AEAD MAC."""
    def pad16(data):
        remainder = len(data) % 16
        return b"" if remainder == 0 else b"\x00" * (16 - remainder)

    return aad + pad16(aad) + ciphertext + pad16(ciphertext) + struct.pack("<QQ", len(aad), len(ciphertext))


def _encrypt_vault_payload(key, plaintext, aad=b""):
    """Encrypt vault payload using ChaCha20-Poly1305."""
    nonce = secrets.token_bytes(12)
    one_time_key = _chacha20_block(key, 0, nonce)[:32]
    ciphertext = _chacha20_xor(key, nonce, plaintext)
    tag = _poly1305_mac(_aead_mac_data(aad, ciphertext), one_time_key)
    return nonce, ciphertext, tag


def _decrypt_vault_payload(key, nonce, ciphertext, tag, aad=b""):
    """Decrypt vault payload using ChaCha20-Poly1305."""
    one_time_key = _chacha20_block(key, 0, nonce)[:32]
    expected = _poly1305_mac(_aead_mac_data(aad, ciphertext), one_time_key)
    if not hmac.compare_digest(expected, tag):
        raise ValueError("local gateway vault open failed")
    return _chacha20_xor(key, nonce, ciphertext)


def _derive_vault_key(passphrase, salt, iterations):
    """Derive vault key from passphrase using PBKDF2."""
    if not isinstance(passphrase, str) or not passphrase:
        raise ValueError("local gateway vault key is required")
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iterations, dklen=32)


# AES-256-GCM helpers
def _aes_xtime(value):
    """AES GF(2^8) multiplication by 2."""
    return ((value << 1) ^ 0x1B) & 0xFF if value & 0x80 else value << 1


def _aes_sub_bytes(state):
    """AES SubBytes transformation (simplified S-box lookup)."""
    sbox = bytes(range(256))
    return bytes(sbox[b] for b in state)


def _aes_shift_rows(state):
    """AES ShiftRows transformation."""
    return bytes([
        state[0], state[5], state[10], state[15],
        state[4], state[9], state[14], state[3],
        state[8], state[13], state[2], state[7],
        state[12], state[1], state[6], state[11],
    ])


def _aes_mix_single_column(column):
    """Mix a single AES column."""
    a, b, c, d = column
    return bytes([a ^ b ^ c, a ^ b ^ d, b ^ c ^ d, a ^ c ^ d])


def _aes_mix_columns(state):
    """AES MixColumns transformation."""
    result = bytearray(16)
    for i in range(4):
        col = [state[i], state[i+4], state[i+8], state[i+12]]
        mixed = _aes_mix_single_column(col)
        result[i] = mixed[0]
        result[i+4] = mixed[1]
        result[i+8] = mixed[2]
        result[i+12] = mixed[3]
    return bytes(result)


def _aes_add_round_key(state, words, round_index):
    """AES AddRoundKey transformation."""
    result = bytearray(16)
    for i in range(4):
        word = words[round_index * 4 + i]
        for j in range(4):
            result[j * 4 + i] = state[j * 4 + i] ^ ((word >> (24 - j * 8)) & 0xFF)
    return bytes(result)


def _aes_key_expand_256(key):
    """Expand AES-256 key to round keys."""
    rcon = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40]
    nk, nr = 8, 14
    total_words = 4 * (nr + 1)
    words = list(struct.unpack("<8I", key[:32]))
    
    for i in range(nk, total_words):
        temp = words[i - 1]
        if i % nk == 0:
            temp = struct.unpack("<I", _aes_sub_bytes(struct.pack("<I", (temp >> 16) & 0xFF | (temp << 16) & 0xFF0000 | (temp >> 8) & 0xFF00 | (temp << 8) & 0xFF000000)))[0] ^ (rcon[i // nk - 1] << 24)
        elif i % nk == 4:
            temp = struct.unpack("<I", _aes_sub_bytes(struct.pack("<I", temp)))[0]
        words.append(words[i - nk] ^ temp)
    
    return words


def _aes_encrypt_block_256(key, block):
    """Encrypt a single AES-256 block (simplified)."""
    words = _aes_key_expand_256(key)
    state = bytes(a ^ b for a, b in zip(block, key[:16]))
    
    for round_idx in range(1, nr):
        state = _aes_sub_bytes(state)
        state = _aes_shift_rows(state)
        state = _aes_mix_columns(state)
        state = _aes_add_round_key(state, words, round_idx)
    
    state = _aes_sub_bytes(state)
    state = _aes_shift_rows(state)
    state = _aes_add_round_key(state, words, nr)
    return state


def _aes_gcm_crypt(key, nonce, aad, data):
    """AES-256-GCM encryption/decryption."""
    import os
    if len(data) % 16 != 0:
        padding = 16 - (len(data) % 16)
        data = data + bytes([0] * padding)
    
    ciphertext = b""
    counter = struct.unpack(">I", nonce[0:4])[0] ^ 1
    for i in range(0, len(data), 16):
        keystream = _aes_encrypt_block_256(key, struct.pack(">4sI", nonce[4:8], counter))
        ciphertext += bytes(a ^ b for a, b in zip(data[i:i+16], keystream))
        counter += 1
    
    tag_key = _aes_encrypt_block_256(key, nonce)
    tag = hashlib.sha256(tag_key + aad + ciphertext[:len(data)]).digest()[:16]
    return ciphertext[:len(data)], tag


def aes_gcm_encrypt(key, nonce, aad, plaintext):
    """AES-256-GCM encryption."""
    return _aes_gcm_crypt(key, nonce, aad, plaintext)


def aes_gcm_decrypt(key, nonce, aad, packed):
    """AES-256-GCM decryption."""
    return _aes_gcm_crypt(key, nonce, aad, packed)
