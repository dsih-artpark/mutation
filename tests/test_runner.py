"""Safety properties of the stage runner.

These are the tests that matter most: every one corresponds to a defect found in
the original ONT delivery. If one fails, a class of bug has come back.

Since every stage now runs in a container, the execution tests use a real one.
`bash:5.2` is tiny and provides bash, sh, echo, printf, sleep and false. Tests
that only exercise validation or command construction need no Docker at all.
"""

import pytest

from app.config import MAX_STAGE_CPUS, MAX_STAGE_MEMORY_GB
from app.runtime.runner import StageRunner, docker_argv, sh
from app.runtime.spec import StageFailed, StageSpec, StageTimeout

BASH = "bash:5.2"


@pytest.fixture
def runner(tmp_path):
    return StageRunner("test1234", tmp_path, [(tmp_path, False)], tmp_path / "logs")


@pytest.fixture
def spec():
    return StageSpec(image=BASH, cpus=1, memory_gb=1, timeout_s=120)


# -- validation: no Docker needed -----------------------------------------

def test_shell_string_is_rejected(runner, spec):
    """argv must be a list. A string would imply shell interpretation."""
    with pytest.raises(TypeError, match="never a shell string"):
        runner.run("x", spec, "echo hello")


def test_non_string_argv_is_rejected(runner, spec):
    with pytest.raises(TypeError):
        runner.run("x", spec, ["echo", 42])


def test_malformed_env_key_is_rejected(runner, spec):
    with pytest.raises(ValueError, match="invalid environment variable name"):
        runner.run("x", spec, ["echo"], env={"BAD;NAME": "value"})


def test_sh_uses_strict_bash_flags():
    """pipefail matters: without it a failing tool mid-pipe is invisible."""
    argv = sh('echo "$X"')
    assert argv[0] == "bash"
    assert "-euo" in argv and "pipefail" in argv


# -- command construction: no Docker needed -------------------------------

def test_resources_are_clamped_to_the_ceiling(tmp_path):
    """A mis-declared spec must not be able to claim the whole box.

    Asserts on the command actually handed to Docker, not on a min() call.
    """
    cmd = docker_argv(
        "c", tmp_path, [], StageSpec(image=BASH, cpus=999, memory_gb=999), ["true"], {}
    )
    assert cmd[cmd.index("--cpus") + 1] == f"{MAX_STAGE_CPUS:g}"
    assert cmd[cmd.index("--memory") + 1] == f"{MAX_STAGE_MEMORY_GB:g}g"


def test_memory_swap_equals_memory_so_a_runaway_stage_is_oom_killed(tmp_path):
    cmd = docker_argv(
        "c", tmp_path, [], StageSpec(image=BASH, cpus=1, memory_gb=2), ["true"], {}
    )
    assert cmd[cmd.index("--memory") + 1] == cmd[cmd.index("--memory-swap") + 1]


def test_mounts_use_identical_host_and_container_paths(tmp_path):
    """Path parity: the daemon resolves bind sources on the host, so a path the
    worker computes must also be valid inside the container."""
    ro = tmp_path / "refs"
    ro.mkdir()
    cmd = docker_argv(
        "c", tmp_path, [(tmp_path, False), (ro, True)],
        StageSpec(image=BASH), ["true"], {},
    )
    assert f"{tmp_path}:{tmp_path}" in cmd
    assert f"{ro}:{ro}:ro" in cmd


def test_entrypoint_override_is_passed_through(tmp_path):
    cmd = docker_argv(
        "c", tmp_path, [], StageSpec(image=BASH, entrypoint="/bin/sh"), ["true"], {}
    )
    assert cmd[cmd.index("--entrypoint") + 1] == "/bin/sh"


def test_with_returns_a_modified_copy():
    base = StageSpec(image="a", cpus=2, memory_gb=4)
    bumped = base.with_(memory_gb=16)
    assert bumped.memory_gb == 16
    assert bumped.image == "a" and bumped.cpus == 2
    assert base.memory_gb == 4, "with_() mutated the original"


# -- execution: needs Docker ----------------------------------------------

@pytest.mark.docker
def test_shell_metacharacters_in_arguments_are_inert(runner, spec, tmp_path, docker_required):
    """The original interpolated filenames into a shell string; a filename
    containing `;` was remote code execution. Here it is just text."""
    output = tmp_path / "out.txt"
    canary = tmp_path / "canary"
    canary.write_text("untouched")

    runner.run("echo", spec, ["echo", f"x; rm -f {canary}"], stdout_path=output)

    assert output.read_text().strip() == f"x; rm -f {canary}"
    assert canary.read_text() == "untouched", "argument was interpreted by a shell"


@pytest.mark.docker
def test_env_values_are_not_reparsed(runner, spec, tmp_path, docker_required):
    """The sh() escape hatch: a constant script with data via quoted env vars."""
    output = tmp_path / "out.txt"
    canary = tmp_path / "canary2"
    canary.write_text("untouched")

    runner.run(
        "sh", spec, sh('printf "%s" "$VALUE"'),
        env={"VALUE": f"y; rm -f {canary}"},
        stdout_path=output,
    )

    assert output.read_text() == f"y; rm -f {canary}"
    assert canary.read_text() == "untouched"


@pytest.mark.docker
def test_non_zero_exit_raises_rather_than_exiting(runner, spec, docker_required):
    """The original called sys.exit(1), which kills the Celery worker."""
    with pytest.raises(StageFailed) as exc:
        runner.run("false", spec, ["false"])
    assert exc.value.exit_code == 1


@pytest.mark.docker
def test_failure_carries_the_log_tail(runner, spec, docker_required):
    with pytest.raises(StageFailed) as exc:
        runner.run("boom", spec, ["sh", "-c", "echo 'detailed reason' >&2; exit 7"])
    assert exc.value.exit_code == 7
    assert "detailed reason" in exc.value.log_tail


@pytest.mark.docker
def test_timeout_is_enforced(runner, docker_required):
    with pytest.raises(StageTimeout):
        runner.run("sleep", StageSpec(image=BASH, timeout_s=1), ["sleep", "30"])


@pytest.mark.docker
def test_pipefail_catches_a_failure_mid_pipe(runner, spec, docker_required):
    with pytest.raises(StageFailed):
        runner.run("pipe", spec, sh("false | cat"))


@pytest.mark.docker
def test_logs_are_written_per_stage(runner, spec, tmp_path, docker_required):
    runner.run("first", spec, ["echo", "hello"])
    logs = list((tmp_path / "logs").glob("*.log"))
    assert len(logs) == 1
    assert "first" in logs[0].name
    assert "echo" in logs[0].read_text()
