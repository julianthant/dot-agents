import unittest

from build_instructions import END, HEADER, START, build


class BuildInstructionsTest(unittest.TestCase):
    def test_keeps_managed_block_and_drops_old_body(self) -> None:
        current = f"old rules\n\n{START}\nmanaged\n{END}\n"
        result = build("# Shared\n", "## Codex\n", current)
        self.assertTrue(result.startswith(HEADER + "# Shared\n"))
        self.assertIn("## Codex\n", result)
        self.assertTrue(result.endswith(f"{START}\nmanaged\n{END}\n"))
        self.assertNotIn("old rules", result)
        self.assertEqual(build("# Shared\n", "## Codex\n", result), result)

    def test_without_managed_block(self) -> None:
        self.assertEqual(build("# S\n", "## C\n", ""), HEADER + "# S\n\n## C\n")


if __name__ == "__main__":
    unittest.main()
