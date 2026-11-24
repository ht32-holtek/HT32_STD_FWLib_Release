import hashlib
import os
import re
import shutil
import tempfile
import urllib.request
import urllib.error
import json
import zipfile
from pathlib import Path

OWNER = "ht32-holtek"
REPOS = [
    "HT32_STD_1xxxx_FWLib",
    "HT32_STD_4xxxx_FWLib",
    "HT32_STD_5xxxx_FWLib",
    "HT32_STD_7xxxx_FWLib",
    "HT32_STD_L5xxxx_FWLib",
]

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
OUTPUT_DIR = Path(".")
INDEX_FILE = OUTPUT_DIR / "index.txt"

# RAW 網址設定（若這批 zip 會放進某個 Repo 的子資料夾，請填例如 "releases/fwlib"，否則留空 ""）
RAW_SUBPATH = ""
GITHUB_REPO = "HT32_STD_FWLib_Release"
GITEE_REPO = "HT32_STD_FWLib_Release"
GITHUB_BRANCH = "main"
GITEE_BRANCH = "master"

GITHUB_RAW_BASE = f"https://raw.githubusercontent.com/{OWNER}/{GITHUB_REPO}/{GITHUB_BRANCH}"
GITEE_RAW_BASE = f"https://gitee.com/{OWNER}/{GITEE_REPO}/raw/{GITEE_BRANCH}"


def sanitize_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip()


def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes:,} B"
    elif size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.2f} KB ({size_bytes:,} bytes)"
    elif size_bytes < 1024 ** 3:
        return f"{size_bytes / (1024 ** 2):.2f} MB ({size_bytes:,} bytes)"
    else:
        return f"{size_bytes / (1024 ** 3):.2f} GB ({size_bytes:,} bytes)"


def calculate_sha256(file_path: Path) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def build_raw_urls(filename: str) -> tuple[str, str]:
    clean_sub = RAW_SUBPATH.strip("/")
    rel_path = f"{clean_sub}/{filename}" if clean_sub else filename
    gh_url = f"{GITHUB_RAW_BASE.rstrip('/')}/{rel_path}"
    gitee_url = f"{GITEE_RAW_BASE.rstrip('/')}/{rel_path}"
    return gh_url, gitee_url


def extract_release_date_from_zip(zip_path: Path) -> str:
    """從 zip 內部的 Release_Notes.txt 擷取 Release 日期。"""
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            target_members = [
                name for name in zf.namelist()
                if not name.endswith("/") and Path(name).name.lower() == "release_notes.txt"
            ]
            if not target_members:
                return "N/A (Release_Notes.txt not found)"

            target_members.sort(key=lambda p: len(Path(p).parts))
            raw_bytes = zf.read(target_members[0])

            text = ""
            for enc in ("utf-8-sig", "utf-8", "cp950", "big5", "latin-1"):
                try:
                    text = raw_bytes.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue

            if not text:
                return "N/A (decode failed)"

            patterns = [
                r"\b(20\d{2}[-/.](?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12]\d|3[01]))\b",
                r"\b(20\d{2}\s*年\s*(?:0?[1-9]|1[0-2])\s*月\s*(?:0?[1-9]|[12]\d|3[01])\s*日?)\b",
                r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+20\d{2})\b",
            ]

            lines = text.splitlines()
            for line in lines:
                if re.search(r"(release\s*date|date|日期)", line, re.IGNORECASE):
                    for pat in patterns:
                        m = re.search(pat, line, re.IGNORECASE)
                        if m:
                            return m.group(1).strip()

            for pat in patterns:
                m = re.search(pat, text, re.IGNORECASE)
                if m:
                    return m.group(1).strip()

            return "N/A (date not matched)"
    except Exception as e:
        return f"N/A ({e})"


def generate_index_txt(target_dir: Path, index_path: Path):
    """掃描 target_dir 下的 .zip 檔案並輸出 index.txt。"""
    zip_files = sorted(
        [p for p in target_dir.iterdir() if p.is_file() and p.suffix.lower() == ".zip"],
        key=lambda p: p.name.lower(),
    )

    lines = []
    for zf_path in zip_files:
        size_bytes = zf_path.stat().st_size
        sha256_val = calculate_sha256(zf_path)
        gh_url, gitee_url = build_raw_urls(zf_path.name)
        rel_date = extract_release_date_from_zip(zf_path)

        lines.append(f"File: {zf_path.name}")
        lines.append(f"  GitHub RAW : {gh_url}")
        lines.append(f"  Gitee RAW  : {gitee_url}")
        lines.append(f"  Size       : {format_size(size_bytes)}")
        lines.append(f"  SHA256     : {sha256_val}")
        lines.append(f"  ReleaseDate: {rel_date}")
        lines.append("-" * 60)

    index_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    print(f"\n已產生 {index_path.resolve()}（共 {len(zip_files)} 個檔案）")


def api_get(url: str):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "HT32-Release-Downloader",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def flatten_zip_top_dir(zip_path: Path):
    """直接從 zip 串流移除單一頂層資料夾，使檔案直接位於 zip 根目錄。"""
    with tempfile.TemporaryDirectory() as tmp_str:
        repacked_path = Path(tmp_str) / "repacked.zip"

        with zipfile.ZipFile(zip_path, "r") as zf_in:
            names = zf_in.namelist()
            top_dirs = {n.split("/", 1)[0] for n in names if "/" in n}
            root_files = [n for n in names if "/" not in n and n]

            if len(top_dirs) == 1 and not root_files:
                prefix = next(iter(top_dirs)) + "/"
                with zipfile.ZipFile(repacked_path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf_out:
                    for item in zf_in.infolist():
                        if item.filename == prefix:
                            continue
                        new_name = item.filename[len(prefix):] if item.filename.startswith(prefix) else item.filename
                        if not new_name:
                            continue
                        new_info = zipfile.ZipInfo(filename=new_name, date_time=item.date_time)
                        new_info.compress_type = item.compress_type
                        new_info.external_attr = item.external_attr
                        zf_out.writestr(new_info, zf_in.read(item.filename))

                shutil.move(str(repacked_path), str(zip_path))


def download_zip(url: str, dest_dir: Path, filename: str, flatten: bool = False):
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_filename = sanitize_name(filename)
    dest_path = dest_dir / safe_filename

    if dest_path.exists() and dest_path.stat().st_size > 0:
        print(f"    [跳過] 已存在: {dest_path}")
        return

    headers = {"User-Agent": "HT32-Release-Downloader"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    req = urllib.request.Request(url, headers=headers)
    temp_path = dest_dir / f".tmp_{safe_filename}.part"

    try:
        with urllib.request.urlopen(req, timeout=120) as resp, open(temp_path, "wb") as out:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                out.write(chunk)

        temp_path.replace(dest_path)

        if flatten:
            flatten_zip_top_dir(dest_path)

        print(f"    [完成] {dest_path} ({dest_path.stat().st_size:,} bytes)")
    except Exception as e:
        if temp_path.exists():
            temp_path.unlink()
        print(f"    [失敗] {safe_filename}: {e}")


def fetch_all_releases(repo: str) -> list:
    releases = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{OWNER}/{repo}/releases?per_page=100&page={page}"
        batch = api_get(url)
        if not batch:
            break
        releases.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return releases


def process_repo(repo: str):
    print(f"\n=== 處理倉庫: {OWNER}/{repo} ===")
    try:
        releases = fetch_all_releases(repo)
    except urllib.error.HTTPError as e:
        print(f"  [錯誤] 無法取得 releases (HTTP {e.code}): {e.reason}")
        return
    except Exception as e:
        print(f"  [錯誤] 無法取得 releases: {e}")
        return

    print(f"  共找到 {len(releases)} 個發行版")
    for rel in releases:
        tag = rel.get("tag_name") or rel.get("name") or "unknown_tag"
        safe_tag = sanitize_name(tag)
        desired_zip = f"{repo}-{safe_tag}.zip"
        print(f"  -> Release: {tag}")

        zip_assets = [
            a for a in rel.get("assets", [])
            if a.get("name", "").lower().endswith(".zip")
        ]
        if zip_assets:
            for asset in zip_assets:
                asset_zip = asset["name"] if len(zip_assets) == 1 else f"{repo}-{safe_tag}-{asset['name']}"
                download_zip(
                    asset["browser_download_url"],
                    OUTPUT_DIR,
                    asset_zip,
                    flatten=False,
                )
        elif rel.get("zipball_url"):
            download_zip(
                rel["zipball_url"],
                OUTPUT_DIR,
                desired_zip,
                flatten=True,
            )


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for repo in REPOS:
        process_repo(repo)
    generate_index_txt(OUTPUT_DIR, INDEX_FILE)
    print(f"\n全部完成！檔案儲存於: {OUTPUT_DIR.resolve()}")


if __name__ == "__main__":
    main()