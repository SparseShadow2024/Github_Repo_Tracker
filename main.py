import asyncio
import json
from pathlib import Path
from typing import Any, Optional

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

GITHUB_API_BASE = "https://api.github.com"
PLUGIN_NAME = "astrbot_plugin_github_repo_tracker"


class RepoState:
    __slots__ = ("last_commit_sha", "last_pr_updated_at", "seen_pr_updates")

    def __init__(
        self,
        last_commit_sha: Optional[str] = None,
        last_pr_updated_at: Optional[str] = None,
        seen_pr_updates: Optional[dict[str, str]] = None,
    ):
        self.last_commit_sha = last_commit_sha
        self.last_pr_updated_at = last_pr_updated_at
        self.seen_pr_updates = seen_pr_updates or {}

    def to_dict(self) -> dict:
        return {
            "last_commit_sha": self.last_commit_sha,
            "last_pr_updated_at": self.last_pr_updated_at,
            "seen_pr_updates": self.seen_pr_updates,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RepoState":
        return cls(
            last_commit_sha=data.get("last_commit_sha"),
            last_pr_updated_at=data.get("last_pr_updated_at"),
            seen_pr_updates=data.get("seen_pr_updates", {}),
        )


@register(
    PLUGIN_NAME,
    "疏影",
    "监听一个或多个 GitHub 仓库的 Commit 与 Pull Request 变化，支持公开与私有仓库。",
    "0.1.0",
)
class GithubRepoTrackerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._poll_task: Optional[asyncio.Task] = None

        self._data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._state_file = self._data_dir / "state.json"
        self._state: dict[str, RepoState] = self._load_state()

    async def initialize(self):
        self._session = aiohttp.ClientSession()
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def terminate(self):
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._session:
            await self._session.close()

    def _load_state(self) -> dict[str, RepoState]:
        if not self._state_file.exists():
            return {}
        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
            return {repo: RepoState.from_dict(s) for repo, s in raw.items()}
        except Exception:
            logger.exception(f"[{PLUGIN_NAME}] 读取状态文件失败，将重新开始追踪")
            return {}

    def _save_state(self):
        raw = {repo: s.to_dict() for repo, s in self._state.items()}
        self._state_file.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    async def _poll_loop(self):
        while True:
            interval = max(1, int(self.config.get("poll_interval_minutes", 10)))
            try:
                await self._poll_once()
            except Exception:
                logger.exception(f"[{PLUGIN_NAME}] 轮询过程中发生未预期的错误")
            await asyncio.sleep(interval * 60)

    async def _poll_once(self):
        repos = self.config.get("repos", [])
        for repo_cfg in repos:
            full_name = (repo_cfg.get("full_name") or "").strip()
            if not full_name:
                continue
            try:
                await self._poll_repo(repo_cfg, full_name)
            except Exception:
                logger.exception(f"[{PLUGIN_NAME}] 轮询仓库 {full_name} 失败")

    def _find_repo_cfg(self, full_name: str) -> Optional[dict]:
        for repo_cfg in self.config.get("repos", []):
            if (repo_cfg.get("full_name") or "").strip().lower() == full_name.lower():
                return repo_cfg
        return None

    def _headers_for(self, repo_cfg: dict) -> dict:
        token = (repo_cfg.get("token") or "").strip() or (
            self.config.get("global_token", "") or ""
        ).strip()
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _poll_repo(self, repo_cfg: dict, full_name: str):
        is_private = bool(repo_cfg.get("is_private", False))
        token = (repo_cfg.get("token") or "").strip()
        if is_private and not token:
            logger.warning(
                f"[{PLUGIN_NAME}] 仓库 {full_name} 标记为私有但未配置 Token，跳过本次轮询"
            )
            return

        target_sessions = repo_cfg.get("target_sessions") or []
        if not target_sessions:
            return

        headers = self._headers_for(repo_cfg)
        state = self._state.setdefault(full_name, RepoState())

        if repo_cfg.get("watch_commits", True):
            await self._check_commits(repo_cfg, full_name, headers, state, target_sessions)
        if repo_cfg.get("watch_pull_requests", True):
            await self._check_pull_requests(full_name, headers, state, target_sessions)

        self._save_state()

    async def _get_commit_stats(
        self, full_name: str, sha: str, headers: dict
    ) -> Optional[dict]:
        detail = await self._get_json(
            f"{GITHUB_API_BASE}/repos/{full_name}/commits/{sha}", headers
        )
        if not detail:
            return None
        return detail.get("stats")

    async def _get_json(self, url: str, headers: dict, params: Optional[dict] = None) -> Any:
        async with self._session.get(url, headers=headers, params=params) as resp:
            if resp.status == 404:
                logger.warning(f"[{PLUGIN_NAME}] 请求 {url} 返回 404，请检查仓库名/权限/Token")
                return None
            if resp.status in (401, 403):
                logger.warning(
                    f"[{PLUGIN_NAME}] 请求 {url} 返回 {resp.status}，Token 可能无效、权限不足或已达到 API 限额"
                )
                return None
            resp.raise_for_status()
            return await resp.json()

    async def _check_commits(
        self,
        repo_cfg: dict,
        full_name: str,
        headers: dict,
        state: RepoState,
        target_sessions: list[str],
    ):
        branch = (repo_cfg.get("branch") or "").strip() or None
        params = {"per_page": 10}
        if branch:
            params["sha"] = branch

        commits = await self._get_json(
            f"{GITHUB_API_BASE}/repos/{full_name}/commits", headers, params
        )
        if not commits:
            return

        latest_sha = commits[0]["sha"]
        if state.last_commit_sha is None:
            state.last_commit_sha = latest_sha
            return

        if latest_sha == state.last_commit_sha:
            return

        new_commits = []
        for commit in commits:
            if commit["sha"] == state.last_commit_sha:
                break
            new_commits.append(commit)

        state.last_commit_sha = latest_sha
        if not new_commits:
            return

        show_stats = bool(self.config.get("show_stats", False))
        for commit in reversed(new_commits):
            stats = None
            if show_stats:
                stats = await self._get_commit_stats(full_name, commit["sha"], headers)
            text = self._format_commit_message(full_name, commit, branch, stats)
            await self._push(target_sessions, text)

    async def _check_pull_requests(
        self,
        full_name: str,
        headers: dict,
        state: RepoState,
        target_sessions: list[str],
    ):
        params = {"state": "all", "sort": "updated", "direction": "desc", "per_page": 10}
        pulls = await self._get_json(
            f"{GITHUB_API_BASE}/repos/{full_name}/pulls", headers, params
        )
        if not pulls:
            return

        if not state.seen_pr_updates and state.last_pr_updated_at is None:
            state.last_pr_updated_at = pulls[0]["updated_at"]
            for pr in pulls:
                state.seen_pr_updates[str(pr["number"])] = pr["updated_at"]
            return

        updated_prs = []
        for pr in pulls:
            number = str(pr["number"])
            updated_at = pr["updated_at"]
            if state.seen_pr_updates.get(number) != updated_at:
                updated_prs.append(pr)
                state.seen_pr_updates[number] = updated_at

        if pulls:
            state.last_pr_updated_at = pulls[0]["updated_at"]

        for pr in reversed(updated_prs):
            text = self._format_pr_message(full_name, pr)
            await self._push(target_sessions, text)

    def _display_flags(self) -> dict:
        return {
            "author": bool(self.config.get("show_author", True)),
            "url": bool(self.config.get("show_url", True)),
            "branch": bool(self.config.get("show_branch", True)),
            "content": bool(self.config.get("show_content", True)),
        }

    def _format_commit_message(
        self,
        full_name: str,
        commit: dict,
        branch: Optional[str] = None,
        stats: Optional[dict] = None,
    ) -> str:
        flags = self._display_flags()
        sha = commit["sha"][:7]
        full_message = commit["commit"]["message"]
        title, _, content = full_message.partition("\n\n")
        content = content.strip()
        author = (
            commit.get("author", {}) or {}
        ).get("login") or commit["commit"]["author"]["name"]
        url = commit["html_url"]

        branch_line = f"分支: {branch}\n" if (flags["branch"] and branch) else ""
        author_line = f"作者: {author}\n" if flags["author"] else ""
        content_line = f"内容: {content}\n" if (flags["content"] and content) else ""
        url_line = f"{url}" if flags["url"] else ""
        stats_line = ""
        if stats:
            additions = stats.get("additions", 0)
            deletions = stats.get("deletions", 0)
            stats_line = f"改动: +{additions} -{deletions}\n"

        return (
            f"Commit {sha}\n"
            f"仓库: {full_name}\n"
            f"{branch_line}"
            f"{stats_line}"
            f"{author_line}"
            f"标题: {title}\n"
            f"{content_line}"
            f"{url_line}"
        ).rstrip("\n")

    def _format_pr_message(self, full_name: str, pr: dict) -> str:
        flags = self._display_flags()
        number = pr["number"]
        title = pr["title"]
        content = (pr.get("body") or "").strip()
        user = pr["user"]["login"]
        state = pr["state"]
        merged = pr.get("merged_at") is not None
        status = "已合并" if merged else ("已关闭" if state == "closed" else "开放中")
        base_branch = pr.get("base", {}).get("ref")
        url = pr["html_url"]

        branch_line = f"分支: {base_branch}\n" if (flags["branch"] and base_branch) else ""
        author_line = f"作者: {user}\n" if flags["author"] else ""
        content_line = f"内容: {content}\n" if (flags["content"] and content) else ""
        url_line = f"{url}" if flags["url"] else ""

        return (
            f"PR #{number} {status}\n"
            f"仓库: {full_name}\n"
            f"{branch_line}"
            f"{author_line}"
            f"标题: {title}\n"
            f"{content_line}"
            f"{url_line}"
        ).rstrip("\n")

    async def _push(self, target_sessions: list[str], text: str):
        for umo in target_sessions:
            umo = (umo or "").strip()
            if not umo:
                continue
            try:
                await self.context.send_message(umo, MessageChain().message(text))
            except Exception:
                logger.exception(f"[{PLUGIN_NAME}] 推送消息到会话 {umo} 失败")

    @filter.command_group("ght")
    def ght(self):
        pass

    @ght.command("whoami")
    async def ght_whoami(self, event: AstrMessageEvent):
        """获取当前会话的 unified_msg_origin，用于填写到仓库的 target_sessions 配置中"""
        yield event.plain_result(event.unified_msg_origin)

    @ght.command("bind")
    async def ght_bind(self, event: AstrMessageEvent, full_name: str):
        """将当前会话绑定为指定仓库的推送目标"""
        repo_cfg = self._find_repo_cfg(full_name)
        if repo_cfg is None:
            yield event.plain_result(f"未找到仓库 {full_name}，请先在管理面板的仓库列表中添加它")
            return

        umo = event.unified_msg_origin
        target_sessions = repo_cfg.setdefault("target_sessions", [])
        if umo in target_sessions:
            yield event.plain_result(f"当前会话已经绑定过 {full_name}")
            return

        target_sessions.append(umo)
        self.config.save_config()
        yield event.plain_result(f"已将当前会话绑定到 {full_name} 的推送列表")

    @ght.command("unbind")
    async def ght_unbind(self, event: AstrMessageEvent, full_name: str):
        """将当前会话从指定仓库的推送目标中移除"""
        repo_cfg = self._find_repo_cfg(full_name)
        if repo_cfg is None:
            yield event.plain_result(f"未找到仓库 {full_name}")
            return

        umo = event.unified_msg_origin
        target_sessions = repo_cfg.get("target_sessions") or []
        if umo not in target_sessions:
            yield event.plain_result(f"当前会话未绑定 {full_name}")
            return

        target_sessions.remove(umo)
        self.config.save_config()
        yield event.plain_result(f"已将当前会话从 {full_name} 的推送列表中移除")

    @ght.command("commits")
    async def ght_commits(self, event: AstrMessageEvent, full_name: str):
        """查询已配置仓库的最新一条 Commit"""
        repo_cfg = self._find_repo_cfg(full_name)
        if repo_cfg is None:
            yield event.plain_result(f"未找到仓库 {full_name}，请先在管理面板的仓库列表中添加它")
            return

        headers = self._headers_for(repo_cfg)
        branch = (repo_cfg.get("branch") or "").strip() or None
        params = {"per_page": 1}
        if branch:
            params["sha"] = branch

        commits = await self._get_json(
            f"{GITHUB_API_BASE}/repos/{full_name}/commits", headers, params
        )
        if not commits:
            yield event.plain_result(f"未能获取 {full_name} 的 Commit，请检查仓库名/权限/Token")
            return

        stats = None
        if bool(self.config.get("show_stats", False)):
            stats = await self._get_commit_stats(full_name, commits[0]["sha"], headers)

        yield event.plain_result(
            self._format_commit_message(full_name, commits[0], branch, stats)
        )

    @ght.command("prs")
    async def ght_prs(self, event: AstrMessageEvent, full_name: str):
        """查询已配置仓库最新更新的一个 Pull Request"""
        repo_cfg = self._find_repo_cfg(full_name)
        if repo_cfg is None:
            yield event.plain_result(f"未找到仓库 {full_name}，请先在管理面板的仓库列表中添加它")
            return

        headers = self._headers_for(repo_cfg)
        params = {"state": "all", "sort": "updated", "direction": "desc", "per_page": 1}
        pulls = await self._get_json(
            f"{GITHUB_API_BASE}/repos/{full_name}/pulls", headers, params
        )
        if not pulls:
            yield event.plain_result(f"未能获取 {full_name} 的 Pull Request，请检查仓库名/权限/Token")
            return

        yield event.plain_result(self._format_pr_message(full_name, pulls[0]))
