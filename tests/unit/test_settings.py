"""Unit tests for the .env loader."""
import os

import pytest


from config.settings import _load_dotenv


class TestDotenv:
    @pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "cp949"])
    def test_reads_notepad_encodings(self, tmp_path, monkeypatch, encoding):
        env = tmp_path / ".env"
        env.write_text("TEST_DOTENV_FROM=서울\nTEST_DOTENV_TIME=0800  # 출발\n", encoding=encoding)
        monkeypatch.delenv("TEST_DOTENV_FROM", raising=False)
        monkeypatch.delenv("TEST_DOTENV_TIME", raising=False)
        _load_dotenv(env)
        assert os.environ["TEST_DOTENV_FROM"] == "서울"
        assert os.environ["TEST_DOTENV_TIME"] == "0800"
