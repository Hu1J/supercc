import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from cc_feishu_bridge.feishu.media import guess_file_type


class TestGuessFileType:
    def test_pdf(self):
        assert guess_file_type(".pdf") == "pdf"

    def test_docx(self):
        # 飞书 file_type=doc（对应 .doc/.docx 均可）
        assert guess_file_type(".docx") == "doc"

    def test_xlsx(self):
        # 飞书 file_type=xls（对应 .xls/.xlsx 均可）
        assert guess_file_type(".xlsx") == "xls"

    def test_png(self):
        assert guess_file_type(".png") == "png"

    def test_jpg(self):
        assert guess_file_type(".jpg") == "png"  # 飞书统一用 png

    def test_zip(self):
        # 飞书不支持 zip 类型，走 stream
        assert guess_file_type(".zip") == "stream"

    def test_txt(self):
        # 飞书不支持 txt 类型，走 stream
        assert guess_file_type(".txt") == "stream"

    def test_unknown(self):
        # 未知扩展名走 stream（保持原扩展名不变，飞书也能接受）
        assert guess_file_type(".xyz") == "stream"

    def test_uppercase(self):
        assert guess_file_type(".PDF") == "pdf"

    # 新增：编程语言扩展名
    def test_py(self):
        assert guess_file_type(".py") == "stream"

    def test_go(self):
        assert guess_file_type(".go") == "stream"

    def test_sh(self):
        assert guess_file_type(".sh") == "stream"

    def test_rs(self):
        assert guess_file_type(".rs") == "stream"

    def test_no_ext(self):
        # 无扩展名走 stream
        assert guess_file_type("") == "stream"

    # .tar.gz 等复合扩展名：os.path.splitext 只取最后一个
    def test_gz(self):
        assert guess_file_type(".gz") == "stream"

    def test_tar(self):
        assert guess_file_type(".tar") == "stream"


class TestSupportedImageExts:
    def test_supported_image_exts_in_main(self):
        """Verify SUPPORTED_IMAGE_EXTS constant matches media.py coverage."""
        from cc_feishu_bridge.main import SUPPORTED_IMAGE_EXTS
        assert ".png" in SUPPORTED_IMAGE_EXTS
        assert ".jpg" in SUPPORTED_IMAGE_EXTS
        assert ".jpeg" in SUPPORTED_IMAGE_EXTS
        assert ".gif" in SUPPORTED_IMAGE_EXTS
        assert ".webp" in SUPPORTED_IMAGE_EXTS
        assert ".bmp" in SUPPORTED_IMAGE_EXTS
        assert ".pdf" not in SUPPORTED_IMAGE_EXTS  # pdf 是文件不是图片