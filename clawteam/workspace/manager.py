"""WorkspaceManager — creates / checkpoints / merges / cleans up git worktrees."""

from __future__ import annotations

import json
import logging
import shutil
import os
from datetime import datetime, timezone
from pathlib import Path

from clawteam.workspace import git
from clawteam.workspace.models import WorkspaceInfo, WorkspaceRegistry

logger = logging.getLogger(__name__)


def _workspaces_root() -> Path:
    from clawteam.team.models import get_data_dir
    p = get_data_dir() / "workspaces"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _registry_path(team_name: str) -> Path:
    return _workspaces_root() / team_name / "workspace-registry.json"


def _load_registry(team_name: str, repo_root: str) -> WorkspaceRegistry:
    path = _registry_path(team_name)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return WorkspaceRegistry.model_validate(data)
        except Exception:
            pass
    return WorkspaceRegistry(team_name=team_name, repo_root=repo_root)


def _save_registry(registry: WorkspaceRegistry) -> None:
    path = _registry_path(registry.team_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(registry.model_dump_json(indent=2), encoding="utf-8")
    tmp.rename(path)


class WorkspaceManager:
    """Manages git worktree-based isolated workspaces for agents."""

    def __init__(self, repo_path: Path | None = None):
        cwd = repo_path or Path.cwd()
        self.repo_root = git.repo_root(cwd)
        self.base_branch = git.current_branch(self.repo_root)

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create_workspace(
        self,
        team_name: str,
        agent_name: str,
        agent_id: str,
    ) -> WorkspaceInfo:
        branch = f"clawteam/{team_name}/{agent_name}"
        wt_path = _workspaces_root() / team_name / agent_name

        # Crash recovery: if worktree already exists, clean it up first
        if wt_path.exists():
            try:
                git.remove_worktree(self.repo_root, wt_path)
            except git.GitError:
                pass
            try:
                git.delete_branch(self.repo_root, branch)
            except git.GitError:
                pass

        git.create_worktree(
            self.repo_root, wt_path, branch, base_ref=self.base_branch,
        )

        # --------------------------
        # 🚀 绝对白名单保护模式 v3.0
        # --------------------------
        # 【绝对不能删的白名单】
        KEEP_ALWAYS = [
            # 核心命脉
            "openclaw.json",
            ".env",
            ".env.local",
            
            # 认知系统
            "SOUL.md",
            "AGENTS.md",
            "TOOLS.md",
            "MEMORY.md",
            "HEARTBEAT.md",
            "IDENTITY.md",
            "USER.md",
            
            # 技能和工具
            "skills/",
            "scripts/",
            ".openclaw/",
            ".clawhub/",
            
            # 环境配置
            "node_modules/",
            "venv/",
            ".venv/",
            "poetry.lock",
            "pyproject.toml",
            "requirements.txt",
            
            # git工作树必须保留
            ".git",
        ]
        
        # 遍历目录删除
        for item in wt_path.iterdir():
            # 检查是否在白名单
            keep = False
            for pattern in KEEP_ALWAYS:
                if pattern.endswith("/"):
                    # 目录匹配
                    if item.is_dir() and item.name == pattern.rstrip("/"):
                        keep = True
                        break
                else:
                    # 文件匹配
                    if item.is_file() and item.name == pattern:
                        keep = True
                        break
            
            # 不在白名单就删除
            if not keep:
                try:
                    if item.is_symlink():
                        # 删 symlink 本身，不解引用（不删真实目标）
                        item.unlink()
                    elif item.is_file():
                        item.unlink()
                    elif item.is_dir():
                        shutil.rmtree(item)
                except Exception as e:
                    logger.warning(f"删除文件失败 {item}: {e}")

        # --------------------------
        # 🔗 新增：软链接共享依赖（解决依赖断层问题）
        # --------------------------
        # 主工作区路径
        main_workspace = self.repo_root
        
        # 需要共享的依赖目录
        shared_dirs = [
            "node_modules",
            ".venv",
            "venv",
        ]
        
        for dir_name in shared_dirs:
            main_dir = main_workspace / dir_name
            target_dir = wt_path / dir_name
            
            # 如果主工作区有这个目录，且Worker目录没有，就创建软链接
            if main_dir.exists() and main_dir.is_dir() and not target_dir.exists():
                try:
                    os.symlink(main_dir, target_dir)
                    logger.info(f"创建软链接成功: {target_dir} -> {main_dir}")
                except Exception as e:
                    logger.warning(f"创建软链接失败 {dir_name}: {e}")

        # --------------------------
        # 🚨 强制自检程序（防呆设计）
        # --------------------------
        required_files = [
            wt_path / "openclaw.json",
            wt_path / "skills",
            wt_path / "scripts",
        ]
        
        for f in required_files:
            if not f.exists():
                raise RuntimeError(f"瘦身错误：核心文件/目录缺失 {f.name}，请检查白名单配置")

        info = WorkspaceInfo(
            agent_name=agent_name,
            agent_id=agent_id,
            team_name=team_name,
            branch_name=branch,
            worktree_path=str(wt_path),
            repo_root=str(self.repo_root),
            base_branch=self.base_branch,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        registry = _load_registry(team_name, str(self.repo_root))
        # Remove stale entry for the same agent, if any
        registry.workspaces = [
            w for w in registry.workspaces if w.agent_name != agent_name
        ]
        registry.workspaces.append(info)
        _save_registry(registry)

        return info

    # ------------------------------------------------------------------
    # Checkpoint
    # ------------------------------------------------------------------

    def checkpoint(
        self,
        team_name: str,
        agent_name: str,
        message: str | None = None,
    ) -> bool:
        info = self._find(team_name, agent_name)
        if info is None:
            return False
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        msg = message or f"[clawteam] checkpoint: {agent_name} @ {ts}"
        return git.commit_all(Path(info.worktree_path), msg)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup_workspace(
        self,
        team_name: str,
        agent_name: str,
        auto_checkpoint: bool = True,
    ) -> bool:
        info = self._find(team_name, agent_name)
        if info is None:
            return False

        if auto_checkpoint:
            try:
                self.checkpoint(team_name, agent_name, f"[clawteam] final checkpoint: {agent_name}")
            except Exception:
                pass

        try:
            git.remove_worktree(self.repo_root, Path(info.worktree_path))
        except git.GitError as e:
            logger.warning("worktree remove failed: %s", e)
        try:
            git.delete_branch(self.repo_root, info.branch_name)
        except git.GitError as e:
            logger.warning("branch delete failed: %s", e)

        registry = _load_registry(team_name, str(self.repo_root))
        registry.workspaces = [
            w for w in registry.workspaces if w.agent_name != agent_name
        ]
        _save_registry(registry)
        return True

    def cleanup_team(self, team_name: str) -> int:
        """Clean up all workspaces for a team. Returns number cleaned."""
        registry = _load_registry(team_name, str(self.repo_root))
        count = 0
        for ws in list(registry.workspaces):
            if self.cleanup_workspace(team_name, ws.agent_name):
                count += 1
        return count

    # ------------------------------------------------------------------
    # Merge
    # ------------------------------------------------------------------

    def merge_workspace(
        self,
        team_name: str,
        agent_name: str,
        target_branch: str | None = None,
        cleanup_after: bool = True,
    ) -> tuple[bool, str]:
        info = self._find(team_name, agent_name)
        if info is None:
            return False, f"No workspace found for {agent_name}"

        # Checkpoint before merge
        self.checkpoint(team_name, agent_name, f"[clawteam] pre-merge checkpoint: {agent_name}")

        target = target_branch or info.base_branch
        success, output = git.merge_branch(
            self.repo_root, info.branch_name, target,
        )

        if success and cleanup_after:
            self.cleanup_workspace(team_name, agent_name, auto_checkpoint=False)

        return success, output

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def list_workspaces(self, team_name: str) -> list[WorkspaceInfo]:
        registry = _load_registry(team_name, str(self.repo_root))
        return registry.workspaces

    def get_workspace(self, team_name: str, agent_name: str) -> WorkspaceInfo | None:
        return self._find(team_name, agent_name)

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @staticmethod
    def try_create(repo_path: Path | None = None) -> WorkspaceManager | None:
        """Return a WorkspaceManager if inside a git repo, else None."""
        try:
            return WorkspaceManager(repo_path)
        except git.GitError:
            return None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _find(self, team_name: str, agent_name: str) -> WorkspaceInfo | None:
        registry = _load_registry(team_name, str(self.repo_root))
        for ws in registry.workspaces:
            if ws.agent_name == agent_name:
                return ws
        return None
