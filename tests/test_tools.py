import os, sys, time, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from toolreg import Param, Tool, ToolError, ToolRegistry, looks_actionable, run_model_tools


def make():
    calls = {"calc": [], "lock": [], "vol": []}
    r = ToolRegistry()
    r.register(Tool("calculator", "exact maths", lambda a: calls["calc"].append(a) or f"= {a['expression']}",
                    {"expression": Param("string", max_length=50)}))
    r.register(Tool("set_volume", "change volume", lambda a: calls["vol"].append(a) or "ok",
                    {"action": Param("string", enum=("get", "set", "up", "down")), "level": Param("integer", required=False, minimum=0, maximum=100)}))
    r.register(Tool("lock_screen", "lock", lambda a: calls["lock"].append(a) or "locked", sensitive=True, confirm_text=lambda a: "Lock your screen now?"))
    r.register(Tool("flaky", "always crashes", lambda a: 1 / 0))
    r.register(Tool("failing", "reports an honest failure", lambda a: (_ for _ in ()).throw(ToolError("Windows refused"))))
    r.register(Tool("slow", "hangs", lambda a: time.sleep(1) or "done"))
    return r, calls


class ValidationTests(unittest.TestCase):
    def setUp(self): self.r, self.calls = make()

    def test_valid_calls_are_cleaned(self):
        self.assertEqual(self.r.validate("set_volume", {"action": "set", "level": 40.0}), {"action": "set", "level": 40})   # 40.0 -> 40
        self.assertEqual(self.r.validate("calculator", {"expression": "  2+2  "}), {"expression": "2+2"})
        self.assertEqual(self.r.validate("set_volume", {"action": "up", "level": None}), {"action": "up"})

    def test_everything_else_is_rejected(self):
        bad = [("rm_rf", {}), ("none", {}), (None, {}), ("calculator", "2+2"), ("calculator", {}), ("calculator", {"expression": 5}),
               ("calculator", {"expression": "x" * 51}), ("calculator", {"expression": "2+2", "command": "calc.exe"}),   # extra key = no free-form commands
               ("calculator", {"expression": "1\x00"}), ("set_volume", {"action": "explode"}), ("set_volume", {"action": "set", "level": 101}),
               ("set_volume", {"action": "set", "level": -1}), ("set_volume", {"action": "set", "level": 5.5}), ("set_volume", {"action": "set", "level": True}),
               ("set_volume", {"action": "set", "level": "40"}), ("set_volume", {"action": "set", "level": float("nan")}), ("lock_screen", {"force": True})]
        for name, args in bad:
            with self.assertRaises(ToolError, msg=f"{name} {args}"):
                self.r.validate(name, args)

    def test_schema_and_prompt_list_only_registered_tools(self):
        enum = self.r.schema()["properties"]["tool"]["enum"]
        self.assertEqual(enum, sorted(self.r.names()) + ["none"]); self.assertNotIn("run_python", enum)
        self.assertIn("calculator(expression: string", self.r.prompt())

    def test_duplicate_and_reserved_names(self):
        with self.assertRaises(ValueError): self.r.register(Tool("calculator", "x", lambda a: ""))
        with self.assertRaises(ValueError): self.r.register(Tool("none", "x", lambda a: ""))


class RunTests(unittest.TestCase):
    def setUp(self): self.r, self.calls = make()

    def test_success_and_failure_are_reported_honestly(self):
        res = self.r.run("calculator", {"expression": "2+2"}, lambda q: True)
        self.assertEqual((res.ok, res.message), (True, "= 2+2"))
        res = self.r.run("failing", {}, lambda q: True)
        self.assertEqual((res.ok, res.message), (False, "Windows refused"))
        res = self.r.run("flaky", {}, lambda q: True)
        self.assertFalse(res.ok); self.assertIn("ZeroDivisionError", res.message)
        res = self.r.run("calculator", {"expression": 7}, lambda q: True)
        self.assertFalse(res.ok); self.assertEqual(self.calls["calc"], [{"expression": "2+2"}])      # invalid call never executed

    def test_sensitive_tool_needs_a_yes(self):
        asked = []
        res = self.r.run("lock_screen", {}, lambda q: asked.append(q) or False)
        self.assertFalse(res.ok); self.assertEqual(self.calls["lock"], []); self.assertEqual(asked, ["Lock your screen now?"])
        res = self.r.run("lock_screen", {}, lambda q: True)
        self.assertTrue(res.ok); self.assertEqual(len(self.calls["lock"]), 1)

    def test_timeout(self):
        res = self.r.run("slow", {}, lambda q: True, timeout=0.1)
        self.assertFalse(res.ok); self.assertIn("can't tell you whether it worked", res.message)


class ModelLoopTests(unittest.TestCase):
    def setUp(self):
        self.r, self.calls = make(); self.said = []
    def go(self, replies, **kw):
        it = iter(replies); self.asked = 0
        def ask_json(messages, schema):
            self.asked += 1
            r = next(it)
            if isinstance(r, Exception): raise r
            return r
        return run_model_tools(self.r, "do something", ask_json, kw.pop("confirm", lambda q: True), self.said.append, **kw)

    def test_model_picks_a_valid_tool(self):
        self.assertTrue(self.go([{"tool": "calculator", "arguments": {"expression": "9*9"}}]))
        self.assertEqual(self.said, ["= 9*9"])

    def test_none_and_garbage_fall_back_to_chat(self):
        for reply in [{"tool": "none", "arguments": {}}, "not a dict", None, [], {"arguments": {}}, RuntimeError("ollama down")]:
            self.said.clear()
            self.assertFalse(self.go([reply]), reply); self.assertEqual(self.said, [])

    def test_invalid_choices_never_run(self):
        for reply in [{"tool": "run_python", "arguments": {"code": "import os"}}, {"tool": "calculator", "arguments": {"expression": "1", "shell": "calc"}},
                      {"tool": "set_volume", "arguments": {"action": "set", "level": 9000}}, {"tool": "calculator", "arguments": "2+2"}]:
            self.assertFalse(self.go([reply]), reply)
        self.assertEqual((self.calls["calc"], self.calls["vol"], self.said), ([], [], []))

    def test_failed_tool_is_reported_not_hidden(self):
        self.assertTrue(self.go([{"tool": "failing", "arguments": {}}]))
        self.assertIn("didn't work: Windows refused", self.said[0])

    def test_confirmation_flows_through(self):
        self.assertTrue(self.go([{"tool": "lock_screen", "arguments": {}}], confirm=lambda q: False))
        self.assertEqual(self.calls["lock"], []); self.assertIn("didn't", self.said[0])

    def test_call_limit_and_repeat_protection(self):
        same = {"tool": "calculator", "arguments": {"expression": "1+1"}}
        self.go([same] * 10, max_calls=5, chain=True)
        self.assertEqual(len(self.calls["calc"]), 1)                                  # identical repeat stops the loop
        self.calls["calc"].clear(); self.said.clear()
        varied = [{"tool": "calculator", "arguments": {"expression": f"{i}+1"}} for i in range(10)]
        self.go(varied, max_calls=3, chain=True)
        self.assertEqual(len(self.calls["calc"]), 3); self.assertEqual(self.asked, 3)  # hard cap, even if the model never says none
        self.calls["calc"].clear()
        self.go(varied, max_calls=3, chain=False)
        self.assertEqual(len(self.calls["calc"]), 1)                                  # default: one tool per message

    def test_looks_actionable(self):
        for t in ["remind me later", "turn the volume down", "take a screenshot", "what do I study", "open the pdf"]: self.assertTrue(looks_actionable(t), t)
        for t in ["tell me a joke", "why is the sky blue", "write a poem"]: self.assertFalse(looks_actionable(t), t)


if __name__ == "__main__":
    unittest.main()
