"""git 辅助：走 dulwich（纯 Python），镜像里不用装 git（避开构建机 apt 卡死）。

技能同步（skills.py）和代码工作区（workspace.py）共用。只做读：克隆、读 HEAD 里的文件；
写回远端一律走 GitLab API（workspace.propose_mr），不在工作区里留写权限凭证。
"""
import io

from dulwich import porcelain
from dulwich.object_store import tree_lookup_path
from dulwich.repo import Repo


class GitError(Exception):
    pass


def clone(url, branch, dest, username="", token=""):
    """克隆到 dest，返回 HEAD 提交号。远程 https 浅克隆；本地路径（测试/开发用）完整克隆。"""
    kw = {"errstream": io.BytesIO(), "checkout": True}
    if url.startswith(("http://", "https://")):
        kw["depth"] = 1
        if token:
            kw.update(username=username or "deploy-token", password=token)
    elif url.startswith("ssh://") or url.startswith("git@"):
        raise GitError(f"不支持 ssh 地址（镜像里没有 ssh 客户端）：{url}。请改用 https 地址。")
    if branch:
        kw["branch"] = branch
    try:
        repo = porcelain.clone(url, str(dest), **kw)
    except Exception as e:
        msg = str(e)
        if token:
            msg = msg.replace(token, "***")
        raise GitError(f"克隆 {url} 失败：{type(e).__name__}: {msg[:300]}")
    try:
        return repo.head().decode()
    finally:
        repo.close()


def head_blob(repo_path, rel):
    """HEAD 里 rel 文件的内容（bytes）；不存在返回 None。"""
    repo = Repo(str(repo_path))
    try:
        tree_id = repo[repo.head()].tree
        try:
            _mode, sha = tree_lookup_path(repo.get_object, tree_id, rel.encode("utf-8"))
        except KeyError:
            return None
        obj = repo[sha]
        return getattr(obj, "data", None)
    finally:
        repo.close()
