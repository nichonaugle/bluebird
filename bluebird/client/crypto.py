import os
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x448 import X448PrivateKey, X448PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from bluebird.util import CurveType
from typing import Union, Tuple

class ClientExchangeHandler:
    """
    A class for handling cryptographic operations, including key generation, 
    shared key derivation, and message encryption using X25519 and AES-GCM or X448 and AES-GCM.

    This class supports both X25519 and X448 curves, which have different key sizes and payload sizes.
    - X25519: Public key size is 32 bytes.
    - X448: Public key size is 56 bytes.

    Note: The payload size will vary depending on the curve used.
    """

    def __init__(self, curve_type: CurveType):
        """
        Initializes the ExchangeHandler with the specified curve type. The curve type 
        determines the cryptographic curve (X25519 or X448) to be used for key generation 
        and cryptographic operations.

        Args:
            curve_type (CurveType): The type of curve to use (CurveType.CURVE25519 or CurveType.CURVE448).

        Raises:
            ValueError: If an unsupported curve type is provided.
        """
        self.curve_type = curve_type
        self.private_key = None
        self.public_key = None
        if self.curve_type == CurveType.CURVE25519:
            self.private_curve_type = X25519PrivateKey
            self.public_curve_type = X25519PublicKey
        elif self.curve_type == CurveType.CURVE448:
            self.private_curve_type = X448PrivateKey
            self.public_curve_type = X448PublicKey
        else:
            raise ValueError("Unsupported CurveType. Select either X25519 or X448")

    def generate_key_pair(self) -> Tuple[Union[X25519PrivateKey, X448PrivateKey], bytes]:
        """
        Generates a new key pair based on the specified curve type.

        Returns:
            tuple[Union[X25519PrivateKey, X448PrivateKey], bytes]: A tuple containing the private key and the raw bytes of the public key.
            
            - For CurveType.CURVE25519: The public key size is 32 bytes.
            - For CurveType.CURVE448: The public key size is 56 bytes.
        """
        self.private_key = self.private_curve_type.generate()
        self.public_key = self.private_key.public_key().public_bytes_raw()
        return self.private_key, self.public_key

    def derive_shared_key(self, ext_public_key: bytes) -> bytes:
        """
        Derives a shared key using the provided private key and an external public key.

        This function performs a key exchange using the specified curve algorithm (X25519 or X448) 
        to derive a shared secret key.

        Args:
            ext_public_key (bytes): The external public key in bytes format.

        Returns:
            bytes: The derived shared key.

        Raises:
            ValueError: If the private key has not been generated.
        """
        if self.private_key is None:
            raise ValueError("Server private key not generated. Must use generate_key_pair() first.")

        return self.private_key.exchange(self.public_curve_type.from_public_bytes(ext_public_key))
    
    def encrypt_msg(self, shared_key: bytes, msg: bytes) -> tuple[bytes, bytes]:
        """
        Encrypts a message using a shared key with AES-GCM.

        Args:
            shared_key (bytes): The shared key used for encryption.
            msg (bytes): The message to be encrypted.

        Returns:
            tuple[bytes, bytes]: A tuple containing the nonce and the encrypted message.
        """
        derived_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=b'handshake data'
        ).derive(shared_key)

        aesgcm = AESGCM(key=derived_key)
        nonce = os.urandom(12)  # never reuse nonce key combo
        encrypted_msg = aesgcm.encrypt(nonce, msg, None)  # Tag is last 16 bytes
        return nonce, encrypted_msg

    def create_encrypted_payload_from_key_pair(self, shared_key: bytes, msg: str, key_pair: Tuple[Union[X25519PrivateKey, X448PrivateKey], bytes]) -> bytes:
        """
        Wrapper for other functions to create of the payload from generated key pair.

        Payload size (disregarding varying message byte size):
        - For CurveType.CURVE25519: public key (32) + nonce (12) + ciphertext (Max: MTU Size bytes - 44 bytes, Min: 44 bytes)
        - For CurveType.CURVE448: public key (56) + nonce (12) + ciphertext (Max: MTU Size bytes - 68 bytes, Min: 68 bytes)
        
        Args:
            shared_key (bytes): The shared key used for encryption.
            msg (bytes): The message to be encrypted.
            key_pair (tuple): Output of "generate_key_pair" function

        Returns:
            payload (bytes): A payload with ECDH public key, nonce, and encrypted message to send to server.
        """

        nonce, encrypted_msg = self.encrypt_msg(shared_key, str.encode(msg))
        payload = self.public_key + nonce + encrypted_msg
        return payload

    def create_encrypted_payload(self, msg: str, ext_public_key: bytes) -> bytes:
        """
        Wrapper for other functions to streamline creation of the payload.

        Payload size (disregarding varying message byte size):
        - For CurveType.CURVE25519: public key (32) + nonce (12) + ciphertext (Max: MTU Size bytes - 44 bytes, Min: 44 bytes)
        - For CurveType.CURVE448: public key (56) + nonce (12) + ciphertext (Max: MTU Size bytes - 68 bytes, Min: 68 bytes)
        
        Args:
            msg (bytes): The message to be encrypted.
            ext_public_key (bytes): The external public key in bytes format.

        Returns:
            payload (bytes): A payload with ECDH public key, nonce, and encrypted message to send to server.
        """
        self.generate_key_pair()
        shared_key = self.derive_shared_key(ext_public_key)
        nonce, encrypted_msg = self.encrypt_msg(shared_key, str.encode(msg))
        payload = self.public_key + nonce + encrypted_msg
        return payload