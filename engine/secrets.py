# -*- coding: utf-8 -*-
"""
engine.secrets —— 本地密钥的存取（只给界面用，不进日志、不进预设）。

为什么单独一个模块
    供应商配置（地址、模型、请求头）是可以随便备份的普通设置，
    但**密钥**不是。两者必须分开存：配置文件可以分享，密钥文件不能。

怎么存
    Windows：用 DPAPI（CryptProtectData）加密，只有当前 Windows 账号能解开，
             别的账号 / 别的机器拿到文件也读不出明文。
    其它平台：退化成 Base64，只是防君子不防小人，注释里写清楚。

约定
    · 存的是空串 = 不动已保存的密钥（界面上的「留空会保留已保存的密钥」）；
    · 任何函数都不打印、不返回明文给日志；出错一律返回 False / 空串。
"""

from __future__ import annotations

import base64
import os

from . import appdata_dir

SECRET_FILE = "llm_credential.dat"


def secret_path() -> str:
    return appdata_dir(SECRET_FILE)


# --------------------------------------------------------------------------- #
# Windows DPAPI
# --------------------------------------------------------------------------- #

def _dpapi_available() -> bool:
    return os.name == "nt"


def _dpapi_protect(raw: bytes) -> bytes | None:
    """用 DPAPI 加密；失败返回 None（调用方决定要不要退回弱方案）。"""
    if not _dpapi_available():
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        buf = ctypes.create_string_buffer(raw, len(raw))
        in_blob = DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        out_blob = DATA_BLOB()

        crypt32.CryptProtectData.restype = wintypes.BOOL
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(DATA_BLOB),
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]

        if not crypt32.CryptProtectData(ctypes.byref(in_blob), "NameCat", None,
                                        None, None, 0x01, ctypes.byref(out_blob)):
            return None
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel32.LocalFree(out_blob.pbData)
    except Exception:
        return None


def _dpapi_unprotect(blob: bytes) -> bytes | None:
    if not _dpapi_available():
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        buf = ctypes.create_string_buffer(blob, len(blob))
        in_blob = DATA_BLOB(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        out_blob = DATA_BLOB()

        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.POINTER(DATA_BLOB),
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]

        if not crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None,
                                          None, None, 0x01, ctypes.byref(out_blob)):
            return None
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel32.LocalFree(out_blob.pbData)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #

def save_secret(text: str) -> bool:
    """保存密钥。空串 = 保持原样不动。返回是否真的写盘了。"""
    text = str(text or "")
    if not text:
        return False
    raw = text.encode("utf-8")
    blob = _dpapi_protect(raw)
    if blob is not None:
        payload = b"DPAPI1:" + blob
    else:
        payload = b"BASE64:" + base64.b64encode(raw)
    path = secret_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(payload)
        return True
    except OSError:
        return False


def load_secret() -> str:
    """读回密钥；读不到（没有文件 / 解不开）一律返回空串，绝不抛异常。"""
    path = secret_path()
    try:
        with open(path, "rb") as fh:
            payload = fh.read()
    except OSError:
        return ""
    try:
        if payload.startswith(b"DPAPI1:"):
            raw = _dpapi_unprotect(payload[7:])
        elif payload.startswith(b"BASE64:"):
            raw = base64.b64decode(payload[7:])
        else:
            raw = None
    except Exception:
        raw = None
    if raw is None:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def clear_secret() -> bool:
    """删掉已保存的密钥（用户主动点「清除密钥」时才用）。"""
    path = secret_path()
    try:
        if os.path.exists(path):
            os.remove(path)
        return True
    except OSError:
        return False


def has_secret() -> bool:
    return bool(load_secret())


__all__ = ["SECRET_FILE", "secret_path", "save_secret", "load_secret",
           "clear_secret", "has_secret"]
