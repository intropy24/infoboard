import sys

from githubkit import GitHub
from githubkit.exception import RateLimitExceeded
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from labqueue.jplag_run import run_jplag


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    gh_pat: str = Field(..., alias="GH_PAT")


class JplagSettings(Settings):
    gh_org: str = Field("intropy24", alias="GH_ORG")


def iter_org_repos(gh: GitHub, org: str):
    page = 1
    while True:
        repos = gh.rest.repos.list_for_org(org, per_page=100, page=page).parsed_data
        yield from repos
        if len(repos) < 100:
            break
        page += 1


if __name__ == "__main__":
    st = JplagSettings()
    gh = GitHub(st.gh_pat)
    students = [r.name for r in iter_org_repos(gh, st.gh_org) if r.name.startswith(f"{st.gh_org}-")]
    print(f"Org {st.gh_org}: {len(students)} student repos\n", flush=True)
    try:
        raise SystemExit(run_jplag(st.gh_pat, st.gh_org, students, workers=6, write_json=True))
    except RateLimitExceeded as e:
        print(f"GitHub API limit исчерпан, повторить примерно через {round(e.retry_after.total_seconds() / 60) + 1} мин.", file=sys.stderr)
        raise SystemExit(1) from None
