"""内置 mihomo 核心生命周期：机场订阅 → 本地代理端口 + 控制器

把 ShellCrash 的核心能力集成进程序：使用者无需安装任何梯子工具，
给 SUB_URLS 一个或多个机场订阅链接即可获得 IP 池。核心只监听 127.0.0.1
随机端口，系统与其它应用流量不经过它。订阅链接是凭证：不落日志、不进仓库。

注意（架构约束）：内置核心为单 mixed-port + 单 select 组，IP 维度是全局单点——
所有账号共享同一出口 IP，切节点对整个进程全局生效（多号并行分片同样受影响）。

供应链安全：自动下载锁定固定版本（_MIHOMO_VERSION），下载后按内置的官方
sha256 表（_KNOWN_SHA256）校验，不匹配拒绝执行并删除文件；下载顺序为
github.com 直连优先、GH_PROXIES 第三方镜像仅作回退。
"""
import atexit
import ctypes
import gzip
import hashlib
import os
import platform
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time

import requests

from clash_ctl import ClashCtl
from config import BASE_DIR, DATA_DIR, GH_PROXIES, MIHOMO_PATH

_RUNTIME_DIR = os.path.join(DATA_DIR, "mihomo_runtime")
_MIHOMO_VERSION = "v1.19.30"   # 锁定下载版本：配合下方官方 sha256 表做供应链校验
_RELEASE_API = f"https://api.github.com/repos/MetaCubeX/mihomo/releases/tags/{_MIHOMO_VERSION}"

# 官方 release 资产 sha256（来源：GitHub API releases assets[].digest 字段，
# 2026-08-29 对 MetaCubeX/mihomo v1.19.30 核实）；表外资产一律不下载、不执行。
# 仅收录自动下载可达平台（linux/darwin × amd64/arm64 的 .gz 包）
_KNOWN_SHA256 = {
    "mihomo-linux-amd64-compatible-v1.19.30.gz": "db214c7a2517e63c150d123178d16d102e03a241ccdae4e5e07ffbe9cf56c6f9",
    "mihomo-linux-amd64-v1.19.30.gz": "cf06ce2c7d1421bdbda14ee4a5b6046672dc35ebf8eecd8e77504ec3c0ed9a84",
    "mihomo-linux-amd64-v1-v1.19.30.gz": "cbe553d0319a414bd3a372c5976a252155b2c4882b66bce88a4d6bba9571a553",
    "mihomo-linux-amd64-v1-go120-v1.19.30.gz": "b358eba638b01c1f1841c4d612d4b6da58419bef96055958cf9c3be04f625642",
    "mihomo-linux-amd64-v1-go123-v1.19.30.gz": "4c993d90d9ed4101e99af836d17a56bd3ddd508e90ec83f6d9a3d1833fb63422",
    "mihomo-linux-amd64-v2-v1.19.30.gz": "87c345ed9905f607b702c17cc78f086b1db0a62b01e87cf9e245f089a5d2b8f7",
    "mihomo-linux-amd64-v2-go120-v1.19.30.gz": "97ebc23b02f1d9b3053600132918d5c53ba0abe282d616c6875d66271b124a4b",
    "mihomo-linux-amd64-v2-go123-v1.19.30.gz": "3b9df99ee6d075f208efdb41f974445d02f2f6a8e339650aac7f599a9d54e430",
    "mihomo-linux-amd64-v3-v1.19.30.gz": "2c3d87ea31a8b420285fa4c1dedd9fb2356fe289b609edac1c671c00738a1544",
    "mihomo-linux-amd64-v3-go120-v1.19.30.gz": "64692c0c9c868285df56db7c568c5e254047cee556ea1eba92eebf53e0df714e",
    "mihomo-linux-amd64-v3-go123-v1.19.30.gz": "0597530d2527c444fd412d5e05aff9bfbea62f91f45c3e945f17ec72953e7c89",
    "mihomo-linux-arm64-v1.19.30.gz": "58896873736d28628f66de3677c8654fa0f180662523148e136cff4f6e890069",
    "mihomo-darwin-amd64-compatible-v1.19.30.gz": "6e75de0732e8afabe413ff7c235e8f16226ce136672371c60787cbf9607402c5",
    "mihomo-darwin-amd64-v1.19.30.gz": "99dfcfe454ed58fb95ee4ba222c39defd051b687ad3e5deabb1b9d6be3103e2f",
    "mihomo-darwin-amd64-v1-v1.19.30.gz": "b5355135a1446ca1af83c794f46435290a59ddd968dd73f7883b9c7977e14f8b",
    "mihomo-darwin-amd64-v1-go120-v1.19.30.gz": "79e5cb88244c182ee67a3e33f6978f7afb2a283607e96e9dc51c50dbd446bf64",
    "mihomo-darwin-amd64-v1-go122-v1.19.30.gz": "470a1ef7bffbde0e28b863355c98f865880cb5ec232ce1b59fb804f0c9fb1a05",
    "mihomo-darwin-amd64-v1-go124-v1.19.30.gz": "9e0bd336a7c7ecb1d40f39ad1c18393185c47232284670516cda664ce10df723",
    "mihomo-darwin-amd64-v2-v1.19.30.gz": "dc9325a01f209411593024790bf6452dbde5dd06d78d86cbf6a3c9ec6485373f",
    "mihomo-darwin-amd64-v2-go120-v1.19.30.gz": "2ff181c1b9a562a2513949ae42ca3491ec798b7218e9224f05b22f57ac8e3403",
    "mihomo-darwin-amd64-v2-go122-v1.19.30.gz": "2091e63e91d8b1aee7b24a750c5fe4a6b5271c4942315017ea9bd0f347b78c96",
    "mihomo-darwin-amd64-v2-go124-v1.19.30.gz": "7ac1be6dd25d3657c491ee3caa74029ad469900a2253ccd2c72eb3fba175b378",
    "mihomo-darwin-amd64-v3-v1.19.30.gz": "0c930b57795181a3e0140d71327cb4a666d6e67cf304edf28385333dfc9155c7",
    "mihomo-darwin-amd64-v3-go120-v1.19.30.gz": "e5555601710b49930bdd1b3f9707e5b51efaa32cbfc41724e0416734185a9bf0",
    "mihomo-darwin-amd64-v3-go122-v1.19.30.gz": "60dccc065d8d87e4a13391d7497030e77f97ca7ce90b944c0c77a32cfeba6559",
    "mihomo-darwin-amd64-v3-go124-v1.19.30.gz": "2ece9f398d31b55102066dc8f6e6a38ff32bea99547308ddf5ca2843406003c7",
    "mihomo-darwin-arm64-v1.19.30.gz": "2c7f3a7904fa1cee291e124123e630e7b1ebd13765dd9bf26c0a28432004d9f4",
    "mihomo-darwin-arm64-go120-v1.19.30.gz": "79322a547bb7ad09c13d366c4b4fc97553896c52b9168d33df21ca4710580b7d",
    "mihomo-darwin-arm64-go122-v1.19.30.gz": "b87f4b02e2fa1bec7d3e1399d8bfa9f5a300610c75a98263e225144d4a85646f",
    "mihomo-darwin-arm64-go124-v1.19.30.gz": "bc847bad45e04fbca3f177743b58241443834693da8efe7723733e8a2001590f",
}


def _gh_urls(url: str) -> list[str]:
    """GitHub 加速（仅用于 release 元数据查询）：依次尝试各加速前缀，最后回退直连"""
    return [p + url for p in GH_PROXIES] + [url]


def _gh_download_urls(url: str) -> list[str]:
    """二进制下载顺序：github.com 直连优先，第三方加速镜像仅作回退
    （镜像可篡改内容，最终由 _KNOWN_SHA256 校验兜底）"""
    return [url] + [p + url for p in GH_PROXIES]


def _free_port() -> int:
    """取一个 127.0.0.1 空闲端口（避免与已有 Clash 实例冲突）"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _go_platform() -> tuple[str, str]:
    goos = {"linux": "linux", "darwin": "darwin"}.get(sys.platform, "")
    if sys.platform.startswith("win"):
        goos = "windows"
    goarch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64",
              "arm64": "arm64"}.get(platform.machine().lower(), "")
    return goos, goarch


def _manual_hint(dest: str):
    """自动下载不可用时的手动放置指引（凭证不落日志，此处仅打印版本与路径）"""
    print(f"[ProxyCore] 请从 https://github.com/MetaCubeX/mihomo/releases/tag/{_MIHOMO_VERSION} "
          f"手动下载对应平台的 .gz 资产，解压后放置到 {dest}")


def _download_binary(dest: str) -> bool:
    """从 GitHub Releases 下载锁定版本的 mihomo 二进制（.gz 解压）；
    下载后强制校验官方 sha256，不匹配拒绝执行并删除文件；失败返回 False"""
    goos, goarch = _go_platform()
    if not goos or not goarch:
        print(f"[ProxyCore] 不支持的平台 {sys.platform}/{platform.machine()}，请手动放置 mihomo 到 {dest}")
        return False
    base = f"mihomo-{goos}-{goarch}"
    # 资产名带版本号（如 mihomo-linux-amd64-compatible-v1.19.30.gz），
    # 需先查锁定版本 release 的资产清单再按名匹配
    assets = {}
    for api in _gh_urls(_RELEASE_API):
        try:
            rel = requests.get(api, timeout=30).json()
            assets = {a["name"]: a["browser_download_url"]
                      for a in rel.get("assets", [])}
            if assets:
                break
        except Exception as e:
            print(f"[ProxyCore] 查询 release 资产清单失败（{api[:40]}...）: {e}")
    if not assets:
        _manual_hint(dest)
        return False
    # 只取 .gz 包（排除 .deb/.rpm/.pkg.tar.zst；windows 为 .zip 暂不支持自动下载），
    # 且只接受内置 sha256 表中的资产（表外资产不下载不执行）
    names = [n for n in assets
             if n.startswith(base) and n.endswith(".gz") and n in _KNOWN_SHA256]
    # linux/amd64 优先 compatible 变体（老 CPU 无 v3 指令集也能跑），其余按名长升序
    # （短名=普通变体优先于 go120/go123 等工具链变体）
    names.sort(key=lambda n: ("-compatible" not in n, len(n)))
    if not names:
        print(f"[ProxyCore] 当前平台 {base} 无可信资产（不在内置校验表内），拒绝自动下载")
        _manual_hint(dest)
        return False
    for name in names:
        expected = _KNOWN_SHA256[name]
        tmp = dest + ".tmp"
        for url in _gh_download_urls(assets[name]):
            try:
                print(f"[ProxyCore] 下载 mihomo 核心: {url}")
                r = requests.get(url, timeout=120)
                r.raise_for_status()
                # 供应链校验：官方 digest 针对 .gz 压缩包本身，先校验再解压
                digest = hashlib.sha256(r.content).hexdigest()
                if digest != expected:
                    print(f"[ProxyCore] 校验和不匹配（{name}，期望 {expected[:16]}...，"
                          f"实际 {digest[:16]}...），拒绝执行")
                    continue    # 换下一个下载源重试（直连/镜像任一可信源匹配即通过）
                data = gzip.decompress(r.content)   # 先解压再落盘，避免截断/0字节残留
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(tmp, "wb") as f:
                    f.write(data)
                os.chmod(tmp, 0o755)
                os.replace(tmp, dest)   # 原子替换：不存在半成品 dest
                return True
            except Exception as e:
                print(f"[ProxyCore] 下载失败（{name}）: {e}")
                if os.path.exists(tmp):
                    os.remove(tmp)
    print(f"[ProxyCore] 自动下载均未通过校验/失败，已放弃")
    _manual_hint(dest)
    return False


def ensure_binary(allow_download: bool = True) -> str | None:
    """定位 mihomo 二进制：MIHOMO_PATH → vendor/mihomo → data/mihomo → 自动下载"""
    candidates = [MIHOMO_PATH] if MIHOMO_PATH else []
    exe = "mihomo.exe" if sys.platform.startswith("win") else "mihomo"
    candidates += [os.path.join(BASE_DIR, "vendor", exe), os.path.join(DATA_DIR, exe)]
    for p in candidates:
        if p and os.path.isfile(p):
            return os.path.abspath(p)
    if not allow_download:
        return None
    dest = os.path.join(DATA_DIR, exe)
    return os.path.abspath(dest) if _download_binary(dest) else None


class ProxyCore:
    """内置 mihomo 核心：start() 成功返回就绪的 ClashCtl，失败返回 None（静默降级）

    注意：单 mixed-port + 单 select 组，IP 维度全局单点——进程内所有账号
    共享同一出口 IP，切节点全局生效（含多号并行分片）。
    """

    @staticmethod
    def _pdeathsig_kwargs() -> dict:
        """Linux 下为子进程设置 PR_SET_PDEATHSIG：父进程退出（含崩溃/被 kill）时
        内核自动 SIGKILL 核心子进程，不留孤儿；非 Linux 平台返回空不加 preexec_fn。

        平台差异（已知限制）：Windows/macOS 无 PDEATHSIG，父进程被强杀时 mihomo
        可能成为孤儿进程继续监听随机端口——正常退出（含 Ctrl+C）仍由 atexit→stop()
        回收；Windows 上如需彻底清理可手动结束 mihomo.exe 进程。"""

        if not sys.platform.startswith("linux"):
            return {}
        libc = ctypes.CDLL(None, use_errno=True)
        PR_SET_PDEATHSIG = 1

        def _set_pdeathsig():
            libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL)

        return {"preexec_fn": _set_pdeathsig}

    def __init__(self, sub_urls: list[str], group: str = "profiler"):
        self._raw_sub_urls = list(sub_urls)   # 原始入参，供 get_core 单例做参数比对
        # 剔除会生成非法 yaml 的链接（含双引号/反斜杠/换行；双引号标量中 \ 是转义符），
        # 避免静默失效；警告只打印序号，凭证不落日志
        self.sub_urls = []
        for i, u in enumerate(sub_urls):
            if '"' in u or "\\" in u or "\n" in u or "\r" in u:
                print(f"[ProxyCore] 警告：第 {i} 条订阅链接含非法字符（引号/反斜杠/换行），已剔除")
            else:
                self.sub_urls.append(u)
        # 组名同样要进 yaml：含引号/反斜杠/换行会生成非法配置或与 self.group 不一致的组名，
        # 回退默认组名（\ 结尾会生成未终止字符串；\n/\t 字面两字符会被 yaml 解析成转义；
        # 逗号会截断 MATCH 规则语法）
        if not group:
            group = "profiler"   # 未配置组名，直接用默认（非异常，不打警告）
        elif any(c in group for c in ('"', "\\", "\n", "\r", ",")):
            print("[ProxyCore] 警告：节点组名含非法字符（引号/反斜杠/换行/逗号），回退默认组名 profiler")
            group = "profiler"
        self.group = group
        self.mix_port = _free_port()
        self.api_port = _free_port()
        while self.api_port == self.mix_port:   # 理论上可撞，守卫一下
            self.api_port = _free_port()
        self.secret = secrets.token_hex(8)
        self._proc: subprocess.Popen | None = None
        self._start_lock = threading.Lock()   # start() 串行化：并发建池不会拉起多个核心
        self._atexit_registered = False       # atexit.register 只注册一次，避免叠加调用

    def _gen_config(self) -> str:
        """生成最小 yaml（字符串模板，项目无 pyyaml 依赖）；每订阅一个 provider，
        select 组 use 引用全部 provider（多订阅节点汇入同一轮换组）"""
        providers = "\n".join(
            f'  sub{i}:\n'
            f'    type: http\n'
            f'    url: "{u}"\n'
            # 订阅必须直连拉取（节点还没加载，走代理组是鸡生蛋问题）
            f'    proxy: DIRECT\n'
            f'    interval: 86400\n'
            f'    health-check:\n'
            f'      enable: true\n'
            f'      url: "https://www.gstatic.com/generate_204"\n'
            f'      interval: 600'
            for i, u in enumerate(self.sub_urls))
        uses = ", ".join(f"sub{i}" for i in range(len(self.sub_urls)))
        return (
            f"mixed-port: {self.mix_port}\n"
            # 显式禁 LAN + 只绑回环：把"只监听 127.0.0.1"从依赖上游默认值变成显式承诺
            f"allow-lan: false\n"
            f"bind-address: 127.0.0.1\n"
            f"external-controller: 127.0.0.1:{self.api_port}\n"
            f'secret: "{self.secret}"\n'
            f"log-level: warning\n"
            # 系统 DNS 可能被污染（订阅域名解析到假 IP），内置 DNS 走国内公共 DNS 直连解析
            f"dns:\n"
            f"  enable: true\n"
            f"  nameserver:\n"
            f"    - 223.5.5.5\n"
            f"    - 119.29.29.29\n"
            f"proxy-providers:\n{providers}\n"
            f"proxy-groups:\n"
            f'  - name: "{self.group}"\n'
            f"    type: select\n"
            f"    use: [{uses}]\n"
            f"rules:\n"
            f'  - "MATCH,{self.group}"\n'
        )

    def start(self, allow_download: bool = True) -> ClashCtl | None:
        """拉起核心子进程并轮询就绪（核心需先拉订阅，最长等 60s）；失败返回 None。

        幂等：已启动且存活（poll() 为 None）直接复用返回既有 ClashCtl，不重复拉起
        （web.py 重新生成 job 会反复 build_pool，进程内只允许一个内置核心）。
        整段在锁内：并发调用者阻塞等待首个调用拉起完成后走复用分支。
        """
        with self._start_lock:
            if self._proc is not None:
                if self._proc.poll() is None:
                    print(f"[ProxyCore] 内置核心已在运行（混合端口 127.0.0.1:{self.mix_port}），复用")
                    return ClashCtl(f"http://127.0.0.1:{self.api_port}", self.secret, self.group)
                self._proc = None       # 进程已死，按重新拉起处理（崩溃自愈）
            if not self.sub_urls:
                print("[ProxyCore] 无可用订阅链接（全部被剔除或未配置），禁用内置核心")
                return None
            bin_path = ensure_binary(allow_download=allow_download)
            if not bin_path:
                print("[ProxyCore] 未找到 mihomo 二进制（可设 MIHOMO_PATH 或放置到 vendor/），禁用内置核心")
                return None
            try:
                os.makedirs(_RUNTIME_DIR, exist_ok=True)
                cfg_path = os.path.join(_RUNTIME_DIR, "config.yaml")
                # os.open 带 0o600 创建：消除"先写后 chmod"的权限窗口（含订阅凭证）
                fd = os.open(cfg_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(self._gen_config())
                os.chmod(cfg_path, 0o600)   # 文件已存在时 os.open 的 mode 不生效，补收紧
            except OSError as e:
                print(f"[ProxyCore] 配置写入失败: {e}，禁用内置核心")
                return None
            try:
                self._proc = subprocess.Popen(
                    [bin_path, "-d", _RUNTIME_DIR, "-f", cfg_path],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    **self._pdeathsig_kwargs())
            except OSError as e:
                print(f"[ProxyCore] 核心启动失败: {e}")
                return None
            if not self._atexit_registered:
                atexit.register(self.stop)
                self._atexit_registered = True
            ctl = ClashCtl(f"http://127.0.0.1:{self.api_port}", self.secret, self.group)
            for _ in range(60):
                if self._proc.poll() is not None:
                    print(f"[ProxyCore] 核心进程退出（code={self._proc.returncode}），禁用内置核心")
                    self._proc = None
                    return None
                nodes = ctl.refresh_nodes() if ctl.available() else []
                if nodes:
                    print(f"[ProxyCore] 内置核心就绪: {len(nodes)} 个节点（混合端口 127.0.0.1:{self.mix_port}）")
                    return ctl
                time.sleep(1)
            print("[ProxyCore] 核心就绪超时（订阅拉取失败？），禁用内置核心")
            self._stop_locked()
            return None

    def _stop_locked(self):
        """终止核心子进程（幂等）；调用方须已持有 _start_lock。

        kill 后补 wait() 回收僵尸；停核心后删除含订阅凭证的 config.yaml。
        """
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)    # kill 后回收，避免僵尸进程
                except subprocess.TimeoutExpired:
                    pass
        try:
            os.remove(os.path.join(_RUNTIME_DIR, "config.yaml"))
        except OSError:
            pass

    def stop(self):
        """终止核心子进程（atexit 注册；幂等）；与 start() 同锁消除竞态"""
        with self._start_lock:
            self._stop_locked()


_CORE_INSTANCE: ProxyCore | None = None
_CORE_LOCK = threading.Lock()


def get_core(sub_urls: list[str], group: str = "profiler") -> ProxyCore:
    """模块级单例入口：同一进程只持有一个内置核心实例（配合 start() 幂等复用，
    web.py 多次重新生成 job 反复 build_pool 不会累积拉起多个 mihomo 子进程）。
    参数与既有实例不同则打警告并复用旧实例（同一进程内配置一般不会变，重建反而泄漏旧核心）。
    """
    global _CORE_INSTANCE
    with _CORE_LOCK:
        if _CORE_INSTANCE is None:
            _CORE_INSTANCE = ProxyCore(sub_urls, group=group)
        elif _CORE_INSTANCE._raw_sub_urls != list(sub_urls) or _CORE_INSTANCE.group != group:
            print("[ProxyCore] 警告：内置核心已按先前参数创建，本次参数不同，复用既有实例")
    return _CORE_INSTANCE
