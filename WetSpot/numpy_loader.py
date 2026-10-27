# Fusion360API Python module
"""numpy の動的取得・検証・ロード。

Fusion 360 同梱の Python には numpy が含まれないため、初回利用時に取得する。
どの段階で失敗しても例外は投げず ``None`` を返す。呼び出し側は ``None`` の場合、
何事もなかったように純 Python 経路へフォールバックすること。

- 取得先は ABI タグ付きディレクトリ ``packages/cp{major}{minor}-win_amd64/``。
  Fusion は自動更新で Python のバージョンが上がることがあるため、
  古い ABI の .pyd を誤って掴まないようにするための分離。
- 完了マーカー ``.numpy-<version>.ok`` が無ければ「未インストール」とみなす。
- 取得は同梱 pip 経由を優先し、失敗時のみ wheel 直ダウンロード + sha256 検証に
  フォールバックする。
- 環境変数 ``WET_SPOT_NO_NUMPY=1`` で常に無効化できる（切り分け用）。
"""

from __future__ import annotations
from typing import Any

import glob
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

_NUMPY_VERSION = "2.5.3"

# ABI タグ -> (wheel ファイル名, sha256, ダウンロード URL)
# sha256 / URL は PyPI JSON API (https://pypi.org/pypi/numpy/2.5.3/json) から取得し固定。
_WHEELS = {
    "cp314-win_amd64": (
        "numpy-2.5.3-cp314-cp314-win_amd64.whl",
        "2c25dfa72943e4336ddb6b0ee4277b47a0c85bede0807530ec68103bf58e2c10",
        "https://files.pythonhosted.org/packages/a4/73/"
        "d2c08231e4fde7e415501fd02c715d96e98599b2d8384445933944152984/"
        "numpy-2.5.3-cp314-cp314-win_amd64.whl",
    ),
}

_DIR = os.path.dirname(os.path.abspath(__file__))
_PACKAGES_ROOT = os.path.join(_DIR, "packages")

# 直近の失敗理由（デバッグ用の文字列。成功時は None）。
last_error = None

# プロセス内キャッシュ。一度 import すると解放できないため、
# 同一プロセス内での再試行は行わず結果を使い回す。
_cache = {"tried": False, "module": None}


def _set_error(message: str) -> None:
    """直近の失敗理由を保存する。"""
    global last_error
    last_error = message
    return None


def _abi_tag() -> str | None:
    """実行中の Python に対応する wheel の ABI タグを返す。"""
    if sys.platform != "win32":
        return None
    if sys.maxsize <= 2**32:
        return None
    major, minor = sys.version_info[:2]
    return "cp{}{}-win_amd64".format(major, minor)


def _packages_dir(abi_tag: str) -> str:
    """ABI ごとのパッケージ保存先を返す。"""
    return os.path.join(_PACKAGES_ROOT, abi_tag)


def _marker_path(pkg_dir: str) -> str:
    """インストール完了マーカーのパスを返す。"""
    return os.path.join(pkg_dir, ".numpy-{}.ok".format(_NUMPY_VERSION))


def _try_import(pkg_dir: str) -> Any | None:
    """pkg_dir を sys.path の先頭に入れて import numpy を試す。失敗時は sys.path を戻す。"""
    inserted = pkg_dir not in sys.path
    if inserted:
        sys.path.insert(0, pkg_dir)
    try:
        import numpy  # noqa: F401

        return numpy
    except Exception as exc:  # noqa: BLE001 - どんな失敗でも None にフォールバックする
        if inserted and pkg_dir in sys.path:
            sys.path.remove(pkg_dir)
        return _set_error("numpy の import に失敗しました: {}".format(exc))


def _find_bundled_pip_wheel() -> str | None:
    """Fusion に同梱された pip wheel を探す。"""
    pattern = os.path.join(sys.prefix, "Lib", "ensurepip", "_bundled", "pip-*.whl")
    candidates = sorted(glob.glob(pattern))
    return candidates[-1] if candidates else None


def _install_via_pip(pkg_dir: str) -> bool:
    """Fusion 同梱の python.exe + 同梱 pip wheel で --target install する。"""
    pip_whl = _find_bundled_pip_wheel()
    if not pip_whl:
        return _set_error("同梱 pip (ensurepip/_bundled) が見つかりません")

    tmp_target = tempfile.mkdtemp(prefix="wet_spot_numpy_", dir=_PACKAGES_ROOT)
    try:
        script = (
            "import sys; sys.path.insert(0, r'{pip_whl}'); "
            "from pip._internal.cli.main import main; "
            "sys.exit(main(['install', '--target', r'{target}', "
            "'--only-binary=:all:', '--no-deps', "
            "'numpy=={version}']))"
        ).format(pip_whl=pip_whl, target=tmp_target, version=_NUMPY_VERSION)

        proc = subprocess.run(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if proc.returncode != 0:
            return _set_error(
                "pip install に失敗しました (code={}): {}".format(
                    proc.returncode, (proc.stdout or "")[-2000:]
                )
            )
        if not os.path.isdir(os.path.join(tmp_target, "numpy")):
            return _set_error(
                "pip install は成功したが numpy パッケージが見当たりません"
            )

        pkg_dir_tmp = pkg_dir + ".new"
        if os.path.isdir(pkg_dir_tmp):
            shutil.rmtree(pkg_dir_tmp, ignore_errors=True)
        shutil.move(tmp_target, pkg_dir_tmp)
        return pkg_dir_tmp
    except Exception as exc:  # noqa: BLE001
        return _set_error("pip install 中に例外が発生しました: {}".format(exc))
    finally:
        if os.path.isdir(tmp_target):
            shutil.rmtree(tmp_target, ignore_errors=True)


def _install_via_direct_download(abi_tag: str, pkg_dir: str) -> bool:
    """wheel を直接ダウンロードし、sha256 検証後 zipfile で展開する。"""
    entry = _WHEELS.get(abi_tag)
    if not entry:
        return _set_error("未対応の ABI タグです: {}".format(abi_tag))
    filename, expected_sha256, url = entry

    tmp_dir = tempfile.mkdtemp(prefix="wet_spot_numpy_dl_", dir=_PACKAGES_ROOT)
    whl_path = os.path.join(tmp_dir, filename)
    try:
        try:
            with (
                urllib.request.urlopen(url, timeout=60) as resp,
                open(whl_path, "wb") as f,
            ):
                shutil.copyfileobj(resp, f)
        except Exception as exc:  # noqa: BLE001
            return _set_error("wheel のダウンロードに失敗しました: {}".format(exc))

        digest = hashlib.sha256()
        with open(whl_path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected_sha256:
            return _set_error(
                "sha256 が一致しません (expected={}, actual={})".format(
                    expected_sha256, digest.hexdigest()
                )
            )

        extract_dir = os.path.join(tmp_dir, "extracted")
        try:
            with zipfile.ZipFile(whl_path) as zf:
                zf.extractall(extract_dir)
        except Exception as exc:  # noqa: BLE001
            return _set_error("wheel の展開に失敗しました: {}".format(exc))

        if not os.path.isdir(os.path.join(extract_dir, "numpy")):
            return _set_error("展開後に numpy パッケージが見当たりません")

        pkg_dir_tmp = pkg_dir + ".new"
        if os.path.isdir(pkg_dir_tmp):
            shutil.rmtree(pkg_dir_tmp, ignore_errors=True)
        shutil.move(extract_dir, pkg_dir_tmp)
        return pkg_dir_tmp
    except Exception as exc:  # noqa: BLE001
        return _set_error("直接ダウンロード経路で例外が発生しました: {}".format(exc))
    finally:
        if os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _install(abi_tag: str, pkg_dir: str) -> bool:
    """numpy を取得して pkg_dir に配置し、マーカーを書く。成功なら True。"""
    os.makedirs(_PACKAGES_ROOT, exist_ok=True)

    staged_dir = _install_via_pip(pkg_dir)
    if staged_dir is None:
        staged_dir = _install_via_direct_download(abi_tag, pkg_dir)
    if staged_dir is None:
        return False

    try:
        if os.path.isdir(pkg_dir):
            shutil.rmtree(pkg_dir, ignore_errors=True)
        os.replace(staged_dir, pkg_dir)
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(staged_dir, ignore_errors=True)
        _set_error("インストール先への配置に失敗しました: {}".format(exc))
        return False

    try:
        with open(_marker_path(pkg_dir), "w", encoding="utf-8") as f:
            f.write(_NUMPY_VERSION)
    except Exception as exc:  # noqa: BLE001
        _set_error("完了マーカーの書き込みに失敗しました: {}".format(exc))
        return False

    return True


def load_numpy(allow_download: bool = True) -> Any | None:
    """numpy を返す。使えない場合は None を返す（例外は投げない）。

    成功・失敗を問わず ``last_error``（モジュール変数）に直近の理由文字列を残す
    （成功時は None）。
    """
    global last_error
    last_error = None

    if _cache["tried"]:
        return _cache["module"]

    def _finish(module: Any) -> Any | None:
        """読み込み結果をプロセス内にキャッシュする。"""
        _cache["tried"] = True
        _cache["module"] = module
        return module

    if os.environ.get("WET_SPOT_NO_NUMPY") == "1":
        _set_error("WET_SPOT_NO_NUMPY=1 により無効化されています")
        return _finish(None)

    abi_tag = _abi_tag()
    if abi_tag is None:
        _set_error("サポート対象外の環境です (win32 かつ 64bit の Python が必要)")
        return _finish(None)

    pkg_dir = _packages_dir(abi_tag)
    marker = _marker_path(pkg_dir)

    if os.path.isfile(marker):
        numpy = _try_import(pkg_dir)
        if numpy is not None:
            return _finish(numpy)
        # マーカーはあるが壊れている。allow_download なら再取得を試みる。

    if not allow_download:
        if last_error is None:
            _set_error("numpy が未取得です (allow_download=False)")
        return _finish(None)

    if not _install(abi_tag, pkg_dir):
        return _finish(None)

    numpy = _try_import(pkg_dir)
    return _finish(numpy)
