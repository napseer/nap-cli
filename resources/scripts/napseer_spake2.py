import base64
import hashlib
import hmac
import json
import secrets


P256_P = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff
P256_N = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551
P256_A = P256_P - 3
P256_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
P256_G = (
    0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
    0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5,
)
P256_M_COMPRESSED = "02886e2f97ace46e55ba9dd7242579f2993b64e16ef3dcab95afd497333d8fa12f"
P256_N_COMPRESSED = "03d8bbd6c639c62937b04d997f38c3770719c629d7014d49a24b4f98baa1292b49"
SPAKE2_CLIENT_LABEL = "client"
SPAKE2_GATEWAY_LABEL = "gateway"


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def hkdf_sha256(secret, salt, info, length=32):
    if isinstance(salt, str):
        salt = salt.encode("utf-8")
    if isinstance(info, str):
        info = info.encode("utf-8")
    prk = hmac.new(salt, secret, hashlib.sha256).digest()
    output = b""
    previous = b""
    counter = 1
    while len(output) < length:
        previous = hmac.new(prk, previous + info + bytes([counter]), hashlib.sha256).digest()
        output += previous
        counter += 1
    return output[:length]


def int_to_bytes(value, length=32):
    return int(value).to_bytes(length, "big")


def bytes_to_int(value):
    return int.from_bytes(value, "big")


def modp(value):
    return value % P256_P


def invp(value):
    return pow(value % P256_P, P256_P - 2, P256_P)


def point_add(left, right):
    if left is None:
        return right
    if right is None:
        return left
    x1, y1 = left
    x2, y2 = right
    if x1 == x2 and (y1 + y2) % P256_P == 0:
        return None
    if left == right:
        slope = modp((3 * x1 * x1 + P256_A) * invp(2 * y1))
    else:
        slope = modp((y2 - y1) * invp(x2 - x1))
    x3 = modp(slope * slope - x1 - x2)
    return (x3, modp(slope * (x1 - x3) - y1))


def point_negate(point):
    if point is None:
        return None
    return (point[0], (-point[1]) % P256_P)


def scalar_mult(scalar, point):
    result = None
    addend = point
    cursor = scalar % P256_N
    while cursor:
        if cursor & 1:
            result = point_add(result, addend)
        addend = point_add(addend, addend)
        cursor >>= 1
    return result


def decompress_point(hex_value):
    raw = bytes.fromhex(hex_value)
    if len(raw) != 33 or raw[0] not in (2, 3):
        raise RuntimeError("invalid compressed P-256 point encoding")
    prefix = raw[0]
    x = bytes_to_int(raw[1:])
    if x >= P256_P:
        raise RuntimeError("compressed P-256 x coordinate out of range")
    y2 = modp(x * x * x + P256_A * x + P256_B)
    y = pow(y2, (P256_P + 1) // 4, P256_P)
    if modp(y * y - y2) != 0:
        raise RuntimeError("compressed point is not on P-256")
    if (y & 1) != (prefix & 1):
        y = (-y) % P256_P
    return (x, y)


def encode_point(point):
    if point is None:
        raise RuntimeError("invalid SPAKE2 point")
    return (b"\x04" + int_to_bytes(point[0]) + int_to_bytes(point[1])).hex()


def decode_point(hex_value):
    raw = bytes.fromhex(hex_value)
    if len(raw) != 65 or raw[0] != 4:
        raise RuntimeError("invalid SPAKE2 point encoding")
    point = (bytes_to_int(raw[1:33]), bytes_to_int(raw[33:65]))
    if point[0] >= P256_P or point[1] >= P256_P:
        raise RuntimeError("SPAKE2 point coordinate out of range")
    if modp(point[1] * point[1] - (point[0] * point[0] * point[0] + P256_A * point[0] + P256_B)) != 0:
        raise RuntimeError("SPAKE2 point is not on P-256")
    return point


def random_scalar():
    while True:
        scalar = bytes_to_int(secrets.token_bytes(32)) % P256_N
        if scalar:
            return scalar


def password_scalar(secret):
    return bytes_to_int(secret) % (P256_N - 1) + 1


def le64(value):
    return int(value).to_bytes(8, "little")


def len_prefixed(value):
    return le64(len(value)) + value


def _spake2_finish_b_with_labels(secret, client_label, gateway_label, aad, browser_message, scalar_y=None):
    w = password_scalar(secret)
    y = scalar_y or random_scalar()
    p_a = decode_point(browser_message)
    p_b = point_add(scalar_mult(y, P256_G), scalar_mult(w, decompress_point(P256_N_COMPRESSED)))
    k = scalar_mult(y, point_add(p_a, point_negate(scalar_mult(w, decompress_point(P256_M_COMPRESSED)))))
    if k is None:
        raise RuntimeError("invalid SPAKE2 shared point")
    tt = b"".join([
        len_prefixed(client_label.encode("utf-8")),
        len_prefixed(gateway_label.encode("utf-8")),
        len_prefixed(bytes.fromhex(browser_message)),
        len_prefixed(bytes.fromhex(encode_point(p_b))),
        len_prefixed(bytes.fromhex(encode_point(k))),
        len_prefixed(int_to_bytes(w)),
    ])
    digest = hashlib.sha256(tt).digest()
    ke, ka = digest[:16], digest[16:]
    confirmation_keys = hkdf_sha256(ka, b"", b"ConfirmationKeys" + aad, 32)
    kc_a, kc_b = confirmation_keys[:16], confirmation_keys[16:]
    return {
        "message": encode_point(p_b),
        "confirmation": base64.b64encode(hmac.new(kc_b, tt, hashlib.sha256).digest()).decode("ascii"),
        "expected_client_confirmation": base64.b64encode(hmac.new(kc_a, tt, hashlib.sha256).digest()).decode("ascii"),
        "secret": ke,
        "transcript_hash": digest.hex(),
    }


def spake2_finish_b(secret, aad, browser_message, scalar_y=None):
    return _spake2_finish_b_with_labels(
        secret,
        SPAKE2_CLIENT_LABEL,
        SPAKE2_GATEWAY_LABEL,
        aad,
        browser_message,
        scalar_y=scalar_y,
    )


def spake2_gateway_finish(secret, spake2_context, browser_message):
    return spake2_finish_b(
        secret,
        canonical_json(spake2_context).encode("utf-8"),
        browser_message,
    )


def relay_key_from_secret(secret, spake2_context, direction):
    context = canonical_json(spake2_context)
    salt = hashlib.sha256(context.encode("utf-8")).digest()
    return hkdf_sha256(secret, salt, f"napseer-relay-aes-gcm:{direction}")


def relay_context_hash(spake2_context):
    return base64.b64encode(hashlib.sha256(canonical_json(spake2_context).encode("utf-8")).digest()).decode("ascii")


def relay_aad(session_id, context_hash, direction, seq):
    return canonical_json({
        "context_hash": context_hash,
        "direction": direction,
        "seq": seq,
        "session_id": session_id,
    }).encode("utf-8")


def _self_test():
    password = int_to_bytes(
        int("2ee57912099d31560b3a44b1184b9b4866e904c49d12ac5042c97dca461b1a5f", 16) - 1
    )
    message_a = (
        "04a56fa807caaa53a4d28dbb9853b9815c61a411118a6fe516a8798434751470"
        "f9010153ac33d0d5f2047ffdb1a3e42c9b4e6be662766e1eeb4116988ede5f912c"
    )
    scalar_y = int("dcb60106f276b02606d8ef0a328c02e4b629f84f89786af5befb0bc75b6e66be", 16)
    vector = _spake2_finish_b_with_labels(
        password,
        "server",
        "client",
        b"",
        message_a,
        scalar_y=scalar_y,
    )
    assert vector["message"] == (
        "0406557e482bd03097ad0cbaa5df82115460d951e3451962f1eaf4367a420676"
        "d09857ccbc522686c83d1852abfa8ed6e4a1155cf8f1543ceca528afb591a1e0b7"
    )
    assert vector["transcript_hash"] == "0e0672dc86f8e45565d338b0540abe6915bdf72e2b35b5c9e5663168e960a91b"
    assert base64.b64decode(vector["expected_client_confirmation"]).hex() == (
        "58ad4aa88e0b60d5061eb6b5dd93e80d9c4f00d127c65b3b35b1b5281fee38f0"
    )
    assert base64.b64decode(vector["confirmation"]).hex() == (
        "d3e2e547f1ae04f2dbdbf0fc4b79f8ecff2dff314b5d32fe9fcef2fb26dc459b"
    )
    changed_aad = _spake2_finish_b_with_labels(password, "server", "client", b"context", message_a, scalar_y=scalar_y)
    assert changed_aad["transcript_hash"] == vector["transcript_hash"]
    assert changed_aad["confirmation"] != vector["confirmation"]
    try:
        decode_point("04" + ("00" * 64))
    except RuntimeError:
        pass
    else:
        raise AssertionError("invalid point was accepted")


if __name__ == "__main__":
    _self_test()
