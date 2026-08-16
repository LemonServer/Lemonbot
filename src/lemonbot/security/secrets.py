from __future__ import annotations

import ctypes
import os
from abc import ABC, abstractmethod
from ctypes import wintypes
from dataclasses import dataclass


class SecretStoreError(RuntimeError):
    pass


class SecretNotFound(SecretStoreError):
    pass


class SecretStore(ABC):
    @abstractmethod
    def get(self, name: str) -> str | None: ...

    @abstractmethod
    def set(self, name: str, value: str) -> None: ...

    @abstractmethod
    def delete(self, name: str) -> bool: ...

    def require(self, name: str) -> str:
        value = self.get(name)
        if value is None:
            raise SecretNotFound(f"required credential is not configured: {name}")
        return value


def _validate_name(name: str) -> None:
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789_-"
    if not name or len(name) > 128 or any(ch not in allowed for ch in name):
        raise ValueError("secret names must contain only lowercase letters, digits, '_' or '-'")


@dataclass(slots=True)
class NamespacedSecretStore(SecretStore):
    inner: SecretStore
    namespace: str

    def _name(self, name: str) -> str:
        _validate_name(name)
        return f"{self.namespace}_{name}"

    def get(self, name: str) -> str | None:
        return self.inner.get(self._name(name))

    def set(self, name: str, value: str) -> None:
        self.inner.set(self._name(name), value)

    def delete(self, name: str) -> bool:
        return self.inner.delete(self._name(name))


class EnvironmentSecretStore(SecretStore):
    """Explicit test/development store; production never selects this implicitly."""

    def __init__(self, *, allow: bool = False, prefix: str = "LEMONBOT_SECRET_") -> None:
        if not allow:
            raise SecretStoreError("environment credentials require an explicit development opt-in")
        self._prefix = prefix

    def _key(self, name: str) -> str:
        _validate_name(name)
        return f"{self._prefix}{name.upper()}"

    def get(self, name: str) -> str | None:
        return os.environ.get(self._key(name))

    def set(self, name: str, value: str) -> None:
        raise SecretStoreError("environment store is read-only")

    def delete(self, name: str) -> bool:
        raise SecretStoreError("environment store is read-only")


if os.name == "nt":
    LPBYTE = ctypes.POINTER(wintypes.BYTE)

    class _CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", LPBYTE),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    _PCREDENTIALW = ctypes.POINTER(_CREDENTIALW)


class WindowsCredentialStore(SecretStore):
    """Stores UTF-8 secrets as Windows Generic Credentials."""

    _TYPE_GENERIC = 1
    _PERSIST_LOCAL_MACHINE = 2
    _ERROR_NOT_FOUND = 1168
    _MAX_BLOB_BYTES = 5120

    def __init__(self, *, prefix: str = "Lemonbot") -> None:
        if os.name != "nt":
            raise SecretStoreError("Windows Credential Manager is only available on Windows")
        self._prefix = prefix
        self._advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._advapi32.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(_PCREDENTIALW),
        ]
        self._advapi32.CredReadW.restype = wintypes.BOOL
        self._advapi32.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
        self._advapi32.CredWriteW.restype = wintypes.BOOL
        self._advapi32.CredDeleteW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self._advapi32.CredDeleteW.restype = wintypes.BOOL
        self._advapi32.CredFree.argtypes = [ctypes.c_void_p]
        self._advapi32.CredFree.restype = None

    def _target(self, name: str) -> str:
        _validate_name(name)
        return f"{self._prefix}:{name}"

    def get(self, name: str) -> str | None:
        pointer = _PCREDENTIALW()
        if not self._advapi32.CredReadW(
            self._target(name), self._TYPE_GENERIC, 0, ctypes.byref(pointer)
        ):
            error = ctypes.get_last_error()
            if error == self._ERROR_NOT_FOUND:
                return None
            raise SecretStoreError(f"CredReadW failed with Windows error {error}")
        try:
            credential = pointer.contents
            blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
            return blob.decode("utf-8")
        finally:
            self._advapi32.CredFree(pointer)

    def set(self, name: str, value: str) -> None:
        if not value:
            raise ValueError("refusing to store an empty credential")
        blob = value.encode("utf-8")
        if len(blob) > self._MAX_BLOB_BYTES:
            raise ValueError("credential is too large for Windows Credential Manager")
        buffer = ctypes.create_string_buffer(blob)
        credential = _CREDENTIALW()
        credential.Type = self._TYPE_GENERIC
        credential.TargetName = self._target(name)
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = ctypes.cast(buffer, LPBYTE)
        credential.Persist = self._PERSIST_LOCAL_MACHINE
        credential.UserName = "Lemonbot"
        if not self._advapi32.CredWriteW(ctypes.byref(credential), 0):
            raise SecretStoreError(
                f"CredWriteW failed with Windows error {ctypes.get_last_error()}"
            )

    def delete(self, name: str) -> bool:
        if self._advapi32.CredDeleteW(self._target(name), self._TYPE_GENERIC, 0):
            return True
        error = ctypes.get_last_error()
        if error == self._ERROR_NOT_FOUND:
            return False
        raise SecretStoreError(f"CredDeleteW failed with Windows error {error}")
