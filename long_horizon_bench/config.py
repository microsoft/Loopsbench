"""Configuration management for Long-Horizon-Bench.

Settings are loaded from environment variables or a .env file.
"""

import os

from dotenv import load_dotenv

load_dotenv()


class Config:
    """Central configuration object. Values come from environment variables."""

    @staticmethod
    def get_setting(key: str, default: str | None = None) -> str | None:
        """Get a setting from environment variables.

        Args:
            key: The setting key (case-insensitive, looked up as uppercase).
            default: Fallback value if the key is not found.

        Returns:
            The value of the setting, or the default.
        """
        env_val = os.environ.get(key.upper())
        if env_val is not None:
            return env_val
        return default

    # --- GHCR / Docker ---

    @property
    def ghcr_org(self) -> str:
        return self.get_setting("LHB_GHCR_ORG", "long-horizon-bench")

    @property
    def ghcr_repo(self) -> str:
        return self.get_setting("LHB_GHCR_REPO", "long-horizon-bench")

    # --- S3 ---

    @property
    def aws_region(self) -> str:
        return self.get_setting("AWS_REGION", "us-west-2")

    @property
    def s3_bucket_name(self) -> str | None:
        return self.get_setting("S3_BUCKET_NAME")

    # --- Misc ---

    @property
    def default_agent_timeout_sec(self) -> float:
        raw = self.get_setting("LHB_DEFAULT_AGENT_TIMEOUT_SEC", "1800")
        return float(raw)

    @property
    def default_test_timeout_sec(self) -> float:
        raw = self.get_setting("LHB_DEFAULT_TEST_TIMEOUT_SEC", "3600")
        return float(raw)


config = Config()
