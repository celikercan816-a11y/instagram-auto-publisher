import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    pass


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class Config:
    app_id: str
    app_secret: str
    redirect_uri: str
    access_token: str
    ig_user_id: str
    gh_repo: str
    gh_branch: str

    @classmethod
    def load(cls, require_token: bool = True) -> "Config":
        return cls(
            app_id=os.environ.get("IG_APP_ID", "").strip(),
            app_secret=os.environ.get("IG_APP_SECRET", "").strip(),
            redirect_uri=os.environ.get("IG_REDIRECT_URI", "https://localhost/").strip(),
            access_token=(_require("IG_ACCESS_TOKEN") if require_token else os.environ.get("IG_ACCESS_TOKEN", "").strip()),
            ig_user_id=(_require("IG_USER_ID") if require_token else os.environ.get("IG_USER_ID", "").strip()),
            gh_repo=os.environ.get("GH_REPO", "").strip(),
            gh_branch=os.environ.get("GH_BRANCH", "main").strip(),
        )

    def media_public_url(self, relative_path: str) -> str:
        """Turn a repo-relative path like 'media/foo.jpg' into a public raw.githubusercontent.com URL."""
        if not self.gh_repo:
            raise ConfigError("GH_REPO is not set, cannot build a public URL for local media")
        relative_path = relative_path.lstrip("/")
        return f"https://raw.githubusercontent.com/{self.gh_repo}/{self.gh_branch}/{relative_path}"
