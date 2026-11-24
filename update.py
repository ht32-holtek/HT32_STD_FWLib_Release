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


def sanitize_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip()


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
    """若 zip 內所有成員都在同一個頂層目錄下，移除該頂層目錄，使檔案直接位於 zip 根目錄。"""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp_dir = Path(tmp_str)
        extract_dir = tmp_dir / "extracted"
        extract_dir.mkdir()

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        entries = list(extract_dir.iterdir())
        # 只有單一頂層資料夾時才攤平（避免把原本就在根目錄的多個檔案/資料夾弄亂）
        if len(entries) == 1 and entries[0].is_dir():
            top_dir = entries[0]
            repacked_path = tmp_dir / "repacked.zip"
            with zipfile.ZipFile(repacked_path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf_out:
                for root, dirs, files in os.walk(top_dir):
                    root_path = Path(root)
                    rel_root = root_path.relative_to(top_dir)
                    # 保留空目錄
                    if not dirs and not files and str(rel_root) != ".":
                        zf_out.writestr(rel_root.as_posix() + "/", "")
                    for file in files:
                        full_file = root_path / file
                        arcname = (rel_root / file).as_posix()
                        zf_out.write(full_file, arcname)

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

        # 1. 若有上傳 .zip 附件 (Assets)，保留原結構下載
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
            # 2. 下載 GitHub Source code zipball，命名為 <Repo>-<Tag>.zip 並攤平頂層目錄
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
    print(f"\n全部完成！檔案儲存於: {OUTPUT_DIR.resolve()}")


if __name__ == "__main__":
    main()