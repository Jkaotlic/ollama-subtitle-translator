"""Smoke tests: rendered index.html contains expected element IDs."""
import pytest
import app as app_module


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


class TestIndexTemplate:
    def test_advanced_panel_sections(self, client):
        html = client.get("/").data.decode("utf-8")
        # Three h4 subsections
        assert 'id="advSectionContent"' in html
        assert 'id="advSectionModel"' in html
        assert 'id="advSectionQuality"' in html

    def test_new_controls_present(self, client):
        html = client.get("/").data.decode("utf-8")
        assert 'id="use_tm"' in html
        assert 'id="use_llm_judge"' in html
        assert 'id="use_back_translation"' in html
        assert 'id="aux_model"' in html
        assert 'id="tmStatus"' in html
        assert 'id="tmClearBtn"' in html

    def test_result_info_block_present(self, client):
        """Will PASS after Task 11 — expected to fail after Task 7 completes."""
        html = client.get("/").data.decode("utf-8")
        assert 'id="resultInfo"' in html

    def test_footer_updated(self, client):
        """Will PASS after Task 9."""
        html = client.get("/").data.decode("utf-8")
        assert "TranslateGemma (Google)" not in html
        assert "Gemma 4" in html and ("140" in html or "Qwen" in html)
