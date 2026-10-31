import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import facts


def mem(*f):
    return {"facts": list(f), "history": []}


class FactTests(unittest.TestCase):
    def test_extract_examples(self):
        self.assertEqual([f for f, _, _ in facts.extract("My name is Sam")], ["my name is Sam"])
        self.assertEqual([f for f, _, _ in facts.extract("I study B.Tech Electronics")], ["I study B.Tech Electronics"])
        self.assertEqual([f for f, _, _ in facts.extract("I am doing B.Tech Electronics.")], ["I study B.Tech Electronics"])
        both = facts.extract("My name is Sam and I study B.Tech Electronics")
        self.assertEqual([f for f, _, _ in both], ["my name is Sam", "I study B.Tech Electronics"])
        self.assertEqual([f for f, _, _ in facts.extract("my favourite colour is teal")], ["my favourite colour is teal"])

    def test_questions_and_commands_yield_nothing(self):
        for t in ["what is my name?", "my name is?", "/remember my name is Sam", "do I live in Pune", "", "hello there"]:
            self.assertEqual(facts.extract(t), [], t)

    def test_confidence(self):
        self.assertEqual(facts.extract("I love hiking")[0][2], "ambiguous")
        self.assertEqual(facts.extract("my name is Sam")[0][2], "high")

    def test_classify(self):
        self.assertEqual(facts.classify("my password is hunter2"), "never")
        self.assertEqual(facts.classify("my card number is 4111 1111 1111 1111"), "never")
        self.assertEqual(facts.classify("my api key is abc"), "never")
        self.assertEqual(facts.classify("I take medication daily"), "sensitive")
        self.assertEqual(facts.classify("my email address is a@b.com"), "sensitive")
        self.assertEqual(facts.classify("I study electronics"), "ok")

    def test_add_dedupe_update(self):
        m = mem()
        self.assertEqual(facts.add_fact(m, "my name is Sam")[0], "added")
        self.assertEqual(facts.add_fact(m, "My name is sam.")[0], "duplicate")          # case/punctuation ignored
        self.assertEqual(facts.add_fact(m, "my name is Alex"), ("updated", "my name is Sam"))
        self.assertEqual(m["facts"], ["my name is Alex"])                                 # replaced, not duplicated
        self.assertEqual(facts.add_fact(m, "I study electronics")[0], "added")
        self.assertEqual(facts.add_fact(m, "I study physics")[0], "updated")
        self.assertEqual(len(m["facts"]), 2)
        self.assertEqual(facts.add_fact(m, "   ")[0], "empty")

    def test_free_form_facts_never_overwrite_each_other(self):
        m = mem()
        facts.add_fact(m, "my laptop has an RTX 3050")
        facts.add_fact(m, "my exam is on Friday")
        self.assertEqual(len(m["facts"]), 2)

    def test_listing_and_delete(self):
        m = mem("a fact", "b fact", "c fact")
        self.assertEqual(facts.listing(m), ["1. a fact", "2. b fact", "3. c fact"])
        self.assertEqual(facts.delete_fact(m, 2), "b fact")
        self.assertEqual(m["facts"], ["a fact", "c fact"])
        for bad in (0, 3, -1, 99):
            with self.assertRaises(IndexError):
                facts.delete_fact(m, bad)
        self.assertEqual(m["facts"], ["a fact", "c fact"])                               # nothing deleted by bad numbers

    def test_learn_auto_saves_clear_facts_without_asking(self):
        m, said = mem(), []
        asked = []
        changed = facts.learn(m, "My name is Sam", "auto", lambda q: asked.append(q) or True, said.append)
        self.assertTrue(changed)
        self.assertEqual(m["facts"], ["my name is Sam"])
        self.assertEqual(asked, [])

    def test_learn_asks_for_ambiguous_sensitive_and_updates(self):
        m, said = mem("my name is Sam"), []
        asked = []
        no = lambda q: asked.append(q) or False
        self.assertFalse(facts.learn(m, "I love hiking", "auto", no, said.append))      # ambiguous -> asked, declined
        self.assertFalse(facts.learn(m, "My name is Alex", "auto", no, said.append))    # update -> asked, declined
        self.assertEqual(m["facts"], ["my name is Sam"])
        self.assertEqual(len(asked), 2)
        self.assertIn("Update 'my name is Sam' to 'my name is Alex'?", asked[1])
        self.assertTrue(facts.learn(m, "My name is Alex", "auto", lambda q: True, said.append))
        self.assertEqual(m["facts"], ["my name is Alex"])

    def test_learn_sensitive_and_never(self):
        m, said, asked = mem(), [], []
        self.assertFalse(facts.learn(m, "I work as a therapist", "auto", lambda q: asked.append(q) or False, said.append))
        self.assertEqual(m["facts"], [])                                                 # sensitive: asked, declined, not saved
        self.assertIn("looks personal", asked[0])
        self.assertEqual(facts.extract("my phone number is 9876543210"), [])              # not a fact pattern, so never auto-saved
        facts.learn(m, "my favourite password is hunter2", "auto", lambda q: True, said.append)
        self.assertEqual(m["facts"], [])
        self.assertTrue(any("won't save" in s for s in said))

    def test_learn_modes(self):
        m = mem()
        self.assertFalse(facts.learn(m, "My name is Sam", "off", lambda q: True, print))
        self.assertEqual(m["facts"], [])
        asked = []
        facts.learn(m, "My name is Sam", "ask", lambda q: asked.append(q) or True, lambda s: None)
        self.assertEqual(len(asked), 1)
        self.assertEqual(m["facts"], ["my name is Sam"])


if __name__ == "__main__":
    unittest.main()
