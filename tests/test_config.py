import json, os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
import jarvis


class ConfigTests(unittest.TestCase):
    def test_missing_file_gives_defaults_with_resolved_paths(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, warn = config.load_config(Path(d) / "config.json", d)
            self.assertEqual(warn, []); self.assertEqual(cfg["ollama"]["model"], "qwen3.5:latest")
            self.assertEqual(cfg["memory"]["file"], str(Path(d) / "memory.json"))        # relative to the Jarvis folder, not the CWD

    def test_partial_file_is_merged_over_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "config.json").write_text(json.dumps({"ollama": {"model": "qwen3.5:4b"}, "browser": {"preferred": "brave"}, "extra": 1}))
            cfg, warn = config.load_config(Path(d) / "config.json")
            self.assertEqual((cfg["ollama"]["model"], cfg["ollama"]["num_ctx"], cfg["browser"]["preferred"]), ("qwen3.5:4b", 6144, "brave"))
            self.assertEqual(warn, ["unknown section 'extra' ignored"])

    def test_every_problem_is_listed_at_once(self):
        bad = {"ollama": {"base_url": "localhost:11434", "timeout_seconds": 0, "think": "no"}, "memory": {"auto_save_facts": "sometimes"},
               "semantic": {"min_similarity": 1.5}, "documents": {"chunk_chars": 300, "chunk_overlap": 400, "allowed_extensions": ["pdf"]},
               "voice": {"listen_seconds": True}, "logging": {"level": "LOUD"}, "reminders": [] }
        with self.assertRaises(config.ConfigError) as cm:
            config.validate(bad)
        msg = str(cm.exception)
        for needle in ["ollama.base_url", "ollama.timeout_seconds", "ollama.think", "memory.auto_save_facts", "semantic.min_similarity",
                       "documents.chunk_overlap", "documents.allowed_extensions", "voice.listen_seconds", "logging.level", "reminders: expected an object"]:
            self.assertIn(needle, msg)

    def test_bad_json_and_wrong_shape(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            p.write_text("{ not json")
            with self.assertRaises(config.ConfigError) as cm: config.load_config(p)
            self.assertIn("not valid JSON", str(cm.exception))
            p.write_text("[1, 2]")
            with self.assertRaises(config.ConfigError): config.load_config(p)

    def test_template_roundtrip_is_valid(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"; config.write_template(p)
            cfg, warn = config.load_config(p); self.assertEqual(warn, [])
            self.assertEqual(json.loads(p.read_text()), config.defaults())

    def test_apply_config_changes_what_jarvis_uses(self):
        saved = (jarvis.MODEL, jarvis.URL, jarvis.NUM_CTX, jarvis.MODES[1]["temp"], jarvis.MODES[4]["temp"], jarvis.FACT_BUDGET)
        try:
            cfg, _ = config.validate({"ollama": {"model": "m1", "base_url": "http://box:1234/", "num_ctx": 2048},
                                      "model_params": {"temperature_chat": 0.9, "temperature_json": 0.1}, "memory": {"fact_budget_tokens": 99}})
            jarvis.apply_config(cfg)
            self.assertEqual((jarvis.MODEL, jarvis.URL, jarvis.NUM_CTX), ("m1", "http://box:1234/api/chat", 2048))
            self.assertEqual((jarvis.MODES[1]["temp"], jarvis.MODES[4]["temp"], jarvis.FACT_BUDGET), (0.9, 0.1, 99))
        finally:
            jarvis.MODEL, jarvis.URL, jarvis.NUM_CTX, jarvis.MODES[1]["temp"], jarvis.MODES[4]["temp"], jarvis.FACT_BUDGET = saved

    def test_shipped_config_json_is_valid(self):
        shipped = Path(__file__).resolve().parent.parent / "config.json"
        if shipped.exists():
            cfg, warn = config.load_config(shipped); self.assertEqual(warn, [])


if __name__ == "__main__":
    unittest.main()
