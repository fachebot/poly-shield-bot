from poly_shield.backend import server


def test_server_main_prints_wallet_context_before_startup(monkeypatch, capsys, tmp_path) -> None:
    captured = {}

    monkeypatch.setattr(server, "inspect_effective_signer", lambda: {
        "status": "valid",
        "source": "local-secret-store",
        "signer_address": "0x1111111111111111111111111111111111111111",
        "signature_type": "1",
        "signature_type_value": 1,
        "proxy_wallet_mode": True,
        "configured_funder": "0x2222222222222222222222222222222222222222",
        "configured_user_address": "0x2222222222222222222222222222222222222222",
        "relationship_note": "signer and funder may differ in proxy-wallet mode",
    })
    monkeypatch.setattr(server.TaskService, "from_db_path",
                        lambda path: object())
    monkeypatch.setattr(server, "build_default_runtime",
                        lambda service: object())
    monkeypatch.setattr(server, "create_app", lambda *args, **kwargs: object())

    def fake_uvicorn_run(app, host: str, port: int) -> None:
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setattr(server.uvicorn, "run", fake_uvicorn_run)

    exit_code = server.main(["--host", "127.0.0.1", "--port",
                            "8787", "--db-path", str(tmp_path / "poly-shield.db")])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Poly Shield Local Server" in output
    assert "Local URL: http://127.0.0.1:8787" in output
    assert "Signer: 0x1111111111111111111111111111111111111111" in output
    assert "Funder: 0x2222222222222222222222222222222222222222" in output
    assert "Proxy Wallet Mode: yes" in output
    assert captured == {"host": "127.0.0.1", "port": 8787}


def test_server_main_aborts_on_invalid_signer_configuration(monkeypatch, capsys, tmp_path) -> None:
    called = {"uvicorn_run": False}

    monkeypatch.setattr(server, "inspect_effective_signer", lambda: {
        "status": "valid",
        "source": "env",
        "signer_address": "0x1111111111111111111111111111111111111111",
        "signature_type": "0",
        "signature_type_value": 0,
        "proxy_wallet_mode": False,
        "configured_funder": "0x2222222222222222222222222222222222222222",
        "configured_user_address": None,
        "relationship_note": "signer usually matches funder in direct-wallet mode",
    })
    monkeypatch.setattr(server.TaskService, "from_db_path",
                        lambda path: object())
    monkeypatch.setattr(server, "build_default_runtime",
                        lambda service: object())
    monkeypatch.setattr(server, "create_app", lambda *args, **kwargs: object())

    def fake_uvicorn_run(app, host: str, port: int) -> None:
        called["uvicorn_run"] = True

    monkeypatch.setattr(server.uvicorn, "run", fake_uvicorn_run)

    exit_code = server.main(["--host", "127.0.0.1", "--port",
                            "8787", "--db-path", str(tmp_path / "poly-shield.db")])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "Signer Config Check: failed" in output
    assert "signer/funder mismatch in direct-wallet mode" in output
    assert "Startup aborted due to invalid signer/funder/signature_type configuration." in output
    assert called["uvicorn_run"] is False
