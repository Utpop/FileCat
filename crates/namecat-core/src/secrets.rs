//! 本地密钥的存取：Windows 用 DPAPI 加密，其它平台退化成 Base64。
//!
//! 为什么单独一个模块（沿用 Python 版注释的结论）
//!     供应商配置（地址、模型、请求头）可以随便备份、可以分享；
//!     **密钥**不行。两者必须分开存：配置文件可分享，密钥文件不能。
//!
//! 磁盘格式（必须与旧版本互相认）
//!     `DPAPI1:` + CryptProtectData 密文      ← Windows 正常路径
//!     `BASE64:` + base64(utf-8 明文)         ← 兜底（换机器/换账号解不开时）
//!     其它前缀 → 一律当"读不到"，返回空串（绝不抛异常）
//!
//! 约定
//!     · `save_secret("")` 是**空操作**（不写盘、也不清空）：界面「留空沿用
//!       已保存的密钥」靠的就是它；要清空必须显式 `clear_secret()`。
//!     · 任何函数都不打印、不返回明文给日志；出错一律返回 false / 空串。
//!
//! 跨实现的坑（P4 记一笔）
//!     CryptProtectData 的 `szDataDescr` 不参与密钥派生，只影响"谁加密的"这类
//!     元信息。为了和旧版本写出的文件**互相**能解，这里仍然原样传 "NameCat"。

use std::path::Path;

pub const SECRET_FILE: &str = "llm_credential.dat";

const DPAPI_PREFIX: &[u8] = b"DPAPI1:";
const B64_PREFIX: &[u8] = b"BASE64:";

/// 保存密钥。空串 = 保持原样不动。返回是否真的写盘了。
pub fn save_secret(path: &str, text: &str) -> bool {
    if text.is_empty() {
        return false;
    }
    let raw = text.as_bytes();
    let payload = match protect(raw) {
        Some(blob) => [DPAPI_PREFIX, blob.as_slice()].concat(),
        None => [B64_PREFIX, b64_encode(raw).as_bytes()].concat(),
    };
    if let Some(parent) = Path::new(path).parent() {
        if !parent.as_os_str().is_empty() && std::fs::create_dir_all(parent).is_err() {
            return false;
        }
    }
    std::fs::write(path, payload).is_ok()
}

/// 读回密钥；读不到（没有文件 / 解不开 / 不是 UTF-8）一律返回空串。
pub fn load_secret(path: &str) -> String {
    let payload = match std::fs::read(path) {
        Ok(bytes) => bytes,
        Err(_) => return String::new(),
    };
    let raw = if payload.starts_with(DPAPI_PREFIX) {
        unprotect(&payload[DPAPI_PREFIX.len()..])
    } else if payload.starts_with(B64_PREFIX) {
        b64_decode(&payload[B64_PREFIX.len()..])
    } else {
        None
    };
    match raw {
        Some(bytes) => String::from_utf8(bytes).unwrap_or_default(),
        None => String::new(),
    }
}

/// 删掉已保存的密钥。文件本来就不存在也算成功（与 Python 版一致）。
pub fn clear_secret(path: &str) -> bool {
    if Path::new(path).exists() {
        std::fs::remove_file(path).is_ok()
    } else {
        true
    }
}

pub fn has_secret(path: &str) -> bool {
    !load_secret(path).is_empty()
}

// --------------------------------------------------------------------------- //
// DPAPI
// --------------------------------------------------------------------------- //

#[cfg(windows)]
fn protect(raw: &[u8]) -> Option<Vec<u8>> {
    use windows_sys::Win32::Foundation::LocalFree;
    use windows_sys::Win32::Security::Cryptography::{
        CRYPT_INTEGER_BLOB, CryptProtectData, CRYPTPROTECT_UI_FORBIDDEN,
    };

    let descr: Vec<u16> = "NameCat\0".encode_utf16().collect();
    let mut input = CRYPT_INTEGER_BLOB {
        cbData: raw.len() as u32,
        pbData: raw.as_ptr() as *mut u8,
    };
    let mut output = CRYPT_INTEGER_BLOB::default();
    // SAFETY: 两个 BLOB 都指向本函数栈上的有效缓冲；描述串以 NUL 结尾；
    // 输出缓冲由 DPAPI 用 LocalAlloc 分配，读完立刻 LocalFree（不跨调用）。
    unsafe {
        let ok = CryptProtectData(
            &mut input,
            descr.as_ptr(),
            std::ptr::null(),
            std::ptr::null(),
            std::ptr::null(),
            CRYPTPROTECT_UI_FORBIDDEN,
            &mut output,
        );
        if ok == 0 {
            return None;
        }
        let blob = std::slice::from_raw_parts(output.pbData, output.cbData as usize).to_vec();
        LocalFree(output.pbData as _);
        Some(blob)
    }
}

#[cfg(windows)]
fn unprotect(blob: &[u8]) -> Option<Vec<u8>> {
    use windows_sys::Win32::Foundation::LocalFree;
    use windows_sys::Win32::Security::Cryptography::{
        CRYPT_INTEGER_BLOB, CryptUnprotectData, CRYPTPROTECT_UI_FORBIDDEN,
    };

    if blob.is_empty() {
        return None;
    }
    let mut input = CRYPT_INTEGER_BLOB {
        cbData: blob.len() as u32,
        pbData: blob.as_ptr() as *mut u8,
    };
    let mut output = CRYPT_INTEGER_BLOB::default();
    // SAFETY: 同 protect。`szDataDescr` 传空指针（Python 版解密的也是 None）。
    unsafe {
        let ok = CryptUnprotectData(
            &mut input,
            std::ptr::null_mut(),
            std::ptr::null(),
            std::ptr::null(),
            std::ptr::null(),
            CRYPTPROTECT_UI_FORBIDDEN,
            &mut output,
        );
        if ok == 0 {
            return None;
        }
        let raw = std::slice::from_raw_parts(output.pbData, output.cbData as usize).to_vec();
        LocalFree(output.pbData as _);
        Some(raw)
    }
}

#[cfg(not(windows))]
fn protect(_raw: &[u8]) -> Option<Vec<u8>> {
    None // 非 Windows 一律退化成 Base64（防君子不防小人，注释里写清楚）
}

#[cfg(not(windows))]
fn unprotect(_blob: &[u8]) -> Option<Vec<u8>> {
    None
}

// --------------------------------------------------------------------------- //
// Base64（标准字母表 + 填充）——只为不引一个新依赖，20 行足够
// --------------------------------------------------------------------------- //

const B64_ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

pub fn b64_encode(raw: &[u8]) -> String {
    let mut out = String::with_capacity((raw.len() + 2) / 3 * 4);
    for chunk in raw.chunks(3) {
        let b = [
            chunk[0],
            *chunk.get(1).unwrap_or(&0),
            *chunk.get(2).unwrap_or(&0),
        ];
        let n = ((b[0] as u32) << 16) | ((b[1] as u32) << 8) | b[2] as u32;
        out.push(B64_ALPHABET[(n >> 18) as usize & 63] as char);
        out.push(B64_ALPHABET[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 {
            B64_ALPHABET[(n >> 6) as usize & 63] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            B64_ALPHABET[n as usize & 63] as char
        } else {
            '='
        });
    }
    out
}

/// 解码。与 `base64.b64decode` 的默认口径一致：忽略字母表外的字符，
/// 填充不足/多余一律返回 None（调用方当"读不到"）。
pub fn b64_decode(text: &[u8]) -> Option<Vec<u8>> {
    let mut bits: u32 = 0;
    let mut have: u32 = 0;
    let mut out: Vec<u8> = Vec::with_capacity(text.len() / 4 * 3 + 3);
    let mut padding = 0usize;
    let mut symbols = 0usize;
    for &byte in text {
        if byte == b'=' {
            padding += 1;
            continue;
        }
        let value = match byte {
            b'A'..=b'Z' => byte - b'A',
            b'a'..=b'z' => byte - b'a' + 26,
            b'0'..=b'9' => byte - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            b'\r' | b'\n' | b' ' | b'\t' => continue,
            _ => return None,
        } as u32;
        if padding > 0 {
            return None; // 填充后面不该再出现有效字符
        }
        bits = (bits << 6) | value;
        have += 6;
        symbols += 1;
        if have >= 8 {
            have -= 8;
            out.push((bits >> have) as u8);
        }
    }
    // Python 的 binascii 口径：去掉填充后总长必须是 4 的倍数，且填充不超过 2 个
    if padding > 2 || (symbols + padding) % 4 != 0 || have >= 6 {
        return None;
    }
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base64_roundtrip_matches_known_vectors() {
        assert_eq!(b64_encode(b""), "");
        assert_eq!(b64_encode(b"f"), "Zg==");
        assert_eq!(b64_encode(b"fo"), "Zm8=");
        assert_eq!(b64_encode(b"foo"), "Zm9v");
        assert_eq!(b64_encode(b"foobar"), "Zm9vYmFy");
        assert_eq!(b64_encode("密钥".as_bytes()), "5a+G6ZKl");
        for text in ["", "a", "ab", "abc", "sk-测试-123", &"x".repeat(200)] {
            let encoded = b64_encode(text.as_bytes());
            assert_eq!(
                b64_decode(encoded.as_bytes()).unwrap(),
                text.as_bytes().to_vec(),
                "往返失败：{text}"
            );
        }
    }

    #[test]
    fn base64_rejects_garbage() {
        assert!(b64_decode(b"Zm9v!").is_none());
        assert!(b64_decode(b"Zg=").is_none(), "填充不足应失败");
        assert!(b64_decode(b"Z===").is_none());
    }

    /// 测试用临时目录：**不用**系统 Temp（沙箱不允许往那儿写）。
    fn temp_dir(name: &str) -> std::path::PathBuf {
        let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../_verify_tmp/core_tmp")
            .join(name);
        let _ = std::fs::remove_dir_all(&dir);
        dir
    }

    #[test]
    fn empty_secret_is_a_noop_and_never_writes() {
        let dir = temp_dir("secret_t1");
        let _ = std::fs::remove_dir_all(&dir);
        let path = dir.join(SECRET_FILE).to_string_lossy().into_owned();
        assert!(!save_secret(&path, ""));
        assert!(!Path::new(&path).exists(), "空串不该建文件");
        assert_eq!(load_secret(&path), "");
        assert!(!has_secret(&path));
    }

    #[test]
    fn roundtrip_is_not_plaintext_on_disk() {
        let dir = temp_dir("secret_t2");
        let _ = std::fs::remove_dir_all(&dir);
        let path = dir.join("sub").join(SECRET_FILE).to_string_lossy().into_owned();
        assert!(save_secret(&path, "sk-roundtrip-中文"));
        assert_eq!(load_secret(&path), "sk-roundtrip-中文");
        let raw = std::fs::read(&path).unwrap();
        assert!(
            !raw.windows(b"sk-roundtrip".len()).any(|w| w == b"sk-roundtrip"),
            "磁盘上不能是明文"
        );
        assert!(raw.starts_with(b"DPAPI1:") || raw.starts_with(b"BASE64:"));
        // 空串不覆盖
        assert!(!save_secret(&path, ""));
        assert_eq!(load_secret(&path), "sk-roundtrip-中文");
        assert!(clear_secret(&path));
        assert_eq!(load_secret(&path), "");
        assert!(clear_secret(&path), "重复清除也算成功");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn unknown_prefix_reads_as_empty() {
        let dir = temp_dir("secret_t3");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join(SECRET_FILE).to_string_lossy().into_owned();
        std::fs::write(&path, b"PLAIN:sk-leak").unwrap();
        assert_eq!(load_secret(&path), "", "不认识的前缀一律当没存过");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
