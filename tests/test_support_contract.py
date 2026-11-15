"""Runs the shipped support contract against the example app."""

from litestar_feedback.support.contract import SupportContract


class TestSupportContract(SupportContract):
    """Every contract test, against ``examples.support_app``."""
