"""Transactional updater for the camoufox-reverse browser binary."""
from __future__ import annotations

import argparse
import configparser
import csv
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator

REPOSITORY = "WhiteNightShadow/camoufox-reverse"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
ASSET_NAME = "camoufox-reverse-win.x86_64.zip"
DOWNLOADS_DIR_NAME = ".browser-downloads"
USER_AGENT = "camoufox-reverse-mcp-updater/1.0"
MAX_ARCHIVE_BYTES = 1_500_000_000
MAX_EXTRACTED_BYTES = 3_000_000_000
MAX_ARCHIVE_FILES = 100_000
CHUNK_SIZE = 1024 * 1024
CURL_RANGE_PART_BYTES = 8 * 1024 * 1024
CURL_RANGE_WORKERS = 12
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
REQUIRED_ROOT_MARKERS = (
    "camoufox.exe",
    "application.ini",
    "properties.json",
    "version.json",
    "xul.dll",
    "omni.ja",
    "browser",
    "defaults",
)
JOURNAL_PHASES = {
    "starting", "downloading", "extracting", "backing_up",
    "backup_complete", "installing", "validating", "rolling_back",
    "installed", "rolled_back",
}
SENTINEL_NAME = ".camoufox-reverse-root.json"
WORK_OWNER_NAME = ".update-owner.json"
BACKUP_OWNER_NAME = ".backup-owner.json"
BACKUP_COMPLETE_NAME = ".backup-complete.json"
TRANSACTION_ID_RE = re.compile(r"[0-9]+-[0-9]+-[0-9a-f]{16}")
MIN_CAMOUFOX_EXE_BYTES = 100_000
MIN_XUL_DLL_BYTES = 10_000_000
MIN_OMNI_JA_BYTES = 100_000


class UpdateError(RuntimeError):
    """A user-facing update failure."""


@dataclass(frozen=True)
class ReleaseInfo:
    tag: str
    version: str
    release: str
    asset_url: str
    sha256: str
    size: int

    @property
    def version_string(self) -> str:
        return f"{self.version}-{self.release}"


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _is_link_or_reparse(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    )


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _same_file(first: Path, second: Path) -> bool:
    try:
        return os.path.samefile(first, second)
    except (FileNotFoundError, OSError):
        return False


def _assert_no_reparse_components(path: Path, label: str) -> None:
    path = _absolute_without_resolving(path)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.exists() and _is_link_or_reparse(current):
            raise UpdateError(f"{label}不能经过符号链接或 reparse point: {current}")


def mcp_root_from_script() -> Path:
    root = _absolute_without_resolving(Path(__file__)).parents[1]
    if not (root / "pyproject.toml").is_file() or not (root / "launch.bat").is_file():
        raise UpdateError(f"无法从更新脚本定位 MCP 根目录: {root}")
    _assert_no_reparse_components(root, "MCP 根目录")
    return root.resolve(strict=True)


def derive_browser_root(mcp_root: Path, override: str | None = None) -> Path:
    if override:
        root = _absolute_without_resolving(Path(override))
    elif os.environ.get("CAMOUFOX_EXECUTABLE_PATH"):
        root = _absolute_without_resolving(Path(os.environ["CAMOUFOX_EXECUTABLE_PATH"])).parent
    else:
        root = _absolute_without_resolving(mcp_root).parent

    if root == Path(root.anchor) or root == _absolute_without_resolving(Path.home()):
        raise UpdateError(f"拒绝使用危险的浏览器根目录: {root}")
    if not root.is_dir():
        raise UpdateError(f"浏览器根目录不存在: {root}")
    _assert_no_reparse_components(root, "浏览器根目录")
    root = root.resolve(strict=True)
    if root == Path(root.anchor) or _same_file(root, Path.home()):
        raise UpdateError(f"拒绝使用危险的浏览器根目录: {root}")

    mcp_root = _absolute_without_resolving(mcp_root).resolve(strict=True)
    if _same_file(mcp_root, root):
        raise UpdateError("MCP 根目录不能与浏览器根目录相同")
    for ancestor in mcp_root.parents:
        if _same_file(ancestor, root):
            if not _same_file(mcp_root.parent, root):
                raise UpdateError("MCP 位于浏览器根目录的多级子目录中，无法安全保护")
            break
    return root


def validate_browser_root(browser_root: Path) -> None:
    missing = [
        name for name in REQUIRED_ROOT_MARKERS
        if not (browser_root / name).exists()
        or (name in {"browser", "defaults"} and not (browser_root / name).is_dir())
        or (name not in {"browser", "defaults"} and not (browser_root / name).is_file())
    ]
    if missing:
        raise UpdateError(f"目标不是完整的 camoufox-reverse 浏览器目录，缺少: {', '.join(missing)}")
    sentinel = browser_root / SENTINEL_NAME
    if sentinel.exists():
        try:
            data = json.loads(sentinel.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UpdateError(f"浏览器根目录标记损坏: {sentinel}: {exc}") from exc
        if data.get("format") != "camoufox-reverse-root/v1":
            raise UpdateError(f"浏览器根目录标记格式不受支持: {sentinel}")
        if data.get("repository") != REPOSITORY:
            raise UpdateError(f"浏览器根目录标记仓库不匹配: {sentinel}")


def _validate_pe64(path: Path, minimum_size: int) -> None:
    if path.stat().st_size < minimum_size:
        raise UpdateError(f"浏览器二进制大小异常: {path}")
    with path.open("rb") as source:
        header = source.read(64)
        if len(header) < 64 or header[:2] != b"MZ":
            raise UpdateError(f"浏览器二进制不是有效 PE 文件: {path}")
        pe_offset = int.from_bytes(header[0x3C:0x40], "little")
        if pe_offset < 64 or pe_offset > path.stat().st_size - 6:
            raise UpdateError(f"浏览器二进制 PE 头偏移非法: {path}")
        source.seek(pe_offset)
        pe_header = source.read(6)
    if pe_header[:4] != b"PE\0\0" or pe_header[4:6] != b"d\x86":
        raise UpdateError(f"浏览器二进制不是 Windows x86_64 PE: {path}")


def validate_runtime_artifacts(browser_root: Path, release: ReleaseInfo) -> None:
    _validate_pe64(browser_root / "camoufox.exe", MIN_CAMOUFOX_EXE_BYTES)
    _validate_pe64(browser_root / "xul.dll", MIN_XUL_DLL_BYTES)

    application = configparser.ConfigParser(interpolation=None)
    try:
        with (browser_root / "application.ini").open("r", encoding="utf-8") as source:
            application.read_file(source)
    except (OSError, configparser.Error) as exc:
        raise UpdateError(f"无法解析 application.ini: {exc}") from exc
    if (
        application.get("App", "CodeName", fallback="") != "Camoufox"
        or application.get("App", "Version", fallback="") != release.version_string
    ):
        raise UpdateError("application.ini 不是目标 camoufox-reverse 版本")

    try:
        properties = json.loads((browser_root / "properties.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError(f"无法解析 properties.json: {exc}") from exc
    if not isinstance(properties, list) or not any(
        isinstance(item, dict) and item.get("property") == "navigator.userAgent"
        for item in properties
    ):
        raise UpdateError("properties.json 缺少 Camoufox 指纹属性定义")

    omni = browser_root / "omni.ja"
    if omni.stat().st_size < MIN_OMNI_JA_BYTES or not zipfile.is_zipfile(omni):
        raise UpdateError(f"omni.ja 不是有效的运行时归档: {omni}")
    try:
        with zipfile.ZipFile(omni) as bundle:
            names = set(bundle.namelist())
            if "chrome.manifest" not in names or "components/components.manifest" not in names:
                raise UpdateError("omni.ja 缺少必要的 Firefox manifest")
            corrupted = bundle.testzip()
            if corrupted:
                raise UpdateError(f"omni.ja 包含损坏文件: {corrupted}")
    except zipfile.BadZipFile as exc:
        raise UpdateError(f"omni.ja 无法读取: {exc}") from exc

    for directory_name in ("browser", "defaults"):
        directory = browser_root / directory_name
        if not any(path.is_file() for path in directory.rglob("*")):
            raise UpdateError(f"浏览器运行时目录为空: {directory}")


def smoke_test_browser(browser_root: Path, release: ReleaseInfo) -> None:
    try:
        completed = subprocess.run(
            [str(browser_root / "camoufox.exe"), "--version"],
            cwd=browser_root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError(f"目标浏览器无法执行 --version: {exc}") from exc
    output = f"{completed.stdout}\n{completed.stderr}".strip()
    if completed.returncode != 0 or release.version_string not in output:
        raise UpdateError(
            f"目标浏览器启动自检失败: exit={completed.returncode}, output={output!r}"
        )


def resolve_browser_root(mcp_root: Path, override: str | None = None) -> Path:
    root = derive_browser_root(mcp_root, override)
    validate_browser_root(root)
    return root


def protected_child_name(browser_root: Path, mcp_root: Path) -> str | None:
    if not _same_file(mcp_root.parent, browser_root):
        return None
    for child in browser_root.iterdir():
        if _same_file(child, mcp_root):
            return child.name
    raise UpdateError(f"无法在浏览器根目录中按文件身份定位 MCP: {mcp_root}")


# Directories/files that must survive browser binary updates.
PROTECTED_LOCAL_NAMES = frozenset({
    "Profiles",
    ".camoufox-data",
    ".camoufox-runtime",
    "camoufox-reverse-mcp",
})


def protected_entry_names(browser_root: Path, mcp_root: Path) -> set[str]:
    names = {SENTINEL_NAME, *PROTECTED_LOCAL_NAMES}
    child_name = protected_child_name(browser_root, mcp_root)
    if child_name:
        names.add(child_name)
    # Also protect any existing .Camoufox.backup-* / update journal siblings.
    try:
        for child in browser_root.iterdir():
            name = child.name
            if name.startswith((".Camoufox.backup-", ".Camoufox.update-")) or name in {
                ".Camoufox.update.json",
                ".Camoufox.update.mutex",
                ".update-owner.json",
                ".backup-owner.json",
                ".backup-complete.json",
            }:
                names.add(name)
    except OSError:
        pass
    return names


def ensure_install_sentinel(browser_root: Path) -> str:
    sentinel = browser_root / SENTINEL_NAME
    if sentinel.exists():
        validate_browser_root(browser_root)
        data = json.loads(sentinel.read_text(encoding="utf-8"))
        installation_id = data.get("installation_id")
        if isinstance(installation_id, str) and re.fullmatch(r"[0-9a-f]{32}", installation_id):
            return installation_id
    else:
        data = {
            "format": "camoufox-reverse-root/v1",
            "repository": REPOSITORY,
            "created_at": int(time.time()),
        }
    installation_id = secrets.token_hex(16)
    data.update({
        "format": "camoufox-reverse-root/v1",
        "repository": REPOSITORY,
        "installation_id": installation_id,
    })
    _write_json_atomic(sentinel, data)
    return installation_id


def read_installation_id(browser_root: Path) -> str:
    sentinel = browser_root / SENTINEL_NAME
    if not sentinel.is_file():
        raise UpdateError(f"浏览器根目录缺少更新器标记: {sentinel}")
    try:
        data = json.loads(sentinel.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError(f"浏览器根目录标记损坏: {sentinel}: {exc}") from exc
    installation_id = data.get("installation_id")
    if (
        data.get("format") != "camoufox-reverse-root/v1"
        or data.get("repository") != REPOSITORY
        or not isinstance(installation_id, str)
        or not re.fullmatch(r"[0-9a-f]{32}", installation_id)
    ):
        raise UpdateError(f"浏览器根目录标记不能绑定更新事务: {sentinel}")
    return installation_id


def parse_release_tag(tag: str) -> tuple[str, str]:
    value = tag.removeprefix("v")
    # New releases append a reverse build suffix (e.g. v152.0.4-beta.30-reverse.5).
    match = re.fullmatch(r"(.+)-((?:alpha|beta)\.\d+)(?:-reverse\.\d+)?", value)
    if not match:
        raise UpdateError(f"无法解析发布版本标签: {tag}")
    return match.group(1), match.group(2)


def _release_from_version_string(version_string: str) -> ReleaseInfo:
    version, release = parse_release_tag(f"v{version_string}")
    return ReleaseInfo(
        tag=f"v{version_string}",
        version=version,
        release=release,
        asset_url="",
        sha256="",
        size=0,
    )


def _release_api_url(tag: str | None) -> str:
    if not tag:
        return LATEST_RELEASE_URL
    return f"https://api.github.com/repos/{REPOSITORY}/releases/tags/{urllib.parse.quote(tag)}"


def _system_executable(filename: str) -> str | None:
    if os.name != "nt":
        return None
    import ctypes

    buffer = ctypes.create_unicode_buffer(32_768)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if length <= 0 or length >= len(buffer):
        return None
    candidate = Path(buffer.value) / filename
    return str(candidate) if candidate.is_file() and not _is_link_or_reparse(candidate) else None


def _curl_path() -> str | None:
    return _system_executable("curl.exe")


def _require_https(url: str, label: str) -> None:
    if urllib.parse.urlsplit(url).scheme.lower() != "https":
        raise UpdateError(f"{label}必须使用 HTTPS: {url}")


def _curl_get_bytes(url: str, accept: str) -> bytes:
    _require_https(url, "请求 URL")
    curl = _curl_path()
    if curl is None:
        raise UpdateError("curl.exe 不可用")
    try:
        completed = subprocess.run(
            [
                curl, "--disable", "--location", "--fail", "--silent", "--show-error",
                "--retry", "3", "--retry-delay", "2",
                "--connect-timeout", "30", "--max-time", "60",
                "--max-filesize", "5000000",
                "--proto", "=https", "--proto-redir", "=https",
                "--header", f"Accept: {accept}",
                "--header", f"User-Agent: {USER_AGENT}",
                url,
            ],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", b"")
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace").strip()
        raise UpdateError(f"curl 请求失败: {detail or exc}") from exc
    return completed.stdout


def fetch_release(
    tag: str | None = None,
    *,
    urlopen: Callable = urllib.request.urlopen,
) -> ReleaseInfo:
    url = _release_api_url(tag)
    try:
        if urlopen is urllib.request.urlopen and _curl_path():
            payload = json.loads(_curl_get_bytes(url, "application/vnd.github+json"))
        else:
            request = urllib.request.Request(
                url,
                headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
            )
            with urlopen(request, timeout=30) as response:
                body = response.read(5_000_001)
                if len(body) > 5_000_000:
                    raise UpdateError("GitHub 发布信息响应过大")
                payload = json.loads(body)
    except (OSError, urllib.error.URLError, json.JSONDecodeError, UpdateError) as exc:
        raise UpdateError(f"查询 GitHub 发布信息失败: {exc}") from exc

    release_tag = str(payload.get("tag_name") or "")
    if tag is not None and release_tag != tag:
        raise UpdateError(f"GitHub 返回了非请求版本: requested={tag}, actual={release_tag}")
    version, release = parse_release_tag(release_tag)
    asset = next(
        (item for item in payload.get("assets", []) if item.get("name") == ASSET_NAME),
        None,
    )
    asset_name = ASSET_NAME
    if asset is None:
        # Newer releases use a versioned asset name (camoufox-<ver>-<rel>-win.x86_64.zip).
        asset_name = f"camoufox-{version}-{release}-win.x86_64.zip"
        asset = next(
            (item for item in payload.get("assets", []) if item.get("name") == asset_name),
            None,
        )
    if asset is None:
        raise UpdateError(
            f"发布 {release_tag} 不包含 Windows x86_64 资产 {ASSET_NAME} 或 {asset_name}"
        )

    digest = str(asset.get("digest") or "")
    if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
        raise UpdateError(f"发布资产缺少可信 SHA-256: {asset_name}")
    size = int(asset.get("size") or 0)
    if size <= 0 or size > MAX_ARCHIVE_BYTES:
        raise UpdateError(f"发布资产大小异常: {size} bytes")
    asset_url = str(asset["browser_download_url"])
    _require_https(asset_url, "发布资产 URL")
    return ReleaseInfo(
        tag=release_tag,
        version=version,
        release=release,
        asset_url=asset_url,
        sha256=digest.split(":", 1)[1].lower(),
        size=size,
    )


def read_current_version(browser_root: Path) -> str | None:
    version_file = browser_root / "version.json"
    if not version_file.is_file():
        return None
    try:
        data = json.loads(version_file.read_text(encoding="utf-8"))
        return f"{data['version']}-{data['release']}"
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return None


def downloads_dir(mcp_root: Path) -> Path:
    return mcp_root / DOWNLOADS_DIR_NAME


def release_archive_cache_path(mcp_root: Path, release: ReleaseInfo) -> Path:
    safe_tag = re.sub(r"[^0-9A-Za-z._-]+", "_", release.tag)
    return downloads_dir(mcp_root) / safe_tag / ASSET_NAME


def archive_is_in_download_cache(archive: Path, mcp_root: Path) -> bool:
    return _is_relative_to(
        archive.resolve(strict=False),
        downloads_dir(mcp_root).resolve(strict=False),
    )


def remove_cached_archive(archive: Path, mcp_root: Path) -> None:
    if not archive_is_in_download_cache(archive, mcp_root):
        return
    try:
        archive.unlink(missing_ok=True)
    except OSError as exc:
        print(f"更新包已安装但删除失败，请手动清理: {archive}: {exc}", file=sys.stderr)
        return
    cache_root = downloads_dir(mcp_root).resolve(strict=False)
    current = archive.parent.resolve(strict=False)
    while current != cache_root and _is_relative_to(current, cache_root):
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent.resolve(strict=False)


def ensure_release_archive(release: ReleaseInfo, archive: Path) -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.exists():
        try:
            _verify_download(archive, release)
            print(f"复用已下载更新包: {archive}")
            return
        except UpdateError as exc:
            print(f"已下载更新包校验失败，重新下载: {exc}", file=sys.stderr)
            archive.unlink(missing_ok=True)
    download_asset(release, archive)


def _range_workers() -> int:
    raw = os.environ.get("CAMOUFOX_REVERSE_DOWNLOAD_WORKERS", "").strip()
    try:
        value = int(raw) if raw else CURL_RANGE_WORKERS
    except ValueError:
        value = CURL_RANGE_WORKERS
    return max(1, min(value, 16))


def _curl_download_range(
    curl: str,
    release: ReleaseInfo,
    part_path: Path,
    start: int,
    end: int,
) -> int:
    part_path.unlink(missing_ok=True)
    subprocess.run(
        [
            curl, "--disable", "--location", "--fail", "--silent", "--show-error",
            "--retry", "3", "--retry-delay", "2",
            "--connect-timeout", "30", "--max-time", "1800",
            "--range", f"{start}-{end}",
            "--proto", "=https", "--proto-redir", "=https",
            "--header", "Accept: application/octet-stream",
            "--header", f"User-Agent: {USER_AGENT}",
            "--output", str(part_path),
            release.asset_url,
        ],
        check=True,
    )
    expected = end - start + 1
    actual = part_path.stat().st_size
    if actual != expected:
        part_path.unlink(missing_ok=True)
        raise UpdateError(f"分片大小不匹配: {part_path.name}: expected={expected}, actual={actual}")
    return actual


def _download_asset_with_curl_ranges(release: ReleaseInfo, destination: Path, curl: str) -> None:
    workers = _range_workers()
    parts: list[tuple[int, int, Path]] = []
    for index, start in enumerate(range(0, release.size, CURL_RANGE_PART_BYTES)):
        end = min(start + CURL_RANGE_PART_BYTES - 1, release.size - 1)
        parts.append((start, end, destination.with_name(f"{destination.name}.part-{index:04d}")))

    print(
        f"启用 curl 分片下载: {len(parts)} parts, workers={workers}, "
        f"size={release.size / 1024 / 1024:.1f} MiB"
    )
    completed = 0
    received = 0
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_curl_download_range, curl, release, part_path, start, end): part_path
                for start, end, part_path in parts
            }
            for future in as_completed(futures):
                received += future.result()
                completed += 1
                print(
                    f"分片下载进度: {completed}/{len(parts)} "
                    f"({received / 1024 / 1024:.1f} MiB)"
                )

        digest = hashlib.sha256()
        with destination.open("xb") as output:
            for _start, _end, part_path in parts:
                with part_path.open("rb") as source:
                    while chunk := source.read(CHUNK_SIZE):
                        digest.update(chunk)
                        output.write(chunk)
                part_path.unlink(missing_ok=True)
            output.flush()
            os.fsync(output.fileno())
        _verify_download(destination, release, received=received, actual_digest=digest.hexdigest())
    except Exception:
        destination.unlink(missing_ok=True)
        for _start, _end, part_path in parts:
            part_path.unlink(missing_ok=True)
        raise


def download_asset(
    release: ReleaseInfo,
    destination: Path,
    *,
    urlopen: Callable = urllib.request.urlopen,
) -> None:
    _require_https(release.asset_url, "发布资产 URL")
    curl = _curl_path()
    if urlopen is urllib.request.urlopen and curl:
        try:
            subprocess.run(
                [
                    curl, "--disable", "--location", "--fail", "--show-error",
                    "--retry", "3", "--retry-delay", "2",
                    "--connect-timeout", "30", "--max-time", "14400",
                    "--max-filesize", str(min(MAX_ARCHIVE_BYTES, release.size)),
                    "--proto", "=https", "--proto-redir", "=https",
                    "--header", "Accept: application/octet-stream",
                    "--header", f"User-Agent: {USER_AGENT}",
                    "--output", str(destination),
                    release.asset_url,
                ],
                check=True,
            )
            _verify_download(destination, release)
            return
        except (OSError, subprocess.CalledProcessError, UpdateError) as exc:
            destination.unlink(missing_ok=True)
            print(f"curl 下载失败，尝试 curl 分片下载: {exc}", file=sys.stderr)
            try:
                _download_asset_with_curl_ranges(release, destination, curl)
                return
            except (OSError, subprocess.CalledProcessError, UpdateError) as range_exc:
                destination.unlink(missing_ok=True)
                print(f"curl 分片下载失败，改用 Python 流式下载: {range_exc}", file=sys.stderr)

    request = urllib.request.Request(
        release.asset_url,
        headers={"Accept": "application/octet-stream", "User-Agent": USER_AGENT},
    )
    digest = hashlib.sha256()
    received = 0
    last_percent = -1
    try:
        with urlopen(request, timeout=60) as response, destination.open("xb") as output:
            final_url = response.geturl() if hasattr(response, "geturl") else release.asset_url
            _require_https(final_url, "下载重定向 URL")
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                received += len(chunk)
                if received > release.size or received > MAX_ARCHIVE_BYTES:
                    raise UpdateError("下载内容超过发布元数据声明的大小")
                digest.update(chunk)
                output.write(chunk)
                percent = int(received * 100 / release.size)
                if percent != last_percent and (percent % 5 == 0 or percent == 100):
                    print(f"下载进度: {percent}% ({received / 1024 / 1024:.1f} MiB)")
                    last_percent = percent
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise

    _verify_download(destination, release, received=received, actual_digest=digest.hexdigest())


def _verify_download(
    destination: Path,
    release: ReleaseInfo,
    *,
    received: int | None = None,
    actual_digest: str | None = None,
) -> None:
    if received is None or actual_digest is None:
        digest = hashlib.sha256()
        received = 0
        with destination.open("rb") as source:
            while chunk := source.read(CHUNK_SIZE):
                received += len(chunk)
                if received > MAX_ARCHIVE_BYTES:
                    destination.unlink(missing_ok=True)
                    raise UpdateError("下载内容超过允许的最大大小")
                digest.update(chunk)
        actual_digest = digest.hexdigest()
    if received != release.size:
        destination.unlink(missing_ok=True)
        raise UpdateError(f"下载大小不匹配: expected={release.size}, actual={received}")
    if actual_digest != release.sha256:
        destination.unlink(missing_ok=True)
        raise UpdateError(
            f"SHA-256 校验失败: expected={release.sha256}, actual={actual_digest}"
        )


def _safe_archive_path(name: str) -> PurePosixPath:
    normalized = name.replace("\\", "/")
    if "\x00" in normalized:
        raise UpdateError("ZIP 包含 NUL 路径")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise UpdateError(f"ZIP 包含路径穿越: {name}")
    devices = {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
    devices.update(f"COM{i}" for i in range(1, 10))
    devices.update(f"LPT{i}" for i in range(1, 10))
    for part in path.parts:
        base_name = part.split(".", 1)[0].upper()
        if (
            ":" in part
            or part.endswith((" ", "."))
            or any(ord(char) < 32 for char in part)
            or base_name in devices
        ):
            raise UpdateError(f"ZIP 包含 Windows 危险路径: {name}")
    return path


def safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    total_size = 0
    with zipfile.ZipFile(archive) as bundle:
        infos = bundle.infolist()
        if len(infos) > MAX_ARCHIVE_FILES:
            raise UpdateError(f"ZIP 文件数量过多: {len(infos)}")
        for info in infos:
            relative = _safe_archive_path(info.filename)
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(unix_mode):
                raise UpdateError(f"ZIP 不允许符号链接: {info.filename}")
            total_size += info.file_size
            if total_size > MAX_EXTRACTED_BYTES:
                raise UpdateError("ZIP 解压后总大小超过限制")
            target = destination.joinpath(*relative.parts)
            if not _is_relative_to(target.resolve(strict=False), destination.resolve()):
                raise UpdateError(f"ZIP 路径越界: {info.filename}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=CHUNK_SIZE)


def locate_payload_root(extracted_root: Path) -> Path:
    if (extracted_root / "camoufox.exe").is_file():
        return extracted_root
    candidates = [path.parent for path in extracted_root.rglob("camoufox.exe")]
    if len(candidates) != 1:
        raise UpdateError(f"ZIP 中应有且仅有一个 camoufox.exe，实际找到 {len(candidates)} 个")
    return candidates[0]


def validate_payload(payload_root: Path, release: ReleaseInfo, protected_names: set[str]) -> None:
    validate_browser_root(payload_root)
    _assert_no_reparse_tree(payload_root)
    for protected_name in protected_names:
        if (payload_root / protected_name).exists():
            raise UpdateError(f"更新包试图覆盖受保护目录: {protected_name}")

    packaged_version = read_current_version(payload_root)
    if packaged_version != release.version_string:
        raise UpdateError(
            f"更新包版本不匹配: tag={release.version_string}, package={packaged_version}"
        )
    validate_runtime_artifacts(payload_root, release)


def validate_installed_browser(browser_root: Path, release: ReleaseInfo) -> None:
    validate_browser_root(browser_root)
    installed_version = read_current_version(browser_root)
    if installed_version != release.version_string:
        raise UpdateError(
            f"更新后的版本不匹配: expected={release.version_string}, actual={installed_version}"
        )
    validate_runtime_artifacts(browser_root, release)


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _assert_no_reparse_tree(root: Path, excluded_names: set[str] | None = None) -> None:
    excluded_names = excluded_names or set()
    if _is_link_or_reparse(root):
        raise UpdateError(f"目录根节点不能是链接或 reparse point: {root}")
    for current_root, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        current = Path(current_root)
        if current == root:
            directory_names[:] = [name for name in directory_names if name not in excluded_names]
        for name in [*directory_names, *file_names]:
            path = current / name
            if _is_link_or_reparse(path):
                raise UpdateError(f"浏览器目录包含不允许的链接或 reparse point: {path}")


def _copy_item(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination, symlinks=False)
    else:
        shutil.copy2(source, destination)


def _tree_inventory(root: Path, excluded_names: set[str], *, sync_files: bool) -> dict[str, dict]:
    inventory: dict[str, dict] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        relative = path.relative_to(root)
        if relative.parts[0] in excluded_names:
            continue
        name = relative.as_posix()
        if path.is_dir():
            inventory[name] = {"type": "directory"}
            continue
        digest = hashlib.sha256()
        with path.open("r+b" if sync_files else "rb") as source:
            while chunk := source.read(CHUNK_SIZE):
                digest.update(chunk)
            if sync_files:
                os.fsync(source.fileno())
        inventory[name] = {
            "type": "file",
            "size": path.stat().st_size,
            "sha256": digest.hexdigest(),
        }
    return inventory


def _clear_browser_payload(browser_root: Path, protected_names: set[str]) -> None:
    _assert_no_reparse_tree(browser_root, protected_names)
    for child in list(browser_root.iterdir()):
        if child.name not in protected_names:
            _remove_path(child)


def _backup_browser(
    browser_root: Path,
    backup_root: Path,
    protected_names: set[str],
) -> dict[str, dict]:
    backup_root.mkdir(parents=True, exist_ok=False)
    transaction_id = backup_root.name.split(".backup-", 1)[-1]
    if not TRANSACTION_ID_RE.fullmatch(transaction_id):
        raise UpdateError(f"备份目录事务 ID 非法: {backup_root}")
    _write_json_atomic(backup_root / BACKUP_OWNER_NAME, {
        "format": "camoufox-reverse-backup-owner/v1",
        "transaction_id": transaction_id,
    })
    _assert_no_reparse_tree(browser_root, protected_names)
    for child in list(browser_root.iterdir()):
        if child.name in protected_names:
            continue
        _copy_item(child, backup_root / child.name)
    _assert_no_reparse_tree(backup_root)
    inventory = _tree_inventory(
        backup_root,
        {BACKUP_OWNER_NAME, BACKUP_COMPLETE_NAME},
        sync_files=True,
    )
    _write_json_atomic(backup_root / BACKUP_COMPLETE_NAME, {
        "format": "camoufox-reverse-backup/v1",
        "transaction_id": transaction_id,
        "inventory": inventory,
    })
    return inventory


def _validate_owned_directory(path: Path, marker_name: str, expected_format: str, transaction_id: str) -> None:
    if _is_link_or_reparse(path):
        raise UpdateError(f"事务目录根节点不能是链接或 reparse point: {path}")
    marker = path / marker_name
    if not marker.is_file():
        raise UpdateError(f"事务目录缺少所有权标记: {marker}")
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError(f"事务目录所有权标记损坏: {marker}: {exc}") from exc
    if data.get("format") != expected_format or data.get("transaction_id") != transaction_id:
        raise UpdateError(f"事务目录所有权不匹配: {path}")


def _validate_tree_inventory(
    root: Path,
    excluded_names: set[str],
    expected_inventory: object,
    label: str,
) -> dict[str, dict]:
    if not isinstance(expected_inventory, dict):
        raise UpdateError(f"{label}完整性清单格式非法")
    actual_inventory = _tree_inventory(root, excluded_names, sync_files=False)
    if actual_inventory != expected_inventory:
        raise UpdateError(f"{label}内容与完整性清单不匹配: {root}")
    return expected_inventory


def _validate_backup(backup_root: Path, transaction_id: str | None = None) -> dict[str, dict]:
    transaction_id = transaction_id or backup_root.name.split(".backup-", 1)[-1]
    _assert_no_reparse_tree(backup_root)
    _validate_owned_directory(
        backup_root,
        BACKUP_OWNER_NAME,
        "camoufox-reverse-backup-owner/v1",
        transaction_id,
    )
    marker = backup_root / BACKUP_COMPLETE_NAME
    if not marker.is_file():
        raise UpdateError(f"备份不完整，缺少完成标记: {marker}")
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError(f"备份完成标记损坏: {marker}: {exc}") from exc
    if (
        data.get("format") != "camoufox-reverse-backup/v1"
        or data.get("transaction_id") != transaction_id
    ):
        raise UpdateError(f"备份格式不受支持: {marker}")
    return _validate_tree_inventory(
        backup_root,
        {BACKUP_OWNER_NAME, BACKUP_COMPLETE_NAME},
        data.get("inventory"),
        "备份",
    )


def _restore_backup(
    browser_root: Path,
    backup_root: Path,
    *,
    protected_names: set[str],
) -> dict[str, dict]:
    backup_inventory = _validate_backup(backup_root)
    _clear_browser_payload(browser_root, protected_names)
    for child in list(backup_root.iterdir()):
        if child.name not in {BACKUP_OWNER_NAME, BACKUP_COMPLETE_NAME}:
            _copy_item(child, browser_root / child.name)
    validate_browser_root(browser_root)
    _sync_tree(browser_root, protected_names)
    _validate_tree_inventory(
        browser_root,
        protected_names,
        backup_inventory,
        "已恢复浏览器",
    )
    return backup_inventory


def apply_payload(
    browser_root: Path,
    mcp_root: Path,
    payload_root: Path,
    backup_root: Path,
    release: ReleaseInfo,
    *,
    phase_callback: Callable[..., None] | None = None,
    prevalidated: bool = False,
) -> None:
    protected_names = protected_entry_names(browser_root, mcp_root)
    protected_names.add(backup_root.name)
    if not prevalidated:
        validate_payload(payload_root, release, protected_names)
        smoke_test_browser(payload_root, release)
    phase = "backing_up"

    try:
        if phase_callback:
            phase_callback(phase)
        backup_inventory = _backup_browser(browser_root, backup_root, protected_names)

        phase = "backup_complete"
        if phase_callback:
            phase_callback(phase, backup_inventory=backup_inventory)

        phase = "installing"
        if phase_callback:
            phase_callback(phase)
        _clear_browser_payload(browser_root, protected_names)
        for child in list(payload_root.iterdir()):
            if child.name in protected_names:
                raise UpdateError(f"更新包包含受保护目录: {child.name}")
            _replace_path(child, browser_root / child.name)

        phase = "validating"
        if phase_callback:
            phase_callback(phase)
        validate_installed_browser(browser_root, release)
        if not mcp_root.is_dir():
            raise UpdateError(f"更新后 MCP 根目录不存在: {mcp_root}")
        protected_child_name(browser_root, mcp_root)
        _sync_tree(browser_root, protected_names)
    except Exception:
        if phase in {"backup_complete", "installing", "validating"}:
            if phase_callback:
                phase_callback("rolling_back")
            _restore_backup(
                browser_root,
                backup_root,
                protected_names=protected_names,
            )
            if phase_callback:
                phase_callback("rolled_back")
            _discard_directory(backup_root)
        elif phase == "backing_up":
            _discard_directory(backup_root)
        raise


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_tree(root: Path, excluded_names: set[str]) -> None:
    _assert_no_reparse_tree(root, excluded_names)
    for child in root.iterdir():
        if child.name in excluded_names:
            continue
        paths = [child] if child.is_file() else child.rglob("*")
        for path in paths:
            if path.is_file():
                with path.open("r+b") as source:
                    os.fsync(source.fileno())
    _sync_directory(root)


def _replace_path(source: Path, destination: Path, *, replace: bool = True) -> None:
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        move_file = kernel32.MoveFileExW
        move_file.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        move_file.restype = ctypes.c_int
        flags = 0x8 | (0x1 if replace else 0)
        if not move_file(str(source), str(destination), flags):
            raise ctypes.WinError(ctypes.get_last_error())
        return
    os.replace(source, destination)
    _sync_directory(destination.parent)


def _write_json_atomic(path: Path, data: dict) -> None:
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
        finally:
            os.close(descriptor)
        _replace_path(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _discard_directory(path: Path) -> None:
    if not path.exists():
        return
    if _is_link_or_reparse(path):
        raise UpdateError(f"拒绝清理链接或 reparse point: {path}")
    tombstone = path.parent / f".{path.name}.discard-{secrets.token_hex(16)}"
    _replace_path(path, tombstone, replace=False)
    shutil.rmtree(tombstone, ignore_errors=True)


def _transaction_paths(browser_root: Path, transaction_id: str) -> tuple[Path, Path]:
    if not TRANSACTION_ID_RE.fullmatch(transaction_id):
        raise UpdateError(f"更新事务 ID 非法: {transaction_id!r}")
    return (
        browser_root / f".{browser_root.name}.update-{transaction_id}",
        browser_root / f".{browser_root.name}.backup-{transaction_id}",
    )


def recover_stale_update(journal_path: Path, browser_root: Path, mcp_root: Path) -> None:
    if not journal_path.exists():
        return
    try:
        data = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError(f"存在无法解析的更新 journal，请人工检查: {journal_path}: {exc}") from exc
    if data.get("format") != "camoufox-reverse-update/v1":
        raise UpdateError(f"更新 journal 格式不受支持: {journal_path}")
    if data.get("installation_id") != read_installation_id(browser_root):
        raise UpdateError(f"更新 journal 不属于当前浏览器安装: {journal_path}")
    phase = str(data.get("phase") or "")
    if phase not in JOURNAL_PHASES:
        raise UpdateError(f"更新 journal 包含未知阶段，拒绝自动处理: {phase!r}")
    transaction_id = str(data.get("transaction_id") or "")
    work_root, backup_root = _transaction_paths(browser_root, transaction_id)
    protected_names = protected_entry_names(browser_root, mcp_root)
    print(f"检测到中断的更新事务，正在恢复: phase={phase}")

    def set_recovery_state(new_phase: str, **changes) -> None:
        nonlocal phase
        phase = new_phase
        data.update(changes, phase=new_phase)
        _write_json_atomic(journal_path, data)

    if work_root.exists():
        _validate_owned_directory(
            work_root,
            WORK_OWNER_NAME,
            "camoufox-reverse-work/v1",
            transaction_id,
        )
    if backup_root.exists():
        _validate_owned_directory(
            backup_root,
            BACKUP_OWNER_NAME,
            "camoufox-reverse-backup-owner/v1",
            transaction_id,
        )
        if phase in {"starting", "downloading", "extracting"}:
            raise UpdateError(f"更新阶段不应存在备份目录，拒绝自动删除: {backup_root}")

    if phase in {"backup_complete", "installing", "validating", "rolling_back"}:
        if not backup_root.is_dir():
            raise UpdateError(f"中断事务缺少不可变备份，拒绝继续: {backup_root}")
        backup_inventory = _restore_backup(
            browser_root,
            backup_root,
            protected_names=protected_names,
        )
        set_recovery_state("rolled_back", backup_inventory=backup_inventory)
    elif phase == "installed":
        target_version = str(data.get("target_version") or "")
        try:
            target_release = _release_from_version_string(target_version)
            validate_installed_browser(browser_root, target_release)
            _validate_tree_inventory(
                browser_root,
                protected_names,
                data.get("payload_inventory"),
                "已安装浏览器",
            )
            if backup_root.exists() and data.get("keep_backup"):
                _validate_backup(backup_root, transaction_id)
        except UpdateError:
            if not backup_root.is_dir():
                raise
            print("已安装浏览器未通过完整性校验，正在恢复旧浏览器")
            backup_inventory = _restore_backup(
                browser_root,
                backup_root,
                protected_names=protected_names,
            )
            set_recovery_state("rolled_back", backup_inventory=backup_inventory)
    elif phase == "rolled_back":
        if backup_root.exists():
            backup_inventory = _validate_backup(backup_root, transaction_id)
            try:
                _validate_tree_inventory(
                    browser_root,
                    protected_names,
                    backup_inventory,
                    "已恢复浏览器",
                )
            except UpdateError:
                _restore_backup(browser_root, backup_root, protected_names=protected_names)
            validate_browser_root(browser_root)
        else:
            backup_inventory = data.get("backup_inventory")
            if isinstance(backup_inventory, dict):
                _validate_tree_inventory(
                    browser_root,
                    protected_names,
                    backup_inventory,
                    "已恢复浏览器",
                )
                validate_browser_root(browser_root)
            else:
                current_version = read_current_version(browser_root) or ""
                validate_installed_browser(browser_root, _release_from_version_string(current_version))

    if work_root.exists():
        _discard_directory(work_root)
    if backup_root.exists() and not (phase == "installed" and data.get("keep_backup")):
        _discard_directory(backup_root)
    journal_path.unlink(missing_ok=True)


@contextmanager
def update_mutex(mutex_path: Path) -> Iterator[None]:
    mutex_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(mutex_path, flags | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        info = mutex_path.lstat()
        if _is_link_or_reparse(mutex_path) or info.st_nlink != 1:
            raise UpdateError(f"更新互斥文件不能是链接或硬链接: {mutex_path}")
        descriptor = os.open(mutex_path, flags)
        descriptor_info = os.fstat(descriptor)
        current_info = mutex_path.lstat()
        if (
            descriptor_info.st_nlink != 1
            or _is_link_or_reparse(mutex_path)
            or not os.path.samestat(descriptor_info, current_info)
        ):
            os.close(descriptor)
            raise UpdateError(f"更新互斥文件在打开期间发生变化: {mutex_path}")
    handle = os.fdopen(descriptor, "r+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise UpdateError("另一个更新进程正在运行") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise UpdateError("另一个更新进程正在运行") from exc
        yield
    finally:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


@contextmanager
def transaction_journal(
    journal_path: Path,
    *,
    transaction_id: str,
    installation_id: str,
    keep_backup: bool,
) -> Iterator[Callable[..., None]]:
    if journal_path.exists():
        raise UpdateError(f"更新 journal 已存在: {journal_path}")
    if not TRANSACTION_ID_RE.fullmatch(transaction_id):
        raise UpdateError(f"更新事务 ID 非法: {transaction_id!r}")
    if not re.fullmatch(r"[0-9a-f]{32}", installation_id):
        raise UpdateError(f"浏览器安装 ID 非法: {installation_id!r}")
    state = {
        "format": "camoufox-reverse-update/v1",
        "pid": os.getpid(),
        "phase": "starting",
        "transaction_id": transaction_id,
        "installation_id": installation_id,
        "keep_backup": keep_backup,
    }
    _write_json_atomic(journal_path, state)

    def set_state(**changes) -> None:
        if "phase" in changes and changes["phase"] not in JOURNAL_PHASES:
            raise UpdateError(f"更新阶段非法: {changes['phase']!r}")
        state.update(changes)
        _write_json_atomic(journal_path, state)

    try:
        yield set_state
    except Exception:
        if state.get("phase") in {
            "starting", "downloading", "extracting", "backing_up", "rolled_back",
        }:
            journal_path.unlink(missing_ok=True)
        raise
    except BaseException:
        raise
    else:
        journal_path.unlink(missing_ok=True)


def running_browser_processes() -> list[str]:
    if os.name != "nt":
        return []
    tasklist = _system_executable("tasklist.exe")
    if tasklist is None:
        raise UpdateError("无法定位系统 tasklist.exe，拒绝执行覆盖更新")
    try:
        completed = subprocess.run(
            [tasklist, "/FO", "CSV", "/NH"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise UpdateError(f"无法可靠检查浏览器进程: {exc}") from exc
    blocked = {"camoufox.exe", "private_browsing.exe"}
    return sorted({row[0] for row in csv.reader(completed.stdout.splitlines()) if row and row[0].lower() in blocked})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="安全更新 camoufox-reverse 定制浏览器")
    parser.add_argument("--browser-root", help="浏览器根目录；默认从 MCP 位置或环境变量推导")
    parser.add_argument("--check", action="store_true", help="仅检查版本，不下载或修改文件")
    parser.add_argument("--tag", help="安装指定发布标签，例如 v135.0.1-beta.25")
    parser.add_argument("--archive", help="使用已下载的本地 ZIP 包，跳过网络下载")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认")
    parser.add_argument("--force", action="store_true", help="版本相同时仍重新安装")
    parser.add_argument("--keep-backup", action="store_true", help="更新成功后保留旧浏览器备份")
    return parser


def run_update(args: argparse.Namespace) -> int:
    mcp_root = mcp_root_from_script()
    browser_root = derive_browser_root(mcp_root, args.browser_root)
    journal_path = browser_root / f".{browser_root.name}.update.json"
    mutex_path = browser_root / f".{browser_root.name}.update.mutex"

    if args.check:
        if journal_path.exists():
            print(f"状态: 检测到中断的更新事务；请不带 --check 运行更新器以恢复: {journal_path}")
            return 2
        validate_browser_root(browser_root)
        release = fetch_release(args.tag)
        current = read_current_version(browser_root) or "unknown"
        print(f"MCP 根目录: {mcp_root}")
        print(f"浏览器根目录: {browser_root}")
        print(f"受保护目录: {mcp_root}")
        print(f"当前版本: {current}")
        print(f"目标版本: {release.version_string}")
        print("状态: 已是最新版本" if current == release.version_string else "状态: 有可用更新")
        return 0

    with update_mutex(mutex_path):
        active = running_browser_processes()
        if active:
            raise UpdateError(f"请先关闭浏览器进程: {', '.join(active)}")
        recover_stale_update(journal_path, browser_root, mcp_root)
        validate_browser_root(browser_root)
        release = fetch_release(args.tag)
        current = read_current_version(browser_root) or "unknown"
        print(f"MCP 根目录: {mcp_root}")
        print(f"浏览器根目录: {browser_root}")
        print(f"受保护目录: {mcp_root}")
        print(f"当前版本: {current}")
        print(f"目标版本: {release.version_string}")
        if current == release.version_string and not args.force:
            print("浏览器已经是目标版本；使用 --force 可重新安装。")
            return 0

        active = running_browser_processes()
        if active:
            raise UpdateError(f"请先关闭浏览器进程: {', '.join(active)}")
        if not args.yes:
            answer = input(f"确认覆盖更新 {current} -> {release.version_string}? [y/N] ").strip().lower()
            if answer not in {"y", "yes"}:
                print("已取消。")
                return 0

        installation_id = ensure_install_sentinel(browser_root)
        transaction_id = f"{int(time.time())}-{os.getpid()}-{secrets.token_hex(8)}"
        work_root, backup_root = _transaction_paths(browser_root, transaction_id)
        extracted = work_root / "extracted"
        archive = release_archive_cache_path(mcp_root, release)
        local_archive = None
        if args.archive:
            local_archive = _absolute_without_resolving(Path(args.archive))
            _assert_no_reparse_components(local_archive, "本地更新包")
            if not local_archive.is_file():
                raise UpdateError(f"本地更新包不存在: {local_archive}")

        with transaction_journal(
            journal_path,
            transaction_id=transaction_id,
            installation_id=installation_id,
            keep_backup=bool(args.keep_backup),
        ) as set_state:
            set_state(phase="downloading", target_version=release.version_string)
            work_root.mkdir(parents=True, exist_ok=False)
            _write_json_atomic(work_root / WORK_OWNER_NAME, {
                "format": "camoufox-reverse-work/v1",
                "transaction_id": transaction_id,
            })
            try:
                archive_to_extract = archive
                if local_archive is not None:
                    print(f"使用本地更新包: {local_archive}")
                    _verify_download(local_archive, release)
                    archive_to_extract = local_archive
                else:
                    ensure_release_archive(release, archive)
                    archive_to_extract = archive
                set_state(phase="extracting")
                safe_extract(archive_to_extract, extracted)
                payload_root = locate_payload_root(extracted)
                validate_payload(
                    payload_root,
                    release,
                    protected_entry_names(browser_root, mcp_root),
                )
                smoke_test_browser(payload_root, release)
                set_state(payload_inventory=_tree_inventory(payload_root, set(), sync_files=False))

                active = running_browser_processes()
                if active:
                    raise UpdateError(f"更新前检测到浏览器进程: {', '.join(active)}")

                apply_payload(
                    browser_root,
                    mcp_root,
                    payload_root,
                    backup_root,
                    release,
                    phase_callback=lambda phase, **changes: set_state(phase=phase, **changes),
                    prevalidated=True,
                )
                set_state(phase="installed")
                print(f"更新完成: {release.version_string}")
                print(f"MCP 目录保持不变: {mcp_root}")
                remove_cached_archive(archive_to_extract, mcp_root)
                if args.keep_backup:
                    print(f"旧浏览器备份: {backup_root}")
                else:
                    _discard_directory(backup_root)
            finally:
                _discard_directory(work_root)
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run_update(build_parser().parse_args(argv))
    except (UpdateError, zipfile.BadZipFile, urllib.error.URLError, OSError) as exc:
        print(f"更新失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
