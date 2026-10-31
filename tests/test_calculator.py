import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from calculator import CalcError, evaluate, normalize, try_calculate


class CalculatorTests(unittest.TestCase):
    def test_exact_arithmetic(self):
        self.assertEqual(evaluate("0.1 + 0.2"), "0.3")          # floats would give 0.30000000000000004
        self.assertEqual(evaluate("23 * 47"), "1081")
        self.assertEqual(evaluate("2 ** 10"), "1024")
        self.assertEqual(evaluate("(1 + 2) * (3 + 4)"), "21")
        self.assertEqual(evaluate("7 // 2"), "3")
        self.assertEqual(evaluate("10 % 3"), "1")
        self.assertEqual(evaluate("-5 + 2"), "-3")
        self.assertEqual(evaluate("1/4"), "0.25")
        self.assertEqual(evaluate("10/3"), "3.33333333333")
        self.assertEqual(evaluate("2 ** 100"), str(2 ** 100))    # big integers stay exact

    def test_functions_and_constants(self):
        self.assertEqual(evaluate("sqrt(16)"), "4")
        self.assertEqual(evaluate("factorial(5)"), "120")
        self.assertEqual(evaluate("abs(-3.5)"), "3.5")
        self.assertEqual(evaluate("round(2.567, 1)"), "2.6")
        self.assertEqual(evaluate("floor(2.9) + ceil(2.1)"), "5")
        self.assertEqual(evaluate("log10(1000)"), "3")
        self.assertEqual(evaluate("log(8, 2)"), "3")
        self.assertEqual(evaluate("sin(0)"), "0")
        self.assertTrue(evaluate("pi").startswith("3.14159"))

    def test_everyday_phrasing(self):
        self.assertEqual(try_calculate("what is 23 x 47?"), (True, "23 x 47 = 1081"))
        self.assertEqual(try_calculate("What's 20% of 150?"), (True, "20% of 150 = 30"))      # shows your own wording back
        self.assertEqual(try_calculate("calculate 2^8")[1], "2^8 = 256")
        self.assertEqual(try_calculate("what's 20% of 150")[1].split(" = ")[1], "30")
        self.assertEqual(try_calculate("150 + 10%")[1].split(" = ")[1], "165")
        self.assertEqual(try_calculate("200 - 25%")[1].split(" = ")[1], "150")
        self.assertEqual(try_calculate("calculate 2^8")[1].split(" = ")[1], "256")
        self.assertEqual(try_calculate("square root of 144")[1].split(" = ")[1], "12")
        self.assertEqual(try_calculate("12 ÷ 4")[1].split(" = ")[1], "3")
        self.assertEqual(try_calculate("1,000 * 3")[1].split(" = ")[1], "3000")

    def test_not_maths_goes_to_the_model(self):
        for text in ["what is python", "tell me a joke", "what is 2024", "I have 3 apples + 2 pears", "what is the capital of france",
                     "open obs", "what is 5", "hello"]:
            self.assertEqual(try_calculate(text)[0], False, text)

    def test_unsafe_input_rejected(self):
        for bad in ["__import__('os').system('echo hi')", "open('x')", "().__class__", "[1,2]", "'a' * 3", "x + 1", "lambda: 1",
                    "sqrt(1, 2)", "print(1)", "abs(x=1)", "True + 1", "1 if 1 else 2", "a.b", "1; 2"]:
            with self.assertRaises(CalcError, msg=bad):
                evaluate(bad)

    def test_resource_limits(self):
        for bad in ["9 ** 9 ** 9", "10 ** 100000", "factorial(100000)", "factorial(2.5)", "factorial(-1)", "9" * 300, "(" * 150 + "1" + ")" * 150]:
            with self.assertRaises(CalcError, msg=bad[:30]):
                evaluate(bad)

    def test_math_errors(self):
        for bad in ["1/0", "0 ** -1", "sqrt(-1)", "log10(0)", "10 % 0", "(-8) ** 0.5", ""]:
            with self.assertRaises(CalcError, msg=bad):
                evaluate(bad)
        self.assertIn("division by zero", try_calculate("5 / 0")[1])
        self.assertTrue(try_calculate("5 / 0")[0])  # looks like maths, so the model is NOT asked to guess

    def test_normalize(self):
        self.assertEqual(normalize("What is 3 x 4?"), "3*4")


if __name__ == "__main__":
    unittest.main()
