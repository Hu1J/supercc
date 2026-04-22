"""qmd CLI tests — DEPRECATED: qmd dependency removed (TF-IDF only)."""
import pytest


@pytest.mark.skip(reason="qmd 已从依赖中移除，语义搜索改用 TF-IDF cosine similarity")
def test_qmd_collection_add_and_query():
    pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
