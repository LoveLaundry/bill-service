import os
import json
import hashlib
import hmac
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_master_key = os.getenv("MASTER_KEY")
if not _master_key:
    raise RuntimeError("FATAL: MASTER_KEY environment variable is not set. Refusing to start.")
MASTER_KEY_ENV = _master_key

# Derive KEK and HMAC Key
KEK = hashlib.sha256((MASTER_KEY_ENV + "-kek").encode()).digest()
HMAC_KEY = hashlib.sha256((MASTER_KEY_ENV + "-hmac").encode()).digest()


def get_search_token(value: str) -> str:
    """Generate a deterministic, secure search token for exact match queries."""
    if not value:
        return ""
    normalized = value.strip().lower()
    return hmac.new(HMAC_KEY, normalized.encode(), hashlib.sha256).hexdigest()


def encrypt_field(plaintext: str, dek: bytes) -> dict:
    """Encrypt a single string field using AES-256-GCM with a DEK."""
    aesgcm = AESGCM(dek)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return {"ciphertext": ciphertext.hex(), "nonce": nonce.hex()}


def decrypt_field(encrypted_data: dict, dek: bytes) -> str:
    """Decrypt a single string field using AES-256-GCM with a DEK."""
    try:
        aesgcm = AESGCM(dek)
        nonce = bytes.fromhex(encrypted_data["nonce"])
        ciphertext = bytes.fromhex(encrypted_data["ciphertext"])
        decrypted_bytes = aesgcm.decrypt(nonce, ciphertext, None)
        return decrypted_bytes.decode("utf-8")
    except Exception as e:
        raise ValueError(f"Decryption failed: {str(e)}")


def encrypt_dict(data: dict, sensitive_fields: list) -> dict:
    """
    Perform envelope encryption on a flat/nested dict.
    Leaves non-sensitive fields intact, encrypts sensitive fields.
    Wraps a generated DEK using KEK and appends encryption metadata.
    """
    # 1. Generate DEK
    dek = AESGCM.generate_key(bit_length=256)

    # 2. Wrap DEK using KEK
    aesgcm_kek = AESGCM(KEK)
    dek_nonce = os.urandom(12)
    wrapped_dek_bytes = aesgcm_kek.encrypt(dek_nonce, dek, None)
    wrapped_dek = {"ciphertext": wrapped_dek_bytes.hex(), "nonce": dek_nonce.hex()}

    # 3. Encrypt sensitive fields
    encrypted_data = {}
    for key, val in data.items():
        if key in sensitive_fields and val is not None:
            # Serialize to json string if it's not a string (e.g. lists, dicts)
            if not isinstance(val, str):
                val_str = json.dumps(val)
                is_json = True
            else:
                val_str = val
                is_json = False

            enc_field = encrypt_field(val_str, dek)
            enc_field["is_json"] = is_json
            encrypted_data[key] = enc_field

            # Handle searchable fields
            if key == "client_name":
                encrypted_data["client_name_search"] = get_search_token(val_str)
            elif key == "email":
                encrypted_data["email_search"] = get_search_token(val_str)
            elif key == "mobile_number":
                encrypted_data["mobile_number_search"] = get_search_token(val_str)
        else:
            encrypted_data[key] = val

    # 4. Add metadata
    encrypted_data["encryption_metadata"] = {
        "version": 1,
        "algorithm": "AES-256-GCM",
        "keyId": "master-key-v1",
        "wrappedDek": wrapped_dek,
    }
    return encrypted_data


def decrypt_dict(encrypted_data: dict, sensitive_fields: list) -> dict:
    """
    Decrypt envelope-encrypted dict.
    """
    if not encrypted_data:
        return encrypted_data

    if "encryption_metadata" not in encrypted_data:
        # If no metadata, fail/reject
        raise ValueError("Encryption metadata missing: document is not secure.")

    meta = encrypted_data["encryption_metadata"]
    wrapped_dek = meta["wrappedDek"]

    # 1. Unwrap DEK using KEK
    try:
        aesgcm_kek = AESGCM(KEK)
        dek_nonce = bytes.fromhex(wrapped_dek["nonce"])
        wrapped_dek_bytes = bytes.fromhex(wrapped_dek["ciphertext"])
        dek = aesgcm_kek.decrypt(dek_nonce, wrapped_dek_bytes, None)
    except Exception as e:
        raise ValueError(f"Failed to unwrap DEK: {str(e)}")

    # 2. Decrypt sensitive fields
    decrypted_data = {}
    for key, val in encrypted_data.items():
        if key == "encryption_metadata" or key.endswith("_search"):
            continue
        elif (
            key in sensitive_fields
            and isinstance(val, dict)
            and "ciphertext" in val
            and "nonce" in val
        ):
            dec_val_str = decrypt_field(val, dek)
            if val.get("is_json"):
                decrypted_data[key] = json.loads(dec_val_str)
            else:
                decrypted_data[key] = dec_val_str
        else:
            decrypted_data[key] = val

    return decrypted_data
