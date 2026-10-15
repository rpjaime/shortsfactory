import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from tiktok_import import TikTokImportError, build_command, explain_error, import_post, normalize_url


@pytest.mark.parametrize("url, expected", [
    ("https://www.tiktok.com/@jaime_rp85/photo/7412345678901234567?is_from_webapp=1&sender_device=pc",
     "https://www.tiktok.com/@jaime_rp85/photo/7412345678901234567"),
    ("www.tiktok.com/@u.ser/video/123", "https://www.tiktok.com/@u.ser/video/123"),
    ("https://vm.tiktok.com/ZGabc123/", "https://vm.tiktok.com/ZGabc123/"),
])
def test_normalize_url(url, expected):
    assert normalize_url(url) == expected


@pytest.mark.parametrize("bad", ["", "https://evil.com/@x/photo/1", "https://www.tiktok.com/@x",
                                 "https://tiktok.com.evil.com/@x/photo/1", "file:///C:/x"])
def test_rejects_non_tiktok(bad):
    with pytest.raises(TikTokImportError):
        normalize_url(bad)


def test_command_only_photos_and_isolated_config(tmp_path):
    cmd = build_command("https://www.tiktok.com/@u/photo/1", tmp_path)
    assert cmd[1:3] == ["-m", "gallery_dl"] and "--config-ignore" in cmd
    for opt in ("videos=false", "audio=false", "photos=true"):
        assert opt in cmd
    assert cmd[cmd.index("-f") + 1] == "{num:>02}.{extension}"
    with pytest.raises(TikTokImportError):
        build_command("https://www.tiktok.com/@u/photo/1", tmp_path, cookies=str(tmp_path / "no.txt"))


def fake_gallery_dl(n=3, desc="5 herramientas de GitHub #ia #dev", ext="jpeg", code=0, post_type="image"):
    def runner(cmd, **kw):
        dest = Path(cmd[cmd.index("-D") + 1])
        for i in range(1, n + 1):
            img = dest / f"{i:02d}.{ext}"
            Image.new("RGB", (1080, 1440), (i * 40, 80, 120)).save(img, "JPEG" if ext in ("jpeg", "jpg") else "PNG")
            (dest / f"{img.name}.json").write_text(json.dumps(
                {"id": "7412345678901234567", "user": "jaime_rp85", "desc": desc, "post_type": post_type}),
                encoding="utf-8")
        return subprocess.CompletedProcess(cmd, code, stdout="", stderr="")
    return runner


def test_import_post_downloads_in_order(tmp_path):
    post = import_post("https://www.tiktok.com/@jaime_rp85/photo/7412345678901234567", tmp_path,
                       runner=fake_gallery_dl(4))
    assert post.images == ["01.jpeg", "02.jpeg", "03.jpeg", "04.jpeg"]
    assert post.folder == tmp_path / "fuente_tiktok" / "7412345678901234567"
    assert post.author == "jaime_rp85" and "GitHub" in post.description
    assert not list(post.folder.glob("*.jpeg.json"))
    assert json.loads((post.folder / "publicacion.json").read_text(encoding="utf-8"))["autor"] == "jaime_rp85"
    # Reimportar la misma publicación sustituye la carpeta
    post2 = import_post("https://www.tiktok.com/@jaime_rp85/photo/7412345678901234567", tmp_path,
                        runner=fake_gallery_dl(2))
    assert post2.images == ["01.jpeg", "02.jpeg"]


def test_import_errors_are_explained(tmp_path):
    def failing(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="[gallery-dl][error] Unsupported URL 'x'")
    with pytest.raises(TikTokImportError, match="actualiza gallery-dl"):
        import_post("https://www.tiktok.com/@u/photo/1", tmp_path, runner=failing)
    assert not list((tmp_path / "fuente_tiktok").glob("descarga-*"))     # sin restos
    assert "conexión" in explain_error("ProxyError: 403 Forbidden")
    assert "bloqueado" in explain_error("HttpError: '403 Forbidden'")


def test_unsupported_format_is_converted(tmp_path):
    def runner(cmd, **kw):
        dest = Path(cmd[cmd.index("-D") + 1])
        Image.new("RGB", (800, 1000), (10, 20, 30)).save(dest / "01.bmp")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    post = import_post("https://www.tiktok.com/@u/photo/9", tmp_path, runner=runner)
    assert post.images == ["01.png"]


def test_real_gallery_dl_is_installed():
    r = subprocess.run([__import__("sys").executable, "-m", "gallery_dl", "--version"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip()
