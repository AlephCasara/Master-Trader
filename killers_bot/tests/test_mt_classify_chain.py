"""Cadeia de fallback do mt-classify (bd Master-Trader-yun).

O que precisa ser provado:
- a ordem da cadeia (primária primeiro) e a resolução de endpoint/bearer_env/
  rpd/response_format de cada entrada de MT_CLASSIFY_FALLBACKS
- um candidato com contador no limite ou marcado exhausted é pulado; a
  primária (sem rpd) nunca é pulada por cota
- linhas de estado datadas de outro dia (Pacific) recomeçam zeradas
- 429 marca o modelo exhausted; só chamada HTTP 200 completa gasta contador
- main(): todos os candidatos falhando sai com exit 1 e o ÚLTIMO erro no
  stderr; um 429 no meio da cadeia entrega a resposta do próximo candidato
"""
import importlib.machinery
import importlib.util
import io
import json
import sys
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "killers_bot" / "mt-classify"

# "mt-classify" tem um traço no nome: carrega como módulo via loader explícito.
_spec = importlib.util.spec_from_loader(
    "mt_classify",
    importlib.machinery.SourceFileLoader("mt_classify", str(SCRIPT)))
mtc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mtc)

CHAIN_ENV = {
    "MT_CLASSIFY_ENDPOINT": "https://home.example/v1/chat/completions",
    "MT_CLASSIFY_BEARER": "primary-key",
    "MT_CLASSIFY_DEFAULT_MODEL": "qwen3-4b",
    "MT_CLASSIFY_FALLBACKS": json.dumps([
        {"model": "gemini-3.5-flash-lite", "bearer_env": "MT_GEMINI_KEY",
         "rpd": 480},
        {"model": "gemma-4-26b", "response_format": False},
    ]),
}


def _today():
    return mtc.pacific_today()


def _yesterday():
    return (datetime.now(mtc.PACIFIC) - timedelta(days=1)).date().isoformat()


# ── construção da cadeia ────────────────────────────────────────────────────

def test_ordem_da_cadeia_e_resolucao_dos_campos():
    env = dict(CHAIN_ENV, MT_GEMINI_KEY="gemini-key")
    cands = mtc.build_candidates(env)
    assert [c["model"] for c in cands] == [
        "qwen3-4b", "gemini-3.5-flash-lite", "gemma-4-26b"]

    primary, fb1, fb2 = cands
    # primária: endpoint/bearer do env, sem limite de cota
    assert primary["endpoint"] == env["MT_CLASSIFY_ENDPOINT"]
    assert primary["bearer"] == "primary-key"
    assert primary["rpd"] is None and primary["response_format"] is True

    # bearer_env resolve a chave do candidato; sem endpoint, herda o primário
    assert fb1["bearer"] == "gemini-key"
    assert fb1["endpoint"] == env["MT_CLASSIFY_ENDPOINT"]
    assert fb1["rpd"] == 480 and fb1["response_format"] is True

    # sem bearer_env volta ao bearer primário; sem rpd = ilimitado
    assert fb2["bearer"] == "primary-key"
    assert fb2["rpd"] is None and fb2["response_format"] is False


def test_bearer_env_vazio_cai_no_bearer_primario():
    env = dict(CHAIN_ENV)  # MT_GEMINI_KEY ausente
    fb1 = mtc.build_candidates(env)[1]
    assert fb1["bearer"] == "primary-key"


def test_model_do_cli_so_afeta_a_primaria():
    env = dict(CHAIN_ENV, MT_GEMINI_KEY="k")
    cands = mtc.build_candidates(env, requested_model="gpt-oss-20b")
    assert cands[0]["model"] == "gpt-oss-20b"
    assert [c["model"] for c in cands[1:]] == [
        "gemini-3.5-flash-lite", "gemma-4-26b"]


def test_fallbacks_invalidos_degradam_para_somente_primaria():
    for raw in ("{nao e json", '{"model": "x"}'):
        cands = mtc.build_candidates(dict(CHAIN_ENV, MT_CLASSIFY_FALLBACKS=raw))
        assert len(cands) == 1 and cands[0]["model"] == "qwen3-4b"


# ── seleção por cota ────────────────────────────────────────────────────────

def _cands():
    return mtc.build_candidates(dict(CHAIN_ENV, MT_GEMINI_KEY="k"))


def test_contador_no_limite_e_pulado():
    # a primária sempre tenta primeiro; a cota decide a partir dos fallbacks
    state = {"gemini-3.5-flash-lite": {"date": _today(), "count": 480,
                                       "exhausted": False}}
    assert mtc.select_candidate(state, _cands())["model"] == "qwen3-4b"
    assert mtc.select_candidate(
        state, _cands()[1:])["model"] == "gemma-4-26b"


def test_exhausted_e_pulado_antes_do_contador():
    state = {"gemini-3.5-flash-lite": {"date": _today(), "count": 3,
                                       "exhausted": True}}
    assert mtc.select_candidate(
        state, _cands()[1:])["model"] == "gemma-4-26b"


def test_primaria_sem_rpd_nunca_e_pulada_por_cota():
    state = {"qwen3-4b": {"date": _today(), "count": 10_000,
                           "exhausted": True}}
    assert mtc.select_candidate(state, _cands())["model"] == "qwen3-4b"


def test_todos_em_cota_devolve_none():
    state = {"gemini-3.5-flash-lite": {"date": _today(), "count": 480,
                                       "exhausted": False},
             "gemma-4-26b": {"date": _today(), "count": 0,
                             "exhausted": True}}
    # 429 marcou a gemma exhausted: mesmo sem rpd declarado, fica de fora
    assert mtc.select_candidate(state, _cands()[1:]) is None


# ── estado: dia Pacific, rollover, outcome ─────────────────────────────────

def test_data_pacific_no_formato_iso():
    import re
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", mtc.pacific_today())


def test_rollover_de_dia_reseta_contadores(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({
        "gemini-3.5-flash-lite": {"date": _yesterday(), "count": 480,
                                  "exhausted": True},
        "gemma-4-26b": {"date": _today(), "count": 7, "exhausted": False},
    }))
    state = mtc.load_state(p)
    # linha de ontem sumiu; a de hoje sobrevive intacta
    assert "gemini-3.5-flash-lite" not in state
    assert state["gemma-4-26b"]["count"] == 7


def test_apply_outcome_com_linha_de_outro_dia_recomeca():
    state = {"m": {"date": _yesterday(), "count": 999, "exhausted": True}}
    mtc.apply_outcome(state, "m", mtc.OUTCOME_COMPLETED, today=_today())
    assert state["m"] == {"date": _today(), "count": 1,
                          "exhausted": False}


def test_so_chamada_200_completa_gasta_contador():
    state = {}
    mtc.apply_outcome(state, "m", mtc.OUTCOME_FAILED)
    assert state["m"]["count"] == 0
    mtc.apply_outcome(state, "m", mtc.OUTCOME_COMPLETED)
    assert state["m"]["count"] == 1


def test_429_marca_exhausted_sem_gastar_contador():
    state = {"m": {"date": _today(), "count": 4, "exhausted": False}}
    mtc.apply_outcome(state, "m", mtc.OUTCOME_RATE_LIMITED)
    assert state["m"]["exhausted"] is True
    assert state["m"]["count"] == 4
    cand = {"model": "m", "endpoint": "e", "bearer": "", "rpd": 5,
            "response_format": True}
    assert not mtc.candidate_available(state, cand)


def test_estado_ausente_ou_corrompido_comeca_fresco(tmp_path):
    assert mtc.load_state(tmp_path / "nao_existe.json") == {}
    p = tmp_path / "state.json"
    p.write_text("{nao e json")
    assert mtc.load_state(p) == {}


def test_caminho_de_estado_inserivel_cai_no_tmp(tmp_path):
    assert mtc.resolve_state_path(
        str(tmp_path / "dir_que_nao_existe" / "s.json")) == \
        mtc.FALLBACK_STATE_PATH
    writable = tmp_path / "s.json"
    assert mtc.resolve_state_path(str(writable)) == str(writable)


# ── main(): contrato CLI atravessando a cadeia ──────────────────────────────

class _Resp:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ok_payload(text):
    return {"choices": [{"message": {"content": text}}]}


def _http_error(code, body=b"boom"):
    return urllib.error.HTTPError("https://x", code, "err", {},
                                  io.BytesIO(body))


def _raise_exit(code=0):
    raise SystemExit(code)


def _run_main(monkeypatch, env, replies, calls):
    """replies: dict model -> valor retornado por urlopen (payload ou
    exceção). Registra em `calls` os corpos das requisições por modelo."""

    def fake_urlopen(req, timeout=None):
        model = json.loads(req.data)["model"]
        calls.append({"model": model, "body": json.loads(req.data),
                      "url": req.full_url})
        reply = replies[model]
        if isinstance(reply, Exception):
            raise reply
        return _Resp(reply)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    for var, val in env.items():
        monkeypatch.setenv(var, val)
    monkeypatch.setattr(sys, "argv", ["mt-classify", "-p", "classifique"])
    mtc.main()


def test_main_429_e_5xx_no_meio_entregam_o_proximo_candidato(
        monkeypatch, tmp_path, capsys):
    state = tmp_path / "state.json"
    env = dict(CHAIN_ENV, MT_GEMINI_KEY="k",
               MT_CLASSIFY_STATE=str(state))
    calls = []
    _run_main(monkeypatch, env, {
        "qwen3-4b": _http_error(429, b"quota exceeded"),
        "gemini-3.5-flash-lite": _http_error(500, b"upstream broke"),
        "gemma-4-26b": _ok_payload('{"kind": "chat"}'),
    }, calls)
    out = capsys.readouterr()
    assert out.out.strip() == '{"kind": "chat"}'
    assert [c["model"] for c in calls] == [
        "qwen3-4b", "gemini-3.5-flash-lite", "gemma-4-26b"]
    assert "429" in out.err and "exhausted" in out.err
    persisted = json.loads(state.read_text())
    assert persisted["qwen3-4b"]["exhausted"] is True
    assert persisted["gemma-4-26b"]["count"] == 1
    # 5xx não gasta cota nem marca exhausted
    assert "gemini-3.5-flash-lite" not in persisted
    # contrato de request preservado: sampling Qwen + json_object na primária
    assert calls[0]["body"]["temperature"] == 0.6
    assert calls[0]["body"]["response_format"] == {"type": "json_object"}
    # entrada com response_format=false não pede json_object (nem sampling Qwen)
    assert "response_format" not in calls[2]["body"]
    assert calls[2]["body"]["temperature"] == 0


def test_main_falha_total_exit_1_com_ultimo_erro(monkeypatch, tmp_path, capsys):
    state = tmp_path / "state.json"
    env = dict(CHAIN_ENV, MT_GEMINI_KEY="k",
               MT_CLASSIFY_STATE=str(state))
    replies = {m: _http_error(500, b"upstream broke")
               for m in ("qwen3-4b", "gemini-3.5-flash-lite", "gemma-4-26b")}
    replies["qwen3-4b"] = urllib.request.URLError("home box off")
    monkeypatch.setattr(sys, "exit", _raise_exit)
    calls = []
    with pytest.raises(SystemExit) as ei:
        _run_main(monkeypatch, env, replies, calls)
    assert ei.value.code == 1
    out = capsys.readouterr()
    assert [c["model"] for c in calls] == [
        "qwen3-4b", "gemini-3.5-flash-lite", "gemma-4-26b"]
    # o ÚLTIMO erro é o reportado, no formato stderr existente
    assert "HTTP 500" in out.err and out.err.strip().splitlines()[-1] == \
        "mt-classify: HTTP 500: b'upstream broke'"
    # 5xx não gasta cota: nenhum contador sequer é persistido
    assert not state.exists() or not json.loads(state.read_text())


def test_main_sem_endpoint_exit_2(monkeypatch, capsys):
    monkeypatch.delenv("MT_CLASSIFY_ENDPOINT", raising=False)
    monkeypatch.setattr(sys, "argv", ["mt-classify", "-p", "x"])
    monkeypatch.setattr(sys, "exit", _raise_exit)
    with pytest.raises(SystemExit) as ei:
        mtc.main()
    assert ei.value.code == 2
    assert "MT_CLASSIFY_ENDPOINT" in capsys.readouterr().err
