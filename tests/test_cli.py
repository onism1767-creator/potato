"""CLI 层测试:子命令编排、退出码、坏输入优雅处理(不吐裸 traceback)。"""

from pathlib import Path

from aivis import cli


def test_validate_missing_config_dir_exits_cleanly(tmp_path: Path) -> None:
    """回归:不存在的 --config-dir 应干净退出(非 0),不抛 FileNotFoundError。"""
    rc = cli.main(["validate", "--config-dir", str(tmp_path / "nope")])
    assert rc == 2


def test_init_missing_required_args_exits_2(tmp_path: Path) -> None:
    rc = cli.main(["init", "x", "--out", str(tmp_path)])  # 缺 --focal-name/--focal-domain
    assert rc == 2


def test_init_then_validate_happy_path(tmp_path: Path) -> None:
    rc = cli.main(
        [
            "init",
            "acme",
            "--out",
            str(tmp_path),
            "--focal-name",
            "Acme Corp",
            "--focal-domain",
            "acme.com",
            "--category",
            "CRM software",
        ]
    )
    assert rc == 0
    cdir = tmp_path / "acme"
    assert (cdir / "questions.yml").exists() and (cdir / "brands.yml").exists()
    assert cli.main(["validate", "--config-dir", str(cdir), "--phase", "final"]) == 0


def test_estimate_command_runs(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    """预算估计:零成本不联网,打印估算与预算对比。"""
    cli.main(
        [
            "init",
            "acme",
            "--out",
            str(tmp_path),
            "--focal-name",
            "Acme Corp",
            "--focal-domain",
            "acme.com",
        ]
    )
    rc = cli.main(["estimate", "--config-dir", str(tmp_path / "acme"), "--provider", "anthropic"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "预算估计" in out and "$" in out and "调用数" in out


def test_setup_to_run_end_to_end_on_mock(tmp_path: Path) -> None:
    """SETUP→MONITORING 整条(mock,零成本):init→validate→run 出报告,焦点品牌进报告。"""
    out = tmp_path / "cfg"
    assert (
        cli.main(
            [
                "init",
                "acme",
                "--out",
                str(out),
                "--focal-name",
                "Acme Corp",
                "--focal-domain",
                "acme.com",
                "--category",
                "CRM software",
            ]
        )
        == 0
    )
    cdir = out / "acme"
    assert cli.main(["validate", "--config-dir", str(cdir), "--phase", "final"]) == 0
    data = tmp_path / "data"
    assert cli.main(["run", "--config-dir", str(cdir), "--data-dir", str(data)]) == 0
    reports = list((data / "analysis").glob("*/report.md"))
    assert len(reports) == 1
    assert "Acme Corp" in reports[0].read_text(encoding="utf-8")  # 焦点品牌进了报告


def test_sequential_flag_sets_run_lock(tmp_path: Path) -> None:
    """--sequential 覆盖配置:run 后 RunLock 记 sequential=True(精修:CLI 开关替代改 yaml)。"""
    from aivis.storage.raw import RawStore

    data = tmp_path / "data"
    assert (
        cli.main(["run", "--config-dir", "configs/demo", "--data-dir", str(data), "--sequential"])
        == 0
    )
    raw = RawStore(data / "raw")
    rid = raw.read_manifests()[-1].run_id
    lock = raw.read_lock(rid)
    assert lock is not None and lock.sequential is True


def test_init_refuses_existing_dir_exits_2(tmp_path: Path) -> None:
    (tmp_path / "acme").mkdir()
    rc = cli.main(
        [
            "init",
            "acme",
            "--out",
            str(tmp_path),
            "--focal-name",
            "Acme Corp",
            "--focal-domain",
            "acme.com",
        ]
    )
    assert rc == 2


def test_run_refuses_a_misconfigured_dir_before_any_call(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    """两道题同一个 id:开跑前就拦下(退出码 2),点名是哪个 id,数据目录都还没建。"""
    import shutil

    bad = tmp_path / "config"
    shutil.copytree("configs/demo", bad)
    questions = bad / "questions.yml"
    text = questions.read_text(encoding="utf-8")
    questions.write_text(text.replace('id: "d02"', 'id: "d01"', 1), encoding="utf-8")
    data = tmp_path / "data"
    rc = cli.main(["run", "--config-dir", str(bad), "--data-dir", str(data), "--yes"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "[配置错误]" in err and "题目 id 重复:d01" in err
    assert "validation error" not in err
    assert not data.exists()
