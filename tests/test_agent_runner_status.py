from service.agents import runner


def test_runner_status_is_sanitized(monkeypatch):
    monkeypatch.setenv("DIFFUSE_AGENT_RUNTIME", "claude")
    monkeypatch.setenv("DIFFUSE_RUNNER_IMAGE_VERSION", "release-digest")
    monkeypatch.setattr(
        runner,
        "cli_status",
        lambda _cli: {
            "ready": False,
            "installed": True,
            "authenticated": False,
            "version": "2.1.224",
            "sandbox_settings_current": True,
            "account": "must-not-leak@example.com",
            "problem": "vendor device code ABCD",
        },
    )

    status = runner.runner_status()

    assert status == {
        "runtime": "claude",
        "image_version": "release-digest",
        "cli_version": "2.1.224",
        "policy_state": "current",
        "state": "not_logged_in",
    }
