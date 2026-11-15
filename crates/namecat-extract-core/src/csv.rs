//! CSV 导出：UTF-8 BOM + RFC4180 转义，保证 Windows Excel 正确识别中文。

use crate::model::PlanItem;

const BOM: &[u8] = &[0xEF, 0xBB, 0xBF];

const HEADERS: &[&str] = &[
    "文件名",
    "来源目录",
    "原始路径",
    "目标路径",
    "文件大小(字节)",
    "操作状态",
    "错误信息",
    "是否自动改名",
    "是否覆盖",
    "备份路径",
];

fn cell(value: &str) -> String {
    let needs = value.contains(',') || value.contains('"')
        || value.contains('\n') || value.contains('\r');
    if !needs {
        return value.to_string();
    }
    // 字段内换行统一成 CRLF（与行分隔符一致，Excel 里能正确显示为单元格内换行）。
    let flat = value
        .replace("\r\n", "\n")
        .replace('\r', "\n")
        .replace('\n', "\r\n");
    format!("\"{}\"", flat.replace('"', "\"\""))
}

/// 导出计划清单或执行结果为 CSV 文本（不含 BOM，BOM 由 `to_bytes` 加）。
/// 直接以 CRLF 写行：旧实现先 `writeln!` 出 \n 再对整串做两次全量 replace，
/// 上万行时会额外产生两份完整字符串拷贝（内存峰值约 3 倍）。
pub fn export(items: &[PlanItem]) -> String {
    let mut out = String::with_capacity(256 + items.len() * 200);
    out.push_str(&HEADERS.iter().map(|h| cell(h)).collect::<Vec<_>>().join(","));
    out.push_str("\r\n");
    for it in items {
        let row = [
            it.name.clone(),
            it.source_dir.to_string_lossy().into_owned(),
            it.source.to_string_lossy().into_owned(),
            it.target.to_string_lossy().into_owned(),
            it.size.to_string(),
            it.status.label().to_string(),
            it.message.clone(),
            if it.renamed { "是" } else { "否" }.to_string(),
            if it.overwritten { "是" } else { "否" }.to_string(),
            it.backup
                .as_ref()
                .map(|p| p.to_string_lossy().into_owned())
                .unwrap_or_default(),
        ];
        out.push_str(&row.iter().map(|v| cell(v)).collect::<Vec<_>>().join(","));
        out.push_str("\r\n");
    }
    out
}

/// 带 UTF-8 BOM 的字节（写文件用）。
pub fn to_bytes(items: &[PlanItem]) -> Vec<u8> {
    let mut out = Vec::with_capacity(BOM.len() + 1024);
    out.extend_from_slice(BOM);
    out.extend_from_slice(export(items).as_bytes());
    out
}

/// 直接写到路径（绑定层的 `export_csv` 调这个）。
pub fn export_to_path(items: &[PlanItem], path: &std::path::Path) -> crate::model::Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(path, to_bytes(items))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::FileStatus;
    use std::path::PathBuf;

    fn item() -> PlanItem {
        PlanItem {
            index: 0,
            name: "a,b\".txt".into(),
            source: PathBuf::from(r"C:\root\sub\a.txt"),
            source_dir: PathBuf::from(r"C:\root\sub"),
            target: PathBuf::from(r"C:\root\a.txt"),
            target_dir: PathBuf::from(r"C:\root"),
            size: 12,
            depth: 2,
            status: FileStatus::Done,
            message: "已完成\n第二行".into(),
            renamed: false,
            overwritten: false,
            backup: None,
        }
    }

    #[test]
    fn bom_is_first_three_bytes() {
        assert_eq!(&to_bytes(&[])[..3], BOM);
    }

    #[test]
    fn special_chars_are_escaped() {
        let text = export(&[item()]);
        assert!(text.contains("\"a,b\"\".txt\""));
        assert!(text.contains("\"已完成\r\n第二行\""));
        assert!(text.ends_with("\r\n"));
    }

    #[test]
    fn headers_include_required_columns() {
        let text = export(&[]);
        for h in HEADERS {
            assert!(text.contains(h), "缺列：{h}");
        }
    }
}
