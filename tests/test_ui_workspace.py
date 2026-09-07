"""Static assets ship locally and the new shell retains the real review controls."""
from pathlib import Path
from fastapi.testclient import TestClient
from codeatlas.server.app import app


def test_workspace_local_assets_and_safe_static_mount():
    client = TestClient(app)
    html = client.get('/').text
    assert 'id="workspaceForm"' in html and 'data-tab="review"' in html
    assert 'expected_bundle_hash' in html and 'save_draft:false' in html
    assert 'data-action="review"' in html and 'aria-current="page"' in html
    for asset in ('workspace.css', 'workspace.js', 'icons/home.svg', 'icons/LICENSE'):
        assert client.get('/static/' + asset).status_code == 200
    assert client.get('/static/%2e%2e/app.py').status_code == 404
    assert client.get('/static/%2e%2e/%2e%2e/db.py').status_code == 404
    js = client.get('/static/workspace.js').text
    assert 'localStorage' not in js and 'sessionStorage' not in js
    assert 'expectedSet&&result.__responseSet!==expectedSet' in js


def test_workspace_runtime_assets_included_in_distribution():
    import tomllib
    root = Path(__file__).parents[1]
    config = tomllib.loads((root / 'pyproject.toml').read_text())
    assets = config['tool']['setuptools']['package-data']['codeatlas']
    assert 'server/static/*.css' in assets
    assert 'server/static/*.js' in assets
    assert 'server/static/icons/*.svg' in assets
    assert 'server/static/icons/LICENSE' in assets
