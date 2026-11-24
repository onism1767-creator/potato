"""向导:预算到顶后能看阶段性报告、能由用户选择是否调高上限接着跑;以及服务端健壮性(2026-11 终审)。

子进程里换不了适配器(mock 永远 $0,触发不了预算上限),所以这里分段测:
runner 对子进程输出的解析、续跑请求的校验与命令、服务端对意外异常的处理。全部离线、零成本。
"""

import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from aivis import cli
from aivis.configio import load_frozen_config
from aivis.gui import api, runner
from aivis.gui import server as server_mod
from aivis.gui.runner import JobStatus, Runner, _run_command, start_run, write_wizard_config
from aivis.gui.server import GuiServer, build_server
from tests.test_gui import _req, server  # noqa: F401  (server 是 pytest 夹具)

_BASE: dict[str, Any] = {
    "focal_name": "Acme Corp",
    "focal_domain": "acme.com",
    "category": "widgets",
    "questions": [
        {"id": "d01", "intent": "discovery", "text": "best widgets?"},
        {"id": "b01", "intent": "brand_defense", "text": "Is Acme Corp any good?"},
    ],
}
_RUN_ID = "0123456789abcdef0123456789abcdef"

# ---------------------------------------------------------------- runner:解析 BUDGET_STOP


class _FakeProc:
    """假子进程:吐预设的输出行,然后以给定退出码结束。"""

    def __init__(self, lines: list[str], rc: int) -> None:
        self.stdout = iter(line + "\n" for line in lines)
        self._rc = rc

    def wait(self) -> int:
        return self._rc


def _watch(lines: list[str], rc: int, tmp_path: Path) -> dict[str, Any]:
    r = Runner()
    job = JobStatus(job_id="j", state="running", provider="real", surfaces=["grok:grok-4.3"])
    r._job = job
    r._watch(_FakeProc(lines, rc), job, tmp_path)  # type: ignore[arg-type]
    return r.status()


def test_exit_code_matches_the_cli() -> None:
    assert runner._EXIT_BUDGET_STOP == cli.EXIT_BUDGET_STOP


def test_budget_stop_is_a_finished_run_with_a_choice_not_an_error(tmp_path: Path) -> None:
    """子进程以"预算到顶"退出(码 6):向导显示完成 + 阶段性报告 + 可续跑的信息,而不是报错。"""
    report = tmp_path / "report.html"
    report.write_text("<html>partial</html>", encoding="utf-8")
    st = _watch(
        [
            "PROGRESS probe 26/72 13.0200",
            f"REPORT {report}",
            f"BUDGET_STOP grok:grok-4.3 {_RUN_ID} 26/72 13.0200",
        ],
        cli.EXIT_BUDGET_STOP,
        tmp_path,
    )
    assert st["state"] == "done" and st["error"] is None
    assert st["report_path"] == str(report)
    assert st["budget_stops"] == [
        {
            "label": "grok:grok-4.3",
            "run_id": _RUN_ID,
            "done": 26,
            "total": 72,
            "spent": 13.02,
            "report_index": 0,
        }
    ]


def test_other_nonzero_exit_codes_are_still_errors(tmp_path: Path) -> None:
    st = _watch(["[引擎错误] 缺少密钥"], 4, tmp_path)
    assert st["state"] == "error" and "退出码 4" in st["error"]
    assert st["budget_stops"] == []


def test_run_command_can_resume_one_surface(tmp_path: Path) -> None:
    cmd = _run_command(
        config_dir=tmp_path, data_dir=tmp_path, surfaces=["grok:grok-4.3"], run_id=_RUN_ID
    )
    assert cmd[-2:] == ["--run-id", _RUN_ID]
    plain = _run_command(config_dir=tmp_path, data_dir=tmp_path, surfaces=["grok:grok-4.3"])
    assert "--run-id" not in plain


# ---------------------------------------------------------------- 续跑请求:只许改预算


class _RecordingRunner(Runner):
    def __init__(self) -> None:
        super().__init__()
        self.started: dict[str, Any] | None = None

    def start(self, **kwargs: Any) -> str:
        self.started = kwargs
        return "job"


def _frozen(work: Path, cap: float = 13.0) -> None:
    payload = {**_BASE, "budget_cap_usd": cap}
    write_wizard_config(work / "config", api._config_from(payload, api._questions_from(payload)))


def _resume(cap: Any, run_id: str = _RUN_ID) -> dict[str, Any]:
    return {
        "provider": "real",
        "budget_cap_usd": cap,
        "surfaces": ["grok:grok-4.3"],
        "api_keys": {"grok": "test-key"},
        "resume": {"surface": "grok:grok-4.3", "run_id": run_id},
    }


def test_resume_keeps_the_frozen_config_and_only_raises_the_cap(tmp_path: Path) -> None:
    _frozen(tmp_path, cap=13.0)
    before = load_frozen_config(tmp_path / "config")
    r = _RecordingRunner()
    assert start_run(r, _resume(40), tmp_path) == {"job_id": "job", "state": "running"}
    after = load_frozen_config(tmp_path / "config")
    assert after.budget_cap_usd == 40.0
    assert after.baseline_fingerprint() == before.baseline_fingerprint()  # 测量基线一字不变
    assert r.started is not None
    assert r.started["run_id"] == _RUN_ID and r.started["surfaces"] == ["grok:grok-4.3"]
    assert r.started["env_keys"] == {"XAI_API_KEY": "test-key"}  # 密钥只进子进程环境


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (_resume(40, run_id="../../etc"), "run_id 不合法"),
        (_resume(40, run_id=""), "run_id 不合法"),
        (_resume(13), "必须大于"),  # 没调高
        (_resume(5), "必须大于"),
        (_resume(None), "数字"),
        (_resume("1e999"), "有限"),
        ({**_resume(40), "api_keys": {}}, "api_key"),
        ({**_resume(40), "resume": "x"}, "resume"),
    ],
)
def test_resume_rejects_bad_requests_before_starting(
    payload: dict[str, Any], message: str, tmp_path: Path
) -> None:
    _frozen(tmp_path, cap=13.0)
    r = _RecordingRunner()
    with pytest.raises(ValueError, match=message):
        start_run(r, payload, tmp_path)
    assert r.started is None
    assert load_frozen_config(tmp_path / "config").budget_cap_usd == 13.0  # 配置没被动过


def test_resume_needs_the_previous_config(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="无法续跑"):
        start_run(_RecordingRunner(), _resume(40), tmp_path)


def test_a_second_run_cannot_overwrite_the_running_ones_config(tmp_path: Path) -> None:
    """已有任务在跑:新请求在写配置之前就被拒(以前是先覆盖配置再拒绝)。"""
    _frozen(tmp_path, cap=13.0)

    class _Busy(_RecordingRunner):
        def is_running(self) -> bool:
            return True

    with pytest.raises(ValueError, match="已有测量任务"):
        start_run(_Busy(), {**_BASE, "provider": "mock", "budget_cap_usd": 99}, tmp_path)
    assert load_frozen_config(tmp_path / "config").budget_cap_usd == 13.0


# ---------------------------------------------------------------- api:坏输入是干净的 400


@pytest.mark.parametrize("bad", [None, "abc", "1e999", "nan", [1]])
def test_bad_budget_is_a_clean_value_error(bad: Any) -> None:
    with pytest.raises(ValueError, match="预算上限"):
        api._config_from({**_BASE, "budget_cap_usd": bad}, api._questions_from(_BASE))


def test_competitors_must_be_objects() -> None:
    with pytest.raises(ValueError, match="competitors"):
        api._config_from({**_BASE, "competitors": ["x"]}, api._questions_from(_BASE))


def test_estimate_real_mode_sums_the_selected_models_not_a_mock_zero() -> None:
    """以前"真实测量"下点估算,后端回的是 mock 的 $0(绿色的"≈ $0")。现在估的是勾选的模型。"""
    one = api.estimate({**_BASE, "surfaces": ["anthropic:claude-haiku-4-5"]})
    two = api.estimate({**_BASE, "surfaces": ["anthropic:claude-haiku-4-5", "openai:gpt-6-luna"]})
    assert one["provider"] == "anthropic" and one["est_usd"] > 0
    assert two["provider"] == "multiple" and two["est_usd"] > one["est_usd"]
    assert api.estimate({**_BASE, "provider": "mock"})["est_usd"] == 0
    with pytest.raises(ValueError, match="请先选择"):
        api.estimate({**_BASE, "provider": "real"})  # 真实模式却一个都没选


def test_gui_does_not_branch_on_engine_names() -> None:
    """引擎差异只许在 providers/registry 里按名字分支(CLAUDE.md §4.3)。"""
    source = Path(api.__file__).read_text(encoding="utf-8")
    assert not re.search(r"provider\s*==\s*[\"'](anthropic|openai|gemini|perplexity|grok)", source)
    assert "MockAdapter" not in source and "AnthropicAdapter" not in source


# ---------------------------------------------------------------- server:意外异常不再断连接


def test_unexpected_error_returns_500_with_a_fixed_message(
    server: GuiServer,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """以前只接 ValueError/RuntimeError:别的异常直接断开连接,页面永远停在"处理中"。"""

    def boom(_payload: dict[str, Any]) -> dict[str, Any]:
        raise TypeError("secret-looking detail sk-should-not-leak")

    monkeypatch.setitem(server_mod._POST_ENDPOINTS, "/api/draft", boom)
    code, body = _req(server, "/api/draft", method="POST", token=server.token, body={})
    assert code == 500
    assert "内部出错" in json.loads(body)["error"]
    assert "sk-should-not-leak" not in body and "Traceback" not in body


def test_null_budget_is_a_400_not_a_dropped_connection(server: GuiServer) -> None:  # noqa: F811
    code, body = _req(
        server,
        "/api/models",
        method="POST",
        token=server.token,
        body={**_BASE, "budget_cap_usd": None},
    )
    assert code == 400 and "预算上限" in json.loads(body)["error"]


def test_non_ascii_token_is_refused_not_crashed(server: GuiServer) -> None:  # noqa: F811
    code, _ = _req(server, "/api/ping", method="POST", token="éé")
    assert code == 403


def test_report_endpoint_only_serves_files_inside_the_work_dir(
    server: GuiServer,  # noqa: F811
    tmp_path: Path,
) -> None:
    outside = tmp_path / "report.html"
    outside.write_text("<html>not ours</html>", encoding="utf-8")
    job = JobStatus(job_id="j", state="done", report_path=str(outside))
    server.runner._job = job
    code, _ = _req(server, "/report", token=server.token)
    assert code == 404
    inside = server.work_root / "report.html"
    inside.write_text("<html>ours</html>", encoding="utf-8")
    job.report_path = str(inside)
    code, body = _req(server, "/report", token=server.token)
    assert code == 200 and "ours" in body


@pytest.mark.skipif(sys.platform != "win32", reason="SO_REUSEADDR 的并绑语义只在 Windows 上有")
def test_second_wizard_on_the_same_port_is_refused_on_windows(
    server: GuiServer,  # noqa: F811
) -> None:
    """双击两次启动脚本:第二个实例必须报端口被占,而不是悄悄并绑出两个服务。"""
    with pytest.raises(OSError):
        build_server(port=server.server_address[1])


# ---------------------------------------------------------------- 页面:文案与两种语言


def _page() -> str:
    return (Path(server_mod.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")


def test_page_offers_the_choice_to_continue_in_both_languages() -> None:
    page = _page()
    for key in ("bs_title", "bs_body", "bs_open", "bs_cap", "bs_go", "bs_stop", "over_selected"):
        entry = re.search(rf"\n\s*{key}:\{{zh:'[^']+',\s*en:'[^']+'\}}", page)
        assert entry, f"i18n 键 {key} 缺中文或英文"
    assert "function doResume(" in page and "resume:{surface:b.label, run_id:b.run_id}" in page


def test_page_no_longer_overstates_what_the_wizard_does() -> None:
    page = _page()
    assert "不再变化</b>(稳定)为止" not in page  # 默认是固定 3 轮,不是"问到稳定为止"
    assert "被引用的页面可访问且确实点了该品牌" not in page  # 向导默认不抓页面
    # 第 3 步的密钥说明:现在是五家,各发各家(起草面板里那句"只发往 api.anthropic.com"仍然成立)
    notice2 = next(ln for ln in page.splitlines() if 'data-i18n-html="s3_notice2"' in ln)
    assert "api.anthropic.com" not in notice2
    assert "硬封顶" not in page and "hard-capped" not in page  # 到线后才停,在途的仍计费
    assert "到底连了哪里" not in page and "exactly where the run connected" not in page
