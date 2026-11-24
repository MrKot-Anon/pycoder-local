"""Portable CI selection; the complete POSIX suite runs on macOS/Ubuntu."""
import os
import unittest

if os.name == "nt":
    modules = ["test_sandbox", "test_unpack", "test_platform", "test_team_parallel", "test_agent", "test_cancellation", "test_transport",
               "test_empty_response", "test_truncated_tools", "test_plan_completion", "test_process_tools",
               "test_instructions", "test_onboarding", "test_model_limits", "test_tui"]
    suite = unittest.defaultTestLoader.loadTestsFromNames(["tests." + name for name in modules])
else:
    suite = unittest.defaultTestLoader.discover("tests")
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
