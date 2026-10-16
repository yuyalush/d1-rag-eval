from src.config import load_config


def test_config_has_all_pipelines():
    cfg = load_config()
    assert set(cfg["configs"]) == {
        "a_baseline",
        "b_d1_filter",
        "c_d1_gate",
        "d_d1_rerank",
        "e_llm_filter",
    }
